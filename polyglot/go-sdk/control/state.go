package control

import (
	"errors"
	"fmt"
	"math"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

// Lifecycle is implemented by doorbell.Transport. Failed calls are NEVER acked.
type Lifecycle interface {
	ToolActivated(uint32) error
	ToolRetired(uint32) error
	EpochAcknowledged(uint8, uint32) error
}

// PaneState is a whole interpretation snapshot. Time is nil when unknown.
// State.Pane returns independent copies, suitable for keeping with an opening
// interaction until its terminal/cancellation has been handled.
type PaneState struct {
	Pane    *pb.PaneEpoch
	Dataset *pb.DatasetState
	Time    *pb.TimeMapping
}

// State installs tool, time and pane arrangements before acknowledging them.
// Current snapshots are bounded by 256 panes and the doorbell's active-tool
// bound. Time mappings are retained per dataset generation until ForgetDataset.
type State struct {
	mu             sync.RWMutex
	transport      Lifecycle
	tools          map[uint32]*pb.ToolBinding
	toolStatements map[uint32]*pb.ToolBinding
	panes          map[uint8]*PaneState
	times          map[timeKey]*pb.TimeMapping
	latest         map[uint64]uint32
	timeBytes      uint64
	reporter       *violations.Reporter
}
type timeKey struct {
	gen      uint64
	revision uint32
}

func NewState(t Lifecycle, reporter *violations.Reporter) *State {
	return &State{transport: t, reporter: reporter, tools: make(map[uint32]*pb.ToolBinding), toolStatements: make(map[uint32]*pb.ToolBinding), panes: make(map[uint8]*PaneState), times: make(map[timeKey]*pb.TimeMapping), latest: make(map[uint64]uint32)}
}

func (s *State) violation(where violations.Site, detail string) error {
	if s.reporter != nil {
		if err := s.reporter.Observe(violations.Finding{Where: where, Detail: detail}); err != nil {
			return err
		}
	}
	return errors.New(detail)
}

// rejectTool preserves actionable local context for rejections without a
// specified ViolationSite (malformed content). Do not route these
// through arrangement_content_mismatch: §11 requires a site per rule, and
// unknown enum values are explicitly local compatibility diagnostics.
func (s *State) rejectTool(t *pb.ToolBinding, detail string) error {
	return fmt.Errorf("ToolBinding refused: tool=%d revision=%d installed_revision=%d mode=%d: %s",
		t.Tool, t.Revision, s.toolStatements[t.Tool].GetRevision(), t.Mode, detail)
}

func (s *State) Apply(c *Client, in *pb.HostMsg) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch x := in.Msg.(type) {
	case *pb.HostMsg_ToolBinding:
		t := x.ToolBinding
		if t.Tool == 0 {
			return s.violation(violations.ZeroID, "ToolBinding.tool is zero")
		}
		if t.Revision == 0 {
			return s.violation(violations.ZeroID, "ToolBinding.revision is zero")
		}
		if old := s.toolStatements[t.Tool]; old != nil {
			if t.Revision < old.Revision {
				return s.violation(violations.ArrangementRegressed, s.rejectTool(t, "stale ToolBinding revision").Error())
			}
			if t.Revision == old.Revision {
				if !proto.Equal(t, old) {
					return s.violation(violations.ArrangementContentMismatch, "conflicting ToolBinding revision")
				}
				return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: t.Tool, Revision: t.Revision}}})
			}
		} else if len(s.toolStatements) >= 65536 {
			return errors.New("tool statement retention limit reached")
		}

		if !t.Retired && (t.ToolId == "" || (t.Mode != pb.ToolMode_TOOL_MODE_FIRE && t.Mode != pb.ToolMode_TOOL_MODE_GESTURE)) {
			return s.rejectTool(t, "ToolBinding requires tool_id and a known mode")
		}
		var err error
		if t.Retired {
			err = s.transport.ToolRetired(t.Tool)
		} else {
			err = s.transport.ToolActivated(t.Tool)
		}
		if err != nil {
			return err
		}
		if t.Retired {
			delete(s.tools, t.Tool)
		} else {
			s.tools[t.Tool] = proto.Clone(t).(*pb.ToolBinding)
		}
		first := s.toolStatements[t.Tool] == nil
		s.toolStatements[t.Tool] = proto.Clone(t).(*pb.ToolBinding)
		if first && t.Revision != 1 {
			if err := s.report(c, violations.ArrangementFirstRevision, fmt.Sprintf("ToolBinding tool=%d first revision=%d; installed", t.Tool, t.Revision)); err != nil {
				return err
			}
		}
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ToolBindingAck{ToolBindingAck: &pb.ToolBindingAck{Tool: t.Tool, Revision: t.Revision}}})
	case *pb.HostMsg_TimeMapping:
		t := x.TimeMapping
		if t.Revision == 0 {
			return s.violation(violations.ZeroID, "TimeMapping.revision is zero")
		}
		key := timeKey{t.Gen, t.Revision}
		if old := s.times[key]; old != nil {
			if !proto.Equal(t, old) {
				return s.violation(violations.ArrangementContentMismatch, "conflicting TimeMapping replay")
			}
		} else {
			if t.Revision <= s.latest[t.Gen] {
				return s.violation(violations.ArrangementRegressed, "stale TimeMapping revision")
			}
			// Local retention limits, not host deadlines or wire limits. Retaining
			// an acknowledged revision is required until its dataset is forgotten.
			bytes := uint64(len(t.T)) * 8
			if len(s.times) >= 1024 || bytes > (64<<20)-s.timeBytes {
				return errors.New("time mapping retention limit reached; retire unused dataset state")
			}
			s.times[key] = proto.Clone(t).(*pb.TimeMapping)
			s.timeBytes += bytes
			// Lookup is index-wise: malformed values do not prevent installation
			// or acknowledgement (§5.5). Replay identity still binds the content.
			for i, v := range t.T {
				if math.IsNaN(v) || math.IsInf(v, 0) || (i > 0 && v < t.T[i-1]) {
					c.diagnostic(fmt.Errorf("TimeMapping gen=%d revision=%d is not finite and ascending; installed", t.Gen, t.Revision))
					break
				}
			}
			first := s.latest[t.Gen] == 0
			s.latest[t.Gen] = t.Revision
			if first && t.Revision != 1 {
				if err := s.report(c, violations.ArrangementFirstRevision, fmt.Sprintf("TimeMapping gen=%d first revision=%d; installed", t.Gen, t.Revision)); err != nil {
					return err
				}
			}
		}
		return c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_TimeMappingAck{TimeMappingAck: &pb.TimeMappingAck{Gen: t.Gen, Revision: t.Revision}}})
	case *pb.HostMsg_EpochChange:
		return s.epochs(c, x.EpochChange)
	}
	return fmt.Errorf("not an input arrangement: %s", kind(in.ProtoReflect()))
}

