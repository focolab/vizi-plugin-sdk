//go:build (linux || darwin) && (amd64 || arm64)

package memory

import (
	"bufio"
	"bytes"
	"context"
	"errors"
	"fmt"
	"hash/crc32"
	"math"
	"os"
	"os/exec"
	"reflect"
	"runtime"
	"strconv"
	"sync"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"golang.org/x/sys/unix"
)

const testID uint64 = 1<<32 | 1

type fixture struct {
	file  *os.File
	peer  []byte
	grant doorbell.Memory
	raw   doorbell.RawRecord
	in    InstallSegment
}

func newFixture(t *testing.T, regions ...Region) *fixture {
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "segment")
	if err != nil {
		t.Fatal(err)
	}
	if err = f.Truncate(4096); err != nil {
		t.Fatal(err)
	}
	b, err := unix.Mmap(int(f.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	le.PutUint64(b, testID)
	copy(b[12:28], "identity proof!!")
	le.PutUint32(b[28:32], crc32.ChecksumIEEE(b[:28]))
	g := doorbell.Memory{SegmentID: testID, Size: 4096, Permissions: doorbell.PluginRead | doorbell.PluginWrite | doorbell.HostRead | doorbell.HostWrite}
	raw, err := doorbell.EncodeMemory(g, 17)
	if err != nil {
		t.Fatal(err)
	}
	x := &fixture{f, b, g, raw, InstallSegment{SegmentID: testID, Purpose: "test", Regions: regions}}
	t.Cleanup(func() {
		if err := unix.Munmap(b); err != nil {
			t.Error(err)
		}
		f.Close()
	})
	return x
}
func registry(t *testing.T) *Registry {
	t.Helper()
	r, err := New(Config{Purposes: []string{"test"}})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Error(err)
		}
	})
	return r
}
func (f *fixture) send(t *testing.T, r *Registry) (Update, error) {
	t.Helper()
	fd, err := unix.Dup(int(f.file.Fd()))
	if err != nil {
		t.Fatal(err)
	}
	file := os.NewFile(uintptr(fd), "grant")
	u, err := r.grant(f.raw, f.grant, file)
	if _, e := file.Stat(); e == nil {
		t.Error("granted descriptor remains open")
	}
	return u, err
}
func installFixture(t *testing.T, f *fixture) (*Registry, *Segment) {
	t.Helper()
	r := registry(t)
	if u, err := f.send(t, r); err != nil || u.Ack != nil {
		t.Fatalf("grant: %+v %v", u, err)
	}
	u, err := r.Install(f.in)
	if err != nil || u.Ack == nil || u.Ack.Failure != nil {
		t.Fatalf("install: %+v %v", u, err)
	}
	if !bytes.Equal(u.Ack.Proof[:], f.peer[12:28]) {
		t.Fatal("proof did not come from mapping")
	}
	s, err := r.Segment(testID)
	if err != nil {
		t.Fatal(err)
	}
	return r, s
}
func constantRegion() Region {
	return Region{Index: 0, Offset: 32, Extent: 64, Discipline: Constant, Producer: Host}
}
func pulsedRegion() Region {
	return Region{Index: 0, Offset: 128, Extent: 3 * (16 + 32), Discipline: Pulsed, Producer: Host, Buffers: 3, SlotBytes: 32}
}
func publishFixture(f *fixture, slot uint32, revision uint64, payload []byte) {
	r := f.in.Regions[0]
	state := r.Offset + uint64(slot)*16
	start := r.Offset + uint64(r.Buffers)*16 + uint64(slot)*r.SlotBytes
	copy(f.peer[start:], payload)
	le.PutUint64(f.peer[state+8:], uint64(len(payload)))
	storeState(f.peer, state, revision)
}

