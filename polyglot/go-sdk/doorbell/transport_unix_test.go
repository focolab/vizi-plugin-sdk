//go:build linux || darwin

package doorbell

import (
	"context"
	"errors"
	"fmt"
	"os"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"testing"
	"time"
	"unsafe"

	"golang.org/x/sys/unix"
)

func pair(t *testing.T) [2]int {
	t.Helper()
	p, err := unix.Socketpair(unix.AF_UNIX, socketType, 0)
	if err != nil {
		t.Fatal(err)
	}
	for _, fd := range p {
		unix.CloseOnExec(fd)
	}
	return p
}
func start(t *testing.T, cfg Config) (*Transport, int, chan Event) {
	t.Helper()
	p := pair(t)
	events := make(chan Event, 4096)
	if cfg.Handler == nil {
		cfg.Handler = func(e Event) { events <- e }
	}
	tr, err := Adopt(p[0], cfg)
	if err != nil {
		unix.Close(p[1])
		t.Fatal(err)
	}
	t.Cleanup(func() {
		tr.Stop()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := tr.Wait(ctx); err != nil {
			t.Error(err)
		}
		unix.Close(p[1])
	})
	return tr, p[1], events
}
func next(t *testing.T, ch <-chan Event) Event {
	t.Helper()
	select {
	case e := <-ch:
		return e
	case <-time.After(2 * time.Second):
		t.Fatal("event timeout")
	}
	return Event{}
}
func terminal(t *testing.T, tr *Transport) Terminal {
	t.Helper()
	select {
	case e, ok := <-tr.Terminal():
		if !ok {
			t.Fatal("terminal missing")
		}
		return e
	case <-time.After(2 * time.Second):
		t.Fatal("terminal timeout")
	}
	return Terminal{}
}
func send(t *testing.T, fd int, b []byte, fds ...int) {
	t.Helper()
	var oob []byte
	if len(fds) > 0 {
		oob = unix.UnixRights(fds...)
	}
	for {
		n, err := unix.SendmsgN(fd, b, oob, nil, sendFlags)
		if errors.Is(err, unix.EINTR) {
			continue
		}
		if err != nil || n != len(b) {
			t.Fatalf("send %d/%d: %v", n, len(b), err)
		}
		return
	}
}
func recvPeer(t *testing.T, fd int) []byte {
	t.Helper()
	poll := []unix.PollFd{{Fd: int32(fd), Events: unix.POLLIN}}
	n, err := unix.Poll(poll, 1000)
	if err != nil || n == 0 {
		t.Fatalf("peer receive readiness: %d %v", n, err)
	}
	var b [120]byte
	n, _, _, _, err = unix.Recvmsg(fd, b[:], nil, 0)
	if err != nil {
		t.Fatal(err)
	}
	return append([]byte{}, b[:n]...)
}
func arrange(t *testing.T, tr *Transport) {
	t.Helper()
	if err := tr.HandshakeComplete(1, 0); err != nil {
		t.Fatal(err)
	}
	if err := tr.ToolActivated(1); err != nil {
		t.Fatal(err)
	}
	if err := tr.EpochAcknowledged(0, 7); err != nil {
		t.Fatal(err)
	}
}
func pipe(t *testing.T) (int, int) {
	t.Helper()
	var p [2]int
	if err := unix.Pipe(p[:]); err != nil {
		t.Fatal(err)
	}
	if err := unix.SetNonblock(p[0], true); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { unix.Close(p[0]) })
	return p[0], p[1]
}
func noWriter(t *testing.T, read int) {
	t.Helper()
	var b [1]byte
	n, err := unix.Read(read, b[:])
	if n != 0 || err != nil {
		t.Fatalf("SCM_RIGHTS writer leaked: read=%d err=%v", n, err)
	}
}

