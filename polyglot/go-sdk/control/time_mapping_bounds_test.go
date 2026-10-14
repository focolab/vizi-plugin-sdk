package control

import (
	"context"
	"math"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestMalformedTimeMappingInstallsAcknowledgesAndBindsContent(t *testing.T) {
	for _, values := range [][]float64{{2, 1}, {math.NaN()}, {math.Inf(1)}} {
		reporter, err := violations.New(violations.Config{})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(reporter.Close)
		s := NewState(&lifecycle{}, reporter)
		c := stateClient(t)
		c.diagnostics = make(chan error, 4)
		mapping := &pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: 7, Revision: 1, T: values}}}
		for i := 0; i < 2; i++ {
			if err := s.Apply(c, mapping); err != nil {
				t.Fatal(err)
			}
			if ack := (<-c.out).msg.GetTimeMappingAck(); ack.GetGen() != 7 || ack.GetRevision() != 1 {
				t.Fatal(ack)
			}
		}
		if len(c.diagnostics) != 1 || !strings.Contains((<-c.diagnostics).Error(), "installed") {
			t.Fatal("missing local diagnostic")
		}
		for site, stats := range reporter.Stats() {
			if stats.Observed != 0 {
				t.Fatal(site, stats)
			}
		}
		epoch := &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{
			Datasets: []*pb.DatasetState{{Gen: 7, Geometry: &pb.Geometry{Gen: 7}, TimeMappingRevision: 1}},
			Panes:    []*pb.PaneEpoch{{ViewId: 0, Epoch: 1, Gen: 7}},
		}}}
		if err := s.Apply(c, epoch); err != nil {
			t.Fatal(err)
		}
		if ack := (<-c.out).msg.GetEpochAck(); len(ack.GetPanes()) != 1 {
			t.Fatal(ack)
		}
		if !proto.Equal(s.Pane(0).Time, mapping.GetTimeMapping()) {
			t.Fatal("index-wise time values lost")
		}
		changed := proto.Clone(mapping).(*pb.HostMsg)
		changed.GetTimeMapping().T = []float64{1, 2}
		if err := s.Apply(c, changed); err == nil {
			t.Fatal("changed replay accepted")
		}
		if stats := reporter.Stats()[violations.ArrangementContentMismatch]; stats.Observed != 1 {
			t.Fatal(stats)
		}
		if len(c.out) != 0 {
			t.Fatal("conflict acknowledged")
		}
	}
}

func TestStringByteBoundOutboundAndInbound(t *testing.T) {
	for _, n := range []int{MaxInlineBytes, MaxInlineBytes + 1} {
		// A multibyte string ensures the bound counts UTF-8 bytes, not runes.
		value := strings.Repeat("é", n/2) + strings.Repeat("x", n%2)
		out := &pb.PluginMsg{Id: "metadata", Msg: &pb.PluginMsg_Metadata{Metadata: &pb.MetadataResult{Text: value}}}
		if err := stateClient(t).validateOutbound(out); (err != nil) != (n > MaxInlineBytes) {
			t.Fatal(n, err)
		}
		labels := &pb.OverlayLabelVolume{LabelNames: map[uint32]string{257: value}}
		if err := ValidateSize(labels); (err != nil) != (n > MaxInlineBytes) {
			t.Fatal(n, err)
		}
		p := openPeer(t, Config{HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportOpened{ExportOpened: &pb.ExportOpened{Handle: "ok"}}}, nil
		}}, nil)
		p.send(t, &pb.HostMsg{Id: "open", Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{SourceMetadata: &pb.MetadataDoc{Text: value}}}})
		reply := p.recv(t)
		if reply.Id != "open" {
			t.Fatal(reply.Id)
		}
		if n > MaxInlineBytes {
			if reply.GetError().GetKind() != pb.Error_PROTOCOL {
				t.Fatal(reply)
			}
		} else if reply.GetExportOpened().GetHandle() != "ok" {
			t.Fatal(reply)
		}
		p.send(t, &pb.HostMsg{Id: "next", Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{}}})
		if next := p.recv(t); next.Id != "next" || next.GetExportOpened().GetHandle() != "ok" {
			t.Fatal("bound refusal closed or disrupted session", next)
		}
	}
}