func TestBothOrdersAndStickyReplay(t *testing.T) {
	for _, meaningFirst := range []bool{false, true} {
		t.Run(strconv.FormatBool(meaningFirst), func(t *testing.T) {
			f := newFixture(t, constantRegion())
			r := registry(t)
			var u Update
			var err error
			if meaningFirst {
				u, err = r.Install(f.in)
			} else {
				u, err = f.send(t, r)
			}
			if err != nil || u.Ack != nil {
				t.Fatalf("first half: %+v %v", u, err)
			}
			st := r.Status(testID)
			if st.State != Pending || st.HasInstall != meaningFirst || st.HasGrant == meaningFirst {
				t.Fatalf("pending: %+v", st)
			}
			if _, err := r.Segment(testID); !errors.Is(err, ErrNotInstalled) {
				t.Fatal(err)
			}
			if meaningFirst {
				u, err = f.send(t, r)
			} else {
				u, err = r.Install(f.in)
			}
			if err != nil || u.Ack == nil || u.Ack.Failure != nil {
				t.Fatalf("second half: %+v %v", u, err)
			}
			original := *u.Ack
			u.Ack.Proof[0] ^= 0xff
			dupe, err := f.send(t, r)
			if err != nil || !reflect.DeepEqual(*dupe.Ack, original) {
				t.Fatalf("repeat: %+v %v", dupe, err)
			}
			dupe, err = r.Install(f.in)
			if err != nil || !reflect.DeepEqual(*dupe.Ack, original) {
				t.Fatal("Install repeat changed acknowledgement")
			}
			f.raw[4]++
			bad, err := f.send(t, r)
			if err != nil || bad.Ack.Failure.Class != Malformed || r.Status(testID).State != Installed {
				t.Fatal("conflict replaced installed state")
			}
		})
	}
}

func TestFailedGrantReplayAndCleanup(t *testing.T) {
	for _, first := range []string{"meaning", "mapping"} {
		t.Run(first, func(t *testing.T) {
			f := newFixture(t, constantRegion())
			r := registry(t)
			var u Update
			var err error
			if first == "meaning" {
				f.in.Purpose = "not-known"
				u, err = r.Install(f.in)
			} else {
				f.peer[28] ^= 1
				u, err = f.send(t, r)
			}
			if err != nil || u.Ack == nil || u.Ack.Failure == nil {
				t.Fatalf("failure: %+v %v", u, err)
			}
			if r.entries[testID].segment != nil {
				t.Fatal("failed install retained mapping")
			}
			original := cloneAck(u.Ack)
			u.Ack.Failure.Detail = "caller changed it"
			if first == "meaning" {
				u, err = f.send(t, r)
			} else {
				u, err = r.Install(f.in)
			}
			if err != nil || !reflect.DeepEqual(u.Ack, original) {
				t.Fatal("missing half did not replay failure")
			}
		})
	}
}

func TestValidationClasses(t *testing.T) {
	for _, tc := range []struct {
		name   string
		change func(*fixture)
		class  FailureClass
	}{
		{"preamble_crc", func(f *fixture) { f.peer[28] ^= 1 }, Malformed},
		{"preamble_identity", func(f *fixture) { le.PutUint64(f.peer, 9); le.PutUint32(f.peer[28:], crc32.ChecksumIEEE(f.peer[:28])) }, Malformed},
		{"preamble_padding", func(f *fixture) { f.peer[8] = 1; le.PutUint32(f.peer[28:], crc32.ChecksumIEEE(f.peer[:28])) }, Malformed},
		{"size_mismatch", func(f *fixture) { f.grant.Size++ }, Malformed},
		{"policy_size", func(f *fixture) { f.grant.Size = 2 << 30 }, TooLarge},
		{"empty_purpose", func(f *fixture) { f.in.Purpose = "" }, Malformed},
		{"purpose_case", func(f *fixture) { f.in.Purpose = "TEST" }, Unsupported},
		{"zero_discipline", func(f *fixture) { f.in.Regions[0].Discipline = 0 }, Malformed},
		{"unknown_discipline", func(f *fixture) { f.in.Regions[0].Discipline = 55 }, Unsupported},
		{"zero_producer", func(f *fixture) { f.in.Regions[0].Producer = 0 }, Malformed},
		{"unknown_producer", func(f *fixture) { f.in.Regions[0].Producer = 99 }, Unsupported},
		{"preamble_overlap", func(f *fixture) { f.in.Regions[0].Offset = 0 }, Malformed},
		{"overflow", func(f *fixture) { f.in.Regions[0].Extent = math.MaxUint64 }, Malformed},
		{"bounds", func(f *fixture) { f.in.Regions[0].Offset = 4090 }, Malformed},
		{"sparse", func(f *fixture) { f.in.Regions[0].Index = 2 }, Malformed},
		{"duplicate_index", func(f *fixture) { f.in.Regions = append(f.in.Regions, f.in.Regions[0]) }, Malformed},
		{"overlap", func(f *fixture) { r := f.in.Regions[0]; r.Index = 1; f.in.Regions = append(f.in.Regions, r) }, Malformed},
		{"self_supersedes", func(f *fixture) { f.in.Supersedes = testID }, Malformed},
		{"pulsed_alignment", func(f *fixture) { f.in.Regions[0] = pulsedRegion(); f.in.Regions[0].Offset++ }, Malformed},
		{"pulsed_extent", func(f *fixture) { f.in.Regions[0] = pulsedRegion(); f.in.Regions[0].Extent++ }, Malformed},
		{"pulsed_overflow", func(f *fixture) { f.in.Regions[0] = pulsedRegion(); f.in.Regions[0].SlotBytes = math.MaxUint64 }, Malformed},
		{"pulsed_two_buffers", func(f *fixture) { f.in.Regions[0] = pulsedRegion(); f.in.Regions[0].Buffers = 2 }, Malformed},
		{"pulsed_readonly", func(f *fixture) { f.in.Regions[0] = pulsedRegion(); f.grant.Permissions = doorbell.PluginRead }, Malformed},
		{"plugin_loan_readonly", func(f *fixture) {
			f.in.Regions[0] = Region{Offset: 64, Extent: 256, Discipline: Loaned, Producer: Plugin, SlotBytes: 64}
			f.grant.Permissions = doorbell.PluginRead
		}, Malformed},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := newFixture(t, constantRegion())
			tc.change(f)
			r := registry(t)
			u, err := f.send(t, r)
			if err != nil {
				t.Fatal(err)
			}
			if u.Ack == nil {
				u, err = r.Install(f.in)
			}
			if err != nil || u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != tc.class {
				t.Fatalf("got %+v %v, want %s", u, err, tc.class)
			}
			if r.entries[testID].segment != nil {
				t.Fatal("mapping retained after validation failure")
			}
		})
	}
	if registry, err := New(Config{}); err != nil {
		t.Fatal(err)
	} else {
		registry.Close()
	}
}