func TestFromEnvStrandedDoorbell(t *testing.T) {
	for _, tc := range []struct {
		name   string
		closed bool
		want   error
	}{
		{"closed", true, unix.EBADF},
		{"not_socket", false, unix.ENOTSOCK},
	} {
		t.Run(tc.name, func(t *testing.T) {
			fd, err := unix.Open(os.DevNull, unix.O_RDONLY|unix.O_CLOEXEC, 0)
			if err != nil {
				t.Fatal(err)
			}
			t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(fd))
			if tc.closed {
				if err := unix.Close(fd); err != nil {
					t.Fatal(err)
				}
			}
			tr, err := FromEnv(Config{Handler: func(Event) {}})
			if tr != nil {
				tr.Stop()
				t.Fatal("adopted stranded descriptor")
			}
			if !errors.Is(err, tc.want) {
				t.Fatalf("lost errno %v: %v", tc.want, err)
			}
			for _, part := range []string{fmt.Sprintf("doorbell fd %d", fd), "inherited", "launcher", "exec the plugin", "pass it through"} {
				if !strings.Contains(err.Error(), part) {
					t.Errorf("missing guidance %q: %v", part, err)
				}
			}
			if _, err := unix.FcntlInt(uintptr(fd), unix.F_GETFD, 0); !errors.Is(err, unix.EBADF) {
				unix.Close(fd)
				t.Fatal("failed adoption leaked fd", err)
			}
		})
	}
}

func TestSetupAndHandshake(t *testing.T) {
	p, err := unix.Socketpair(unix.AF_UNIX, unix.SOCK_STREAM, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(p[1])
	if _, err = Adopt(p[0], Config{Handler: func(Event) {}}); err == nil {
		t.Fatal("accepted stream on Unix")
	}
	if _, err = unix.FcntlInt(uintptr(p[0]), unix.F_GETFD, 0); !errors.Is(err, unix.EBADF) {
		t.Fatal("failed adoption leaked fd")
	}
	tr, peer, _ := start(t, Config{})
	fdFlags, err := unix.FcntlInt(uintptr(tr.fd), unix.F_GETFD, 0)
	if err != nil || fdFlags&unix.FD_CLOEXEC == 0 {
		t.Fatal("inherited fd remains inheritable")
	}
	status, _ := unix.FcntlInt(uintptr(tr.fd), unix.F_GETFL, 0)
	if status&unix.O_NONBLOCK == 0 {
		t.Fatal("blocking socket")
	}
	if _, err = tr.Notify(note()); !errors.Is(err, ErrHandshake) {
		t.Fatal(err)
	}
	if err = tr.HandshakeComplete(0, 12); !errors.Is(err, ErrHandshake) {
		t.Fatal(err)
	}
	if err = tr.HandshakeComplete(1, 0); err != nil {
		t.Fatal(err)
	}
	if result, err := tr.Notify(note()); err != nil || result != Sent {
		t.Fatal(result, err)
	}
	b := recvPeer(t, peer)
	if len(b) != 112 || le.Uint32(b[4:8]) != 1 {
		t.Fatalf("outbound %x", b)
	}
	tr.Stop()
	term := terminal(t, tr)
	if term.Reason != "local_stop" || term.Fault != nil {
		t.Fatal(term)
	}
	if _, err = tr.Notify(note()); !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}

func TestRealSocketFramingFaults(t *testing.T) {
	for _, tc := range []struct {
		name  string
		bytes func(*testing.T) []byte
		class string
	}{
		{"short", func(t *testing.T) []byte { return []byte{1} }, "bad_length"},
		{"oversized", func(t *testing.T) []byte { r := notification(t, 1); return append(r[:], make([]byte, 100)...) }, "bad_length"},
		{"crc", func(t *testing.T) []byte { r := notification(t, 1); r[10] ^= 1; return r[:] }, "bad_crc"},
		{"padding", func(t *testing.T) []byte { r := notification(t, 1); r[85] = 1; checksum(&r); return r[:] }, "bad_padding"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tr, peer, events := start(t, Config{})
			send(t, peer, tc.bytes(t))
			term := terminal(t, tr)
			if term.Fault == nil || term.Fault.Class != tc.class {
				t.Fatalf("%+v", term)
			}
			wantTransport := "seqpacket"
			if socketType == unix.SOCK_DGRAM {
				wantTransport = "dgram"
			}
			if term.Fault.Transport != wantTransport {
				t.Fatalf("wire transport class %q, want %q", term.Fault.Transport, wantTransport)
			}
			first := next(t, events)
			if first.Frame == nil || first.Frame.Disposition != FramingRejected {
				t.Fatal(first)
			}
			if last := next(t, events); last.Terminal == nil {
				t.Fatal(last)
			}
			tr.Stop()
			if _, ok := <-tr.Terminal(); ok {
				t.Fatal("second terminal")
			}
			if len(term.Fault.Capture) > 120 {
				t.Fatal("unbounded capture")
			}
		})
	}
}

