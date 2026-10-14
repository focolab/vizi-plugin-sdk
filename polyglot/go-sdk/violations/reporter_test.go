package violations

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math"
	"os"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"testing"
	"testing/synctest"
	"time"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
)

func newReporter(t *testing.T, config Config) *Reporter {
	t.Helper()
	r, err := New(config)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func admit(t *testing.T, err error) {
	t.Helper()
	if err != nil {
		t.Fatal(err)
	}
}

func next(t *testing.T, r *Reporter) Report {
	t.Helper()
	p, err := r.Next(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return p
}

func TestNovelBeforeRepeatsAndQuietFlush(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{ReportInterval: time.Second, RepeatInterval: time.Second})
		raw := doorbell.RawRecord{doorbell.OpInput, 2, 3}
		admit(t, r.ObserveRecord(raw, Finding{ZeroID, "first"}))
		first := next(t, r)
		if first.Count != 1 || first.Record == nil || *first.Record != raw {
			t.Fatalf("first: %+v", first)
		}
		admit(t, r.ObserveRecord(raw, Finding{ZeroID, "repeat"}))
		// A pending Next wakes for a new category, but cannot spend another
		// token early. At the deadline novelty beats the older repeat.
		out := make(chan Report, 1)
		go func() { out <- next(t, r) }()
		synctest.Wait()
		time.Sleep(100 * time.Millisecond)
		admit(t, r.ObserveRecord(raw, Finding{UnknownTool, "novel original"}))
		for i := 0; i < 100; i++ {
			admit(t, r.ObserveRecord(raw, Finding{UnknownTool, "novel repeat"}))
		}
		raw[2] = 99 // observations copy the first record
		synctest.Wait()
		select {
		case p := <-out:
			t.Fatalf("rate bypassed: %+v", p)
		default:
		}
		time.Sleep(900 * time.Millisecond)
		synctest.Wait()
		p := <-out
		if p.Where != UnknownTool || p.Count != 1 || p.Detail != "novel original" || p.Record[2] != 3 {
			t.Fatalf("novel first was lost or aggregated: %+v", p)
		}
		// No more observations. Next's timer still produces every repeat.
		start := time.Now()
		p = next(t, r)
		if p.Where != ZeroID || p.Count != 1 || p.Record != nil || time.Since(start) != time.Second {
			t.Fatalf("old repeat: %+v at %v", p, time.Since(start))
		}
		p = next(t, r)
		if p.Where != UnknownTool || p.Count != 100 || p.Record != nil {
			t.Fatalf("delayed repeats: %+v", p)
		}
		if got := r.Stats()[UnknownTool]; got != (Counts{Observed: 101, Emitted: 101, Reports: 2}) {
			t.Fatalf("counts: %+v", got)
		}
	})
}

func TestFloodRetainsAllSitesAndSharesOneBudget(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		// No sender at all during the flood; storage must retain every site.
		for i := 0; i < 1000; i++ {
			for s := Site(1); s < siteLimit; s++ {
				admit(t, r.Observe(Finding{s, "diagnostic"}))
			}
		}
		start := time.Now()
		for s := Site(1); s < siteLimit; s++ {
			p := next(t, r)
			if p.Where != s || p.Count != 1 || time.Since(start) != time.Duration(s-1)*100*time.Millisecond {
				t.Fatalf("first reports escaped global budget: %+v at %v", p, time.Since(start))
			}
		}
		r.Close()
		for s := Site(1); s < siteLimit; s++ {
			p := next(t, r)
			if p.Where != s || p.Count != 999 {
				t.Fatalf("repeat ordering/count: %+v", p)
			}
		}
		if _, err := r.Next(context.Background()); err != io.EOF {
			t.Fatalf("drain: %v", err)
		}
		for s, c := range r.Stats() {
			if c != (Counts{Observed: 1000, Emitted: 1000, Reports: 2}) {
				t.Fatalf("%s: %+v", s, c)
			}
		}
	})
}

