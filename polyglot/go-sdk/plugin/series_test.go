package plugin

import (
	"context"
	"errors"
	"fmt"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestSeriesListValidationAndMessagePrefix(t *testing.T) {
	for _, entries := range [][]SeriesEntry{
		nil, {{ID: "a", ShapeTCZYX: []uint32{}}}, {{ID: "a", ShapeTCZYX: []uint32{1, 1, 0, 1, 1}}},
		{{ID: "a", DType: pb.DType_DTYPE_F64}}, {{ID: "a", DType: pb.DType(999)}}, {{ID: string([]byte{0xff})}},
	} {
		if _, err := seriesList(&SeriesChoice{Entries: entries}); err == nil {
			t.Fatal("bad choice accepted", entries)
		}
	}
	entries := make([]SeriesEntry, 60000)
	for i := range entries {
		entries[i] = SeriesEntry{ID: fmt.Sprintf("%020d", i), Label: strings.Repeat("L", 20)}
	}
	list, err := seriesList(&SeriesChoice{Entries: entries})
	if err != nil || list.Truncated || len(list.Entries) != 60000 {
		t.Fatal("large valid listing refused", err)
	}
	if err := control.ValidateSize(&pb.PluginMsg{Msg: &pb.PluginMsg_SeriesList{SeriesList: list}}); err != nil {
		t.Fatal(err)
	}
	// Shared source strings keep the test small while the wire size exceeds 32 MiB.
	entries = make([]SeriesEntry, 40)
	description := strings.Repeat("d", 1<<20)
	for i := range entries {
		entries[i] = SeriesEntry{ID: fmt.Sprint(i), Description: description}
	}
	list, err = seriesList(&SeriesChoice{Entries: entries})
	if err != nil || !list.Truncated || len(list.Entries) != 31 {
		t.Fatalf("prefix len=%d truncated=%v err=%v", len(list.GetEntries()), list.GetTruncated(), err)
	}
	for i, entry := range list.Entries {
		if entry.Id != fmt.Sprint(i) {
			t.Fatal("reordered prefix", i, entry.Id)
		}
	}
	wire := &pb.PluginMsg{Id: "open", Msg: &pb.PluginMsg_SeriesList{SeriesList: list}}
	if err := control.ValidateSize(wire); err != nil {
		t.Fatal(err)
	}
	encoded, err := proto.Marshal(wire)
	if err != nil || len(encoded) > control.MaxMessageBytes {
		t.Fatal(len(encoded), err)
	}
}

func seriesCall(t *testing.T, cfg control.Config, uri, reader string, series *string) (*pb.PluginMsg, error) {
	t.Helper()
	return cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: uri, ReaderId: reader, Series: series}}})
}

func TestSeriesExchangePreservesReaderAndReusesListing(t *testing.T) {
	var events []string
	serial := 0
	h := ReaderHandlers{Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
		events = append(events, "close "+q.Handle)
		return &pb.Closed{Handle: q.Handle}, nil
	}}
	h.OpenSeries = func(_ context.Context, _ *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
		if q.ReaderId != "r" {
			t.Fatal("callback not routed", q)
		}
		serial++
		local := fmt.Sprintf("instance-%d", serial)
		events = append(events, "new "+q.Uri)
		if q.Uri == "one" || q.Series != nil {
			return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: local}}, nil
		}
		return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: ""}, {ID: "second"}},
			Open: func(_ context.Context, _ *control.Client, follow *pb.OpenReader) (ReaderOpenResult, error) {
				if follow.Series == nil || *follow.Series != "" || follow.ReaderId != "r" {
					t.Fatal("choice/presence/reader lost", follow)
				}
				events = append(events, "reuse "+local)
				return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: local}}, nil
			}, Close: func() error { events = append(events, "abandon "+local); return nil },
		}}, nil
	}
	cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": h}}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := seriesCall(t, cfg, "one", "", nil)
	if err != nil || first.GetReaderOpened() == nil {
		t.Fatal(first, err)
	}
	listed, err := seriesCall(t, cfg, "many", "", nil)
	if err != nil || len(listed.GetSeriesList().GetEntries()) != 2 {
		t.Fatal(listed, err)
	}
	if !reflect.DeepEqual(events, []string{"new one", "new many"}) {
		t.Fatal(events)
	}
	chosen, err := seriesCall(t, cfg, "many", "", proto.String(""))
	if err != nil || chosen.GetReaderOpened().GetHandle() == first.GetReaderOpened().GetHandle() {
		t.Fatal(chosen, err)
	}
	if !reflect.DeepEqual(events, []string{"new one", "new many", "reuse instance-2", "close instance-1"}) {
		t.Fatal(events)
	}
	// Listing the current URI first closes its open handle (H2's sole exception).
	_, err = seriesCall(t, cfg, "many", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(events[4:], []string{"close instance-2", "new many"}) {
		t.Fatal(events)
	}
	// An unrelated open disposes the pending listing before accessing any file.
	_, err = seriesCall(t, cfg, "one", "", nil)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(events[6:], []string{"abandon instance-3", "new one"}) {
		t.Fatal(events)
	}
	// A recent-files open, with no preceding listing, uses a fresh opener.
	_, err = seriesCall(t, cfg, "many", "", proto.String("second"))
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(events[8:], []string{"new many", "close instance-4"}) {
		t.Fatal(events)
	}
	if err := cfg.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
		t.Fatal(err)
	}
}

