//go:build (linux || darwin) && (amd64 || arm64)

package control

import (
	"context"
	"fmt"
	"math"
	"sync/atomic"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestStageParametersRejectedOnBothMessages(t *testing.T) {
	h := stageRing(t)
	var calls atomic.Int32
	specs := []manifest.StageParam{
		{Key: "f", Type: "float", Value: 2, Min: 0, Max: 5},
		{Key: "i", Type: "int", Value: 2, Min: 0, Max: 5},
		{Key: "b", Type: "bool", Value: false},
		{Key: "s", Type: "choice", Value: "A", Options: []string{"A", "B"}},
		{Key: "text", Type: "string", Value: "é", MaxLength: 2},
	}
	good := map[string]*pb.Value{"f": {Kind: &pb.Value_F{F: 2}}, "i": {Kind: &pb.Value_I{I: 2}}, "b": {Kind: &pb.Value_B{B: false}}, "s": {Kind: &pb.Value_S{S: "A"}}}
	good["text"] = &pb.Value{Kind: &pb.Value_S{S: "é"}}
	p := h.peer(t, map[string]StageHandlers{"s": {Params: specs, PlanWithParams: func(_ context.Context, _ *Client, q *pb.PlanStage, params stage.Params) (*pb.StagePlanned, error) {
		calls.Add(1)
		if params["f"] != float64(2) || params["i"] != int64(2) || params["b"] != false || params["s"] != "A" || params["text"] != "é" {
			return nil, fmt.Errorf("wrong plain params: %v", params)
		}
		return &pb.StagePlanned{Output: q.Input}, nil
	}, Process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
		if s.Params["text"] != "é" {
			return fmt.Errorf("string changed before processing: %v", s.Params)
		}
		return nil
	}}}, Config{})
	p.send(t, &pb.HostMsg{Id: "good", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: good}}})
	if out := stageRecv(t, p); out.GetStagePlanned() == nil {
		t.Fatal(out)
	}
	a := &pb.StageArm{StageId: "s", Revision: 1, Pipeline: 1, Position: 1, SegmentId: h.id, Input: stageShape(), Output: stageShape(), Params: good}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 1 {
		t.Fatal(ack)
	}
	cases := []struct {
		name, key string
		value     *pb.Value
	}{
		{"missing", "f", nil},
		{"extra", "extra", &pb.Value{Kind: &pb.Value_I{I: 1}}},
		{"unset", "f", &pb.Value{}},
		{"float as int", "f", &pb.Value{Kind: &pb.Value_I{I: 2}}},
		{"int as float", "i", &pb.Value{Kind: &pb.Value_F{F: 2}}},
		{"bool as int", "b", &pb.Value{Kind: &pb.Value_I{I: 0}}},
		{"choice as bool", "s", &pb.Value{Kind: &pb.Value_B{B: true}}},
		{"float below min", "f", &pb.Value{Kind: &pb.Value_F{F: -1}}},
		{"float above max", "f", &pb.Value{Kind: &pb.Value_F{F: 6}}},
		{"int below min", "i", &pb.Value{Kind: &pb.Value_I{I: -1}}},
		{"int above max", "i", &pb.Value{Kind: &pb.Value_I{I: 6}}},
		{"NaN", "f", &pb.Value{Kind: &pb.Value_F{F: math.NaN()}}},
		{"positive infinity", "f", &pb.Value{Kind: &pb.Value_F{F: math.Inf(1)}}},
		{"negative infinity", "f", &pb.Value{Kind: &pb.Value_F{F: math.Inf(-1)}}},
		{"choice case mismatch", "s", &pb.Value{Kind: &pb.Value_S{S: "a"}}},
		{"string as int", "text", &pb.Value{Kind: &pb.Value_I{I: 1}}},
		{"string byte bound", "text", &pb.Value{Kind: &pb.Value_S{S: "é!"}}},
	}
	for i, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			bad := proto.Clone(a).(*pb.StageArm)
			bad.Revision = 2
			if tc.value == nil {
				delete(bad.Params, tc.key)
			} else {
				bad.Params[tc.key] = tc.value
			}
			p.send(t, &pb.HostMsg{Id: freshID("bad"), Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: bad.Params}}})
			out := stageRecv(t, p)
			if out.GetError() == nil || out.GetError().Kind != pb.Error_INVALID_ARGUMENT || calls.Load() != 1 {
				t.Fatal(out, calls.Load())
			}
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: bad}})
			stageBarrier(t, p) // Fails if the unusable arm was acknowledged.
			if got := p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != uint64(i+1) {
				t.Fatal(got)
			}
		})
	}
	// Valid values still need their own successful plan before a new arm.
	changed := proto.Clone(a).(*pb.StageArm)
	changed.Revision = 2
	changed.Params["f"] = &pb.Value{Kind: &pb.Value_F{F: 3}}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: changed}})
	stageBarrier(t, p)
	if got := p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != uint64(len(cases)+1) {
		t.Fatal(got)
	}
	// Every rejected replacement left the accepted arm intact.
	h.fill(1, 1, 1)
	h.wait(t, 2)
}

