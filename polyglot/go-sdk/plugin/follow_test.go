package plugin

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestFollowWithoutDriverDiagnosesAndEnds(t *testing.T) {
	diagnostics := make(chan error, 1)
	cfg, err := (Handlers{}).Configure(control.Config{PluginID: "follow.test", Diagnostic: func(err error) { diagnostics <- err }})
	if err != nil {
		t.Fatal(err)
	}
	c, stream := listTestSession(t, cfg)
	if err := stream.Send(&pb.HostMsg{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 9, PanelId: "missing", Targets: []string{"t"}}}}); err != nil {
		t.Fatal(err)
	}
	m, err := stream.Recv()
	if err != nil || m.GetEndFollow().GetFollowId() != 9 {
		t.Fatal(m, err)
	}
	select {
	case err := <-diagnostics:
		if !strings.Contains(err.Error(), "no driver") {
			t.Fatal(err)
		}
	case <-c.Context().Done():
		t.Fatal(c.Context().Err())
	}
}

func TestFollowDeclarations(t *testing.T) {
	targets := []string{"t", "z", "select_region", "frame_region", "look_at", "orbit_point"}
	d := definition()
	p := d.Panel(PanelSpec{ID: "p", Title: "P", Root: Label("l", ""), FollowTargets: targets}, nil)
	d.OnFollow(p, func(context.Context, *control.Client, *Follow) error { return nil })
	targets[0] = "bad"
	app := buildDefinition(t, d)
	data, err := app.Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	if got := decodeManifest(t, data).Contributions.Panels[0].FollowTargets; len(got) != 6 || got[0] != "t" {
		t.Fatal(got)
	}
	for _, which := range []string{"foreign", "missing targets", "nil handler", "duplicate handler", "duplicate target", "unknown target"} {
		t.Run(which, func(t *testing.T) {
			d := definition()
			targets := []string{"t"}
			if which == "missing targets" {
				targets = nil
			}
			if which == "duplicate target" {
				targets = []string{"t", "t"}
			}
			if which == "unknown target" {
				targets = []string{"camera"}
			}
			p := d.Panel(PanelSpec{ID: "p", Title: "P", Root: Label("l", ""), FollowTargets: targets}, nil)
			h := FollowHandler(func(context.Context, *control.Client, *Follow) error { return nil })
			if which == "foreign" {
				p = definition().Panel(PanelSpec{ID: "other", Title: "Other", Root: Label("l", "")}, nil)
			}
			if which == "nil handler" {
				h = nil
			}
			d.OnFollow(p, h)
			if which == "duplicate handler" {
				d.OnFollow(p, h)
			}
			if _, err := d.Build(); err == nil {
				t.Fatal("accepted invalid follow")
			}
		})
	}
}

