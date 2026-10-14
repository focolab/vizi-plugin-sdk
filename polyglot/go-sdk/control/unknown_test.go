package control

import (
	"context"
	"errors"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
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

	// The id of an in-flight call identifies a reply. Complete only that call
	// with a local error, without replying UNSUPPORTED back to the host.
	done := make(chan error, 1)
	go func() { _, err := p.client.GetViewState(p.ctx); done <- err }()
	request := p.recv(t)
	p.send(t, futureHostMessage(request.Id))
	if err := <-done; !errors.Is(err, ErrUnsupportedReply) {
		t.Fatalf("future reply: %v", err)
	}
	refuse("after-reply")

	// Once cancellation has removed the waiter, an unknown kind with the old
	// id is a request under §11, even though a known late reply would be dropped.
	ctx, cancel := context.WithCancel(p.ctx)
	defer cancel()
	go func() { _, err := p.client.GetViewState(ctx); done <- err }()
	request = p.recv(t)
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
