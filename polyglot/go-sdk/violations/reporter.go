package violations

import (
	"context"
	"errors"
	"io"
	"math"
	"strings"
	"sync"
	"time"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

type category struct {
	counts     Counts
	seen       bool
	first      Report
	repeats    uint64
	detail     string
	lastReport time.Time
}

type group struct {
	count  uint64
	detail string
}

// Reporter belongs to one session, shared by ALL its detectors. It stores every
// known category in a fixed table, irrespective of output readiness or rate.
// No goroutine is started; Next owns the timer while the sender is waiting.
// Observation and Stats methods are safe concurrently with Next and Close.
type Reporter struct {
	mu         sync.Mutex
	config     Config
	states     [siteLimit]category
	first      []Site // at most one entry per ordinary category, first-seen order
	grants     []Report
	grantHead  int
	grantCount int
	repeatFrom Site
	nextReport time.Time
	closed     bool
	wake       chan struct{}
	reader     chan struct{}
}

func New(config Config) (*Reporter, error) {
	if config.ReportInterval < 0 || config.RepeatInterval < 0 || config.MaxGrantReports < 0 {
		return nil, errors.New("violation reporter intervals and grant capacity must be positive")
	}
	if config.ReportInterval == 0 {
		config.ReportInterval = 100 * time.Millisecond
	}
	if config.RepeatInterval == 0 {
		config.RepeatInterval = time.Second
	}
	if config.MaxGrantReports == 0 {
		config.MaxGrantReports = 64
	}
	return &Reporter{
		config: config, first: make([]Site, 0, int(siteLimit)-1),
		grants: make([]Report, config.MaxGrantReports), repeatFrom: 1,
		wake: make(chan struct{}, 1), reader: make(chan struct{}, 1),
	}, nil
}

// Observe records one recordless occurrence. Repeated calls are distinct
// occurrences, even with identical text. Use ObserveRecord for record batches.
func (r *Reporter) Observe(f Finding) error {
	if !f.Where.valid() {
		return ErrUnknownSite
	}
	var groups [siteLimit]group
	groups[f.Where] = group{1, bounded(f.Detail)}
	return r.observe(groups, nil)
}

// ObserveRecord admits one received record atomically. Multiple findings of the
// same rule count once and their details are combined within MaxDetailBytes.
// Two calls with identical bytes count twice: bytes and wrapping wire IDs are
// not reception identities. Submit a reception exactly once.
// M/zero_id is automatically kept as an individual report carrying the raw M.
func (r *Reporter) ObserveRecord(raw doorbell.RawRecord, findings ...Finding) error {
	var groups [siteLimit]group
	for _, f := range findings {
		if !f.Where.valid() {
			return ErrUnknownSite
		}
		g := &groups[f.Where]
		g.count = 1
		g.detail = combine(g.detail, f.Detail)
	}
	return r.observe(groups, &raw)
}

func (r *Reporter) observe(groups [siteLimit]group, raw *doorbell.RawRecord) error {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.closed {
		return ErrClosed
	}
	grant := raw != nil && raw[0] == doorbell.OpMemory && groups[ZeroID].count != 0
	if grant && r.grantCount == len(r.grants) {
		return ErrGrantQueueFull
	}
	// Validate the whole batch before admitting any part of it.
	for s := Site(1); s < siteLimit; s++ {
		if groups[s].count > math.MaxUint64-r.states[s].counts.Observed {
			return ErrCountOverflow
		}
	}
	wake := false
	for s := Site(1); s < siteLimit; s++ {
		g := groups[s]
		if g.count == 0 {
			continue
		}
		state := &r.states[s]
		state.counts.Observed += g.count
		if s == ZeroID && grant {
			wake = wake || r.grantCount == 0
			idx := (r.grantHead + r.grantCount) % len(r.grants)
			r.grants[idx] = Report{s, g.detail, copyRecord(raw), 1}
			r.grantCount++
			continue
		}
		if !state.seen {
			wake = true
			state.seen = true
			state.first = Report{s, g.detail, copyRecord(raw), 1}
			r.first = append(r.first, s)
			g.count-- // the first report always has count=1, even if delayed
		}
		wake = wake || (state.repeats == 0 && g.count != 0)
		state.repeats += g.count
		state.detail = g.detail
	}
	// Extra repeats cannot advance a deadline or alter novelty. Avoid waking
	// the waiting sender and rebuilding its timer at the detector's rate.
	if wake {
		r.signal()
	}
	return nil
}

// Next waits for rate capacity and returns one report, prioritizing never-emitted
// ordinary categories, then individual M/zero_id grants, then due repeats in
// round-robin order. Delayed first reports retain their original record; later
// occurrences remain pending for a repeat. A quiet session still flushes them.
//
// Call only when the reliable sender can accept the next message. Do not prefetch
// into an unbounded queue: priority applies until Next transfers ownership.
// Concurrent calls are serialized; cancellation while waiting consumes nothing.
// After Close, Next drains at the normal rate and then returns io.EOF.
func (r *Reporter) Next(ctx context.Context) (Report, error) {
	select {
	case r.reader <- struct{}{}:
		defer func() { <-r.reader }()
	case <-ctx.Done():
		return Report{}, ctx.Err()
	}
	for {
		r.mu.Lock()
		if err := ctx.Err(); err != nil {
			r.mu.Unlock()
			return Report{}, err
		}
		now := time.Now()
		report, ready, due, pending := r.take(now)
		closed := r.closed
		r.mu.Unlock()
		if ready {
			return report, nil
		}
		if closed && !pending {
			return Report{}, io.EOF
		}
		var timer *time.Timer
		var tick <-chan time.Time
		if !due.IsZero() {
			timer = time.NewTimer(time.Until(due))
			tick = timer.C
		}
		select {
		case <-ctx.Done():
		case <-r.wake:
		case <-tick:
		}
		if timer != nil {
			timer.Stop()
		}
	}
}

// take runs under mu. A rate deadline is never extended by observations.
func (r *Reporter) take(now time.Time) (Report, bool, time.Time, bool) {
	var report Report
	var due time.Time
	var repeat Site
	pending := len(r.first) > 0 || r.grantCount > 0
	if pending {
		due = r.nextReport
	} else {
		for i := Site(0); i < siteLimit-1; i++ {
			s := 1 + (r.repeatFrom-1+i)%(siteLimit-1)
			state := &r.states[s]
			if state.repeats == 0 {
				continue
			}
			at := state.lastReport.Add(r.config.RepeatInterval)
			if at.Before(r.nextReport) {
				at = r.nextReport
			}
			if !pending || at.Before(due) {
				due = at
			}
			pending = true
			if !at.After(now) && repeat == 0 {
				repeat = s
			}
		}
	}
	if !pending || due.After(now) {
		return Report{}, false, due, pending
	}
	switch {
	case len(r.first) > 0:
		s := r.first[0]
		r.first = r.first[1:]
		report = r.states[s].first
		r.states[s].first = Report{}
		r.states[s].lastReport = now
	case r.grantCount > 0:
		report = r.grants[r.grantHead]
		r.grants[r.grantHead] = Report{}
		r.grantHead = (r.grantHead + 1) % len(r.grants)
		r.grantCount--
	default:
		state := &r.states[repeat]
		n := min(state.repeats, uint64(math.MaxUint32))
		report = Report{Where: repeat, Detail: state.detail, Count: uint32(n)}
		state.repeats -= n
		state.lastReport = now
		r.repeatFrom = 1 + repeat%(siteLimit-1)
	}
	state := &r.states[report.Where]
	state.counts.Emitted += uint64(report.Count)
	state.counts.Reports++
	r.nextReport = now.Add(r.config.ReportInterval)
	return report, true, time.Time{}, true
}

// Stats returns all categories, including zero counts, as an independent snapshot.
func (r *Reporter) Stats() map[Site]Counts {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make(map[Site]Counts, int(siteLimit)-1)
	for s := Site(1); s < siteLimit; s++ {
		c := r.states[s].counts
		c.Pending = c.Observed - c.Emitted
		out[s] = c
	}
	return out
}

// Close stops new observations and wakes Next. Pending reports remain drainable
// without bypassing the rate limit. Cancel the sender context to bound teardown;
// Stats then exposes any undelivered occurrences. Close is idempotent.
func (r *Reporter) Close() {
	r.mu.Lock()
	r.closed = true
	r.signal()
	r.mu.Unlock()
}

func (r *Reporter) signal() {
	select {
	case r.wake <- struct{}{}:
	default:
	}
}

func copyRecord(raw *doorbell.RawRecord) *doorbell.RawRecord {
	if raw == nil {
		return nil
	}
	copy := *raw
	return &copy
}

func bounded(s string) string {
	// Slice before sanitizing/copying, so an arbitrary diagnostic cannot make
	// retained storage or normalization work proportional to its original size.
	if len(s) > MaxDetailBytes {
		s = s[:MaxDetailBytes-3]
		for len(s) > 0 && !utf8.ValidString(s) {
			// Invalid internal UTF-8 is normalized below; only trim a partial tail.
			_, size := utf8.DecodeLastRuneInString(s)
			if size != 1 || s[len(s)-1] < utf8.RuneSelf {
				break
			}
			s = s[:len(s)-1]
		}
		s += "..."
	}
	return strings.Clone(strings.ToValidUTF8(s, "?"))
}

func combine(a, b string) string {
	if a == "" {
		return bounded(b)
	}
	if b == "" {
		return a
	}
	return bounded(a + "; " + bounded(b))
}