func (s *State) epochs(c *Client, in *pb.EpochChange) error {
	datasets := make(map[uint64]*pb.DatasetState, len(in.Datasets))
	for _, d := range in.Datasets {
		if d == nil {
			return errors.New("EpochChange contains nil DatasetState")
		}
		if _, exists := datasets[d.Gen]; exists {
			return errors.New("duplicate EpochChange dataset")
		}
		datasets[d.Gen] = d
		// §8.10 does not make geometry presence or its redundant gen an ack
		// gate. Preserve the supplied state (including absence) and diagnose
		// locally; never strand this dataset's panes or unrelated panes.
		if d.Geometry == nil {
			c.diagnostic(fmt.Errorf("EpochChange DatasetState gen=%d has no geometry", d.Gen))
		} else if d.Geometry.Gen != d.Gen {
			c.diagnostic(fmt.Errorf("EpochChange DatasetState gen=%d carries Geometry.gen=%d", d.Gen, d.Geometry.Gen))
		}
	}
	seen := make(map[uint32]bool, len(in.Panes))
	for _, p := range in.Panes {
		if p == nil || p.ViewId > 255 || seen[p.ViewId] {
			return errors.New("invalid or duplicate EpochChange pane")
		}
		seen[p.ViewId] = true
	}
	ack := &pb.EpochAck{}
	var failures []error
	for _, p := range in.Panes {
		if p.Epoch == 0 {
			failures = append(failures, s.violation(violations.ZeroID, fmt.Sprintf("EpochChange pane view_id=%d has zero epoch", p.ViewId)))
			continue
		}
		d := datasets[p.Gen]
		if d == nil {
			failures = append(failures, s.violation(violations.MissingArrangement, "pane names absent DatasetState"))
			continue
		}
		var tm *pb.TimeMapping
		if d.TimeMappingRevision != 0 {
			tm = s.times[timeKey{d.Gen, d.TimeMappingRevision}]
			if tm == nil {
				failures = append(failures, s.violation(violations.MissingArrangement, "pane names unacknowledged TimeMapping"))
				continue
			}
		}
		badChannels := false
		// Geometry.channels supplies names, not a documented exhaustive
		// channel inventory. Unnamed high channels remain visible as supplied.
		for _, ch := range p.ChannelsHigh {
			if ch < 15 {
				badChannels = true
			}
		}
		if badChannels {
			failures = append(failures, errors.New("invalid channels_high"))
			continue
		}
		view := uint8(p.ViewId)
		if old := s.panes[view]; old != nil {
			if p.Epoch < old.Pane.Epoch {
				failures = append(failures, s.violation(violations.ArrangementRegressed, "stale pane epoch"))
				continue
			}
			if p.Epoch == old.Pane.Epoch && (!proto.Equal(p, old.Pane) || !proto.Equal(d, old.Dataset)) {
				failures = append(failures, s.violation(violations.ArrangementContentMismatch, "same pane epoch carries different state"))
				continue
			}
		}
		next := &PaneState{Pane: proto.Clone(p).(*pb.PaneEpoch), Dataset: proto.Clone(d).(*pb.DatasetState)}
		if tm != nil {
			next.Time = tm // immutable internal mapping; public snapshots clone it
		}
		// Readers take this mutex, so no event can observe a successfully
		// advanced transport epoch without also observing its whole state.
		if err := s.transport.EpochAcknowledged(view, p.Epoch); err != nil {
			failures = append(failures, err)
			continue
		}
		s.panes[view] = next
		ack.Panes = append(ack.Panes, &pb.PaneEpoch{ViewId: p.ViewId, Epoch: p.Epoch})
	}
	if len(ack.Panes) > 0 {
		if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_EpochAck{EpochAck: ack}}); err != nil {
			return err
		}
	}
	return errors.Join(failures...)
}

