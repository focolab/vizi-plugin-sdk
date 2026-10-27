//go:build linux || darwin || windows

package doorbell

import (
	"context"
	"crypto/subtle"
	"errors"
	"fmt"
	"sync"
	"time"
)

// Config contains local resource policy, not wire constants.
type Config struct {
	Handler func(Event)
	// Maximum queued lifecycle cancellation outcomes. Default 1024. Inputs
	// that could exceed it fail without mutation and must not be acknowledged.
	MaxPendingOutcomes int
	// Maximum simultaneously active tools, also bounding open interactions.
	// Default 1024. Refusal is local; the supervisor rejects the activation.
	MaxActiveTools int
	// SetupTimeout bounds Windows connection, token write, and wake setup.
	// Default 5 seconds. Unix adoption does not use this timeout.
	SetupTimeout time.Duration
}

type Transport struct {
	mu                    sync.Mutex // short state/syscall sections only, never poll or callback
	fd                    socketHandle
	wake                  [2]socketHandle
	info                  SocketInfo
	handler               func(Event)
	maxPending, maxTools  int
	ready, closed, exited bool
	nextID                uint32
	decoder               decoder
	input                 inputState
	pending               []Event
	terminal              chan Terminal
	done                  chan struct{}
	stats                 Stats
	sendRecord            func(socketHandle, []byte) (int, error)
	closeSocket           func()
	closeWake             func()
	partial               streamWindow
	expectedToken         string
}

func (cfg *Config) defaults() error {
	if cfg.Handler == nil {
		return errors.New("doorbell requires an event handler")
	}
	if cfg.MaxPendingOutcomes == 0 {
		cfg.MaxPendingOutcomes = 1024
	}
	if cfg.MaxActiveTools == 0 {
		cfg.MaxActiveTools = 1024
	}
	if cfg.MaxPendingOutcomes < 1 || cfg.MaxActiveTools < 1 {
		return errors.New("doorbell resource bounds must be positive")
	}
	return nil
}

// Platform constructors install owned sockets and release functions before start.
func newTransport(fd socketHandle, wake [2]socketHandle, info SocketInfo, cfg Config) *Transport {
	t := &Transport{
		fd: fd, wake: wake, info: info, handler: cfg.Handler,
		maxPending: cfg.MaxPendingOutcomes, maxTools: cfg.MaxActiveTools,
		nextID: 1, decoder: newDecoder(), input: newInputState(),
		terminal: make(chan Terminal, 1), done: make(chan struct{}),
		stats: Stats{Diagnostics: make(map[string]uint64)},
	}
	t.decoder.windows = platformWindows
	t.sendRecord = sendSocket
	return t
}
func (t *Transport) start() {
	started := make(chan struct{})
	go t.listen(started)
	<-started
}

// HandshakeComplete records major-version compatibility; this is not negotiation.
// Call before publishing Ready. Failure does not permit a partially live session.
// On Windows pass Hello.session_token as the third argument. An absent or
// mismatched echo terminates without a DoorbellFault. Unix callers omit it.
func (t *Transport) HandshakeComplete(major, minor uint32, sessionToken ...string) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return ErrClosed
	}
	if !CompatibleVersion(major, minor) {
		return fmt.Errorf("%w: peer %d.%d", ErrHandshake, major, minor)
	}
	if t.expectedToken != "" {
		if len(sessionToken) != 1 || subtle.ConstantTimeCompare([]byte(sessionToken[0]), []byte(t.expectedToken)) != 1 {
			t.terminateLocked(Terminal{Reason: "handshake_mismatch"})
			return fmt.Errorf("%w: missing or mismatched session token", ErrHandshake)
		}
	} else if len(sessionToken) != 0 {
		return fmt.Errorf("%w: token is not applicable to this transport", ErrHandshake)
	}
	t.ready = true
	return nil
}

// Terminal returns the supervisor mailbox, published once before socket release.
// Drain independently of Handler; this channel closes after its single value.
func (t *Transport) Terminal() <-chan Terminal { return t.terminal }

