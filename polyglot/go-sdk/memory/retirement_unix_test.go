//go:build linux || darwin

package memory

import (
	"errors"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"hash/crc32"
	"runtime"
	"testing"
	"time"
)

func awaitRetiring(t *testing.T, s *Segment) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for !s.retiring.Load() {
		if time.Now().After(deadline) {
			t.Fatal("retirement did not latch")
		}
		runtime.Gosched()
	}
}

func TestOverlayRetirementPinsLoanCopyThroughSend(t *testing.T) {
	for _, rollback := range []bool{false, true} {
		t.Run(map[bool]string{false: "admit", true: "rollback"}[rollback], func(t *testing.T) {
			f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 512, Discipline: Loaned, Producer: Plugin})
			f.in.Purpose, f.in.Subject = PurposeOverlay, "o"
			r, err := New(Config{})
			if err != nil {
				t.Fatal(err)
			}
			t.Cleanup(func() { r.Close() })
			if _, err = f.send(t, r); err != nil {
				t.Fatal(err)
			}
			if u, err := r.Install(f.in); err != nil || u.Ack == nil || u.Ack.Failure != nil {
				t.Fatal(u, err)
			}
			s, _ := r.Segment(testID)
			entered, resume := make(chan struct{}), make(chan struct{})
			defer func() {
				select {
				case <-resume:
				default:
					close(resume)
				}
			}()
			s.loanCopy = func(dst, src []byte) { close(entered); <-resume; copy(dst, src) }
			written := make(chan Deposit, 1)
			writeErr := make(chan error, 1)
			go func() { d, err := s.WriteLoan(0, []byte("loan")); written <- d; writeErr <- err }()
			<-entered
			retired := make(chan error, 1)
			go func() { _, err := r.Retire(testID); retired <- err }()
			awaitRetiring(t, s)
			select {
			case err := <-retired:
				t.Fatal("retirement passed a parked copy", err)
			default:
			}
			close(resume)
			key := <-written
			if err := <-writeErr; err != nil {
				t.Fatal(err)
			}
			if err := <-retired; !errors.Is(err, ErrLoanActive) {
				t.Fatal(err)
			}
			drained := s.LoansDrained()
			select {
			case <-drained:
				t.Fatal("unpublished loan did not pin mapping")
			default:
			}
			if string(f.peer[32:36]) != "loan" {
				t.Fatal("copy lost")
			}
			if _, err := s.WriteLoan(0, []byte("late")); !errors.Is(err, ErrRetired) {
				t.Fatal(err)
			}
			var sent func()
			if rollback {
				err = s.ReleaseLoan(key)
			} else {
				sent, err = r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
			}
			if err != nil {
				t.Fatal(err)
			}
			if !rollback {
				select {
				case <-drained:
					t.Fatal("admission ended claim before send")
				default:
				}
				sent()
			}
			<-drained
			if u, err := r.Retire(testID); err != nil || len(u.Released) != 1 {
				t.Fatal(u, err)
			}
		})
	}
}

func TestRetirementWaitsForDepositCopy(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 512, Discipline: Loaned, Producer: Host})
	r, s := installFixture(t, f)
	copy(f.peer[32:36], []byte("data"))
	read, err := s.AcceptDeposit(Deposit{testID, 0, 32, 4})
	if err != nil {
		t.Fatal(err)
	}
	entered, resume := make(chan struct{}), make(chan struct{})
	defer func() {
		select {
		case <-resume:
		default:
			close(resume)
		}
	}()
	s.depositCopy = func(dst, src []byte) { close(entered); <-resume; copy(dst, src) }
	copied := make(chan error, 1)
	buf := make([]byte, 4)
	go func() { _, err := read.Copy(buf); copied <- err }()
	<-entered
	retired := make(chan error, 1)
	go func() { _, err := r.Retire(testID); retired <- err }()
	awaitRetiring(t, s)
	select {
	case err := <-retired:
		t.Fatal("unmapped a parked read", err)
	default:
	}
	close(resume)
	if err := <-copied; err != nil {
		t.Fatal(err)
	}
	if err := <-retired; err != nil {
		t.Fatal(err)
	}
	if string(buf) != "data" {
		t.Fatal(buf)
	}
}

func TestSupersessionAcknowledgesNewArenaWhileOldPublicationDrains(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 512, Discipline: Loaned, Producer: Plugin})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("old"))
	if err != nil {
		t.Fatal(err)
	}
	g := newFixture(t, f.in.Regions...)
	g.in.SegmentID = testID + 1
	g.in.Supersedes = testID
	g.grant.SegmentID = testID + 1
	le.PutUint64(g.peer, testID+1)
	le.PutUint32(g.peer[28:32], crc32.ChecksumIEEE(g.peer[:28]))
	g.raw, err = doorbell.EncodeMemory(g.grant, 18)
	if err != nil {
		t.Fatal(err)
	}
	if _, err = g.send(t, r); err != nil {
		t.Fatal(err)
	}
	u, err := r.Install(g.in)
	if err != nil || u.Ack == nil || u.Ack.Failure != nil || len(u.Released) != 0 || len(u.PendingReleases) != 1 || u.PendingReleases[0] != testID {
		t.Fatal(u, err)
	}
	next, err := r.PublicationSegment(testID)
	if err != nil || next.ID() != testID+1 {
		t.Fatal(next, err)
	}
	sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
	if err != nil {
		t.Fatal(err)
	}
	sent()
	if u, err := r.Retire(testID); err != nil || len(u.Released) != 1 {
		t.Fatal(u, err)
	}
	if _, err := r.Retire(testID + 1); err != nil {
		t.Fatal(err)
	}
	if _, err := r.PublicationSegment(testID); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
}

