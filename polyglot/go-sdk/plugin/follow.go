package plugin

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math"
	"slices"
	"sync"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// FollowHandler runs on its own goroutine until the person ends the Follow or
// the handler returns. Returning, an error, or a panic ends it automatically.
// Honor ctx or use Wait; Go cannot forcibly terminate an application goroutine.
// Errors and panics are reported through control.Config.Diagnostic.
type FollowHandler func(context.Context, *control.Client, *Follow) error

// OnFollow registers a driver for this definition's panel. Its declared
// FollowTargets supply the manifest; the host's start supplies the live targets.
func (d *Definition) OnFollow(panel PanelRef, handler FollowHandler) {
	d.open()
	if panel.owner != d.owner || handler == nil {
		d.fail(errors.New("follow requires this definition's panel and a handler"))
		return
	}
	for _, p := range d.m.Contributions.Panels {
		if p.ID == panel.id && len(p.FollowTargets) == 0 {
			d.fail(errors.New("follow panel must declare follow_targets"))
			return
		}
	}
	if d.h.Follows == nil {
		d.h.Follows = map[string]FollowHandler{}
	}
	if d.h.Follows[panel.id] != nil {
		d.fail(fmt.Errorf("duplicate follow handler for %q", panel.id))
		return
	}
	d.h.Follows[panel.id] = handler
}

// OfferFollow updates panel state, including before the panel opens. Only the
// person can start it, through the host's Follow button and master switch.
func (p Panel) OfferFollow(label string) error {
	if p.ID == "" || p.Client == nil {
		return errors.New("follow offer requires a panel ID and client")
	}
	return p.Client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OfferFollow{OfferFollow: &pb.OfferFollow{PanelId: p.ID, Label: label}}})
}
func (p Panel) WithdrawFollow() error { return p.OfferFollow("") }

// Follow is one host-started run. Metadata is immutable; methods are safe for
// concurrent use. Each Move is a whole state, not a delta. No cause is required.
type Follow struct {
	mu             sync.Mutex
	id             uint64
	panel          string
	targets        []string
	resumed        bool
	client         *control.Client
	ctx            context.Context
	cancel         context.CancelFunc
	seq            uint64
	ended, endSent bool
	reason         pb.FollowEndReason
	detail         string
}

func (f *Follow) ID() uint64               { return f.id }
func (f *Follow) PanelID() string          { return f.panel }
func (f *Follow) Targets() []string        { return slices.Clone(f.targets) }
func (f *Follow) Resumed() bool            { return f.resumed }
func (f *Follow) Context() context.Context { return f.ctx }
func (f *Follow) Active() bool             { return f.ctx.Err() == nil }

// Reason and Detail reflect the host's end notice. On session loss they remain
// unspecified/empty; no host end notice is invented. End stops the local driver
// immediately; PLUGIN_ENDED is available only after the host acknowledges it.
func (f *Follow) Reason() pb.FollowEndReason { f.mu.Lock(); defer f.mu.Unlock(); return f.reason }
func (f *Follow) Detail() string             { f.mu.Lock(); defer f.mu.Unlock(); return f.detail }

// Move returns false without sending after termination. An undeclared target
// or queue admission failure returns an error. Successful admissions have a
// strictly increasing seq; acceptance here does not acknowledge host application.
func (f *Follow) Move(v ViewTargets) (bool, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ctx.Err() != nil {
		return false, nil
	}
	hasTarget := false
	for _, target := range []struct {
		name    string
		present bool
	}{
		{"t", v.T != nil}, {"z", v.Z != nil}, {"select_region", v.SelectRegion != nil},
		{"frame_region", v.FrameRegion != nil}, {"look_at", v.LookAt != nil}, {"orbit_point", v.OrbitPoint != nil},
	} {
		hasTarget = hasTarget || target.present
		if target.present && !slices.Contains(f.targets, target.name) {
			return false, fmt.Errorf("follow does not drive %q", target.name)
		}
	}
	if !hasTarget {
		return false, errors.New("follow move requires at least one target")
	}
	if f.seq == math.MaxUint64 {
		return false, errors.New("follow sequence exhausted")
	}
	state := &pb.FollowState{FollowId: f.id, Seq: f.seq + 1, T: v.T, Z: v.Z, SelectRegion: v.SelectRegion, FrameRegion: v.FrameRegion, LookAt: v.LookAt, OrbitPoint: v.OrbitPoint}
	if err := f.client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_FollowState{FollowState: state}}); err != nil {
		return false, err
	}
	f.seq++
	return true, nil
}

