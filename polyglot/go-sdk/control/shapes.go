package control

import (
	"context"
	"errors"
	"fmt"
	"math"
	"sync"
	"unicode/utf8"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// ShapeHandler computes from the edit's complete rest-form geometry. commit is
// false for a preview, true for the final shape. The SDK supplies DragId and Seq
// on the result. Positions are controls, one per Roi.vertices; the host applies
// the CompileOperator to obtain display positions. Return REJECTED with a message
// to decline, or an error (which the SDK converts to REJECTED). Panics and invalid
// results also become REJECTED.
// Each callback owns its edit. Callbacks may run concurrently, including a
// commit while an obsolete preview is still running. Honor ctx cancellation:
// previews are canceled on supersession, commit or end; commits on end.
// There is no work deadline. Session shutdown cancels all callbacks.
type ShapeHandler func(ctx context.Context, client *Client, edit *pb.ShapeEdit, commit bool) (*pb.ShapeResult, error)

const shapeCoalescingSlack = 4

type shapeDrag struct {
	latest    *pb.ShapeEdit
	cancel    context.CancelFunc
	committed bool
	running   bool
}

// Only live drags are retained. IDs are monotonic across queues; the high water
// mark suppresses old previews without a session-long set of ended IDs. A commit
// can be the first message for an older ID, and is always answered.
type shapeRuntime struct {
	mu       sync.Mutex
	client   *Client
	ctx      context.Context
	drags    map[uint64]*shapeDrag
	high     uint64
	previews chan struct{}
	commits  chan struct{}
}

func newShapeRuntime(ctx context.Context, c *Client) *shapeRuntime {
	return &shapeRuntime{ctx: ctx, client: c, drags: make(map[uint64]*shapeDrag), previews: make(chan struct{}, shapeCoalescingSlack), commits: make(chan struct{}, c.cfg.MaxPendingRequests)}
}

func (s *shapeRuntime) drag(edit *pb.ShapeEdit) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.ctx.Err() != nil {
		return
	}
	d := s.drags[edit.DragId]
	if d == nil {
		if edit.DragId <= s.high {
			return
		}
		s.high = edit.DragId
		// The host can begin another drag before the previous end arrives, but
		// only four preview keys may be in flight. Dropping previews is permitted.
		if len(s.drags) >= cap(s.commits)+shapeCoalescingSlack {
			return
		}
		d = &shapeDrag{}
		s.drags[edit.DragId] = d
	}
	if d.committed || (d.latest != nil && edit.Seq <= d.latest.Seq) {
		return
	}
	d.latest = proto.Clone(edit).(*pb.ShapeEdit)
	if d.cancel != nil {
		d.cancel()
	}
	if d.running {
		return
	}
	select {
	case s.previews <- struct{}{}:
		d.running = true
		go s.preview(edit.DragId, d)
	default:
		// A callback ignoring cancellation cannot grow the worker count.
		delete(s.drags, edit.DragId)
	}
}

func (s *shapeRuntime) preview(id uint64, d *shapeDrag) {
	defer func() { <-s.previews }()
	for {
		s.mu.Lock()
		if s.ctx.Err() != nil || s.drags[id] != d || d.committed {
			s.mu.Unlock()
			return
		}
		edit := d.latest
		ctx, cancel := context.WithCancel(s.ctx)
		d.cancel = cancel
		s.mu.Unlock()
		result := s.compute(ctx, edit, false)
		cancel()
		s.mu.Lock()
		if s.ctx.Err() != nil || s.drags[id] != d || d.committed {
			s.mu.Unlock()
			return
		}
		if edit != d.latest {
			s.mu.Unlock()
			continue
		}
		// Serialize result admission with commit/end reception. Already admitted
		// messages may be on the transport; the host silently drops stale proposals.
		err := s.client.send(&pb.PluginMsg{Msg: &pb.PluginMsg_ShapeProposal{ShapeProposal: result}})
		d.running = false
		d.cancel = nil
		s.mu.Unlock()
		if err != nil && !errors.Is(err, ErrQueueFull) && s.ctx.Err() == nil {
			s.client.Diagnose(err)
		}
		return
	}
}

func (s *shapeRuntime) commit(edit *pb.ShapeEdit) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.high = max(s.high, edit.DragId)
	d := s.drags[edit.DragId]
	if d != nil && d.committed {
		return
	} // A repeated pending commit must not create another answer.
	if d != nil && d.cancel != nil {
		d.cancel()
	}
	s.client.dropShapeProposal(edit.DragId)
	if s.ctx.Err() != nil {
		return
	}
	if d == nil {
		d = &shapeDrag{}
	}
	d.committed = true
	d.latest = nil
	if s.client.cfg.Shapers[edit.TypeName] == nil {
		delete(s.drags, edit.DragId)
		s.answer(rejectedShape(edit, fmt.Sprintf("no shaper registered for type %q", edit.TypeName)))
		return
	}
	// Final callbacks have separate capacity from previews and ordinary
	// requests. Saturation rejects the commit immediately, never silently.
	select {
	case s.commits <- struct{}{}:
	default:
		delete(s.drags, edit.DragId)
		s.answer(rejectedShape(edit, "shape commit capacity exhausted"))
		return
	}
	s.drags[edit.DragId] = d
	ctx, cancel := context.WithCancel(s.ctx)
	d.cancel = cancel
	edit = proto.Clone(edit).(*pb.ShapeEdit)
	go func() {
		defer func() { cancel(); <-s.commits }()
		result := s.compute(ctx, edit, true)
		s.mu.Lock()
		defer s.mu.Unlock()
		// End can overtake a callback. An answer after end is allowed and is
		// dropped by the host; honor the stronger cancellation by dropping it here.
		if s.ctx.Err() != nil || s.drags[edit.DragId] != d {
			return
		}
		s.answer(result)
		delete(s.drags, edit.DragId)
	}()
}

