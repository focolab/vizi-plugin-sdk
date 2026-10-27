package control

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

type peer struct {
	stream grpc.BidiStreamingClient[pb.HostMsg, pb.PluginMsg]
	client *Client
	ctx    context.Context
}

func openPeer(t *testing.T, cfg Config, hello *pb.Hello, opts ...grpc.CallOption) *peer {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	t.Cleanup(cancel)
	attached := make(chan *Client, 1)
	cfg.PluginID = "test.plugin"
	if cfg.BuildIdentity == "" {
		cfg.BuildIdentity = "test.plugin test build"
	}
	if cfg.Handshake == nil {
		cfg.Handshake = func(*pb.Hello) error { return nil }
	}
	cfg.Attach = func(c *Client) error { attached <- c; return nil }
	if cfg.Diagnostic == nil {
		cfg.Diagnostic = func(error) {}
	}
	s, err := NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	l := bufconn.Listen(1 << 20)
	g := s.GRPCServer()
	go g.Serve(l)
	t.Cleanup(func() { g.Stop(); l.Close() })
	conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	stream, err := pb.NewPluginClient(conn).Session(ctx, opts...)
	if err != nil {
		t.Fatal(err)
	}
	if hello == nil {
		hello = &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 0}
	}
	if err := stream.Send(&pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: hello}}); err != nil {
		t.Fatal(err)
	}
	p := &peer{stream: stream, ctx: ctx}
	if hello.ProtocolMajor == 1 && hello.SessionToken != "reject" {
		first, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if first.GetReady() == nil || first.Id != s.identity || first.GetReady().ProtocolMajor != 1 || first.GetReady().ProtocolMinor != 0 {
			t.Fatalf("bad first message: %v", first)
		}
		select {
		case p.client = <-attached:
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		}
	}
	return p
}
func (p *peer) send(t *testing.T, m *pb.HostMsg) {
	t.Helper()
	if err := p.stream.Send(m); err != nil {
		t.Fatal(err)
	}
}
func (p *peer) recv(t *testing.T) *pb.PluginMsg {
	t.Helper()
	m, err := p.stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func TestExactVersionAndAuthentication(t *testing.T) {
	t.Run("version", func(t *testing.T) {
		p := openPeer(t, Config{}, &pb.Hello{ProtocolMinor: 12})
		_, err := p.stream.Recv()
		if err == nil || !strings.Contains(err.Error(), "host 0.12, Go SDK 1.0") {
			t.Fatalf("%v", err)
		}
	})
	t.Run("token", func(t *testing.T) {
		p := openPeer(t, Config{Handshake: func(*pb.Hello) error { return errors.New("token mismatch") }}, &pb.Hello{ProtocolMajor: 1, ProtocolMinor: 0, SessionToken: "reject"})
		_, err := p.stream.Recv()
		if err == nil || !strings.Contains(err.Error(), "token mismatch") {
			t.Fatalf("%v", err)
		}
	})
}

func TestCommandCanCallHostWhileArrangementsContinue(t *testing.T) {
	p := openPeer(t, Config{
		HandleRequest: func(ctx context.Context, c *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
			asset, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "model"})
			if err != nil {
				return nil, err
			}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: asset.Declined}}}, nil
		},
		Arrange: func(c *Client, m *pb.HostMsg) error {
			return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: m.GetToolBinding().Tool}}})
		},
	}, nil)
	p.send(t, &pb.HostMsg{Id: "host-id", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "run"}}})
	request := p.recv(t)
	if request.GetRequestAsset() == nil || request.Id == "" {
		t.Fatal(request)
	}
	// Deliberately collide ids: class must be chosen before correlation.
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 7}}})
	ack := p.recv(t)
	if ack.GetToolBindingAck().GetTool() != 7 || ack.Id != "" {
		t.Fatal(ack)
	}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{Declined: true}}})
	done := p.recv(t)
	if done.Id != "host-id" || !done.GetCommandDone().GetOk() {
		t.Fatal(done)
	}
}

func TestAbandonedDescriptorIsReleasedExactlyOnce(t *testing.T) {
	var released atomic.Int32
	diagnostics := make(chan error, 8)
	p := openPeer(t, Config{Prepare: func(context.Context, *Client, *pb.HostMsg) error { return nil }, Release: func(c *Client, m *pb.HostMsg) error {
		if m.GetDataVolume() != nil {
			released.Add(1)
			return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ReleaseDeposit{ReleaseDeposit: &pb.ReleaseDeposit{SegmentId: 11, Region: 2, Offset: 192, Len: 64}}})
		}
		return nil
	}, Diagnostic: func(e error) { diagnostics <- e }}, nil)
	ctx, cancel := context.WithCancel(p.ctx)
	call := make(chan error, 1)
	go func() { _, err := p.client.ReadDataVolume(ctx, &pb.ReadDataVolume{}); call <- err }()
	request := p.recv(t)
	cancel()
	if err := <-call; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: &pb.SegmentRef{SegmentId: 11, Offset: 192, Len: 64}}}})
	if release := p.recv(t).GetReleaseDeposit(); release == nil || release.Offset != 192 || release.Region != 2 {
		t.Fatal(release)
	}
	if released.Load() != 1 {
		t.Fatal(released.Load())
	}
	select {
	case e := <-diagnostics:
		t.Fatalf("late reply produced a diagnostic: %v", e)
	default:
	}

}

