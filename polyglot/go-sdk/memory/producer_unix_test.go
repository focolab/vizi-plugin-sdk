//go:build (linux || darwin) && (amd64 || arm64)

package memory

import (
	"fmt"
	"runtime"
	"testing"
	"time"
)

func TestPluginProducedConstantAndOwnedGrantsInstall(t *testing.T) {
	for _, discipline := range []Discipline{Constant, Owned} {
		t.Run(fmt.Sprint(discipline), func(t *testing.T) {
			f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 64, Discipline: discipline, Producer: Plugin})
			r, _ := installFixture(t, f)
			if got := r.Status(testID); got.State != Installed {
				t.Fatal("known producer was refused", got)
			}
			// Replay must keep the same successful acknowledgement.
			if u, err := r.Install(f.in); err != nil || u.Ack == nil || u.Ack.Failure != nil {
				t.Fatal("installed grant replay failed", u, err)
			}
		})
	}
}

func TestRetirementWaitsForPublicationCommit(t *testing.T) {
	f, s := crossFixture(t, Plugin)
	entered, finish := make(chan struct{}), make(chan struct{})
	s.publishCopy = func(dst, src []byte) error { close(entered); <-finish; copy(dst, src); return nil }
	published := make(chan error, 1)
	go func() { _, err := s.PublishPulsed(0, []byte("committed")); published <- err }()
	<-entered
	retired := make(chan error, 1)
	go func() { retired <- s.release() }()
	deadline := time.Now().Add(time.Second)
	for !s.retiring.Load() {
		if time.Now().After(deadline) {
			close(finish)
			t.Fatal("retirement did not latch")
		}
		runtime.Gosched()
	}
	select {
	case err := <-retired:
		close(finish)
		t.Fatal("unmapped a held write claim", err)
	default:
	}
	close(finish)
	if err := <-published; err != nil {
		t.Fatal(err)
	}
	if err := <-retired; err != nil {
		t.Fatal(err)
	}
	if loadState(f.peer, 48) != 1 || string(f.peer[336:345]) != "committed" {
		t.Fatal("retirement lost the in-flight publish")
	}
	if _, err := s.PublishPulsed(0, []byte("late")); err != ErrRetired {
		t.Fatal(err)
	}
}

func TestRetirementSettlesOutstandingLoans(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 512, Discipline: Loaned, Producer: Plugin, SlotBytes: 64})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("loan"))
	if err != nil {
		t.Fatal(err)
	}
	if string(f.peer[32:36]) != "loan" {
		t.Fatal("loan is not shared")
	}
	// Retirement settles sent loans, but unpublished writes must first finish.
	sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
	if err != nil {
		t.Fatal(err)
	}
	sent()
	if _, err := r.Retire(testID); err != nil {
		t.Fatal(err)
	}
	if err := s.ReleaseLoan(key); err != ErrRetired {
		t.Fatal(err)
	}
	stats, _ := s.LoanStats(0)
	if stats.Outstanding != 0 {
		t.Fatal(stats)
	}
}