func TestReadConstantAndLoanedOwnedHandoffs(t *testing.T) {
	f := newFixture(t, constantRegion(), Region{Index: 1, Offset: 256, Extent: 512, Discipline: Loaned, Producer: Host, SlotBytes: 64}, Region{Index: 2, Offset: 800, Extent: 64, Discipline: Owned, Producer: Host})
	copy(f.peer[32:], "constant")
	copy(f.peer[320:], "loan!")
	copy(f.peer[800:], "owned")
	r, s := installFixture(t, f)
	b := make([]byte, 8)
	if err := s.ReadConstant(0, 0, b); err != nil || string(b) != "constant" {
		t.Fatalf("%q %v", b, err)
	}
	if err := s.ReadConstant(0, math.MaxUint64, b); !errors.Is(err, ErrBounds) {
		t.Fatal(err)
	}
	// §7: the key echoes the descriptor's MAPPING-relative offset, so region 1
	// at 256 makes the deposit at mapping byte 320 releasable as 320, not as 64.
	d, err := s.ResolveDeposit(320, 5)
	if err != nil || d != (Deposit{testID, 1, 320, 5}) {
		t.Fatalf("release key: %+v %v", d, err)
	}
	if _, err := s.AcceptDeposit(Deposit{testID, 1, 64, 5}); !errors.Is(err, ErrBounds) {
		t.Fatal("region-relative offset accepted as a descriptor")
	}
	if _, err := s.ResolveDeposit(760, 16); !errors.Is(err, ErrBounds) {
		t.Fatal("cross-region descriptor accepted")
	}
	read, err := s.AcceptDeposit(d)
	if err != nil {
		t.Fatal(err)
	}
	alias := *read
	if _, err := read.Copy(b[:4]); !errors.Is(err, ErrBounds) {
		t.Fatal(err)
	}
	release, err := read.Copy(b)
	if err != nil || release != d || string(b[:5]) != "loan!" {
		t.Fatalf("%+v %q %v", release, b, err)
	}
	if _, err := alias.Copy(b); !errors.Is(err, ErrConsumed) {
		t.Fatal("copied handle bypassed consume-once")
	}
	o, err := s.AcceptOwned(2, 0, 5)
	if err != nil {
		t.Fatal(err)
	}
	if err := o.Copy(b); err != nil || string(b[:5]) != "owned" {
		t.Fatalf("%q %v", b, err)
	}
	if err := o.Copy(b); !errors.Is(err, ErrConsumed) {
		t.Fatal(err)
	}
	if send, err := r.RequestRelease(testID); err != nil || !send {
		t.Fatal(send, err)
	}
	if send, _ := r.RequestRelease(testID); send {
		t.Fatal("repeated release request")
	}
	// An ignored ReleaseRequest cannot revoke a later descriptor.
	read, err = s.AcceptDeposit(d)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := read.Copy(b); err != nil {
		t.Fatal(err)
	}
	if _, err := r.Retire(testID); err != nil {
		t.Fatal(err)
	}
	if string(b[:5]) != "loan!" {
		t.Fatal("retirement invalidated caller copy")
	}
	if err := s.ReadConstant(0, 0, b); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
}

