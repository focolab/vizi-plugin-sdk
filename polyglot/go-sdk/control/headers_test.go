package control

import (
	"context"
	"net"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

func TestSessionHeadersBeforeHello(t *testing.T) {
	for _, valid := range []bool{true, false} {
		name := "valid Hello and immediate request"
		if !valid {
			name = "invalid Hello still rejected"
		}
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			var authenticated atomic.Bool
			connected, release := make(chan struct{}), make(chan struct{})
			defer close(release)
			s, err := NewServer(Config{
				PluginID:  "test.headers",
				Handshake: func(*pb.Hello) error { authenticated.Store(true); return nil },
				Connected: func(c *Client) {
					close(connected)
					select {
					case <-release:
					case <-c.ctx.Done():
					}
				},
				HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
					return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
				},
			})
			if err != nil {
				t.Fatal(err)
			}
			listener := bufconn.Listen(1 << 20)
			defer listener.Close()
			server := s.GRPCServer()
			go server.Serve(listener)
			defer server.Stop()
			conn, err := grpc.NewClient("passthrough:///headers", grpc.WithTransportCredentials(insecure.NewCredentials()),
				grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			stream, err := pb.NewPluginClient(conn).Session(ctx)
			if err != nil {
				t.Fatal(err)
			}
			// No request message has been sent. This real gRPC client waits for
			// response headers, reproducing a host that awaits Session before Hello.
			if _, err := stream.Header(); err != nil {
				t.Fatalf("headers must arrive before Hello: %v", err)
			}
			if err := ctx.Err(); err != nil {
				t.Fatalf("call expired while waiting for headers before Hello: %v", err)
			}
			if authenticated.Load() {
				t.Fatal("headers must not authenticate a session")
			}
			major := uint32(1)
			if !valid {
				major = 2
			}
			if err := stream.Send(&pb.HostMsg{Id: "Hello from headers test", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: major}}}); err != nil {
				t.Fatal(err)
			}
			first, err := stream.Recv()
			if !valid {
				if status.Code(err) != codes.FailedPrecondition || first != nil || authenticated.Load() {
					t.Fatalf("invalid Hello was not rejected: %v, %v", first, err)
				}
				return
			}
			if err != nil || first.GetReady() == nil || !authenticated.Load() {
				t.Fatalf("expected Ready after authentication: %v, %v", first, err)
			}
			// The request can follow Ready immediately, even while Connected is
			// deliberately parked. Channels, not a delay, hold that callback open.
			if err := stream.Send(&pb.HostMsg{Id: "immediate", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "run"}}}); err != nil {
				t.Fatal(err)
			}
			select {
			case <-connected:
			case <-ctx.Done():
				t.Fatal("Connected did not start")
			}
			reply, err := stream.Recv()
			if err != nil || reply.GetId() != "immediate" || !reply.GetCommandDone().GetOk() {
				t.Fatalf("request after Ready was gated by Connected: %v, %v", reply, err)
			}
		})
	}
}
