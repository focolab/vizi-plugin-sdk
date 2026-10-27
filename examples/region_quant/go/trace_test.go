package main

import (
	"context"
	"errors"
	"sync"
	"testing"
	"testing/synctest"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestTraceRetriesTransientFailureWithBackoffAndNoDuplicateFrames(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		var calls []uint32
		var times []time.Time
		diagnostics := 0
		w := newTraceWorker(func(context.Context) (*pb.ViewState, error) {
			return &pb.ViewState{ShapeTczyx: []uint32{2, 1, 1, 1, 1}}, nil
		},
			func(_ context.Context, _ string, frame, _ uint32) (measurement, error) {
				calls = append(calls, frame)
				times = append(times, time.Now())
				if len(calls) == 2 || len(calls) == 3 {
					return measurement{}, &control.HostError{Kind: pb.Error_IO_ERROR, Message: "temporary read error"}
				}
				return measurement{count: 1, sum: uint64(frame + 1)}, nil
			}, func(string) error { return nil }, func(error) { diagnostics++ })
		w.syncRows(map[string]row{"r": {id: "r"}}, 0, nil, false)
		w.selectRegion("r")
		stopped := make(chan struct{})
		go func() { defer close(stopped); w.run(ctx) }()
		time.Sleep(350 * time.Millisecond)
		synctest.Wait()
		got := w.snapshot()
		if !got.complete || got.done != 2 || got.best != 1 || diagnostics != 2 {
			t.Fatal(got, diagnostics)
		}
		if len(calls) != 5 || calls[2] != 0 || calls[3] != 0 || calls[4] != 1 {
			t.Fatal(calls)
		}
		if times[2].Sub(times[1]) < 100*time.Millisecond || times[3].Sub(times[2]) < 200*time.Millisecond {
			t.Fatal("retry lacked backoff", times)
		}
		cancel()
		<-stopped
	})
}

func TestMeasureFailurePreservesOnlyFailedRowsNumbers(t *testing.T) {
	old := measurement{count: 3, sum: 17}
	rows := map[string]row{"failed": {id: "failed", measurement: old}, "good": {id: "good"}, "gone": {id: "gone"}}
	failure := &control.HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "frame unavailable"}
	err := measureRows(context.Background(), rows, 0, 0, func(_ context.Context, id string, _, _ uint32) (measurement, error) {
		switch id {
		case "failed":
			return measurement{}, failure
		case "gone":
			return measurement{}, &control.HostError{Kind: pb.Error_NOT_FOUND}
		default:
			return measurement{count: 1, sum: 99}, nil
		}
	})
	if !errors.Is(err, failure) || len(rows) != 2 || rows["failed"].measurement != old || rows["good"].sum != 99 {
		t.Fatal(rows, err)
	}
}

func TestTraceRetriesOtherErrorsCapsBackoffAndResetsAfterSuccess(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		var calls []time.Time
		w := newTraceWorker(func(context.Context) (*pb.ViewState, error) {
			return &pb.ViewState{ShapeTczyx: []uint32{1, 1, 1, 1, 1}}, nil
		}, func(context.Context, string, uint32, uint32) (measurement, error) {
			calls = append(calls, time.Now())
			if len(calls) <= 8 {
				if len(calls)%2 == 0 {
					return measurement{}, errors.New("read failed")
				}
				return measurement{}, &control.HostError{Kind: pb.Error_OUT_OF_BOUNDS}
			}
			return measurement{count: 1, sum: 5}, nil
		}, func(string) error { return nil }, func(error) {})
		w.datasetOpened(1)
		w.syncRows(map[string]row{"r": {id: "r"}}, 0, nil, false)
		w.selectRegion("r")
		stopped := make(chan struct{})
		go func() { defer close(stopped); w.run(ctx) }()
		time.Sleep(17 * time.Second)
		synctest.Wait()
		want := []time.Duration{100 * time.Millisecond, 200 * time.Millisecond, 400 * time.Millisecond, 800 * time.Millisecond, 1600 * time.Millisecond, 3200 * time.Millisecond, 5 * time.Second, 5 * time.Second}
		if len(calls) != 9 || !w.snapshot().complete {
			t.Fatal(calls, w.snapshot())
		}
		for i, delay := range want {
			if calls[i+1].Sub(calls[i]) != delay {
				t.Fatal(i, calls)
			}
		}
		w.mu.Lock()
		if w.rows["r"].backoff != 0 {
			t.Error("success retained retry delay")
		}
		w.mu.Unlock()
		cancel()
		<-stopped
	})
}