func TestFollowMovesAllTargetsAndEndsOnce(t *testing.T) {
	drive := make(chan *Follow, 1)
	step := make(chan struct{})
	completed := make(chan struct{})
	barrier := make(chan struct{}, 1)
	h := Handlers{Panels: map[string]PanelOpenedHandler{"p": func(context.Context, *control.Client) error { barrier <- struct{}{}; return nil }}, Follows: map[string]FollowHandler{"p": func(ctx context.Context, c *control.Client, f *Follow) error {
		drive <- f
		select {
		case <-step:
		case <-ctx.Done():
		}
		close(completed)
		return nil
	}}}
	cfg, err := h.Configure(control.Config{PluginID: "follow.test"})
	if err != nil {
		t.Fatal(err)
	}
	// Configure takes a handler-map snapshot.
	delete(h.Follows, "p")
	c, stream := listTestSession(t, cfg)
	send := func(m *pb.HostMsg) {
		t.Helper()
		if err := stream.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	recv := func() *pb.PluginMsg {
		t.Helper()
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return m
	}
	panel := Panel{ID: "p", Client: c}
	if err := panel.OfferFollow("Tour"); err != nil {
		t.Fatal(err)
	}
	if got := recv().GetOfferFollow(); got.GetPanelId() != "p" || got.GetLabel() != "Tour" {
		t.Fatal(got)
	}
	send(&pb.HostMsg{Id: "one-shot-not-a-request", Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 8, PanelId: "p", Targets: []string{"t", "z", "select_region", "frame_region", "look_at", "orbit_point"}, Resumed: true}}})
	var f *Follow
	select {
	case f = <-drive:
	case <-c.Context().Done():
		t.Fatal(c.Context().Err())
	}
	if f.ID() != 8 || f.PanelID() != "p" || !f.Resumed() || !f.Active() {
		t.Fatal(f)
	}
	targets := f.Targets()
	targets[0] = "changed"
	v := ViewTargets{T: proto.Uint32(0), Z: proto.Uint32(0), SelectRegion: proto.String(""), FrameRegion: proto.String("r"), LookAt: &pb.LookAt{TargetZyx: []float64{1, 2, 3}, FitExtent: 4, DirectionZyx: []float64{0, 0, 1}}, OrbitPoint: &pb.OrbitPoint{Zyx: []float64{3, 2, 1}}}
	for i := uint64(1); i <= 3; i++ {
		if ok, err := f.Move(v); !ok || err != nil {
			t.Fatal(ok, err)
		}
		m := recv()
		q := m.GetFollowState()
		want := &pb.FollowState{FollowId: 8, Seq: i, T: v.T, Z: v.Z, SelectRegion: v.SelectRegion, FrameRegion: v.FrameRegion, LookAt: v.LookAt, OrbitPoint: v.OrbitPoint}
		if m.Id != "" || !proto.Equal(q, want) {
			t.Fatal(m)
		}
	}
	close(step)
	if got := recv().GetEndFollow(); got.GetFollowId() != 8 {
		t.Fatal(got)
	}
	<-completed
	if f.Active() || f.Wait(time.Hour) {
		t.Fatal("active after local end")
	}
	if err := f.End(); err != nil {
		t.Fatal(err)
	}
	if ok, err := f.Move(v); ok || err != nil {
		t.Fatal(ok, err)
	}
	send(&pb.HostMsg{Msg: &pb.HostMsg_FollowEnded{FollowEnded: &pb.FollowEnded{FollowId: 8, Reason: pb.FollowEndReason_FOLLOW_END_PLUGIN_ENDED}}})
	send(&pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "p"}}})
	select {
	case <-barrier:
	case <-c.Context().Done():
		t.Fatal("end not processed")
	}
	if f.Reason() != pb.FollowEndReason_FOLLOW_END_PLUGIN_ENDED {
		t.Fatal(f.Reason())
	}
	// Barrier: only the withdrawal may follow the single EndFollow.
	if err := panel.WithdrawFollow(); err != nil {
		t.Fatal(err)
	}
	if m := recv(); m.GetOfferFollow() == nil || m.GetOfferFollow().Label != "" {
		t.Fatal(m)
	}
}