func TestCancellationRacingDeliveryReleasesOnce(t *testing.T) {
	for i := 0; i < 30; i++ {
		var released atomic.Int32
		p := openPeer(t, Config{Release: func(*Client, *pb.HostMsg) error { released.Add(1); return nil }}, nil)
		ctx, cancel := context.WithCancel(p.ctx)
		done := make(chan struct{})
		go func() {
			defer close(done)
			r, err := p.client.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetViewState{GetViewState: &pb.GetViewState{}}})
			if err == nil {
				r.Close()
			}
		}()
		request := p.recv(t)
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
		cancel()
		<-done
		deadline := time.NewTimer(time.Second)
		for released.Load() != 1 {
			select {
			case <-deadline.C:
				t.Fatalf("release count %d", released.Load())
			case <-time.After(time.Millisecond):
			}
		}
		deadline.Stop()
	}
}

func TestBlockedReaderDoesNotBlockShutdownHook(t *testing.T) {
	started := make(chan struct{})
	finish := make(chan struct{})
	hook := make(chan struct{})
	p := openPeer(t, Config{HandleRequest: func(ctx context.Context, _ *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
		close(started)
		select {
		case <-finish:
		case <-ctx.Done():
			return nil, ctx.Err()
		}
		return &pb.PluginMsg{Msg: &pb.PluginMsg_ReaderOpened{ReaderOpened: &pb.ReaderOpened{Handle: "reader"}}}, nil
	}, Shutdown: func(_ context.Context, c *Client, sh *pb.Shutdown) error {
		if sh.Reason != pb.ShutdownReason_SHUTDOWN_REASON_RESTART {
			return errors.New("reason lost")
		}
		close(hook)
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_DoorbellFinalReport{DoorbellFinalReport: &pb.DoorbellFinalReport{Detail: "persisted"}}})
	}}, nil)
	p.send(t, &pb.HostMsg{Id: "open", Msg: &pb.HostMsg_OpenReader{OpenReader: &pb.OpenReader{Uri: "test"}}})
	<-started
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{Reason: pb.ShutdownReason_SHUTDOWN_REASON_RESTART, DeadlineMs: proto.Uint32(2000)}}})
	select {
	case <-hook:
	case <-p.ctx.Done():
		t.Fatal("hook queued behind reader")
	}
	if p.recv(t).GetDoorbellFinalReport() == nil {
		t.Fatal("missing final report")
	}
	if _, err := p.client.GetViewState(p.ctx); !errors.Is(err, ErrShuttingDown) {
		t.Fatalf("new work admitted: %v", err)
	}
	close(finish)
	if p.recv(t).GetReaderOpened() == nil {
		t.Fatal("in-flight result lost")
	}
	if m := p.recv(t); m.GetShutdownReport().GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(m)
	}
	if _, err := p.stream.Recv(); !errors.Is(err, io.EOF) {
		t.Fatalf("unclean shutdown: %v", err)
	}
}

func TestAllHostRequestsReceiveAnErrorWhenUnsupported(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	for k, r := range hostRoutes {
		if r.class != Request {
			continue
		}
		m := &pb.HostMsg{Id: k}
		ref := m.ProtoReflect()
		f := ref.Descriptor().Fields().ByName(protoreflect.Name(k))
		ref.Mutable(f)
		p.send(t, m)
		reply := p.recv(t)
		if reply.Id != k || reply.GetError().GetKind() != pb.Error_UNSUPPORTED {
			t.Fatalf("%s: %v", k, reply)
		}
	}
}

func TestRideAlongWaitDoesNotBlockRouting(t *testing.T) {
	grant := make(chan struct{})
	waiting := make(chan struct{})
	defer close(grant)
	p := openPeer(t, Config{Prepare: func(ctx context.Context, _ *Client, _ *pb.HostMsg) error {
		close(waiting)
		select {
		case <-grant:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}, Release: func(*Client, *pb.HostMsg) error { return nil }, Arrange: func(c *Client, _ *pb.HostMsg) error {
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: 1}}})
	}}, nil)
	p.send(t, &pb.HostMsg{Id: "export", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1}}, Install: &pb.InstallSegment{SegmentId: 1}}}})
	<-waiting
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ToolBinding{ToolBinding: &pb.ToolBinding{Tool: 1}}})
	if p.recv(t).GetToolBindingAck() == nil {
		t.Fatal("arrangement blocked by missing capability")
	}
}
