package control

import (
	"context"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

// A stream with a writable Ready and a permanently stalled data writer verifies
// the bound without relying on HTTP/2 buffer sizes or a cooperative peer.
type stalledStream struct {
	ctx     context.Context
	in      chan *pb.HostMsg
	blocked chan struct{}
}

func (s *stalledStream) Context() context.Context     { return s.ctx }
func (s *stalledStream) SetHeader(metadata.MD) error  { return nil }
func (s *stalledStream) SendHeader(metadata.MD) error { return nil }
func (s *stalledStream) SetTrailer(metadata.MD)       {}
func (s *stalledStream) SendMsg(any) error            { return nil }
func (s *stalledStream) RecvMsg(any) error            { return nil }
func (s *stalledStream) Recv() (*pb.HostMsg, error) {
	select {
	case m := <-s.in:
		return m, nil
	case <-s.ctx.Done():
		return nil, s.ctx.Err()
	}
}
func (s *stalledStream) Send(m *pb.PluginMsg) error {
	if m.GetReady() != nil {
		return nil
	}
	close(s.blocked)
	<-s.ctx.Done()
	return s.ctx.Err()
}
func TestShutdownDeadlineBoundsStalledWriter(t *testing.T) {
	for _, overrun := range []bool{false, true} {
		ctx, cancel := context.WithCancel(context.Background())
		stream := &stalledStream{ctx: ctx, in: make(chan *pb.HostMsg, 2), blocked: make(chan struct{})}
		cfg := Config{BuildIdentity: "test test build", PluginID: "test", Handshake: func(*pb.Hello) error { return nil }, Diagnostic: func(error) {}}
		if overrun {
			cfg.Shutdown = func(ctx context.Context, _ *Client, _ *pb.Shutdown) error { <-ctx.Done(); return ctx.Err() }
		}
		server, err := NewServer(cfg)
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		stream.in <- &pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}
		stream.in <- &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(20)}}}
		done := make(chan error, 1)
		go func() { done <- server.Session(stream) }()
		select {
		case <-stream.blocked:
		case <-time.After(time.Second):
			cancel()
			t.Fatal("writer did not attempt final report")
		}
		select {
		case err := <-done:
			if err != nil {
				cancel()
				t.Fatal(err)
			}
		case <-time.After(time.Second):
			cancel()
			t.Fatal("stalled writer defeated deadline")
		}
		cancel()
	}
}