func TestRetirementWaitsForEveryLoanMessageSent(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 512, Discipline: Loaned, Producer: Plugin})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("shared"))
	if err != nil {
		t.Fatal(err)
	}
	refs := []LoanUse{{key.SegmentID, key.Offset, key.Length, 2}}
	first, err := r.AdmitLoans(refs)
	if err != nil {
		t.Fatal(err)
	}
	first()
	if _, err := r.Retire(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal("first send released a two-use loan", err)
	}
	second, err := r.AdmitLoans(refs)
	if err != nil {
		t.Fatal("begun reservation refused while retiring", err)
	}
	if _, err := r.SettleRetirement(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal("queue admission released mapping", err)
	}
	first() // duplicate completion cannot count as the second send
	if _, err := r.SettleRetirement(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal(err)
	}
	second()
	results := make(chan Update, 2)
	for range 2 {
		go func() {
			u, err := r.SettleRetirement(testID)
			if err != nil {
				t.Error(err)
			}
			results <- u
		}()
	}
	a, b := <-results, <-results
	if len(a.Released)+len(b.Released) != 1 {
		t.Fatal("settlement sent duplicate releases", a, b)
	}
}

func TestSendCompletionCannotEndReusedReservation(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 512, Discipline: Loaned, Producer: Plugin})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("same"))
	if err != nil {
		t.Fatal(err)
	}
	sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
	if err != nil {
		t.Fatal(err)
	}
	// The host may receive and return the loan before Send returns locally.
	if err := s.ApplyHostRelease(key); err != nil {
		t.Fatal(err)
	}
	reused, err := s.WriteLoan(0, []byte("same"))
	if err != nil || reused != key {
		t.Fatal(reused, err)
	}
	sent()
	if _, err := r.Retire(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal("old completion ended new claim", err)
	}
	if err := s.ReleaseLoan(reused); err != nil {
		t.Fatal(err)
	}
	if _, err := r.SettleRetirement(testID); err != nil {
		t.Fatal(err)
	}
}

func TestArrivedDepositPinsBeforeCopyAndAllowsRetirement(t *testing.T) {
	for _, beforeGrant := range []bool{false, true} {
		t.Run(map[bool]string{false: "installed", true: "before-grant"}[beforeGrant], func(t *testing.T) {
			f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 512, Discipline: Loaned, Producer: Host})
			r := registry(t)
			copy(f.peer[64:68], []byte("read"))
			if !beforeGrant {
				f.send(t, r)
				r.Install(f.in)
			}
			claim, err := r.ClaimDeposit(testID)
			if err != nil {
				t.Fatal(err)
			}
			defer claim.Close()
			if beforeGrant {
				f.send(t, r)
				r.Install(f.in)
			}
			if _, err := r.Retire(testID); !errors.Is(err, ErrLoanActive) {
				t.Fatal(err)
			}
			if _, err := r.ClaimDeposit(testID); !errors.Is(err, ErrRetired) {
				t.Fatal("new read started while retiring", err)
			}
			data := make([]byte, 4)
			if err := claim.Copy(64, 4, data); err != nil || string(data) != "read" {
				t.Fatal(data, err)
			}
			claim.Close()
			if u, err := r.SettleRetirement(testID); err != nil || len(u.Released) != 1 {
				t.Fatal(u, err)
			}
		})
	}
}

func TestRetiringLoanCanStillBeSliced(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 512, Discipline: Loaned, Producer: Plugin})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("abcd"))
	if err != nil {
		t.Fatal(err)
	}
	if _, err = r.Retire(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal(err)
	}
	if _, err = s.Arena(); err != nil {
		t.Fatal("cannot inspect existing arena", err)
	}
	key, err = s.SliceLoan(key, 2, 2)
	if err != nil || string(f.peer[64:66]) != "cd" {
		t.Fatal(key, err)
	}
	sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
	if err != nil {
		t.Fatal(err)
	}
	sent()
	if u, err := r.SettleRetirement(testID); err != nil || len(u.Released) != 1 {
		t.Fatal(u, err)
	}
}

func TestCloseAbandonsUnsentAndUnreadClaims(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 512, Discipline: Loaned, Producer: Plugin}, Region{Index: 1, Offset: 576, Extent: 512, Discipline: Loaned, Producer: Host})
	r, s := installFixture(t, f)
	key, err := s.WriteLoan(0, []byte("unsent"))
	if err != nil {
		t.Fatal(err)
	}
	sent, err := r.AdmitLoans([]LoanUse{{key.SegmentID, key.Offset, key.Length, 1}})
	if err != nil {
		t.Fatal(err)
	}
	claim, err := r.ClaimDeposit(testID)
	if err != nil {
		t.Fatal(err)
	}
	read, err := s.AcceptDeposit(Deposit{testID, 1, 576, 4})
	if err != nil {
		t.Fatal(err)
	}
	if _, err = r.Retire(testID); !errors.Is(err, ErrLoanActive) {
		t.Fatal(err)
	}
	// A failed transport cannot deliver the queued descriptor. Session Close
	// abandons its claim and any replies whose owners were canceled.
	if err = r.Close(); err != nil {
		t.Fatal(err)
	}
	if err = claim.Copy(576, 4, make([]byte, 4)); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
	if _, err = read.Copy(make([]byte, 4)); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
	claim.Close()
	read.Close()
	sent()
	sent()
	if s.loanPins != 0 || s.data != nil {
		t.Fatal("late cleanup revived mapping")
	}
}
