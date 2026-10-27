package plugin

import (
	"context"
	"fmt"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestViewDeclarations(t *testing.T) {
	d := definition()
	cmd := d.Command(CommandSpec{ID: "jump"}, noopCommand)
	plain := Button("button", "Jump", cmd)
	targets := []string{"t", "z", "select_region", "frame_region", "look_at", "orbit_point"}
	d.Panel(PanelSpec{ID: "p", Title: "P", ViewTargets: targets, Root: Column("root",
		plain.MovesView(), ClickableImage("trace", cmd).MovesView(),
		ListRows(ListSpec{ID: "list", Selectable: true, Command: cmd}, Checkbox("cell", "", false, cmd).MovesView()).MovesView(),
		RoiPickerWithOptions("roi", "", cmd, RoiPickerOptions{FollowsSelection: true, Filter: "mine"}),
	)}, nil)
	d.Tool(ToolSpec{ID: "tool", Mode: "fire", ViewTargets: targets}, ToolHandlers{Event: func(*control.Session, doorbell.Event) {}})
	targets[0] = "mutated"
	app := buildDefinition(t, d)
	raw, err := app.Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, raw)
	w := m.Contributions.Panels[0].Root.Children
	if plain.spec.MovesView || m.Contributions.Panels[0].ViewTargets[0] != "t" || m.Contributions.Tools[0].ViewTargets[0] != "t" || !w[0].MovesView || w[1].Command != cmd.ID() || !w[2].Row[0].MovesView || !w[3].FollowsSelection || (w[3].Filter == nil || *w[3].Filter != "mine") {
		t.Fatal(string(raw))
	}
	for _, test := range []string{"no targets", "cell no targets", "duplicate", "unknown", "tool duplicate", "label", "passive image", "passive list", "image no command", "foreign image", "filter"} {
		t.Run(test, func(t *testing.T) {
			d := definition()
			c := d.Command(CommandSpec{ID: "jump"}, noopCommand)
			s := PanelSpec{ID: "p", Title: "P", ViewTargets: []string{"t"}, Root: Button("b", "", c).MovesView()}
			switch test {
			case "no targets":
				s.ViewTargets = nil
			case "cell no targets":
				s.ViewTargets = nil
				s.Root = ListRows(ListSpec{ID: "list"}, Button("b", "", c).MovesView())
			case "duplicate":
				s.ViewTargets = []string{"t", "t"}
			case "unknown":
				s.ViewTargets = []string{"camera"}
			case "tool duplicate":
				d.Tool(ToolSpec{ID: "tool", ViewTargets: []string{"t", "t"}}, ToolHandlers{Event: func(*control.Session, doorbell.Event) {}})
			case "label":
				s.Root = Label("label", "").MovesView()
			case "passive image":
				s.Root = Image("i").MovesView()
			case "passive list":
				s.Root = ListRows(ListSpec{ID: "l"}, Label("cell", "")).MovesView()
			case "image no command":
				s.Root = ClickableImage("i", CommandRef{})
			case "foreign image":
				s.Root = ClickableImage("i", cmd)
			case "filter":
				s.Root = RoiPickerWithOptions("roi", "", c, RoiPickerOptions{Filter: "theirs"})
			}
			d.Panel(s, nil)
			if _, err := d.Build(); err == nil {
				t.Fatal("accepted invalid declaration")
			}
		})
	}
}

func TestCommandViewCauseAndAfterReply(t *testing.T) {
	for _, after := range []bool{false, true} {
		t.Run(fmt.Sprint(after), func(t *testing.T) {
			d := definition()
			done := make(chan error, 1)
			clickArgs := Args{"x": Int(0), "y": Int(27), "button": String("left"), "modifiers": String("shift+ctrl"), "image": Int(4096), "row": String("result")}
			d.Command(CommandSpec{ID: "jump"}, func(ctx context.Context, c *control.Client, args Args) (Result, error) {
				for key, value := range clickArgs {
					if !proto.Equal(args[key], value) {
						return Result{}, fmt.Errorf("click arg %s changed", key)
					}
				}
				work := func(ctx context.Context, c *control.Client) error {
					cause := CommandCause(ctx)
					var result *pb.ViewRequestResult
					var err error
					if cause == 42 {
						result, err = MoveView(ctx, c, ViewTargets{T: proto.Uint32(0)})
					} else {
						result, err = DeclineView(ctx, c)
					}
					if cause == 0 {
						if err == nil {
							return fmt.Errorf("missing cause accepted")
						}
						return nil
					}
					if err != nil {
						return err
					}
					if result.Cause != cause {
						return fmt.Errorf("wrong cause")
					}
					return nil
				}
				if after {
					return Result{AfterReply: func(ctx context.Context, c *control.Client) error { err := work(ctx, c); done <- err; return err }}, nil
				}
				return Result{}, work(ctx, c)
			})
			cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
			if err != nil {
				t.Fatal(err)
			}
			_, stream := listTestSession(t, cfg.Control)
			recv := func() *pb.PluginMsg {
				t.Helper()
				m, err := stream.Recv()
				if err != nil {
					t.Fatal(err)
				}
				return m
			}
			for _, cause := range []uint64{42, 43, 0} {
				if err := stream.Send(&pb.HostMsg{Id: freshID("command"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "jump", Cause: cause, Args: clickArgs}}}); err != nil {
					t.Fatal(err)
				}
				if after {
					if m := recv(); !m.GetCommandDone().GetOk() {
						t.Fatal(m)
					}
				}
				if cause != 0 {
					q := recv()
					v := q.GetViewRequest()
					if v == nil || v.Cause != cause || v.ToolCause != nil || v.Decline != (cause == 43) || (cause == 42 && (v.T == nil || *v.T != 0)) {
						t.Fatal(q)
					}
					outcome := pb.ViewOutcome_VIEW_OUTCOME_APPLIED
					if v.Decline {
						outcome = pb.ViewOutcome_VIEW_OUTCOME_DECLINED
					}
					if err := stream.Send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewRequestResult{ViewRequestResult: &pb.ViewRequestResult{Cause: cause, Outcome: outcome}}}); err != nil {
						t.Fatal(err)
					}
				}
				if after {
					if err := <-done; err != nil {
						t.Fatal(err)
					}
				} else {
					if m := recv(); !m.GetCommandDone().GetOk() {
						t.Fatal(m)
					}
				}
			}
		})
	}
}

