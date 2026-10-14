package doorbell

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

const winToken = "0123456789abcdef0123456789ABCDEF"

func winStart(t *testing.T, cfg Config) (*Transport, *net.TCPConn, chan Event) {
	t.Helper()
	listener, err := net.ListenTCP("tcp4", &net.TCPAddr{IP: net.IPv4(127, 0, 0, 1)})
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	listener.SetDeadline(time.Now().Add(3 * time.Second))
	accepted := make(chan *net.TCPConn, 1)
	failed := make(chan error, 1)
	go func() {
		conn, err := listener.AcceptTCP()
		if err != nil {
			failed <- err
			return
		}
		conn.SetReadDeadline(time.Now().Add(3 * time.Second))
		var token [32]byte
		if _, err = io.ReadFull(conn, token[:]); err != nil {
			conn.Close()
			failed <- err
			return
		}
		if string(token[:]) != winToken {
			conn.Close()
			failed <- errors.New("wrong token preamble")
			return
		}
		conn.SetReadDeadline(time.Time{})
		accepted <- conn
	}()
	events := make(chan Event, 128)
	if cfg.Handler == nil {
		cfg.Handler = func(e Event) { events <- e }
	}
	tr, err := Dial(context.Background(), listener.Addr().String(), winToken, cfg)
	if err != nil {
		t.Fatal(err)
	}
	var host *net.TCPConn
	select {
	case host = <-accepted:
	case err = <-failed:
		tr.Stop()
		t.Fatal(err)
	case <-time.After(3 * time.Second):
		tr.Stop()
		t.Fatal("accept timeout")
	}
	t.Cleanup(func() {
		tr.Stop()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := tr.Wait(ctx); err != nil {
			t.Error(err)
		}
		host.Close()
	})
	return tr, host, events
}
func winNext(t *testing.T, events <-chan Event) Event {
	t.Helper()
	select {
	case e := <-events:
		return e
	case <-time.After(time.Second):
		t.Fatal("event timeout")
	}
	return Event{}
}
func winTerminal(t *testing.T, tr *Transport) Terminal {
	t.Helper()
	select {
	case e, ok := <-tr.Terminal():
		if !ok {
			t.Fatal("terminal already consumed")
		}
		return e
	case <-time.After(time.Second):
		t.Fatal("terminal timeout")
	}
	return Terminal{}
}
func winSend(t *testing.T, host *net.TCPConn, b []byte) {
	t.Helper()
	host.SetWriteDeadline(time.Now().Add(time.Second))
	for len(b) > 0 {
		n, err := host.Write(b)
		if err != nil {
			t.Fatal(err)
		}
		b = b[n:]
	}
}
func winReady(t *testing.T, tr *Transport) {
	t.Helper()
	if err := tr.HandshakeComplete(1, 0, winToken); err != nil {
		t.Fatal(err)
	}
}

func TestWindowsStartupAndRecords(t *testing.T) {
	tr, host, events := winStart(t, Config{})
	if _, err := tr.Notify(note()); !errors.Is(err, ErrHandshake) {
		t.Fatal(err)
	}
	winReady(t, tr)
	r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 4096, Permissions: PluginRead, Name: `Local\arbitrary-name`}, 1)
	// A real TCP connection carries the named capability, not a descriptor.
	winSend(t, host, r[:17])
	winSend(t, host, r[17:])
	e := winNext(t, events)
	if e.Frame == nil || e.Frame.Disposition != Decoded || e.Frame.Memory.Name != `Local\arbitrary-name` || e.Frame.Descriptor != nil {
		t.Fatal(e)
	}
	if _, err := tr.Notify(note()); err != nil {
		t.Fatal(err)
	}
	host.SetReadDeadline(time.Now().Add(time.Second))
	var got RawRecord
	if _, err := io.ReadFull(host, got[:]); err != nil {
		t.Fatal(err)
	}
	want := notification(t, 1)
	if got != want {
		t.Fatalf("record differs: %x", got)
	}
	if n, err := windows.GetsockoptInt(tr.fd, windows.IPPROTO_TCP, windows.TCP_NODELAY); err != nil || n != 1 {
		t.Fatal(n, err)
	}
}

func TestWindowsBadTokenEcho(t *testing.T) {
	for _, tokens := range [][]string{nil, {strings.Repeat("f", 32)}, {winToken, winToken}} {
		tr, _, _ := winStart(t, Config{})
		if err := tr.HandshakeComplete(1, 0, tokens...); !errors.Is(err, ErrHandshake) {
			t.Fatal(err)
		}
		term := winTerminal(t, tr)
		if term.Reason != "handshake_mismatch" || term.Fault != nil {
			t.Fatal(term)
		}
		if _, err := tr.Notify(note()); !errors.Is(err, ErrClosed) {
			t.Fatal(err)
		}
	}
}

