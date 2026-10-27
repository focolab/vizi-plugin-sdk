//go:build (linux || darwin) && (amd64 || arm64)

package control

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

// An arm naming a dtype this build does not know is refused alone: not
// acknowledged, diagnosed with the dtype, never a ContractViolation, and the
// session continues.
func TestStageArmUnknownDTypeRefusedPerItem(t *testing.T) {
	h := stageRing(t)
	diagnostics := make(chan error, 8)
	p := h.peer(t, map[string]StageHandlers{"s": {Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { return nil }}},
		Config{Diagnostic: func(err error) { diagnostics <- err }})
	p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape()}}})
	if stageRecv(t, p).GetStagePlanned() == nil {
		t.Fatal("missing plan")
	}
	a := &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: 1, SegmentId: h.id, Input: stageShape(), Output: stageShape()}
	for _, change := range []func(*pb.StageArm){
		func(a *pb.StageArm) { a.Input.Dtype = 5 },
		func(a *pb.StageArm) { a.Output.Dtype = 1000 },
	} {
		b := proto.Clone(a).(*pb.StageArm)
		change(b)
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: b}})
		stageBarrier(t, p)
		select {
		case err := <-diagnostics:
			if !strings.Contains(err.Error(), "is not a DType this build knows") {
				t.Fatal(err)
			}
		case <-time.After(3 * time.Second):
			t.Fatal("the refusal was silent")
		}
	}
	for site, counts := range p.client.cfg.Reporter.Stats() {
		if counts.Observed != 0 {
			t.Fatal("unknown dtype reported as a violation", site, counts)
		}
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 1 {
		t.Fatal("the session did not continue", ack)
	}
}

// An arm whose params carry a Value kind this build does not know (a later
// minor's oneof arm in place of a known one) is refused like an unknown dtype:
// not acknowledged, diagnosed with the kind, never a ContractViolation, and
// the plan stays planned, so a re-arm with a known kind is acknowledged.
func TestStageArmUnknownOneofArmRefusedPerItem(t *testing.T) {
	h := stageRing(t)
	diagnostics := make(chan error, 8)
	handler := StageHandlers{Params: []manifest.StageParam{{Key: "mode", Type: "int", Value: 1, Min: 1, Max: 2}},
		Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { return nil }}
	p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{Diagnostic: func(err error) { diagnostics <- err }})
	params := map[string]*pb.Value{"mode": {Kind: &pb.Value_I{I: 1}}}
	p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: params}}})
	if stageRecv(t, p).GetStagePlanned() == nil {
		t.Fatal("missing plan")
	}
	later := &pb.Value{}
	later.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 7, protowire.BytesType), []byte{0x08, 0x01}))
	a := &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: 1, SegmentId: h.id, Input: stageShape(), Output: stageShape(),
		Params: map[string]*pb.Value{"mode": later}}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	stageBarrier(t, p)
	select {
	case err := <-diagnostics:
		if !strings.Contains(err.Error(), `params["mode"] Value.kind (field 7) is a oneof arm this build does not know`) {
			t.Fatal(err)
		}
	case <-time.After(3 * time.Second):
		t.Fatal("the refusal was silent")
	}
	for site, counts := range p.client.cfg.Reporter.Stats() {
		if counts.Observed != 0 {
			t.Fatal("unknown oneof arm reported as a violation", site, counts)
		}
	}
	a.Params = params
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 1 {
		t.Fatal("the plan did not stay planned for a re-arm", ack)
	}
}

// A plan's identity and an arm's content are their known fields: an unknown
// field on either side of the plan-and-arm match, or on a restatement, changes
// nothing.
func TestStageMatchAndRestatementCompareKnownFieldsOnly(t *testing.T) {
	for _, tc := range []struct {
		name      string
		planInput *pb.StageShape
		armInput  *pb.StageShape
	}{
		{"arm input only", stageShape(), withUnknown(stageShape())},
		{"plan input only", withUnknown(stageShape()), stageShape()},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := stageRing(t)
			p := h.peer(t, map[string]StageHandlers{"s": {Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { return nil }}}, Config{})
			p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: withUnknown(&pb.PlanStage{StageId: "s", Input: tc.planInput})}})
			if stageRecv(t, p).GetStagePlanned() == nil {
				t.Fatal("missing plan")
			}
			a := &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: 1, SegmentId: h.id, Input: tc.armInput, Output: stageShape()}
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
			if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 1 {
				t.Fatal("arm not matched to its plan", ack)
			}
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: withUnknown(a)}})
			if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 1 {
				t.Fatal("restatement not acknowledged again", ack)
			}
			if got := p.client.cfg.Reporter.Stats()[violations.ArrangementContentMismatch].Observed + p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != 0 {
				t.Fatal("unknown fields reported", got)
			}
		})
	}
}