func TestPulsedConsumesNewestAndSkipsLastSeen(t *testing.T) {
	f := newFixture(t, pulsedRegion())
	_, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("old"))
	publishFixture(f, 1, 3, []byte("new"))
	publishFixture(f, 2, 2, []byte("middle"))
	if _, err := s.ReadPulsed(0, make([]byte, 3)); !errors.Is(err, ErrBounds) {
		t.Fatal(err)
	}
	if loadState(f.peer, 144) != 3 {
		t.Fatal("short destination consumed value")
	}
	b := make([]byte, 32)
	sample, err := s.ReadPulsed(0, b)
	if err != nil || sample.Revision != 3 || string(b[:sample.Length]) != "new" {
		t.Fatalf("%+v %q %v", sample, b, err)
	}
	if loadState(f.peer, 144) != 0 || loadState(f.peer, 128) != 1 {
		t.Fatal("release did not consume exactly selected buffer")
	}
	sample, err = s.ReadPulsed(0, b)
	if err != nil || sample.Revision != 0 {
		t.Fatal("read older READY value")
	}
	publishFixture(f, 1, 3, []byte("repeat"))
	sample, err = s.ReadPulsed(0, b)
	if err != nil || sample.Revision != 0 {
		t.Fatal("reread last_seen")
	}
}

func TestPulsedImpossibleStatesAndLengthsQuarantineOnlyRegion(t *testing.T) {
	for _, word := range []uint64{writeClaim, readClaim, writeClaim | readClaim | 7} {
		t.Run(fmt.Sprintf("state_%x", word), func(t *testing.T) {
			c := constantRegion()
			c.Index = 1
			f := newFixture(t, pulsedRegion(), c)
			_, s := installFixture(t, f)
			storeState(f.peer, 128, word)
			_, err := s.ReadPulsed(0, make([]byte, 32))
			assertViolation(t, err, "impossible_state_word")
			if loadState(f.peer, 128) != word {
				t.Fatal("cleared foreign state")
			}
			if _, err = s.ReadPulsed(0, make([]byte, 32)); !errors.Is(err, ErrQuarantined) {
				t.Fatal(err)
			}
			if err = s.ReadConstant(1, 0, make([]byte, 4)); err != nil {
				t.Fatal("unrelated region quarantined", err)
			}
		})
	}
	for _, length := range []uint64{0, 33, math.MaxUint64} {
		t.Run(fmt.Sprintf("length_%d", length), func(t *testing.T) {
			f := newFixture(t, pulsedRegion())
			_, s := installFixture(t, f)
			publishFixture(f, 0, 1, []byte("value"))
			le.PutUint64(f.peer[136:], length)
			_, err := s.ReadPulsed(0, make([]byte, 32))
			assertViolation(t, err, "invalid_published_length")
			if loadState(f.peer, 128) != 0 {
				t.Fatal("invalid-length claim leaked")
			}
		})
	}
}
func assertViolation(t *testing.T, err error, where string) {
	t.Helper()
	var v *Violation
	if !errors.As(err, &v) || v.Where != where {
		t.Fatalf("got %v, want %s", err, where)
	}
}

func TestPulsedBoundedContentionAndReleaseInterference(t *testing.T) {
	f := newFixture(t, pulsedRegion())
	_, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("value"))
	calls := 0
	s.cas = func([]byte, uint64, uint64, uint64) bool { calls++; return false }
	sample, err := s.ReadPulsed(0, make([]byte, 32))
	if err != nil || sample.Revision != 0 || calls != 8 || loadState(f.peer, 128) != 1 {
		t.Fatal("contention was not bounded or destroyed READY")
	}
	s.cas = func(b []byte, offset, old, next uint64) bool {
		if next == 0 {
			storeState(b, offset, 19)
			return false
		}
		return casState(b, offset, old, next)
	}
	_, err = s.ReadPulsed(0, make([]byte, 32))
	assertViolation(t, err, "release_cas_failed")
	if loadState(f.peer, 128) != 19 {
		t.Fatal("release repaired foreign state")
	}
}