func TestDatasetFrameCountBeforeTraceAndFloatRowsDoNotMeasure(t *testing.T) {
	a := &application{rows: map[string]row{"r": {id: "r", name: "kept", measurement: measurement{count: 1, sum: 99}}}}
	a.unsupported.Store(true)
	// There is deliberately no session: an f32 update must perform no host read.
	if err := a.remeasure(context.Background(), a.rows, 0, 0); err != nil {
		t.Fatal(err)
	}
	r := a.rows["r"]
	if r.name != "kept" || !r.unmeasured || r.measurement != (measurement{}) || swatchIndex(r.measurement) != 0 {
		t.Fatal(r)
	}
	w := newTraceWorker(nil, nil, nil, nil)
	w.datasetOpened(9)
	w.syncRows(map[string]row{"r": {id: "r"}}, 0, nil, true)
	w.selectRegion("r")
	if got := w.snapshot(); got.total != 9 || got.done != 0 {
		t.Fatal(got)
	}
	w.syncRows(a.rows, 0, nil, false)
	if len(w.rows) != 0 {
		t.Fatal("float trace queued", w.rows)
	}
}

func TestSameChannelDoesNotReadOrPublish(t *testing.T) {
	ready := make(chan struct{})
	close(ready)
	a := &application{ready: ready, gate: make(chan struct{}, 1), c: 2}
	// A redundant choice must work without accessing a session or trace worker.
	if _, err := a.channel(context.Background(), nil, plugin.Args{"value": plugin.Int(2)}); err != nil {
		t.Fatal(err)
	}
}

func TestMeasureRowsKeepsSurvivingChangesWhenRegionDisappears(t *testing.T) {
	rows := map[string]row{"deleted": {id: "deleted"}, "changed": {id: "changed", name: "new name"}, "other": {id: "other"}}
	err := measureRows(context.Background(), rows, 2, 1, func(_ context.Context, id string, frame, channel uint32) (measurement, error) {
		if frame != 2 || channel != 1 {
			t.Fatal(frame, channel)
		}
		if id == "deleted" {
			return measurement{}, &control.HostError{Kind: pb.Error_NOT_FOUND}
		}
		return measurement{count: 2, sum: 10}, nil
	})
	if err != nil || len(rows) != 2 || rows["changed"].name != "new name" || rows["changed"].sum != 10 || rows["other"].count != 2 {
		t.Fatal(rows, err)
	}
}

