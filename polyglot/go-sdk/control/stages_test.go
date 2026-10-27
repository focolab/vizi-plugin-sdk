package control

import (
	"context"
	"errors"
	"google.golang.org/protobuf/proto"
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestStageRoutingAndUnarmedSession(t *testing.T) {
	p := openPeer(t, Config{HandleRequest: func(_ context.Context, _ *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
		if q := m.GetPlanStage(); q != nil {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_StagePlanned{StagePlanned: &pb.StagePlanned{Output: q.Input}}}, nil
		}
		return nil, &HostError{Kind: pb.Error_UNSUPPORTED, Message: "unused"}
	}}, nil)
	if HostClass(&pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: &pb.StageArm{}}}) != Arrangement {
		t.Fatal("stage arm is not an arrangement")
	}
	p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: &pb.StageShape{Dtype: pb.DType_DTYPE_U8, Z: 1, Y: 1, X: 1}}}})
	if got := p.recv(t); got.Id != "plan" || got.GetStagePlanned() == nil {
		t.Fatal(got)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: &pb.StageArm{StageId: "s", Revision: 1, SegmentId: 1, Position: 1, Pipeline: 1}}})
	p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "none"}}})
	got := p.recv(t)
	if got.GetContractViolation() != nil {
		got = p.recv(t)
	}
	if !strings.HasPrefix(got.Id, "barrier") {
		t.Fatal("unusable arm was acknowledged or session stopped", got)
	}
}

func TestStagePlanErrorsOverControl(t *testing.T) {
	for _, mode := range []string{"panic", "exception", "refusal", "invalid plan", "protocol bound", "invalid input", "unknown dtype"} {
		t.Run(mode, func(t *testing.T) {
			called := false
			p := openPeer(t, Config{HandleRequest: func(_ context.Context, _ *Client, m *pb.HostMsg) (*pb.PluginMsg, error) {
				called = true
				switch mode {
				case "panic":
					panic("author bug")
				case "exception":
					return nil, errors.New("author bug")
				case "refusal":
					return nil, &HostError{Kind: pb.Error_UNSUPPORTED, Message: "declined"}
				}
				out := proto.Clone(m.GetPlanStage().Input).(*pb.StageShape)
				if mode == "invalid plan" {
					out.X++
				}
				if mode == "protocol bound" {
					out.Channels = []uint32{65536}
				}
				return &pb.PluginMsg{Msg: &pb.PluginMsg_StagePlanned{StagePlanned: &pb.StagePlanned{Output: out}}}, nil
			}}, nil)
			shape := &pb.StageShape{Dtype: pb.DType_DTYPE_U8, X: 1, Y: 1, Z: 1}
			if mode == "invalid input" {
				shape.Dtype = pb.DType_DTYPE_UNSPECIFIED
			}
			if mode == "unknown dtype" {
				shape.Dtype = 99 // a later minor's: this plan alone is refused
			}
			p.send(t, &pb.HostMsg{Id: "plan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: shape}}})
			got := p.recv(t)
			want := pb.Error_INTERNAL
			if mode == "refusal" || mode == "unknown dtype" {
				want = pb.Error_UNSUPPORTED
			}
			if mode == "protocol bound" || mode == "invalid input" {
				want = pb.Error_PROTOCOL
			}
			if got.Id != "plan" || got.GetError() == nil || got.GetError().Kind != want {
				t.Fatal(got, "want", want)
			}
			if mode == "unknown dtype" && !strings.Contains(got.GetError().Message, "dtype 99") {
				t.Fatal("the refusal does not name the dtype", got)
			}
			if (mode == "invalid input" || mode == "unknown dtype") && called {
				t.Fatal("invalid input reached raw author callback")
			}
			// The refusal is the plan's alone: the session continues.
			if mode == "unknown dtype" {
				shape.Dtype = pb.DType_DTYPE_U8
				p.send(t, &pb.HostMsg{Id: "again", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: shape}}})
				if got := p.recv(t); got.Id != "again" || got.GetStagePlanned() == nil {
					t.Fatal(got)
				}
			}
		})
	}
}
