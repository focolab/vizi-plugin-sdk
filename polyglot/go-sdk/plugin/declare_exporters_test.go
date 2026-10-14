package plugin

import (
	"context"
	"fmt"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestDefinitionExporterSuffixNormalization(t *testing.T) {
	handlers := ExportHandlers{
		Open:   func(context.Context, *control.Client, *pb.OpenExport) (*pb.ExportOpened, error) { return nil, nil },
		Chunk:  func(context.Context, *control.Client, *pb.ExportChunk) (*pb.ChunkWritten, error) { return nil, nil },
		Finish: func(context.Context, *control.Client, *pb.FinishExport) (*pb.ExportDone, error) { return nil, nil },
		Abort:  func(context.Context, *control.Client, *pb.AbortExport) (*pb.ExportDone, error) { return nil, nil },
	}
	for _, tc := range []struct{ suffix, want string }{
		{"", ""}, {".", ""}, {"... \t\r\n\v\f", ""},
		{"\u00a0", ".\u00a0"}, {".\u2003", ".\u2003"},
		{" \t.", ". \t."}, {".. H5 ", ". h5 "}, {"...OME.TIF", ".ome.tif"},
	} {
		d := definition()
		d.Exporter(ExporterSpec{ID: "e", Extension: tc.suffix}, handlers)
		a, err := d.Build()
		if tc.want == "" {
			if err == nil || !strings.Contains(err.Error(), "extension must be non-empty") {
				t.Fatalf("suffix %q: %v", tc.suffix, err)
			}
			continue
		}
		if err != nil {
			t.Fatalf("suffix %q: %v", tc.suffix, err)
		}
		data, err := a.Manifest("./plugin")
		if err != nil {
			t.Fatal(err)
		}
		if got := decodeManifest(t, data).Contributions.Exporters[0].Extension; got != tc.want {
			t.Fatalf("suffix %q: got %q, want %q", tc.suffix, got, tc.want)
		}
	}
}

func TestDefinitionExportersHaveIndependentHandlesAndSessionState(t *testing.T) {
	d := definition()
	events := []string{}
	for _, id := range []string{"one", "two"} {
		d.Exporter(ExporterSpec{ID: id, Extension: "RAW"}, ExportHandlers{
			Open: func(context.Context, *control.Client, *pb.OpenExport) (*pb.ExportOpened, error) {
				return &pb.ExportOpened{Handle: "local"}, nil
			},
			Chunk: func(_ context.Context, _ *control.Client, q *pb.ExportChunk) (*pb.ChunkWritten, error) {
				events = append(events, id+":chunk:"+q.Handle)
				return &pb.ChunkWritten{Handle: q.Handle, TotalBytes: 3}, nil
			},
			Finish: func(_ context.Context, _ *control.Client, q *pb.FinishExport) (*pb.ExportDone, error) {
				events = append(events, id+":finish:"+q.Handle)
				return &pb.ExportDone{Handle: q.Handle}, nil
			},
			Abort: func(_ context.Context, _ *control.Client, q *pb.AbortExport) (*pb.ExportDone, error) {
				events = append(events, id+":abort:"+q.Handle)
				return &pb.ExportDone{Handle: q.Handle}, nil
			},
		})
	}
	a := buildDefinition(t, d)
	cfg, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	request := func(m *pb.HostMsg) *pb.PluginMsg {
		t.Helper()
		out, err := cfg.Control.HandleRequest(context.Background(), nil, m)
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	handles := []string{}
	for _, id := range []string{"one", "two"} {
		out := request(&pb.HostMsg{Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{ExporterId: id}}})
		handles = append(handles, out.GetExportOpened().Handle)
	}
	if handles[0] == handles[1] {
		t.Fatal("colliding wire handles")
	}
	for _, h := range handles {
		q := &pb.ExportChunk{Handle: h}
		out := request(&pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: q}})
		if q.Handle != h || out.GetChunkWritten().Handle != h {
			t.Fatal("handle not translated or request mutated")
		}
	}
	request(&pb.HostMsg{Msg: &pb.HostMsg_FinishExport{FinishExport: &pb.FinishExport{Handle: handles[0]}}})
	if err := cfg.Control.Shutdown(context.Background(), nil, &pb.Shutdown{}); err != nil {
		t.Fatal(err)
	}
	if got := fmt.Sprint(events); got != "[one:chunk:local two:chunk:local one:finish:local two:abort:local]" {
		t.Fatal(got)
	}
	fresh, err := a.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fresh.Control.HandleRequest(context.Background(), nil, &pb.HostMsg{Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Handle: handles[1]}}}); err == nil {
		t.Fatal("session retained old export handles")
	}
}
