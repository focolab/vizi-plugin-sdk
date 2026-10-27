package main

import (
	"bytes"
	"context"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"log"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
)

func TestExportCalibrationLoggingAndStatus(t *testing.T) {
	var logs bytes.Buffer
	oldWriter := log.Writer()
	log.SetOutput(&logs)
	t.Cleanup(func() { log.SetOutput(oldWriter) })
	for _, tc := range []struct {
		name, document, sources, voxelSize string
		fields                             [4]float64
	}{
		{"all fields", `<Pixels PhysicalSizeZ="99"/>`, "", "1.000 × 2.000 × 3.000 µm (z × y × x), Δt 4 s", [4]float64{1, 2, 3, 4}},
		{"source document ignored", `<Pixels PhysicalSizeY="5"/>`, "[fields neither fields fields]", "1.000 × ? × 3.000 µm (z × y × x), Δt 4 s", [4]float64{1, 0, 3, 4}},
		{"unknown spatial", "", "[fields fields neither fields]", "1.000 × 2.000 × ? µm (z × y × x), Δt 4 s", [4]float64{1, 2, 0, 4}},
		{"unknown time", "", "[fields fields fields neither]", "1.000 × 2.000 × 3.000 µm (z × y × x), Δt ? s", [4]float64{1, 2, 3, 0}},
		{"all unknown", `<Pixels PhysicalSizeZ="99"/>`, "", "", [4]float64{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			logs.Reset()
			a := newApplication()
			close(a.ready)
			path := filepath.Join(t.TempDir(), "export.crate")
			opened, err := a.openExport(context.Background(), nil, &pb.OpenExport{
				ExporterId: "crate", Locator: path, ShapeTczyx: []uint32{1, 2, 1, 1, 1}, Dtype: 2,
				SpacingZ: tc.fields[0], SpacingY: tc.fields[1], SpacingX: tc.fields[2], TimeIncrement: tc.fields[3],
				SpacingUnit: "um", TimeUnit: "s", SourceMetadata: &pb.MetadataDoc{Form: "ome-xml", Text: tc.document},
			})
			if err != nil {
				t.Fatal(err)
			}
			w := a.exports[opened.Handle].writer
			t.Cleanup(func() {
				if err := w.Abort(); err != nil {
					t.Error(err)
				}
			})
			for c := uint32(0); c < 2; c++ {
				if err := w.Write(0, c, []byte{0, 0}, true); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := a.finishExport(context.Background(), nil, &pb.FinishExport{Handle: opened.Handle}); err != nil {
				t.Fatal(err)
			}
			if tc.sources == "" {
				if logs.Len() != 0 {
					t.Fatalf("healthy export logged: %s", logs.String())
				}
			} else if !strings.Contains(logs.String(), path+": export calibration Z/Y/X/time=") ||
				!strings.Contains(logs.String(), "sources="+tc.sources) || strings.Count(logs.String(), "\n") != 1 {
				t.Fatalf("expected one log naming all four sources %s; got %q", tc.sources, logs.String())
			}
			wantStatus := "Exported " + path + " (2 blocks)"
			if tc.voxelSize == "" {
				wantStatus += " without voxel size — the host supplied no spacing."
			} else {
				wantStatus += "; voxel size " + tc.voxelSize
			}
			if a.status != wantStatus {
				t.Fatalf("status = %q; want %q", a.status, wantStatus)
			}
		})
	}
}

func TestReadBeforeGrantNamesMissingPurpose(t *testing.T) {
	r, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	a := newApplication()
	a.session = &control.Session{Memory: control.NewMemory(r)}
	if _, err = a.deposit([]byte{42}, []uint32{1, 1, 1}, 1); err == nil || !strings.Contains(err.Error(), "purpose G") {
		t.Fatal(err)
	}
}

func TestSourceIndicesAndRefusalAreTransactional(t *testing.T) {
	ts, tr, err := rankIndices(nil, []uint32{40}, 2, "t")
	if err != nil || tr[0] != 0 {
		t.Fatal(ts, tr, err)
	}
	ts, tr, err = rankIndices(ts, []uint32{49}, 2, "t")
	if err != nil || tr[0] != 1 {
		t.Fatal(ts, tr, err)
	}
	if _, _, err = rankIndices(ts, []uint32{50}, 2, "t"); err == nil {
		t.Fatal("third timepoint accepted")
	}
	if len(ts) != 2 || ts[0] != 40 || ts[1] != 49 {
		t.Fatal("refusal mutated accepted timepoints")
	}
	cs, cr, err := rankIndices(nil, []uint32{2, 5}, 2, "c")
	if err != nil || cr[0] != 0 || cr[1] != 1 {
		t.Fatal(cs, cr, err)
	}
	_, cr, err = rankIndices(cs, []uint32{5, 2}, 2, "c")
	if err != nil || cr[0] != 1 || cr[1] != 0 {
		t.Fatal(cr, err)
	}
}

func TestReaderReplacementKeepsFilesAndPanelSeparate(t *testing.T) {
	a := newApplication()
	close(a.ready)
	cfg, err := a.runtime()
	if err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	t.Cleanup(func() {
		if err := cfg.Control.Shutdown(ctx, nil, &pb.Shutdown{}); err != nil {
			t.Error(err)
		}
	})
	call := func(q *pb.HostMsg) (*pb.PluginMsg, error) { return cfg.Control.HandleRequest(ctx, nil, q) }
	open := func(uri string) string {
		t.Helper()
		out, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: uri}}})
		if err != nil {
			t.Fatal(err)
		}
		return out.GetReaderOpened().Handle
	}
	first := open(fixture("sample.crate"))
	old := a.reader
	nextPath := filepath.Join(t.TempDir(), "next.crate")
	data, err := os.ReadFile(fixture("nospacing.crate"))
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(nextPath, data, 0600); err != nil {
		t.Fatal(err)
	}
	second := open(nextPath)
	if first == second || old.file != nil || a.reader == old || len(a.readers) != 1 || a.head != nextPath || a.vox != "(unknown)" {
		t.Fatal("replacement lost file/panel state", a.head, a.vox, len(a.readers))
	}
	current := a.reader
	if _, err := current.Volume(0, 0, true); err != nil {
		t.Fatal("replacement file was closed", err)
	}
	if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_CloseReader{CloseReader: &pb.CloseReader{Handle: first}}}); err != nil {
		t.Fatal(err)
	}
	if a.reader != current || a.head != nextPath {
		t.Fatal("old close cleared current reader")
	}
	if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: fixture("unfinalized.crate")}}}); err == nil {
		t.Fatal("bad open succeeded")
	}
	if a.reader != current || a.head != nextPath || len(a.readers) != 1 {
		t.Fatal("refused open changed current reader")
	}
	if _, err := current.Volume(0, 0, true); err != nil {
		t.Fatal("refused open closed current file", err)
	}
	// A failed same-URI reload must leave no reader, since exclusive locks force
	// the close before trying the file again.
	if err := os.WriteFile(nextPath, []byte("invalid crate"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := call(&pb.HostMsg{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: nextPath}}}); err == nil {
		t.Fatal("invalid reload succeeded")
	}
	if a.reader != nil || current.file != nil || len(a.readers) != 0 || a.head != "No crate open" {
		t.Fatal("failed reload kept reader")
	}
}