func TestTraceInvalidationDiscardsInFlightResultAndRestartsAtZero(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		type read struct {
			id    string
			t, c  uint32
			reply chan measurement
		}
		reads := make(chan read)
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		w := newTraceWorker(func(context.Context) (*pb.ViewState, error) {
			return &pb.ViewState{ShapeTczyx: []uint32{2, 2, 1, 1, 1}}, nil
		},
			func(_ context.Context, id string, frame, c uint32) (measurement, error) {
				q := read{id, frame, c, make(chan measurement)}
				reads <- q
				return <-q.reply, nil
			},
			func(string) error { return nil }, func(err error) { t.Error(err) })
		rows := map[string]row{"r": {id: "r", name: "first"}}
		w.syncRows(rows, 0, nil, false)
		w.selectRegion("r")
		stopped := make(chan struct{})
		go func() { defer close(stopped); w.run(ctx) }()
		first := <-reads
		if first.t != 0 || first.c != 0 {
			t.Fatal(first)
		}
		// Snapshots and invalidation complete while an old read ignores cancellation.
		if got := w.snapshot(); got.complete || got.done != 0 || got.total != 2 {
			t.Fatal(got)
		}
		w.syncRows(rows, 1, nil, false)
		first.reply <- measurement{count: 1, sum: 999}
		restarted := <-reads
		if restarted.t != 0 || restarted.c != 1 {
			t.Fatal(restarted)
		}
		if got := w.snapshot(); got.done != 0 {
			t.Fatal("stale sample published", got)
		}
		restarted.reply <- measurement{count: 1, sum: 3}
		second := <-reads
		if second.t != 1 || second.c != 1 {
			t.Fatal(second)
		}
		// Replacing the same ID also invalidates a partially completed trace.
		rows["r"] = row{id: "r", name: "renamed"}
		w.syncRows(rows, 1, map[string]bool{"r": true}, false)
		second.reply <- measurement{count: 1, sum: 999}
		replacement := <-reads
		if replacement.t != 0 || replacement.c != 1 {
			t.Fatal(replacement)
		}
		replacement.reply <- measurement{count: 1, sum: 5}
		final := <-reads
		final.reply <- measurement{count: 2, sum: 10} // Exact tie keeps frame zero.
		synctest.Wait()
		got := w.snapshot()
		if !got.complete || got.done != 2 || got.name != "renamed" || !got.found || got.best != 0 {
			t.Fatal(got)
		}
		// Ordinary current-frame refreshes keep the complete trace.
		w.syncRows(rows, 1, nil, false)
		synctest.Wait()
		if !w.snapshot().complete {
			t.Fatal("unchanged trace invalidated")
		}
		w.syncRows(nil, 1, nil, false)
		if w.snapshot().selected {
			t.Fatal("deleted trace retained")
		}
		cancel()
		<-stopped
	})
}

func TestTraceMissingRegionDoesNotKillWorker(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		var mu sync.Mutex
		var labels []string
		w := newTraceWorker(func(context.Context) (*pb.ViewState, error) {
			return &pb.ViewState{ShapeTczyx: []uint32{3, 1, 1, 1, 1}}, nil
		},
			func(_ context.Context, id string, frame, c uint32) (measurement, error) {
				if id == "a" {
					return measurement{}, &control.HostError{Kind: pb.Error_NOT_FOUND, Message: "unknown region"}
				}
				if frame == 0 {
					return measurement{}, nil
				}
				return measurement{count: 1, sum: 9}, nil
			}, func(s string) error { mu.Lock(); defer mu.Unlock(); labels = append(labels, s); return nil }, func(err error) { t.Errorf("unexpected diagnostic: %v", err) })
		w.syncRows(map[string]row{"a": {id: "a"}, "b": {id: "b", name: "beta"}}, 0, nil, false)
		stopped := make(chan struct{})
		go func() { defer close(stopped); w.run(ctx) }()
		synctest.Wait()
		w.selectRegion("a")
		if w.snapshot().complete {
			t.Fatal("missing region completed")
		}
		w.selectRegion("b")
		got := w.snapshot()
		if !got.complete || !got.found || got.best != 1 || got.done != 3 {
			t.Fatal(got)
		}
		mu.Lock()
		if labels[len(labels)-1] != "" {
			t.Fatal(labels)
		}
		for _, wanted := range []string{"Measuring beta: 0 of 3 frames", "Measuring beta: 1 of 3 frames", "Measuring beta: 2 of 3 frames", "Measuring beta: 3 of 3 frames"} {
			found := false
			for _, s := range labels {
				found = found || s == wanted
			}
			if !found {
				t.Errorf("missing progress %q: %v", wanted, labels)
			}
		}
		mu.Unlock()
		// A later push after NOT_FOUND still starts work, including reused IDs.
		w.syncRows(map[string]row{"b": {id: "b"}}, 0, nil, true)
		synctest.Wait()
		if !w.snapshot().complete {
			t.Fatal("worker stopped after missing region")
		}
		cancel()
		<-stopped
	})
}
