//go:build (linux || darwin) && (amd64 || arm64)

package control

import (
	"context"
	"encoding/binary"
	"sync"
	"sync/atomic"
	"testing"
	"unsafe"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestStageRearmAcknowledgedDuringTurn(t *testing.T) {
	h := stageRing(t)
	h.regions = []*pb.Region{
		{Index: 0, Offset: 64, Extent: 384, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_STAGED, Producer: pb.RegionProducer_REGION_PRODUCER_HOST},
		{Index: 1, Offset: 512, Extent: 384, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_STAGED, Producer: pb.RegionProducer_REGION_PRODUCER_HOST},
	}
	second := &stageHost{id: h.id, data: h.data[448:], word: (*uint64)(unsafe.Pointer(&h.data[512]))}
	started, release := make(chan struct{}), make(chan struct{})
	var once sync.Once
	free := func() { once.Do(func() { close(release) }) }
	seen := make(chan uint32, 4)
	handler := StageHandlers{Params: []manifest.StageParam{{Key: "mode", Type: "int", Value: 1, Min: 1, Max: 2}},
		PlanWithParams: func(_ context.Context, _ *Client, q *pb.PlanStage, params stage.Params) (*pb.StagePlanned, error) {
			out := proto.Clone(q.Input).(*pb.StageShape)
			reads := []uint32{4}
			if params["mode"] == int64(2) {
				out.Dtype = pb.DType_DTYPE_F32
				reads = []uint32{7}
			}
			return &pb.StagePlanned{Output: out, Reads: reads}, nil
		},
		Process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			seen <- a.Revision
			if a.Revision == 1 {
				close(started)
				<-release
			} else {
				s.Layout.DType = pb.DType_DTYPE_F32
			}
			if s.Params["mode"] != int64(a.Revision) {
				t.Error("wrong armed params", s.Params, a.Revision)
			}
			return nil
		},
	}
	p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{})
	t.Cleanup(free) // unblock before peer cleanup if any assertion fails
	plan := func(mode int64) *pb.StageArm {
		t.Helper()
		params := map[string]*pb.Value{"mode": {Kind: &pb.Value_I{I: mode}}}
		p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: params}}})
		out := stageRecv(t, p).GetStagePlanned()
		if out == nil {
			t.Fatal("missing plan")
		}
		return &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: uint32(mode), SegmentId: h.id, Input: stageShape(), Output: out.Output, Params: params}
	}
	arm := func(a *pb.StageArm) {
		t.Helper()
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
		if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != a.Revision {
			t.Fatal(ack)
		}
	}
	arm(plan(1))
	h.fill(1, 1, 1)
	select {
	case <-started:
	case <-p.ctx.Done():
		t.Fatal("turn never started")
	}
	// Ack must arrive while the previous revision is still held by author code.
	arm(plan(2))
	if atomic.LoadUint64(h.word)>>48 != 1 {
		t.Fatal("old turn handed off before release")
	}
	// The next slot lacks revision 2's read-only channel. It must refuse BEFORE
	// calling the author, even though revision 1 was allowed to read channel 4.
	second.fill(1, 1, 0)
	binary.LittleEndian.PutUint16(second.data[64+258:], 8)
	atomic.StoreUint64(second.word, uint64(1)|uint64(1)<<32|uint64(1)<<48)
	free()
	h.wait(t, 2)
	second.wait(t, -1)
	if binary.LittleEndian.Uint32(h.data[64+8:]) != uint32(stage.Processed) || binary.LittleEndian.Uint32(h.data[64+28:]) != uint32(pb.DType_DTYPE_U8) {
		t.Fatal("old turn held to new output")
	}
	if binary.LittleEndian.Uint32(second.data[64+24:]) != 2 {
		t.Fatal("missing reads not refused")
	}
	if got := <-seen; got != 1 || len(seen) != 0 {
		t.Fatal("unexpected author call", got, len(seen))
	}
	second.fill(2, 1, 1)
	second.wait(t, 2)
	if got := <-seen; got != 2 {
		t.Fatal("next turn retained old arm", got)
	}
	if binary.LittleEndian.Uint32(second.data[64+28:]) != uint32(pb.DType_DTYPE_F32) {
		t.Fatal("new output was not enforced")
	}
}

func TestStageRefusesUnusableShapesAndMissingPlans(t *testing.T) {
	h := stageRing(t)
	p := h.peer(t, map[string]StageHandlers{"s": {Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { return nil }}}, Config{})
	a := &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: 1, SegmentId: h.id, Input: stageShape(), Output: stageShape()}
	refuse := func(q *pb.StageArm) {
		t.Helper()
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: q}})
		stageBarrier(t, p)
	}
	refuse(a) // no answered plan
	p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape()}}})
	if stageRecv(t, p).GetStagePlanned() == nil {
		t.Fatal("missing plan")
	}
	for _, change := range []func(*pb.StageArm){
		func(a *pb.StageArm) { a.Input = nil }, func(a *pb.StageArm) { a.Output = nil },
		func(a *pb.StageArm) { a.Input.Channels = []uint32{4, 4} },
		func(a *pb.StageArm) { a.Output.X = 0 },
	} {
		b := proto.Clone(a).(*pb.StageArm)
		change(b)
		refuse(b)
	}
	if got := p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != 5 {
		t.Fatal(got)
	}
}
