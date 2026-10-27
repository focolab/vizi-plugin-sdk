package memory

import (
	"sync"
	"sync/atomic"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// Segment copies ordinary payloads under a lifetime lock. Staged turns instead
// pin their mapping until the borrower returns; loans pin until every send or
// release, and arrived deposits until read/disposition. Each Pulsed region shares
// last_seen and writer revision; each Loaned arena shares its allocator, regardless
// of how many callers retrieve the segment.
type Segment struct {
	mu            sync.Mutex
	stagePins     int
	loanPins      int
	loansDrained  chan struct{}
	releasedLoans map[Deposit]struct{} // exact outstanding keys captured under mu at unmap
	id            uint64
	purpose       string
	subject       string
	proof         [16]byte
	data          []byte
	regions       []regionState
	installed     bool
	retiring      atomic.Bool
	abandonStages atomic.Bool   // session teardown, distinct from ordinary retirement
	consumerStop  chan struct{} // created lazily and closed on release, under mu
	// Optional CAS backend override for deterministic contention/interference tests.
	cas func([]byte, uint64, uint64, uint64) bool
	// Test-only copy failure injection, after a publishing claim is acquired.
	publishCopy func([]byte, []byte) error
	loanCopy    func([]byte, []byte) // test-only barrier inside a loaned write
	depositCopy func([]byte, []byte) // test-only barrier inside a deposit read
}
type regionState struct {
	layout      Region
	stageBusy   bool
	lastSeen    uint64
	quarantined bool
	consumer    *PulsedConsumer
	publishing  PublishStats
	loanCursor  uint64
	loans       map[Deposit]*loanUse
	loanRefused uint64
}

func (s *Segment) ID() uint64 { return s.id }

// Purpose is immutable after installation and does not replace per-region roles.
func (s *Segment) Purpose() string { return s.purpose }

// Subject is the manifest contribution ID for O/P; empty for G/V/X/S.
func (s *Segment) Subject() string { return s.subject }
func (s *Segment) Regions() []Region {
	s.mu.Lock()
	defer s.mu.Unlock()
	out := make([]Region, len(s.regions))
	for i := range s.regions {
		out[i] = cloneRegion(s.regions[i].layout)
	}
	return out
}
func (s *Segment) region(index uint32, allowRetiring bool) (*regionState, error) {
	if s.data == nil || (s.retiring.Load() && !allowRetiring) {
		return nil, ErrRetired
	}
	if !s.installed {
		return nil, ErrNotInstalled
	}
	if uint64(index) >= uint64(len(s.regions)) {
		return nil, ErrBounds
	}
	r := &s.regions[index]
	if r.quarantined {
		return nil, ErrQuarantined
	}
	return r, nil
}
func inRange(offset, length, extent uint64) bool { return offset <= extent && length <= extent-offset }

func (s *Segment) release() error {
	// Latch before waiting for the current copy, preventing queued new operations
	// from extending the lifetime. Staged borrows defer unmap until they finish.
	s.retiring.Store(true)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.consumerStop != nil {
		close(s.consumerStop)
		s.consumerStop = nil
	}
	if s.data == nil {
		return nil
	}
	if s.stagePins != 0 {
		return ErrStageActive
	}
	if s.loanPins != 0 && !s.abandonStages.Load() {
		return ErrLoanActive
	}
	if err := unmap(s.data); err != nil {
		return err
	}
	s.data = nil
	if s.loanPins != 0 {
		close(s.loansDrained)
		s.loanPins = 0
	}
	for i := range s.regions {
		for key := range s.regions[i].loans {
			if s.releasedLoans == nil {
				s.releasedLoans = make(map[Deposit]struct{})
			}
			s.releasedLoans[key] = struct{}{}
		}
		s.regions[i].loans = nil
		s.regions[i].loanCursor = 0
	}
	return nil
}

// ReadConstant copies len(dst) bytes at a REGION-relative offset. The returned
// data remains valid after retirement. While an in-flight operation delays unmap,
// Constant re-reads are safe; after unmap every read fails with ErrRetired.
func (s *Segment) ReadConstant(index uint32, offset uint64, dst []byte) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, true)
	if err != nil {
		return err
	}
	if r.layout.Discipline != Constant {
		return ErrDiscipline
	}
	if !inRange(offset, uint64(len(dst)), r.layout.Extent) {
		return ErrBounds
	}
	start := r.layout.Offset + offset
	copy(dst, s.data[start:start+uint64(len(dst))])
	return nil
}

