package main

import (
	"context"
	"errors"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type job struct {
	work  func() error
	reply chan error
	pick  bool
}
type application struct {
	session        *control.Session
	panel, profile plugin.Panel
	panelOpen      atomic.Bool
	aim, marks     *plugin.Overlay
	ctx            context.Context
	cancel         context.CancelFunc
	jobs           chan job
	done           chan struct{}
	schedule       sync.Mutex // admission only; never held during a request or job
	busy           bool
	statusMu       sync.Mutex // status can be changed directly by the listener
	status         string
	// Only the worker owns the following model state.
	picks  []pick
	radius float64
	refine bool
	plot   []byte
	label  string
	// Only the doorbell listener owns gesture state.
	interaction uint32
	liveFailed  bool
}

func newApplication(s *control.Session) *application {
	ctx, cancel := context.WithCancel(s.Client.Context())
	aim, _ := plugin.NewOverlay(s.Client, "probe.aim")
	marks, _ := plugin.NewOverlay(s.Client, "probe.picks")
	a := &application{session: s, panel: plugin.Panel{ID: "probe", Client: s.Client}, profile: plugin.Panel{ID: "profile", Client: s.Client}, aim: aim, marks: marks, ctx: ctx, cancel: cancel, jobs: make(chan job, 64), done: make(chan struct{}), status: "Ready", radius: 3, refine: true, label: "No pick yet"}
	go func() {
		defer close(a.done)
		for {
			select {
			case <-ctx.Done():
				return
			case j := <-a.jobs:
				err := j.work()
				// A session end (Shutdown, or the host gone) fails the job's
				// host request at once; it is not news for a closing panel.
				if err != nil && !errors.Is(err, control.ErrSessionEnded) {
					a.say(err.Error())
				}
				if j.pick {
					a.schedule.Lock()
					a.busy = false
					a.schedule.Unlock()
				}
				if j.reply != nil {
					j.reply <- err
				}
			}
		}
	}()
	return a
}
func (a *application) say(text string) {
	a.statusMu.Lock()
	defer a.statusMu.Unlock()
	a.status = text
	if !a.panelOpen.Load() {
		return
	}
	if err := a.panel.SetValue("status", plugin.String(text)); err != nil {
		log.Printf("status: %v", err)
	}
}
func (a *application) submit(j job) error {
	a.schedule.Lock()
	defer a.schedule.Unlock()
	if j.pick && a.busy {
		return errors.New("Busy: a pick is still in flight.")
	}
	select {
	case <-a.ctx.Done():
		return a.ctx.Err()
	case a.jobs <- j:
		if j.pick {
			a.busy = true
		}
		return nil
	default:
		return errors.New("Application work queue is full.")
	}
}
func (a *application) call(ctx context.Context, work func() error) error {
	reply := make(chan error, 1)
	if err := a.submit(job{work: work, reply: reply}); err != nil {
		return err
	}
	select {
	case err := <-reply:
		return err
	case <-ctx.Done():
		return ctx.Err()
	case <-a.ctx.Done():
		return a.ctx.Err()
	}
}
func countText(n int) string {
	if n == 0 {
		return "No picks"
	}
	if n == 1 {
		return "1 pick"
	}
	return fmt.Sprintf("%d picks", n)
}
func (a *application) showPicks() error {
	if a.panelOpen.Load() {
		if err := a.panel.SetValue("count", plugin.String(countText(len(a.picks)))); err != nil {
			return err
		}
	}
	items := []*pb.OverlayItem{}
	if len(a.picks) > 0 {
		positions := make([]point, len(a.picks))
		for i, p := range a.picks {
			positions[i] = p.position
		}
		m, err := (plugin.GeometryEncoder{ByteOrder: payload.LittleEndian}).Markers("picks", positions)
		if err != nil {
			return err
		}
		m.GetMarker().Shape = "disc"
		m.GetMarker().SizePx = 8
		m.Style = &pb.OverlayStyle{ColorRgba: []uint32{255, 204, 0, 255}}
		items = append(items, m)
		for i, p := range a.picks {
			t, err := plugin.TextItem(fmt.Sprintf("label-%d", i+1), strconv.Itoa(i+1), p.position, plugin.TextOptions{ColorRGBA: &[4]uint8{255, 204, 0, 255}})
			if err != nil {
				return err
			}
			items = append(items, t)
		}
	}
	return a.marks.Replace(items...)
}
func (a *application) refresh(id string) error {
	if id == "profile" {
		if len(a.plot) > 0 {
			if err := a.profile.SetPNG(a.session, 0, "plot", a.plot); err != nil {
				return err
			}
		}
		return a.profile.SetValue("pick", plugin.String(a.label))
	}
	if !a.panelOpen.Load() {
		return nil
	}
	a.statusMu.Lock()
	defer a.statusMu.Unlock()
	return errors.Join(a.panel.SetValue("status", plugin.String(a.status)), a.panel.SetValue("count", plugin.String(countText(len(a.picks)))), a.panel.SetValue("radius", plugin.String(strconv.FormatFloat(a.radius, 'g', -1, 64))), a.panel.SetValue("refine", plugin.Bool(a.refine)))
}
func (a *application) command(id string, args plugin.Args) error {
	switch id {
	case "probe.undo":
		if len(a.picks) > 0 {
			a.picks = a.picks[:len(a.picks)-1]
		}
		if err := a.showPicks(); err != nil {
			return err
		}
		a.say("Undid the last pick.")
	case "probe.clear":
		a.picks = nil
		if err := a.showPicks(); err != nil {
			return err
		}
		a.say("Cleared all picks.")
	case "probe.set_radius":
		v, err := args.Number("value")
		if text, e := args.String("value"); e == nil {
			v, err = strconv.ParseFloat(strings.TrimSpace(text), 64)
		}
		if err != nil || !finite(v) || v <= 0 {
			return errors.New("ROI radius must be a positive finite number.")
		}
		a.radius = v
		return a.refresh("probe")
	case "probe.set_refine":
		v, err := args.Bool("value")
		if err != nil {
			return err
		}
		a.refine = v
		return a.refresh("probe")
	case "probe.contribute":
		q := &pb.ContributeRois{ReplaceAll: true}
		for i, p := range a.picks {
			q.Rois = append(q.Rois, &pb.RoiContribution{ContribId: fmt.Sprintf("pick-%d", i+1), Name: fmt.Sprintf("Pick %d", i+1), Style: &pb.RoiStyle{Color: "#ffcc00"}, Roi: ellipsoid(p.position, a.radius)})
		}
		if _, err := a.session.Client.ContributeRois(a.ctx, q); err != nil {
			return err
		}
		a.say(fmt.Sprintf("Contributed %d ROIs.", len(a.picks)))
	}
	return nil
}
func bindingStatus(b *pb.ToolBinding) string {
	parts := []string{"Probe active: release to pick"}
	for _, t := range b.Triggers {
		if t.Key != "" {
			parts = append(parts, fmt.Sprintf("%s (%s)", t.Key, t.Hold))
		} else {
			parts = append(parts, fmt.Sprintf("%s mods=%d (%s)", t.Button, t.Mods, t.Hold))
		}
	}
	for _, a := range b.Actions {
		key := a.Key
		if key == "" {
			key = "no assigned key"
		}
		parts = append(parts, a.ActionId+": "+key)
	}
	return strings.Join(parts, "; ") + "."
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
	h := plugin.Handlers{Commands: map[string]plugin.CommandHandler{}, Panels: map[string]plugin.PanelOpenedHandler{}}
	for _, id := range []string{"probe.undo", "probe.clear", "probe.set_radius", "probe.set_refine", "probe.contribute"} {
		h.Commands[id] = func(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
			if err := wait(ctx); err != nil {
				return plugin.Result{}, err
			}
			err := a.call(ctx, func() error { return a.command(id, args) })
			return plugin.Result{}, err
		}
	}
	for _, id := range []string{"probe", "profile"} {
		h.Panels[id] = func(ctx context.Context, _ *control.Client) error {
			if err := wait(ctx); err != nil {
				return err
			}
			return a.call(ctx, func() error {
				if id == "probe" {
					a.panelOpen.Store(true)
				}
				return a.refresh(id)
			})
		}
	}
	h.Activated = map[string]plugin.ToolLifecycleHandler{"probe.point": func(ctx context.Context, _ *control.Client, b *pb.ToolBinding) error {
		if err := wait(ctx); err != nil {
			return err
		}
		a.say(bindingStatus(b))
		return nil
	}}
	h.Deactivated = map[string]plugin.ToolLifecycleHandler{"probe.point": func(ctx context.Context, _ *control.Client, _ *pb.ToolBinding) error {
		if err := wait(ctx); err != nil {
			return err
		}
		a.clearAim()
		a.say("Ready")
		return nil
	}}
	h.Shutdown = func(ctx context.Context, _ *control.Client, _ *pb.Shutdown) error {
		if err := wait(ctx); err != nil {
			return err
		}
		a.cancel()
		select {
		case <-a.done:
			return nil
		case <-ctx.Done():
			return ctx.Err()
		}
	}
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
