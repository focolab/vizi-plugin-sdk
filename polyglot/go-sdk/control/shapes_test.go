package control

import (
	"context"
	"errors"
	"math"
	"strings"
	"sync/atomic"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func shapeEdit(id uint64, seq uint32) *pb.ShapeEdit {
	return &pb.ShapeEdit{DragId: id, Seq: seq, TypeName: "tetra", RegionId: "copy", Move: &pb.ConstrainMoveRequest{Roi: &pb.Roi{TypeName: "tetra", Vertices: []*pb.Vertex{{Position: &pb.Vec3{Z: 1}}, {Position: &pb.Vec3{X: 2}}}}, P1: &pb.Vec3{Y: float64(seq)}}}
}
func shapeResult(edit *pb.ShapeEdit) *pb.ShapeResult {
	return &pb.ShapeResult{Positions: []*pb.Vec3{proto.Clone(edit.Move.P1).(*pb.Vec3), {X: 2}}}
}
func shapeSignal(t *testing.T, ch <-chan struct{}) {
	t.Helper()
	select {
	case <-ch:
	case <-time.After(3 * time.Second):
		t.Fatal("shape callback did not advance")
	}
}

func TestShapeCommitBypassesCommandAndPreview(t *testing.T) {
	command, preview, release := make(chan struct{}), make(chan struct{}), make(chan struct{})
	defer close(release)
	var faults atomic.Int32
	p := openPeer(t, Config{
		Diagnostic: func(error) { faults.Add(1) },
		HandleRequest: func(ctx context.Context, _ *Client, _ *pb.HostMsg) (*pb.PluginMsg, error) {
			close(command)
			select {
			case <-release:
			case <-ctx.Done():
			}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
		},
		Shapers: map[string]ShapeHandler{"tetra": func(ctx context.Context, _ *Client, edit *pb.ShapeEdit, commit bool) (*pb.ShapeResult, error) {
			if !commit {
				close(preview)
				select {
				case <-release:
				case <-ctx.Done():
				}
				return shapeResult(edit), nil
			}
			if edit.ContribId != "" || edit.Move.Roi.TypeName != "tetra" {
				return nil, errors.New("lost full input for copied region")
			}
			return shapeResult(edit), nil
		}},
	}, nil)
	p.send(t, &pb.HostMsg{Id: "busy", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "busy"}}})
	shapeSignal(t, command)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeDrag{ShapeDrag: shapeEdit(1, 1)}})
	shapeSignal(t, preview)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeCommit{ShapeCommit: shapeEdit(1, 0)}})
	answer := p.recv(t)
	if answer.Id != "" || answer.GetShapeAnswer().GetDragId() != 1 || answer.GetShapeAnswer().GetResolution() != pb.Resolution_OK || len(answer.GetShapeAnswer().GetPositions()) != 2 {
		t.Fatal(answer)
	}
	// A spinbox commit has no preceding drag and cannot wait for the command.
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeDrag{ShapeDrag: shapeEdit(1, 2)}})
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeCommit{ShapeCommit: shapeEdit(2, 0)}})
	if answer := p.recv(t).GetShapeAnswer(); answer.GetDragId() != 2 || answer.Seq != 0 {
		t.Fatal(answer)
	}
	if faults.Load() != 0 {
		t.Fatal("valid ordering diagnosed")
	}
}

