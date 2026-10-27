package memory

import (
	"context"
	"errors"
	"os"
	"reflect"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// Registry joins M and InstallSegment, saves replayable outcomes, and owns every
// mapping. Methods are concurrency-safe. Never copy a Registry or Segment.
// The registry has session lifetime: terminal identities are retained until Close.
type Registry struct {
	mu            sync.Mutex
	purposes      map[string]bool
	contributions map[string]map[string]bool
	maxSize       uint64
	entries       map[uint64]*entry
	closed        bool
	changed       chan struct{}
}
type entry struct {
	state            State
	raw              *doorbell.RawRecord
	grant            doorbell.Memory
	install          *InstallSegment
	segment          *Segment
	ack              *Ack
	releaseRequested bool
	depositPins      int
	crossedLoans     map[Deposit]struct{}
}

func New(cfg Config) (*Registry, error) {
	if cfg.Purposes == nil {
		cfg.Purposes = []string{PurposeReader, PurposeVolume, PurposeOverlay, PurposePanel, PurposeExport, PurposeStage}
	}
	purposes := make(map[string]bool, len(cfg.Purposes))
	for _, p := range cfg.Purposes {
		if p == "" {
			return nil, errors.New("empty purpose in configuration")
		}
		purposes[p] = true
	}
	var contributions map[string]map[string]bool
	if cfg.Contributions != nil {
		contributions = make(map[string]map[string]bool)
	}
	for purpose, ids := range cfg.Contributions {
		if purpose != PurposeOverlay && purpose != PurposePanel {
			return nil, errors.New("only O/P have contribution subjects")
		}
		contributions[purpose] = make(map[string]bool)
		for _, id := range ids {
			if id == "" || contributions[purpose][id] {
				return nil, errors.New("contributions require distinct nonempty IDs")
			}
			contributions[purpose][id] = true
		}
	}
	if cfg.MaxSegmentBytes == 0 {
		cfg.MaxSegmentBytes = 1 << 30
	}
	if cfg.MaxSegmentBytes < 32 || cfg.MaxSegmentBytes > uint64(^uint(0)>>1) {
		return nil, errors.New("MaxSegmentBytes must fit a Go slice and include the preamble")
	}
	return &Registry{purposes: purposes, contributions: contributions, maxSize: cfg.MaxSegmentBytes, entries: make(map[uint64]*entry), changed: make(chan struct{})}, nil
}
func cloneAck(a *Ack) *Ack {
	if a == nil {
		return nil
	}
	c := *a
	if a.Failure != nil {
		f := *a.Failure
		c.Failure = &f
	}
	return &c
}
func (r *Registry) get(id uint64) *entry {
	e := r.entries[id]
	if e == nil {
		e = &entry{state: Pending}
		r.entries[id] = e
	}
	return e
}
func (e *entry) update(id uint64) Update {
	if e.state == Released {
		return Update{Released: []uint64{id}}
	}
	return Update{Ack: cloneAck(e.ack)}
}

// Grant must run during the doorbell callback for a Decoded M. It claims the
// descriptor immediately; it is consumed on ALL outcomes, including duplicates
// and local errors. Rejected doorbell frames keep their transport reporting route
// and must not be passed here. Windows opens the existing named section.
func (r *Registry) Grant(frame *doorbell.Frame) (Update, error) {
	if frame == nil || frame.Disposition != doorbell.Decoded || frame.Memory == nil || !frame.HasRecord {
		return Update{}, errors.New("Grant requires a decoded doorbell M frame")
	}
	var file *os.File
	if frame.Descriptor != nil {
		var err error
		file, err = frame.Descriptor.Claim()
		if err != nil {
			return Update{}, err
		}
	}
	return r.grant(frame.Raw, *frame.Memory, file)
}

// RejectGrant retains a record-local malformed M outcome for replay and queries.
// The doorbell has already released its rejected capability. Zero-id records
// use ContractViolation instead and cannot enter this identity-keyed state.
func (r *Registry) RejectGrant(frame *doorbell.Frame) (Update, error) {
	if frame == nil || frame.Disposition != doorbell.Rejected || !frame.HasRecord || !frame.HasHeader || frame.Memory == nil || frame.Memory.SegmentID == 0 || frame.Violation == nil || frame.Violation.Route != "grant_ack" {
		return Update{}, errors.New("RejectGrant requires a malformed M with a nonzero identity")
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	if r.closed {
		return Update{}, ErrClosed
	}
	id := frame.Memory.SegmentID
	e := r.get(id)
	if e.state == Released {
		return e.update(id), nil
	}
	if e.raw != nil {
		if *e.raw != frame.Raw {
			return conflict(id), nil
		}
		return e.update(id), nil
	}
	raw := frame.Raw
	e.raw = &raw
	e.grant = *frame.Memory
	if e.state == FailedState {
		return e.update(id), nil
	}
	return r.fail(e, id, failure(Malformed, frame.Violation.Where))
}

// grant takes ownership of file. Tests can exercise real mappings without
// constructing the transport's intentionally opaque Descriptor capability.
func (r *Registry) grant(raw doorbell.RawRecord, g doorbell.Memory, file *os.File) (Update, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	if file != nil {
		defer func() {
			if file != nil {
				file.Close()
			}
		}()
	}
	if r.closed {
		return Update{}, ErrClosed
	}
	if g.SegmentID == 0 {
		return Update{}, errors.New("zero_id belongs to the doorbell ContractViolation route")
	}
	e := r.get(g.SegmentID)
	if e.state == Released {
		return e.update(g.SegmentID), nil
	}
	if e.raw != nil {
		if *e.raw != raw {
			return conflict(g.SegmentID), nil
		}
		return e.update(g.SegmentID), nil
	}
	e.raw = &raw
	e.grant = g
	if e.state == FailedState {
		return e.update(g.SegmentID), nil
	}
	if g.Size < 32 || g.Permissions&doorbell.PluginRead == 0 || g.Permissions&0xf0 != 0 {
		return r.fail(e, g.SegmentID, failure(Malformed, "invalid M size or permissions"))
	}
	if g.Size > r.maxSize {
		return r.fail(e, g.SegmentID, failure(TooLarge, "M.size exceeds MaxSegmentBytes"))
	}
	data, f := mapGrant(g, file)
	file = nil // mapGrant consumed it
	if f != nil {
		return r.fail(e, g.SegmentID, f)
	}
	e.segment = &Segment{id: g.SegmentID, data: data}
	proof, f := preamble(data, g.SegmentID)
	if f != nil {
		return r.fail(e, g.SegmentID, f)
	}
	e.segment.proof = proof
	return r.finish(e, g.SegmentID)
}

func conflict(id uint64) Update {
	return Update{Ack: &Ack{SegmentID: id, Failure: failure(Malformed, "conflicting grant for an existing segment identity")}}
}

// Install accepts the control-plane half in either order. The input is copied;
// callers may reuse their protobuf storage after this method returns. Comparison
// of repeats is field-by-field, including repeated-field order, never protobuf bytes.
func (r *Registry) Install(in InstallSegment) (Update, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	if r.closed {
		return Update{}, ErrClosed
	}
	if in.SegmentID == 0 {
		return Update{}, errors.New("InstallSegment requires a nonzero identity")
	}
	e := r.get(in.SegmentID)
	if e.state == Released {
		return e.update(in.SegmentID), nil
	}
	// Canonicalize nil versus empty repeated fields for semantic equality.
	in.Regions = append([]Region{}, in.Regions...)
	for i := range in.Regions {
		in.Regions[i] = cloneRegion(in.Regions[i])
	}
	if e.install != nil {
		if !reflect.DeepEqual(*e.install, in) {
			return conflict(in.SegmentID), nil
		}
		return e.update(in.SegmentID), nil
	}
	e.install = &in
	if e.state == FailedState {
		return e.update(in.SegmentID), nil
	}
	if f := validateInstall(in, r.purposes); f != nil {
		return r.fail(e, in.SegmentID, f)
	}
	if r.contributions != nil && (in.Purpose == PurposeOverlay || in.Purpose == PurposePanel) && !r.contributions[in.Purpose][in.Subject] {
		return r.fail(e, in.SegmentID, failure(Malformed, "subject does not name a declared contribution"))
	}
	return r.finish(e, in.SegmentID)
}

func (r *Registry) fail(e *entry, id uint64, f *GrantFailure) (Update, error) {
	if e.segment != nil {
		if err := e.segment.release(); err != nil {
			return Update{}, err
		}
		e.segment = nil
	}
	e.state = FailedState
	e.ack = &Ack{SegmentID: id, Failure: f}
	return e.update(id), nil
}

// ApplyHostRelease settles a live loan or exactly one outstanding loan crossing
// segment retirement (§9.3). The registry lock serializes lookup with retirement;
// the segment lock serializes the outstanding-key snapshot with live releases.
// The bool reports a settled crossing, for the session's diagnostic counter.
func (r *Registry) ApplyHostRelease(key Deposit) (bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return false, ErrClosed
	}
	e := r.entries[key.SegmentID]
	if e != nil && e.state == Installed {
		return false, e.segment.ApplyHostRelease(key)
	}
	if e != nil && e.state == Released {
		if _, ok := e.crossedLoans[key]; ok {
			delete(e.crossedLoans, key)
			return true, nil
		}
	}
	return false, &Violation{"unknown_release", "ReleaseLoan names no outstanding reservation", key.SegmentID, key.Region}
}
func (r *Registry) finish(e *entry, id uint64) (Update, error) {
	if e.install == nil || e.segment == nil {
		return Update{}, nil
	}
	if f := validateMapping(*e.install, e.grant); f != nil {
		return r.fail(e, id, f)
	}
	s := e.segment
	s.purpose = e.install.Purpose
	s.subject = e.install.Subject
	s.regions = make([]regionState, len(e.install.Regions))
	for _, region := range e.install.Regions {
		s.regions[region.Index].layout = cloneRegion(region)
	}
	for i := 0; i < e.depositPins; i++ {
		s.beginUse()
	}
	s.installed = true
	e.state = Installed
	e.ack = &Ack{SegmentID: id, Proof: s.proof}
	u := e.update(id)
	if old := e.install.Supersedes; old != 0 {
		retired, err := r.retire(old)
		u.Released = retired.Released
		if errors.Is(err, ErrLoanActive) {
			u.PendingReleases = []uint64{old}
			err = nil
		}
		return u, err
	}
	return u, nil
}

// Status returns local lifecycle state, retaining Released rather than pretending
// the identity was never present. All five states map to GrantState, including
// GRANT_STATE_RELEASED for a query crossing retirement (§9a). This method does
// not itself produce a wire response.
func (r *Registry) Status(id uint64) Status {
	r.mu.Lock()
	defer r.mu.Unlock()
	e := r.entries[id]
	if e == nil {
		return Status{State: NotPresent}
	}
	return Status{e.state, e.raw != nil, e.install != nil, cloneAck(e.ack)}
}
func (r *Registry) Segment(id uint64) (*Segment, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, ErrClosed
	}
	e := r.entries[id]
	if e == nil || e.state != Installed {
		return nil, ErrNotInstalled
	}
	return e.segment, nil
}