func TestRetirementWaitsForClaimAndInvalidatesCachedHandles(t *testing.T) {
	f := newFixture(t, pulsedRegion(), Region{Index: 1, Offset: 512, Extent: 256, Discipline: Loaned, Producer: Host, SlotBytes: 64})
	r, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("held"))
	loan, err := s.AcceptDeposit(Deposit{testID, 1, 512, 7})
	if err != nil {
		t.Fatal(err)
	}
	held, resume := make(chan struct{}), make(chan struct{})
	s.cas = func(b []byte, offset, old, next uint64) bool {
		ok := casState(b, offset, old, next)
		if ok && next&readClaim != 0 {
			close(held)
			<-resume
		}
		return ok
	}
	readDone := make(chan error, 1)
	go func() { _, err := s.ReadPulsed(0, make([]byte, 32)); readDone <- err }()
	<-held
	retired := make(chan Update, 1)
	retireErr := make(chan error, 1)
	go func() { u, err := r.Retire(testID); retired <- u; retireErr <- err }()
	deadline := time.Now().Add(time.Second)
	for !s.retiring.Load() && time.Now().Before(deadline) {
		runtime.Gosched()
	}
	if !s.retiring.Load() {
		close(resume)
		t.Fatal("retirement never latched")
	}
	select {
	case <-retired:
		close(resume)
		t.Fatal("released with held claim")
	default:
	}
	close(resume)
	if err := <-readDone; err != nil {
		t.Fatal(err)
	}
	u := <-retired
	if err := <-retireErr; !errors.Is(err, ErrLoanActive) || len(u.Released) != 0 {
		t.Fatal(u, err)
	}
	if _, err := loan.Copy(make([]byte, 7)); err != nil {
		t.Fatal("arrived deposit refused while retiring", err)
	}
	u, err = r.Retire(testID)
	if err != nil || len(u.Released) != 1 || loadState(f.peer, 128) != 0 {
		t.Fatal("retirement abandoned claim", u, err)
	}
	if _, err := s.AcceptDeposit(Deposit{testID, 1, 512, 7}); !errors.Is(err, ErrRetired) {
		t.Fatal(err)
	}
	if _, err := s.ReadPulsed(0, make([]byte, 32)); !errors.Is(err, ErrRetired) {
		t.Fatal("old lane handle usable", err)
	}
}

func TestRetireEveryStateAndSupersedes(t *testing.T) {
	for _, state := range []string{"unknown", "grant", "install", "failed", "installed"} {
		t.Run(state, func(t *testing.T) {
			f := newFixture(t, constantRegion())
			r := registry(t)
			switch state {
			case "grant":
				f.send(t, r)
			case "install":
				r.Install(f.in)
			case "failed":
				f.in.Purpose = "bad"
				r.Install(f.in)
			case "installed":
				f.send(t, r)
				r.Install(f.in)
			}
			for i := 0; i < 2; i++ {
				u, err := r.Retire(testID)
				if err != nil || !reflect.DeepEqual(u.Released, []uint64{testID}) {
					t.Fatal(u, err)
				}
			}
			u, err := f.send(t, r)
			if err != nil || len(u.Released) != 1 || u.Ack != nil {
				t.Fatal("late M not answered as released")
			}
			u, err = r.Install(f.in)
			if err != nil || len(u.Released) != 1 || u.Ack != nil {
				t.Fatal("late meaning not answered as released")
			}
		})
	}
	for _, bad := range []bool{false, true} {
		t.Run(fmt.Sprintf("supersedes_bad_%v", bad), func(t *testing.T) {
			f := newFixture(t, constantRegion())
			r, s := installFixture(t, f)
			g := newFixture(t, constantRegion())
			g.grant.SegmentID++
			g.in.SegmentID++
			g.in.Supersedes = testID
			le.PutUint64(g.peer, g.grant.SegmentID)
			le.PutUint32(g.peer[28:], crc32.ChecksumIEEE(g.peer[:28]))
			if bad {
				g.peer[28] ^= 1
			}
			g.raw, _ = doorbell.EncodeMemory(g.grant, 18)
			if u, err := r.Install(g.in); err != nil || len(u.Released) != 0 {
				t.Fatal(u, err)
			}
			if err := s.ReadConstant(0, 0, make([]byte, 4)); err != nil {
				t.Fatal("pending replacement retired original")
			}
			u, err := g.send(t, r)
			if err != nil {
				t.Fatal(err)
			}
			if bad {
				if len(u.Released) != 0 || r.Status(testID).State != Installed {
					t.Fatal("failed replacement retired original")
				}
			} else {
				if len(u.Released) != 1 || r.Status(testID).State != Released {
					t.Fatal("successful replacement did not retire original")
				}
			}
		})
	}
}