func TestShapeCommitResults(t *testing.T) {
	for _, tc := range []struct {
		name       string
		handler    ShapeHandler
		resolution pb.Resolution
		message    string
	}{
		{"missing", nil, pb.Resolution_REJECTED, "no shaper registered"},
		{"error", func(context.Context, *Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) {
			return nil, errors.New("cannot reshape")
		}, pb.Resolution_REJECTED, "cannot reshape"},
		{"panic", func(context.Context, *Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) { panic("bad shape") }, pb.Resolution_REJECTED, "shaper panic"},
		{"nil", func(context.Context, *Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) { return nil, nil }, pb.Resolution_REJECTED, "no result"},
		{"count", func(context.Context, *Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) {
			return &pb.ShapeResult{Positions: []*pb.Vec3{{}}}, nil
		}, pb.Resolution_REJECTED, "2 positions"},
		{"nan", func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
			r := shapeResult(e)
			r.Positions[0].Z = math.NaN()
			return r, nil
		}, pb.Resolution_REJECTED, "finite"},
		{"infinity", func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
			r := shapeResult(e)
			r.Positions[0].X = math.Inf(1)
			return r, nil
		}, pb.Resolution_REJECTED, "finite"},
		{"nil position", func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
			r := shapeResult(e)
			r.Positions[0] = nil
			return r, nil
		}, pb.Resolution_REJECTED, "finite"},
		{"rejected with points", func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
			r := shapeResult(e)
			r.Resolution = pb.Resolution_REJECTED
			return r, nil
		}, pb.Resolution_REJECTED, "no positions"},
		{"rejected", func(context.Context, *Client, *pb.ShapeEdit, bool) (*pb.ShapeResult, error) {
			return &pb.ShapeResult{Resolution: pb.Resolution_REJECTED, Message: "not possible"}, nil
		}, pb.Resolution_REJECTED, "not possible"},
		{"clamped", func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
			r := shapeResult(e)
			r.Resolution = pb.Resolution_CLAMPED
			r.DragId = 99
			r.Seq = 99
			return r, nil
		}, pb.Resolution_CLAMPED, ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			p := openPeer(t, Config{Shapers: map[string]ShapeHandler{"tetra": tc.handler}}, nil)
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeCommit{ShapeCommit: shapeEdit(1, 0)}})
			msg := p.recv(t)
			r := msg.GetShapeAnswer()
			if r == nil || r.DragId != 1 || r.Seq != 0 || r.Resolution != tc.resolution || !strings.Contains(r.Message, tc.message) {
				t.Fatal(msg)
			}
			if r.Resolution == pb.Resolution_REJECTED && len(r.Positions) != 0 {
				t.Fatal("rejection carries positions")
			}
		})
	}
}

func TestShapePreviewSupersedesAndStops(t *testing.T) {
	for _, ending := range []string{"commit", "ended"} {
		t.Run(ending, func(t *testing.T) {
			c := stateClient(t)
			c.cfg.MaxPendingRequests = 2
			started, cancelled, release, latest := make(chan struct{}), make(chan struct{}), make(chan struct{}), make(chan struct{})
			c.cfg.Shapers = map[string]ShapeHandler{"tetra": func(ctx context.Context, _ *Client, e *pb.ShapeEdit, commit bool) (*pb.ShapeResult, error) {
				if e.Seq == 1 {
					close(started)
					<-ctx.Done()
					close(cancelled)
					<-release
				}
				if e.Seq == 3 {
					close(latest)
					<-ctx.Done()
				}
				if e.Seq == 2 {
					t.Error("superseded pending edit reached callback")
				}
				return shapeResult(e), nil
			}}
			s := newShapeRuntime(c.ctx, c)
			s.drag(shapeEdit(1, 1))
			shapeSignal(t, started)
			s.drag(shapeEdit(1, 2))
			shapeSignal(t, cancelled)
			s.drag(shapeEdit(1, 3))
			close(release)
			shapeSignal(t, latest)
			if ending == "commit" {
				s.commit(shapeEdit(1, 0))
				if r := nextBatch(t, c)[0].GetShapeAnswer(); r == nil || r.DragId != 1 {
					t.Fatal(r)
				}
			} else {
				s.end(1)
			}
			s.drag(shapeEdit(1, 4))
			// Wait for the cancelled preview worker to finish, then check all output.
			deadline := time.After(3 * time.Second)
			for len(s.previews) != 0 {
				select {
				case <-deadline:
					t.Fatal("preview did not stop")
				default:
					time.Sleep(time.Millisecond)
				}
			}
			c.queueMu.Lock()
			defer c.queueMu.Unlock()
			if len(c.pending) != 0 || len(c.out) != 0 {
				t.Fatal("obsolete proposal queued after commit/end")
			}
		})
	}
}

func TestShapeEndedOutcomesAndUnknownDrag(t *testing.T) {
	ended := make(chan *pb.ShapeEditEnded, 4)
	var faults atomic.Int32
	p := openPeer(t, Config{Diagnostic: func(error) { faults.Add(1) }, ShapeEditEnded: func(_ context.Context, _ *Client, e *pb.ShapeEditEnded) error { ended <- e; return nil }}, nil)
	for i, outcome := range []pb.ShapeEditEnded_Outcome{pb.ShapeEditEnded_COMMITTED, pb.ShapeEditEnded_REVERTED, pb.ShapeEditEnded_CANCELLED} {
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeEditEnded{ShapeEditEnded: &pb.ShapeEditEnded{DragId: uint64(i + 1), Outcome: outcome, Detail: "host detail"}}})
		select {
		case e := <-ended:
			if e.Outcome != outcome || e.Detail != "host detail" {
				t.Fatal(e)
			}
		case <-p.ctx.Done():
			t.Fatal(p.ctx.Err())
		}
	}
	if faults.Load() != 0 {
		t.Fatal("unknown ended drag diagnosed")
	}
}

