//go:build (linux || darwin) && (amd64 || arm64)

package memory

import (
	"errors"
	"runtime"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

func takeUpdate(t *testing.T, c *PulsedConsumer) PulsedUpdate {
	t.Helper()
	select {
	case u, ok := <-c.Updates():
		if !ok {
			t.Fatal("consumer closed without expected update")
		}
		return u
	default:
		t.Fatal("missing scheduled update")
	}
	return PulsedUpdate{}
}

func TestConsumerPollsQuietSessionWithoutHints(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newFixture(t, pulsedRegion())
		_, s := installFixture(t, f)
		c, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Stop()
		synctest.Wait()
		if c.Stats().Checks != 1 {
			t.Fatal("missing immediate initial check")
		}
		// The writer publishes its final value after the first scan, then goes
		// quiet. No D and no other traffic ever arrive.
		publishFixture(f, 0, 1, []byte("last value"))
		time.Sleep(39 * time.Millisecond)
		synctest.Wait()
		if c.Stats().Checks != 1 {
			t.Fatal("unexpected early scan without a hint")
		}
		time.Sleep(time.Millisecond)
		synctest.Wait()
		u := takeUpdate(t, c)
		if u.Error != nil || u.Sample.Revision != 1 || string(u.Data) != "last value" {
			t.Fatalf("%+v", u)
		}
		if loadState(f.peer, 128) != 0 {
			t.Fatal("scheduled copy leaked a claim")
		}
		time.Sleep(40 * time.Millisecond)
		synctest.Wait()
		if c.Stats().Checks != 3 {
			t.Fatal("quiet poll did not continue")
		}
	})
}

func TestConsumerHintsCannotPostponePollAndAreRateLimited(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newFixture(t, pulsedRegion())
		_, s := installFixture(t, f)
		c, err := s.ConsumePulsed(0, ConsumerConfig{HintInterval: 30 * time.Millisecond})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Stop()
		synctest.Wait()
		n := doorbell.Notification{SegmentID: testID}
		n.Mask.Set(0)
		for i := 0; i < 10000; i++ {
			s.Hint(n, 0)
		}
		synctest.Wait()
		if c.Stats().Checks != 1 {
			t.Fatal("hint flood bypassed receiver rate limit")
		}
		time.Sleep(30 * time.Millisecond)
		synctest.Wait()
		if c.Stats().Checks != 2 {
			t.Fatal("queued hint did not accelerate a scan")
		}
		publishFixture(f, 0, 1, []byte("no more hints"))
		time.Sleep(10 * time.Millisecond)
		synctest.Wait()
		if c.Stats().Checks != 3 {
			t.Fatal("hint at 30ms postponed unconditional 40ms poll")
		}
		if u := takeUpdate(t, c); u.Sample.Revision != 1 {
			t.Fatalf("%+v", u)
		}
	})
}

func TestConsumerSweepAcceleratesEveryPeerLane(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		second := pulsedRegion()
		second.Index = 1
		second.Offset = 512
		f := newFixture(t, pulsedRegion(), second)
		_, s := installFixture(t, f)
		first, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer first.Stop()
		other, err := s.ConsumePulsed(1, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer other.Stop()
		synctest.Wait()
		n := doorbell.Notification{SegmentID: testID}
		n.Mask.Set(511)
		// Exercise the same bounded hint path under a flood of stray bits.
		for i := 0; i < 1000; i++ {
			s.Hint(n, 0)
		}
		synctest.Wait()
		if first.Stats().Checks != 1 || other.Stats().Checks != 1 {
			t.Fatal("sweep bypassed hint rate limit")
		}
		time.Sleep(time.Millisecond)
		synctest.Wait()
		if first.Stats().Checks != 2 || other.Stats().Checks != 2 {
			t.Fatal("sweep failed to accelerate every active peer lane")
		}
		time.Sleep(39 * time.Millisecond)
		synctest.Wait()
		if first.Stats().Checks < 3 || other.Stats().Checks < 3 {
			t.Fatal("sweep postponed unconditional polls")
		}
	})
}

