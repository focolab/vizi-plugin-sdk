//go:build linux || darwin

package doorbell

import (
	"bufio"
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"testing"
	"time"

	"golang.org/x/sys/unix"
)

// This is a host probe, not a candidate canonical server. The stdout line is
// solely a test synchronization barrier in place of the future gRPC handshake.
func TestSeparateProcessDescriptorAndMapping(t *testing.T) {
	p := pair(t)
	pluginFD := os.NewFile(uintptr(p[0]), "inherited-doorbell")
	defer pluginFD.Close()
	hostFD := os.NewFile(uintptr(p[1]), "host-doorbell")
	defer hostFD.Close()
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, os.Args[0], "-test.run=^TestPluginHelperProcess$")
	cmd.Env = append(os.Environ(), "VIZI_GO_TEST_CHILD=1", "VIZI_PLUGIN_DOORBELL_FD=3")
	cmd.ExtraFiles = []*os.File{pluginFD}
	var stderr bytes.Buffer
	cmd.Stderr = &stderr
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		t.Fatal(err)
	}
	if err = cmd.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() {
		if cmd.ProcessState == nil {
			cmd.Process.Kill()
			cmd.Wait()
		}
	}()
	pluginFD.Close() // the host must not keep the plugin endpoint alive
	line, err := bufio.NewReader(stdout).ReadString('\n')
	if err != nil || line != "READY\n" {
		t.Fatalf("child readiness %q %v: %s", line, err, stderr.String())
	}
	file, err := os.CreateTemp(t.TempDir(), "memory")
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if err = file.Truncate(4096); err != nil {
		t.Fatal(err)
	}
	if err = os.Remove(file.Name()); err != nil {
		t.Fatal(err)
	}
	memory, err := unix.Mmap(int(file.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Munmap(memory)
	copy(memory[32:], "host data")
	r, _ := EncodeMemory(Memory{SegmentID: 0x100000001, Size: 4096, Permissions: PluginRead | PluginWrite | HostRead | HostWrite}, 1)
	send(t, p[1], r[:], int(file.Fd()))
	notification := recvPeer(t, p[1])
	d := newDecoder()
	frame, fault := d.decode(notification, 0, false)
	if fault != nil || frame.Notification == nil || frame.Notification.SegmentID != 0x100000001 {
		t.Fatal(frame, fault)
	}
	if string(memory[32:42]) != "child data" {
		t.Fatalf("shared mapping did not change: %q", memory[32:42])
	}
	if err = hostFD.Close(); err != nil {
		t.Fatal(err)
	}
	if err = cmd.Wait(); err != nil {
		t.Fatalf("child: %v: %s", err, stderr.String())
	}
}

func TestPluginHelperProcess(t *testing.T) {
	if os.Getenv("VIZI_GO_TEST_CHILD") != "1" {
		t.Skip("subprocess helper")
	}
	var tr *Transport
	var err error
	handled := make(chan struct{}, 1)
	tr, err = FromEnv(Config{Handler: func(e Event) {
		if e.Frame == nil || e.Frame.Disposition != Decoded || e.Frame.Memory == nil {
			return
		}
		f, err := e.Frame.Descriptor.Claim()
		if err != nil {
			panic(err)
		}
		defer f.Close()
		memory, err := unix.Mmap(int(f.Fd()), 0, int(e.Frame.Memory.Size), unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
		if err != nil {
			panic(err)
		}
		defer unix.Munmap(memory)
		if string(memory[32:41]) != "host data" {
			panic("wrong mapping")
		}
		copy(memory[32:], "child data")
		var mask Mask
		mask.Set(0)
		if _, err = tr.Notify(Notification{SegmentID: e.Frame.Memory.SegmentID, Mask: mask}); err != nil {
			panic(err)
		}
		select {
		case handled <- struct{}{}:
		default:
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer tr.Stop()
	if err = tr.HandshakeComplete(1, 0); err != nil {
		t.Fatal(err)
	}
	fmt.Println("READY")
	if socketType == unix.SOCK_DGRAM {
		// The parent's close is not observable here (see TestZeroReadAndPeerClose),
		// so end the session the way a plugin does when the control plane says
		// to — but only once the grant this test exists to exercise has been
		// handled, since Stop closes the socket the parent is still sending on.
		select {
		case <-handled:
		case <-time.After(4 * time.Second):
			t.Fatal("no grant arrived before the deadline")
		}
		tr.Stop()
	} else {
		term := terminal(t, tr)
		if term.Reason != "peer_gone" || term.Fault != nil {
			t.Fatal(term)
		}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err = tr.Wait(ctx); err != nil {
		t.Fatal(err)
	}
}
