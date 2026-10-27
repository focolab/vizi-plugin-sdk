//go:build (linux || darwin) && (amd64 || arm64)

package memory

import (
	"bufio"
	"bytes"
	"context"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"testing"
	"time"
)

type pythonPeer struct {
	in  io.WriteCloser
	out *bufio.Reader
	t   *testing.T
}

func startPython(t *testing.T, f *fixture, role string, extra ...string) *pythonPeer {
	t.Helper()
	python := os.Getenv("VIZI_MEMORY_PYTHON")
	if python == "" {
		t.Skip("set VIZI_MEMORY_PYTHON to a Python with real atomicshm; CI requires this harness")
	}
	script, err := filepath.Abs("../../../scripts/memory_peer.py")
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), 25*time.Second)
	cmd := exec.CommandContext(ctx, python, append([]string{script, role, strconv.FormatUint(testID, 10)}, extra...)...)
	cmd.ExtraFiles = []*os.File{f.file}
	cmd.Stderr = os.Stderr
	in, err := cmd.StdinPipe()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	out, err := cmd.StdoutPipe()
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	if err := cmd.Start(); err != nil {
		cancel()
		t.Fatal(err)
	}
	p := &pythonPeer{in, bufio.NewReader(out), t}
	t.Cleanup(func() {
		in.Close()
		err := cmd.Wait()
		cancel()
		if err != nil {
			t.Error("Python peer:", err)
		}
	})
	hello := p.receive()
	if hello["backend"] == nil || hello["proof"] != hex.EncodeToString(f.peer[12:28]) {
		t.Fatal("invalid native peer handshake", hello)
	}
	t.Logf("Python atomicshm %v backend %v", hello["version"], hello["backend"])
	return p
}

func (p *pythonPeer) send(value any) {
	p.t.Helper()
	if err := json.NewEncoder(p.in).Encode(value); err != nil {
		p.t.Fatal(err)
	}
}
func (p *pythonPeer) receive() map[string]any {
	p.t.Helper()
	line, err := p.out.ReadBytes('\n')
	if err != nil {
		p.t.Fatal("Python response:", err)
	}
	var result map[string]any
	if err := json.Unmarshal(line, &result); err != nil {
		p.t.Fatal(err, string(line))
	}
	return result
}
func (p *pythonPeer) call(value any) map[string]any { p.send(value); return p.receive() }

func crossPayload(sequence uint64) []byte {
	data := make([]byte, 16+sequence%241)
	le.PutUint64(data, sequence)
	for i := 8; i < len(data); i++ {
		data[i] = byte((sequence + uint64(i)*17) % 251)
	}
	return data
}

func crossFixture(t *testing.T, producer Producer) (*fixture, *Segment) {
	f := newFixture(t, Region{Index: 0, Offset: 32, Extent: 3 * (16 + 256), Discipline: Pulsed, Producer: producer, Buffers: 3, SlotBytes: 256})
	_, s := installFixture(t, f)
	return f, s
}

func TestPythonPulsedConformance(t *testing.T) {
	for _, pythonWriter := range []bool{true, false} {
		t.Run(fmt.Sprintf("python_writer=%t", pythonWriter), func(t *testing.T) {
			producer, role := Plugin, "read"
			if pythonWriter {
				producer, role = Host, "publish"
			}
			f, s := crossFixture(t, producer)
			p := startPython(t, f, role)
			// Endpoint-relative producer declarations let each SDK play its real
			// plugin role against the other's host role, over identical bytes.
			publish := func(data []byte) {
				if pythonWriter {
					if got := p.call(map[string]any{"op": "publish", "data": hex.EncodeToString(data)}); got["published"] != true {
						t.Fatal(got)
					}
				} else if sample, err := s.PublishPulsed(0, data); err != nil || sample.Revision == 0 {
					t.Fatal(sample, err)
				}
			}
			read := func(want []byte, revision uint64) {
				if pythonWriter {
					buf := make([]byte, 256)
					sample, err := s.ReadPulsed(0, buf)
					if err != nil || sample.Revision != revision || !bytes.Equal(buf[:sample.Length], want) {
						t.Fatal(sample, err, buf[:sample.Length])
					}
				} else {
					got := p.call(map[string]any{"op": "read"})
					if want == nil {
						if got["data"] != nil {
							t.Fatal(got)
						}
						return
					}
					if got["data"] != hex.EncodeToString(want) || got["revision"] != float64(revision) {
						t.Fatal(got)
					}
				}
			}
			publish([]byte("one"))
			publish([]byte("two"))
			publish([]byte("three"))
			// Empty-slot rotation is identical in both writers.
			if got := []uint64{loadState(f.peer, 32), loadState(f.peer, 48), loadState(f.peer, 64)}; !bytes.Equal([]byte{byte(got[0]), byte(got[1]), byte(got[2])}, []byte{3, 1, 2}) {
				t.Fatal(got)
			}
			publish([]byte("four")) // all READY: overwrite oldest, preserve newest
			if loadState(f.peer, 32) != 3 || loadState(f.peer, 48) != 4 || loadState(f.peer, 64) != 2 {
				t.Fatal("oldest/newest READY selection disagrees")
			}
			read([]byte("four"), 4)
			read(nil, 0) // old READY revisions must not be delivered later
			publish([]byte("five"))
			read([]byte("five"), 5)
		})
	}
}

