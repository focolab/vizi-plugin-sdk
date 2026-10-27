package control

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"sync/atomic"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
)

// Memory connects the byte-access registry to control messages. Its Registry
// remains available for Constant, Pulsed and plugin-produced Loaned operations.
// Prepare/Release implement the server's descriptor hooks. Every descriptor use
// must pass through this adapter exactly once; do not also emit ReleaseDeposit.
type Memory struct {
	Registry            *memory.Registry
	mu                  sync.Mutex
	uses                map[reservationKey]*depositUses
	claims              map[*pb.HostMsg]*memory.DepositClaim
	sends               map[*pb.PluginMsg][]func()
	dropped             atomic.Uint64
	loanChanged         chan struct{}
	loanRetiring        map[uint64]bool
	droppedPublications atomic.Uint64
	crossedReleases     atomic.Uint64
	stageRetiring       map[uint64]struct{} // pending RetireSegment while a stage borrow drains
}

// ErrDescriptorDropped tells the dispatcher that a failed or retired grant
// settled a descriptor without a read. Host requests still receive an error.
var ErrDescriptorDropped = errors.New("descriptor dropped with its grant")

// DroppedDescriptors counts descriptors disposed after grant failure/retirement.
func (m *Memory) DroppedDescriptors() uint64 { return m.dropped.Load() }

// CrossedReleases counts matching, exactly-once ReleaseLoan messages received
// after this session released their segment. A match emits no diagnostic or
// violation; unknown keys and duplicate returns remain unknown_release.
func (m *Memory) CrossedReleases() uint64 { return m.crossedReleases.Load() }

type depositUses struct {
	declared, remaining uint32
	// Admission reports a mismatch once; disposition must not count it again.
	// Entries live only until this descriptor is disposed or its segment retires.
	admitted map[*pb.HostMsg]struct{}
}

// LoanChanges returns a broadcast closed after the next successfully applied
// host release. Capture it BEFORE attempting a reservation to avoid lost wakes.
// Notifications carry no ownership: callers retry their own arena allocation.
func (m *Memory) LoanChanges() <-chan struct{} {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.loanChanged == nil {
		m.loanChanged = make(chan struct{})
	}
	return m.loanChanged
}

func NewMemory(registry *memory.Registry) *Memory {
	return &Memory{Registry: registry, uses: make(map[reservationKey]*depositUses)}
}

func InstallFromProto(in *pb.InstallSegment) (memory.InstallSegment, error) {
	if in == nil {
		return memory.InstallSegment{}, errors.New("missing InstallSegment")
	}
	out := memory.InstallSegment{SegmentID: in.SegmentId, Purpose: in.Purpose, Subject: in.Subject, Supersedes: in.Supersedes}
	for _, r := range in.Regions {
		if r == nil {
			return out, errors.New("nil region")
		}
		out.Regions = append(out.Regions, memory.Region{Index: r.Index, Offset: r.Offset, Extent: r.Extent, Discipline: memory.Discipline(r.Discipline), Producer: memory.Producer(r.Producer), Buffers: r.Buffers, SlotBytes: r.SlotBytes, ID: r.Id, EpochOffset: r.EpochOffset})
	}
	return out, nil
}

func grantClass(c memory.FailureClass) pb.GrantFailureClass {
	switch c {
	case memory.Retryable:
		return pb.GrantFailureClass_GRANT_FAILURE_RETRYABLE
	case memory.TooLarge:
		return pb.GrantFailureClass_GRANT_FAILURE_TOO_LARGE
	case memory.Malformed:
		return pb.GrantFailureClass_GRANT_FAILURE_MALFORMED
	case memory.Unsupported:
		return pb.GrantFailureClass_GRANT_FAILURE_UNSUPPORTED
	default:
		return pb.GrantFailureClass_GRANT_FAILURE_FAILED
	}
}

