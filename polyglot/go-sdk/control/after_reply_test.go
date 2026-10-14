package control

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestAfterReplyOrderingAndWorkerBound(t *testing.T) {
	var saved context.Context
	started := make(chan struct{})
	finished := make(chan struct{})
	p := openPeer(t, Config{MaxPendingRequests: 1, HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
		saved = ctx
		err := AfterReply(ctx, func(ctx context.Context, c *Client) error {
			close(started)
			defer close(finished)
			_, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "test"})
			return err
		})
		return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, err
	}}, nil)
	invoke := func(id string) {
		p.send(t, &pb.HostMsg{Id: id, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "pick"}}})
	}
	invoke("one")
	if m := p.recv(t); m.Id != "one" || !m.GetCommandDone().GetOk() {
		t.Fatal(m)
	}
	req := p.recv(t)
	if req.GetRequestAsset() == nil {
		t.Fatal(req)
	}
	<-started
	if err := AfterReply(saved, func(context.Context, *Client) error { return nil }); err == nil {
		t.Fatal("retained handler context accepted")
	}
	invoke("two")
	if m := p.recv(t); m.Id != "two" || m.GetCommandDone().GetOk() {
		t.Fatal("worker bound not enforced", m)
	}
	p.send(t, &pb.HostMsg{Id: req.Id, Msg: &pb.HostMsg_AssetResult{AssetResult: &pb.AssetResult{Declined: true}}})
	select {
	case <-finished:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
}

func TestAfterReplySuppressesFailedHandlers(t *testing.T) {
	for _, mode := range []string{"error", "panic", "nil", "wrong-kind", "failed-command"} {
		t.Run(mode, func(t *testing.T) {
			var ran atomic.Bool
			p := openPeer(t, Config{MaxPendingRequests: 1, HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
				if err := AfterReply(ctx, func(context.Context, *Client) error { ran.Store(true); return nil }); err != nil {
					t.Fatal(err)
				}
				switch mode {
				case "error":
					return nil, errors.New("refused")
				case "panic":
					panic("refused")
				case "nil":
					return nil, nil
				case "wrong-kind":
					return &pb.PluginMsg{Msg: &pb.PluginMsg_Closed{Closed: &pb.Closed{}}}, nil
				default:
					return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: false}}}, nil
				}
			}}, nil)
			for _, id := range []string{"one", "two"} {
				p.send(t, &pb.HostMsg{Id: id, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "test"}}})
				if m := p.recv(t); m.Id != id || successfulReply(m) {
					t.Fatal(m)
				}
			}
			if ran.Load() || len(p.client.afterReplySlots) != 0 {
				t.Fatal("failed reply retained or started continuation")
			}
		})
	}
}

func TestAfterReplyWaitsForSendAndSuppressesSendFailure(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	stream := &stalledStream{ctx: ctx, in: make(chan *pb.HostMsg, 2), blocked: make(chan struct{})}
	var ran atomic.Bool
	attached := make(chan *Client, 1)
	srv, err := NewServer(Config{PluginID: "after-reply-test", Handshake: func(*pb.Hello) error { return nil }, Attach: func(c *Client) error { attached <- c; return nil }, HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
		err := AfterReply(ctx, func(context.Context, *Client) error { ran.Store(true); return nil })
		return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, err
	}})
	if err != nil {
		t.Fatal(err)
	}
	stream.in <- &pb.HostMsg{Id: "Hello from test host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}}
	stream.in <- &pb.HostMsg{Id: "cmd", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "test"}}}
	done := make(chan error, 1)
	go func() { done <- srv.Session(stream) }()
	c := <-attached
	select {
	case <-stream.blocked:
	case <-time.After(time.Second):
		t.Fatal("no reply attempted")
	}
	if ran.Load() {
		t.Fatal("work started before Send completed")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("session did not end")
	}
	deadline := time.Now().Add(time.Second)
	for len(c.afterReplySlots) != 0 && time.Now().Before(deadline) {
		time.Sleep(time.Millisecond)
	}
	if ran.Load() || len(c.afterReplySlots) != 0 {
		t.Fatal("send failure started or retained work")
	}
}

func TestAfterReplyAdmissionFailureReclaimsWorkerSlot(t *testing.T) {
	var ran atomic.Bool
	p := openPeer(t, Config{AdmitOutbound: func(m *pb.PluginMsg) error {
		if m.GetCommandDone() != nil {
			return errors.New("test admission refusal")
		}
		return nil
	}, HandleRequest: func(ctx context.Context, c *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
		err := AfterReply(ctx, func(context.Context, *Client) error { ran.Store(true); return nil })
		return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, err
	}}, nil)
	p.send(t, &pb.HostMsg{Id: "cmd", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "test"}}})
	select {
	case <-p.client.Context().Done():
	case <-p.ctx.Done():
		t.Fatal("admission refusal did not end session")
	}
	if ran.Load() || len(p.client.afterReplySlots) != 0 {
		t.Fatal("refused reply started or retained continuation")
	}
}

func TestAfterReplyCallbackFailureIsDiagnostic(t *testing.T) {
	for _, panics := range []bool{false, true} {
		t.Run(fmt.Sprint(panics), func(t *testing.T) {
			diagnostic := make(chan error, 2)
			p := openPeer(t, Config{Diagnostic: func(err error) { diagnostic <- err }, HandleRequest: func(ctx context.Context, c *Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
				if in.Id == "first" {
					if err := AfterReply(ctx, func(context.Context, *Client) error {
						if panics {
							panic("test callback")
						}
						return errors.New("test callback")
					}); err != nil {
						return nil, err
					}
				}
				return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
			}}, nil)
			for _, id := range []string{"first", "second"} {
				p.send(t, &pb.HostMsg{Id: id, Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "test"}}})
				if m := p.recv(t); m.Id != id || !m.GetCommandDone().GetOk() {
					t.Fatal(m)
				}
				if id == "first" {
					select {
					case err := <-diagnostic:
						if !strings.Contains(err.Error(), "test callback") {
							t.Fatal(err)
						}
					case <-p.ctx.Done():
						t.Fatal("callback failure not diagnosed")
					}
				}
			}
			if len(p.client.afterReplySlots) != 0 {
				t.Fatal("callback failure leaked worker slot")
			}
		})
	}
}
