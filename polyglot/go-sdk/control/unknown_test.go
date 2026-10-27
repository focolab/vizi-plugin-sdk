package control

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/encoding/protowire"
)

func futureHostMessage(id string) *pb.HostMsg {
	m := &pb.HostMsg{Id: id}
	m.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 10000, protowire.BytesType), []byte("future payload")))
	return m
}

func TestUnknownHostKinds(t *testing.T) {
	diagnostics := make(chan error, 1)
	p := openPeer(t, Config{Diagnostic: func(err error) { diagnostics <- err }}, nil)
	refuse := func(id string) {
		t.Helper()
		p.send(t, futureHostMessage(id))
		got := p.recv(t)
		if got.Id != id || got.GetError() == nil || got.GetError().Kind != pb.Error_UNSUPPORTED {
			t.Fatalf("unknown request: %v", got)
		}
	}
	refuse("host-future")

	// An empty-id push is only a local diagnostic. The next request is a wire
	// barrier proving no Error or ContractViolation was emitted for the push.
	p.send(t, futureHostMessage(""))
	select {
	case err := <-diagnostics:
		if !strings.Contains(err.Error(), "unknown HostMsg") {
			t.Fatal(err)
		}
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	refuse("after-push")

	done := make(chan error, 1)
	// Once cancellation has removed the waiter, an unknown kind with the old
	// id is a request under §11, even though a known late reply would be dropped.
	ctx, cancel := context.WithCancel(p.ctx)
	defer cancel()
	go func() { _, err := p.client.GetViewState(ctx); done <- err }()
	request := p.recv(t)
	cancel()
	if err := <-done; !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	refuse(request.Id)
	if count := p.client.UnmatchedReplies(); count != 0 {
		t.Fatalf("unknown kinds inflated unmatched reply count: %d", count)
	}

	// A subsequent ordinary call still completes normally.
	go func() { _, err := p.client.GetViewState(p.ctx); done <- err }()
	request = p.recv(t)
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
	if err := <-done; err != nil {
		t.Fatal(err)
	}
}

// An unknown kind under the id of a request the plugin has pending is not that
// request's reply: it faults the session, naming the id and the request, and
// the call fails with the fault instead of waiting for a reply.
func TestUnknownKindUnderPendingIDFaults(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	done := make(chan error, 1)
	go func() { _, err := p.client.GetViewState(p.ctx); done <- err }()
	request := p.recv(t)
	if !strings.HasPrefix(request.Id, "q") {
		t.Fatalf("plugin request id %q is not of the form q<n>", request.Id)
	}
	p.send(t, futureHostMessage(request.Id))
	expectFault(t, p, request.Id)
	err := <-done
	if err == nil {
		t.Fatal("an unknown kind under a pending id completed the call")
	}
	for _, name := range []string{"get_view_state", fmt.Sprintf("%q", request.Id), "does not know"} {
		if !strings.Contains(err.Error(), name) {
			t.Fatalf("the fault does not name %s: %v", name, err)
		}
	}
}

// The ledger keeps the ids of messages that deserve a reply apart from the ids
// of replies: an unknown kind under one of this end's request ids, then the
// real reply under it, is not a reused id.
func TestLedgerSeparatesRequestsFromReplies(t *testing.T) {
	var l idLedger
	if dup, err := l.observe(futureHostMessage("q1")); dup || err != nil {
		t.Fatal(dup, err)
	}
	if dup, err := l.observe(&pb.HostMsg{Id: "q1", Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}}); dup || err != nil {
		t.Fatalf("the reply after an unknown kind under its id: %v %v", dup, err)
	}
}

// The unknown-kind rule applies only after the handshake: a first message
// that is not Hello, of any kind (including one this SDK does not know),
// fails the session rather than being refused UNSUPPORTED.
func TestFirstMessageMustBeHello(t *testing.T) {
	for name, first := range map[string]*pb.HostMsg{
		"known":   {Id: "r1", Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}},
		"unknown": futureHostMessage("r1"),
	} {
		t.Run(name, func(t *testing.T) {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			s, err := NewServer(Config{PluginID: "test.plugin", BuildIdentity: "test.plugin test build", Handshake: func(*pb.Hello) error { return nil }, Attach: func(*Client) error { return nil }, Diagnostic: func(error) {}})
			if err != nil {
				t.Fatal(err)
			}
			l := bufconn.Listen(1 << 20)
			g := s.GRPCServer()
			go g.Serve(l)
			defer func() { g.Stop(); l.Close() }()
			conn, err := grpc.NewClient("passthrough:///test", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) { return l.Dial() }))
			if err != nil {
				t.Fatal(err)
			}
			defer conn.Close()
			stream, err := pb.NewPluginClient(conn).Session(ctx)
			if err != nil {
				t.Fatal(err)
			}
			if err := stream.Send(first); err != nil {
				t.Fatal(err)
			}
			got, err := stream.Recv()
			if status.Code(err) != codes.FailedPrecondition {
				t.Fatalf("first message %s: got %v, %v; want the session to fail", name, got, err)
			}
		})
	}
}
