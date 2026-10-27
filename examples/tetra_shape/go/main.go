package main

import (
	"context"
	"log"
	"os"
	"os/signal"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func declaration() (*plugin.Application, error) {
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.tetra_shape", Name: "Tetra Shape", Version: "0.1.0"})
	d.Needs(plugin.ContributeROIs)
	d.Command(plugin.CommandSpec{ID: "tetra.contribute", Title: "Contribute the tetrahedra"}, func(ctx context.Context, c *control.Client, _ plugin.Args) (plugin.Result, error) {
		_, err := c.ContributeRois(ctx, contributions())
		return plugin.Result{}, err
	})
	d.RegionType(plugin.RegionTypeSpec{ID: "tetra.volume", Name: "Tetrahedron (keeps its volume)"}, func(ctx context.Context, _ *control.Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		return volumeShape(e), nil
	})
	// Deliberately no shaper: the SDK must reject this type's commits itself.
	d.RegionType(plugin.RegionTypeSpec{ID: "tetra.unshaped", Name: "Tetrahedron (no shaper)"}, nil)
	d.OnShapeEditEnded(func(_ context.Context, _ *control.Client, e *pb.ShapeEditEnded) error {
		log.Printf("shape edit %d ended: %s; detail=%q", e.DragId, e.Outcome, e.Detail)
		return nil
	})
	return d.Build()
}

func main() {
	app, err := declaration()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.tetra_shape", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, stop := signal.NotifyContext(context.Background(), os.Interrupt)
	defer stop()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{}); code != 0 {
		os.Exit(code)
	}
}