func TestSeriesSingletonAndRefusalsCloseListing(t *testing.T) {
	for _, mode := range []string{"singleton", "named list", "empty", "bad shape", "unknown", "singleton list", "close panic", "close error"} {
		t.Run(mode, func(t *testing.T) {
			closed, calls := 0, 0
			h := ReaderHandlers{OpenSeries: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
				calls++
				if q.Series != nil && mode == "singleton" {
					if *q.Series != "" {
						t.Fatal(q)
					}
					return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: "opened"}}, nil
				}
				if mode == "unknown" {
					return ReaderOpenResult{}, &control.HostError{Kind: pb.Error_NOT_FOUND, Message: "no such image"}
				}
				entries := []SeriesEntry{{ID: ""}, {ID: "b"}}
				switch mode {
				case "singleton", "singleton list":
					entries = entries[:1]
				case "empty", "close panic", "close error":
					entries = nil
				case "bad shape":
					entries[0].ShapeTCZYX = []uint32{1, 2}
				}
				return ReaderOpenResult{Choice: &SeriesChoice{Entries: entries, Close: func() error {
					closed++
					if mode == "close panic" {
						panic("cleanup panic")
					}
					if mode == "close error" {
						return errors.New("cleanup error")
					}
					return nil
				}}}, nil
			}}
			cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": h}}).Configure(control.Config{})
			if err != nil {
				t.Fatal(err)
			}
			var selected *string
			if mode == "named list" || mode == "unknown" {
				selected = proto.String("b")
			}
			out, err := seriesCall(t, cfg, "many", "r", selected)
			if mode == "singleton" {
				if err != nil || out.GetReaderOpened() == nil || calls != 2 || closed != 1 {
					t.Fatal(out, err, calls, closed)
				}
				return
			}
			wantClosed := 1
			if mode == "singleton list" {
				wantClosed = 2
			} // Two fresh listings own separate resources.
			var refusal *control.HostError
			if !errors.As(err, &refusal) {
				t.Fatal(err)
			}
			if mode == "unknown" {
				if refusal.Kind != pb.Error_NOT_FOUND {
					t.Fatal(refusal)
				}
			} else if refusal.Kind != pb.Error_INVALID_ARGUMENT || closed != wantClosed {
				t.Fatal(refusal, closed)
			}
			if mode == "named list" || mode == "singleton list" {
				if !strings.Contains(refusal.Message, "the reader answered with a list") {
					t.Fatal(refusal)
				}
			} else if mode != "unknown" && !strings.Contains(refusal.Message, "open failed: the reader's SeriesChoice is not valid:") {
				t.Fatal(refusal)
			}
		})
	}
}

func TestHeldListingExpiryDoesNotCloseItsSuccessor(t *testing.T) {
	if seriesHoldTime != 10*time.Minute {
		t.Fatal(seriesHoldTime)
	}
	s := &seriesState{}
	closed := 0
	choice := &SeriesChoice{Close: func() error { closed++; return nil }}
	q := &pb.OpenReader{Uri: "A", ReaderId: "r"}
	s.hold(nil, q, choice)
	old := s.pending
	// Same URI/id without a series is a fresh listing, not a continuation.
	if s.take(nil, q) != nil || closed != 1 {
		t.Fatal(closed)
	}
	s.hold(nil, q, choice)
	current := s.pending
	s.expire(nil, old) // a timer already running when its old listing was replaced
	if s.pending != current || closed != 1 {
		t.Fatal("stale timer closed successor")
	}
	s.expire(nil, current)
	s.expire(nil, current)
	if s.pending != nil || closed != 2 {
		t.Fatal("expiry did not close exactly once", closed)
	}
}