// A handoff's state is shared even if the exported handle value is copied.
type handoff struct {
	s              *Segment
	index          uint32
	offset, length uint64
	consumed       bool // protected by s.mu
	held           bool // deposit claim, protected by s.mu
}
type DepositRead struct{ h *handoff }
type OwnedRead struct{ h *handoff }

// AcceptDeposit is called exactly once per descriptor delivered by the reliable
// control plane, never from D or a poll. Offset is MAPPING-relative, exactly as
// the descriptor carried it (§1, §7); ResolveDeposit finds the region for one.
// This authorization cannot be inferred from bytes: the descriptor is the host's
// promise not to reuse them.
func (s *Segment) AcceptDeposit(d Deposit) (*DepositRead, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.beginUse()
	retained := false
	defer func() {
		if !retained {
			s.finishUse()
		}
	}()
	if d.SegmentID != s.id {
		return nil, ErrBounds
	}
	r, err := s.region(d.Region, false)
	if err != nil {
		return nil, err
	}
	if r.layout.Discipline != Loaned || r.layout.Producer != Host {
		return nil, ErrDiscipline
	}
	if d.Length == 0 || d.Offset < r.layout.Offset ||
		!inRange(d.Offset-r.layout.Offset, d.Length, r.layout.Extent) {
		return nil, ErrBounds
	}
	retained = true
	return &DepositRead{&handoff{s: s, index: d.Region, offset: d.Offset, length: d.Length, held: true}}, nil
}

// ResolveDeposit finds the unique region a mapping-relative descriptor lies in
// and returns its release key, whose offset is that same number unchanged. It
// does not authorize a read on its own.
func (s *Segment) ResolveDeposit(offset, length uint64) (Deposit, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	return s.resolveDeposit(offset, length, false)
}

func (s *Segment) resolveDeposit(offset, length uint64, held bool) (Deposit, error) {
	if s.data == nil || (s.retiring.Load() && !held) {
		return Deposit{}, ErrRetired
	}
	if length == 0 {
		return Deposit{}, ErrBounds
	}
	for i := range s.regions {
		r := &s.regions[i]
		if offset < r.layout.Offset || !inRange(offset-r.layout.Offset, length, r.layout.Extent) {
			continue
		}
		if r.quarantined {
			return Deposit{}, ErrQuarantined
		}
		if r.layout.Discipline != Loaned || r.layout.Producer != Host {
			return Deposit{}, ErrDiscipline
		}
		return Deposit{s.id, r.layout.Index, offset, length}, nil
	}
	return Deposit{}, ErrBounds
}

// Copy consumes this local handoff once and returns the EXACT ReleaseDeposit
// fields to send reliably, after copying. No shared bytes escape. A small dst
// leaves the handoff unconsumed so it can be retried. On error no release is due
// from this call. Close abandons a failed/unread handoff and lets retirement settle it.
func (d *DepositRead) Copy(dst []byte) (Deposit, error) {
	h := d.h
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	if err := h.copy(dst, Loaned); err != nil {
		return Deposit{}, err
	}
	h.finish()
	return Deposit{h.s.id, h.index, h.offset, h.length}, nil
}

