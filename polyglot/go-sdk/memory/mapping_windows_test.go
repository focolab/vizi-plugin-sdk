//go:build windows && (amd64 || arm64)

package memory

import (
	"fmt"
	"hash/crc32"
	"os"
	"testing"
	"time"
	"unsafe"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"golang.org/x/sys/windows"
)

func windowsFixture(t *testing.T) (doorbell.Memory, []byte, func()) {
	t.Helper()
	name := fmt.Sprintf("Local\\vizi-sdk-test-%d-%d", os.Getpid(), time.Now().UnixNano())
	wname, err := windows.UTF16PtrFromString(name)
	if err != nil {
		t.Fatal(err)
	}
	h, err := windows.CreateFileMapping(windows.InvalidHandle, nil, windows.PAGE_READWRITE, 0, 4096, wname)
	if err != nil {
		t.Fatal(err)
	}
	addr, err := windows.MapViewOfFile(h, windows.FILE_MAP_READ|windows.FILE_MAP_WRITE, 0, 0, 4096)
	if err != nil {
		windows.CloseHandle(h)
		t.Fatal(err)
	}
	data := mappedBytes(addr, 4096)
	le.PutUint64(data, 81)
	copy(data[12:28], "windows proof!!!")
	le.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	closed := false
	closeHost := func() {
		if closed {
			return
		}
		closed = true
		if err := windows.UnmapViewOfFile(addr); err != nil {
			t.Error(err)
		}
		if err := windows.CloseHandle(h); err != nil {
			t.Error(err)
		}
	}
	t.Cleanup(closeHost)
	return doorbell.Memory{SegmentID: 81, Size: 4096, Permissions: doorbell.PluginRead | doorbell.PluginWrite, Name: name}, data, closeHost
}

func windowsRegistry(t *testing.T, g doorbell.Memory, in InstallSegment) (*Registry, Update) {
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
	if _, err = r.Install(in); err != nil {
		t.Fatal(err)
	}
	raw, err := doorbell.EncodeMemory(g, 1)
	if err != nil {
		t.Fatal(err)
	}
	u, err := r.Grant(&doorbell.Frame{Raw: raw, HasRecord: true, Disposition: doorbell.Decoded, Memory: &g})
	if err != nil {
		t.Fatal(err)
	}
	return r, u
}

func TestWindowsMappingReadWriteAndRetirement(t *testing.T) {
	g, peer, closeHost := windowsFixture(t)
	copy(peer[1024:], "constant")
	in := InstallSegment{SegmentID: 81, Purpose: "test", Regions: []Region{
		{Index: 0, Offset: 32, Extent: 3 * (16 + 64), Discipline: Pulsed, Producer: Plugin, Buffers: 3, SlotBytes: 64},
		{Index: 1, Offset: 1024, Extent: 64, Discipline: Constant, Producer: Host},
	}}
	r, u := windowsRegistry(t, g, in)
	if u.Ack == nil || u.Ack.Failure != nil {
		t.Fatal(u)
	}
	s, err := r.Segment(81)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := s.PublishPulsed(0, []byte("shared")); err != nil {
		t.Fatal(err)
	}
	if loadState(peer, 48) != 1 || string(peer[144:150]) != "shared" {
		t.Fatal("publication is not shared")
	}
	buf := make([]byte, 8)
	if err := s.ReadConstant(1, 0, buf); err != nil || string(buf) != "constant" {
		t.Fatal(err, string(buf))
	}
	// Closing the host's view and section handle must not destroy our view.
	closeHost()
	if err := s.ReadConstant(1, 0, buf); err != nil {
		t.Fatal(err)
	}
	if u, err := r.Retire(81); err != nil || len(u.Released) != 1 {
		t.Fatal(u, err)
	}
	if err := s.ReadConstant(1, 0, buf); err != ErrRetired {
		t.Fatal(err)
	}
	// No leaked SDK handle/view may keep the named object alive.
	name, _ := windows.UTF16PtrFromString(g.Name)
	h, _, _ := openFileMappingW.Call(windows.FILE_MAP_READ, 0, uintptr(unsafe.Pointer(name)))
	if h != 0 {
		windows.CloseHandle(windows.Handle(h))
		t.Fatal("retirement leaked named section")
	}
}

func TestWindowsMissingNameDoesNotCreate(t *testing.T) {
	g, _, closeHost := windowsFixture(t)
	closeHost()
	_, u := windowsRegistry(t, g, InstallSegment{SegmentID: 81, Purpose: "test"})
	if u.Ack == nil || u.Ack.Failure == nil {
		t.Fatal("missing section was accepted", u)
	}
	name, _ := windows.UTF16PtrFromString(g.Name)
	h, _, _ := openFileMappingW.Call(windows.FILE_MAP_READ, 0, uintptr(unsafe.Pointer(name)))
	if h != 0 {
		windows.CloseHandle(windows.Handle(h))
		t.Fatal("missing section was silently created")
	}
}

func TestWindowsPreambleAndReadOnly(t *testing.T) {
	for _, corrupt := range []bool{false, true} {
		t.Run(fmt.Sprintf("corrupt=%t", corrupt), func(t *testing.T) {
			g, peer, closeHost := windowsFixture(t)
			g.Permissions = doorbell.PluginRead
			if corrupt {
				peer[12] ^= 1
			}
			r, u := windowsRegistry(t, g, InstallSegment{SegmentID: 81, Purpose: "test", Regions: []Region{{Index: 0, Offset: 32, Extent: 64, Discipline: Constant, Producer: Host}}})
			if corrupt {
				if u.Ack == nil || u.Ack.Failure == nil || u.Ack.Failure.Class != Malformed {
					t.Fatal(u)
				}
				closeHost()
				name, _ := windows.UTF16PtrFromString(g.Name)
				h, _, _ := openFileMappingW.Call(windows.FILE_MAP_READ, 0, uintptr(unsafe.Pointer(name)))
				if h != 0 {
					windows.CloseHandle(windows.Handle(h))
					t.Fatal("failed grant leaked section")
				}
			} else {
				if u.Ack == nil || u.Ack.Failure != nil {
					t.Fatal(u)
				}
				s, _ := r.Segment(81)
				if err := s.ReadConstant(0, 0, make([]byte, 64)); err != nil {
					t.Fatal(err)
				}
			}
		})
	}
}

func TestWindowsMappingCannotExceedSection(t *testing.T) {
	g, _, _ := windowsFixture(t)
	g.Size = 8192
	_, u := windowsRegistry(t, g, InstallSegment{SegmentID: 81, Purpose: "test"})
	if u.Ack == nil || u.Ack.Failure == nil {
		t.Fatal("oversized view accepted", u)
	}
}
