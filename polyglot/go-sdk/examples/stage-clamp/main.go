// stage-clamp demonstrates a declared image processing stage. Build it, then
// run --manifest[=DIR] or --install[=DIR]; no arguments serves a host session.
package main

import (
	"context"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"google.golang.org/protobuf/proto"
)

func main() {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.stage-clamp", Name: "Clamp bright pixels", Version: "0.1.0"})
	d.Stage(plugin.StageSpec{ID: "clamp", Name: "Clamp U8 values", Params: []plugin.StageParam{
		{Key: "ceiling", Name: "Maximum brightness", Type: "int", Value: 200, Min: 0, Max: 255},
	}}, plugin.StageHandlers{
		PlanWithParams: func(_ context.Context, _ *control.Client, q *pb.PlanStage, _ stage.Params) (*pb.StagePlanned, error) {
			if q.Input.Dtype != pb.DType_DTYPE_U8 {
				return nil, &control.HostError{Kind: pb.Error_UNSUPPORTED, Message: "clamp accepts U8 input"}
			}
			plan := &pb.StagePlanned{Output: proto.Clone(q.Input).(*pb.StageShape)}
			for _, c := range q.Input.Channels {
				plan.Modifies = append(plan.Modifies, &pb.StageChannelNeed{Channel: c, Needs: []uint32{c}})
			}
			return plan, nil
		},
		Process: func(ctx context.Context, _ *control.Client, _ *pb.StageArm, slot *stage.Slot) error {
			ceiling := byte(slot.Params["ceiling"].(int64))
			for _, channel := range slot.Layout.Channels {
				pixels, err := slot.Plane(channel)
				if err != nil {
					return err
				}
				for i, v := range pixels {
					if i%65536 == 0 {
						if err := ctx.Err(); err != nil {
							return err
						}
					}
					if v > ceiling {
						pixels[i] = ceiling
					}
				}
			}
			return nil // SDK commits metadata, outcome and turn, then sends D.
		},
	})
	app, err := d.Build()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.stage-clamp", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{}); code != 0 {
		os.Exit(code)
	}
}
