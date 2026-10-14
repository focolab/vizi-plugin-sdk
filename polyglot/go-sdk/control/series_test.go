package control

import (
	"context"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestSeriesListIsOnlyAnOpenReaderReply(t *testing.T) {
	p := openPeer(t, Config{HandleRequest: func(_ context.Context, _ *Client, q *pb.HostMsg) (*pb.PluginMsg, error) {
		if open := q.GetOpenReader(); open != nil && (open.Series == nil || *open.Series != "") {
			t.Error("lost explicitly selected empty series ID")
		}
		return &pb.PluginMsg{Msg: &pb.PluginMsg_SeriesList{SeriesList: &pb.SeriesList{Entries: []*pb.SeriesEntry{{Id: "a"}, {Id: "b"}}}}}, nil
	}}, nil)
	p.send(t, &pb.HostMsg{Id: "open", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Series: proto.String("")}}})
	if got := p.recv(t); got.Id != "open" || len(got.GetSeriesList().GetEntries()) != 2 {
		t.Fatal(got)
	}
	p.send(t, &pb.HostMsg{Id: "wrong", Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{}}})
	if got := p.recv(t); got.Id != "wrong" || got.GetError().GetKind() != pb.Error_INTERNAL || !strings.Contains(got.GetError().GetMessage(), "wrong reply kind") {
		t.Fatal(got)
	}
	if got := pluginRoutes["series_list"].class; got != ReplyClass {
		t.Fatal(got)
	}
}

func TestSeriesListRepeatedBound(t *testing.T) {
	if MaxSeries != 1<<22 {
		t.Fatal(MaxSeries)
	}
	entry := &pb.SeriesEntry{Id: strings.Repeat("i", 20), Label: strings.Repeat("l", 20)}
	for _, count := range []int{60000, 70000} {
		list := &pb.SeriesList{Entries: make([]*pb.SeriesEntry, count)}
		for i := range list.Entries {
			list.Entries[i] = entry
		}
		if err := ValidateSize(&pb.PluginMsg{Id: "open", Msg: &pb.PluginMsg_SeriesList{SeriesList: list}}); err != nil {
			t.Fatal(count, err)
		}
	}
}

func TestViewStateExposesDatasetSeries(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	for _, series := range []*string{nil, proto.String(""), proto.String("/images/cell-1")} {
		done := make(chan *pb.ViewState, 1)
		go func() {
			v, err := p.client.GetViewState(p.ctx)
			if err != nil {
				t.Error(err)
			}
			done <- v
		}()
		request := p.recv(t)
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{DatasetSeries: series, SelectedRegionId: "r", Channels: []*pb.ChannelInfo{{Index: 0, Name: "Channel 1", Source: pb.ChannelNameSource_CHANNEL_NAME_SOURCE_PLACEHOLDER}}}}})
		select {
		case v := <-done:
			if (v.DatasetSeries == nil) != (series == nil) || (series != nil && v.GetDatasetSeries() != *series) || v.GetSelectedRegionId() != "r" || len(v.Channels) != 1 || v.Channels[0].Declared() || v.Channels[0].Name != "Channel 1" {
				t.Fatal(v)
			}
		case <-p.ctx.Done():
			t.Fatal(p.ctx.Err())
		}
	}
}
