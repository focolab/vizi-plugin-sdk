package control

import (
	"context"
	"errors"
	"math"
	"slices"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
)

// QueueStats reports cumulative sender pressure without generating a diagnostic
// per drag-rate update. Refused messages consume no new use; previously admitted
// reservations still belong to the host. Merged counts replacement keys, not messages.
type QueueStats struct{ Merged, Refused, RefusedInTeardown uint64 }

func (c *Client) QueueStats() QueueStats {
	c.queueMu.Lock()
	defer c.queueMu.Unlock()
	return c.queueStats
}

type pushKey struct {
	kind, id, widget, row string
	segment               uint64
	region                uint32
	level, t              uint32
	hasT                  bool
}

func (c *Client) initQueue() {
	if c.wake == nil {
		c.wake = make(chan struct{}, 1)
	}
	if c.pending == nil {
		c.pending = make(map[pushKey]*pb.PluginMsg)
	}
}
func (c *Client) wakeWriter() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}
func (c *Client) outputError() error {
	if c.ctx.Err() != nil {
		return context.Cause(c.ctx)
	}
	if c.sealed {
		return ErrShuttingDown
	}
	return nil
}

// Snapshotting before every ordered send prevents an old coalesced property
// from overtaking a later descriptor-bearing patch, removal, or request. Frozen
// snapshots still count against the coalescing bound until the writer takes them.
func (c *Client) snapshot() []*pb.PluginMsg {
	batch := make([]*pb.PluginMsg, 0, len(c.order))
	for _, key := range c.order {
		batch = append(batch, c.pending[key])
	}
	clear(c.pending)
	c.order = nil
	return batch
}

// A loan-bearing panel patch depends on its own keys and the panel's rows.
// Other widgets remain mergeable until their own boundary or the next drain.
func (c *Client) snapshotFor(m *pb.PluginMsg) []*pb.PluginMsg {
	p := m.GetPanelPatch()
	if p == nil || !needsOrderedDelivery(m.ProtoReflect()) {
		return c.snapshot()
	}
	own := make(map[pushKey]bool)
	for _, op := range p.Ops {
		own[pushKey{kind: "panel", id: p.PanelId, widget: op.WidgetId, row: op.RowKey}] = true
		// Without declarations a raw mixed rows/image patch cannot identify
		// which pending cells precede its row lifetime boundary.
		if op.GetSetRows() != nil && c.cfg.Panels[p.PanelId].Cells == nil {
			return c.snapshot()
		}
	}
	var batch []*pb.PluginMsg
	kept := c.order[:0]
	for _, key := range c.order {
		freeze := own[key]
		if key.kind == "panel" && key.id == p.PanelId && key.row != "" {
			for _, op := range p.Ops {
				if op.GetSetRows() != nil && c.cfg.Panels[p.PanelId].Cells[key.widget] == op.WidgetId {
					freeze = true
				}
			}
		}
		if key.kind == "panel" && key.id == p.PanelId && key.row == "" {
			for _, op := range c.pending[key].GetPanelPatch().Ops {
				freeze = freeze || op.GetSetRows() != nil
			}
		}
		if freeze {
			batch = append(batch, c.pending[key])
			delete(c.pending, key)
		} else {
			kept = append(kept, key)
		}
	}
	c.order = kept
	return batch
}

func (c *Client) enqueueOrdered(m *pb.PluginMsg, sent chan error, fatal bool) error {
	c.queueMu.Lock()
	defer c.queueMu.Unlock()
	return c.enqueueOrderedLocked(m, sent, fatal)
}

func (c *Client) enqueueOrderedLocked(m *pb.PluginMsg, sent chan error, fatal bool) error {
	c.initQueue()
	if err := c.outputError(); err != nil {
		return err
	}
	if len(c.out) == cap(c.out) {
		if fatal {
			c.Fail(ErrQueueFull)
		} else {
			c.queueStats.Refused++
		}
		return ErrQueueFull
	}
	clone := proto.Clone(m).(*pb.PluginMsg)
	if c.cfg.AdmitOutbound != nil {
		if err := c.cfg.AdmitOutbound(clone); err != nil {
			c.queueStats.Refused++
			return err
		}
	}
	before := c.snapshotFor(m)
	c.out <- outbound{msg: clone, sent: sent, before: before}
	c.wakeWriter()
	return nil
}

