package control

import (
	"context"
	"net"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

func appearanceMsg(theme string, text uint32) *pb.HostMsg {
	return &pb.HostMsg{Msg: &pb.HostMsg_Appearance{Appearance: &pb.HostAppearance{Theme: theme, WindowBg: 0x202020ff, Text: text}}}
}

func appearanceBarrier(t *testing.T, p *peer) {
	t.Helper()
	p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "barrier"}}})
	if q := p.recv(t); q.GetCommandDone() == nil {
		t.Fatal(q)
	}
}

func answerDone(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
	return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
}

func TestAppearanceRoutesAsHostCoalescing(t *testing.T) {
	if c := HostClass(appearanceMsg("dark", 0)); c != HostCoalescing {
		t.Fatal(c)
	}
}

func TestAppearanceNewestWinsOverWire(t *testing.T) {
	heard := make(chan *pb.HostAppearance, 8)
	release := make(chan struct{})
	p := openPeer(t, Config{
		Appearance: func(ctx context.Context, c *Client, a *pb.HostAppearance) error {
			heard <- a
			if a.Text == 1 {
				select {
				case <-release:
				case <-ctx.Done():
				}
			}
			return nil
		},
		HandleRequest: answerDone,
	}, nil)
	if _, ok := p.client.Appearance(); ok {
		t.Fatal("an appearance before the first push")
	}
	p.send(t, appearanceMsg("dark", 1))
	if a := <-heard; a.Theme != "dark" || a.Text != 1 {
		t.Fatal(a)
	}
	// The handler is parked: these coalesce to the newest, keyed by the session.
	p.send(t, appearanceMsg("light", 2))
	p.send(t, appearanceMsg("dark", 3))
	p.send(t, appearanceMsg("light", 4))
	appearanceBarrier(t, p)
	if a, ok := p.client.Appearance(); !ok || a.Theme != "light" || a.Text != 4 || a.WindowBg != 0x202020ff {
		t.Fatal(a, ok)
	}
	close(release)
	if a := <-heard; a.Theme != "light" || a.Text != 4 {
		t.Fatal("not the newest:", a)
	}
	select {
	case a := <-heard:
		t.Fatal("a superseded appearance was delivered", a)
	case <-time.After(50 * time.Millisecond):
	}
}

func TestAppearanceWithoutHandlerIsRecordedQuietly(t *testing.T) {
	diagnostics := make(chan error, 4)
	p := openPeer(t, Config{Diagnostic: func(err error) { diagnostics <- err }, HandleRequest: answerDone}, nil)
	p.send(t, appearanceMsg("light", 0x101010ff))
	appearanceBarrier(t, p)
	a, ok := p.client.Appearance()
	if !ok || a.Theme != "light" || a.Text != 0x101010ff {
		t.Fatal(a, ok)
	}
	a.Theme = "mutated"
	if again, _ := p.client.Appearance(); again.Theme != "light" {
		t.Fatal("the cached appearance is shared with a caller")
	}
	select {
	case err := <-diagnostics:
		t.Fatal("an unhandled appearance is not a fault:", err)
	default:
	}
}

// An appearance before Hello, so before Ready, is a first message that is not
// Hello: refused like any other, and the session never starts.
func TestAppearanceBeforeReadyIsRefused(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	called := make(chan struct{}, 1)
	s, err := NewServer(Config{
		PluginID:      "test.appearance",
		Handshake:     func(*pb.Hello) error { return nil },
		Appearance:    func(context.Context, *Client, *pb.HostAppearance) error { called <- struct{}{}; return nil },
		HandleRequest: answerDone,
	})
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	defer listener.Close()
	server := s.GRPCServer()
	go server.Serve(listener)
	defer server.Stop()
	conn, err := grpc.NewClient("passthrough:///appearance", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := stream.Send(appearanceMsg("dark", 1)); err != nil {
		t.Fatal(err)
	}
	m, err := stream.Recv()
	if status.Code(err) != codes.FailedPrecondition || m != nil {
		t.Fatal(m, err)
	}
	select {
	case <-called:
		t.Fatal("an appearance before Ready reached the handler")
	default:
	}
}