func TestFollowHostEndsWakeDriverDespiteBlockedPanel(t *testing.T) {
	for reason := pb.FollowEndReason_FOLLOW_END_STOPPED; reason <= pb.FollowEndReason_FOLLOW_END_INVALID; reason++ {
		t.Run(reason.String(), func(t *testing.T) {
			started := make(chan *Follow, 1)
			ended := make(chan error, 1)
			blocked := make(chan struct{})
			unblock := make(chan struct{})
			defer close(unblock)
			h := Handlers{Panels: map[string]PanelOpenedHandler{"p": func(ctx context.Context, _ *control.Client) error {
				close(blocked)
				select {
				case <-unblock:
				case <-ctx.Done():
				}
				return nil
			}}, Follows: map[string]FollowHandler{"p": func(_ context.Context, _ *control.Client, f *Follow) error {
				started <- f
				if f.Wait(time.Hour) {
					ended <- errors.New("dwell did not cancel")
					return nil
				}
				if f.Reason() != reason || f.Detail() != "host detail" {
					ended <- fmt.Errorf("end = %v %q", f.Reason(), f.Detail())
					return nil
				}
				if ok, err := f.Move(ViewTargets{T: proto.Uint32(0)}); ok || err != nil {
					ended <- fmt.Errorf("post-end move = %v %v", ok, err)
					return nil
				}
				ended <- nil
				return nil
			}}}
			cfg, err := h.Configure(control.Config{PluginID: "follow.test"})
			if err != nil {
				t.Fatal(err)
			}
			c, stream := listTestSession(t, cfg)
			send := func(m *pb.HostMsg) {
				t.Helper()
				if err := stream.Send(m); err != nil {
					t.Fatal(err)
				}
			}
			send(&pb.HostMsg{Msg: &pb.HostMsg_PanelOpened{PanelOpened: &pb.PanelOpened{PanelId: "p"}}})
			select {
			case <-blocked:
			case <-c.Context().Done():
				t.Fatal("panel not entered")
			}
			send(&pb.HostMsg{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 1, PanelId: "p", Targets: []string{"t"}}}})
			var f *Follow
			select {
			case f = <-started:
			case <-c.Context().Done():
				t.Fatal("start blocked behind panel")
			}
			for _, v := range []ViewTargets{{}, {Z: proto.Uint32(0)}, {SelectRegion: proto.String("")}, {FrameRegion: proto.String("r")}, {LookAt: &pb.LookAt{}}, {OrbitPoint: &pb.OrbitPoint{}}} {
				if ok, err := f.Move(v); ok || err == nil {
					t.Fatal("undeclared target accepted")
				}
			}
			send(&pb.HostMsg{Id: "end notice", Msg: &pb.HostMsg_FollowEnded{FollowEnded: &pb.FollowEnded{FollowId: 1, Reason: reason, Detail: "host detail"}}})
			select {
			case err := <-ended:
				if err != nil {
					t.Fatal(err)
				}
			case <-c.Context().Done():
				t.Fatal("end blocked behind panel")
			}
			if err := f.End(); err != nil {
				t.Fatal(err)
			}
			if err := (Panel{ID: "p", Client: c}).WithdrawFollow(); err != nil {
				t.Fatal(err)
			}
			m, err := stream.Recv()
			if err != nil || m.GetOfferFollow() == nil {
				t.Fatal("unexpected state or end", m, err)
			}
		})
	}
}

func TestFollowMissingReturningFailingPanickingHandlers(t *testing.T) {
	for _, which := range []string{"missing", "return", "error", "panic", "explicit"} {
		t.Run(which, func(t *testing.T) {
			diagnostics := make(chan error, 2)
			h := Handlers{Follows: map[string]FollowHandler{}}
			if which != "missing" {
				h.Follows["p"] = func(_ context.Context, _ *control.Client, f *Follow) error {
					switch which {
					case "error":
						return errors.New("driver error")
					case "panic":
						panic("driver panic")
					case "explicit":
						return f.End()
					}
					return nil
				}
			}
			cfg, err := h.Configure(control.Config{PluginID: "follow.test", Diagnostic: func(err error) { diagnostics <- err }})
			if err != nil {
				t.Fatal(err)
			}
			c, s := listTestSession(t, cfg)
			if err := s.Send(&pb.HostMsg{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 99, PanelId: "p", Targets: []string{"t"}}}}); err != nil {
				t.Fatal(err)
			}
			m, err := s.Recv()
			if err != nil || m.GetEndFollow().GetFollowId() != 99 {
				t.Fatal(m, err)
			}
			if which == "error" || which == "panic" {
				select {
				case err := <-diagnostics:
					if !strings.Contains(err.Error(), "driver "+which) {
						t.Fatal(err)
					}
				case <-c.Context().Done():
					t.Fatal("no diagnostic")
				}
			}
			if err := (Panel{ID: "p", Client: c}).WithdrawFollow(); err != nil {
				t.Fatal(err)
			}
			m, err = s.Recv()
			if err != nil || m.GetOfferFollow() == nil {
				t.Fatal("duplicate end", m, err)
			}
		})
	}
}

