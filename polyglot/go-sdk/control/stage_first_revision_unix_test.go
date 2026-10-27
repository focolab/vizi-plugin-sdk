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

// A refused arm is still received:
// after revision 1 at a position is refused for an unknown Value kind or an
// unknown dtype, the host's re-arm at revision 2 with known content is
// acknowledged and is not the position's first, so no
// arrangement_first_revision is reported. A genuine first arm at revision 2,
// with nothing before it, still is.
func TestRefusedFirstStageArmCountsAsReceived(t *testing.T) {
	later := func() *pb.Value {
		v := &pb.Value{}
		v.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 7, protowire.BytesType), []byte{0x08, 0x01}))
		return v
	}
	for _, tc := range []struct {
		name   string
		refuse func(*pb.StageArm) // nil: nothing precedes revision 2
		report uint64
	}{
		{"unknown value kind", func(a *pb.StageArm) { a.Params = map[string]*pb.Value{"mode": later()} }, 0},
		{"unknown dtype", func(a *pb.StageArm) { a.Input.Dtype = 5 }, 0},
		{"genuine first at revision 2", nil, 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
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
			a := &pb.StageArm{StageId: "s", Pipeline: 1, Position: 1, Revision: 1, SegmentId: h.id, Input: stageShape(), Output: stageShape(), Params: params}
			if tc.refuse != nil {
				b := proto.Clone(a).(*pb.StageArm)
				tc.refuse(b)
				p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: b}})
				stageBarrier(t, p) // the refused arm drew no acknowledgement
				select {
				case err := <-diagnostics:
					if !strings.Contains(err.Error(), "refused, not acknowledged") {
						t.Fatal(err)
					}
				case <-time.After(3 * time.Second):
					t.Fatal("the refusal was silent")
				}
			}
			a.Revision = 2
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
			if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 2 {
				t.Fatal("revision 2 not acknowledged", ack)
			}
			if got := p.client.cfg.Reporter.Stats()[violations.ArrangementFirstRevision].Observed; got != tc.report {
				t.Fatal("arrangement_first_revision reports", got, "want", tc.report)
			}
		})
	}
}