func TestStageParametersArePerPositionSnapshots(t *testing.T) {
	h := stageRing(t)
	observations := make(chan string, 16)
	specs := []manifest.StageParam{{Key: "sigma", Type: "float", Value: 2, Min: 2, Max: 5}}
	handler := StageHandlers{Params: specs,
		PlanWithParams: func(_ context.Context, _ *Client, q *pb.PlanStage, params stage.Params) (*pb.StagePlanned, error) {
			need := uint32(4)
			if params["sigma"].(float64) == 5 {
				need = 7
			}
			// Author mutations must not change the request used to retain the plan.
			params["sigma"] = float64(99)
			q.Params["sigma"].Kind = &pb.Value_F{F: 99}
			return &pb.StagePlanned{Output: q.Input, Modifies: []*pb.StageChannelNeed{{Channel: need, Needs: []uint32{need}}}}, nil
		},
		Check: func(_ context.Context, _ *Client, a *pb.StageArm, info stage.Info) error {
			observations <- fmt.Sprintf("check:%d:%g", a.Position, info.Params["sigma"])
			info.Params["sigma"] = float64(99)
			a.Params["sigma"].Kind = &pb.Value_F{F: 99}
			return nil
		},
		Process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			observations <- fmt.Sprintf("process:%d:%g", a.Position, s.Params["sigma"])
			if s.Params["sigma"] != a.Params["sigma"].GetF() {
				return fmt.Errorf("arm and slot disagree")
			}
			s.Data()[0] += byte(s.Params["sigma"].(float64))
			s.Params["sigma"] = float64(99)
			return nil
		},
	}
	p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{})
	var arms []*pb.StageArm
	// Plan both positions BEFORE arming either: the second plan must not erase
	// the first position's parameter-specific dependencies.
	for i, sigma := range []float64{2, 5} {
		params := map[string]*pb.Value{"sigma": {Kind: &pb.Value_F{F: sigma}}}
		p.send(t, &pb.HostMsg{Id: freshID("plan"), Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: params}}})
		if out := stageRecv(t, p); out.GetStagePlanned() == nil {
			t.Fatal(out)
		}
		arms = append(arms, &pb.StageArm{StageId: "s", Revision: 1, Pipeline: 1, Position: uint32(i + 1), SegmentId: h.id, Input: stageShape(), Output: stageShape(), Params: params})
	}
	for _, a := range arms {
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
		if ack := stageRecv(t, p).GetStageArmAck(); ack.GetPosition() != a.Position {
			t.Fatal(ack)
		}
	}
	for iter := uint32(1); iter <= 2; iter++ {
		h.fill(iter, 1, 1)
		h.wait(t, 3)
		if h.data[384] != 8 {
			t.Fatal("positions lost their settings", h.data[384])
		}
		for _, want := range []string{"check:1:2", "process:1:2", "check:2:5", "process:2:5"} {
			if got := <-observations; got != want {
				t.Fatal(got, want)
			}
		}
	}
	// Omit channel 7: position 1 runs with its captured need(4), position 2
	// refuses before author code with its captured need(7).
	h.fill(3, 1, 0)
	h.data[64+258] = 8
	atomic.StoreUint64(h.word, uint64(3)|uint64(1)<<32|uint64(1)<<48)
	h.wait(t, -2)
	for _, want := range []string{"check:1:2", "process:1:2"} {
		if got := <-observations; got != want {
			t.Fatal(got, want)
		}
	}
	if len(observations) != 0 || h.data[384] != 3 {
		t.Fatal("wrong arm dependencies", len(observations), h.data[384])
	}
	// Replanning changed values does not permit changing the same revision.
	changed := proto.Clone(arms[0]).(*pb.StageArm)
	changed.Params["sigma"] = &pb.Value{Kind: &pb.Value_F{F: 5}}
	p.send(t, &pb.HostMsg{Id: "replan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape(), Params: changed.Params}}})
	if out := stageRecv(t, p); out.GetStagePlanned() == nil {
		t.Fatal(out)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: changed}})
	stageBarrier(t, p)
	if got := p.client.cfg.Reporter.Stats()[violations.ArrangementContentMismatch].Observed; got != 1 {
		t.Fatal(got)
	}
	changed.Revision = 2
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: changed}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 2 {
		t.Fatal(ack)
	}
	h.fill(4, 1, 1)
	h.wait(t, 3)
	if h.data[384] != 11 {
		t.Fatal("new revision did not use changed params", h.data[384])
	}
	for _, want := range []string{"check:1:5", "process:1:5", "check:2:5", "process:2:5"} {
		if got := <-observations; got != want {
			t.Fatal(got, want)
		}
	}
}