func TestDescriptorCleanupOnAllDiscards(t *testing.T) {
	for _, kind := range []string{"unknown", "bad_crc", "short", "extra", "ctrunc", "rejected", "unclaimed"} {
		t.Run(kind, func(t *testing.T) {
			tr, peer, events := start(t, Config{})
			r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
			data := r[:]
			count := 1
			terminalExpected := false
			switch kind {
			case "unknown":
				r[0] = 0x99
				checksum(&r)
			case "bad_crc":
				r[108] ^= 1
				terminalExpected = true
			case "short":
				data = data[:3]
				terminalExpected = true
			case "extra":
				count = 2
				terminalExpected = true
			case "ctrunc":
				count = 8
				terminalExpected = true
			case "rejected":
				le.PutUint64(r[16:], 1)
				checksum(&r)
			}
			var readers, writers []int
			for i := 0; i < count; i++ {
				a, b := pipe(t)
				readers = append(readers, a)
				writers = append(writers, b)
			}
			send(t, peer, data, writers...)
			for _, w := range writers {
				unix.Close(w)
			}
			if terminalExpected {
				term := terminal(t, tr)
				if term.Fault == nil {
					t.Fatal(term)
				}
				if kind == "ctrunc" && term.Fault.Class != "ctrunc" {
					// The rule is that rights the buffer cannot hold end the
					// session; protocol section 7 rule 3 explicitly allows
					// receivers to reach that outcome "by different routes".
					// Darwin can cut the control buffer mid-header, so the
					// rights fail to parse and the route is recv_error rather
					// than ctrunc. Requiring the label would make the same
					// traffic conforming on one platform and not the other.
					if socketType != unix.SOCK_DGRAM || term.Fault.Class != "recv_error" {
						t.Fatal(term.Fault)
					}
				}
				if kind == "extra" && term.Fault.Class != "wrong_fd_count" {
					t.Fatal(term.Fault)
				}
			} else {
				barrier := notification(t, 2)
				send(t, peer, barrier[:])
			}
			first := next(t, events)
			if kind != "unclaimed" && first.Frame.Descriptor != nil {
				t.Fatal("discard exposed fd")
			}
			next(t, events) // next emit proves prior callback cleanup has completed
			if kind == "ctrunc" && socketType == unix.SOCK_DGRAM {
				// OPEN, needs a Darwin machine to settle. Rights that overflow
				// the control buffer are discarded by the Linux kernel, so
				// nothing is installed and nothing leaks. On Darwin the strict
				// parse fails (the header appears to claim more than was
				// written), and any descriptor that WAS installed cannot be
				// enumerated, so it cannot be closed. Recovering them means
				// reading fd numbers out of a buffer we have already decided
				// is malformed, and closing a wrongly derived number could
				// take out the doorbell socket itself — the double close
				// protocol section 8 exists to prevent. The session is ending
				// either way; the descriptors are released at process exit.
				t.Skip("Darwin may install descriptors a truncated control buffer cannot name")
			}
			for _, read := range readers {
				noWriter(t, read)
			}
			if kind == "unclaimed" {
				if _, err := first.Frame.Descriptor.Claim(); err == nil {
					t.Fatal("late claim succeeded")
				}
			}
		})
	}
}