func TestWindowsPartialEOFAndCoalescing(t *testing.T) {
	tr, host, events := winStart(t, Config{})
	winReady(t, tr)
	a := notification(t, 1)
	b := notification(t, 2)
	stream := append(append(append([]byte{}, a[:]...), b[:]...), []byte("partial")...)
	winSend(t, host, stream)
	host.CloseWrite()
	for i := uint32(1); i <= 2; i++ {
		e := winNext(t, events)
		if e.Frame == nil || e.Frame.Header.ID != i {
			t.Fatal(e)
		}
	}
	term := winTerminal(t, tr)
	if term.Reason != "peer_gone" || term.Fault != nil || string(term.Capture) != "partial" {
		t.Fatal(term)
	}
	if tr.Stats().Received != 2 {
		t.Fatal("partial window counted as frame")
	}
}

func TestWindowsFaultDoesNotReadNextFrame(t *testing.T) {
	tr, host, events := winStart(t, Config{})
	winReady(t, tr)
	a := notification(t, 1)
	a[108] ^= 1
	b := notification(t, 2)
	winSend(t, host, append(a[:], b[:]...))
	term := winTerminal(t, tr)
	if term.Fault == nil || term.Fault.Class != "bad_crc" || len(term.Fault.Capture) != 112 {
		t.Fatal(term)
	}
	if e := winNext(t, events); e.Frame == nil || e.Frame.Disposition != FramingRejected {
		t.Fatal(e)
	}
	if e := winNext(t, events); e.Terminal == nil {
		t.Fatal(e)
	}
	if tr.Stats().Received != 1 {
		t.Fatal("received after fault")
	}
}

func TestWindowsLifecycleAndStopWhileHandlerBlocks(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	events := make(chan Event, 16)
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	defer unblock()
	tr, host, _ := winStart(t, Config{Handler: func(e Event) {
		events <- e
		if e.Frame != nil && e.Frame.Input != nil {
			close(entered)
			<-release
		}
	}})
	winReady(t, tr)
	tr.ToolActivated(1)
	tr.EpochAcknowledged(0, 7)
	r := inputRecord(t, opening(), 1)
	winSend(t, host, r[:])
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("handler entry timeout")
	}
	if err := tr.EpochAcknowledged(0, 8); err != nil {
		t.Fatal(err)
	}
	tr.Stop()
	term := winTerminal(t, tr)
	if term.Reason != "local_stop" {
		t.Fatal(term)
	}
	unblock()
	if e := winNext(t, events); e.Frame == nil {
		t.Fatal(e)
	}
	if e := winNext(t, events); e.Cancellation == nil || e.Cancellation.Opening.Epoch != 7 {
		t.Fatal(e)
	}
	if e := winNext(t, events); e.Terminal == nil {
		t.Fatal(e)
	}
}

func TestWindowsIdleLifecycleWake(t *testing.T) {
	tr, host, events := winStart(t, Config{})
	winReady(t, tr)
	tr.ToolActivated(1)
	tr.EpochAcknowledged(0, 7)
	r := inputRecord(t, opening(), 1)
	winSend(t, host, r[:])
	winNext(t, events)
	if err := tr.ToolRetired(1); err != nil {
		t.Fatal(err)
	}
	if e := winNext(t, events); e.Cancellation == nil {
		t.Fatal(e)
	}
}

func TestWindowsSendClassifications(t *testing.T) {
	for _, tc := range []struct {
		name          string
		n             int
		err           error
		reason, class string
	}{
		{"short", 17, nil, "fault", "short_write"},
		{"ambiguous", 0, windows.WSAEINVAL, "fault", "ambiguous_send"},
		{"pending is not refusal", 0, windows.ERROR_IO_PENDING, "fault", "ambiguous_send"},
		{"local shutdown", 0, windows.WSAESHUTDOWN, "locally_closed", ""},
		{"peer reset", 0, windows.WSAECONNRESET, "peer_gone", ""},
		{"peer abort", 0, windows.WSAECONNABORTED, "peer_gone", ""},
		{"would block", 0, windows.WSAEWOULDBLOCK, "", ""},
		{"no buffers", 0, windows.WSAENOBUFS, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tr, _, _ := winStart(t, Config{})
			winReady(t, tr)
			calls := 0
			tr.mu.Lock()
			tr.sendRecord = func(windows.Handle, []byte) (int, error) { calls++; return tc.n, tc.err }
			tr.mu.Unlock()
			result, _ := tr.Notify(note())
			if result != Dropped || calls != 1 {
				t.Fatal(result, calls)
			}
			if tc.reason == "" {
				if tr.Stats().Refused != 1 {
					t.Fatal(tr.Stats())
				}
				return
			}
			term := winTerminal(t, tr)
			if term.Reason != tc.reason {
				t.Fatal(term)
			}
			if tc.class != "" && (term.Fault == nil || term.Fault.Class != tc.class || term.Fault.Ordinal != nil || term.Fault.Capture != nil) {
				t.Fatal(term)
			}
			tr.Notify(note())
			if calls != 1 {
				t.Fatal("write after terminal")
			}
		})
	}
}