// The normative coalescer is at the host, which owns received descriptor uses.
// This optional sender optimization may only discard descriptor-free values.
// Forward every accepted resource-bearing message for the host to dispose of;
// locally releasing one could invalidate another message sharing its uses count.
func needsOrderedDelivery(m protoreflect.Message) bool {
	// Future fields might carry descriptors or meaning we cannot merge.
	if len(m.GetUnknown()) != 0 {
		return true
	}
	name := m.Descriptor().FullName()
	if name == "vizi.plugin.v1.SegmentRef" || name == "vizi.plugin.v1.ImageRef" {
		return true
	}
	found := false
	m.Range(func(f protoreflect.FieldDescriptor, v protoreflect.Value) bool {
		if f.Message() == nil {
			return true
		}
		if f.IsMap() {
			if f.MapValue().Message() != nil {
				v.Map().Range(func(_ protoreflect.MapKey, x protoreflect.Value) bool {
					found = needsOrderedDelivery(x.Message())
					return !found
				})
			}
		} else if f.IsList() {
			for i := 0; i < v.List().Len() && !found; i++ {
				found = needsOrderedDelivery(v.List().Get(i).Message())
			}
		} else {
			found = needsOrderedDelivery(v.Message())
		}
		return !found
	})
	return found
}

func (c *Client) enqueueCoalescing(m *pb.PluginMsg) error {
	if w := m.GetWorkProgress(); w != nil {
		m = proto.Clone(m).(*pb.PluginMsg)
		m.GetWorkProgress().Title = clipWorkText(w.Title)
		m.GetWorkProgress().Text = clipWorkText(w.Text)
	}
	if err := c.validateOutbound(m); err != nil {
		return err
	}
	c.queueMu.Lock()
	defer c.queueMu.Unlock()
	c.initQueue()
	if err := c.outputError(); err != nil {
		return err
	}
	ordered := needsOrderedDelivery(m.ProtoReflect())
	if p := m.GetPanelPatch(); p != nil {
		counts := make(map[pushKey]int)
		sensitive := make(map[pushKey]bool)
		for _, op := range p.Ops {
			if op == nil || op.Op == nil {
				return errors.New("panel patch requires a widget operation")
			}
			key := pushKey{widget: op.WidgetId, row: op.RowKey}
			counts[key]++
			switch op.Op.(type) {
			case *pb.PatchOp_SetOptions:
				ordered = true
				sensitive[key] = true
			case *pb.PatchOp_SetRows:
				// Without a manifest the owner of pending row cells is unknown.
				// Keep the conservative ordered boundary for raw custom servers.
				if _, declared := c.cfg.Panels[p.PanelId]; !declared || len(p.Ops) > 1 {
					ordered = true
				}
				sensitive[key] = true
			}
		}
		for widget := range sensitive {
			if counts[widget] > 256 {
				c.queueStats.Refused++
				return ErrQueueFull
			}
		}
	}
	if ordered {
		// Freeze earlier values before an options/rows patch. A set_rows boundary
		// also separates cell writes from row removal/recreation, which resets
		// their state. Later values may coalesce until the next boundary.
		// No lifetime widget metadata is needed (§5.4).
		return c.enqueueOrderedLocked(m, nil, false)
	}
	// Prepare every changed key before committing, so a failed multi-widget
	// Send cannot partially apply a patch or silently lose its other widgets.
	changes := make(map[pushKey]*pb.PluginMsg)
	var order []pushKey
	var merged uint64
	put := func(key pushKey, msg *pb.PluginMsg) error {
		old := changes[key]
		if old == nil {
			old = c.pending[key]
			order = append(order, key)
		}
		if old != nil {
			var err error
			msg, err = mergePush(old, msg)
			if err != nil {
				return err
			}
			merged++
		}
		if err := ValidateSize(msg); err != nil {
			return err
		}
		changes[key] = msg
		return nil
	}
	clone := proto.Clone(m).(*pb.PluginMsg)
	var err error
	switch x := clone.Msg.(type) {
	case *pb.PluginMsg_ShapeProposal:
		if x.ShapeProposal == nil {
			return errors.New("nil shape proposal")
		}
		err = put(pushKey{kind: "shape", segment: x.ShapeProposal.DragId}, clone)
	case *pb.PluginMsg_WorkProgress:
		w := x.WorkProgress
		if w == nil || w.Subject == nil {
			return errors.New("work progress requires a subject")
		}
		key := pushKey{kind: "work_request", id: w.GetRequestId()}
		if _, task := w.Subject.(*pb.WorkProgress_TaskId); task {
			key.kind, key.id = "work_task", w.GetTaskId()
		}
		if key.id == "" {
			return errors.New("work progress requires a nonempty subject id")
		}
		err = put(key, clone)
	case *pb.PluginMsg_OfferFollow:
		if x.OfferFollow == nil {
			return errors.New("nil follow offer")
		}
		err = put(pushKey{kind: "follow_offer", id: x.OfferFollow.PanelId}, clone)
	case *pb.PluginMsg_FollowState:
		if x.FollowState == nil {
			return errors.New("nil follow state")
		}
		err = put(pushKey{kind: "follow", segment: x.FollowState.FollowId}, clone)
	case *pb.PluginMsg_PanelPatch:
		if x.PanelPatch == nil {
			return errors.New("nil panel patch")
		}
		for _, op := range x.PanelPatch.Ops {
			if op == nil || op.Op == nil {
				return errors.New("panel patch requires a widget operation")
			}
			key := pushKey{kind: "panel", id: x.PanelPatch.PanelId, widget: op.WidgetId, row: op.RowKey}
			err = put(key, &pb.PluginMsg{Msg: &pb.PluginMsg_PanelPatch{PanelPatch: &pb.PanelPatch{PanelId: key.id, Ops: []*pb.PatchOp{op}}}})
			if err != nil {
				break
			}
		}
	case *pb.PluginMsg_OverlayChunk:
		if x.OverlayChunk == nil {
			return errors.New("nil overlay chunk")
		}
		v := x.OverlayChunk
		err = put(pushKey{kind: "chunk", id: v.OverlayId, level: v.Level, t: v.GetT(), hasT: v.T != nil}, clone)
	case *pb.PluginMsg_OverlayLabelTable:
		if x.OverlayLabelTable == nil {
			return errors.New("nil overlay label table")
		}
		err = put(pushKey{kind: "label_table", id: x.OverlayLabelTable.OverlayId}, clone)
	case *pb.PluginMsg_OverlayUpdate:
		if x.OverlayUpdate == nil {
			return errors.New("nil overlay update")
		}
		err = put(pushKey{kind: "overlay", id: x.OverlayUpdate.OverlayId}, clone)
	case *pb.PluginMsg_SegmentShortfall:
		if x.SegmentShortfall == nil {
			return errors.New("nil segment shortfall")
		}
		err = put(pushKey{kind: "shortfall", segment: x.SegmentShortfall.SegmentId, region: x.SegmentShortfall.Region}, clone)
	}
	if err != nil {
		if errors.Is(err, ErrQueueFull) {
			c.queueStats.Refused++
		}
		return err
	}
	drop := make(map[pushKey]bool)
	if patch := m.GetPanelPatch(); patch != nil {
		layout := c.cfg.Panels[patch.PanelId]
		for _, op := range patch.Ops {
			if rows := op.GetSetRows(); rows != nil {
				for key := range c.pending {
					if key.kind == "panel" && key.id == patch.PanelId && key.row != "" && layout.Cells[key.widget] == op.WidgetId && !slices.Contains(rows.Keys, key.row) {
						drop[key] = true
					}
				}
			}
		}
	}
	for key := range changes {
		delete(drop, key)
	}
	added := 0
	addedWork := 0
	addedShape := 0
	for key := range changes {
		if c.pending[key] == nil {
			added++
			if key.kind == "shape" {
				addedShape++
			}
			if key.kind == "work_request" || key.kind == "work_task" {
				addedWork++
			}
		}
	}
	limit := c.cfg.CoalescingCapacity
	if limit == 0 {
		limit = 4096
	}
	if c.coalescingCount-c.workCoalescingCount-c.shapeCoalescingCount+added-addedWork-addedShape-len(drop) > limit || c.workCoalescingCount+addedWork > workCoalescingSlack || c.shapeCoalescingCount+addedShape > shapeCoalescingSlack {
		c.queueStats.Refused++
		return ErrQueueFull
	}
	if len(drop) > 0 {
		kept := c.order[:0]
		for _, key := range c.order {
			if drop[key] {
				delete(c.pending, key)
				c.coalescingCount--
				c.queueStats.Merged++
			} else {
				kept = append(kept, key)
			}
		}
		c.order = kept
	}
	for _, key := range order {
		if c.pending[key] == nil {
			c.order = append(c.order, key)
		}
		c.pending[key] = changes[key]
	}
	c.coalescingCount += added
	c.workCoalescingCount += addedWork
	c.shapeCoalescingCount += addedShape
	c.queueStats.Merged += merged
	c.wakeWriter()
	return nil
}