func TestHintsAreSchedulingOnly(t *testing.T) {
	c := constantRegion()
	c.Index = 1
	f := newFixture(t, pulsedRegion(), c, Region{Index: 2, Offset: 512, Extent: 128, Discipline: Loaned, Producer: Host, SlotBytes: 64}, Region{Index: 3, Offset: 768, Extent: 128, Discipline: Owned, Producer: Host})
	_, s := installFixture(t, f)
	n := doorbell.Notification{SegmentID: testID, Epoch: 2, ViewID: 0}
	n.Mask.Set(0)
	n.Mask.Set(2)
	n.Mask.Set(3)
	if p := s.Hint(n, 1); len(p.Regions) != 0 || !reflect.DeepEqual(p.Diagnostics, []string{"future_epoch"}) {
		t.Fatalf("future pane-zero hint: %+v", p)
	}
	if p := s.Hint(n, 3); len(p.Regions) != 0 || !reflect.DeepEqual(p.Diagnostics, []string{"stale_epoch"}) {
		t.Fatalf("stale hint: %+v", p)
	}
	p := s.Hint(n, 2)
	if !reflect.DeepEqual(p.Regions, []uint32{0}) || len(p.Violations) != 1 || p.Violations[0].Where != "owned_region_hint" {
		t.Fatalf("%+v", p)
	}
	n.Mask.Set(511)
	p = s.Hint(n, 2)
	if !reflect.DeepEqual(p.Regions, []uint32{0}) {
		t.Fatalf("out-of-range hint scheduled an unnamed region: %+v", p)
	}
	n.Mask = doorbell.Mask{}
	n.Mask.Set(511)
	p = s.Hint(n, 2)
	if !reflect.DeepEqual(p.Regions, []uint32{0}) || !reflect.DeepEqual(p.Diagnostics, []string{"out_of_range_region"}) {
		t.Fatalf("stray bit did not sweep eligible Pulsed regions: %+v", p)
	}
	n.Mask.Set(1)
	p = s.Hint(n, 2)
	if !reflect.DeepEqual(p.Regions, []uint32{0}) || !reflect.DeepEqual(p.Diagnostics, []string{"out_of_range_region", "hint_on_constant_region"}) {
		t.Fatalf("sweep scheduled Constant or lost its diagnostic: %+v", p)
	}
	n.Mask = doorbell.Mask{}
	n.Mask.Set(1)
	p = s.Hint(n, 2)
	if len(p.Regions) != 0 || !reflect.DeepEqual(p.Diagnostics, []string{"hint_on_constant_region"}) {
		t.Fatalf("Constant-only hint scheduled work: %+v", p)
	}
	// Poll works with no hint and after a hint has been discarded.
	publishFixture(f, 0, 1, []byte("poll"))
	if sample, err := s.ReadPulsed(0, make([]byte, 32)); err != nil || sample.Revision != 1 {
		t.Fatal(sample, err)
	}
}

func TestHintOnPluginProducedPulsedIsCounted(t *testing.T) {
	lane := pulsedRegion()
	lane.Producer = Plugin
	f := newFixture(t, lane)
	_, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("mine"))
	n := doorbell.Notification{SegmentID: testID}
	n.Mask.Set(0)
	p := s.Hint(n, 0)
	if len(p.Regions) != 0 || !reflect.DeepEqual(p.Diagnostics, []string{"hint_on_produced_region"}) {
		t.Fatalf("%+v", p)
	}
	if loadState(f.peer, 128) != 1 {
		t.Fatal("hint consumed plugin's own value")
	}
}

func TestLoanedConsumerIgnoresAllocationAlignment(t *testing.T) {
	for _, alignment := range []uint64{0, 1, 64, 128, math.MaxUint64} {
		t.Run(strconv.FormatUint(alignment, 10), func(t *testing.T) {
			f := newFixture(t, Region{Offset: 64, Extent: 128, Discipline: Loaned, Producer: Host, SlotBytes: alignment})
			copy(f.peer[64:], "loan")
			_, s := installFixture(t, f)
			d := Deposit{testID, 0, 64, 4}
			h, err := s.AcceptDeposit(d)
			if err != nil {
				t.Fatal(err)
			}
			buf := make([]byte, 4)
			release, err := h.Copy(buf)
			if err != nil || release != d || string(buf) != "loan" {
				t.Fatal(release, string(buf), err)
			}
		})
	}
}

