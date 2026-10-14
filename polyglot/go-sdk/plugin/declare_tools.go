package plugin

import (
	"context"
	"fmt"
	"slices"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// ActionHandler runs on the doorbell listener, including during a gesture. It
// must not block on host calls. Frame preserves position and context diagnostics.
type ActionHandler func(*control.Session, *doorbell.Frame) error
type ActionSpec struct {
	ID, Name, Key string
	Pane          *PaneButton
	Handle        ActionHandler
}
type ToolSpec struct {
	ViewTargets        []string
	ID, Name           string
	Views              []string
	Menu               *string
	Mode               string
	CancelOnCameraMove *bool
	// WantsPick false asks the host not to compute its surface pick for this
	// tool's 3D presses; nil means true. It is a request about what the host
	// computes, not a guarantee about what arrives: a host that predates the
	// field still sends RayAndPicked, and a host that picks still sends a bare
	// Ray when it found nothing. Input.Ray and Input.Pick read either kind.
	WantsPick *bool
	Triggers  []Trigger
	Actions   []ActionSpec
	Pane      *PaneButton
}

// Event receives primary input, cancellations, and unrecognized action indices.
// A recognized action is dispatched only to its ActionSpec.Handle. Event keeps
// the full frame (including ImplicitlyClosed and InputContextError). Callbacks
// have the same nonblocking requirement as Control.Plugin.DoorbellHandler.
type ToolHandlers struct {
	Event                  func(*control.Session, doorbell.Event)
	Activated, Deactivated ToolLifecycleHandler
}
type toolDefinition struct {
	spec     manifest.Tool
	handlers ToolHandlers
	actions  []ActionSpec
}
type ToolRef struct {
	owner *definitionID
	id    string
}

func (r ToolRef) ID() string { return r.id }
func (d *Definition) Tool(spec ToolSpec, handlers ToolHandlers) ToolRef {
	d.open()
	t := manifest.Tool{ViewTargets: slices.Clone(spec.ViewTargets), ID: spec.ID, Name: spec.Name, Views: spec.Views, Menu: spec.Menu, Mode: spec.Mode, CancelOnCameraMove: spec.CancelOnCameraMove, WantsPick: spec.WantsPick, Triggers: spec.Triggers, Pane: spec.Pane}
	if handlers.Event == nil {
		d.fail(fmt.Errorf("tool %q needs an Event handler for primary input and cancellation", spec.ID))
	}
	for _, a := range spec.Actions {
		if a.Handle == nil {
			d.fail(fmt.Errorf("action %q needs a handler", a.ID))
		}
		t.Actions = append(t.Actions, manifest.Action{ID: a.ID, Name: a.Name, Key: a.Key, Pane: a.Pane})
	}
	d.m.Contributions.Tools = append(d.m.Contributions.Tools, t)
	d.tools = append(d.tools, toolDefinition{t, handlers, slices.Clone(spec.Actions)})
	return ToolRef{d.owner, spec.ID}
}
func (a *Application) configureTools(base *control.Plugin) error {
	if len(a.tools) == 0 {
		return nil
	}
	if base.Control.ToolActivated != nil || base.Control.ToolDeactivated != nil {
		return fmt.Errorf("definition owns tool lifecycle dispatch")
	}
	byID := map[string]toolDefinition{}
	for _, t := range a.tools {
		byID[t.spec.ID] = t
	}
	// Retain the association through queued retirement cancellations. The
	// deactivation hook runs after those cancellations and removes it.
	var mu sync.Mutex
	handles := map[uint32]string{}
	remember := func(b *pb.ToolBinding) { mu.Lock(); handles[b.GetTool()] = b.GetToolId(); mu.Unlock() }
	base.Control.ToolActivated = func(ctx context.Context, c *control.Client, b *pb.ToolBinding) error {
		remember(b)
		if f := byID[b.GetToolId()].handlers.Activated; f != nil {
			return f(ctx, c, b)
		}
		return nil
	}
	base.Control.ToolDeactivated = func(ctx context.Context, c *control.Client, b *pb.ToolBinding) error {
		mu.Lock()
		delete(handles, b.GetTool())
		mu.Unlock()
		if f := byID[b.GetToolId()].handlers.Deactivated; f != nil {
			return f(ctx, c, b)
		}
		return nil
	}
	fallback := base.DoorbellHandler
	base.DoorbellHandler = func(s *control.Session, e doorbell.Event) {
		var handle uint32
		if e.Cancellation != nil {
			handle = e.Cancellation.Opening.Tool
		} else if e.Frame != nil && e.Frame.Disposition == doorbell.Decoded && e.Frame.Input != nil {
			handle = e.Frame.Input.Tool
		}
		id := ""
		if handle != 0 {
			if b := s.State.Tool(handle); b != nil {
				id = b.ToolId
				remember(b)
			} else {
				mu.Lock()
				id = handles[handle]
				mu.Unlock()
			}
		}
		if t, ok := byID[id]; ok {
			if e.Frame != nil && e.Frame.Input != nil {
				if action, found := s.State.ActionID(*e.Frame.Input); found {
					for _, a := range t.actions {
						if a.ID == action {
							if err := a.Handle(s, e.Frame); err != nil {
								s.Client.Diagnose(err)
							}
							return
						}
					}
				}
			}
			if t.handlers.Event != nil {
				t.handlers.Event(s, e)
			}
			return
		}
		if fallback != nil {
			fallback(s, e)
		}
	}
	return nil
}
