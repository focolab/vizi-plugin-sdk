package control

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"sort"
	"sync"
	"sync/atomic"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

// StageHandlers runs processing on a dedicated worker, independently of control
// requests. Check receives only copied metadata and may return stage.Refusal.
// Process borrows the slot until it returns; it must not retain any pixel slice
// or spawn work that outlives the call. Any error/panic from Process means failed,
// even a Refusal: memory may already have changed. Callbacks must honor context
// cancellation. Plan/PlanWithParams can run concurrently with Check/Process.
type StageHandlers struct {
	// Params is the declaration for manually registered handlers. Definition.Stage
	// supplies it from StageSpec.Params; do not declare it in both places.
	Params []manifest.StageParam
	// PlanWithParams receives checked plain values. Set exactly one of Plan and
	// PlanWithParams. Plan remains available for existing and raw-wire handlers.
	PlanWithParams func(context.Context, *Client, *pb.PlanStage, stage.Params) (*pb.StagePlanned, error)
	Plan           func(context.Context, *Client, *pb.PlanStage) (*pb.StagePlanned, error)
	Check          func(context.Context, *Client, *pb.StageArm, stage.Info) error
	Process        func(context.Context, *Client, *pb.StageArm, *stage.Slot) error
}

// CloneStageHandlers snapshots and validates parameter declarations.
func CloneStageHandlers(handlers map[string]StageHandlers) (map[string]StageHandlers, error) {
	out := maps.Clone(handlers)
	for id, h := range out {
		if h.Plan != nil && h.PlanWithParams != nil {
			return nil, fmt.Errorf("stage %q: select only one planning callback", id)
		}
		if err := manifest.ValidateStageParams(h.Params); err != nil {
			return nil, fmt.Errorf("stage %q: %w", id, err)
		}
		h.Params = manifest.CloneStageParams(h.Params)
		out[id] = h
	}
	return out, nil
}

// PlanRequest checks parameters and invokes the selected planning callback.
func (h StageHandlers) PlanRequest(ctx context.Context, c *Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
	params, err := stage.CheckParams(h.Params, q.GetParams())
	if err != nil {
		return nil, &HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: err.Error()}
	}
	if h.PlanWithParams != nil {
		return h.PlanWithParams(ctx, c, q, params)
	}
	if h.Plan != nil {
		return h.Plan(ctx, c, q)
	}
	return nil, &HostError{Kind: pb.Error_NOT_FOUND, Message: "stage has no planner"}
}

type stageKey struct{ pipeline, position uint32 }
type stagePlan struct {
	plan *pb.StagePlanned
}
type stageBinding struct {
	arm      *pb.StageArm
	segment  *memory.Segment
	params   stage.Params
	needs    []uint16
	last     []uint32
	reported map[uint32]bool
}
type stageRuntime struct {
	c        *Client
	ctx      context.Context
	cancel   context.CancelFunc
	done     chan struct{}
	wake     chan struct{}
	mu       sync.Mutex
	handlers map[string]StageHandlers
	plans    map[string]stagePlan
	arms     map[stageKey]*stageBinding
	ignored  atomic.Uint64
}

func newStageRuntime(c *Client) *stageRuntime {
	ctx, cancel := context.WithCancel(c.ctx)
	r := &stageRuntime{c: c, ctx: ctx, cancel: cancel, done: make(chan struct{}), wake: make(chan struct{}, 1), handlers: maps.Clone(c.cfg.Stages), plans: map[string]stagePlan{}, arms: map[stageKey]*stageBinding{}}
	go r.run()
	return r
}
func (r *stageRuntime) signal() {
	select {
	case r.wake <- struct{}{}:
	default:
	}
}

// IgnoredStageHints counts hinted slots whose atomic word names no active arm
// in this session (including never-filled slots). Hints do not confer ownership.
func (c *Client) IgnoredStageHints() uint64 {
	if c.stages == nil {
		return 0
	}
	return c.stages.ignored.Load()
}

