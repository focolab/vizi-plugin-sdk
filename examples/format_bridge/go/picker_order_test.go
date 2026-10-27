package main

import (
	"context"
	"net"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"
)

// The typed SDK continuation must acknowledge before asking for a file.
func TestPickerAcknowledgementOrdering(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	workerDone := make(chan error, 1)
	h := plugin.Handlers{Commands: map[string]plugin.CommandHandler{
		"crate.pick_calibration": func(context.Context, *control.Client, plugin.Args) (plugin.Result, error) {
			return plugin.Result{AfterReply: func(ctx context.Context, c *control.Client) error {
				_, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "calibration_override", ForceReprompt: true})
				workerDone <- err
				return err
			}}, nil
		},
	}}
	cfg, err := h.Configure(control.Config{PluginID: "picker-order-test", Handshake: func(*pb.Hello) error { return nil }})
	if err != nil {
		t.Fatal(err)
	}
	srv, err := control.NewServer(cfg)
	if err != nil {
		t.Fatal(err)
	}
	listener := bufconn.Listen(1 << 20)
	server := srv.GRPCServer()
	go server.Serve(listener)
	defer server.Stop()
	defer listener.Close()
	conn, err := grpc.NewClient("passthrough:///picker-test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return listener.Dial() }))
	if err != nil {
		t.Fatal(err)
	}
	defer conn.Close()
	stream, err := pb.NewPluginClient(conn).Session(ctx)
	if err != nil {
		t.Fatal(err)
	}
	send := func(m *pb.HostMsg) {
		t.Helper()
		if err := stream.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	recv := func() *pb.PluginMsg {
		t.Helper()
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return m
	}
	send(&pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
	if recv().GetReady() == nil {
		t.Fatal("no Ready")
	}
	send(&pb.HostMsg{Id: "cmd", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "crate.pick_calibration"}}})
	if done := recv(); done.Id != "cmd" || !done.GetCommandDone().GetOk() {
		t.Fatal("acknowledgement did not come first", done)
	}
	first := recv()
	if first.GetRequestAsset() == nil {
		t.Fatal("expected picker after acknowledgement", first)
	}
	send(&pb.HostMsg{Id: first.Id, Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{Declined: true}}})
	select {
	case err := <-workerDone:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}
