package control

import (
	"errors"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
)

var testIDs atomic.Uint64

// freshID is a host request id never used before in this test binary: a
// receiver faults a session on an id reused with different content.
func freshID(prefix string) string {
	return fmt.Sprintf("%s-%d", prefix, testIDs.Add(1))
}

func expectFault(t *testing.T, p *peer, id string) {
	t.Helper()
	for {
		got, err := p.stream.Recv()
		if err == nil {
			if got.GetContractViolation() != nil || got.GetShutdownReport() != nil {
				continue
			}
			t.Fatalf("session continued after reuse of %q: %v", id, got)
		}
		if status.Code(err) != codes.InvalidArgument || !strings.Contains(err.Error(), fmt.Sprintf("%q", id)) {
			t.Fatalf("reuse of %q: %v", id, err)
		}
		return
	}
}

// A request id the host has used is never used again: a byte-identical
// re-send is deduplicated (no second dispatch, no second answer), and the
// same id with different content faults the session, naming the id.
func TestReusedRequestID(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	unsupported := func(id string) {
		t.Helper()
		got := p.recv(t)
		if got.Id != id || got.GetError().GetKind() != pb.Error_UNSUPPORTED {
			t.Fatalf("want UNSUPPORTED for %q: %v", id, got)
		}
	}
	p.send(t, futureHostMessage("r1"))
	unsupported("r1")
	p.send(t, futureHostMessage("r1")) // a re-send: deduplicated
	p.send(t, futureHostMessage("r2")) // barrier: its answer is the next message
	unsupported("r2")

	other := &pb.HostMsg{Id: "r1"}
	other.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 10000, protowire.BytesType), []byte("other payload")))
	p.send(t, other)
	expectFault(t, p, "r1")
}

// A reply id is tracked the same way: a re-sent reply is dropped, and a
// different reply under the same id faults the session.
func TestReusedReplyID(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	done := make(chan error, 1)
	go func() { _, err := p.client.GetViewState(p.ctx); done <- err }()
	request := p.recv(t)
	reply := &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}}
	p.send(t, reply)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	p.send(t, reply) // a re-send: deduplicated
	p.send(t, futureHostMessage("r1"))
	if got := p.recv(t); got.Id != "r1" || got.GetError() == nil {
		t.Fatalf("barrier: %v", got)
	}
	if n := p.client.UnmatchedReplies(); n != 0 {
		t.Fatalf("a deduplicated reply counted as unmatched: %d", n)
	}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_INTERNAL}}})
	expectFault(t, p, request.Id)
}

// The ledger remembers the last IDWindow ids received; an older id is not
// checked, so the duplicate check never grows without bound.
func TestIDWindowSlides(t *testing.T) {
	var l idLedger
	query := func(id string, segment uint64) *pb.HostMsg {
		return &pb.HostMsg{Id: id, Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: segment}}}
	}
	for i := 0; i <= IDWindow; i++ {
		if dup, err := l.observe(query(fmt.Sprintf("r%d", i), 1)); dup || err != nil {
			t.Fatal(i, dup, err)
		}
	}
	if len(l.requests) != IDWindow || len(l.window) != IDWindow {
		t.Fatalf("window holds %d ids, %d entries", len(l.requests), len(l.window))
	}
	if dup, err := l.observe(query("r0", 2)); dup || err != nil {
		t.Fatalf("an id older than the window is not checked: %v %v", dup, err)
	}
	if _, err := l.observe(query("r2", 2)); err == nil {
		t.Fatal("an id inside the window is checked")
	}
}

// A pending request's id belongs to its reply alone: a known reply of another
// kind under it faults the session, naming the id, the request and the reply,
// and the waiter never receives it.
func TestWrongReplyKind(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	type result struct {
		rois *pb.RoiList
		err  error
	}
	done := make(chan result, 1)
	go func() { rois, err := p.client.GetRois(p.ctx); done <- result{rois, err} }()
	request := p.recv(t)
	if request.GetGetRois() == nil {
		t.Fatalf("want get_rois: %v", request)
	}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
	expectFault(t, p, request.Id)
	got := <-done
	if got.rois != nil || got.err == nil {
		t.Fatalf("the waiter received a reply of the wrong kind: %v %v", got.rois, got.err)
	}
	for _, name := range []string{"get_rois", "view_state", fmt.Sprintf("%q", request.Id)} {
		if !strings.Contains(got.err.Error(), name) {
			t.Fatalf("the fault does not name %s: %v", name, got.err)
		}
	}
}

// host_error is every request's reply, as is the request's own reply kind.
func TestHostErrorAnswersAnyRequest(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	done := make(chan error, 1)
	go func() { _, err := p.client.GetRois(p.ctx); done <- err }()
	request := p.recv(t)
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_NOT_FOUND, Message: "no dataset"}}})
	var hostErr *HostError
	if err := <-done; !errors.As(err, &hostErr) || hostErr.Kind != pb.Error_NOT_FOUND {
		t.Fatalf("want the host's NOT_FOUND: %v", err)
	}
	p.send(t, futureHostMessage("r1"))
	if got := p.recv(t); got.Id != "r1" || got.GetError() == nil {
		t.Fatalf("the session did not continue: %v", got)
	}
}