func mergePush(old, next *pb.PluginMsg) (*pb.PluginMsg, error) {
	if patch := old.GetPanelPatch(); patch != nil {
		out := proto.Clone(old).(*pb.PluginMsg)
		ops := out.GetPanelPatch().Ops
		for _, op := range next.GetPanelPatch().Ops {
			rowOps := 0
			var previousRows *pb.RowKeys
			for _, prev := range ops {
				if rows := prev.GetSetRows(); rows != nil {
					rowOps++
					previousRows = rows
				}
			}
			held := rowOps > 1
			if rows := op.GetSetRows(); rows != nil && previousRows != nil {
				for _, key := range rows.Keys {
					if !slices.Contains(previousRows.Keys, key) {
						held = true
					}
				}
			}
			if held {
				if len(ops) >= 256 {
					return nil, ErrQueueFull
				}
				ops = append(ops, op)
				continue
			}
			property := op.ProtoReflect().WhichOneof(op.ProtoReflect().Descriptor().Oneofs().ByName("op"))
			index := -1
			for i, prev := range ops {
				if prev.ProtoReflect().WhichOneof(prev.ProtoReflect().Descriptor().Oneofs().ByName("op")) == property {
					index = i
					break
				}
			}
			if index < 0 {
				ops = append(ops, op)
			} else if op.GetSetValue() != nil && previousRows != nil {
				// A new selection belongs after the rows pending with it.
				ops = append(append(ops[:index:index], ops[index+1:]...), op)
			} else {
				ops[index] = op
			}
		}
		out.GetPanelPatch().Ops = ops
		return out, nil
	}
	if a := old.GetSegmentShortfall(); a != nil {
		b := next.GetSegmentShortfall()
		if uint64(a.Dropped)+uint64(b.Dropped) > math.MaxUint32 {
			return nil, ErrQueueFull
		}
		out := proto.Clone(next).(*pb.PluginMsg)
		if a.WantedBytes >= b.WantedBytes {
			out = proto.Clone(old).(*pb.PluginMsg)
		}
		out.GetSegmentShortfall().Dropped = a.Dropped + b.Dropped
		return out, nil
	}
	return next, nil // OverlayUpdate is a full replacement, including empty items.
}

