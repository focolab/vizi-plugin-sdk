package memory

import (
	"errors"
	"sync"
	"sync/atomic"
	"time"
)

// ConsumerConfig specifies receiver-owned scheduling policy, not wire constants.
// PollInterval defaults to 40 ms. HintInterval defaults to 1 ms and limits early
// checks induced by D. Both must be positive after defaults are applied.
type ConsumerConfig struct {
	PollInterval time.Duration
	HintInterval time.Duration
}

// PulsedUpdate contains an independent Go-owned copy or a terminal read error.
// Error may contain joined Violations; the supervisor must report them. No mapped
// bytes or claims are retained while the application handles an update.
type PulsedUpdate struct {
	Sample    Sample
	Data      []byte
	Error     error
	Pane      *PaneEpoch
	PaneError error // bad annotation; Data remains usable
}

type ConsumerStats struct {
	Checks    uint64
	Coalesced uint64 // unread mailbox entries superseded by a newer update
}

// PulsedConsumer owns an unconditional timer for one actively consumed region.
// The one-entry Updates mailbox keeps the latest copy, replacing an unread older
// update. A slow or absent receiver cannot block polling, other consumers, or
// retirement. A terminal corruption error is retained in the mailbox before it
// closes. Stop, retirement, session close or quarantine ends the schedule.
// Never copy a PulsedConsumer.
type PulsedConsumer struct {
	segment     *Segment
	index       uint32
	cfg         ConsumerConfig
	updates     chan PulsedUpdate
	wake        chan struct{}
	stop        chan struct{}
	done        chan struct{}
	segmentStop <-chan struct{}
	once        sync.Once
	checks      atomic.Uint64
	coalesced   atomic.Uint64
}

// ConsumePulsed begins active consumption, with an immediate check followed by a
// check every PollInterval independently of D and application activity. Hints can
// add checks but never postpone that deadline. The interval bounds scheduled
// checks; actual execution also depends on Go/OS scheduling and completion of a
// current segment operation (this is not a real-time runtime).
//
// One consumer may own a region at a time. Direct ReadPulsed calls and a second
// consumer fail with ErrConsumerActive until this consumer's Done closes. Merely
// installing a segment does not consume values an application has not requested.
func (s *Segment) ConsumePulsed(index uint32, cfg ConsumerConfig) (*PulsedConsumer, error) {
	if cfg.PollInterval == 0 {
		cfg.PollInterval = 40 * time.Millisecond
	}
	if cfg.HintInterval == 0 {
		cfg.HintInterval = time.Millisecond
	}
	if cfg.PollInterval < 0 || cfg.HintInterval < 0 {
		return nil, errors.New("Pulsed scheduling intervals must be positive")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	r, err := s.region(index, false)
	if err != nil {
		return nil, err
	}
	if r.layout.Discipline != Pulsed || r.layout.Producer != Host {
		return nil, ErrDiscipline
	}
	if r.consumer != nil {
		return nil, ErrConsumerActive
	}
	if s.consumerStop == nil {
		s.consumerStop = make(chan struct{})
	}
	c := &PulsedConsumer{
		segment: s, index: index, cfg: cfg,
		updates: make(chan PulsedUpdate, 1), wake: make(chan struct{}, 1),
		stop: make(chan struct{}), done: make(chan struct{}), segmentStop: s.consumerStop,
	}
	r.consumer = c
	go c.run(int(r.layout.SlotBytes))
	return c, nil
}

func (c *PulsedConsumer) Updates() <-chan PulsedUpdate { return c.updates }
func (c *PulsedConsumer) Done() <-chan struct{}        { return c.done }

// Stop ends active consumption without retiring the host's segment. It does not
// wait; Done closes after any in-flight copy/claim is finished and released.
func (c *PulsedConsumer) Stop() { c.once.Do(func() { close(c.stop) }) }
func (c *PulsedConsumer) Stats() ConsumerStats {
	return ConsumerStats{Checks: c.checks.Load(), Coalesced: c.coalesced.Load()}
}
func (c *PulsedConsumer) hint() {
	select {
	case c.wake <- struct{}{}:
	default:
	}
}
func (c *PulsedConsumer) deliver(update PulsedUpdate) {
	select {
	case c.updates <- update:
		return
	default:
	}
	// Only this goroutine produces updates. A racing receiver may have drained
	// the mailbox; either way there is space for the newest result afterwards.
	select {
	case <-c.updates:
		c.coalesced.Add(1)
	default:
	}
	c.updates <- update
}
func (c *PulsedConsumer) scan(buf []byte) bool {
	c.checks.Add(1)
	sample, err := c.segment.readPulsed(c.index, buf, c)
	if err != nil {
		if !errors.Is(err, ErrRetired) {
			c.deliver(PulsedUpdate{Error: err})
		}
		return false
	}
	if sample.Revision != 0 {
		layout := c.segment.Regions()[c.index]
		pair, pairErr := layout.ReadEpoch(buf, 0)
		c.deliver(PulsedUpdate{Sample: sample, Data: append([]byte(nil), buf[:sample.Length]...), Pane: pair, PaneError: pairErr})
	}
	return true
}
func (c *PulsedConsumer) run(slotBytes int) {
	defer func() {
		c.segment.mu.Lock()
		defer c.segment.mu.Unlock()
		c.segment.regions[c.index].consumer = nil
		close(c.updates)
		close(c.done)
	}()
	buf := make([]byte, slotBytes)
	nextPoll := time.Now() // immediate first check
	var nextHint time.Time
	dirty := false
	timer := time.NewTimer(time.Hour)
	defer timer.Stop()
	for {
		select {
		case <-c.stop:
			return
		case <-c.segmentStop:
			return
		default:
		}
		now := time.Now()
		// Check the independent deadline FIRST, so continuous hints cannot
		// starve the poll through select fairness or by resetting its timer.
		if !now.Before(nextPoll) {
			if !c.scan(buf) {
				return
			}
			nextPoll = now.Add(c.cfg.PollInterval)
			nextHint = now.Add(c.cfg.HintInterval)
			dirty = false
		} else if dirty && !now.Before(nextHint) {
			if !c.scan(buf) {
				return
			}
			nextHint = now.Add(c.cfg.HintInterval)
			dirty = false
		}
		deadline := nextPoll
		wake := c.wake
		if dirty {
			wake = nil // coalesce a flood without spinning on its wakeups
			if nextHint.Before(deadline) {
				deadline = nextHint
			}
		}
		timer.Reset(time.Until(deadline))
		select {
		case <-c.stop:
			return
		case <-c.segmentStop:
			return
		case <-timer.C:
		case <-wake:
			dirty = true
		}
	}
}
