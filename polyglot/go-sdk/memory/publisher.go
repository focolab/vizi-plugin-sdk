package memory

import (
	"errors"
	"sort"
)

// ErrRevisionExhausted stops this writer without quarantining its last READY
// value. The current ViolationSite enum has no revision-exhaustion value; the
// supervisor must surface this locally and arrange replacement/retirement.
var ErrRevisionExhausted = errors.New("Pulsed revision space exhausted")

type PublishStats struct {
	LastRevision, Published, Coalesced uint64
	Exhausted                          bool
}

// PublishPulsed copies a nonempty payload into a plugin-produced Pulsed region.
// A zero Sample with nil error means coalesced after eight unsuccessful scans.
// Each valid attempt allocates one revision, including coalesced attempts. State
// belongs to the region, so concurrent callers share one serialized writer.
// No mapping aliases or outstanding claims escape. A successful call may be
// followed by a best-effort D; notification delivery is never part of the commit.
func (s *Segment) PublishPulsed(index uint32, payload []byte) (Sample, error) {
	return s.PublishPulsedEpoch(index, payload, nil)
}

// PublishPulsedEpoch commits payload and its pane pair under the same claim.
// A pane-associated lane requires a nonzero epoch; other lanes require nil.
func (s *Segment) PublishPulsedEpoch(index uint32, payload []byte, pair *PaneEpoch) (Sample, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, false)
	if err != nil {
		return Sample{}, err
	}
	l := r.layout
	if l.Discipline != Pulsed || l.Producer != Plugin {
		return Sample{}, ErrDiscipline
	}
	if len(payload) == 0 || uint64(len(payload)) > l.PayloadCapacity() {
		return Sample{}, ErrBounds
	}
	if (l.EpochOffset != nil) != (pair != nil) || (pair != nil && pair.Epoch == 0) {
		return Sample{}, errors.New("publication pane pair does not match lane")
	}
	if r.publishing.LastRevision == revisionMask {
		r.publishing.Exhausted = true
		return Sample{}, ErrRevisionExhausted
	}
	r.publishing.LastRevision++
	revision := r.publishing.LastRevision
	words := make([]uint64, l.Buffers)
	ready := make([]uint32, 0, l.Buffers)
	candidates := make([]uint32, 0, l.Buffers)
	for attempt := 0; attempt < 8; attempt++ {
		ready, candidates = ready[:0], candidates[:0]
		for i := range words {
			word := loadState(s.data, l.Offset+uint64(i)*16)
			words[i] = word
			flags := word & (writeClaim | readClaim)
			if (word&revisionMask == 0 && flags != 0) || flags == writeClaim|readClaim {
				return Sample{}, s.quarantine(r, "impossible_state_word", "invalid Pulsed claim flags or revision zero")
			}
			if word != 0 && flags == 0 {
				ready = append(ready, uint32(i))
			}
		}
		for k := uint64(0); k < uint64(l.Buffers); k++ {
			i := uint32((revision + k) % uint64(l.Buffers))
			if words[i] == 0 {
				candidates = append(candidates, i)
			}
		}
		sort.SliceStable(ready, func(i, j int) bool { return words[ready[i]] < words[ready[j]] })
		if len(ready) > 1 {
			candidates = append(candidates, ready[:len(ready)-1]...)
		}
		for _, i := range candidates {
			state := l.Offset + uint64(i)*16
			if !s.compareSwap(state, words[i], revision|writeClaim) {
				continue
			}
			if err := s.publishClaimed(r, i, revision, payload, pair); err != nil {
				return Sample{}, err
			}
			r.publishing.Published++
			return Sample{revision, len(payload)}, nil
		}
	}
	r.publishing.Coalesced++
	return Sample{}, nil
}

func (s *Segment) publishClaimed(r *regionState, i uint32, revision uint64, payload []byte, pair *PaneEpoch) (err error) {
	l := r.layout
	state := l.Offset + uint64(i)*16
	committed := false
	defer func() {
		if !committed && !s.compareSwap(state, revision|writeClaim, 0) {
			err = errors.Join(err, s.quarantine(r, "release_cas_failed", "write claim was modified by peer during failed publication"))
		}
	}()
	start := l.Offset + uint64(l.Buffers)*16 + uint64(i)*l.SlotBytes
	dst := s.data[start : start+uint64(len(payload))]
	if s.publishCopy != nil {
		if err = s.publishCopy(dst, payload); err != nil {
			return err
		}
	} else {
		copy(dst, payload)
	}
	if pair != nil {
		h := start + *l.EpochOffset
		le.PutUint32(s.data[h:h+4], uint32(pair.ViewID))
		le.PutUint32(s.data[h+4:h+8], pair.Epoch)
	}
	le.PutUint64(s.data[state+8:state+16], uint64(len(payload)))
	storeState(s.data, state, revision) // release: payload -> length -> READY
	committed = true
	return nil
}

// PublishingStats remains available after retirement or quarantine.
func (s *Segment) PublishingStats(index uint32) (PublishStats, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if uint64(index) >= uint64(len(s.regions)) {
		return PublishStats{}, ErrBounds
	}
	r := &s.regions[index]
	if r.layout.Discipline != Pulsed || r.layout.Producer != Plugin {
		return PublishStats{}, ErrDiscipline
	}
	return r.publishing, nil
}