func TestClaimedGrantMapsSharedMemory(t *testing.T) {
	claimed := make(chan *os.File, 1)
	tr, peer, events := start(t, Config{Handler: func(e Event) {
		if e.Frame != nil && e.Frame.Descriptor != nil {
			f, err := e.Frame.Descriptor.Claim()
			if err != nil {
				panic(err)
			}
			claimed <- f
		}
	}})
	_ = events
	backing, err := os.CreateTemp(t.TempDir(), "shared")
	if err != nil {
		t.Fatal(err)
	}
	defer backing.Close()
	if err = backing.Truncate(4096); err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(backing.Name()); err != nil {
		t.Fatal(err)
	} // capability survives unlink on Linux and Darwin
	host, err := unix.Mmap(int(backing.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Munmap(host)
	copy(host[32:], []byte("host state"))
	r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 4096, Permissions: PluginRead | PluginWrite | HostRead | HostWrite}, 1)
	send(t, peer, r[:], int(backing.Fd()))
	var granted *os.File
	select {
	case granted = <-claimed:
	case <-time.After(time.Second):
		t.Fatal("grant timeout")
	}
	defer granted.Close()
	flags, err := unix.FcntlInt(granted.Fd(), unix.F_GETFD, 0)
	if err != nil || flags&unix.FD_CLOEXEC == 0 {
		t.Fatal("granted fd inheritable")
	}
	plugin, err := unix.Mmap(int(granted.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Munmap(plugin)
	if string(plugin[32:42]) != "host state" {
		t.Fatal("not shared")
	}
	copy(plugin[32:], []byte("reply data"))
	if string(host[32:42]) != "reply data" {
		t.Fatal("write not shared")
	}
	tr.Stop()
	if _, err = granted.Stat(); err != nil {
		t.Fatal("transport closed claimed file", err)
	}
}

func TestWakeOrderingAndStopWithBlockedHandler(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	var releaseOnce sync.Once
	releaseHandler := func() { releaseOnce.Do(func() { close(release) }) }
	events := make(chan Event, 16)
	tr, peer, _ := start(t, Config{Handler: func(e Event) {
		events <- e
		if e.Frame != nil && e.Frame.Input != nil && e.Frame.Input.Phase == Press {
			close(entered)
			<-release
		}
	}})
	defer releaseHandler()
	arrange(t, tr)
	r := inputRecord(t, opening(), 1)
	send(t, peer, r[:])
	<-entered
	if err := tr.ToolRetired(1); err != nil {
		t.Fatal(err)
	} // cannot wait on blocked callback
	stopped := make(chan struct{})
	go func() { tr.Stop(); close(stopped) }()
	select {
	case <-stopped:
	case <-time.After(time.Second):
		t.Fatal("stop joined handler")
	}
	term := terminal(t, tr)
	if term.Reason != "local_stop" {
		t.Fatal(term)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Millisecond)
	defer cancel()
	if err := tr.Wait(ctx); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatal("Wait should remain blocked", err)
	}
	if err := tr.ToolActivated(2); !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
	// Callback sequence is opening, cancellation, terminal; the mailbox above
	// already accepted termination independently of this sequence's progress.
	if e := next(t, events); e.Frame == nil {
		t.Fatal(e)
	}
	releaseHandler()
	if e := next(t, events); e.Cancellation == nil || e.Cancellation.Opening.Interaction != 1 {
		t.Fatal(e)
	}
	if e := next(t, events); e.Terminal == nil {
		t.Fatal(e)
	}
}

func TestLifecycleWakesIdleListener(t *testing.T) {
	tr, peer, events := start(t, Config{})
	arrange(t, tr)
	r := inputRecord(t, opening(), 1)
	send(t, peer, r[:])
	next(t, events)
	if err := tr.EpochAcknowledged(0, 8); err != nil {
		t.Fatal(err)
	}
	e := next(t, events)
	if e.Cancellation == nil || e.Cancellation.Opening.Epoch != 7 {
		t.Fatal(e)
	}
	// A repeated ack cannot close a gesture opened since the first ack.
	in := opening()
	in.Epoch = 8
	in.Interaction = 2
	r = inputRecord(t, in, 2)
	send(t, peer, r[:])
	next(t, events)
	if err := tr.EpochAcknowledged(0, 8); err != nil {
		t.Fatal(err)
	}
	if err := tr.EpochAcknowledged(0, 7); !errors.Is(err, ErrLifecycle) {
		t.Fatal(err)
	}
	in.Phase = Release
	in.Sequence = 1
	r = inputRecord(t, in, 3)
	send(t, peer, r[:])
	if e = next(t, events); e.Frame == nil || e.Frame.Disposition != Decoded {
		t.Fatal(e)
	}
	if err := tr.ToolActivated(10); err != nil {
		t.Fatal(err)
	}
	if err := tr.ToolRetired(9); err != nil {
		t.Fatal("gap must count as retired", err)
	}
	if err := tr.ToolActivated(9); !errors.Is(err, ErrLifecycle) {
		t.Fatal(err)
	}
}

func TestFaultCancelsOpenInteractionsAndRefusesReentrantSend(t *testing.T) {
	var tr *Transport
	sent := make(chan error, 1)
	events := make(chan Event, 16)
	tr, peer, _ := start(t, Config{Handler: func(e Event) {
		events <- e
		if e.Terminal != nil {
			_, err := tr.Notify(note())
			sent <- err
		}
	}})
	arrange(t, tr)
	r := inputRecord(t, opening(), 1)
	send(t, peer, r[:])
	next(t, events)
	r = notification(t, 2)
	r[108] ^= 1
	send(t, peer, r[:])
	term := terminal(t, tr)
	if term.Fault.Class != "bad_crc" {
		t.Fatal(term)
	}
	if e := next(t, events); e.Frame == nil || e.Frame.Disposition != FramingRejected {
		t.Fatal(e)
	}
	if e := next(t, events); e.Cancellation == nil || e.Cancellation.Opening.Interaction != 1 {
		t.Fatal(e)
	}
	if e := next(t, events); e.Terminal == nil {
		t.Fatal(e)
	}
	if err := <-sent; !errors.Is(err, ErrClosed) {
		t.Fatal(err)
	}
}

func TestHandlerPanicClosesOnlyUnclaimedDescriptor(t *testing.T) {
	for _, claim := range []bool{false, true} {
		t.Run(fmt.Sprint(claim), func(t *testing.T) {
			var file *os.File
			tr, peer, _ := start(t, Config{Handler: func(e Event) {
				if e.Frame != nil && e.Frame.Descriptor != nil {
					if claim {
						var err error
						file, err = e.Frame.Descriptor.Claim()
						if err != nil {
							panic(err)
						}
					}
					panic("consumer bug")
				}
			}})
			read, write := pipe(t)
			r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
			send(t, peer, r[:], write)
			unix.Close(write)
			term := terminal(t, tr)
			if term.Reason != "handler_panic" || term.Fault != nil {
				t.Fatal(term)
			}
			if claim {
				if file == nil {
					t.Fatal("claim failed")
				}
				if _, err := file.Stat(); err != nil {
					t.Fatal(err)
				}
				file.Close()
			}
			noWriter(t, read)
		})
	}
}

func TestBackpressureBurnsIDsWithoutEscalation(t *testing.T) {
	tr, peer, _ := start(t, Config{})
	tr.HandshakeComplete(1, 0)
	// Fill until a clean refusal, with a strict test bound independent of stock
	// buffer depth. No receiver drain occurs during this loop.
	var count int
	for ; count < 100000; count++ {
		result, err := tr.Notify(note())
		if err != nil {
			t.Fatal(err)
		}
		if result == Dropped {
			break
		}
	}
	if count == 100000 {
		t.Fatal("did not reach backpressure")
	}
	if tr.Stats().Refused != 1 {
		t.Fatal(tr.Stats())
	}
	select {
	case term := <-tr.Terminal():
		t.Fatal("D refusal escalated", term)
	default:
	}
	// Drain all accepted records; no retry of the refused record may appear.
	for i := 0; i < count; i++ {
		b := recvPeer(t, peer)
		if le.Uint32(b[4:8]) != uint32(i+1) {
			t.Fatal("id order")
		}
	}
	if result, err := tr.Notify(note()); err != nil || result != Sent {
		t.Fatal(result, err)
	}
	b := recvPeer(t, peer)
	if le.Uint32(b[4:8]) != uint32(count+2) {
		t.Fatal("refusal did not burn id")
	}
}

func TestConcurrentSendOrderAndWrap(t *testing.T) {
	tr, peer, _ := start(t, Config{})
	tr.HandshakeComplete(1, 0)
	tr.mu.Lock()
	tr.nextID = 0xfffffff0
	tr.mu.Unlock()
	var wg sync.WaitGroup
	for i := 0; i < 16; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			if r, err := tr.Notify(note()); err != nil || r != Sent {
				t.Errorf("%v %v", r, err)
			}
		}()
	}
	wg.Wait()
	for i := uint32(0); i < 16; i++ {
		b := recvPeer(t, peer)
		if le.Uint32(b[4:8]) != 0xfffffff0+i {
			t.Fatal("send order")
		}
	}
	if _, err := tr.Notify(note()); err != nil {
		t.Fatal(err)
	}
	b := recvPeer(t, peer)
	if le.Uint32(b[4:8]) != 0 {
		t.Fatal("skipped zero on wrap")
	}
}

