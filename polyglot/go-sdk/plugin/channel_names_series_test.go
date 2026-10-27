package plugin

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestReaderChannelNamesValidationAndCleanup(t *testing.T) {
	for _, routed := range []bool{false, true} {
		for _, series := range []bool{false, true} {
			t.Run(strings.Join([]string{map[bool]string{false: "singular", true: "routed"}[routed], map[bool]string{false: "open", true: "series"}[series]}, "/"), func(t *testing.T) {
				var closed []string
				open := func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
					names := []string{"red", ""}
					if q.Uri == "bad" {
						names = append(names, "extra")
					}
					if q.Uri == "short" {
						names = names[:1]
					}
					if q.Uri == "none" {
						names = nil
					}
					return &pb.ReaderOpened{Handle: q.Uri, ShapeTczyx: []uint32{1, 2, 1, 1, 1}, Dtype: pb.DType_DTYPE_U8, ChannelNames: names}, nil
				}
				h := ReaderHandlers{Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
					closed = append(closed, q.Handle)
					return &pb.Closed{Handle: q.Handle}, nil
				}, Metadata: func(_ context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
					return &pb.MetadataResult{Text: q.Handle}, nil
				}}
				if series {
					h.OpenSeries = func(ctx context.Context, c *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
						o, e := open(ctx, c, q)
						return ReaderOpenResult{Opened: o}, e
					}
				} else {
					h.Open = open
				}
				handlers := Handlers{Reader: h}
				if routed {
					handlers = Handlers{Readers: map[string]ReaderHandlers{"r": h}}
				}
				cfg, err := handlers.Configure(control.Config{})
				if err != nil {
					t.Fatal(err)
				}
				old, err := seriesCall(t, cfg, "old", "r", nil)
				if err != nil {
					t.Fatal(err)
				}
				if !reflect.DeepEqual(old.GetReaderOpened().ChannelNames, []string{"red", ""}) {
					t.Fatal(old)
				}
				out, err := seriesCall(t, cfg, "bad", "r", nil)
				var refusal *control.HostError
				if out != nil || !errors.As(err, &refusal) || refusal.Kind != pb.Error_INVALID_ARGUMENT || refusal.Message != "open failed: the reader's ReaderInfo is not valid: channel_names names 3 channels of 2" {
					t.Fatal(out, err)
				}
				if !reflect.DeepEqual(closed, []string{"bad"}) {
					t.Fatal("bad reader must close, previous must survive", closed)
				}
				meta, err := cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: old.GetReaderOpened().Handle}}})
				if err != nil || meta.GetMetadata().Text != "old" {
					t.Fatal(meta, err)
				}
				for _, uri := range []string{"short", "none"} {
					if _, err = seriesCall(t, cfg, uri, "r", nil); err != nil {
						t.Fatal(err)
					}
				}
			})
		}
	}
}

func TestDuplicateSeriesIDsCloseListing(t *testing.T) {
	for _, id := range []string{"x", ""} {
		closed := 0
		cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": {OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
			return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: id}, {ID: id}}, Close: func() error { closed++; return nil }}}, nil
		}}}}).Configure(control.Config{})
		if err != nil {
			t.Fatal(err)
		}
		out, err := seriesCall(t, cfg, "many", "r", nil)
		var refusal *control.HostError
		if out != nil || !errors.As(err, &refusal) || refusal.Kind != pb.Error_INVALID_ARGUMENT || refusal.Message != "open failed: the reader's SeriesChoice is not valid: series id '"+id+"' is listed twice" || closed != 1 {
			t.Fatal(out, err, closed)
		}
	}
	// Even a duplicate beyond the prefix that fits must refuse the whole list.
	entries := make([]SeriesEntry, 40)
	for i := range entries {
		entries[i] = SeriesEntry{ID: strings.Repeat("x", i+1), Description: strings.Repeat("d", 1<<20)}
	}
	entries[39].ID = entries[0].ID
	if _, err := seriesList(&SeriesChoice{Entries: entries}); err == nil {
		t.Fatal("duplicate beyond trim accepted")
	}
}

func TestExportSourceDescriptorIsDeliveredWithoutParsing(t *testing.T) {
	for _, raw := range []string{`{"source":{"uri":"x"}}`, `[]`, `null`, `{invalid`, ""} {
		expected := &pb.OpenExport{SourceMetadata: &pb.MetadataDoc{Form: "ome-xml", Text: "source document"}, Provenance: raw, Channels: []*pb.ChannelInfo{{Index: 5, Name: "green", Source: pb.ChannelNameSource_CHANNEL_NAME_SOURCE_FILE}, {Index: 9, Name: "Channel 10", Source: pb.ChannelNameSource_CHANNEL_NAME_SOURCE_PLACEHOLDER}}}
		called := false
		cfg, err := (Handlers{Export: ExportHandlers{Open: func(_ context.Context, _ *control.Client, q *pb.OpenExport) (*pb.ExportOpened, error) {
			called = true
			if !proto.Equal(q, expected) || !q.Channels[0].Declared() || q.Channels[1].Declared() {
				t.Fatal(q)
			}
			return &pb.ExportOpened{Handle: "e"}, nil
		}}}).Configure(control.Config{})
		if err != nil {
			t.Fatal(err)
		}
		wire, err := proto.Marshal(&pb.HostMsg{Msg: &pb.HostMsg_OpenExport{OpenExport: expected}})
		if err != nil {
			t.Fatal(err)
		}
		var request pb.HostMsg
		if err = proto.Unmarshal(wire, &request); err != nil {
			t.Fatal(err)
		}
		if _, err = cfg.HandleRequest(context.Background(), nil, &request); err != nil || !called {
			t.Fatal(err, called)
		}
	}
}
