package memory

import "errors"

// ErrLoanActive means a loan publication or arrived deposit still holds a claim.
// Retire must be retried after LoansDrained closes.
var ErrLoanActive = errors.New("loaned work still holds segment")

// LoansDrained closes when every in-flight reservation/read is completed or rolled
// back. Capture after retirement is latched, so no new reservation can begin.
func (s *Segment) LoansDrained() <-chan struct{} {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.loanPins == 0 {
		done := make(chan struct{})
		close(done)
		return done
	}
	return s.loansDrained
}

// finishLoan is called with mu held after all sends, host release or rollback.
func (s *Segment) finishLoan(loan *loanUse) {
	if !loan.pending {
		return
	}
	loan.pending = false
	s.finishUse()
}

// Count before observing retiring, under the same lock as unmap.
func (s *Segment) beginUse() {
	if s.loanPins == 0 {
		s.loansDrained = make(chan struct{})
	}
	s.loanPins++
}
func (s *Segment) finishUse() {
	if s.loanPins == 0 {
		return
	} // Close already abandoned these claims.
	s.loanPins--
	if s.loanPins == 0 {
		close(s.loansDrained)
	}
}

var ErrArenaFull = errors.New("Loaned arena cannot fit this publication")

// Deprecated: Loaned slot_bytes is reserved and never gates allocation.
var ErrLoanAlignment = errors.New("Loaned allocation requires the specified slot_bytes alignment of 64")

type LoanStats struct {
	Cursor, Outstanding, Refused uint64
}

// Shortfall is captured under the allocator lock at the failed allocation.
// The control plane coalesces these per (SegmentID, Region) before reporting.
type Shortfall struct {
	SegmentID                     uint64
	Region                        uint32
	WantedBytes, LargestFreeBytes uint64
}

// WriteLoan copies a nonempty payload and reserves its bytes until ReleaseLoan
// or segment retirement. The returned key is what the descriptor and the release
// both carry: a MAPPING-relative offset (§1, §7) and the requested length. The
// ring's own modular address stays region-relative inside this allocator and is
// converted here, at its boundary, so the two forms never mix in one number.
// Every caller shares this region's cursor. Reservations are padded to the 64-byte
// alignment required by §7. The bump cursor rewinds only when no loans remain.
// ErrArenaFull is a counted, immediate refusal, never a wait or implicit growth.
//
// Success reserves bytes immediately; it does NOT admit a descriptor to the
// control queue. It pins retirement until every declared use is sent or released.
// If building or sending fails before ANY use is admitted, the caller
// MUST reclaim the reservation with s.ReleaseLoan(key) exactly once (or
// retry admission). Otherwise no host ReleaseLoan will arrive. Once any use is
// admitted, bytes remain owned by the host until release or retirement. Calling
// ReleaseLoan after a later admission failure marks the unadmitted remainder
// stranded and returns ErrLoanAdmitted; it does not reclaim bytes. This removes
// that remainder's retirement claim after admitted sends complete. See
// control.PublishLoan for automatic rollback of a single-use panel/overlay push.
func (s *Segment) WriteLoan(index uint32, payload []byte) (Deposit, error) {
	d, _, err := s.WriteLoanReport(index, payload)
	return d, err
}

// WriteLoanReport also returns the precise shortfall on ErrArenaFull. It does
// not send a message per failure; use a bounded control-plane coalescer. Its
// successful reservation has the same caller-owned admission/rollback obligation
// as WriteLoan.
func (s *Segment) WriteLoanReport(index uint32, payload []byte) (Deposit, *Shortfall, error) {
	return s.writeLoan(index, payload, true)
}

// TryWriteLoan reserves a solicited chunk without counting arena exhaustion as
// a refusal or shortfall. The caller retains the answer and retries on release.
// Successful reservations have exactly the same ownership as WriteLoan.
func (s *Segment) TryWriteLoan(index uint32, payload []byte) (Deposit, error) {
	d, _, err := s.writeLoan(index, payload, false)
	return d, err
}

func (s *Segment) writeLoan(index uint32, payload []byte, countRefusal bool) (Deposit, *Shortfall, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.beginUse()
	retained := false
	defer func() {
		if !retained {
			s.finishUse()
		}
	}()
	r, err := s.region(index, false)
	if err != nil {
		return Deposit{}, nil, err
	}
	if r.layout.Discipline != Loaned || r.layout.Producer != Plugin {
		return Deposit{}, nil, ErrDiscipline
	}
	n := uint64(len(payload))
	if n == 0 {
		return Deposit{}, nil, ErrBounds
	}
	padded := (n + 63) &^ uint64(63) // len fits an int, so addition cannot overflow
	if !inRange(r.loanCursor, padded, r.layout.Extent) {
		if !countRefusal {
			return Deposit{}, nil, ErrArenaFull
		}
		r.loanRefused++
		return Deposit{}, &Shortfall{s.id, index, n, r.layout.Extent - r.loanCursor}, ErrArenaFull
	}
	key := Deposit{s.id, index, r.layout.Offset + r.loanCursor, n}
	if s.loanCopy != nil {
		s.loanCopy(s.data[key.Offset:key.Offset+n], payload)
	} else {
		copy(s.data[key.Offset:key.Offset+n], payload)
	}
	if r.loans == nil {
		r.loans = make(map[Deposit]*loanUse)
	}
	retained = true
	r.loans[key] = &loanUse{pending: true}
	r.loanCursor += padded
	return key, nil, nil
}