func TestToolViewOnlyFire(t *testing.T) {
	ctx := context.Background()
	for _, phase := range []doorbell.Phase{doorbell.Press, doorbell.Drag, doorbell.Release, doorbell.Cancel} {
		e := doorbell.Input{Phase: phase, Tool: 7, Interaction: 9}
		if _, err := MoveToolView(ctx, nil, e, ViewTargets{Z: proto.Uint32(0)}); err == nil {
			t.Fatal(phase)
		}
		if _, err := DeclineToolView(ctx, nil, e); err == nil {
			t.Fatal(phase)
		}
	}
	c, stream := listTestSession(t, control.Config{PluginID: "org.example.view-test"})
	for _, tc := range []struct {
		action  uint16
		decline bool
		outcome pb.ViewOutcome
		reason  pb.ViewRefusal
	}{
		{0, false, pb.ViewOutcome_VIEW_OUTCOME_APPLIED, pb.ViewRefusal_VIEW_REFUSAL_NONE},
		{1, false, pb.ViewOutcome_VIEW_OUTCOME_APPLIED, pb.ViewRefusal_VIEW_REFUSAL_NONE},
		{1, true, pb.ViewOutcome_VIEW_OUTCOME_DECLINED, pb.ViewRefusal_VIEW_REFUSAL_NONE},
		{0, false, pb.ViewOutcome_VIEW_OUTCOME_REFUSED, pb.ViewRefusal_VIEW_REFUSAL_NOT_ENABLED},
		{1, false, pb.ViewOutcome_VIEW_OUTCOME_REFUSED, pb.ViewRefusal_VIEW_REFUSAL_UNSUPPORTED},
	} {
		targets := ViewTargets{T: proto.Uint32(0), LookAt: &pb.LookAt{TargetZyx: []float64{1, 2, 3}, FitExtent: 5, DirectionZyx: []float64{0, 0, 1}}, OrbitPoint: &pb.OrbitPoint{Zyx: []float64{3, 2, 1}}}
		done := make(chan error, 1)
		go func() {
			e := doorbell.Input{Phase: doorbell.Fire, Tool: 7, Interaction: 9, Action: tc.action}
			var r *pb.ViewRequestResult
			var err error
			if tc.decline {
				r, err = DeclineToolView(ctx, c, e)
			} else {
				r, err = MoveToolView(ctx, c, e, targets)
			}
			if err == nil && (r.Outcome != tc.outcome || r.Reason != tc.reason) {
				err = fmt.Errorf("unexpected result %v", r)
			}
			done <- err
		}()
		q, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		want := targets.Request(0)
		if tc.decline {
			want = &pb.ViewRequest{Decline: true}
		}
		want.ToolCause = &pb.ToolCause{Tool: 7, Interaction: 9}
		if !proto.Equal(q.GetViewRequest(), want) {
			t.Fatal(q, want)
		}
		if err := stream.Send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewRequestResult{ViewRequestResult: &pb.ViewRequestResult{ToolCause: want.ToolCause, Outcome: tc.outcome, Reason: tc.reason}}}); err != nil {
			t.Fatal(err)
		}
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
}

func TestToolViewTargetsRequirePanel(t *testing.T) {
	for _, tc := range []struct {
		name                      string
		panel                     bool
		panelTargets, toolTargets []string
		valid                     bool
	}{
		{name: "moving tool without panel", toolTargets: []string{"t"}},
		{name: "ordinary panel has no switch", panel: true, toolTargets: []string{"t"}},
		{name: "panel supplies switch", panel: true, panelTargets: []string{"t"}, toolTargets: []string{"t"}, valid: true},
		{name: "tool targets are independent", panel: true, panelTargets: []string{"t"}, toolTargets: []string{"look_at"}, valid: true},
		{name: "nonmoving tool needs no panel", valid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := definition()
			d.Tool(ToolSpec{ID: "pick", Mode: "fire", ViewTargets: tc.toolTargets}, ToolHandlers{Event: func(*control.Session, doorbell.Event) {}})
			if tc.panel {
				d.Panel(PanelSpec{ID: "p", Title: "P", ViewTargets: tc.panelTargets, Root: Label("status", "")}, nil)
			}
			_, err := d.Build()
			if (err == nil) != tc.valid {
				t.Fatal(err)
			}
		})
	}
}