func TestRepeatDeadlineIndependentOfTrafficAndFairness(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{ReportInterval: 10 * time.Millisecond})
		admit(t, r.Observe(Finding{ZeroID, "a"}))
		admit(t, r.Observe(Finding{UnknownTool, "b"}))
		start := time.Now()
		next(t, r)
		next(t, r)
		for i := 0; i < 9; i++ {
			admit(t, r.Observe(Finding{ZeroID, "a"}))
			admit(t, r.Observe(Finding{UnknownTool, "b"}))
			time.Sleep(100 * time.Millisecond)
		}
		p := next(t, r)
		if p.Where != ZeroID || p.Count != 9 || time.Since(start) != time.Second {
			t.Fatalf("traffic postponed deadline: %+v at %v", p, time.Since(start))
		}
		admit(t, r.Observe(Finding{ZeroID, "hot site"}))
		p = next(t, r)
		if p.Where != UnknownTool || p.Count != 9 || time.Since(start) != time.Second+10*time.Millisecond {
			t.Fatalf("hot repeat starved another: %+v", p)
		}
	})
}

func TestHintCountsRecordsAndPreservesRegions(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		raw := doorbell.RawRecord{doorbell.OpNotification}
		plan := memory.HintPlan{
			Diagnostics: []string{"future_epoch"},
			Violations: []memory.Violation{
				{Where: "owned_region_hint", SegmentID: 41, Region: 2, Detail: "Owned hint"},
				{Where: "owned_region_hint", SegmentID: 41, Region: 7, Detail: "Owned hint"},
			},
		}
		admit(t, r.ObserveHint(raw, plan))
		p := next(t, r)
		if p.Count != 1 || !strings.Contains(p.Detail, "segment 41 region 2") || !strings.Contains(p.Detail, "region 7") {
			t.Fatalf("unactionable detail/count: %+v", p)
		}
		// Identical received bytes still describe two further receptions.
		admit(t, r.ObserveHint(raw, plan))
		admit(t, r.ObserveHint(raw, plan))
		if p = next(t, r); p.Count != 2 || p.Record != nil {
			t.Fatalf("counted regions or deduplicated bytes: %+v", p)
		}
		if r.Stats()[OwnedRegionHint].Observed != 3 {
			t.Fatal(r.Stats())
		}
	})
}

func TestJoinedMemoryErrorsAreDistinctRecordlessViolations(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		err := fmt.Errorf("copy: %w", errors.Join(
			&memory.Violation{Where: "invalid_published_length", SegmentID: 19, Region: 3, Detail: "length 0"},
			&memory.Violation{Where: "release_cas_failed", SegmentID: 19, Region: 3, Detail: "foreign state"},
			memory.ErrQuarantined,
		))
		admit(t, r.ObserveError(err))
		admit(t, r.ObserveError(memory.ErrQuarantined))
		for _, s := range []Site{InvalidPublishedLength, ReleaseCASFailed} {
			p := next(t, r)
			if p.Where != s || p.Count != 1 || p.Record != nil || !strings.Contains(p.Detail, "segment 19 region 3") {
				t.Fatalf("joined error lost: %+v", p)
			}
		}
		admit(t, r.ObserveError(errors.Join(
			&memory.Violation{Where: "impossible_state_word", SegmentID: 19, Region: 1},
			&memory.Violation{Where: "impossible_state_word", SegmentID: 19, Region: 2},
		)))
		if p := next(t, r); p.Count != 1 || p.Where != ImpossibleStateWord {
			t.Fatal(p)
		}
		if p := next(t, r); p.Count != 1 || p.Where != ImpossibleStateWord {
			t.Fatal(p)
		}
		if c := r.Stats()[ImpossibleStateWord]; c.Observed != 2 || c.Pending != 0 {
			t.Fatal(c)
		}
	})
}