func AckMessage(a *memory.Ack) *pb.PluginMsg {
	ack := &pb.GrantAck{SegmentId: a.SegmentID}
	if a.Failure != nil {
		ack.Outcome = &pb.GrantAck_Failed{Failed: &pb.GrantFailed{FailureClass: grantClass(a.Failure.Class), Detail: a.Failure.Detail}}
	} else {
		ack.Outcome = &pb.GrantAck_Installed{Installed: &pb.GrantInstalled{IdentityProof: append([]byte(nil), a.Proof[:]...)}}
	}
	return &pb.PluginMsg{Msg: &pb.PluginMsg_GrantAck{GrantAck: ack}}
}

func (m *Memory) sendUpdate(c *Client, u memory.Update, err error) error {
	if err != nil {
		return err
	}
	if u.Ack != nil {
		if err := c.Send(AckMessage(u.Ack)); err != nil {
			return err
		}
		if u.Ack.Failure == nil {
			if seg, err := m.Registry.Segment(u.Ack.SegmentID); err == nil && seg.Purpose() == memory.PurposePanel {
				c.Diagnose(c.flushPanelImages(seg.Subject()))
			}
		}
	}
	for _, id := range u.PendingReleases {
		m.deferLoanRetirement(c, id)
	}
	for _, id := range u.Released {
		m.mu.Lock()
		for key := range m.uses {
			if key.segment == id {
				delete(m.uses, key)
			}
		}
		m.mu.Unlock()
		if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_SegmentReleased{SegmentReleased: &pb.SegmentReleased{SegmentId: id}}}); err != nil {
			return err
		}
	}
	return nil
}

// Grant runs inside the doorbell callback, so the descriptor is claimed before
// the transport releases it. It sends only mapped/validated/applied outcomes.
func (m *Memory) Grant(c *Client, f *doorbell.Frame) error {
	var u memory.Update
	var err error
	if f != nil && f.Disposition == doorbell.Rejected {
		u, err = m.Registry.RejectGrant(f)
	} else {
		u, err = m.Registry.Grant(f)
	}
	return m.sendUpdate(c, u, err)
}

func (m *Memory) Install(c *Client, in *pb.InstallSegment) error {
	x, err := InstallFromProto(in)
	if err != nil {
		return err
	}
	u, err := m.Registry.Install(x)
	return m.sendUpdate(c, u, err)
}

// Apply handles segment arrangements, queries, retirements and incoming releases.
// Query-after-retirement returns Released without reviving the identity.
func (m *Memory) Apply(c *Client, in *pb.HostMsg) error {
	switch x := in.Msg.(type) {
	case *pb.HostMsg_InstallSegment:
		return m.Install(c, x.InstallSegment)
	case *pb.HostMsg_RetireSegment:
		id := x.RetireSegment.SegmentId
		m.mu.Lock()
		u, err := m.Registry.Retire(id)
		if errors.Is(err, memory.ErrStageActive) {
			if m.stageRetiring == nil {
				m.stageRetiring = map[uint64]struct{}{}
			}
			m.stageRetiring[id] = struct{}{}
			m.mu.Unlock()
			return nil
		}
		if errors.Is(err, memory.ErrLoanActive) {
			u.PendingReleases = []uint64{id}
			err = nil
		}
		if err == nil {
			delete(m.stageRetiring, id)
		}
		m.mu.Unlock()
		return m.sendUpdate(c, u, err)
	case *pb.HostMsg_ReleaseLoan:
		r := x.ReleaseLoan
		crossed, err := m.Registry.ApplyHostRelease(memory.Deposit{SegmentID: r.SegmentId, Region: r.Region, Offset: r.Offset, Length: r.Len})
		if err != nil {
			return err
		}
		if crossed {
			m.crossedReleases.Add(1)
			return nil
		}
		m.mu.Lock()
		if m.loanChanged != nil {
			close(m.loanChanged)
			m.loanChanged = nil
		}
		m.mu.Unlock()
		return nil
	case *pb.HostMsg_GrantStateQuery:
		id := x.GrantStateQuery.SegmentId
		state := m.Registry.Status(id)
		out := &pb.GrantState{SegmentId: id}
		switch state.State {
		case memory.NotPresent:
			out.State = pb.GrantStateKind_GRANT_STATE_NOT_PRESENT
		case memory.Pending:
			out.State = pb.GrantStateKind_GRANT_STATE_PENDING
			out.HaveCapability, out.HaveInstall = &state.HasGrant, &state.HasInstall
		case memory.Installed:
			out.State = pb.GrantStateKind_GRANT_STATE_INSTALLED
			out.IdentityProof = append([]byte(nil), state.Ack.Proof[:]...)
		case memory.FailedState:
			out.State = pb.GrantStateKind_GRANT_STATE_FAILED
			failure := grantClass(state.Ack.Failure.Class)
			out.FailureClass = &failure
			out.Detail = &state.Ack.Failure.Detail
		case memory.Released:
			out.State = pb.GrantStateKind_GRANT_STATE_RELEASED
		}
		return c.Send(&pb.PluginMsg{Id: in.Id, Msg: &pb.PluginMsg_GrantState{GrantState: out}})
	}
	return fmt.Errorf("not a memory operation: %s", kind(in.ProtoReflect()))
}

