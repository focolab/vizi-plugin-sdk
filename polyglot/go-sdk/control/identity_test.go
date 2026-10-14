package control

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestHelloIdentityRefusalPrecedesLifecycle(t *testing.T) {
	ids := []string{"", "hello", "Hello from ", "Ready; I am peer", " Hello from peer", "Hello from \xff", "Hello from " + strings.Repeat("x", 2048), "Hello from " + strings.Repeat("é", 1024)}
	for r := rune(0); r <= 0x9f; r++ {
		if r <= 0x1f || r >= 0x7f {
			ids = append(ids, "Hello from build"+string(r))
		}
	}
	for i, id := range ids {
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), time.Second)
			defer cancel()
			var called bool
			s, err := NewServer(Config{PluginID: "identity.test", BuildIdentity: "plugin build abc", Attach: func(*Client) error { called = true; return nil }, Handshake: func(*pb.Hello) error { called = true; return nil }})
			if err != nil {
				t.Fatal(err)
			}
			stream := &recordingStream{stalledStream: &stalledStream{ctx: ctx, in: make(chan *pb.HostMsg, 1)}, out: make(chan *pb.PluginMsg, 1)}
			stream.in <- &pb.HostMsg{Id: id, Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}
			err = s.Session(stream)
			if status.Code(err) != codes.FailedPrecondition || called || len(stream.out) != 0 {
				t.Fatal("invalid Hello passed the identity gate", err, called)
			}
			if !strings.Contains(err.Error(), "Ready; I am plugin build abc") || !strings.Contains(err.Error(), identityPreview(id)) || strings.ContainsAny(err.Error(), "\n\r\x00") || len(err.Error()) > 1500 {
				t.Fatal("unsafe or incomplete identity diagnostic", err)
			}
		})
	}
}

func TestBuildIdentityValidatedAtConstruction(t *testing.T) {
	for _, build := range []string{"plugin\nforged", "plugin\u0085bad", "\xff", strings.Repeat("x", 2048-len(readyIdentityPrefix)+1)} {
		if _, err := NewServer(Config{PluginID: "plugin", BuildIdentity: build, Handshake: func(*pb.Hello) error { return nil }}); err == nil {
			t.Fatalf("invalid build identity accepted: %q", build)
		}
	}
	for _, build := range []string{"", "plugin 2.1 abc123", " ", "插件 ✨\u200d"} {
		if _, err := NewServer(Config{PluginID: "different.manifest.id", BuildIdentity: build, Handshake: func(*pb.Hello) error { return nil }}); err != nil {
			t.Fatal("extra identity restriction", err)
		}
	}
}

func TestHandshakeIdentitiesOnGRPC(t *testing.T) {
	// Free-form content is neither parsed nor matched to protocol/manifest IDs.
	const build = "插件 custom build abc123; Go SDK"
	p := openPeer(t, Config{BuildIdentity: build}, nil)
	if p.client.HostIdentity() != "Hello from test host 1.0" || !strings.HasPrefix(p.client.PluginIdentity(), "Ready; I am "+build+";") {
		t.Fatal("handshake identities not exposed", p.client.HostIdentity(), p.client.PluginIdentity())
	}
	// The handshake's nonempty ID must not become an ordinary reply waiter.
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(500)}}})
	if m := p.recv(t); m.GetShutdownReport() == nil || m.Id != "" {
		t.Fatal("handshake identity leaked into correlation", m)
	}
}

func TestHelloIdentityBoundAndFreeForm(t *testing.T) {
	for _, id := range []string{"Hello from  ", "Hello from arbitrary 插件\u200d", "Hello from " + strings.Repeat("é", 1018) + "x"} {
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		var seen atomic.Pointer[Client]
		s, err := NewServer(Config{PluginID: "plugin", BuildIdentity: "plugin build", Attach: func(c *Client) error { seen.Store(c); return nil }, Handshake: func(h *pb.Hello) error { h.ProtocolMajor = 99; return nil }})
		if err != nil {
			cancel()
			t.Fatal(err)
		}
		stream := &recordingStream{stalledStream: &stalledStream{ctx: ctx, in: make(chan *pb.HostMsg, 1)}, out: make(chan *pb.PluginMsg, 1)}
		stream.in <- &pb.HostMsg{Id: id, Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}
		done := make(chan error, 1)
		go func() { done <- s.Session(stream) }()
		var out *pb.PluginMsg
		select {
		case out = <-stream.out:
		case err := <-done:
			cancel()
			t.Fatal("valid identity refused", err)
		case <-ctx.Done():
			cancel()
			t.Fatal("Ready not sent", ctx.Err())
		}
		cancel()
		<-done
		if out.Id != s.identity || seen.Load().HostIdentity() != id {
			t.Fatal("valid free-form identity changed", out)
		}
	}
}