func TestInjectedSendOutcomes(t *testing.T) {
	for _, tc := range []struct {
		name          string
		count         int
		err           error
		class, reason string
	}{
		{"short", 5, nil, "short_write", "fault"},
		{"ambiguous", 0, unix.EIO, "ambiguous_send", "fault"},
		{"peer reset", 0, unix.ECONNRESET, "", "peer_gone"},
		{"no buffers", 0, unix.ENOBUFS, "", ""},
		{"user fd budget", 0, unix.ETOOMANYREFS, "", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tr, _, _ := start(t, Config{})
			tr.HandshakeComplete(1, 0)
			calls := 0
			tr.mu.Lock()
			tr.sendRecord = func(int, []byte) (int, error) { calls++; return tc.count, tc.err }
			tr.mu.Unlock()
			result, _ := tr.Notify(note())
			if result != Dropped || calls != 1 {
				t.Fatal(result, calls)
			}
			if tc.reason != "" {
				term := terminal(t, tr)
				if term.Reason != tc.reason {
					t.Fatal(term)
				}
				if tc.class != "" {
					if term.Fault.Class != tc.class || term.Fault.Capture != nil || term.Fault.Ordinal != nil || term.Fault.ExpectedCRC != nil || term.Fault.ControlTruncated != nil {
						t.Fatal(term.Fault)
					}
				}
				tr.Notify(note())
				if calls != 1 {
					t.Fatal("send after terminal")
				}
			} else if tr.Stats().Refused != 1 {
				t.Fatal(tr.Stats())
			}
		})
	}
	t.Run("interrupted", func(t *testing.T) {
		tr, peer, _ := start(t, Config{})
		tr.HandshakeComplete(1, 0)
		tr.mu.Lock()
		real := tr.sendRecord
		calls := 0
		var first RawRecord
		tr.sendRecord = func(fd int, b []byte) (int, error) {
			calls++
			if calls == 1 {
				copy(first[:], b)
				return 0, unix.EINTR
			}
			var second RawRecord
			copy(second[:], b)
			if second != first {
				t.Error("EINTR changed record")
			}
			return real(fd, b)
		}
		tr.mu.Unlock()
		result, err := tr.Notify(note())
		if result != Sent || err != nil || calls != 2 {
			t.Fatal(result, err, calls)
		}
		recvPeer(t, peer)
	})
}