// ApplyHostRelease applies one exactly-once reliable host release, keyed by the
// mapping-relative offset the descriptor carried and the original requested
// length, not allocator padding. Mistargeted, unknown and wrong-producer
// releases report a violation and quarantine the named region without changing
// any reservation.
// A duplicate can alias a later
// reuse of the same key: dispatch exactly once; the protocol has no generation.
// Only the control receiver should call this method, for a received ReleaseLoan.
func (s *Segment) ApplyHostRelease(key Deposit) error {
	return s.releaseLoan(key, true)
}

// ReleaseLoan reclaims a reservation that has never been admitted. An admitted
// reservation belongs to the peer until its release or segment retirement;
// attempting local reclaim marks it stranded and returns ErrLoanAdmitted. Its
// bytes stay owned by the host, but unsent/unadmitted uses no longer pin retirement.
func (s *Segment) ReleaseLoan(key Deposit) error {
	return s.releaseLoan(key, false)
}

func (s *Segment) releaseLoan(key Deposit, fromHost bool) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.data == nil {
		return ErrRetired
	}
	if key.SegmentID != s.id || uint64(key.Region) >= uint64(len(s.regions)) {
		return &Violation{"unknown_release", "ReleaseLoan names no region in this segment", key.SegmentID, key.Region}
	}
	r := &s.regions[key.Region]
	if r.quarantined {
		return ErrQuarantined
	}
	if r.layout.Producer != Plugin {
		return s.quarantine(r, "wrong_producer_release", "ReleaseLoan names a host-produced region")
	}
	// This is the cross-check `region` is carried for: an offset that falls
	// outside the region it names is a mistargeting, reported as such rather
	// than as unknown_release, which means no reservation at all.
	if key.Offset < r.layout.Offset || !inRange(key.Offset-r.layout.Offset, key.Length, r.layout.Extent) {
		return s.quarantine(r, "release_region_mismatch", "ReleaseLoan's offset falls outside the region it names")
	}
	loan, ok := r.loans[key]
	if !ok {
		return s.quarantine(r, "unknown_release", "ReleaseLoan names no outstanding reservation")
	}
	if !fromHost && loan.named > 0 {
		loan.stranded = true
		if loan.sent == loan.named {
			s.finishLoan(loan)
		}
		return ErrLoanAdmitted
	}
	s.finishLoan(loan)
	delete(r.loans, key)
	if len(r.loans) == 0 {
		r.loanCursor = 0
	}
	return nil
}

func (s *Segment) LoanStats(index uint32) (LoanStats, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if uint64(index) >= uint64(len(s.regions)) {
		return LoanStats{}, ErrBounds
	}
	r := &s.regions[index]
	if r.layout.Discipline != Loaned || r.layout.Producer != Plugin {
		return LoanStats{}, ErrDiscipline
	}
	return LoanStats{r.loanCursor, uint64(len(r.loans)), r.loanRefused}, nil
}

// SliceLoan replaces an unpublished reservation with a subrange of its bytes.
// The subrange is moved to the original aligned offset, and the returned key
// replaces key for admission and release. No second arena allocation is needed.
// The original allocator footprint remains reserved until the loan is released.
// The caller must exclusively own the unpublished loan; admitted loans are never
// changed. Errors leave both bytes and reservation identity unchanged.
func (s *Segment) SliceLoan(key Deposit, offset, length uint64) (Deposit, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if key.SegmentID != s.id {
		return Deposit{}, ErrBounds
	}
	r, err := s.region(key.Region, true)
	if err != nil {
		return Deposit{}, err
	}
	if r.layout.Discipline != Loaned || r.layout.Producer != Plugin {
		return Deposit{}, ErrDiscipline
	}
	loan := r.loans[key]
	if loan == nil {
		return Deposit{}, ErrBounds
	}
	if loan.named != 0 {
		return Deposit{}, ErrLoanAdmitted
	}
	if length == 0 || !inRange(offset, length, key.Length) {
		return Deposit{}, ErrBounds
	}
	next := key
	next.Length = length
	copy(s.data[key.Offset:key.Offset+length], s.data[key.Offset+offset:key.Offset+offset+length])
	delete(r.loans, key)
	r.loans[next] = loan
	return next, nil
}
