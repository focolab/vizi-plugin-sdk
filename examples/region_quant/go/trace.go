package main

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Trace state has its own short-held lock. Neither host reads nor application
// model-lock waits run under it, so brightest can always take a cheap snapshot.
type trace struct {
	row               row
	channel           uint32
	frames            []measurement
	total             uint32
	pending, complete bool
	best              uint32
	found             bool
	cancel            context.CancelFunc
	retryAt           time.Time
	backoff           time.Duration
}
type traceSnapshot struct {
	name                      string
	done, total               uint32
	selected, complete, found bool
	best                      uint32
}
type traceWorker struct {
	mu       sync.Mutex
	rows     map[string]*trace
	selected string
	total    uint32
	wake     chan struct{}
	view     func(context.Context) (*pb.ViewState, error)
	measure  func(context.Context, string, uint32, uint32) (measurement, error)
	publish  func(string) error
	diagnose func(error)
}

func newTraceWorker(view func(context.Context) (*pb.ViewState, error), measure func(context.Context, string, uint32, uint32) (measurement, error), publish func(string) error, diagnose func(error)) *traceWorker {
	return &traceWorker{rows: map[string]*trace{}, wake: make(chan struct{}, 1), view: view, measure: measure, publish: publish, diagnose: diagnose}
}

// The application learned T on the first/new-generation push. Invalidate all
// old identities before any reads for the new dataset begin.
func (w *traceWorker) datasetOpened(total uint32) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for _, tr := range w.rows {
		if tr.cancel != nil {
			tr.cancel()
		}
	}
	clear(w.rows)
	w.total = total
	select {
	case w.wake <- struct{}{}:
	default:
	}
}

// syncRows is called with the application model lock held. Replacing a trace
// also replaces its identity: even a canceled read that returns success cannot
// publish into the new trace. Unchanged rows retain completed traces across t.
func (w *traceWorker) syncRows(rows map[string]row, channel uint32, replace map[string]bool, reset bool) {
	w.mu.Lock()
	defer w.mu.Unlock()
	for id, tr := range w.rows {
		r, exists := rows[id]
		if reset || !exists || r.unmeasured || replace[id] || tr.channel != channel {
			if tr.cancel != nil {
				tr.cancel()
			}
			delete(w.rows, id)
		}
	}
	for id, r := range rows {
		if !r.unmeasured && w.rows[id] == nil {
			w.rows[id] = &trace{row: r, channel: channel, total: w.total, pending: true}
		}
	}
	select {
	case w.wake <- struct{}{}:
	default:
	}
}
func (w *traceWorker) selectRegion(id string) { w.mu.Lock(); defer w.mu.Unlock(); w.selected = id }
func (w *traceWorker) snapshot() traceSnapshot {
	w.mu.Lock()
	defer w.mu.Unlock()
	tr := w.rows[w.selected]
	if tr == nil {
		return traceSnapshot{}
	}
	return traceSnapshot{name: tr.row.display(), done: uint32(len(tr.frames)), total: tr.total, selected: true, complete: tr.complete, found: tr.found, best: tr.best}
}
func (w *traceWorker) label(text string) {
	if err := w.publish(text); err != nil {
		w.diagnose(err)
	}
}
func (w *traceWorker) run(ctx context.Context) {
	for {
		w.mu.Lock()
		if ctx.Err() != nil {
			w.mu.Unlock()
			return
		}
		var current *trace
		var retryAt time.Time
		now := time.Now()
		// One row at a time; stable ID order makes queue behavior reproducible.
		for id, tr := range w.rows {
			if tr.pending && tr.retryAt.After(now) {
				if retryAt.IsZero() || tr.retryAt.Before(retryAt) {
					retryAt = tr.retryAt
				}
				continue
			}
			if tr.pending && (current == nil || id < current.row.id) {
				current = tr
			}
		}
		if current == nil {
			w.label("")
			w.mu.Unlock()
			var timer *time.Timer
			var retry <-chan time.Time
			if !retryAt.IsZero() {
				timer = time.NewTimer(time.Until(retryAt))
				retry = timer.C
			}
			select {
			case <-ctx.Done():
				if timer != nil {
					timer.Stop()
				}
				return
			case <-w.wake:
			case <-retry:
			}
			if timer != nil {
				timer.Stop()
			}
			continue
		}
		current.pending = false
		work, cancel := context.WithCancel(ctx)
		current.cancel = cancel
		w.mu.Unlock()
		err := w.compute(work, current)
		cancel()
		w.mu.Lock()
		current.cancel = nil
		if err == nil {
			current.backoff, current.retryAt = 0, time.Time{}
		}
		if transientTraceError(err) && ctx.Err() == nil && w.rows[current.row.id] == current {
			current.pending = true
			current.backoff = min(max(100*time.Millisecond, current.backoff*2), 5*time.Second)
			current.retryAt = time.Now().Add(current.backoff)
		}
		w.mu.Unlock()
		if errors.Is(err, control.ErrSessionEnded) {
			return // the session ended (Shutdown, or the host gone): no read will be answered
		}
		var host *control.HostError
		if err != nil && !errors.Is(err, context.Canceled) && !(errors.As(err, &host) && host.Kind == pb.Error_NOT_FOUND) {
			w.diagnose(err)
		}
	}
}
func transientTraceError(err error) bool {
	return err != nil && !regionMissing(err) && !errors.Is(err, control.ErrSessionEnded)
}

func (w *traceWorker) compute(ctx context.Context, tr *trace) error {
	view, err := w.view(ctx)
	if err != nil {
		return err
	}
	if len(view.ShapeTczyx) != 5 {
		return fmt.Errorf("trace view shape must be TCZYX")
	}
	total := view.ShapeTczyx[0]
	w.mu.Lock()
	if ctx.Err() != nil || w.rows[tr.row.id] != tr {
		w.mu.Unlock()
		return context.Canceled
	}
	w.total = total
	for _, queued := range w.rows {
		if queued.pending {
			queued.total = total
		}
	}
	// A retry recomputes a coherent trace; partial samples must not be appended twice.
	tr.frames = nil
	tr.best, tr.found, tr.complete = 0, false, false
	tr.total = total
	if total == 0 {
		tr.complete = true
	}
	w.label(fmt.Sprintf("Measuring %s: 0 of %d frames", tr.row.display(), total))
	w.mu.Unlock()
	for t := uint32(0); t < total; t++ {
		value, err := w.measure(ctx, tr.row.id, t, tr.channel)
		if err != nil {
			return err
		}
		w.mu.Lock()
		if ctx.Err() != nil || w.rows[tr.row.id] != tr {
			w.mu.Unlock()
			return context.Canceled
		}
		tr.frames = append(tr.frames, value)
		if value.count != 0 && (!tr.found || greaterMean(value, tr.frames[tr.best])) {
			tr.best, tr.found = t, true
		}
		tr.complete = uint32(len(tr.frames)) == total
		w.label(fmt.Sprintf("Measuring %s: %d of %d frames", tr.row.display(), len(tr.frames), total))
		w.mu.Unlock()
	}
	return nil
}