func TestFollowWaitUsesCancellationAndDuration(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		f := &Follow{ctx: ctx, cancel: cancel}
		before := time.Now()
		if !f.Wait(time.Second) || time.Since(before) != time.Second {
			t.Fatal("wrong dwell")
		}
		done := make(chan bool, 1)
		go func() { done <- f.Wait(time.Hour) }()
		synctest.Wait()
		cancel()
		if <-done {
			t.Fatal("did not cancel")
		}
		if ok, err := f.Move(ViewTargets{}); ok || err != nil {
			t.Fatal(ok, err)
		}
		if err := f.End(); err != nil {
			t.Fatal(err)
		} // A nil client proves no send on session loss.
	})
}

func TestFollowSessionLossCancelsWithoutEnd(t *testing.T) {
	started := make(chan *Follow, 1)
	finished := make(chan error, 1)
	h := Handlers{Follows: map[string]FollowHandler{"p": func(ctx context.Context, _ *control.Client, f *Follow) error {
		started <- f
		<-ctx.Done()
		if f.Wait(time.Hour) || f.Active() || f.Reason() != pb.FollowEndReason_FOLLOW_END_UNSPECIFIED {
			finished <- errors.New("session loss invented a host end")
			return nil
		}
		finished <- f.End()
		return nil
	}}}
	cfg, err := h.Configure(control.Config{PluginID: "follow.test"})
	if err != nil {
		t.Fatal(err)
	}
	c, s := listTestSession(t, cfg)
	if err := s.Send(&pb.HostMsg{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 1, PanelId: "p", Targets: []string{"t"}}}}); err != nil {
		t.Fatal(err)
	}
	select {
	case <-started:
	case <-c.Context().Done():
		t.Fatal("not started")
	}
	if err := s.CloseSend(); err != nil {
		t.Fatal(err)
	}
	// EOF must follow session loss, with no unsolicited EndFollow in front of it.
	if m, err := s.Recv(); err == nil {
		t.Fatal("unexpected message", m)
	}
	select {
	case err := <-finished:
		if err != nil {
			t.Fatal(err)
		}
	case <-s.Context().Done():
		// A closed stream can precede the application's final scheduling; keep the
		// test bounded independently of the stream without imposing a timing order.
		select {
		case err := <-finished:
			if err != nil {
				t.Fatal(err)
			}
		case <-time.After(5 * time.Second):
			t.Fatal("driver did not cancel")
		}
	}
}

func TestFollowFailedAdmissionDoesNotAdvanceSequence(t *testing.T) {
	started := make(chan *Follow, 1)
	h := Handlers{Follows: map[string]FollowHandler{"p": func(ctx context.Context, _ *control.Client, f *Follow) error { started <- f; <-ctx.Done(); return nil }}}
	cfg, err := h.Configure(control.Config{PluginID: "follow.test"})
	if err != nil {
		t.Fatal(err)
	}
	c, s := listTestSession(t, cfg)
	if err := s.Send(&pb.HostMsg{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 1, PanelId: "p", Targets: []string{"t", "frame_region"}}}}); err != nil {
		t.Fatal(err)
	}
	var f *Follow
	select {
	case f = <-started:
	case <-c.Context().Done():
		t.Fatal("not started")
	}
	v := ViewTargets{T: proto.Uint32(0)}
	if ok, err := f.Move(ViewTargets{FrameRegion: proto.String(strings.Repeat("x", control.MaxMessageBytes))}); ok || err == nil {
		t.Fatal(ok, err)
	}
	if ok, err := f.Move(v); !ok || err != nil {
		t.Fatal(ok, err)
	}
	m, err := s.Recv()
	if err != nil || m.GetFollowState().GetSeq() != 1 {
		t.Fatal(m, err)
	}
}