func TestLoanedProducerAlignmentDoesNotFailInstallation(t *testing.T) {
	for _, alignment := range []uint64{0, 1, 64, 128, math.MaxUint64} {
		t.Run(strconv.FormatUint(alignment, 10), func(t *testing.T) {
			f := newFixture(t, Region{Offset: 64, Extent: 128, Discipline: Loaned, Producer: Plugin, SlotBytes: alignment})
			_, s := installFixture(t, f)
			if _, err := s.AcceptDeposit(Deposit{testID, 0, 64, 4}); !errors.Is(err, ErrDiscipline) {
				t.Fatal("ignoring allocator metadata granted consumer access", err)
			}
		})
	}
}

func TestSweepOnlyReachesPeerPulsedAndPreservesExplicitBits(t *testing.T) {
	c := constantRegion()
	c.Index = 1
	p := pulsedRegion()
	p.Index = 4
	p.Offset = 1024
	own := pulsedRegion()
	own.Index = 5
	own.Offset = 1536
	own.Producer = Plugin
	f := newFixture(t, pulsedRegion(), c,
		Region{Index: 2, Offset: 512, Extent: 128, Discipline: Loaned, Producer: Host},
		Region{Index: 3, Offset: 768, Extent: 128, Discipline: Owned, Producer: Host}, p, own)
	_, s := installFixture(t, f)
	n := doorbell.Notification{SegmentID: testID}
	n.Mask.Set(511)
	plan := s.Hint(n, 0)
	if !reflect.DeepEqual(plan.Regions, []uint32{0, 4}) || len(plan.Violations) != 0 ||
		!reflect.DeepEqual(plan.Diagnostics, []string{"out_of_range_region"}) {
		t.Fatalf("sweep touched an ineligible unnamed region: %+v", plan)
	}
	for _, index := range []uint16{0, 1, 2, 3, 5, 510} {
		n.Mask.Set(index)
	}
	plan = s.Hint(n, 0)
	if !reflect.DeepEqual(plan.Regions, []uint32{0, 4}) || len(plan.Violations) != 1 ||
		plan.Violations[0].Where != "owned_region_hint" ||
		!reflect.DeepEqual(plan.Diagnostics, []string{"out_of_range_region", "out_of_range_region", "hint_on_constant_region", "hint_on_loaned_region", "hint_on_produced_region"}) {
		t.Fatalf("stray bits lost explicit dispositions or duplicated scheduled work: %+v", plan)
	}
	if _, err := s.AcceptOwned(3, 0, 1); err != nil {
		t.Fatal("bad hint quarantined Owned region", err)
	}
}

func TestConcurrentReadersShareConsumerState(t *testing.T) {
	f := newFixture(t, pulsedRegion())
	_, s := installFixture(t, f)
	publishFixture(f, 0, 1, []byte("once"))
	var wg sync.WaitGroup
	results := make(chan Sample, 8)
	for i := 0; i < 8; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			sample, err := s.ReadPulsed(0, make([]byte, 32))
			if err != nil {
				t.Error(err)
			}
			results <- sample
		}()
	}
	wg.Wait()
	close(results)
	count := 0
	for sample := range results {
		if sample.Revision != 0 {
			count++
		}
	}
	if count != 1 {
		t.Fatalf("%d consumers observed the value", count)
	}
}