func TestConsumerSlowReceiverCannotStopPolling(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newFixture(t, pulsedRegion())
		r, s := installFixture(t, f)
		c, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Stop()
		synctest.Wait()
		for revision := uint64(1); revision <= 3; revision++ {
			publishFixture(f, 0, revision, []byte{byte(revision)})
			time.Sleep(40 * time.Millisecond)
			synctest.Wait()
		}
		if stats := c.Stats(); stats.Checks != 4 || stats.Coalesced != 2 {
			t.Fatalf("%+v", stats)
		}
		u := takeUpdate(t, c)
		if u.Sample.Revision != 3 || len(u.Data) != 1 || u.Data[0] != 3 {
			t.Fatalf("%+v", u)
		}
		if _, err := r.Retire(testID); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		select {
		case <-c.Done():
		default:
			t.Fatal("retirement left poller alive")
		}
		if u.Data[0] != 3 {
			t.Fatal("retirement invalidated copied update")
		}
	})
}

func TestConsumerExclusiveOwnershipStopAndClose(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newFixture(t, pulsedRegion())
		r, s := installFixture(t, f)
		if _, err := s.ConsumePulsed(0, ConsumerConfig{PollInterval: -1}); err == nil {
			t.Fatal("negative interval accepted")
		}
		c, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Stop()
		synctest.Wait()
		if _, err := s.ConsumePulsed(0, ConsumerConfig{}); !errors.Is(err, ErrConsumerActive) {
			t.Fatal(err)
		}
		if _, err := s.ReadPulsed(0, make([]byte, 32)); !errors.Is(err, ErrConsumerActive) {
			t.Fatal(err)
		}
		c.Stop()
		c.Stop()
		synctest.Wait()
		checks := c.Stats().Checks
		time.Sleep(time.Second)
		synctest.Wait()
		if c.Stats().Checks != checks {
			t.Fatal("stopped consumer kept polling")
		}
		c2, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer c2.Stop()
		synctest.Wait()
		if err := r.Close(); err != nil {
			t.Fatal(err)
		}
		synctest.Wait()
		select {
		case <-c2.Done():
		default:
			t.Fatal("session close left poller alive")
		}
	})
}

func TestConsumerQuarantineRetainsTerminalViolation(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		f := newFixture(t, pulsedRegion())
		_, s := installFixture(t, f)
		publishFixture(f, 0, 1, []byte("unread"))
		c, err := s.ConsumePulsed(0, ConsumerConfig{})
		if err != nil {
			t.Fatal(err)
		}
		defer c.Stop()
		synctest.Wait()
		storeState(f.peer, 128, writeClaim)
		time.Sleep(40 * time.Millisecond)
		synctest.Wait()
		select {
		case <-c.Done():
		default:
			t.Fatal("quarantined consumer still scheduled")
		}
		u := takeUpdate(t, c)
		assertViolation(t, u.Error, "impossible_state_word")
		if _, ok := <-c.Updates(); ok {
			t.Fatal("terminal mailbox not closed")
		}
		if loadState(f.peer, 128) != writeClaim {
			t.Fatal("cleared corrupt state")
		}
	})
}

func TestConsumerRetirementFinishesHeldClaim(t *testing.T) {
	// A goroutine waiting on a mutex is not durably blocked for synctest.Wait;
	// use explicit claim/release synchronisation for this lifetime test instead.
	f := newFixture(t, pulsedRegion())
	r, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("held"))
	held, resume := make(chan struct{}), make(chan struct{})
	var once sync.Once
	unblock := func() { once.Do(func() { close(resume) }) }
	defer unblock()
	s.cas = func(b []byte, offset, old, next uint64) bool {
		ok := casState(b, offset, old, next)
		if ok && next&readClaim != 0 {
			close(held)
			<-resume
		}
		return ok
	}
	c, err := s.ConsumePulsed(0, ConsumerConfig{})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Stop()
	<-held
	retired := make(chan Update, 1)
	go func() {
		u, err := r.Retire(testID)
		if err != nil {
			t.Error(err)
		}
		retired <- u
	}()
	deadline := time.Now().Add(time.Second)
	for !s.retiring.Load() && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if !s.retiring.Load() {
		t.Fatal("retirement did not latch")
	}
	select {
	case <-retired:
		t.Fatal("released before finishing claim")
	default:
	}
	unblock()
	if u := <-retired; len(u.Released) != 1 {
		t.Fatalf("%+v", u)
	}
	if loadState(f.peer, 128) != 0 {
		t.Fatal("retirement abandoned scheduled read claim")
	}
	select {
	case <-c.Done():
	case <-time.After(time.Second):
		t.Fatal("consumer did not stop")
	}
	if u := takeUpdate(t, c); string(u.Data) != "held" {
		t.Fatalf("%+v", u)
	}
}
