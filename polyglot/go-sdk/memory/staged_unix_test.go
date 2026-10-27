//go:build (linux || darwin) && (amd64 || arm64)

package memory

import (
	"bytes"
	"errors"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"golang.org/x/sys/unix"
	"os"
	"os/exec"
	"testing"
	"time"
)

func TestStagedBorrowPinsRetirementAndChecksOnlyOwnedHeaders(t *testing.T) {
	f := newFixture(t, Region{Offset: 64, Extent: 384, Discipline: Staged, Producer: Host})
	f.in.Purpose = PurposeStage
	r, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Error(err)
		}
	})
	// Every header byte is invalid. The grant must inspect none of them.
	for i := 64; i < 448; i++ {
		f.peer[i] = 255
	}
	if _, err := f.send(t, r); err != nil {
		t.Fatal(err)
	}
	u, err := r.Install(f.in)
	if err != nil || u.Ack == nil || u.Ack.Failure != nil {
		t.Fatal(u, err)
	}
	s, err := r.Segment(testID)
	if err != nil {
		t.Fatal(err)
	}
	if turn, err := s.TryStageTurn(0, 1, 1, 0); err != nil || turn != nil {
		t.Fatal("wrong owner touched", turn, err)
	}
	storeState(f.peer, 64, 0x00010001ffffffff)
	turn, err := s.TryStageTurn(0, 1, 1, 0)
	if err != nil || turn == nil {
		t.Fatal(turn, err)
	}
	if second, err := s.TryStageTurn(0, 1, 1, 0); err != nil || second != nil {
		t.Fatal("two local borrowers", second, err)
	}
	if _, err := r.Retire(testID); !errors.Is(err, ErrStageActive) {
		t.Fatal(err)
	}
	if err := r.Close(); !errors.Is(err, ErrStageActive) {
		t.Fatal(err)
	}
	copyOfTurn := *turn
	turn.Data[300] = 42 // Mapping remains valid after both attempts to retire it.
	if err := turn.Finish(stage.Failed, 1); err != nil {
		t.Fatal(err)
	}
	if err := copyOfTurn.Abandon(); !errors.Is(err, ErrConsumed) {
		t.Fatal("copied handle double-release", err)
	}
	if got := loadState(f.peer, 64); got != 0x00010001ffffffff {
		t.Fatalf("published after retirement: %x", got)
	}
	if f.peer[364] != 42 {
		t.Fatal("borrow was not shared")
	}
	if s.data != nil {
		t.Fatal("deferred retirement did not unmap")
	}
	if err := r.Close(); err != nil {
		t.Fatal(err)
	}
}

// Runs a second Go process against the same bytes. This specifically checks the
// native atomic backend across process boundaries, beyond two local sessions.
func TestStagedNativeProcessHandoff(t *testing.T) {
	const childEnv = "VIZI_GO_STAGE_TEST_MAPPING"
	if path := os.Getenv(childEnv); path != "" {
		f, err := os.OpenFile(path, os.O_RDWR, 0)
		if err != nil {
			t.Fatal(err)
		}
		defer f.Close()
		data, err := unix.Mmap(int(f.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
		if err != nil {
			t.Fatal(err)
		}
		if _, e := preamble(data, testID); e != nil {
			t.Fatal(e)
		}
		s := &Segment{id: testID, purpose: PurposeStage, data: data, installed: true, regions: []regionState{{layout: Region{Offset: 64, Extent: 384, Discipline: Staged, Producer: Host}}}}
		defer s.release()
		deadline := time.Now().Add(3 * time.Second)
		for time.Now().Before(deadline) {
			turn, err := s.TryStageTurn(0, 1, 2, 0)
			if err != nil {
				t.Fatal(err)
			}
			if turn == nil {
				time.Sleep(time.Millisecond)
				continue
			}
			slot, err := stage.DecodeSlot(turn.Data)
			if err != nil {
				t.Fatal(err)
			}
			if slot.DoneThrough != 1 || slot.Data()[0] != 41 {
				t.Fatal("previous process writes invisible")
			}
			slot.Data()[0]++
			if err := turn.Finish(stage.Processed, 0); err != nil {
				t.Fatal(err)
			}
			return
		}
		t.Fatal("child never acquired turn")
		return
	}
	f := newFixture(t, Region{Offset: 64, Extent: 384, Discipline: Staged, Producer: Host})
	f.in.Purpose = PurposeStage
	r, err := New(Config{})
	if err != nil {
		t.Fatal(err)
	}
	defer r.Close()
	if _, err := f.send(t, r); err != nil {
		t.Fatal(err)
	}
	u, err := r.Install(f.in)
	if err != nil || u.Ack == nil || u.Ack.Failure != nil {
		t.Fatal(u, err)
	}
	s, err := r.Segment(testID)
	if err != nil {
		t.Fatal(err)
	}
	b := f.peer[64:448]
	le.PutUint32(b[28:], 1)
	le.PutUint32(b[32:], 1)
	le.PutUint32(b[36:], 1)
	le.PutUint64(b[56:], 64)
	for _, o := range []int{40, 44, 48, 68, 76, 84} {
		le.PutUint32(b[o:], 1)
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestStagedNativeProcessHandoff$")
	cmd.Env = append(os.Environ(), childEnv+"="+f.file.Name())
	var output bytes.Buffer
	cmd.Stdout = &output
	cmd.Stderr = &output
	if err := cmd.Start(); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if cmd.ProcessState == nil {
			cmd.Process.Kill()
			cmd.Wait()
		}
	})
	storeState(f.peer, 64, 0x0001000100000001)
	turn, err := s.TryStageTurn(0, 1, 1, 0)
	if err != nil || turn == nil {
		t.Fatal(turn, err)
	}
	turn.Data[320] = 41
	if err := turn.Finish(stage.Processed, 0); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Wait(); err != nil {
		t.Fatalf("child: %v\n%s", err, output.String())
	}
	if got := loadState(f.peer, 64); got != 0x0003000100000001 || f.peer[384] != 42 || le.Uint32(f.peer[84:]) != 2 {
		t.Fatalf("bad process handoff: %016x pixel=%d", got, f.peer[384])
	}
}