// Retire finishes synchronous reads before unmapping. Cached Segment handles
// refuse subsequent operations. ErrLoanActive/ErrStageActive defer release until
// publication/borrow completion; retry then. Unknown and released IDs replay.
func (r *Registry) Retire(id uint64) (Update, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	if r.closed {
		return Update{}, ErrClosed
	}
	if id == 0 {
		return Update{}, errors.New("cannot retire zero identity")
	}
	return r.retire(id)
}
func (r *Registry) retire(id uint64) (Update, error) {
	e := r.get(id)
	if e.segment != nil {
		if err := e.segment.release(); err != nil {
			return Update{}, err
		}
		e.crossedLoans = e.segment.releasedLoans
		e.segment = nil
	}
	e.state = Released
	e.ack = nil
	return Update{Released: []uint64{id}}, nil
}

// RequestRelease returns true once when the caller should send ReleaseRequest.
// It changes no access rights; descriptors arriving afterwards are still usable.
func (r *Registry) RequestRelease(id uint64) (bool, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return false, ErrClosed
	}
	e := r.entries[id]
	if e == nil || e.state != Installed {
		return false, ErrNotInstalled
	}
	if e.releaseRequested {
		return false, nil
	}
	e.releaseRequested = true
	return true, nil
}

// Close is session teardown ONLY, never unilateral segment retirement. No
// messages are owed at teardown. An unmap failure is returned and retained so a
// later Close can retry; no false SegmentReleased assertion is produced.
func (r *Registry) Close() error {
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	r.closed = true
	var errs []error
	for _, e := range r.entries {
		if e.segment != nil {
			e.segment.abandonStages.Store(true)
			if err := e.segment.release(); err != nil {
				errs = append(errs, err)
				continue
			}
			e.segment = nil
		}
	}
	if len(errs) == 0 {
		r.entries = make(map[uint64]*entry)
	}
	return errors.Join(errs...)
}