func TestHeldSeriesRequiresExactURIReaderAndSelectedPresence(t *testing.T) {
	for _, q := range []*pb.OpenReader{
		{Uri: "B", ReaderId: "r", Series: proto.String("a")},
		{Uri: "A", ReaderId: "other", Series: proto.String("a")},
		{Uri: "A", ReaderId: "r"},
	} {
		s := &seriesState{}
		closed := 0
		s.hold(nil, &pb.OpenReader{Uri: "A", ReaderId: "r"}, &SeriesChoice{Close: func() error { closed++; return nil }})
		if got := s.take(nil, q); got != nil || closed != 1 {
			t.Fatal("wrong listing reused", q, closed)
		}
	}
}

func TestSeriesRefusalPreservesDifferentURIReader(t *testing.T) {
	closed, abandoned := 0, 0
	h := ReaderHandlers{
		OpenSeries: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (ReaderOpenResult, error) {
			if q.Uri == "current" {
				return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: "local"}}, nil
			}
			return ReaderOpenResult{Choice: &SeriesChoice{Close: func() error { abandoned++; return nil }}}, nil
		},
		Metadata: func(_ context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
			return &pb.MetadataResult{Text: q.Handle}, nil
		},
		Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
			closed++
			return &pb.Closed{Handle: q.Handle}, nil
		},
	}
	cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": h}}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	opened, err := seriesCall(t, cfg, "current", "r", nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seriesCall(t, cfg, "invalid", "r", nil); err == nil {
		t.Fatal("invalid listing accepted")
	}
	meta, err := cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: opened.GetReaderOpened().Handle}}})
	if err != nil || meta.GetMetadata().GetText() != "local" || closed != 0 || abandoned != 1 {
		t.Fatal(meta, err, closed, abandoned)
	}
	if err := cfg.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
		t.Fatal(err)
	}
}

func TestLegacyReaderIgnoresSeriesAndSeriesDeclaration(t *testing.T) {
	d := definition()
	d.Reader(ReaderSpec{ID: "r", FallbackExtensions: []string{"...OME.TIF"}}, ReaderHandlers{Open: func(context.Context, *control.Client, *pb.OpenReader) (*pb.ReaderOpened, error) {
		return &pb.ReaderOpened{Handle: "legacy"}, nil
	}})
	app := buildDefinition(t, d)
	data, err := app.Manifest("reader")
	if err != nil {
		t.Fatal(err)
	}
	reader := decodeManifest(t, data).Contributions.Readers[0]
	if len(reader.Extensions) != 0 || !reflect.DeepEqual(reader.FallbackExtensions, []string{".ome.tif"}) {
		t.Fatal(reader)
	}
	cfg, err := app.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if out, err := seriesCall(t, cfg.Control, "one", "r", proto.String("ignored")); err != nil || out.GetReaderOpened() == nil {
		t.Fatal(out, err)
	}
	series := definition()
	series.Reader(ReaderSpec{ID: "r", Extensions: []string{"nwb"}}, ReaderHandlers{OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
		return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: "a"}, {ID: "b"}}}}, nil
	}})
	app = buildDefinition(t, series)
	cfg, err = app.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if out, err := seriesCall(t, cfg.Control, "many", "r", nil); err != nil || out.GetSeriesList() == nil {
		t.Fatal(out, err)
	}
	if err := cfg.Control.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
		t.Fatal(err)
	}
}

func TestRawSeriesContinuationTracksReaderGeometry(t *testing.T) {
	h := ReaderHandlers{OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
		return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: "a"}, {ID: "b"}}, Open: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
			return ReaderOpenResult{Opened: &pb.ReaderOpened{Handle: "local", ShapeTczyx: []uint32{1, 1, 2, 3, 4}, Dtype: pb.DType_DTYPE_U8}}, nil
		}}}, nil
	}, Volume: func(context.Context, *control.Client, *pb.ReadVolume) (*pb.SegmentRef, error) {
		return &pb.SegmentRef{ShapeZyx: []uint32{2, 3, 4}, Dtype: pb.DType_DTYPE_U8}, nil
	}}
	cfg, err := (Handlers{Reader: h}).Configure(control.Config{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := seriesCall(t, cfg, "many", "r", nil); err != nil {
		t.Fatal(err)
	}
	if _, err := seriesCall(t, cfg, "many", "r", proto.String("a")); err != nil {
		t.Fatal(err)
	}
	if _, err := cfg.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: "local"}}}); err != nil {
		t.Fatal(err)
	}
}
