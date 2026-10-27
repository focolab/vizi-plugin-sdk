package memory

import (
	"errors"
	"fmt"
	"sync"
)

var ErrUsesExceeded = errors.New("reservation has no remaining declared uses")
var ErrUsesDisagreement = errors.New("reservation use count differs from its first admission")
var ErrLoanAdmitted = errors.New("admitted reservation can only be released by the host or retirement")

type loanUse struct {
	pending               bool // pins mapping until all declared messages are sent or release
	declared, named, sent uint32
	stranded              bool // local reclaim attempted after admission
}

// LoanUse names a mapping-relative descriptor at outbound admission. Uses 0
// and 1 both declare one message. ImageRef always declares one.
type LoanUse struct {
	SegmentID, Offset, Length uint64
	Uses                      uint32
}

// NameLoans atomically checks and counts one message's plugin-produced loans.
// The caller must have reserved queue capacity and must enqueue on success.
// A failure changes no counts. Host-produced references are not sender loans.
// Registry then segment lock order matches retirement; no callbacks run here.
// Prefer AdmitLoans in a sender so its returned completion can end the claim
// after transport Send. NameLoans alone retains it until host release or Close.
func (r *Registry) NameLoans(refs []LoanUse) error {
	_, err := r.AdmitLoans(refs)
	return err
}

// AdmitLoans atomically admits one message and returns its idempotent send
// completion. Call completion only after successful Send. It captures reservation
// identity, so a racing host release and reuse of the same range cannot complete
// a newer loan. A failed transport abandons remaining claims at session Close.
func (r *Registry) AdmitLoans(refs []LoanUse) (func(), error) {
	if len(refs) == 0 {
		return func() {}, nil
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return nil, ErrClosed
	}
	segments := make(map[uint64]*Segment)
	defer func() {
		for _, s := range segments {
			s.mu.Unlock()
		}
	}()
	for _, ref := range refs {
		if segments[ref.SegmentID] != nil {
			continue
		}
		e := r.entries[ref.SegmentID]
		if e == nil || e.state != Installed {
			return nil, ErrNotInstalled
		}
		s := e.segment
		s.mu.Lock()
		segments[ref.SegmentID] = s
	}
	changes := make(map[*loanUse]uint32)
	owners := make(map[*loanUse]*Segment)
	for _, ref := range refs {
		s := segments[ref.SegmentID]
		if s.data == nil {
			return nil, ErrRetired
		}
		found := false
		for i := range s.regions {
			region := &s.regions[i]
			layout := region.layout
			if ref.Offset < layout.Offset || !inRange(ref.Offset-layout.Offset, ref.Length, layout.Extent) {
				continue
			}
			found = true
			if region.quarantined {
				return nil, ErrQuarantined
			}
			if layout.Producer != Plugin {
				if s.retiring.Load() {
					return nil, ErrRetired
				}
				break
			}
			if layout.Discipline != Loaned {
				return nil, ErrDiscipline
			}
			key := Deposit{s.id, layout.Index, ref.Offset, ref.Length}
			loan := region.loans[key]
			if loan == nil {
				return nil, fmt.Errorf("segment %d offset %d length %d has no reservation", s.id, ref.Offset, ref.Length)
			}
			if s.retiring.Load() && !loan.pending {
				return nil, ErrRetired
			}
			if loan.stranded && s.retiring.Load() {
				return nil, ErrRetired
			}
			uses := max(ref.Uses, 1)
			if loan.named > 0 && loan.declared != uses {
				return nil, ErrUsesDisagreement
			}
			if loan.named >= uses {
				return nil, ErrUsesExceeded
			}
			if _, duplicate := changes[loan]; duplicate {
				return nil, errors.New("message repeats a loan reservation")
			}
			changes[loan] = uses
			owners[loan] = s
			break
		}
		if !found {
			return nil, ErrBounds
		}
	}
	for loan, uses := range changes {
		if !loan.pending {
			owners[loan].beginUse()
			loan.pending = true
		}
		loan.stranded = false
		loan.declared = uses
		loan.named++
	}
	var once sync.Once
	return func() {
		once.Do(func() {
			for loan, s := range owners {
				s.mu.Lock()
				loan.sent++
				if loan.pending && (loan.sent == loan.declared || (loan.stranded && loan.sent == loan.named)) {
					s.finishLoan(loan)
				}
				s.mu.Unlock()
			}
		})
	}, nil
}

// LoanUseStats describes currently outstanding loans. Stranded counts loans
// whose local reclaim was refused after admission; retirement still frees them.
type LoanUseStats struct {
	Named, PendingUses, Stranded uint64
}

func (s *Segment) LoanUseStats(index uint32) (LoanUseStats, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if uint64(index) >= uint64(len(s.regions)) {
		return LoanUseStats{}, ErrBounds
	}
	r := &s.regions[index]
	if r.layout.Discipline != Loaned || r.layout.Producer != Plugin {
		return LoanUseStats{}, ErrDiscipline
	}
	var stats LoanUseStats
	for _, loan := range r.loans {
		stats.Named += uint64(loan.named)
		stats.PendingUses += uint64(loan.declared - loan.named)
		if loan.stranded {
			stats.Stranded++
		}
	}
	return stats, nil
}
