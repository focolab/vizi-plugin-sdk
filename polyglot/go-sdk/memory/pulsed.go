package memory

import "errors"

const (
	writeClaim   uint64 = 1 << 62
	readClaim    uint64 = 1 << 63
	revisionMask uint64 = writeClaim - 1
)

// Sample describes a copied consume-once value. No sample (including 8 lost
// claims) is Sample{} with nil error. Revision never wraps and zero means absent.
type Sample struct {
	Revision uint64
	Length   int
}

func (s *Segment) compareSwap(offset, old, next uint64) bool {
	if s.cas != nil {
		return s.cas(s.data, offset, old, next)
	}
	return casState(s.data, offset, old, next)
}

// ReadPulsed is the low-level synchronous primitive for callers that already own
// an unconditional read schedule. Applications should use ConsumePulsed, which
// supplies that schedule. ReadPulsed refuses a region with an active consumer.
// It copies the newest READY revision strictly above this region's
// last_seen. dst must have length >= SlotBytes, checked BEFORE claiming anything;
// this prevents a caller's undersized buffer from consuming an unread value.
// It makes at most 8 scan/claim attempts, never waits for the producer, and always
// releases its own claim before returning. dst is unspecified on error; only a
// successful nonzero Sample authorises using dst[:Sample.Length]. An arranged
// pane header is also copied at EpochOffset; decode it from the full dst before
// discarding the slot tail. Both copies precede release of the claim.
func (s *Segment) ReadPulsed(index uint32, dst []byte) (Sample, error) {
	return s.readPulsed(index, dst, nil)
}

func (s *Segment) readPulsed(index uint32, dst []byte, consumer *PulsedConsumer) (Sample, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, false)
	if err != nil {
		return Sample{}, err
	}
	l := r.layout
	if l.Discipline != Pulsed || l.Producer != Host {
		return Sample{}, ErrDiscipline
	}
	if r.consumer != consumer {
		return Sample{}, ErrConsumerActive
	}
	if uint64(len(dst)) < l.SlotBytes {
		return Sample{}, ErrBounds
	}
	for attempt := 0; attempt < 8; attempt++ {
		var revision uint64
		var chosen uint32
		for i := uint32(0); i < l.Buffers; i++ {
			word := loadState(s.data, l.Offset+uint64(i)*16)
			flags := word & (writeClaim | readClaim)
			id := word & revisionMask
			if (id == 0 && flags != 0) || flags == writeClaim|readClaim {
				return Sample{}, s.quarantine(r, "impossible_state_word", "invalid Pulsed claim flags or revision zero")
			}
			if flags == 0 && id > r.lastSeen && id > revision {
				revision, chosen = id, i
			}
		}
		if revision == 0 {
			return Sample{}, nil
		}
		state := l.Offset + uint64(chosen)*16
		claimed := revision | readClaim
		if !s.compareSwap(state, revision, claimed) {
			continue
		}
		length := le.Uint64(s.data[state+8 : state+16])
		payload := l.Offset + uint64(l.Buffers)*16 + uint64(chosen)*l.SlotBytes
		if length == 0 || length > l.PayloadCapacity() || !inRange(payload, length, uint64(len(s.data))) {
			invalid := s.quarantine(r, "invalid_published_length", "Pulsed length is zero or exceeds payload bounds")
			if !s.compareSwap(state, claimed, 0) {
				return Sample{}, errors.Join(s.quarantine(r, "release_cas_failed", "invalid-length claim was modified by peer"), invalid)
			}
			return Sample{}, invalid
		}
		copy(dst, s.data[payload:payload+length])
		if l.EpochOffset != nil {
			h := *l.EpochOffset
			copy(dst[h:h+8], s.data[payload+h:payload+h+8])
		}
		if !s.compareSwap(state, claimed, 0) {
			return Sample{}, s.quarantine(r, "release_cas_failed", "read claim was modified by peer")
		}
		r.lastSeen = revision
		return Sample{revision, int(length)}, nil
	}
	return Sample{}, nil
}