func TestGrantExceptionRoutesCapacityAndOwnership(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{MaxGrantReports: 2})
		admit(t, r.ObserveFrame(doorbell.Frame{Violation: &doorbell.Violation{Route: "grant_ack", Where: "size_below_preamble"}}))
		admit(t, r.ObserveFrame(doorbell.Frame{Diagnostics: []doorbell.Diagnostic{{Code: "unknown_opcode", Count: 1}}}))
		for i := byte(1); i <= 2; i++ {
			f := doorbell.Frame{
				Raw: doorbell.RawRecord{doorbell.OpMemory, 2, i}, HasRecord: true, HasHeader: true,
				Header:    doorbell.Header{Op: doorbell.OpMemory},
				Violation: &doorbell.Violation{Where: "zero_id", Route: "contract_violation"},
			}
			admit(t, r.ObserveFrame(f))
			f.Raw[2] = 0
		}
		raw := doorbell.RawRecord{doorbell.OpMemory, 2, 3}
		if err := r.ObserveRecord(raw, Finding{ZeroID, "grant"}, Finding{UnknownTool, "other"}); err != ErrGrantQueueFull {
			t.Fatalf("capacity: %v", err)
		}
		if r.Stats()[UnknownTool].Observed != 0 || r.Stats()[ZeroID].Observed != 2 {
			t.Fatal("partial admission", r.Stats())
		}
		for i := byte(1); i <= 2; i++ {
			p := next(t, r)
			if p.Where != ZeroID || p.Count != 1 || p.Record == nil || p.Record[2] != i {
				t.Fatalf("grant merged or record aliased: %+v", p)
			}
			if i == 1 {
				admit(t, r.ObserveRecord(raw, Finding{ZeroID, "retry rejected admission"}))
			}
		}
		if p := next(t, r); p.Record[2] != 3 || p.Count != 1 {
			t.Fatal(p)
		}
		// Ordinary E/zero_id still has its own first + repeat aggregation.
		admit(t, r.ObserveRecord(doorbell.RawRecord{doorbell.OpInput}, Finding{ZeroID, "input"}))
		if p := next(t, r); p.Record[0] != doorbell.OpInput {
			t.Fatal(p)
		}
		if c := r.Stats()[ZeroID]; c != (Counts{Observed: 4, Emitted: 4, Reports: 4}) {
			t.Fatal(c)
		}
	})
}

func TestCancellationAndCloseDoNotLosePendingReports(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		admit(t, r.Observe(Finding{ZeroID, "first"}))
		next(t, r)
		admit(t, r.Observe(Finding{ZeroID, "repeat"}))
		ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
		defer cancel()
		if _, err := r.Next(ctx); !errors.Is(err, context.DeadlineExceeded) {
			t.Fatal(err)
		}
		if r.Stats()[ZeroID].Pending != 1 {
			t.Fatal("cancellation consumed a report")
		}
		r.Close()
		r.Close()
		if err := r.Observe(Finding{UnknownTool, "late"}); err != ErrClosed {
			t.Fatal(err)
		}
		if p := next(t, r); p.Count != 1 || p.Record != nil {
			t.Fatal(p)
		}
		if _, err := r.Next(context.Background()); err != io.EOF {
			t.Fatal(err)
		}
		// Closing an empty reporter wakes an already waiting sender.
		r = newReporter(t, Config{})
		out := make(chan error, 1)
		go func() { _, err := r.Next(context.Background()); out <- err }()
		synctest.Wait()
		r.Close()
		if err := <-out; err != io.EOF {
			t.Fatal(err)
		}
	})
}

func TestConcurrentObserversAndReader(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		var emit sync.WaitGroup
		emit.Add(1)
		go func() {
			defer emit.Done()
			for {
				if _, err := r.Next(context.Background()); err == io.EOF {
					return
				} else if err != nil {
					t.Error(err)
					return
				}
			}
		}()
		var observe sync.WaitGroup
		for s := Site(1); s < siteLimit; s++ {
			observe.Add(1)
			go func() {
				defer observe.Done()
				for i := 0; i < 100; i++ {
					if err := r.Observe(Finding{s, "concurrent"}); err != nil {
						t.Error(err)
					}
					_ = r.Stats()
				}
			}()
		}
		observe.Wait()
		r.Close()
		emit.Wait()
		for s, c := range r.Stats() {
			if c.Observed != 100 || c.Emitted != 100 || c.Pending != 0 {
				t.Fatalf("%s lost counts: %+v", s, c)
			}
		}
	})
}