func (c *Client) sealOutput() <-chan error { return c.sealWithReport(nil) }

// The final report has a reserved slot, so a full ordinary queue cannot lose it.
func (c *Client) sealWithReport(report *pb.ShutdownReport) <-chan error {
	c.queueMu.Lock()
	defer c.queueMu.Unlock()
	c.initQueue()
	c.sealed = true
	if report != nil {
		c.finalReport = &pb.PluginMsg{Msg: &pb.PluginMsg_ShutdownReport{ShutdownReport: report}}
	}
	c.barrier = make(chan error, 1)
	c.wakeWriter()
	return c.barrier
}

func (c *Client) nextOutbound() (outbound, bool) {
	for {
		c.queueMu.Lock()
		c.initQueue()
		if c.ctx.Err() != nil {
			c.queueMu.Unlock()
			return outbound{}, false
		}
		var x outbound
		found := true
		select {
		case x = <-c.out:
		default:
			if len(c.order) > 0 {
				x.before = c.snapshot()
			} else if c.finalReport != nil {
				x.msg = c.finalReport
				c.finalReport = nil
			} else if c.barrier != nil {
				x.sent = c.barrier
				c.barrier = nil
			} else {
				found = false
			}
		}
		c.coalescingCount -= len(x.before)
		for _, m := range x.before {
			if m.GetShapeProposal() != nil {
				c.shapeCoalescingCount--
			}
			if m.GetWorkProgress() != nil {
				c.workCoalescingCount--
			}
		}
		wake := c.wake
		c.queueMu.Unlock()
		if found {
			return x, true
		}
		select {
		case <-c.ctx.Done():
			return outbound{}, false
		case <-wake:
		}
	}
}