func TestReceiveErrorClassifier(t *testing.T) {
	for _, err := range []error{unix.EPIPE, unix.ECONNREFUSED, unix.ECONNRESET, unix.EDESTADDRREQ, unix.ENOTCONN} {
		if !peerGone(err) {
			t.Fatal(err)
		}
	}
	for _, err := range []error{unix.EAGAIN, unix.EWOULDBLOCK} {
		if !wouldBlock(err) {
			t.Fatal(err)
		}
	}
	if peerGone(unix.EIO) || wouldBlock(unix.ENOBUFS) {
		t.Fatal("ambiguous error ignored")
	}
}

// x/sys/unix.SendmsgN silently inserts a payload byte for empty rights sends
// (including Linux seqpacket), so it cannot produce an empty record. Send
// the actual zero-byte record directly instead.
func sendEmptyRights(t *testing.T, fd int, fds []int) {
	t.Helper()
	oob := unix.UnixRights(fds...)
	msg := unix.Msghdr{Control: &oob[0]}
	msg.SetControllen(len(oob))
	for {
		n, _, errno := unix.Syscall(unix.SYS_SENDMSG, uintptr(fd), uintptr(unsafe.Pointer(&msg)), uintptr(sendFlags))
		runtime.KeepAlive(oob)
		if errno == unix.EINTR {
			continue
		}
		if runtime.GOOS == "darwin" && errno == unix.EMSGSIZE {
			t.Skip("Darwin rejects zero-length SCM_RIGHTS datagrams with EMSGSIZE; a peer cannot produce this D6 frame")
		}
		if errno != 0 || n != 0 {
			t.Fatalf("empty rights send: n=%d errno=%v", n, errno)
		}
		return
	}
}