// Wait dwells until the duration elapses, returning false immediately on end.
func (f *Follow) Wait(duration time.Duration) bool {
	if !f.Active() {
		return false
	}
	timer := time.NewTimer(duration)
	defer timer.Stop()
	select {
	case <-f.ctx.Done():
		return false
	case <-timer.C:
		return f.Active()
	}
}

// End requests one plugin end and cancels the local driver. It is idempotent.
// Admission errors are returned; a full one-shot queue faults the session,
// following Client.Send policy. No state is sent after a successful end.
func (f *Follow) End() error {
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.ended || f.endSent || f.ctx.Err() != nil {
		return nil
	}
	if err := f.client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_EndFollow{EndFollow: &pb.EndFollow{FollowId: f.id}}}); err != nil {
		return err
	}
	f.endSent = true
	f.cancel()
	return nil
}
func (f *Follow) finish(reason pb.FollowEndReason, detail string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if !f.ended {
		f.ended = true
		f.reason = reason
		f.detail = detail
		f.cancel()
	}
}

// The manager runs transitions synchronously on the receive loop, so an end is
// never stuck behind an application panel callback or a blocked Follow handler.
func configureFollows(base control.Config, handlers map[string]FollowHandler) (control.Config, error) {
	if base.FollowTransition != nil {
		return control.Config{}, errors.New("typed handlers would replace follow transitions")
	}
	handlers = maps.Clone(handlers)
	for id, h := range handlers {
		if id == "" || h == nil {
			return control.Config{}, errors.New("follows require panel IDs and handlers")
		}
	}
	var mu sync.Mutex
	runs := map[uint64]*Follow{}
	closed := false
	stop := func() {
		mu.Lock()
		defer mu.Unlock()
		closed = true
		for id, f := range runs {
			f.finish(pb.FollowEndReason_FOLLOW_END_UNSPECIFIED, "")
			delete(runs, id)
		}
	}
	base.FollowTransition = func(c *control.Client, m *pb.HostMsg) error {
		mu.Lock()
		defer mu.Unlock()
		if closed {
			return nil
		}
		switch x := m.Msg.(type) {
		case *pb.HostMsg_FollowStarted:
			q := x.FollowStarted
			if q == nil || q.FollowId == 0 || q.PanelId == "" {
				return errors.New("invalid follow start")
			}
			if runs[q.FollowId] != nil {
				return errors.New("duplicate follow start")
			}
			// The host sends the previous end first. Limit retained state even for a
			// malformed peer that skips that transition; cancel the displaced driver.
			for id, f := range runs {
				f.finish(pb.FollowEndReason_FOLLOW_END_UNSPECIFIED, "")
				delete(runs, id)
			}
			ctx, cancel := context.WithCancel(c.Context())
			f := &Follow{id: q.FollowId, panel: q.PanelId, targets: slices.Clone(q.Targets), resumed: q.Resumed, client: c, ctx: ctx, cancel: cancel}
			runs[q.FollowId] = f
			handler := handlers[q.PanelId]
			if handler == nil {
				c.Diagnose(fmt.Errorf("Follow for panel %q has no driver", q.PanelId))
				return f.End()
			}
			go func() {
				defer func() {
					if p := recover(); p != nil {
						c.Diagnose(fmt.Errorf("follow handler panic: %v", p))
					}
					if err := f.End(); err != nil {
						c.Diagnose(err)
						c.Fail(err)
					}
				}()
				if err := handler(ctx, c, f); err != nil && !errors.Is(err, context.Canceled) {
					c.Diagnose(err)
				}
			}()
		case *pb.HostMsg_FollowEnded:
			q := x.FollowEnded
			if q == nil {
				return errors.New("empty follow end")
			}
			if f := runs[q.FollowId]; f != nil {
				f.finish(q.Reason, q.Detail)
				delete(runs, q.FollowId)
			}
		}
		return nil
	}
	shutdown, end := base.Shutdown, base.End
	base.Shutdown = func(ctx context.Context, c *control.Client, q *pb.Shutdown) error {
		stop()
		if shutdown != nil {
			return shutdown(ctx, c, q)
		}
		return nil
	}
	base.End = func(err error) {
		stop()
		if end != nil {
			end(err)
		}
	}
	return base, nil
}