func TestWireCountChunkingAndAtomicOverflowRejection(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		r := newReporter(t, Config{})
		admit(t, r.Observe(Finding{ZeroID, "first"}))
		next(t, r)
		// Reach large counter boundaries without billions of detector calls.
		r.states[ZeroID].repeats = math.MaxUint32 + 7
		r.states[ZeroID].counts.Observed = math.MaxUint32 + 8
		if p := next(t, r); p.Count != math.MaxUint32 {
			t.Fatal(p)
		}
		if p := next(t, r); p.Count != 7 {
			t.Fatal(p)
		}
		if c := r.Stats()[ZeroID]; c.Pending != 0 || c.Emitted != math.MaxUint32+8 {
			t.Fatal(c)
		}
		r.states[ZeroID].counts.Observed = math.MaxUint64
		err := r.ObserveRecord(doorbell.RawRecord{}, Finding{UnknownTool, "valid"}, Finding{ZeroID, "overflow"})
		if err != ErrCountOverflow || r.Stats()[UnknownTool].Observed != 0 {
			t.Fatalf("overflow was not transactional: %v", err)
		}
	})
}

func TestValidationAndBoundedUTF8Detail(t *testing.T) {
	r := newReporter(t, Config{})
	for _, cfg := range []Config{{ReportInterval: -1}, {RepeatInterval: -1}, {MaxGrantReports: -1}} {
		if _, err := New(cfg); err == nil {
			t.Fatal("accepted invalid config", cfg)
		}
	}
	if err := r.ObserveRecord(doorbell.RawRecord{}, Finding{ZeroID, "ok"}, Finding{Site(100), "unknown"}); err != ErrUnknownSite {
		t.Fatal(err)
	}
	if r.Stats()[ZeroID].Observed != 0 {
		t.Fatal("partial unknown-site admission")
	}
	if err := r.ObserveFrame(doorbell.Frame{Violation: &doorbell.Violation{Route: "contract_violation", Where: "zero_id"}}); err != ErrInvalidFrame {
		t.Fatal(err)
	}
	if err := r.ObserveHint(doorbell.RawRecord{}, memory.HintPlan{}); err != ErrInvalidFrame {
		t.Fatal(err)
	}
	for _, detail := range []string{strings.Repeat("界", 1000), strings.Repeat("a", 10000), "\xffbad", strings.Repeat("x", 1020) + "界" + "tail"} {
		got := bounded(detail)
		if len(got) > MaxDetailBytes || !utf8.ValidString(got) {
			t.Fatalf("invalid bounded detail: len=%d, valid=%t", len(got), utf8.ValidString(got))
		}
	}
	stats := r.Stats()
	stats[ZeroID] = Counts{Observed: 123}
	if r.Stats()[ZeroID].Observed != 0 {
		t.Fatal("Stats aliases reporter state")
	}
}

func TestSitesMatchAuthoritativeIDL(t *testing.T) {
	data, err := os.ReadFile("../../../proto/vizi_plugin.proto")
	if err != nil {
		t.Fatal(err)
	}
	re := regexp.MustCompile(`(?m)^\s*VIOLATION_SITE_([A-Z_]+)\s*=\s*([0-9]+);`)
	seen := map[Site]bool{}
	for _, m := range re.FindAllStringSubmatch(string(data), -1) {
		n, err := strconv.ParseUint(m[2], 10, 32)
		if err != nil {
			t.Fatal(err)
		}
		if n == 0 {
			continue
		}
		s, err := ParseSite(strings.ToLower(m[1]))
		if err != nil || s != Site(n) {
			t.Fatalf("IDL %s=%d maps to %v, %v", m[1], n, s, err)
		}
		seen[s] = true
	}
	if len(seen) != int(siteLimit)-1 {
		t.Fatalf("SDK and IDL category sets differ: %v", seen)
	}
}
