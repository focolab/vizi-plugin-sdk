package main

import (
	"context"
	"errors"
	"log"
	"math"
	"sync"
	"sync/atomic"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type settings struct {
	minSize int
	scale   float64
	grow3D  bool
	mode    string
}
type scope struct {
	t, c       uint32
	generation uint64
}
type application struct {
	session                         *control.Session
	panel                           plugin.Panel
	panelOpen                       atomic.Bool
	trail                           *plugin.Overlay
	labelOverlay                    *plugin.Overlay
	ctx                             context.Context
	cancel                          context.CancelFunc
	jobs                            chan func() error
	done                            chan struct{}
	busy                            atomic.Bool
	status                          atomic.Value
	liveBytes, liveCount, liveNanos atomic.Uint64
	gesture                         *gesture   // listener only
	mu                              sync.Mutex // model; never taken by tool callbacks
	settings                        settings
	labels                          labelMap
	scope                           scope
	regions                         []*pb.RoiRecord
	region                          int
	plot                            []byte
}

func newApplication(s *control.Session) *application {
	ctx, cancel := context.WithCancel(s.Client.Context())
	trail, _ := plugin.NewOverlay(s.Client, "seg.trail")
	labelOverlay, _ := plugin.NewOverlay(s.Client, "seg.labels")
	a := &application{session: s, panel: plugin.Panel{ID: "seg", Client: s.Client}, trail: trail, labelOverlay: labelOverlay, ctx: ctx, cancel: cancel, jobs: make(chan func() error, 1), done: make(chan struct{}), settings: settings{50, 1, true, "Replace"}}
	a.status.Store("Ready")
	go func() {
		defer close(a.done)
		for {
			select {
			case <-ctx.Done():
				return
			case work := <-a.jobs:
				// A session end fails the job's host request at once; it is
				// not news for a closing panel.
				if err := work(); err != nil && !errors.Is(err, control.ErrSessionEnded) {
					a.say(err.Error())
				}
				a.busy.Store(false)
			}
		}
	}()
	return a
}
func (a *application) say(s string) {
	a.status.Store(s)
	if !a.panelOpen.Load() {
		return
	}
	if err := a.panel.SetValue("status", plugin.String(s)); err != nil {
		log.Printf("status: %v", err)
	}
}
func (a *application) start(work func() error) bool {
	if !a.busy.CompareAndSwap(false, true) {
		return false
	}
	a.jobs <- work
	return true
}
func (a *application) accept(work func() error) (plugin.Result, error) {
	if !a.busy.CompareAndSwap(false, true) {
		return plugin.Result{}, errors.New(msgBusy)
	}
	return plugin.Result{Message: "Started", AfterReply: func(ctx context.Context, _ *control.Client) error {
		select {
		case a.jobs <- work:
			return nil
		case <-ctx.Done():
			a.busy.Store(false)
			return ctx.Err()
		}
	}}, nil
}
func (a *application) refreshPanelLocked() error {
	if !a.panelOpen.Load() {
		return nil
	}
	var errs []error
	errs = append(errs, a.panel.SetValue("size", plugin.Int(int64(a.settings.minSize))), a.panel.SetValue("thresh", plugin.Float(a.settings.scale)), a.panel.SetValue("grow3d", plugin.Bool(a.settings.grow3D)), a.panel.SetValue("mode", plugin.String(a.settings.mode)), a.panel.SetEnabled("commit", len(a.labels.voxels) > 0), a.panel.SetVisible("rmeasure", len(a.regions) > 0))
	names := []string{"(none)"}
	selected := "(none)"
	if len(a.regions) > 0 {
		names = nil
		for _, r := range a.regions {
			names = append(names, r.Name)
		}
		if a.region >= 0 && a.region < len(names) {
			selected = names[a.region]
		}
	}
	errs = append(errs, a.panel.SetOptions("region", names...), a.panel.SetValue("region", plugin.String(selected)), a.panel.SetValue("status", plugin.String(a.status.Load().(string))))
	if len(a.plot) > 0 {
		errs = append(errs, a.panel.SetPNG(a.session, 0, "plot", a.plot))
	}
	return errors.Join(errs...)
}
func (a *application) edit(id string, args plugin.Args) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	var err error
	switch id {
	case "seg.set_min_size":
		var v int64
		v, err = args.Int("value")
		if err == nil {
			if v < 1 || v > 5000 {
				err = errors.New("Minimum size must be between 1 and 5000.")
			} else {
				a.settings.minSize = int(v)
			}
		}
	case "seg.set_threshold":
		var v float64
		v, err = args.Float("value")
		if err == nil {
			if math.IsNaN(v) || math.IsInf(v, 0) || v < .5 || v > 2 {
				err = errors.New("Threshold scale must be between 0.5 and 2.")
			} else {
				a.settings.scale = v
			}
		}
	case "seg.set_3d":
		var v bool
		v, err = args.Bool("value")
		if err == nil {
			a.settings.grow3D = v
		}
	case "seg.set_mode":
		var v string
		v, err = args.String("value")
		if err == nil {
			if v != "Replace" && v != "Add" {
				err = errors.New("Mode must be Replace or Add.")
			} else {
				a.settings.mode = v
			}
		}
	case "seg.set_region":
		var v int64
		v, err = args.Int("index")
		if err == nil {
			a.region = int(v)
		} // preserve stale index: measure refreshes, never guesses
	}
	return errors.Join(err, a.refreshPanelLocked())
}
func (a *application) command(ctx context.Context, id string, args plugin.Args) (plugin.Result, error) {
	switch id {
	case "seg.pick":
		view, err := a.session.Client.GetViewState(ctx)
		if err != nil {
			return plugin.Result{}, err
		}
		if view.Mip {
			return plugin.Result{}, errors.New(msgMIP)
		}
		reply, err := a.session.Client.ActivateTool(ctx, &pb.ActivateTool{ToolId: "seg.lasso"})
		if err != nil {
			return plugin.Result{}, err
		}
		if !reply.Active {
			return plugin.Result{}, errors.New("Lasso activation refused.")
		}
		a.say(msgActive)
	case "seg.commit":
		a.mu.Lock()
		has := len(a.labels.voxels) > 0
		a.mu.Unlock()
		if !has {
			return plugin.Result{}, errors.New("Nothing to commit.")
		}
		return a.accept(a.commit)
	case "seg.measure":
		a.mu.Lock()
		has := len(a.regions) > 0
		a.mu.Unlock()
		if !has {
			if err := a.refreshRegions(); err != nil {
				return plugin.Result{}, err
			}
			return plugin.Result{}, errors.New("No committed region selected; refreshed regions.")
		}
		return a.accept(a.measure)
	case "seg.reset":
		a.mu.Lock()
		a.labels = labelMap{}
		err := a.removeLabelsLocked()
		if a.panelOpen.Load() {
			err = errors.Join(err, a.panel.SetEnabled("commit", false))
		}
		a.mu.Unlock()
		if err != nil {
			return plugin.Result{}, err
		}
		a.say("Cleared")
	default:
		return plugin.Result{}, a.edit(id, args)
	}
	return plugin.Result{}, nil
}
func runtimeDeclaration() (*plugin.Application, control.Plugin, error) {
	ready := make(chan struct{})
	var a *application
	wait := func(ctx context.Context) error {
		select {
		case <-ready:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
	commands := map[string]plugin.CommandHandler{}
	for _, id := range []string{"seg.pick", "seg.commit", "seg.measure", "seg.reset", "seg.set_min_size", "seg.set_threshold", "seg.set_3d", "seg.set_mode", "seg.set_region"} {
		commands[id] = func(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
			if err := wait(ctx); err != nil {
				return plugin.Result{}, err
			}
			r, err := a.command(ctx, id, args)
			if err != nil && !errors.Is(err, control.ErrSessionEnded) {
				a.say(err.Error())
			}
			return r, err
		}
	}
	h := plugin.Handlers{Commands: commands, Panels: map[string]plugin.PanelOpenedHandler{"seg": func(ctx context.Context, _ *control.Client) error {
		if err := wait(ctx); err != nil {
			return err
		}
		a.mu.Lock()
		defer a.mu.Unlock()
		a.panelOpen.Store(true)
		return a.refreshPanelLocked()
	}},
		Activated: map[string]plugin.ToolLifecycleHandler{"seg.lasso": func(ctx context.Context, _ *control.Client, _ *pb.ToolBinding) error {
			if err := wait(ctx); err != nil {
				return err
			}
			a.say(msgActive)
			return nil
		}},
		Deactivated: map[string]plugin.ToolLifecycleHandler{"seg.lasso": func(ctx context.Context, _ *control.Client, _ *pb.ToolBinding) error {
			if err := wait(ctx); err != nil {
				return err
			}
			a.clearGesture()
			a.say("Ready")
			return nil
		}},
		Shutdown: func(ctx context.Context, _ *control.Client, _ *pb.Shutdown) error {
			if err := wait(ctx); err != nil {
				return err
			}
			a.cancel()
			select {
			case <-a.done:
				log.Printf("live samples=%d latest_bytes=%d total_publish_ns=%d", a.liveCount.Load(), a.liveBytes.Load(), a.liveNanos.Load())
				return nil
			case <-ctx.Done():
				return ctx.Err()
			}
		}}
	base := control.Plugin{Started: func(s *control.Session) { a = newApplication(s); close(ready) }, DoorbellHandler: func(s *control.Session, e doorbell.Event) {
		if wait(s.Client.Context()) == nil {
			a.event(e)
		}
	}}
	app, err := declare(h, base.DoorbellHandler)
	base.DoorbellHandler = nil
	return app, base, err
}

func runtimeConfig() (control.Plugin, error) {
	app, base, err := runtimeDeclaration()
	if err != nil {
		return control.Plugin{}, err
	}
	return app.Configure(base)
}
