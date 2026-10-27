package memory

import (
	"errors"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
)

// ErrStageActive means retirement is latched but a callback still borrows memory.
// Release must be retried after the callback returns; never acknowledge release
// or unmap memory underneath a running author callback.
var ErrStageActive = errors.New("processing-stage callback still holds segment")

// StagedTurn pins a region until Finish or Abandon. Copies share the same
// one-use release state. Data and every derived slice expire on release.
type StagedTurn struct {
	Turn stage.Turn // observational snapshot; changing it cannot change the handoff
	Data []byte
	hold *stagedBorrow
}
type stagedBorrow struct {
	segment   *Segment
	index     uint32
	turn      stage.Turn
	data      []byte
	published bool
	closed    bool // protected by segment.mu, including copied public handles
}

// StageWord reads only the atomic word; it does not grant access to the header.
func (s *Segment) StageWord(index uint32) (stage.Turn, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, false)
	if err != nil {
		return stage.Turn{}, err
	}
	if r.layout.Discipline != Staged {
		return stage.Turn{}, ErrDiscipline
	}
	return stage.DecodeTurn(loadState(s.data, r.layout.Offset)), nil
}

// TryStageTurn uses one acquire load to establish ownership and deduplication.
// No nonatomic slot byte is touched until that load matches an armed position.
// A nil turn with nil error means the slot is not new work for this position.
func (s *Segment) TryStageTurn(index uint32, pipeline uint16, position int16, last uint32) (*StagedTurn, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if pipeline == 0 || position < 1 || position > 32766 {
		return nil, ErrBounds
	}
	r, err := s.region(index, false)
	if err != nil {
		return nil, err
	}
	if r.layout.Discipline != Staged {
		return nil, ErrDiscipline
	}
	if r.stageBusy {
		return nil, nil
	}
	t := stage.DecodeTurn(loadState(s.data, r.layout.Offset))
	if !t.IsNew(pipeline, position, last) {
		return nil, nil
	}
	r.stageBusy = true
	s.stagePins++
	start, end := r.layout.Offset, r.layout.Offset+r.layout.Extent
	data := s.data[start:end:end]
	return &StagedTurn{Turn: t, Data: data, hold: &stagedBorrow{segment: s, index: index, turn: t, data: data}}, nil
}

// Finish writes the outcome and release-stores the next holder, including a
// turn already held when retirement began (§9.1). Session teardown instead
// abandons the borrow: a session that has ended must not publish more work.
// The caller must have stopped ALL use of borrowed memory before calling this.
func (t *StagedTurn) Finish(outcome stage.Outcome, reason uint32) error {
	if outcome < stage.Processed || outcome > stage.Failed || (outcome == stage.Processed && reason != 0) || (outcome != stage.Processed && (reason < 1 || reason > 3)) {
		return errors.New("invalid stage outcome/reason")
	}
	return t.release(outcome, reason)
}
func (t *StagedTurn) Abandon() error { return t.release(stage.None, 0) }
func (t *StagedTurn) release(outcome stage.Outcome, reason uint32) error {
	h := t.hold
	s := h.segment
	s.mu.Lock()
	if h.closed {
		s.mu.Unlock()
		return ErrConsumed
	}
	h.closed = true
	r := &s.regions[h.index]
	if outcome != stage.None && !s.abandonStages.Load() {
		le.PutUint32(h.data[8:12], uint32(outcome))
		le.PutUint32(h.data[24:28], reason)
		if outcome == stage.Processed {
			le.PutUint32(h.data[20:24], uint32(h.turn.Position))
		}
		storeState(s.data, r.layout.Offset, h.turn.HandoffWord(outcome == stage.Processed))
		h.published = true
	}
	r.stageBusy = false
	s.stagePins--
	t.Data = nil
	h.data = nil
	cleanup := s.stagePins == 0 && s.retiring.Load()
	s.mu.Unlock()
	if cleanup {
		return s.release()
	}
	return nil
}

// HandedOn reports whether Finish published a turn. It remains usable after
// unmapping, so callers can send the owed D even when retirement just drained.
func (t *StagedTurn) HandedOn() bool {
	t.hold.segment.mu.Lock()
	defer t.hold.segment.mu.Unlock()
	return t.hold.published
}
