//go:build (linux || darwin || windows) && (amd64 || arm64)

package memory

import (
	"errors"
	"sync"
	"sync/atomic"
	"testing"
)

func TestLoanUsesAtomicAcrossReservationsAndReclaim(t *testing.T) {
	a, b := producerSegment(), producerSegment()
	b.id++
	r := &Registry{entries: map[uint64]*entry{a.id: {state: Installed, segment: a}, b.id: {state: Installed, segment: b}}}
	ka, _ := a.WriteLoan(1, []byte("a"))
	kb, _ := b.WriteLoan(1, []byte("b"))
	ua := LoanUse{ka.SegmentID, ka.Offset, ka.Length, 2}
	ub := LoanUse{kb.SegmentID, kb.Offset, kb.Length, 1}
	if err := r.NameLoans([]LoanUse{ub}); err != nil {
		t.Fatal(err)
	}
	if err := r.NameLoans([]LoanUse{ua, ub}); !errors.Is(err, ErrUsesExceeded) {
		t.Fatal(err)
	}
	if stats, _ := a.LoanUseStats(1); stats.Named != 0 {
		t.Fatal("partial commit", stats)
	}
	if err := r.NameLoans([]LoanUse{ua}); err != nil {
		t.Fatal(err)
	}
	changed := ua
	changed.Uses = 3
	if err := r.NameLoans([]LoanUse{changed}); !errors.Is(err, ErrUsesDisagreement) {
		t.Fatal(err)
	}
	if err := a.ReleaseLoan(ka); !errors.Is(err, ErrLoanAdmitted) {
		t.Fatal(err)
	}
	if stats, _ := a.LoanUseStats(1); stats.Named != 1 || stats.PendingUses != 1 || stats.Stranded != 1 {
		t.Fatal(stats)
	}
	if err := r.NameLoans([]LoanUse{ua}); err != nil {
		t.Fatal(err)
	}
	if err := r.NameLoans([]LoanUse{ua}); !errors.Is(err, ErrUsesExceeded) {
		t.Fatal(err)
	}
	if err := a.ApplyHostRelease(ka); err != nil {
		t.Fatal(err)
	}
	if stats, _ := a.LoanUseStats(1); stats != (LoanUseStats{}) {
		t.Fatal(stats)
	}
	// Reusing the same arena range starts a new reservation, with no old count.
	ka2, _ := a.WriteLoan(1, []byte("a"))
	if ka2 != ka {
		t.Fatal(ka2, ka)
	}
	ua.Uses = 0
	if err := r.NameLoans([]LoanUse{ua}); err != nil {
		t.Fatal(err)
	}
	ua.Uses = 1
	if err := r.NameLoans([]LoanUse{ua}); !errors.Is(err, ErrUsesExceeded) {
		t.Fatal("0 and 1 must both declare one", err)
	}
}

func TestConcurrentLoanNamingsNeverExceedDeclaration(t *testing.T) {
	s := producerSegment()
	r := &Registry{entries: map[uint64]*entry{s.id: {state: Installed, segment: s}}}
	key, _ := s.WriteLoan(1, []byte("loan"))
	ref := LoanUse{key.SegmentID, key.Offset, key.Length, 2}
	var admitted atomic.Int32
	var wg sync.WaitGroup
	for range 20 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if err := r.NameLoans([]LoanUse{ref}); err == nil {
				admitted.Add(1)
			} else if !errors.Is(err, ErrUsesExceeded) {
				t.Error(err)
			}
		}()
	}
	wg.Wait()
	if admitted.Load() != 2 {
		t.Fatal(admitted.Load())
	}
}

func TestStrandedReservationDropsClaimAfterAdmittedSends(t *testing.T) {
	for _, sentFirst := range []bool{false, true} {
		s := producerSegment()
		r := &Registry{entries: map[uint64]*entry{s.id: {state: Installed, segment: s}}}
		key, err := s.WriteLoan(1, []byte("loan"))
		if err != nil {
			t.Fatal(err)
		}
		sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 2}})
		if err != nil {
			t.Fatal(err)
		}
		if sentFirst {
			sent()
		}
		if err := s.ReleaseLoan(key); !errors.Is(err, ErrLoanAdmitted) {
			t.Fatal(err)
		}
		if !sentFirst {
			select {
			case <-s.LoansDrained():
				t.Fatal("admitted send lost its claim")
			default:
			}
			sent()
		}
		// Capturing after the final claim drops must return an already-closed
		// channel, including the check-to-wait window in deferred retirement.
		select {
		case <-s.LoansDrained():
		default:
			t.Fatal("stranded reservation pinned retirement")
		}
		if stats, _ := s.LoanStats(1); stats.Outstanding != 1 {
			t.Fatal("host-owned bytes reclaimed", stats)
		}
	}
}