func TestPythonPulsedConcurrentOrdering(t *testing.T) {
	const count = 5000
	for _, pythonWriter := range []bool{true, false} {
		t.Run(fmt.Sprintf("python_writer=%t", pythonWriter), func(t *testing.T) {
			producer, role := Plugin, "read"
			if pythonWriter {
				producer, role = Host, "publish"
			}
			f, s := crossFixture(t, producer)
			p := startPython(t, f, role)
			if pythonWriter {
				p.send(map[string]any{"op": "publish_many", "count": count})
				buf := make([]byte, 256)
				var last, revision uint64
				samples := 0
				deadline := time.Now().Add(20 * time.Second)
				for last < count {
					if time.Now().After(deadline) {
						t.Fatalf("stalled at %d", last)
					}
					sample, err := s.ReadPulsed(0, buf)
					if err != nil {
						t.Fatal(err)
					}
					if sample.Revision == 0 {
						runtime.Gosched()
						continue
					}
					sequence := le.Uint64(buf)
					if sequence <= last || sample.Revision <= revision || !bytes.Equal(buf[:sample.Length], crossPayload(sequence)) {
						t.Fatalf("torn/regressed payload %d after %d", sequence, last)
					}
					last, revision = sequence, sample.Revision
					samples++
				}
				if got := p.receive(); got["published"] != true {
					t.Fatal(got)
				}
				t.Logf("Go consumed %d coherent values through final sequence %d", samples, last)
			} else {
				p.send(map[string]any{"op": "read_until", "count": count})
				for sequence := uint64(1); sequence <= count; sequence++ {
					sample, err := s.PublishPulsed(0, crossPayload(sequence))
					if err != nil || (sequence == count && sample.Revision == 0) {
						t.Fatal(sample, err)
					}
					if sequence%64 == 0 {
						time.Sleep(time.Microsecond)
					}
				}
				got := p.receive()
				if got["last"] != float64(count) || got["samples"].(float64) < 1 {
					t.Fatal(got)
				}
				t.Logf("Python consumed %v coherent values through final sequence %v", got["samples"], got["last"])
			}
		})
	}
}

// The Python reference installs a lane at 64 over garbage in the host-reserved
// bytes [32..64), and the bytes Go and Python exchange stay coherent.
func TestPythonInstallsOverHostReservedBytes(t *testing.T) {
	f := newFixture(t, Region{Index: 0, Offset: 64, Extent: 3 * (16 + 256), Discipline: Pulsed, Producer: Plugin, Buffers: 3, SlotBytes: 256})
	stampReserved(f.peer, false, true)
	_, s := installFixture(t, f)
	p := startPython(t, f, "read", "--offset", "64")
	if sample, err := s.PublishPulsed(0, []byte("over reserved")); err != nil || sample.Revision == 0 {
		t.Fatal(sample, err)
	}
	if got := p.call(map[string]any{"op": "read"}); got["data"] != hex.EncodeToString([]byte("over reserved")) {
		t.Fatal(got)
	}
	if !bytes.Equal(f.peer[32:64], reservedGarbage()) {
		t.Fatal("host-reserved bytes were written")
	}
}