func descriptor(in *pb.HostMsg) (*pb.SegmentRef, *pb.InstallSegment) {
	if d := in.GetDataVolume(); d != nil {
		return d.Segment, d.Install
	}
	if d := in.GetMaskVoxels(); d != nil {
		return d.Segment, nil
	}
	if d := in.GetRoiInclusion(); d != nil {
		return d.Segment, nil
	}
	if d := in.GetExportChunk(); d != nil {
		return d.GetSegment(), d.Install
	}
	return nil, nil
}

func inlineExport(in *pb.HostMsg) *pb.ExportChunk {
	chunk := in.GetExportChunk()
	if _, ok := chunk.GetSource().(*pb.ExportChunk_Inline); ok {
		return chunk
	}
	return nil
}

// An inline payload survives an invalid ride-along (§8.12). Strip it before
// routing so an inline-only plugin needs no descriptor preparation hooks.
func (c *Client) discardInlineInstall(in *pb.HostMsg) error {
	if chunk := inlineExport(in); chunk != nil && chunk.Install != nil {
		if err := c.reportViolation(violations.InstallWithoutSegment, fmt.Sprintf("ExportChunk.install segment=%d without a segment source; ignoring install", chunk.Install.SegmentId)); err != nil {
			return err
		}
		chunk.Install = nil
	}
	return nil
}

// Prepare applies an embedded installation before accessing its descriptor,
// then waits without blocking routing for either missing installation half.
func (m *Memory) Prepare(ctx context.Context, c *Client, in *pb.HostMsg) error {
	if err := m.Admit(c, in); err != nil {
		return err
	}
	return m.WaitDescriptor(ctx, c, in)
}

// Admit applies embedded meaning synchronously in control-stream order.
// An inline export's invalid install is reported and removed from in.
func (m *Memory) Admit(c *Client, in *pb.HostMsg) error {
	if inlineExport(in) != nil {
		return c.discardInlineInstall(in)
	}
	d, install := descriptor(in)
	if d == nil {
		if install != nil {
			return &memory.Violation{Where: "install_without_segment", Detail: "ExportChunk.install without a segment", SegmentID: install.SegmentId}
		}
		return errors.New("missing SegmentRef")
	}
	if install != nil {
		if install.SegmentId != d.SegmentId {
			return errors.New("ride-along installation names a different segment")
		}
		if err := m.Install(c, install); err != nil {
			return err
		}
	}
	state := m.Registry.Status(d.SegmentId).State
	if state == memory.FailedState || state == memory.Released {
		return nil
	}
	m.mu.Lock()
	if m.claims == nil {
		m.claims = make(map[*pb.HostMsg]*memory.DepositClaim)
	}
	if m.claims[in] == nil {
		claim, err := m.Registry.ClaimDeposit(d.SegmentId)
		if err != nil {
			m.mu.Unlock()
			var failure *memory.GrantFailure
			if errors.Is(err, memory.ErrRetired) || errors.As(err, &failure) {
				return nil
			}
			return err
		}
		m.claims[in] = claim
	}
	key := reservationKey{d.SegmentId, d.Offset, d.Len}
	uses := max(d.Uses, 1)
	u := m.uses[key]
	if u == nil {
		u = &depositUses{declared: uses, remaining: uses}
		m.uses[key] = u
	}
	_, seen := u.admitted[in]
	if u.admitted == nil {
		u.admitted = make(map[*pb.HostMsg]struct{})
	}
	u.admitted[in] = struct{}{}
	declared := u.declared
	m.mu.Unlock()
	if !seen && declared != uses {
		return reportUses(c, key, declared, uses)
	}
	return nil
}