// signal is called with mu held. Waiters are bounded by the control-plane's
// admitted descriptors; no goroutine or timer is allocated per registry entry.
func (r *Registry) signal() { close(r.changed); r.changed = make(chan struct{}) }

// Wait joins the two halves without blocking the control-plane dispatcher. It
// returns the saved grant failure, retirement, closure, or caller cancellation.
// Bare and embedded descriptors both wait, including an identity for which
// neither half has arrived. The host owns the grant's escalation deadline.
func (r *Registry) Wait(ctx context.Context, id uint64) (*Segment, error) {
	for {
		r.mu.Lock()
		if r.closed {
			r.mu.Unlock()
			return nil, ErrClosed
		}
		e := r.entries[id]
		if e != nil {
			switch e.state {
			case Installed:
				s := e.segment
				r.mu.Unlock()
				return s, nil
			case FailedState:
				ack := cloneAck(e.ack)
				r.mu.Unlock()
				return nil, ack.Failure
			case Released:
				r.mu.Unlock()
				return nil, ErrRetired
			}
		}
		changed := r.changed
		r.mu.Unlock()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-changed:
		}
	}
}

// FindContribution finds an installed O/P segment by its authoritative subject.
// If multiple active grants match, the caller must select explicitly; this API
// does not invent an ordering rule. Superseded/retired grants are excluded.
func (r *Registry) FindContribution(purpose, subject string) (*Segment, error) {
	if (purpose != PurposeOverlay && purpose != PurposePanel) || subject == "" {
		return nil, errors.New("contribution lookup requires O/P and subject")
	}
	return r.find(purpose, subject)
}