// HintStages is nonblocking apart from short metadata/atomic-read locks. ServeEnv
// calls it for decoded D records. Custom servers should do the same.
func (c *Client) HintStages(n doorbell.Notification) {
	r := c.stages
	if r == nil || c.cfg.StageMemory == nil {
		return
	}
	seg, err := c.cfg.StageMemory.Registry.Segment(n.SegmentID)
	if err != nil || seg.Purpose() != memory.PurposeStage {
		return
	}
	r.mu.Lock()
	for i := range seg.Regions() {
		if !n.Mask.Has(uint16(i)) {
			continue
		}
		t, err := seg.StageWord(uint32(i))
		b := r.arms[stageKey{uint32(t.Pipeline), uint32(t.Position)}]
		if err != nil || t.Iteration == 0 || b == nil || b.segment != seg {
			r.ignored.Add(1)
		}
	}
	r.mu.Unlock()
	r.signal()
}

// Deterministic encoding includes the entire input and exact parameter values.
func stagePlanKey(q *pb.PlanStage) string {
	b, _ := (proto.MarshalOptions{Deterministic: true}).Marshal(q)
	return string(b)
}
func (r *stageRuntime) record(q *pb.PlanStage, p *pb.StagePlanned) error {
	if err := stage.ValidatePlan(q.Input, p); err != nil {
		return err
	}
	if _, ok := r.handlers[q.StageId]; !ok {
		return nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	key := stagePlanKey(q)
	if _, exists := r.plans[key]; !exists && len(r.plans) >= 65536 {
		return errors.New("stage plan retention limit reached")
	}
	r.plans[key] = stagePlan{proto.Clone(p).(*pb.StagePlanned)}
	return nil
}
func (r *stageRuntime) arm(a *pb.StageArm) error {
	if a.Revision == 0 {
		return r.c.reportViolation(violations.ZeroID, "StageArm carries revision 0")
	}
	refuse := func(detail string) error { return r.c.reportViolation(violations.StageArmRefused, detail) }
	if r.ctx.Err() != nil {
		return refuse("stage worker has stopped")
	}
	h, ok := r.handlers[a.StageId]
	if !ok || h.Process == nil || a.Pipeline == 0 || a.Pipeline > 65535 || a.Position == 0 || a.Position > 32766 || r.c.cfg.StageMemory == nil || r.c.cfg.StageNotify == nil {
		return refuse("unusable stage identity, position, handler or memory registry")
	}
	params, err := stage.CheckParams(h.Params, a.Params)
	if err != nil {
		return refuse(err.Error())
	}
	seg, err := r.c.cfg.StageMemory.Registry.Segment(a.SegmentId)
	if err != nil || seg.Purpose() != memory.PurposeStage {
		return refuse("stage arm requires an installed S segment")
	}
	// A retired segment can still have a registry entry while its callback drains.
	if _, err = seg.StageWord(0); err != nil {
		return refuse("stage ring is retiring or unavailable")
	}
	key := stageKey{a.Pipeline, a.Position}
	r.mu.Lock()
	defer r.mu.Unlock()
	old := r.arms[key]
	ack := func() error {
		return r.c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_StageArmAck{StageArmAck: &pb.StageArmAck{StageId: a.StageId, Revision: a.Revision, Pipeline: a.Pipeline, Position: a.Position}}})
	}
	if old != nil {
		if a.Revision < old.arm.Revision {
			return r.c.reportViolation(violations.ArrangementRegressed, "stage arm revision regressed")
		}
		if a.Revision == old.arm.Revision {
			if !proto.Equal(a, old.arm) {
				return r.c.reportViolation(violations.ArrangementContentMismatch, "stage arm changed at same revision")
			}
			return ack()
		}
	} else if len(r.arms) >= 65536 {
		return refuse("stage arm retention limit reached")
	}
	if err := stage.ValidateShape(a.Input); err != nil {
		return refuse(err.Error())
	}
	if err := stage.ValidateShape(a.Output); err != nil {
		return refuse(err.Error())
	}
	if a.Output.Z == 0 || a.Output.Y == 0 || a.Output.X == 0 {
		return refuse("armed output axes must be at least 1")
	}
	if a.Output.Z > a.Input.Z || a.Output.Y > a.Input.Y || a.Output.X > a.Input.X {
		return refuse("armed output grows input")
	}
	p, ok := r.plans[stagePlanKey(&pb.PlanStage{StageId: a.StageId, Input: a.Input, Params: a.Params})]
	if !ok {
		return refuse("stage arm has no matching input and params negotiation")
	}
	// The arm controls output bounds. The just-completed negotiation supplies
	// dependencies absent from StageArm, snapshotted for this arm and revision.
	if len(a.NewChannels) != len(p.plan.Adds) {
		return refuse("stage arm channel assignments do not match declared additions")
	}
	used := map[uint32]bool{}
	for _, c := range a.Input.Channels {
		if used[c] {
			return refuse("duplicate input channel")
		}
		used[c] = true
	}
	for _, add := range p.plan.Adds {
		c, ok := a.NewChannels[add.Key]
		if !ok || c >= stage.MaxChannels || used[c] {
			return refuse("invalid or colliding added-channel assignment")
		}
		used[c] = true
	}
	needSet := map[uint16]bool{}
	for _, c := range p.plan.Reads {
		needSet[uint16(c)] = true
	}
	for _, m := range p.plan.Modifies {
		needSet[uint16(m.Channel)] = true
		for _, c := range m.Needs {
			needSet[uint16(c)] = true
		}
	}
	for _, add := range p.plan.Adds {
		for _, c := range add.Needs {
			needSet[uint16(c)] = true
		}
	}
	needs := make([]uint16, 0, len(needSet))
	for c := range needSet {
		needs = append(needs, c)
	}
	b := &stageBinding{arm: proto.Clone(a).(*pb.StageArm), segment: seg, params: params, needs: needs, last: make([]uint32, len(seg.Regions()))}
	if old != nil && old.segment == seg {
		b.last = old.last
	}
	if old == nil && a.Revision != 1 {
		if err := r.c.reportViolation(violations.ArrangementFirstRevision, "first stage arm revision is not 1"); err != nil {
			return err
		}
	}
	if err := ack(); err != nil {
		return err
	}
	r.arms[key] = b
	r.signal()
	return nil
}
func (r *stageRuntime) run() {
	defer close(r.done)
	interval := r.c.cfg.StagePollInterval
	if interval <= 0 {
		interval = 40 * time.Millisecond
	}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	// Rate-limit hint-triggered scans independently of the periodic backstop.
	var lastScan time.Time
	for {
		select {
		case <-r.ctx.Done():
			return
		case <-ticker.C:
		case <-r.wake:
		}
		if wait := time.Until(lastScan.Add(time.Millisecond)); wait > 0 {
			timer := time.NewTimer(wait)
			select {
			case <-timer.C:
			case <-r.ctx.Done():
				timer.Stop()
				return
			}
		}
		lastScan = time.Now()
		r.mu.Lock()
		keys := make([]stageKey, 0, len(r.arms))
		for k := range r.arms {
			keys = append(keys, k)
		}
		r.mu.Unlock()
		sort.Slice(keys, func(i, j int) bool {
			if keys[i].pipeline == keys[j].pipeline {
				return keys[i].position < keys[j].position
			}
			return keys[i].pipeline < keys[j].pipeline
		})
		for _, key := range keys {
			if r.ctx.Err() != nil {
				return
			}
			for i := 0; ; i++ {
				if r.ctx.Err() != nil {
					break
				}
				r.mu.Lock()
				// A re-arm can be acknowledged while the previous slot runs. Take
				// the installed revision under the same lock as this turn's claim.
				b := r.arms[key]
				if i >= len(b.last) {
					r.mu.Unlock()
					break
				}
				turn, err := b.segment.TryStageTurn(uint32(i), uint16(key.pipeline), int16(key.position), b.last[i])
				if err != nil {
					if b.reported == nil {
						b.reported = make(map[uint32]bool)
					}
					report := !b.reported[uint32(i)]
					b.reported[uint32(i)] = true
					r.mu.Unlock()
					if report {
						r.c.Diagnose(fmt.Errorf("stage %s region %d: %w", b.arm.StageId, i, err))
					}
					break
				}
				if turn == nil {
					r.mu.Unlock()
					continue
				}
				b.last[i] = turn.Turn.Iteration
				r.mu.Unlock()
				r.process(b, turn, uint32(i))
			}
		}
	}
}
func (r *stageRuntime) process(b *stageBinding, t *memory.StagedTurn, index uint32) {
	outcome, reason := stage.Failed, uint32(2)
	defer func() {
		if p := recover(); p != nil {
			outcome, reason = stage.Failed, 2
			r.c.Diagnose(fmt.Errorf("stage %s panic: %v", b.arm.StageId, p))
		}
		var err error
		if r.ctx.Err() != nil {
			err = t.Abandon()
		} else {
			err = t.Finish(outcome, reason)
		}
		r.c.Diagnose(err)
		if t.HandedOn() && r.ctx.Err() == nil && r.c.cfg.StageNotify != nil {
			n := doorbell.Notification{SegmentID: b.arm.SegmentId}
			_ = n.Mask.Set(uint16(index))
			r.c.Diagnose(r.c.cfg.StageNotify(n))
		}
		if r.c.ctx.Err() == nil {
			r.c.Diagnose(r.c.cfg.StageMemory.settleStageRetirements(r.c))
		}
	}()
	slot, err := stage.DecodeSlot(t.Data)
	if err == nil {
		err = slot.ValidateShape(b.arm.Input)
	}
	if err != nil {
		reason = 1
		r.c.Diagnose(r.c.reportViolation(violations.StageSlotInvalid, fmt.Sprintf("segment %d region %d: %v", b.arm.SegmentId, index, err)))
		return
	}
	slot.Params = maps.Clone(b.params)
	original := slot.Info().Layout
	present := map[uint16]bool{}
	for _, c := range slot.Layout.Channels {
		present[c] = true
	}
	for _, c := range b.needs {
		if !present[c] {
			outcome, reason = stage.Refused, 2
			return
		}
	}
	h := r.handlers[b.arm.StageId]
	if h.Check != nil {
		if err := h.Check(r.ctx, r.c, proto.Clone(b.arm).(*pb.StageArm), slot.Info()); err != nil {
			var refusal *stage.Refusal
			if errors.As(err, &refusal) && refusal.Reason >= 1 && refusal.Reason <= 3 {
				outcome, reason = stage.Refused, refusal.Reason
			} else {
				r.c.Diagnose(err)
			}
			return
		}
	}
	if r.ctx.Err() != nil {
		return
	}
	if err := h.Process(r.ctx, r.c, proto.Clone(b.arm).(*pb.StageArm), slot); err != nil {
		r.c.Diagnose(err)
		return
	}
	if r.ctx.Err() != nil {
		return
	}
	if slot.Layout.Allocation[0] == 0 || slot.Layout.Allocation[1] == 0 || slot.Layout.Allocation[2] == 0 {
		r.c.Diagnose(errors.New("stage output axes must be at least 1"))
		return
	}
	if err := slot.ValidateShape(b.arm.Output); err != nil {
		r.c.Diagnose(err)
		return
	}

	// Processing preserves input planes and may append only the assigned additions.
	if len(slot.Layout.Channels) != len(original.Channels)+len(b.arm.NewChannels) {
		r.c.Diagnose(errors.New("stage did not preserve input planes and append declared additions"))
		return
	}
	for i, c := range original.Channels {
		if slot.Layout.Channels[i] != c {
			r.c.Diagnose(errors.New("stage changed input channel identities/order"))
			return
		}
	}
	additions := map[uint16]bool{}
	for _, c := range b.arm.NewChannels {
		additions[uint16(c)] = true
	}
	for _, c := range slot.Layout.Channels[len(original.Channels):] {
		if !additions[c] {
			r.c.Diagnose(errors.New("stage appended an unassigned or duplicate channel"))
			return
		}
		delete(additions, c)
	}
	for i, n := range slot.Layout.Allocation {
		if n > original.Allocation[i] {
			r.c.Diagnose(errors.New("stage grew the current spatial allocation"))
			return
		}
	}
	if err := slot.CommitLayout(); err != nil {
		r.c.Diagnose(err)
		return
	}
	outcome, reason = stage.Processed, 0
}