func TestZeroReadAndPeerClose(t *testing.T) {
	for _, tc := range []struct {
		name  string
		fds   int
		class string
	}{
		{"one right", 1, "bad_length"},
		{"two rights", 2, "wrong_fd_count"},
		{"truncated rights", 8, "ctrunc"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			tr, peer, _ := start(t, Config{})
			var readers, writers []int
			defer func() {
				// A failed or skipped send still owns the original pipe writers.
				for _, w := range writers {
					unix.Close(w)
				}
			}()
			for i := 0; i < tc.fds; i++ {
				r, w := pipe(t)
				readers = append(readers, r)
				writers = append(writers, w)
			}
			sendEmptyRights(t, peer, writers)
			for _, w := range writers {
				unix.Close(w)
			}
			writers = nil
			term := terminal(t, tr)
			if term.Fault == nil || term.Fault.Class != tc.class {
				t.Fatalf("empty record with %d rights: %+v", tc.fds, term)
			}
			if term.Fault.ObservedLength == nil || *term.Fault.ObservedLength != 0 || term.Fault.Ordinal == nil || *term.Fault.Ordinal != 1 {
				t.Fatalf("missing empty-frame evidence: %+v", term.Fault)
			}
			if tc.class == "ctrunc" && socketType == unix.SOCK_DGRAM {
				// As in TestDescriptorCleanupOnAllDiscards, Darwin may not
				// return parseable rights when its control buffer truncates.
				t.Skip("Darwin may install descriptors a truncated control buffer cannot name")
			}
			for _, r := range readers {
				noWriter(t, r)
			}
		})
	}
	t.Run("empty datagram", func(t *testing.T) {
		tr, peer, _ := start(t, Config{})
		send(t, peer, []byte{})
		term := terminal(t, tr)
		if socketType == unix.SOCK_DGRAM {
			if term.Fault == nil || term.Fault.Class != "bad_length" {
				t.Fatal(term)
			}
		} else if term.Reason != "peer_gone" || term.Fault != nil {
			t.Fatal(term)
		}
	})
	t.Run("peer close", func(t *testing.T) {
		if socketType == unix.SOCK_DGRAM {
			// macOS announces peer death exactly once, to whoever is reading
			// at that instant, and the socket then goes quiet. Protocol
			// section 8 says teardown is announced by the control plane and
			// the one-shot signal is corroboration; a test that requires it
			// is building the shutdown path this document says not to build.
			t.Skip("a datagram peer close is not observable on the doorbell")
		}
		p := pair(t)
		events := make(chan Event, 8)
		tr, err := Adopt(p[0], Config{Handler: func(e Event) { events <- e }})
		if err != nil {
			t.Fatal(err)
		}
		defer tr.Stop()
		unix.Close(p[1])
		term := terminal(t, tr)
		if term.Reason != "peer_gone" || term.Fault != nil {
			t.Fatal(term)
		}
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err = tr.Wait(ctx); err != nil {
			t.Fatal(err)
		}
	})
}