// WaitDescriptor is Prepare's waiting half, paired with Admit by ServeEnv.
func (m *Memory) WaitDescriptor(ctx context.Context, _ *Client, in *pb.HostMsg) error {
	if chunk := inlineExport(in); chunk != nil && chunk.Install == nil {
		return nil
	}
	d, _ := descriptor(in)
	if d == nil {
		return errors.New("missing SegmentRef")
	}
	s, err := m.Registry.Wait(ctx, d.SegmentId)
	if err != nil {
		var failure *memory.GrantFailure
		if errors.Is(err, memory.ErrRetired) || errors.As(err, &failure) {
			return fmt.Errorf("%w: segment %d: %w", ErrDescriptorDropped, d.SegmentId, err)
		}
		return err
	}
	if claim := m.claimFor(d); claim != nil {
		_, err = claim.Resolve(d.Offset, d.Len)
	} else {
		_, err = s.ResolveDeposit(d.Offset, d.Len)
	}
	if errors.Is(err, memory.ErrRetired) {
		return fmt.Errorf("%w: %w", ErrDescriptorDropped, err)
	}
	return err
}

// CopyDescriptor copies bytes while this message/reply still owns its use. It
// does not release the reservation: the server releases export uses after the
// handler returns; Call callers defer Reply.Close. Allocation is caller-owned.
func (m *Memory) CopyDescriptor(d *pb.SegmentRef, dst []byte) (err error) {
	defer func() {
		if d != nil && (errors.Is(err, memory.ErrRetired) || (errors.Is(err, memory.ErrNotInstalled) && m.Registry.Status(d.SegmentId).State == memory.Released)) {
			err = fmt.Errorf("%w: %w", ErrDescriptorDropped, memory.ErrRetired)
		}
	}()
	if d == nil {
		return errors.New("missing SegmentRef")
	}
	if claim := m.claimFor(d); claim != nil {
		return claim.Copy(d.Offset, d.Len, dst)
	}
	s, err := m.Registry.Segment(d.SegmentId)
	if err != nil {
		return err
	}
	key, err := s.ResolveDeposit(d.Offset, d.Len)
	if err != nil {
		return err
	}
	read, err := s.AcceptDeposit(key)
	if err != nil {
		return err
	}
	defer read.Close()
	_, err = read.Copy(dst)
	return err
}

// Release counts EVERY disposition, including late/unmatched replies and
// failed copies. Only the final declared use emits one ReleaseDeposit. Values
// 0 and 1 both mean one. A mismatch reports uses_disagreement; the first
// received count wins. Admission and disposition count each mismatch only once.
func (m *Memory) Release(c *Client, in *pb.HostMsg) (err error) {
	d, _ := descriptor(in)
	if d == nil {
		return nil
	}
	m.mu.Lock()
	claim := m.claims[in]
	delete(m.claims, in)
	m.mu.Unlock()
	if claim != nil {
		defer claim.Close()
	}
	defer func() {
		if errors.Is(err, memory.ErrRetired) || (errors.Is(err, memory.ErrNotInstalled) && m.Registry.Status(d.SegmentId).State == memory.Released) {
			m.dropped.Add(1)
			m.mu.Lock()
			delete(m.uses, reservationKey{d.SegmentId, d.Offset, d.Len})
			m.mu.Unlock()
			err = nil
		}
	}()
	state := m.Registry.Status(d.SegmentId)
	if state.State == memory.Released || state.State == memory.FailedState {
		m.dropped.Add(1)
		m.mu.Lock()
		delete(m.uses, reservationKey{d.SegmentId, d.Offset, d.Len})
		m.mu.Unlock()
		return nil
	} // retirement/failed install settled access
	s, err := m.Registry.Segment(d.SegmentId)
	if err != nil {
		return err
	}
	var key memory.Deposit
	if claim != nil {
		key, err = claim.Resolve(d.Offset, d.Len)
	} else {
		key, err = s.ResolveDeposit(d.Offset, d.Len)
	}
	if err != nil {
		return err
	}
	uses := d.Uses
	if uses == 0 {
		uses = 1
	}
	m.mu.Lock()
	identity := reservationKey{key.SegmentID, key.Offset, key.Length}
	u := m.uses[identity]
	if u == nil {
		u = &depositUses{declared: uses, remaining: uses}
		m.uses[identity] = u
	}
	_, admitted := u.admitted[in]
	delete(u.admitted, in)
	declared := u.declared
	u.remaining--
	last := u.remaining == 0
	if last {
		delete(m.uses, identity)
	}
	m.mu.Unlock()
	var reportErr error
	if !admitted && declared != uses {
		reportErr = reportUses(c, identity, declared, uses)
	}
	if !last {
		return reportErr
	}
	return errors.Join(reportErr, c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_ReleaseDeposit{ReleaseDeposit: &pb.ReleaseDeposit{SegmentId: key.SegmentID, Region: key.Region, Offset: key.Offset, Len: key.Length}}}))
}

