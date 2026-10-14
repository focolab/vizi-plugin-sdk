package doorbell

import "sort"

type interaction struct {
	opening  Input
	raw      RawRecord
	sequence uint32
}
type inputState struct {
	highTool, watermark uint32
	active              map[uint32]bool
	open                map[uint32]interaction
	byTool              map[uint32]uint32
	epochs              [256]uint32
}

func newInputState() inputState {
	return inputState{active: make(map[uint32]bool), open: make(map[uint32]interaction), byTool: make(map[uint32]uint32)}
}
func (s *inputState) remove(id uint32) (interaction, bool) {
	i, ok := s.open[id]
	if !ok {
		return i, false
	}
	delete(s.open, id)
	if s.byTool[i.opening.Tool] == id {
		delete(s.byTool, i.opening.Tool)
	}
	return i, true
}
func (s *inputState) cancel(id uint32, reason string) []Event {
	i, ok := s.remove(id)
	if !ok {
		return nil
	}
	return []Event{{Cancellation: &Cancellation{i.opening, reason}}}
}
func (s *inputState) cancelWhere(match func(Input) bool, reason string) []Event {
	ids := make([]uint32, 0, len(s.open))
	for id, i := range s.open {
		if match(i.opening) {
			ids = append(ids, id)
		}
	}
	sort.Slice(ids, func(i, j int) bool { return ids[i] < ids[j] })
	var out []Event
	for _, id := range ids {
		out = append(out, s.cancel(id, reason)...)
	}
	return out
}
func (s *inputState) reject(f *Frame, where string) []Event {
	reject(f, where, "contract_violation")
	e := f.Input
	if e.Phase == Fire {
		return nil
	}
	if e.Phase == Press {
		if s.active[e.Tool] {
			return s.cancel(s.byTool[e.Tool], where)
		}
		return nil
	}
	if e.Interaction != 0 {
		return s.cancel(e.Interaction, where)
	}
	return s.cancel(s.byTool[e.Tool], where)
}
func ignore(f *Frame, code string) { f.Disposition = Ignored; diag(f, code, 1, 0, 0) }

// apply follows E section 2's ordered checks. Lifecycle calls use this same
// state under Transport.mu; cancellation events are appended before unlocking.
func (s *inputState) apply(f *Frame) []Event {
	e := f.Input
	if e.Phase > Fire {
		ignore(f, "unknown_phase")
		return nil
	}
	if e.Phase.opening() && e.Interaction > 0 && e.Interaction <= s.watermark {
		// E section 2's explicit live-opening replay exception: a familiar id
		// cannot merge two different gestures. Closed ids need no retained bytes.
		if old, ok := s.open[e.Interaction]; ok && old.raw != f.Raw {
			return s.reject(f, "interaction_mismatch")
		}
		ignore(f, "duplicate_opening")
		return nil
	}
	if e.Interaction == 0 || e.Tool == 0 || e.Generation == 0 || e.Epoch == 0 {
		return s.reject(f, "zero_id")
	}
	if e.Phase.opening() && e.Sequence != 0 {
		return s.reject(f, "seq_on_open")
	}
	if why := positionViolation(*e, f.Raw[8:108]); why != "" {
		return s.reject(f, why)
	}
	if !targetValid(e.Target) {
		return s.reject(f, "invalid_target")
	}
	installed := s.epochs[e.ViewID]
	if e.Epoch < installed {
		ignore(f, "stale_epoch")
		return nil
	}
	if e.Epoch > installed {
		return s.reject(f, "epoch_mismatch")
	}
	if e.Tool > s.highTool {
		return s.reject(f, "unknown_tool")
	}
	if !s.active[e.Tool] {
		ignore(f, "retired_tool")
		return nil
	}
	if !e.Phase.opening() {
		i, ok := s.open[e.Interaction]
		if !ok {
			ignore(f, "unknown_interaction")
			return nil
		}
		if i.opening.Tool != e.Tool || i.opening.Generation != e.Generation || i.opening.ViewID != e.ViewID {
			return s.reject(f, "interaction_mismatch")
		}
		if e.Sequence <= i.sequence {
			ignore(f, "duplicate_sequence")
			return nil
		}
		if gap := e.Sequence - i.sequence - 1; gap > 0 {
			diag(f, "sequence_gap", uint64(gap), uint64(i.sequence)+1, uint64(e.Sequence))
		}
	}
	// Preserve original bytes for reports/replay; expose only understood values.
	if e.Modifiers&0xf0 != 0 {
		diag(f, "reserved_modifiers", 1, 0, uint64(e.Modifiers&0xf0))
		e.Modifiers &= 0x0f
	}
	if e.Position.Flags&0xfc != 0 {
		diag(f, "reserved_position_flags", 1, 0, uint64(e.Position.Flags&0xfc))
		e.Position.Flags &= 3
	}
	if e.Target.Kind > Handle {
		diag(f, "unknown_target_kind", 1, 0, uint64(e.Target.Kind))
		e.Target = Target{}
	}
	if positionSize(e.Position.Kind) < 0 {
		diag(f, "unknown_position_kind", 1, 0, uint64(e.Position.Kind))
		e.Position = Position{}
	}
	if e.Position.Kind == None && e.Position.Flags&Miss != 0 {
		diag(f, "miss_without_position", 1, 0, 0)
		e.Position.Flags &^= Miss
	}
	if e.Phase.opening() {
		s.watermark = e.Interaction
	}
	switch e.Phase {
	case Press:
		f.ImplicitlyClosed = s.byTool[e.Tool]
		s.remove(f.ImplicitlyClosed)
		s.open[e.Interaction] = interaction{*e, f.Raw, 0}
		s.byTool[e.Tool] = e.Interaction
	case Drag:
		i := s.open[e.Interaction]
		i.sequence = e.Sequence
		s.open[e.Interaction] = i
	case Release, Cancel:
		s.remove(e.Interaction)
	}
	return nil
}
