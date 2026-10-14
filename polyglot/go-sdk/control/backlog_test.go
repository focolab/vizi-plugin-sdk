package control

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestDescriptorBacklogFaultsAtThirtyThirdUninstalledReply(t *testing.T) {
	registry, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer registry.Close()
	mem := NewMemory(registry)
	p := openPeer(t, Config{Admit: mem.Admit, Prepare: mem.WaitDescriptor, Release: mem.Release, Arrange: mem.Apply}, nil)
	for i := 0; i < 32; i++ {
		p.send(t, &pb.HostMsg{Id: fmt.Sprintf("late-%d", i), Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: &pb.SegmentRef{SegmentId: 17, Offset: uint64(128 + 64*i), Len: 4}}}})
	}
	// Arrangements/queries are still serviced despite the full descriptor hold.
	p.send(t, &pb.HostMsg{Id: "barrier", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
	if got := p.recv(t); got.Id != "barrier" || got.GetGrantState() == nil {
		t.Fatal(got)
	}
	p.send(t, &pb.HostMsg{Id: "overflow", Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 4096, Len: 4}}}})
	if _, err := p.stream.Recv(); err == nil || !strings.Contains(err.Error(), "descriptor preparation backlog exceeds 32") {
		t.Fatalf("expected explicit backlog fault, got %v", err)
	}
}

func TestOutgoingRequestLimitIsLocal(t *testing.T) {
	p := openPeer(t, Config{MaxPendingRequests: 1}, nil)
	done := make(chan error, 1)
	ctx, cancel := context.WithCancel(p.ctx)
	defer cancel()
	go func() { _, err := p.client.GetViewState(ctx); done <- err }()
	first := p.recv(t)
	if _, err := p.client.GetViewState(p.ctx); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	p.send(t, &pb.HostMsg{Id: first.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	if p.client.Context().Err() != nil {
		t.Fatal("local request refusal faulted session")
	}
}
