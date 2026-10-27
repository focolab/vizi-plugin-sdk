package peakroi

import (
	"context"
	"errors"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type sdkHost struct{ session *control.Session }

func (h sdkHost) view(ctx context.Context) (*pb.ViewState, error) {
	return h.session.Client.GetViewState(ctx)
}
func (h sdkHost) read(ctx context.Context, q *pb.ReadDataVolume) (*payload.Volume, error) {
	return payload.ReadVolume(ctx, h.session.Client, h.session.Memory, q, payload.CopyOptions{MaxBytes: uint64(maxAddressBytes), ByteOrder: payload.LittleEndian})
}
func (h sdkHost) contribute(ctx context.Context, q *pb.ContributeRois) error {
	reply, err := h.session.Client.ContributeRois(ctx, q)
	if err == nil && reply == nil {
		return errors.New("missing RoisContributed reply")
	}
	return err
}
func (h sdkHost) patch(ops ...*pb.PatchOp) error {
	return (plugin.Panel{ID: "peak", Client: h.session.Client}).Patch(ops...)
}

// Declaration builds the manifest and its runtime callbacks together. Started is
// asynchronous in the SDK, so every callback waits for session publication.
func Declaration() (*plugin.Application, control.Plugin, error) {
	ready := make(chan struct{})
	var app *application
	wait := func(ctx context.Context) error {
		select {
		case <-ready:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	commands := make(map[string]plugin.CommandHandler)
	for axis, id := range []string{"peak.set_z", "peak.set_y", "peak.set_x"} {
		commands[id] = func(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
			if err := wait(ctx); err != nil {
				return plugin.Result{}, err
			}
			return plugin.Result{}, app.setExtent(axis, args)
		}
	}
	commands["peak.set_mode"] = func(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
		if err := wait(ctx); err != nil {
			return plugin.Result{}, err
		}
		return plugin.Result{}, app.setMode(args)
	}
	commands["peak.run"] = func(ctx context.Context, _ *control.Client, _ plugin.Args) (plugin.Result, error) {
		if err := wait(ctx); err != nil {
			return plugin.Result{}, err
		}
		return app.run(ctx)
	}
	handlers := plugin.Handlers{
		Commands: commands,
		Panels: map[string]plugin.PanelOpenedHandler{"peak": func(ctx context.Context, _ *control.Client) error {
			if err := wait(ctx); err != nil {
				return err
			}
			return app.opened()
		}},
		Shutdown: func(ctx context.Context, _ *control.Client, _ *pb.Shutdown) error {
			if err := wait(ctx); err != nil {
				return err
			}
			return app.shutdown(ctx)
		},
	}
	base := control.Plugin{
		Memory: memory.Config{MaxSegmentBytes: uint64(maxAddressBytes)},
		Started: func(session *control.Session) {
			app = newApplication(session.Client.Context(), sdkHost{session})
			close(ready)
		},
	}
	built, err := declare(handlers)
	return built, base, err
}

func Runtime() (control.Plugin, error) {
	app, base, err := Declaration()
	if err != nil {
		return control.Plugin{}, err
	}
	return app.Configure(base)
}