// A separate process consumes real MAP_SHARED state. The parent uses only the
// spec's raw layout and atomic handoff, not another SDK implementation.
func TestProcessSharedPulsed(t *testing.T) {
	if os.Getenv("VIZI_MEMORY_READER_CHILD") == "1" {
		file := os.NewFile(3, "shared")
		r := registry(t)
		g := doorbell.Memory{SegmentID: testID, Size: 4096, Permissions: doorbell.PluginRead | doorbell.PluginWrite}
		raw, _ := doorbell.EncodeMemory(g, 1)
		if _, err := r.grant(raw, g, file); err != nil {
			t.Fatal(err)
		}
		if u, err := r.Install(InstallSegment{SegmentID: testID, Purpose: "test", Regions: []Region{pulsedRegion()}}); err != nil || u.Ack == nil || u.Ack.Failure != nil {
			t.Fatal(u, err)
		}
		s, err := r.Segment(testID)
		if err != nil {
			t.Fatal(err)
		}
		fmt.Println("ready")
		for expected := uint64(1); expected <= 50; expected++ {
			deadline := time.Now().Add(5 * time.Second)
			for {
				b := make([]byte, 32)
				sample, err := s.ReadPulsed(0, b)
				if err != nil {
					t.Fatal(err)
				}
				if sample.Revision != 0 {
					if sample.Revision != expected || string(b[:sample.Length]) != fmt.Sprintf("value-%d", expected) {
						t.Fatal("torn or out-of-order payload", sample, string(b))
					}
					fmt.Println(expected)
					break
				}
				if time.Now().After(deadline) {
					t.Fatal("child read timeout")
				}
				runtime.Gosched()
			}
		}
		return
	}
	f := newFixture(t, pulsedRegion())
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestProcessSharedPulsed$")
	cmd.Env = append(os.Environ(), "VIZI_MEMORY_READER_CHILD=1")
	cmd.ExtraFiles = []*os.File{f.file}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	out, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { cancel(); cmd.Wait() }()
	scan := bufio.NewScanner(out)
	if !scan.Scan() || scan.Text() != "ready" {
		t.Fatal("child not ready", scan.Text(), stderr.String())
	}
	for revision := uint64(1); revision <= 50; revision++ {
		slot := uint32(revision % 3)
		state := uint64(128) + uint64(slot)*16
		if !casState(f.peer, state, 0, revision|writeClaim) {
			t.Fatal("reader did not release")
		}
		publishFixture(f, slot, revision, []byte(fmt.Sprintf("value-%d", revision)))
		if !scan.Scan() || scan.Text() != strconv.FormatUint(revision, 10) {
			t.Fatal("child read failed", scan.Text())
		}
	}
	// Drain the testing harness's PASS line before waiting.
	for scan.Scan() {
	}
	if err := cmd.Wait(); err != nil {
		t.Fatal(err, stderr.String())
	}
}

func TestDoorbellGrantIntegration(t *testing.T) {
	f := newFixture(t, constantRegion())
	r := registry(t)
	if _, err := r.Install(f.in); err != nil {
		t.Fatal(err)
	}
	kind := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		kind = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, kind, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[1])
	updates := make(chan Update, 2)
	errs := make(chan error, 2)
	tr, err := doorbell.Adopt(pair[0], doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame != nil && e.Frame.Memory != nil {
			u, err := r.Grant(e.Frame)
			updates <- u
			errs <- err
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		tr.Stop()
		ctx, cancel := context.WithTimeout(context.Background(), time.Second)
		defer cancel()
		if err := tr.Wait(ctx); err != nil {
			t.Error(err)
		}
	}()
	if err := tr.HandshakeComplete(1, 0); err != nil {
		t.Fatal(err)
	}
	for repeat := 0; repeat < 2; repeat++ {
		if _, err := unix.SendmsgN(pair[1], f.raw[:], unix.UnixRights(int(f.file.Fd())), nil, 0); err != nil {
			t.Fatal(err)
		}
		select {
		case u := <-updates:
			if err := <-errs; err != nil || u.Ack == nil || u.Ack.Failure != nil {
				t.Fatalf("%+v %v", u, err)
			}
		case <-time.After(2 * time.Second):
			t.Fatal("doorbell grant timeout")
		}
	}
	if _, err := r.Segment(testID); err != nil {
		t.Fatal(err)
	}
}

func TestMappingUsesExactlyDeclaredAccess(t *testing.T) {
	f := newFixture(t, constantRegion())
	r := registry(t)
	readonly, err := os.Open(f.file.Name())
	if err != nil {
		t.Fatal(err)
	}
	u, err := r.grant(f.raw, f.grant, readonly)
	if err != nil || u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != Malformed {
		t.Fatalf("writable mapping over read-only fd: %+v %v", u, err)
	}
	// A read-only declaration over a writable fd is valid; never probe for more.
	f.grant.Permissions = doorbell.PluginRead
	r = registry(t)
	if _, err := f.send(t, r); err != nil {
		t.Fatal(err)
	}
	if u, err := r.Install(f.in); err != nil || u.Ack == nil || u.Ack.Failure != nil {
		t.Fatalf("read-only declaration: %+v %v", u, err)
	}
	s, err := r.Segment(testID)
	if err != nil {
		t.Fatal(err)
	}
	if err := s.ReadConstant(0, 0, make([]byte, 4)); err != nil {
		t.Fatal(err)
	}
}