// Wait waits for listener and callback completion. It does not request a stop.
func (t *Transport) Wait(ctx context.Context) error {
	select {
	case <-t.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (t *Transport) Stats() Stats {
	t.mu.Lock()
	defer t.mu.Unlock()
	s := t.stats
	s.Diagnostics = make(map[string]uint64, len(t.stats.Diagnostics))
	for k, v := range t.stats.Diagnostics {
		s.Diagnostics[k] = v
	}
	return s
}

func (t *Transport) wakeLocked() {
	for {
		_, err := sendWake(t.wake[1], []byte{1})
		if interrupted(err) {
			continue
		}
		return
	}
}

// terminateLocked first latches, then publishes to a mailbox that is empty by
// construction, then releases the socket. No consumer code can wedge this path.
// Ordered callbacks may still be running; their descriptors are not touched.
func (t *Transport) terminateLocked(term Terminal) {
	if t.closed {
		return
	}
	t.closed = true
	if socketIsStream && t.partial.used > 0 {
		term.Capture = t.partial.capture()
	}
	if term.Fault != nil {
		term.Fault.Transport = transportName
		term.Fault.Socket = t.info
	}
	t.pending = append(t.pending, t.input.cancelWhere(func(Input) bool { return true }, term.Reason)...)
	t.pending = append(t.pending, Event{Terminal: &term})
	t.terminal <- term
	close(t.terminal)
	t.wakeLocked()
	t.closeSocket()
}

// Stop promptly publishes one local teardown, releases the doorbell socket, and
// wakes the listener. It is safe from a callback and never joins that callback.
func (t *Transport) Stop() {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.terminateLocked(Terminal{Reason: "local_stop"})
}

// Notify makes at most one successful syscall attempt, retrying EINTR only.
// Backpressure drops immediately, burns the admitted ID and never escalates.
// An empty mask is suppressed before admission and does not consume an ID.
func (t *Transport) Notify(n Notification) (SendResult, error) {
	// Check arguments before admission, so invalid local input burns no ID.
	if _, err := EncodeNotification(n, 0); err != nil {
		return Dropped, err
	}
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed {
		return Dropped, ErrClosed
	}
	if !t.ready {
		return Dropped, ErrHandshake
	}
	if n.Mask == (Mask{}) {
		t.stats.Suppressed++
		return Dropped, nil
	}
	r, _ := EncodeNotification(n, t.nextID)
	t.nextID++
	for {
		count, err := t.sendRecord(t.fd, r[:])
		if interrupted(err) {
			continue
		}
		if err != nil {
			if backpressure(err) {
				t.stats.Refused++
				return Dropped, nil
			}
			if peerGone(err) {
				t.terminateLocked(Terminal{Reason: "peer_gone", OSError: osError(err)})
				return Dropped, ErrClosed
			}
			if locallyClosed(err) {
				t.terminateLocked(Terminal{Reason: "locally_closed", OSError: osError(err)})
				return Dropped, ErrClosed
			}
			t.terminateLocked(Terminal{Reason: "fault", Fault: &Fault{Class: "ambiguous_send", Direction: "plugin_to_host", OSError: osError(err), Detail: err.Error()}})
			return Dropped, err
		}
		if count != RecordSize {
			t.terminateLocked(Terminal{Reason: "fault", Fault: &Fault{Class: "short_write", Direction: "plugin_to_host", ObservedLength: &count, OSError: ptr(0)}})
			return Dropped, errors.New("short doorbell send")
		}
		t.stats.Sent++
		return Sent, nil
	}
}

func (t *Transport) lifecycle(change func() ([]Event, error)) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.exited {
		return ErrClosed
	}
	out, err := change()
	if err != nil {
		return err
	}
	t.pending = append(t.pending, out...)
	if len(out) > 0 {
		t.wakeLocked()
	}
	return nil
}

// AfterPending schedules short supervisor work on the listener after all events
// already queued, including lifecycle cancellations. It never runs inline.
// It shares MaxPendingOutcomes and fails without enqueueing when full or closed.
// Like Handler, work must not block on host requests. Panics terminate the
// transport through the normal handler-panic path.
func (t *Transport) AfterPending(work func()) error {
	if work == nil {
		return errors.New("nil listener work")
	}
	return t.lifecycle(func() ([]Event, error) {
		if len(t.pending) >= t.maxPending {
			return nil, ErrQueueFull
		}
		return []Event{{dispatch: work}}, nil
	})
}

// ToolActivated installs an activation before the supervisor acknowledges it.
// Gaps are accepted, but calls must arrive in increasing-handle order. Repeating
// a live handle succeeds; zero and already-retired handles fail.
func (t *Transport) ToolActivated(handle uint32) error {
	return t.lifecycle(func() ([]Event, error) {
		if t.input.active[handle] {
			return nil, nil
		}
		if handle == 0 || handle <= t.input.highTool {
			return nil, ErrLifecycle
		}
		if len(t.input.active) >= t.maxTools {
			return nil, ErrToolLimit
		}
		t.input.active[handle] = true
		t.input.highTool = handle
		return nil, nil
	})
}

// ToolRetired retires a handle and enqueues cancellation of its open gesture.
// An inactive handle at or below the high-water mark is an idempotent no-op.
func (t *Transport) ToolRetired(handle uint32) error {
	return t.lifecycle(func() ([]Event, error) {
		if handle == 0 || handle > t.input.highTool {
			return nil, ErrLifecycle
		}
		if t.input.byTool[handle] != 0 && len(t.pending) >= t.maxPending {
			return nil, ErrQueueFull
		}
		delete(t.input.active, handle)
		return t.input.cancel(t.input.byTool[handle], "tool_retired"), nil
	})
}

// EpochAcknowledged installs the pane epoch before the gRPC acknowledgement.
// Newer epochs cancel the pane's gestures; equal epochs preserve them; older or
// zero epochs fail. The installed epoch survives pane retirement and ID reuse.
func (t *Transport) EpochAcknowledged(view uint8, epoch uint32) error {
	// Reserve precisely under the same lock: a pane can have several tools.
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.exited {
		return ErrClosed
	}
	old := t.input.epochs[view]
	if epoch == 0 || epoch < old {
		return ErrLifecycle
	}
	if epoch == old {
		return nil
	}
	count := 0
	for _, i := range t.input.open {
		if i.opening.ViewID == view {
			count++
		}
	}
	if len(t.pending)+count > t.maxPending {
		return ErrQueueFull
	}
	t.input.epochs[view] = epoch
	out := t.input.cancelWhere(func(e Input) bool { return e.ViewID == view }, "epoch_changed")
	t.pending = append(t.pending, out...)
	if len(out) > 0 {
		t.wakeLocked()
	}
	return nil
}

// CancelInteraction retracts an opening rejected by the supervisor. Unknown or
// already-closed interactions are no-ops. Never reject an accepted terminal at
// the supervisor: its interaction is already closed and cannot be retracted.
func (t *Transport) CancelInteraction(id uint32) error {
	return t.lifecycle(func() ([]Event, error) {
		if _, ok := t.input.open[id]; ok && len(t.pending) >= t.maxPending {
			return nil, ErrQueueFull
		}
		return t.input.cancel(id, "supervisor_cancelled"), nil
	})
}

// CancelInteractions cancels every open gesture in a pane, for example on close.
func (t *Transport) CancelInteractions(view uint8) error {
	t.mu.Lock()
	defer t.mu.Unlock()
	if t.closed || t.exited {
		return ErrClosed
	}
	count := 0
	for _, i := range t.input.open {
		if i.opening.ViewID == view {
			count++
		}
	}
	if len(t.pending)+count > t.maxPending {
		return ErrQueueFull
	}
	out := t.input.cancelWhere(func(e Input) bool { return e.ViewID == view }, "pane_cancelled")
	t.pending = append(t.pending, out...)
	if len(out) > 0 {
		t.wakeLocked()
	}
	return nil
}

func (t *Transport) emit(e Event) {
	var descriptor *Descriptor
	if e.Frame != nil {
		descriptor = e.Frame.Descriptor
	}
	defer func() {
		descriptor.release()
		if p := recover(); p != nil {
			t.mu.Lock()
			t.stats.Diagnostics["handler_panic"]++
			t.terminateLocked(Terminal{Reason: "handler_panic"})
			t.mu.Unlock()
		}
	}()
	if e.dispatch != nil {
		e.dispatch()
	} else {
		t.handler(e)
	}
}

func (t *Transport) receiveErrorLocked(err error) {
	switch {
	case peerGone(err):
		t.terminateLocked(Terminal{Reason: "peer_gone", OSError: osError(err)})
	case locallyClosed(err):
		t.terminateLocked(Terminal{Reason: "locally_closed", OSError: osError(err)})
	default:
		t.terminateLocked(Terminal{Reason: "fault", Fault: &Fault{
			Class: "recv_error", Direction: "host_to_plugin", Ordinal: ptr(t.decoder.ordinal + 1),
			OSError: osError(err), Detail: err.Error(),
		}})
	}
}

func (t *Transport) listen(started chan struct{}) {
	close(started)
	defer func() {
		t.mu.Lock()
		// A consumer Goexit cannot be recovered, but must still notify the supervisor.
		if !t.closed {
			t.terminateLocked(Terminal{Reason: "listener_exited"})
		}
		t.exited = true
		t.closeWake()
		t.mu.Unlock()
		close(t.done)
	}()
	for {
		t.mu.Lock()
		if len(t.pending) > 0 {
			e := t.pending[0]
			t.pending[0] = Event{}
			t.pending = t.pending[1:]
			t.mu.Unlock()
			t.emit(e)
			continue
		}
		if t.closed {
			t.mu.Unlock()
			return
		}
		fd, wake := t.fd, t.wake[0]
		t.mu.Unlock()
		woken, err := pollSockets(fd, wake)
		if interrupted(err) {
			continue
		}
		t.mu.Lock()
		// A stop can release/recycle fd while poll runs. Never use it after the latch.
		if t.closed {
			t.mu.Unlock()
			continue
		}
		if err != nil {
			t.receiveErrorLocked(err)
			t.mu.Unlock()
			continue
		}
		if woken {
			if err := drainWake(wake); err != nil {
				// The private wake channel failed, not the peer's doorbell.
				t.terminateLocked(Terminal{Reason: "wake_error", OSError: osError(err)})
			}
			t.mu.Unlock()
			continue
		}
		packet, err := t.receivePacket()
		if interrupted(err) || wouldBlock(err) {
			t.mu.Unlock()
			continue
		}
		if err != nil {
			t.receiveErrorLocked(err)
			t.mu.Unlock()
			continue
		}
		if packet.eof {
			t.terminateLocked(Terminal{Reason: "peer_gone"})
			t.mu.Unlock()
			continue
		}
		if !packet.complete {
			t.mu.Unlock()
			continue
		}
		t.acceptLocked(packet)
		t.mu.Unlock()
	}
}

// acceptLocked owns descriptor cleanup, input state, and atomic event enqueue.
func (t *Transport) acceptLocked(packet receivedPacket) {
	frame, fault := t.decoder.decode(packet.data, len(packet.descriptors), packet.ctrunc)
	t.stats.Received++
	var cancellations []Event
	if frame != nil && fault == nil && frame.Input != nil {
		cancellations = t.input.apply(frame)
	}
	if fault == nil && frame != nil && frame.Disposition == Decoded && frame.Memory != nil && len(packet.descriptors) == 1 {
		frame.Descriptor = packet.descriptors[0]
		packet.descriptors = nil
	}
	for _, d := range packet.descriptors {
		d.release()
	}
	if frame != nil {
		for _, d := range frame.Diagnostics {
			t.stats.Diagnostics[d.Code] += d.Count
		}
		t.pending = append(t.pending, Event{Frame: frame})
		t.pending = append(t.pending, cancellations...)
	} else {
		t.pending = append(t.pending, Event{Frame: &Frame{Ordinal: t.decoder.ordinal, Disposition: FramingRejected}})
	}
	if fault != nil {
		t.terminateLocked(Terminal{Reason: "fault", Fault: fault})
	}
}
