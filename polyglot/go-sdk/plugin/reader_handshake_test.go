package plugin

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

func TestReaderReplacementHandshakeOnStream(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	events := make(chan string, 32)
	diagnostics := make(chan error, 16)
	var refuse atomic.Bool
	var closeFailure atomic.Int32
	// Only the serial request worker touches these file handles.
	files := make(map[string]string)
	serial := 0
	handlers := ReaderHandlers{
		Open: func(_ context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
			events <- "open " + q.Uri
			if q.Uri == "bad" || refuse.Load() {
				return nil, errors.New("open failed")
			}
			for _, uri := range files {
				if uri == q.Uri {
					return nil, errors.New("file still exclusively locked")
				}
			}
			serial++
			h := fmt.Sprintf("local-%d", serial)
			files[h] = q.Uri
			return &pb.ReaderOpened{Handle: h}, nil
		},
		Close: func(_ context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
			events <- "close " + files[q.Handle]
			delete(files, q.Handle)
			switch closeFailure.Load() {
			case 1:
				return nil, errors.New("close failed after releasing file")
			case 2:
				panic("close panic after releasing file")
			}
			return &pb.Closed{Handle: q.Handle}, nil
		},
		Metadata: func(_ context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
			uri, ok := files[q.Handle]
			if !ok {
				return nil, errors.New("callback received a closed handle")
			}
			return &pb.MetadataResult{Text: uri}, nil
		},
	}
	cfg, err := (Handlers{Readers: map[string]ReaderHandlers{"r": handlers}}).Configure(control.Config{PluginID: "reader.test", Handshake: func(*pb.Hello) error { return nil }, Diagnostic: func(err error) { diagnostics <- err }})
	if err != nil {
		t.Fatal(err)
	}
	server, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	defer listener.Close()
	grpcServer := server.GRPCServer()
	go grpcServer.Serve(listener)
	defer grpcServer.Stop()
	conn, err := grpc.NewClient("passthrough:///reader", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
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
	if call(&pb.HostMsg{Id: "Hello from reader test 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}).GetReady() == nil {
		t.Fatal("missing Ready")
	}
	expectEvents := func(want ...string) {
		t.Helper()
		for _, w := range want {
			select {
			case got := <-events:
				if got != w {
					t.Fatalf("event %q, want %q", got, w)
				}
			case <-ctx.Done():
				t.Fatal(ctx.Err())
			}
		}
		select {
		case extra := <-events:
			t.Fatal("extra callback", extra)
		default:
		}
	}
	seen := make(map[string]bool)
	open := func(uri string) string {
		t.Helper()
		out := call(&pb.HostMsg{Id: freshID("open"), Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{ReaderId: "r", Uri: uri}}})
		h := out.GetReaderOpened().GetHandle()
		if h == "" || seen[h] {
			t.Fatal("missing/reused wire handle", out)
		}
		seen[h] = true
		return h
	}
	metadata := func(h, uri string) {
		t.Helper()
		if out := call(&pb.HostMsg{Id: freshID("metadata"), Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: h}}}); out.GetMetadata().GetText() != uri {
			t.Fatal("wrong reader", out)
		}
	}
	close := func(h string) {
		t.Helper()
		if out := call(&pb.HostMsg{Id: freshID("close"), Msg: &pb.HostMsg_CloseReader{CloseReader: &pb.CloseReader{Handle: h}}}); out.GetClosed().GetHandle() != h {
			t.Fatal(out)
		}
	}
	diagnostic := func(part string) {
		t.Helper()
		select {
		case err := <-diagnostics:
			if !strings.Contains(err.Error(), part) {
				t.Fatal(err)
			}
		case <-ctx.Done():
			t.Fatal("missing diagnostic", part)
		}
	}
	a := open("A")
	expectEvents("open A")
	out := call(&pb.HostMsg{Id: freshID("bad"), Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "bad"}}})
	if out.GetError() == nil {
		t.Fatal(out)
	}
	expectEvents("open bad")
	metadata(a, "A")
	b := open("B")
	expectEvents("open B", "close A")
	metadata(b, "B")
	for _, q := range []*pb.HostMsg{
		{Id: "old-volume", Msg: &pb.HostMsg_ReadVolume{ReadVolume: &pb.ReadVolume{Handle: a}}},
		{Id: "old-plane", Msg: &pb.HostMsg_ReadPlane{ReadPlane: &pb.ReadPlane{Handle: a}}},
		{Id: "old-metadata", Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: a}}},
	} {
		out := call(q)
		e := out.GetError()
		if e == nil || e.Kind != pb.Error_NOT_FOUND || !strings.Contains(e.Message, a) {
			t.Fatal(out)
		}
		diagnostic("host out of step")
	}
	close(a)
	close(a)
	expectEvents()
	metadata(b, "B")
	// Same URI reload closes first, before attempting to take the exclusive lock.
	b2 := open("B")
	expectEvents("close B", "open B")
	metadata(b2, "B")
	refuse.Store(true)
	out = call(&pb.HostMsg{Id: "reload-fail", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "B"}}})
	if out.GetError() == nil {
		t.Fatal(out)
	}
	expectEvents("close B", "open B")
	out = call(&pb.HostMsg{Id: "gone", Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: b2}}})
	if out.GetError().GetKind() != pb.Error_NOT_FOUND {
		t.Fatal(out)
	}
	diagnostic("host out of step")
	close(b2)
	expectEvents()
	refuse.Store(false)
	current := open("C")
	expectEvents("open C")
	// A cleanup error/panic cannot turn a successful different-URI open into an
	// Error promising that nothing changed. It is diagnosed; the new reader lives.
	currentURI := "C"
	for _, mode := range []int32{1, 2} {
		closeFailure.Store(mode)
		old := current
		uri := fmt.Sprintf("D%d", mode)
		current = open(uri)
		expectEvents("open "+uri, "close "+currentURI)
		currentURI = uri
		diagnostic("replaced reader close failed")
		metadata(current, uri)
		close(old)
	}
	closeFailure.Store(1)
	out = call(&pb.HostMsg{Id: "close-fails", Msg: &pb.HostMsg_CloseReader{CloseReader: &pb.CloseReader{Handle: current}}})
	if out.GetError() == nil {
		t.Fatal("close failure was not reported", out)
	}
	expectEvents("close " + currentURI)
	close(current)
	expectEvents() // a failure after release must not retry the closed file
	out = call(&pb.HostMsg{Id: "closed-metadata", Msg: &pb.HostMsg_Metadata{Metadata: &pb.Metadata{Handle: current}}})
	if out.GetError().GetKind() != pb.Error_NOT_FOUND {
		t.Fatal(out)
	}
	diagnostic("host out of step")
	closeFailure.Store(0)
	out = call(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	if out.GetShutdownReport().GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(out)
	}
}