func TestBoundedLifecycleQueueIsTransactional(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	events := make(chan Event, 32)
	tr, peer, _ := start(t, Config{MaxPendingOutcomes: 1, Handler: func(e Event) {
		events <- e
		if e.Frame != nil && e.Frame.Input != nil && e.Frame.Input.Interaction == 2 {
			close(entered)
			<-release
		}
	}})
	defer close(release)
	arrange(t, tr)
	tr.ToolActivated(2)
	e := opening()
	r := inputRecord(t, e, 1)
	send(t, peer, r[:])
	next(t, events)
	e.Tool = 2
	e.Interaction = 2
	r = inputRecord(t, e, 2)
	send(t, peer, r[:])
	<-entered
	if err := tr.CancelInteraction(1); err != nil {
		t.Fatal(err)
	}
	if err := tr.CancelInteraction(2); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	if err := tr.CancelInteraction(999); err != nil {
		t.Fatal("idempotent no-op refused", err)
	}
	if err := tr.EpochAcknowledged(0, 8); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	tr.mu.Lock()
	epoch := tr.input.epochs[0]
	_, live := tr.input.open[2]
	tr.mu.Unlock()
	if epoch != 7 || !live {
		t.Fatal("failed input mutated state")
	}
}

func TestActiveToolLimitDoesNotConsumeHandle(t *testing.T) {
	tr, _, _ := start(t, Config{MaxActiveTools: 1})
	if err := tr.ToolActivated(1); err != nil {
		t.Fatal(err)
	}
	if err := tr.ToolActivated(2); !errors.Is(err, ErrToolLimit) {
		t.Fatal(err)
	}
	if err := tr.ToolActivated(1); err != nil {
		t.Fatal("repeat live activation", err)
	}
	if err := tr.ToolRetired(1); err != nil {
		t.Fatal(err)
	}
	if err := tr.ToolActivated(2); err != nil {
		t.Fatal("refused activation consumed handle", err)
	}
}

func TestSendFaultWhileGrantHandlerBlocked(t *testing.T) {
	entered := make(chan struct{})
	release := make(chan struct{})
	claimed := make(chan *os.File, 1)
	var once sync.Once
	unblock := func() { once.Do(func() { close(release) }) }
	defer unblock()
	tr, peer, _ := start(t, Config{Handler: func(e Event) {
		if e.Frame != nil && e.Frame.Descriptor != nil {
			close(entered)
			<-release
			file, err := e.Frame.Descriptor.Claim()
			if err != nil {
				panic(err)
			}
			claimed <- file
		}
	}})
	tr.HandshakeComplete(1, 0)
	read, write := pipe(t)
	r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
	send(t, peer, r[:], write)
	unix.Close(write)
	<-entered
	tr.mu.Lock()
	tr.sendRecord = func(int, []byte) (int, error) { return 0, unix.EIO }
	tr.mu.Unlock()
	if _, err := tr.Notify(note()); !errors.Is(err, unix.EIO) {
		t.Fatal(err)
	}
	term := terminal(t, tr)
	if term.Fault == nil || term.Fault.Class != "ambiguous_send" {
		t.Fatal(term)
	}
	// The fd already inside a callback must not be closed by the fault path.
	var b [1]byte
	if _, err := unix.Read(read, b[:]); !wouldBlock(err) {
		t.Fatal("fault closed callback descriptor", err)
	}
	unblock()
	var file *os.File
	select {
	case file = <-claimed:
	case <-time.After(time.Second):
		t.Fatal("claim after fault failed")
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := tr.Wait(ctx); err != nil {
		t.Fatal(err)
	}
	if _, err := file.Stat(); err != nil {
		t.Fatal("claimed descriptor double-closed", err)
	}
	file.Close()
	noWriter(t, read)
}
