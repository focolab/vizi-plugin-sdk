//go:build linux || darwin

package doorbell

import (
	"errors"
	"testing"
	"time"
)

func TestListenerWorkBoundAndOrder(t *testing.T) {
	tr, _, _ := start(t, Config{MaxPendingOutcomes: 1})
	entered, release, done := make(chan struct{}), make(chan struct{}), make(chan struct{})
	defer func() {
		select {
		case <-release:
		default:
			close(release)
		}
	}()
	if err := tr.AfterPending(func() { close(entered); <-release }); err != nil {
		t.Fatal(err)
	}
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("work not dispatched")
	}
	if err := tr.AfterPending(func() { close(done) }); err != nil {
		t.Fatal(err)
	}
	if err := tr.AfterPending(func() { t.Error("refused work ran") }); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	if err := tr.AfterPending(nil); err == nil {
		t.Fatal("nil accepted")
	}
	select {
	case <-done:
		t.Fatal("work ran concurrently")
	default:
	}
	close(release)
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("queued work not dispatched")
	}
	tr.Stop()
	if err := tr.AfterPending(func() {}); !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}