// FindPurpose finds the sole active G, V or X segment. Unlike O/P these
// purposes have no contribution subject. It refuses ambiguity rather than
// guessing between concurrent active grants; callers with explicit descriptors
// should continue to use Segment. Retired/superseded grants are excluded.
func (r *Registry) FindPurpose(purpose string) (*Segment, error) {
	if purpose != PurposeReader && purpose != PurposeVolume && purpose != PurposeExport {
		return nil, errors.New("purpose lookup requires G/V/X; use FindContribution for O/P")
	}
	return r.find(purpose, "")
}

func (r *Registry) find(purpose, subject string) (*Segment, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, ErrClosed
	}
	var found *Segment
	for _, e := range r.entries {
		if e.state == Installed && e.segment.purpose == purpose && e.segment.subject == subject && !e.segment.retiring.Load() {
			if found != nil {
				return nil, errors.New("multiple installed segments match contribution; select segment explicitly")
			}
			found = e.segment
		}
	}
	if found == nil {
		if purpose == PurposePanel || purpose == PurposeOverlay {
			for _, e := range r.entries {
				if e.install != nil && e.install.Purpose == purpose && e.install.Subject == subject && (e.state == Released || (e.segment != nil && e.segment.retiring.Load())) {
					return nil, ErrRetired
				}
			}
		}
		return nil, ErrNotInstalled
	}
	return found, nil
}

// PublicationSegment follows explicit supersedes links when a selected arena
// retires. Unknown identities remain errors; retirement without a successor is
// ErrRetired, which push helpers count and quietly discard.
func (r *Registry) PublicationSegment(id uint64) (*Segment, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, ErrClosed
	}
	seen := make(map[uint64]bool)
	for !seen[id] {
		seen[id] = true
		e := r.entries[id]
		if e == nil {
			return nil, ErrNotInstalled
		}
		if e.state == Installed && !e.segment.retiring.Load() {
			return e.segment, nil
		}
		if e.state != Released && (e.segment == nil || !e.segment.retiring.Load()) {
			return nil, ErrNotInstalled
		}
		next := uint64(0)
		for candidate, successor := range r.entries {
			if successor.install != nil && successor.install.Supersedes == id && (successor.state == Installed || successor.state == Released) {
				if next != 0 {
					return nil, errors.New("multiple successors for publication segment")
				}
				next = candidate
			}
		}
		if next == 0 {
			return nil, ErrRetired
		}
		id = next
	}
	return nil, errors.New("cyclic publication supersedes chain")
}

// SettleRetirement retries a deferred retirement without replaying a release
// another caller already completed. Explicit Retire still replays terminal IDs.
func (r *Registry) SettleRetirement(id uint64) (Update, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	defer r.signal()
	if r.closed {
		return Update{}, ErrClosed
	}
	e := r.entries[id]
	if e == nil || e.state == Released {
		return Update{}, nil
	}
	return r.retire(id)
}