func reportUses(c *Client, key reservationKey, declared, received uint32) error {
	return c.reportViolation(violations.UsesDisagreement, fmt.Sprintf("segment=%d offset=%d len=%d uses=%d disagrees with first uses=%d; retaining first count", key.segment, key.offset, key.length, received, declared))
}

// settleStageRetirements is called after borrowed stage work returns. Recording
// the pending retirement and this retry share mu, so a return racing Apply
// cannot lose the one-way RetireSegment's eventual SegmentReleased.
func (m *Memory) settleStageRetirements(c *Client) error {
	m.mu.Lock()
	var updates []memory.Update
	var errs []error
	for id := range m.stageRetiring {
		u, err := m.Registry.SettleRetirement(id)
		if errors.Is(err, memory.ErrStageActive) {
			continue
		}
		if err != nil {
			errs = append(errs, err)
			continue
		}
		delete(m.stageRetiring, id)
		updates = append(updates, u)
	}
	m.mu.Unlock()
	for _, u := range updates {
		if err := m.sendUpdate(c, u, nil); err != nil {
			errs = append(errs, err)
		}
	}
	return errors.Join(errs...)
}

// DroppedPublications counts pushes quietly discarded because their arena retired.
func (m *Memory) DroppedPublications() uint64 { return m.droppedPublications.Load() }

// DropRetiredPublication handles normal retirement without an error diagnostic.
// Invalid arguments, allocation pressure and other failures are not suppressed.
func (m *Memory) DropRetiredPublication(err error) bool {
	if !errors.Is(err, memory.ErrRetired) {
		return false
	}
	m.droppedPublications.Add(1)
	return true
}

func (m *Memory) deferLoanRetirement(c *Client, id uint64) {
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.loanRetiring[id] {
		return
	}
	seg, err := m.Registry.Segment(id)
	if err != nil {
		return
	} // A concurrent retry already released it.
	done := seg.LoansDrained()
	if m.loanRetiring == nil {
		m.loanRetiring = make(map[uint64]bool)
	}
	m.loanRetiring[id] = true
	go func() {
		select {
		case <-done:
		case <-c.Context().Done():
			return
		}
		m.mu.Lock()
		u, err := m.Registry.SettleRetirement(id)
		delete(m.loanRetiring, id)
		m.mu.Unlock()
		if c.Context().Err() == nil {
			if err := m.sendUpdate(c, u, err); err != nil {
				c.Fail(err)
			}
		}
	}()
}

// Callers retain Reply/message ownership during lookup and copy.
func (m *Memory) claimFor(d *pb.SegmentRef) *memory.DepositClaim {
	m.mu.Lock()
	defer m.mu.Unlock()
	for in, claim := range m.claims {
		ref, _ := descriptor(in)
		if ref == d {
			return claim
		}
	}
	return nil
}
