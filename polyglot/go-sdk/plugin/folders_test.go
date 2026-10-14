package plugin

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestReaderAndExporterFolders(t *testing.T) {
	for _, registered := range []bool{false, true} {
		t.Run(map[bool]string{false: "raw", true: "registered"}[registered], func(t *testing.T) {
			settings := t.TempDir() // The host creates only the settings folder.
			t.Setenv("VIZI_PLUGIN_SETTINGS_DIR", settings)
			readerDir := filepath.Join(t.TempDir(), "reader file", "plugins", "test")
			sourceDir := filepath.Join(t.TempDir(), "export source", "plugins", "test")
			locator := filepath.Join(t.TempDir(), "destination.raw")
			want := ""
			calls := 0
			check := func(q interface{ GetFileStateDir() string }) {
				t.Helper()
				calls++
				if got, err := control.SettingsDir(); err != nil || got != settings {
					t.Fatalf("settings outside command context: %q %v", got, err)
				}
				got := control.FileStateDir(q)
				if want == "" {
					if got != nil {
						t.Fatal("empty wire value is not none")
					}
				} else if got == nil || *got != want {
					t.Fatalf("file state folder: %v, want %q", got, want)
				}
			}
			reader := ReaderHandlers{
				Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
					check(q)
					return &pb.ReaderOpened{Handle: q.Uri, ShapeTczyx: []uint32{1, 1, 1, 1, 1}, Dtype: pb.DType_DTYPE_U8}, nil
				},
				Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
					return &pb.Closed{Handle: q.Handle}, nil
				},
			}
			exporter := ExportHandlers{
				Open: func(_ context.Context, _ *control.Client, q *pb.OpenExport) (*pb.ExportOpened, error) {
					check(q)
					if q.Locator != locator {
						t.Fatal("export destination changed")
					}
					return &pb.ExportOpened{Handle: "export"}, nil
				},
				Chunk: func(_ context.Context, _ *control.Client, q *pb.ExportChunk) (*pb.ChunkWritten, error) {
					return &pb.ChunkWritten{Handle: q.Handle}, nil
				},
				Finish: func(_ context.Context, _ *control.Client, q *pb.FinishExport) (*pb.ExportDone, error) {
					return &pb.ExportDone{Handle: q.Handle}, nil
				},
				Abort: func(_ context.Context, _ *control.Client, q *pb.AbortExport) (*pb.ExportDone, error) {
					return &pb.ExportDone{Handle: q.Handle}, nil
				},
			}
			var cfg control.Config
			var err error
			if registered {
				d := definition()
				d.Reader(ReaderSpec{ID: "reader", Extensions: []string{"raw"}}, reader)
				d.Exporter(ExporterSpec{ID: "exporter", Extension: "raw"}, exporter)
				app := buildDefinition(t, d)
				var plugin control.Plugin
				plugin, err = app.Configure(control.Plugin{})
				cfg = plugin.Control
			} else {
				cfg, err = (Handlers{Reader: reader, Export: exporter}).Configure(control.Config{})
			}
			if err != nil {
				t.Fatal(err)
			}
			for _, present := range []bool{true, false} {
				requests := []*pb.HostMsg{
					{Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "reader", Uri: "input.raw"}}},
					{Msg: &pb.HostMsg_OpenExport{OpenExport: &pb.OpenExport{ExporterId: "exporter", Locator: locator}}},
				}
				if present {
					requests[0].GetOpenReader().FileStateDir = readerDir
					requests[1].GetOpenExport().FileStateDir = sourceDir
				}
				for _, request := range requests {
					want = ""
					if present {
						want = sourceDir
						if request.GetOpenReader() != nil {
							want = readerDir
						}
					}
					// Exercise wire serialization and the authoring routers (including
					// the reader's clone/ID rewrite) before reaching the callback.
					wire, err := proto.Marshal(request)
					if err != nil {
						t.Fatal(err)
					}
					var decoded pb.HostMsg
					if err := proto.Unmarshal(wire, &decoded); err != nil {
						t.Fatal(err)
					}
					if _, err := cfg.HandleRequest(context.Background(), nil, &decoded); err != nil {
						t.Fatal(err)
					}
				}
			}
			if calls != 4 {
				t.Fatalf("only %d callbacks reached", calls)
			}
			for _, path := range []string{readerDir, sourceDir, locator} {
				if _, err := os.Stat(path); !os.IsNotExist(err) {
					t.Fatalf("SDK created %q: %v", path, err)
				}
			}
		})
	}
}