func TestWindowsABIAndNonblockingReceive(t *testing.T) {
	var p wsaPollFD
	want := uintptr(8)
	if unsafe.Sizeof(windows.Handle(0)) == 8 {
		want = 16
	}
	if unsafe.Sizeof(p) != want || unsafe.Offsetof(p.events) != unsafe.Sizeof(p.fd) {
		t.Fatal("WSAPOLLFD ABI")
	}
	tr, _, _ := winStart(t, Config{})
	// With no incoming traffic, a synchronous receive must refuse immediately.
	tr.mu.Lock()
	var b [1]byte
	_, err := recvSocket(tr.fd, b[:])
	tr.mu.Unlock()
	if !wouldBlock(err) {
		t.Fatal("socket is not nonblocking", err)
	}
	if locallyClosed(windows.WSAECONNRESET) || peerGone(windows.WSAESHUTDOWN) {
		t.Fatal("local/peer classification")
	}
}

func TestWindowsCancelledSetup(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if tr, err := Dial(ctx, "127.0.0.1:1", winToken, Config{Handler: func(Event) {}}); err == nil || tr != nil {
		t.Fatal(tr, err)
	}
	if tr, err := Dial(context.Background(), "127.0.0.1:1", winToken, Config{Handler: func(Event) {}, SetupTimeout: -1}); err == nil || tr != nil {
		t.Fatal(tr, err)
	}
}

func TestWindowsInterruptedSendAndBurnedID(t *testing.T) {
	tr, host, _ := winStart(t, Config{})
	winReady(t, tr)
	var attempted RawRecord
	calls := 0
	tr.mu.Lock()
	tr.sendRecord = func(_ windows.Handle, b []byte) (int, error) {
		calls++
		if calls == 1 {
			copy(attempted[:], b)
			return 0, windows.WSAEINTR
		}
		if string(attempted[:]) != string(b) {
			t.Error("EINTR changed the record")
		}
		return 0, windows.WSAEWOULDBLOCK
	}
	tr.mu.Unlock()
	if result, err := tr.Notify(note()); result != Dropped || err != nil || calls != 2 {
		t.Fatal(result, err, calls)
	}
	tr.mu.Lock()
	tr.sendRecord = sendSocket
	tr.mu.Unlock()
	if result, err := tr.Notify(note()); result != Sent || err != nil {
		t.Fatal(result, err)
	}
	host.SetReadDeadline(time.Now().Add(time.Second))
	var got RawRecord
	if _, err := io.ReadFull(host, got[:]); err != nil {
		t.Fatal(err)
	}
	if want := notification(t, 2); got != want {
		t.Fatal("refusal did not burn its ID")
	}
}

func TestWindowsReceiveErrorClassifications(t *testing.T) {
	for _, tc := range []struct {
		err    error
		reason string
	}{
		{windows.WSAESHUTDOWN, "locally_closed"},
		{windows.WSAECONNRESET, "peer_gone"},
		{windows.WSAECONNABORTED, "peer_gone"},
		{windows.WSAENOTCONN, "peer_gone"},
		{windows.WSAENOBUFS, "fault"}, // A send refusal is not a receive refusal.
		{windows.WSAEINVAL, "fault"},
	} {
		t.Run(tc.err.Error(), func(t *testing.T) {
			tr, _, _ := winStart(t, Config{})
			tr.mu.Lock()
			copy(tr.partial.tail(), "part")
			tr.partial.advance(4)
			tr.receiveErrorLocked(tc.err)
			tr.mu.Unlock()
			term := winTerminal(t, tr)
			if term.Reason != tc.reason || string(term.Capture) != "part" {
				t.Fatal(term)
			}
			if tc.reason == "fault" {
				if term.Fault == nil || term.Fault.Class != "recv_error" || term.Fault.Ordinal == nil || *term.Fault.Ordinal != 1 || term.Fault.Capture != nil || term.Fault.OSError == nil {
					t.Fatal(term)
				}
			} else if term.Fault != nil || term.OSError == nil {
				t.Fatal(term)
			}
		})
	}
}

func TestWindowsNativeLocalShutdown(t *testing.T) {
	tr, _, _ := winStart(t, Config{})
	winReady(t, tr)
	tr.mu.Lock()
	err := windows.Shutdown(tr.fd, windows.SHUT_WR)
	tr.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	tr.Notify(note())
	term := winTerminal(t, tr)
	if term.Reason != "locally_closed" || term.Fault != nil {
		t.Fatal(term)
	}
}

func TestWindowsNativePeerReset(t *testing.T) {
	tr, host, _ := winStart(t, Config{})
	if err := host.SetLinger(0); err != nil {
		t.Fatal(err)
	}
	host.Close()
	term := winTerminal(t, tr)
	if term.Reason != "peer_gone" || term.Fault != nil {
		t.Fatal(term)
	}
}

func TestWindowsWakeFailureIsLocal(t *testing.T) {
	tr, _, _ := winStart(t, Config{})
	tr.mu.Lock()
	err := windows.Shutdown(tr.wake[1], windows.SHUT_WR)
	tr.mu.Unlock()
	if err != nil {
		t.Fatal(err)
	}
	term := winTerminal(t, tr)
	if term.Reason != "wake_error" || term.Fault != nil {
		t.Fatal(term)
	}
}