// AcceptOwned is the explicit control-plane ownership boundary. Call it once
// only when the request/reply actually hands this range to the plugin. The spec
// does not identify that exchange or its deduplication key; the embedding control
// plane must enforce it. It must retain ownership until Copy finishes and perform
// any required reply afterwards. Offset is REGION-relative; this is never a poll.
func (s *Segment) AcceptOwned(index uint32, offset, length uint64) (*OwnedRead, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, false)
	if err != nil {
		return nil, err
	}
	if r.layout.Discipline != Owned {
		return nil, ErrDiscipline
	}
	if length == 0 || !inRange(offset, length, r.layout.Extent) {
		return nil, ErrBounds
	}
	return &OwnedRead{&handoff{s: s, index: index, offset: r.layout.Offset + offset, length: length}}, nil
}
func (o *OwnedRead) Copy(dst []byte) error {
	h := o.h
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	return h.copy(dst, Owned)
}
func (h *handoff) copy(dst []byte, discipline Discipline) error {
	if h.consumed {
		return ErrConsumed
	}
	r, err := h.s.region(h.index, h.held)
	if err != nil {
		return err
	}
	if r.layout.Discipline != discipline {
		return ErrDiscipline
	}
	if h.length > uint64(len(dst)) {
		return ErrBounds
	}
	if h.s.depositCopy != nil {
		h.s.depositCopy(dst, h.s.data[h.offset:h.offset+h.length])
	} else {
		copy(dst, h.s.data[h.offset:h.offset+h.length])
	}
	h.consumed = true
	return nil
}

func (s *Segment) quarantine(r *regionState, where, detail string) error {
	r.quarantined = true
	return &Violation{where, detail, s.id, r.layout.Index}
}

// Hint interprets a D mask without reading payloads or claiming buffers. Pass the
// acknowledged epoch for n.ViewID (zero if unknown); epoch zero on n means no pane
// association. Out-of-range bits are counted and trigger a sweep of host-produced
// Pulsed regions only. Valid bits retain their normal diagnostics. Eligible regions
// signal active PulsedConsumers, which rate-limit scans without delaying polls.
// Hint does not quarantine Owned regions: legitimate handoffs remain usable.
func (s *Segment) Hint(n doorbell.Notification, acknowledgedEpoch uint32) HintPlan {
	s.mu.Lock()
	defer s.mu.Unlock()
	var p HintPlan
	if n.SegmentID != s.id {
		p.Diagnostics = append(p.Diagnostics, "unknown_segment")
		return p
	}
	if s.data == nil || s.retiring.Load() {
		p.Diagnostics = append(p.Diagnostics, "retired_segment")
		return p
	}
	if n.Epoch != 0 && n.Epoch != acknowledgedEpoch {
		code := "stale_epoch"
		if n.Epoch > acknowledgedEpoch {
			code = "future_epoch"
		}
		p.Diagnostics = append(p.Diagnostics, code)
		return p
	}
	if n.Mask == (doorbell.Mask{}) {
		p.Diagnostics = append(p.Diagnostics, "empty_mask")
		return p
	}
	sweep := false
	for i := len(s.regions); i < 512; i++ {
		if n.Mask.Has(uint16(i)) {
			sweep = true
			p.Diagnostics = append(p.Diagnostics, "out_of_range_region")
		}
	}
	for i := range s.regions {
		r := &s.regions[i]
		explicit := i < 512 && n.Mask.Has(uint16(i))
		if !explicit && !(sweep && r.layout.Discipline == Pulsed && r.layout.Producer == Host) {
			continue
		}
		if r.quarantined {
			p.Diagnostics = append(p.Diagnostics, "quarantined_region")
			continue
		}
		switch r.layout.Discipline {
		case Staged:
			p.Regions = append(p.Regions, uint32(i))
		case Constant:
			p.Diagnostics = append(p.Diagnostics, "hint_on_constant_region")
		case Pulsed:
			if r.layout.Producer == Host {
				p.Regions = append(p.Regions, uint32(i))
				if r.consumer != nil {
					r.consumer.hint()
				}
			} else {
				p.Diagnostics = append(p.Diagnostics, "hint_on_produced_region")
			}
		case Owned:
			p.Violations = append(p.Violations, Violation{"owned_region_hint", "Owned requires a control-plane handoff", s.id, uint32(i)})
		case Loaned:
			p.Diagnostics = append(p.Diagnostics, "hint_on_loaned_region")
		}
	}
	return p
}

// Close abandons an unread deposit. Copy closes its claim on success; callers
// must Close after a failed copy or when deciding not to read it.
func (d *DepositRead) Close() {
	h := d.h
	h.s.mu.Lock()
	defer h.s.mu.Unlock()
	h.finish()
	h.consumed = true
}
func (h *handoff) finish() {
	if h.held {
		h.held = false
		h.s.finishUse()
	}
}