func (s *shapeRuntime) end(id uint64) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.high = max(s.high, id)
	if d := s.drags[id]; d != nil && d.cancel != nil {
		d.cancel()
	}
	delete(s.drags, id)
	s.client.dropShapeProposal(id)
}

// Recheck immediately before transport, including snapshots frozen by an
// unrelated ordered push. Anything already in Send is in flight and the host's
// silent-drop rule applies; never hold the runtime lock across network I/O.
func (s *shapeRuntime) shouldSend(m *pb.PluginMsg) bool {
	r := m.GetShapeProposal()
	if r == nil {
		return true
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	d := s.drags[r.DragId]
	return s.ctx.Err() == nil && d != nil && !d.committed && d.latest != nil && d.latest.Seq == r.Seq
}

func (s *shapeRuntime) answer(result *pb.ShapeResult) {
	result.Seq = 0
	if err := s.client.send(&pb.PluginMsg{Msg: &pb.PluginMsg_ShapeAnswer{ShapeAnswer: result}}); err != nil && s.ctx.Err() == nil {
		s.client.Fail(err)
	}
}

func rejectedShape(edit *pb.ShapeEdit, message string) *pb.ShapeResult {
	return &pb.ShapeResult{DragId: edit.DragId, Seq: edit.Seq, Resolution: pb.Resolution_REJECTED, Message: clipWorkText(message)}
}

func (s *shapeRuntime) compute(ctx context.Context, edit *pb.ShapeEdit, commit bool) (result *pb.ShapeResult) {
	// Keep validation independent of callback mutation of its owned input.
	id, seq := edit.DragId, edit.Seq
	count := len(edit.GetMove().GetRoi().GetVertices())
	defer func() {
		if p := recover(); p != nil {
			result = rejectedShape(&pb.ShapeEdit{DragId: id, Seq: seq}, fmt.Sprintf("shaper panic: %v", p))
		}
	}()
	handler := s.client.cfg.Shapers[edit.TypeName]
	if handler == nil {
		return rejectedShape(edit, fmt.Sprintf("no shaper registered for type %q", edit.TypeName))
	}
	result, err := handler(ctx, s.client, proto.Clone(edit).(*pb.ShapeEdit), commit)
	if err != nil {
		return rejectedShape(&pb.ShapeEdit{DragId: id, Seq: seq}, err.Error())
	}
	if err = validateShapeResult(result, count); err != nil {
		return rejectedShape(&pb.ShapeEdit{DragId: id, Seq: seq}, err.Error())
	}
	result = proto.Clone(result).(*pb.ShapeResult)
	result.DragId, result.Seq = id, seq
	if err = ValidateSize(&pb.PluginMsg{Msg: &pb.PluginMsg_ShapeAnswer{ShapeAnswer: result}}); err != nil {
		return rejectedShape(&pb.ShapeEdit{DragId: id, Seq: seq}, err.Error())
	}
	return result
}

func validateShapeResult(result *pb.ShapeResult, vertices int) error {
	if result == nil {
		return errors.New("shaper returned no result")
	}
	if !utf8.ValidString(result.Message) {
		return errors.New("shape message must be UTF-8")
	}
	switch result.Resolution {
	case pb.Resolution_REJECTED:
		if len(result.Positions) != 0 {
			return errors.New("REJECTED shape must have no positions")
		}
		if result.Message == "" {
			return errors.New("shaper rejected the edit without a message")
		}
		return nil
	case pb.Resolution_OK, pb.Resolution_CLAMPED:
	default:
		return errors.New("shaper returned an unknown resolution")
	}
	if len(result.Positions) != vertices {
		return fmt.Errorf("shape needs %d positions, got %d", vertices, len(result.Positions))
	}
	for _, p := range result.Positions {
		if p == nil || math.IsNaN(p.Z) || math.IsNaN(p.Y) || math.IsNaN(p.X) || math.IsInf(p.Z, 0) || math.IsInf(p.Y, 0) || math.IsInf(p.X, 0) {
			return errors.New("shape positions must be finite ZYX triples")
		}
	}
	return nil
}

func (c *Client) dropShapeProposal(id uint64) {
	c.queueMu.Lock()
	defer c.queueMu.Unlock()
	key := pushKey{kind: "shape", segment: id}
	if c.pending[key] == nil {
		return
	}
	delete(c.pending, key)
	for i, k := range c.order {
		if k == key {
			c.order = append(c.order[:i], c.order[i+1:]...)
			break
		}
	}
	c.coalescingCount--
	c.shapeCoalescingCount--
}
