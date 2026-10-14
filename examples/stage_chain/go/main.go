package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"google.golang.org/protobuf/proto"
)

func definition() (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.stage_chain", Name: "Stage Chain", Version: "0.1.0"})
	stages := []plugin.StageSpec{
		{ID: "gain", Name: "Gain", Params: []plugin.StageParam{
			{Key: "factor", Name: "Factor", Type: "float", Value: 1.0, Min: 0, Max: 16},
			{Key: "channel", Name: "Channel", Type: "int", Value: 0, Min: 0, Max: 65535},
			{Key: "stall_ms", Name: "Stall (ms)", Type: "int", Value: 0, Min: 0, Max: 60000},
		}},
		{ID: "diff", Name: "Absolute difference", Params: []plugin.StageParam{
			{Key: "a", Type: "int", Value: 0, Min: 0, Max: 65535},
			{Key: "b", Type: "int", Value: 1, Min: 0, Max: 65535},
		}},
		{ID: "widen", Name: "Widen to f32", Params: []plugin.StageParam{
			{Key: "crop", Name: "Crop (voxels, y and x)", Type: "int", Value: 0, Min: 0, Max: 1024},
		}},
		{ID: "drift", Name: "Drift (pose only)", Params: []plugin.StageParam{
			{Key: "channel", Type: "int", Value: 0, Min: 0, Max: 65535},
		}},
	}
	drift := driftState{arms: make(map[armKey]history)}
	for _, spec := range stages {
		h := plugin.StageHandlers{PlanWithParams: func(_ context.Context, _ *control.Client, q *pb.PlanStage, params stage.Params) (*pb.StagePlanned, error) {
			return plan(q.StageId, q.Input, params)
		}}
		switch spec.ID {
		case "gain":
			h.Process = gain
		case "diff":
			h.Process = diff
		case "widen":
			h.Process = widen
		case "drift":
			h.Check, h.Process = drift.check, drift.process
		}
		d.Stage(spec, h)
	}
	return d.Build()
}

func plan(id string, input *pb.StageShape, params stage.Params) (*pb.StagePlanned, error) {
	p := &pb.StagePlanned{Output: proto.Clone(input).(*pb.StageShape)}
	decline := func(message string) (*pb.StagePlanned, error) {
		return nil, &control.HostError{Kind: pb.Error_UNSUPPORTED, Message: message}
	}
	channel := func(key string) uint32 { return uint32(params[key].(int64)) }
	switch id {
	case "gain", "drift":
		c := channel("channel")
		if !slices.Contains(input.Channels, c) {
			return decline(fmt.Sprintf("input lacks channel %d", c))
		}
		if id == "gain" {
			p.Modifies = []*pb.StageChannelNeed{{Channel: c, Needs: []uint32{c}}}
		} else {
			if input.Dtype != pb.DType_DTYPE_U8 && input.Dtype != pb.DType_DTYPE_U16 {
				return decline("drift requires u8 or u16")
			}
			p.Reads = []uint32{c}
		}
	case "diff":
		a, b := channel("a"), channel("b")
		if !slices.Contains(input.Channels, a) || !slices.Contains(input.Channels, b) {
			return decline("diff requires both input channels")
		}
		p.Adds = []*pb.StageNewChannel{{Key: "absdiff", Name: "Absolute difference", Needs: []uint32{a, b}}}
	case "widen":
		crop := uint32(params["crop"].(int64))
		if 2*crop >= input.Y || 2*crop >= input.X {
			return decline("crop leaves no y or x voxels")
		}
		p.Output.Y, p.Output.X = input.Y-2*crop, input.X-2*crop
		p.Output.Dtype = pb.DType_DTYPE_F32
		for _, c := range input.Channels {
			p.Modifies = append(p.Modifies, &pb.StageChannelNeed{Channel: c, Needs: []uint32{c}})
		}
	default:
		return nil, &control.HostError{Kind: pb.Error_NOT_FOUND, Message: "unknown stage"}
	}
	return p, nil
}

func main() {
	app, err := definition()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.stage_chain", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{}); code != 0 {
		os.Exit(code)
	}
}