func TestShapeProposalCoalescingSlack(t *testing.T) {
	c := stateClient(t)
	c.cfg.CoalescingCapacity = 1
	if err := c.Send(overlay("ordinary", 1)); err != nil {
		t.Fatal(err)
	}
	proposal := func(id uint64, seq uint32) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_ShapeProposal{ShapeProposal: &pb.ShapeResult{DragId: id, Seq: seq}}}
	}
	for id := uint64(1); id <= 4; id++ {
		if err := c.enqueueCoalescing(proposal(id, 1)); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.enqueueCoalescing(proposal(1, 3)); err != nil {
		t.Fatal(err)
	}
	if err := c.enqueueCoalescing(proposal(5, 1)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	c.dropShapeProposal(2)
	if err := c.enqueueCoalescing(proposal(5, 1)); err != nil {
		t.Fatal(err)
	}
	batch := nextBatch(t, c)
	if len(batch) != 5 || batch[1].GetShapeProposal().Seq != 3 || c.shapeCoalescingCount != 0 {
		t.Fatal(batch, c.shapeCoalescingCount)
	}
	if err := c.Send(overlay("next", 1)); err != nil {
		t.Fatal(err)
	}
}

func TestShapeCommitCapacityAndSessionCancellation(t *testing.T) {
	c := stateClient(t)
	c.cfg.MaxPendingRequests = 1
	started, stopped := make(chan struct{}), make(chan struct{})
	c.cfg.Shapers = map[string]ShapeHandler{"tetra": func(ctx context.Context, _ *Client, _ *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
		close(started)
		<-ctx.Done()
		close(stopped)
		return nil, ctx.Err()
	}}
	s := newShapeRuntime(c.ctx, c)
	s.commit(shapeEdit(1, 0))
	shapeSignal(t, started)
	s.commit(shapeEdit(2, 0))
	if r := nextBatch(t, c)[0].GetShapeAnswer(); r.GetDragId() != 2 || r.Resolution != pb.Resolution_REJECTED || r.Message == "" {
		t.Fatal(r)
	}
	c.cancel(ErrClosed)
	shapeSignal(t, stopped)
}

func TestShapeProposalUsesLatestFullInputAndProtectsCorrelation(t *testing.T) {
	p := openPeer(t, Config{Shapers: map[string]ShapeHandler{"tetra": func(_ context.Context, _ *Client, e *pb.ShapeEdit, _ bool) (*pb.ShapeResult, error) {
		r := shapeResult(e)
		// Author code owns its input; mutation cannot alter SDK bookkeeping or the
		// expected result length, and author-supplied correlation fields are ignored.
		e.DragId = 900
		e.Seq = 900
		e.Move.Roi.Vertices = nil
		r.DragId = 999
		r.Seq = 999
		return r, nil
	}}}, nil)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeDrag{ShapeDrag: shapeEdit(1, 7)}})
	if r := p.recv(t).GetShapeProposal(); r.GetDragId() != 1 || r.Seq != 7 || len(r.Positions) != 2 || r.Positions[0].Y != 7 {
		t.Fatal(r)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ShapeCommit{ShapeCommit: shapeEdit(1, 0)}})
	if r := p.recv(t).GetShapeAnswer(); r.GetDragId() != 1 || r.Seq != 0 || len(r.Positions) != 2 {
		t.Fatal(r)
	}
}

func TestShapeEndSuppressesFrozenProposals(t *testing.T) {
	for _, commit := range []bool{false, true} {
		c := stateClient(t)
		c.cfg.MaxPendingRequests = 1
		s := newShapeRuntime(c.ctx, c)
		s.high = 1
		s.drags[1] = &shapeDrag{latest: shapeEdit(1, 1)}
		proposal := &pb.PluginMsg{Msg: &pb.PluginMsg_ShapeProposal{ShapeProposal: &pb.ShapeResult{DragId: 1, Seq: 1}}}
		if err := c.enqueueCoalescing(proposal); err != nil {
			t.Fatal(err)
		}
		if !s.shouldSend(proposal) {
			t.Fatal("live proposal suppressed")
		}
		// A one-shot freezes the coalescer before the end reaches the dispatcher.
		if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "o"}}}); err != nil {
			t.Fatal(err)
		}
		if commit {
			s.commit(shapeEdit(1, 0))
		} else {
			s.end(1)
		}
		batch := nextBatch(t, c)
		if len(batch) != 2 || batch[0].GetShapeProposal() == nil || s.shouldSend(batch[0]) || !s.shouldSend(batch[1]) {
			t.Fatal(batch)
		}
		if c.shapeCoalescingCount != 0 {
			t.Fatal("frozen proposal capacity leaked")
		}
	}
}
