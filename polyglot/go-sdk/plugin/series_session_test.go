package plugin

import (
	"context"
	"errors"
	"net"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

func seriesPeer(t *testing.T, handlers Handlers) (context.Context, context.CancelFunc, func(*pb.HostMsg) *pb.PluginMsg, <-chan error) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	diagnostics := make(chan error, 8)
	cfg, err := handlers.Configure(control.Config{PluginID: "series.test", Handshake: func(*pb.Hello) error { return nil }, Diagnostic: func(err error) { diagnostics <- err }})
	if err != nil {
		t.Fatal(err)
	}
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	l := bufconn.Listen(1 << 20)
	g := server.GRPCServer()
	go g.Serve(l)
	t.Cleanup(func() { g.Stop(); l.Close() })
	conn, err := grpc.NewClient("passthrough:///series", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(control.MaxMessageBytes)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	call := func(q *pb.HostMsg) *pb.PluginMsg {
		t.Helper()
		if err := stream.Send(q); err != nil {
			t.Fatal(err)
		}
		out, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return out
	}
	if call(&pb.HostMsg{Id: "Hello from series tests", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}).GetReady() == nil {
		t.Fatal("missing Ready")
	}
	return ctx, cancel, call, diagnostics
}

func TestHeldSeriesClosesAtSessionEnd(t *testing.T) {
	for _, raw := range []bool{false, true} {
		for _, abrupt := range []bool{false, true} {
			t.Run(map[bool]string{false: "routed", true: "raw"}[raw]+"/"+map[bool]string{false: "shutdown", true: "disconnect"}[abrupt], func(t *testing.T) {
				closed := make(chan struct{}, 2)
				r := ReaderHandlers{OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
					return ReaderOpenResult{Choice: &SeriesChoice{Entries: []SeriesEntry{{ID: "a"}, {ID: "b"}}, Close: func() error { closed <- struct{}{}; return nil }}}, nil
				}}
				h := Handlers{Readers: map[string]ReaderHandlers{"r": r}}
				if raw {
					h = Handlers{Reader: r}
				}
				_, cancel, call, _ := seriesPeer(t, h)
				out := call(&pb.HostMsg{Id: "list", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "many", ReaderId: "r"}}})
				if out.Id != "list" || len(out.GetSeriesList().GetEntries()) != 2 {
					t.Fatal(out)
				}
				if abrupt {
					cancel()
				} else {
					out = call(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(2000)}}})
					if out.GetShutdownReport().GetOutcome() != pb.ShutdownReport_COMPLETED {
						t.Fatal(out)
					}
				}
				select {
				case <-closed:
				case <-time.After(5 * time.Second):
					t.Fatal("session end left its listing open")
				}
			})
		}
	}
}

func TestInvalidSeriesReportsRefusalAndDiagnosesClose(t *testing.T) {
	r := ReaderHandlers{OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
		return ReaderOpenResult{Choice: &SeriesChoice{Close: func() error { return errors.New("listing cleanup failed") }}}, nil
	}}
	ctx, _, call, diagnostics := seriesPeer(t, Handlers{Readers: map[string]ReaderHandlers{"r": r}})
	out := call(&pb.HostMsg{Id: "bad", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "empty"}}})
	if out.Id != "bad" || out.GetError().GetKind() != pb.Error_INVALID_ARGUMENT || !strings.Contains(out.GetError().GetMessage(), "a SeriesChoice lists no series") {
		t.Fatal(out)
	}
	select {
	case err := <-diagnostics:
		if !strings.Contains(err.Error(), "listing cleanup failed") {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}

func TestTruncatedSeriesListOnStream(t *testing.T) {
	entries := make([]SeriesEntry, 40)
	for i := range entries {
		entries[i] = SeriesEntry{ID: strings.Repeat("i", i+1), Description: strings.Repeat("d", 1<<20)}
	}
	h := ReaderHandlers{OpenSeries: func(context.Context, *control.Client, *pb.OpenReader) (ReaderOpenResult, error) {
		return ReaderOpenResult{Choice: &SeriesChoice{Entries: entries}}, nil
	}}
	_, _, call, _ := seriesPeer(t, Handlers{Readers: map[string]ReaderHandlers{"r": h}})
	out := call(&pb.HostMsg{Id: "large", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "many"}}})
	if out.Id != "large" || !out.GetSeriesList().GetTruncated() || len(out.GetSeriesList().GetEntries()) != 31 {
		t.Fatal("bad truncated wire list", out.GetError(), len(out.GetSeriesList().GetEntries()))
	}
	for i, entry := range out.GetSeriesList().Entries {
		if entry.Id != entries[i].ID {
			t.Fatal("reordered wire prefix", i)
		}
	}
	if err := control.ValidateSize(out); err != nil {
		t.Fatal(err)
	}
}
