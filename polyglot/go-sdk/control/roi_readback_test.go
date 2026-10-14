package control

import (
	"errors"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestRoiInclusionWithoutDepositAndHostRefusals(t *testing.T) {
	registry, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer registry.Close()
	mem := NewMemory(registry)
	// There is deliberately no descriptor preparation hook or installed segment.
	// Empty inclusions must still route, and Memory.Release must owe nothing.
	p := openPeer(t, Config{Release: mem.Release}, nil)
	type result struct {
		reply *RoiInclusionReply
		err   error
	}
	results := make(chan result, 1)
	for _, present := range []bool{false, true} {
		go func() {
			r, err := p.client.ReadRoiInclusion(p.ctx, &pb.ReadRoiInclusion{RegionId: "region", T: 7, RoiMinZyx: []uint32{1, 2, 3}, RoiMaxZyx: []uint32{4, 5, 6}})
			results <- result{r, err}
		}()
		request := p.recv(t)
		if q := request.GetReadRoiInclusion(); q == nil || !proto.Equal(q, &pb.ReadRoiInclusion{RegionId: "region", T: 7, RoiMinZyx: []uint32{1, 2, 3}, RoiMaxZyx: []uint32{4, 5, 6}}) {
			t.Fatal(request)
		}
		expected := &pb.RoiInclusion{RegionId: "region", T: 7, OpenGeneration: 19, Present: present}
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_RoiInclusion{RoiInclusion: expected}})
		got := <-results
		if got.err != nil || got.reply == nil || !proto.Equal(got.reply.Inclusion, expected) {
			t.Fatal(got)
		}
		if err := got.reply.Close(); err != nil {
			t.Fatal(err)
		}
		if err := got.reply.Close(); err != nil {
			t.Fatal(err)
		}
	}
	for _, kind := range []pb.Error_Kind{pb.Error_NOT_FOUND, pb.Error_OUT_OF_BOUNDS, pb.Error_INVALID_ARGUMENT, pb.Error_UNSUPPORTED} {
		go func() {
			r, err := p.client.ReadRoiInclusion(p.ctx, &pb.ReadRoiInclusion{RegionId: "region", T: 7, RoiMinZyx: []uint32{1, 2, 3}, RoiMaxZyx: []uint32{4, 5, 6}})
			results <- result{r, err}
		}()
		request := p.recv(t) // No ReleaseDeposit may precede this request.
		if request.GetReadRoiInclusion() == nil {
			t.Fatal(request)
		}
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: kind, Message: "host detail"}}})
		got := <-results
		var hostErr *HostError
		if got.reply != nil || !errors.As(got.err, &hostErr) || hostErr.Kind != kind || hostErr.Message != "host detail" {
			t.Fatal(got)
		}
		if p.client.Context().Err() != nil {
			t.Fatal("ordinary refusal faulted session")
		}
	}
}

func TestOversizedRoiReadCanRetrySmallerWindow(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	type result struct {
		rois *pb.RoiList
		err  error
	}
	results := make(chan result, 1)
	for _, window := range []bool{false, true} {
		go func() {
			var r *pb.RoiList
			var err error
			if window {
				r, err = p.client.GetRoisWindow(p.ctx, 0, 100)
			} else {
				r, err = p.client.GetRois(p.ctx)
			}
			results <- result{r, err}
		}()
		request := p.recv(t)
		if request.GetGetRois() == nil {
			t.Fatal(request)
		}
		detail := "GetRois reply size 128 exceeds limit 64; request fewer frames"
		p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_OUT_OF_BOUNDS, Message: detail}}})
		got := <-results
		var hostErr *HostError
		if got.rois != nil || !errors.As(got.err, &hostErr) || hostErr.Kind != pb.Error_OUT_OF_BOUNDS || hostErr.Message != detail {
			t.Fatal(got)
		}
	}
	go func() { r, err := p.client.GetRoisWindow(p.ctx, 0, 0); results <- result{r, err} }()
	request := p.recv(t)
	q := request.GetGetRois()
	if q == nil || q.PoseTStart == nil || q.PoseTEnd == nil || *q.PoseTStart != 0 || *q.PoseTEnd != 0 {
		t.Fatal(request)
	}
	expected := &pb.RoiList{Rois: []*pb.RoiRecord{{RegionId: "region", PoseDomainTStart: proto.Int32(0), PoseDomainTEnd: proto.Int32(100), Roi: &pb.Roi{TStart: proto.Int32(0), TEnd: proto.Int32(0), PoseTranslation: []float64{1, 2, 3}}}}}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_Rois{Rois: expected}})
	got := <-results
	if got.err != nil || !proto.Equal(got.rois, expected) {
		t.Fatal(got)
	}
	if p.client.Context().Err() != nil {
		t.Fatal("oversize refusal faulted session")
	}
}