func (s *State) Pane(view uint8) *PaneState {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p := s.panes[view]
	if p == nil {
		return nil
	}
	out := &PaneState{Pane: proto.Clone(p.Pane).(*pb.PaneEpoch), Dataset: proto.Clone(p.Dataset).(*pb.DatasetState)}
	if p.Time != nil {
		out.Time = proto.Clone(p.Time).(*pb.TimeMapping)
	}
	return out
}

// AcknowledgedEpoch is the allocation-free fast-path lookup for D hints.
func (s *State) AcknowledgedEpoch(view uint8) uint32 {
	s.mu.RLock()
	defer s.mu.RUnlock()
	if p := s.panes[view]; p != nil {
		return p.Pane.Epoch
	}
	return 0
}
func (s *State) Tool(handle uint32) *pb.ToolBinding {
	s.mu.RLock()
	defer s.mu.RUnlock()
	t := s.tools[handle]
	if t == nil {
		return nil
	}
	return proto.Clone(t).(*pb.ToolBinding)
}

// ActionID resolves an action FIRE through the currently installed tool
// arrangement. Primary input, unknown indices and inactive tools return false.
// An unknown index is forward compatibility, never a fault or diagnostic.
func (s *State) ActionID(in doorbell.Input) (string, bool) {
	if in.Phase != doorbell.Fire || in.Action == 0 {
		return "", false
	}
	s.mu.RLock()
	defer s.mu.RUnlock()
	for _, action := range s.tools[in.Tool].GetActions() {
		if action.GetAction() == uint32(in.Action) {
			return action.GetActionId(), true
		}
	}
	return "", false
}

// ForgetDataset drops the independent mapping cache only after no current pane
// refers to it. Captured PaneState values keep their own copy for cancellations.
// This is a local lifetime operation, not a protocol message or an epoch reset.
func (s *State) ForgetDataset(gen uint64) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	for _, p := range s.panes {
		if p.Pane.Gen == gen {
			return errors.New("dataset still referenced by a pane")
		}
	}
	for key := range s.times {
		if key.gen == gen {
			s.timeBytes -= uint64(len(s.times[key].T)) * 8
			delete(s.times, key)
		}
	}
	delete(s.latest, gen)
	return nil
}

// report records a defect whose documented fallback still installs the statement.
func (s *State) report(c *Client, where violations.Site, detail string) error {
	if s.reporter != nil {
		return s.reporter.Observe(violations.Finding{Where: where, Detail: detail})
	}
	return c.reportViolation(where, detail)
}
