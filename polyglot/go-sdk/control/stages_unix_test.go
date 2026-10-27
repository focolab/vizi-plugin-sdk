//go:build (linux || darwin) && (amd64 || arm64)

package control

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"math"
	"os"
	"runtime"
	"strings"
	"sync/atomic"
	"testing"
	"time"
	"unsafe"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"golang.org/x/sys/unix"
	"google.golang.org/protobuf/proto"
)

// Independent host double: real MAP_SHARED bytes, SCM_RIGHTS grants and gRPC.
// No SDK slot encoder constructs these headers. Every session gets the SAME id.
type stageHost struct {
	regions []*pb.Region
	id      uint64
	file    *os.File
	data    []byte
	word    *uint64
}

const stageSegmentID uint64 = 0x1200000001

func stageRing(t *testing.T, ids ...uint64) *stageHost {
	id := stageSegmentID
	if len(ids) > 0 {
		id = ids[0]
	}
	t.Helper()
	f, err := os.CreateTemp(t.TempDir(), "stage-ring")
	if err != nil {
		t.Fatal(err)
	}
	if err := f.Truncate(4096); err != nil {
		t.Fatal(err)
	}
	b, err := unix.Mmap(int(f.Fd()), 0, 4096, unix.PROT_READ|unix.PROT_WRITE, unix.MAP_SHARED)
	if err != nil {
		t.Fatal(err)
	}
	le := binary.LittleEndian
	le.PutUint64(b, id)
	copy(b[12:28], "shared-ring-id!!")
	le.PutUint32(b[28:32], crc32.ChecksumIEEE(b[:28]))
	h := &stageHost{file: f, data: b, word: (*uint64)(unsafe.Pointer(&b[64])), id: id}
	t.Cleanup(func() { unix.Munmap(b); f.Close() })
	return h
}
func stageShape() *pb.StageShape {
	return &pb.StageShape{Channels: []uint32{4, 7}, Z: 1, Y: 1, X: 4, Dtype: pb.DType_DTYPE_U8}
}
func (h *stageHost) fill(iter uint32, pipeline uint16, position int16) {
	b := h.data[64:448]
	clear(b[8:])
	le := binary.LittleEndian
	le.PutUint32(b[28:], 1)
	le.PutUint32(b[32:], 2)
	le.PutUint32(b[36:], 3)
	le.PutUint32(b[40:], 1)
	le.PutUint32(b[44:], 1)
	le.PutUint32(b[48:], 4)
	le.PutUint64(b[56:], 64)
	le.PutUint32(b[68:], 1)
	le.PutUint32(b[76:], 1)
	le.PutUint32(b[84:], 4)
	for _, o := range []int{112, 144, 176} {
		le.PutUint64(b[o:], math.Float64bits(1))
	}
	le.PutUint16(b[256:], 4)
	le.PutUint16(b[258:], 7)
	copy(b[320:], []byte{1, 2, 3, 4, 5, 6, 7, 8})
	// Host publishes only after all ordinary writes, independently packed bits.
	atomic.StoreUint64(h.word, uint64(iter)|uint64(pipeline)<<32|uint64(uint16(position))<<48)
}
func (h *stageHost) wait(t *testing.T, position int16) {
	t.Helper()
	deadline := time.Now().Add(3 * time.Second)
	for time.Now().Before(deadline) {
		if int16(atomic.LoadUint64(h.word)>>48) == position {
			return
		}
		time.Sleep(time.Millisecond)
	}
	t.Fatalf("turn never reached %d: %016x", position, atomic.LoadUint64(h.word))
}
func (h *stageHost) peer(t *testing.T, handlers map[string]StageHandlers, cfg Config) *peer {
	t.Helper()
	r, err := memory.New(memory.Config{})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		if err := r.Close(); err != nil {
			t.Error(err)
		}
	})
	m := NewMemory(r)
	cfg.StageMemory = m
	if cfg.Arrange == nil {
		cfg.Arrange = m.Apply
	}
	cfg.Stages = handlers
	cfg.StagePollInterval = 2 * time.Millisecond
	if cfg.StageNotify == nil {
		cfg.StageNotify = func(doorbell.Notification) error { return nil }
	}
	cfg.HandleRequest = func(ctx context.Context, c *Client, q *pb.HostMsg) (*pb.PluginMsg, error) {
		if plan := q.GetPlanStage(); plan != nil {
			handler := handlers[plan.StageId]
			var out *pb.StagePlanned
			var err error
			if handler.Plan != nil || handler.PlanWithParams != nil {
				out, err = handler.PlanRequest(ctx, c, plan)
			} else {
				out = &pb.StagePlanned{Output: proto.Clone(plan.Input).(*pb.StageShape)}
			}
			return &pb.PluginMsg{Msg: &pb.PluginMsg_StagePlanned{StagePlanned: out}}, err
		}
		return nil, &HostError{Kind: pb.Error_UNSUPPORTED, Message: "test barrier"}
	}
	p := openPeer(t, cfg, nil)
	h.grantTo(t, p, m)
	// Shutdown the stage worker before registry cleanup, including failed tests.
	t.Cleanup(func() {
		p.client.stages.cancel()
		select {
		case <-p.client.stages.done:
		case <-time.After(time.Second):
			t.Error("stage callback failed to drain")
		}
	})
	return p
}
func (h *stageHost) grantTo(t *testing.T, p *peer, m *Memory) {
	t.Helper()
	// Install first, then transfer the same file capability to this session.
	in := &pb.InstallSegment{SegmentId: h.id, Purpose: "S", Regions: []*pb.Region{{Offset: 64, Extent: 384, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_STAGED, Producer: pb.RegionProducer_REGION_PRODUCER_HOST}}}
	if h.regions != nil {
		in.Regions = h.regions
	}
	if err := m.Install(p.client, in); err != nil {
		t.Fatal(err)
	}
	typ := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		typ = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, typ, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer unix.Close(pair[0])
	done := make(chan error, 1)
	db, err := doorbell.Adopt(pair[1], doorbell.Config{Handler: func(e doorbell.Event) {
		if e.Frame != nil {
			done <- m.Grant(p.client, e.Frame)
		}
	}})
	if err != nil {
		t.Fatal(err)
	}
	defer db.Stop()
	raw, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: h.id, Size: 4096, Permissions: doorbell.PluginRead | doorbell.PluginWrite}, 1)
	if err != nil {
		t.Fatal(err)
	}
	if err := unix.Sendmsg(pair[0], raw[:], unix.UnixRights(int(h.file.Fd())), nil, 0); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	if ack := p.recv(t).GetGrantAck(); ack.GetInstalled() == nil {
		t.Fatal("S grant failed", ack)
	}
}
func stageArm(t *testing.T, p *peer, id string, pipeline, position uint32) *pb.StageArm {
	t.Helper()
	p.send(t, &pb.HostMsg{Id: freshID("plan"), Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: id, Input: stageShape()}}})
	out := stageRecv(t, p)
	if out.GetStagePlanned() == nil {
		t.Fatal(out)
	}
	a := &pb.StageArm{StageId: id, Revision: 1, SegmentId: stageSegmentID, Pipeline: pipeline, Position: position, Input: stageShape(), Output: proto.Clone(out.GetStagePlanned().Output).(*pb.StageShape)}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	ack := stageRecv(t, p).GetStageArmAck()
	if ack.GetStageId() != id || ack.Pipeline != pipeline || ack.Position != position || ack.Revision != 1 {
		t.Fatal(ack)
	}
	return a
}
func stageRecv(t *testing.T, p *peer) *pb.PluginMsg {
	t.Helper()
	for {
		m := p.recv(t)
		if m.GetContractViolation() == nil {
			return m
		}
	}
}
func stageBarrier(t *testing.T, p *peer) {
	t.Helper()
	p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{}}})
	if m := stageRecv(t, p); !strings.HasPrefix(m.Id, "barrier") {
		t.Fatal("unexpected ack", m)
	}
}

func TestStageTwoSessionsConsecutiveAndHostGap(t *testing.T) {
	h := stageRing(t)
	var calls1, calls2 atomic.Int32
	notifications := make(chan doorbell.Notification, 32)
	handler := func(count *atomic.Int32) StageHandlers {
		return StageHandlers{Process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			count.Add(1)
			s.Data()[0]++
			if a.Position == 2 && s.DoneThrough != 1 {
				return errors.New("previous outcome lost")
			}
			return nil
		}}
	}
	cfg := Config{StageNotify: func(n doorbell.Notification) error { notifications <- n; return nil }}
	p1 := h.peer(t, map[string]StageHandlers{"s": handler(&calls1)}, cfg)
	p2 := h.peer(t, map[string]StageHandlers{"s": handler(&calls2)}, cfg)
	stageArm(t, p1, "s", 1, 1)
	stageArm(t, p2, "s", 1, 2)
	// No D is delivered or relayed; both sessions must progress by polling.
	h.fill(math.MaxUint32, 1, 1)
	h.wait(t, 3)
	if h.data[384] != 3 || calls1.Load() != 1 || calls2.Load() != 1 {
		t.Fatal(h.data[384], calls1.Load(), calls2.Load())
	}
	for i := 0; i < 2; i++ {
		select {
		case n := <-notifications:
			if n.SegmentID != stageSegmentID || !n.Mask.Has(0) {
				t.Fatal(n)
			}
		case <-time.After(time.Second):
			t.Fatal("missing host D")
		}
	}
	if binary.LittleEndian.Uint32(h.data[84:]) != 2 {
		t.Fatal("done_through not advanced")
	}
	// Revisit same iteration at the same position: neither polling nor D reruns it.
	atomic.StoreUint64(h.word, uint64(math.MaxUint32)|uint64(1)<<32|uint64(1)<<48)
	n := doorbell.Notification{SegmentID: stageSegmentID}
	_ = n.Mask.Set(0)
	p1.client.HintStages(n)
	time.Sleep(12 * time.Millisecond)
	if calls1.Load() != 1 {
		t.Fatal("duplicate iteration ran")
	}
	h.fill(1, 1, 1)
	h.wait(t, 3) // equality-only wrap, not ordering
	if calls1.Load() != 2 || calls2.Load() != 2 {
		t.Fatal("wrap ignored")
	}
	p1.client.HintStages(n)
	if p1.client.IgnoredStageHints() != 1 {
		t.Fatal("unowned D not counted")
	}
	// Same stage in another pipeline, odd positions: stop at the host's gap.
	stageArm(t, p1, "s", 2, 1)
	stageArm(t, p1, "s", 2, 3)
	h.fill(2, 2, 1)
	h.wait(t, 2)
	time.Sleep(10 * time.Millisecond)
	if calls1.Load() != 3 {
		t.Fatal("host gap skipped")
	}
	atomic.StoreUint64(h.word, uint64(2)|uint64(2)<<32|uint64(3)<<48)
	h.wait(t, 4)
	if calls1.Load() != 4 {
		t.Fatal("second arm replaced first")
	}
}

func TestStageOutcomesAndCleanRefusal(t *testing.T) {
	for _, tc := range []struct {
		name            string
		check           func(context.Context, *Client, *pb.StageArm, stage.Info) error
		process         func(context.Context, *Client, *pb.StageArm, *stage.Slot) error
		corrupt         func([]byte)
		outcome, reason uint32
		wantByte        byte
		needs           bool
	}{
		{name: "causal refusal", check: func(context.Context, *Client, *pb.StageArm, stage.Info) error { return &stage.Refusal{Reason: 1} }, outcome: 2, reason: 1, wantByte: 1},
		{name: "changed parameters", check: func(context.Context, *Client, *pb.StageArm, stage.Info) error { return &stage.Refusal{Reason: 3} }, outcome: 2, reason: 3, wantByte: 1},
		{name: "missing dependency", needs: true, corrupt: func(b []byte) { binary.LittleEndian.PutUint16(b[258:], 8) }, outcome: 2, reason: 2, wantByte: 1},

		{name: "output bound uses immutable arm", process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			a.Output.X = 5
			s.Layout.Allocation[2] = 5
			return nil
		}, outcome: 3, reason: 2, wantByte: 1},
		{name: "output dtype differs from arm", process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Layout.DType = pb.DType_DTYPE_F32
			return nil
		}, outcome: 3, reason: 2, wantByte: 1},
		{name: "zero output allocation", process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Layout.Allocation[2] = 0
			s.Layout.Extent[2] = [2]uint32{0, 0}
			return nil
		}, outcome: 3, reason: 2, wantByte: 1},
		{name: "removed input plane", process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Layout.Channels = s.Layout.Channels[:1]
			return nil
		}, outcome: 3, reason: 2, wantByte: 1},
		{name: "growth within armed maximum", corrupt: func(b []byte) { binary.LittleEndian.PutUint32(b[48:], 2); binary.LittleEndian.PutUint32(b[84:], 2) }, process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Layout.Allocation[2] = 4
			return nil
		}, outcome: 3, reason: 2, wantByte: 1},
		{name: "partial write", process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Data()[0] = 99
			return errors.New("failed")
		}, outcome: 3, reason: 2, wantByte: 99},
		{name: "panic", process: func(_ context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
			s.Data()[0] = 99
			panic("failed")
		}, outcome: 3, reason: 2, wantByte: 99},
		{name: "late refusal is failure", process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { return &stage.Refusal{Reason: 1} }, outcome: 3, reason: 2, wantByte: 1},
		{name: "reserved", corrupt: func(b []byte) { b[216] = 1 }, outcome: 3, reason: 1, wantByte: 1},
		{name: "duplicate channel", corrupt: func(b []byte) { binary.LittleEndian.PutUint16(b[258:], 4) }, outcome: 3, reason: 1, wantByte: 1},
		{name: "wrong dtype", corrupt: func(b []byte) { binary.LittleEndian.PutUint32(b[28:], 2) }, outcome: 3, reason: 1, wantByte: 1},
		{name: "larger allocation", corrupt: func(b []byte) { binary.LittleEndian.PutUint32(b[48:], 5) }, outcome: 3, reason: 1, wantByte: 1},
	} {
		t.Run(tc.name, func(t *testing.T) {
			h := stageRing(t)
			var called atomic.Int32
			handler := StageHandlers{Check: tc.check, Process: func(ctx context.Context, c *Client, a *pb.StageArm, s *stage.Slot) error {
				called.Add(1)
				if tc.process != nil {
					return tc.process(ctx, c, a, s)
				}
				return nil
			}}
			if tc.needs {
				handler.Plan = func(_ context.Context, _ *Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
					return &pb.StagePlanned{Output: q.Input, Modifies: []*pb.StageChannelNeed{{Channel: 4, Needs: []uint32{7}}}}, nil
				}
			}
			p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{})
			stageArm(t, p, "s", 1, 2)
			h.fill(1, 1, 0)
			b := h.data[64:448]
			binary.LittleEndian.PutUint32(b[20:], 1)
			if tc.corrupt != nil {
				tc.corrupt(b)
			}
			before := bytes.Clone(b[28:])
			fixedBefore := bytes.Clone(b[12:24])
			atomic.StoreUint64(h.word, uint64(1)|uint64(1)<<32|uint64(2)<<48)
			h.wait(t, -2)
			if binary.LittleEndian.Uint32(b[8:]) != tc.outcome || binary.LittleEndian.Uint32(b[24:]) != tc.reason || binary.LittleEndian.Uint32(b[20:]) != 1 || b[320] != tc.wantByte {
				t.Fatal("wrong outcome", b[8:28], b[320])
			}
			if !bytes.Equal(fixedBefore, b[12:24]) || !bytes.Equal(before[:320-28], b[28:320]) {
				t.Fatal("non-success rewrote unrelated header fields")
			}
			if tc.outcome == 2 && (!bytes.Equal(before, b[28:]) || called.Load() != 0) {
				t.Fatal("refusal dirtied data or ran processor")
			}
			if tc.reason == 1 && tc.outcome == 3 && p.client.cfg.Reporter.Stats()[violations.StageSlotInvalid].Observed != 1 {
				t.Fatal("invalid header unreported")
			}
		})
	}
}

func TestStageArmRevisionsRetirementAndRefusal(t *testing.T) {
	h := stageRing(t)
	var calls atomic.Int32
	p := h.peer(t, map[string]StageHandlers{"s": {Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { calls.Add(1); return nil }}}, Config{})
	a := stageArm(t, p, "s", 1, 1)
	// An identical revision re-acks with both identity fields, without a new arm.
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.Pipeline != 1 || ack.Position != 1 || ack.Revision != 1 {
		t.Fatal(ack)
	}
	a.Revision = 2
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.Revision != 2 {
		t.Fatal(ack)
	}
	for _, change := range []func(*pb.StageArm){func(a *pb.StageArm) { a.Revision = 1 }, func(a *pb.StageArm) { a.Output.X = 3 }} {
		bad := proto.Clone(a).(*pb.StageArm)
		change(bad)
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: bad}})
		stageBarrier(t, p)
	}
	stats := p.client.cfg.Reporter.Stats()
	if stats[violations.ArrangementRegressed].Observed != 1 || stats[violations.ArrangementContentMismatch].Observed != 1 {
		t.Fatal(stats)
	}
	for _, change := range []func(*pb.StageArm){
		func(a *pb.StageArm) { a.Pipeline = 0 }, func(a *pb.StageArm) { a.Pipeline = 65536 }, func(a *pb.StageArm) { a.Position = 32767 },
		func(a *pb.StageArm) { a.StageId = "unknown" }, func(a *pb.StageArm) { a.SegmentId++ }, func(a *pb.StageArm) { a.Revision = 0 },
	} {
		bad := proto.Clone(a).(*pb.StageArm)
		change(bad)
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: bad}})
		stageBarrier(t, p)
	}
	if got := p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != 5 {
		t.Fatal(got)
	}
	if got := p.client.cfg.Reporter.Stats()[violations.ZeroID].Observed; got != 1 {
		t.Fatal("zero revision diagnostic", got)
	}
	h.fill(1, 1, 1)
	h.wait(t, 2)
	// A higher revision on the same ring retains per-slot iteration history.
	a.Revision = 3
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	stageRecv(t, p)
	atomic.StoreUint64(h.word, uint64(1)|uint64(1)<<32|uint64(1)<<48)
	time.Sleep(12 * time.Millisecond)
	if calls.Load() != 1 {
		t.Fatal("re-arm reran same volume")
	}
	atomic.StoreUint64(h.word, uint64(1)|uint64(1)<<32) // host drains before retiring
	if _, err := p.client.cfg.StageMemory.Registry.Retire(stageSegmentID); err != nil {
		t.Fatal(err)
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	stageBarrier(t, p)
	if got := p.client.cfg.Reporter.Stats()[violations.StageArmRefused].Observed; got != 6 {
		t.Fatal("retirement revived arm", got)
	}
}

func TestStageRepackCropAndAddChannel(t *testing.T) {
	h := stageRing(t)
	handler := StageHandlers{
		Plan: func(_ context.Context, _ *Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
			return &pb.StagePlanned{Output: &pb.StageShape{Z: 1, Y: 1, X: 2, Dtype: pb.DType_DTYPE_F32}, Adds: []*pb.StageNewChannel{{Key: "sum", Needs: []uint32{4, 7}}}}, nil
		},
		Process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			// Copy before widening/repacking; no full-volume SDK backup exists.
			first, _ := s.Plane(4)
			second, _ := s.Plane(7)
			values := [6]float32{float32(first[0]), float32(first[1]), float32(second[0]), float32(second[1]), float32(first[0]) + float32(second[0]), float32(first[1]) + float32(second[1])}
			for i, v := range values {
				binary.LittleEndian.PutUint32(s.Data()[4*i:], math.Float32bits(v))
			}
			s.Layout.DType = pb.DType_DTYPE_F32
			s.Layout.Allocation = [3]uint32{1, 1, 2}
			s.Layout.Extent = [3][2]uint32{{0, 1}, {0, 1}, {0, 2}}
			s.Layout.Channels = append(s.Layout.Channels, uint16(a.NewChannels["sum"]))
			s.Layout.Origin = [3]float64{0.5, 1.5, 2.5}
			s.Layout.ComposePose([9]float64{1, 0, 0, 0, 1, 0, 0, 0, 1}, [3]float64{1, 2, 3})
			return nil
		},
	}
	p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{})
	p.send(t, &pb.HostMsg{Id: freshID("plan"), Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape()}}})
	out := stageRecv(t, p).GetStagePlanned()
	a := &pb.StageArm{StageId: "s", Revision: 1, SegmentId: stageSegmentID, Pipeline: 65535, Position: 32766, Input: stageShape(), Output: out.Output, NewChannels: map[string]uint32{"sum": 65535}}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.Position != 32766 {
		t.Fatal(ack)
	}
	h.fill(1, 65535, 32766)
	h.wait(t, 32767)
	b := h.data[64:448]
	le := binary.LittleEndian
	if le.Uint32(b[8:]) != 1 || le.Uint32(b[20:]) != 32766 || le.Uint32(b[28:]) != 3 || le.Uint32(b[32:]) != 3 || le.Uint32(b[48:]) != 2 || le.Uint16(b[260:]) != 65535 {
		t.Fatal("bad repacked header")
	}
	for i, want := range []float32{1, 2, 5, 6, 6, 8} {
		if got := math.Float32frombits(le.Uint32(b[320+4*i:])); got != want {
			t.Fatal(i, got, want)
		}
	}
	if math.Float64frombits(le.Uint64(b[88:])) != 0.5 || math.Float64frombits(le.Uint64(b[200:])) != 3 {
		t.Fatal("placement/pose not committed")
	}
}

func TestStageCanCallHostAndShutdownWaitsForBorrow(t *testing.T) {
	h := stageRing(t)
	started := make(chan struct{})
	release := make(chan struct{})
	cancelled := make(chan struct{})
	answered := make(chan struct{})
	// Cleanup must free a deliberately stalled callback even when an assertion fails.
	var released atomic.Bool
	free := func() {
		if released.CompareAndSwap(false, true) {
			close(release)
		}
	}
	handler := StageHandlers{Process: func(ctx context.Context, c *Client, _ *pb.StageArm, s *stage.Slot) error {
		close(started)
		if _, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_GetViewState{GetViewState: &pb.GetViewState{}}}); err != nil {
			return err
		}
		close(answered)
		<-ctx.Done()
		close(cancelled)
		<-release
		s.Data()[0] = 42 // remains mapped until this function actually returns
		return ctx.Err()
	}}
	p := h.peer(t, map[string]StageHandlers{"s": handler}, Config{})
	t.Cleanup(free)
	stageArm(t, p, "s", 1, 1)
	h.fill(1, 1, 1)
	select {
	case <-started:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	request := stageRecv(t, p)
	if request.GetGetViewState() == nil {
		t.Fatal(request)
	}
	p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_ViewState{ViewState: &pb.ViewState{}}})
	stageBarrier(t, p) // processing cannot hold the control dispatch or memory mutex
	select {
	case <-answered:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(2000)}}})
	select {
	case <-cancelled:
	case <-p.ctx.Done():
		t.Fatal("stage did not receive cancellation")
	}
	if _, err := p.client.cfg.StageMemory.Registry.Retire(stageSegmentID); !errors.Is(err, memory.ErrStageActive) {
		t.Fatal("retired a live borrow", err)
	}
	if got := atomic.LoadUint64(h.word); got != 0x0001000100000001 {
		t.Fatal("locally timed out turn", got)
	}
	free()
	report := stageRecv(t, p)
	if report.GetShutdownReport() == nil {
		t.Fatal(report)
	}
	if h.data[384] != 42 || atomic.LoadUint64(h.word) != 0x0001000100000001 {
		t.Fatal("canceled callback published or lost mapping")
	}
}

func TestStagePreflightMetadataCannotMutateBorrow(t *testing.T) {
	h := stageRing(t)
	p := h.peer(t, map[string]StageHandlers{"s": {
		Check: func(_ context.Context, _ *Client, a *pb.StageArm, i stage.Info) error {
			a.Output.X = 0
			i.Layout.Channels[0] = 99
			i.Layout.Allocation[2] = 99
			return nil
		},
		Process: func(_ context.Context, _ *Client, a *pb.StageArm, s *stage.Slot) error {
			if a.Output.X != 4 || s.Layout.Channels[0] != 4 || s.Layout.Allocation[2] != 4 {
				return errors.New("preflight snapshot aliases processing")
			}
			return nil
		},
	}}, Config{})
	stageArm(t, p, "s", 1, 1)
	h.fill(1, 1, 1)
	h.wait(t, 2)
	if binary.LittleEndian.Uint32(h.data[72:]) != 1 {
		t.Fatal("preflight mutated borrowed state")
	}
}

func TestStageArmOutlivesRingAndFreezesDependencies(t *testing.T) {
	old := stageRing(t)
	var needsBoth atomic.Bool
	var calls atomic.Int32
	handler := StageHandlers{
		Plan: func(_ context.Context, _ *Client, q *pb.PlanStage) (*pb.StagePlanned, error) {
			needs := []uint32{4}
			if needsBoth.Load() {
				needs = append(needs, 7)
			}
			return &pb.StagePlanned{Output: q.Input, Modifies: []*pb.StageChannelNeed{{Channel: 4, Needs: needs}}}, nil
		},
		Process: func(context.Context, *Client, *pb.StageArm, *stage.Slot) error { calls.Add(1); return nil },
	}
	p := old.peer(t, map[string]StageHandlers{"s": handler}, Config{})
	a := stageArm(t, p, "s", 1, 1)
	withoutSeven := func(iter uint32) {
		old.fill(iter, 1, 0)
		binary.LittleEndian.PutUint16(old.data[64+258:], 8)
		atomic.StoreUint64(old.word, uint64(iter)|uint64(1)<<32|uint64(1)<<48)
		old.wait(t, 2)
	}
	withoutSeven(1)
	needsBoth.Store(true)
	p.send(t, &pb.HostMsg{Id: "replan", Msg: &pb.HostMsg_PlanStage{PlanStage: &pb.PlanStage{StageId: "s", Input: stageShape()}}})
	if stageRecv(t, p).GetStagePlanned() == nil {
		t.Fatal("replan failed")
	}
	withoutSeven(2) // Later planning must not change the original arm's needs.
	if calls.Load() != 2 {
		t.Fatal("replanning changed an installed arm")
	}
	m := p.client.cfg.StageMemory
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: old.id}}})
	if ack := stageRecv(t, p).GetSegmentReleased(); ack.GetSegmentId() != old.id {
		t.Fatal(ack)
	}
	next := stageRing(t, stageSegmentID+1)
	next.grantTo(t, p, m)
	a.SegmentId = next.id
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	stageBarrier(t, p)
	if p.client.cfg.Reporter.Stats()[violations.ArrangementContentMismatch].Observed != 1 {
		t.Fatal("retirement reset revision identity")
	}
	a.Revision = 2
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	if ack := stageRecv(t, p).GetStageArmAck(); ack.GetRevision() != 2 {
		t.Fatal(ack)
	}
	if p.client.cfg.Reporter.Stats()[violations.ArrangementFirstRevision].Observed != 0 {
		t.Fatal("replacement ring treated as a new identity")
	}
	next.fill(1, 1, 1)
	next.wait(t, 2) // Same iteration as old ring is fresh here.
	if calls.Load() != 3 {
		t.Fatal("new ring retained old iteration history")
	}
	next.fill(2, 1, 0)
	binary.LittleEndian.PutUint16(next.data[64+258:], 8)
	atomic.StoreUint64(next.word, uint64(2)|uint64(1)<<32|uint64(1)<<48)
	next.wait(t, -1)
	if calls.Load() != 3 || binary.LittleEndian.Uint32(next.data[64+24:]) != 2 {
		t.Fatal("new arm did not capture new dependencies")
	}
	a.Revision = 1
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_StageArm{StageArm: a}})
	stageBarrier(t, p)
	if p.client.cfg.Reporter.Stats()[violations.ArrangementRegressed].Observed != 1 {
		t.Fatal("stale revision accepted on replacement")
	}
}

func TestStageRetirementDrainsAndAcknowledgesWithoutRetry(t *testing.T) {
	h := stageRing(t)
	started := make(chan struct{})
	release := make(chan struct{})
	notifications := make(chan doorbell.Notification, 1)
	var released atomic.Bool
	free := func() {
		if released.CompareAndSwap(false, true) {
			close(release)
		}
	}
	p := h.peer(t, map[string]StageHandlers{"s": {Process: func(ctx context.Context, _ *Client, _ *pb.StageArm, s *stage.Slot) error {
		close(started)
		select {
		case <-release:
		case <-ctx.Done():
			return ctx.Err()
		}
		s.Data()[0] = 42
		return nil
	}}}, Config{StageNotify: func(n doorbell.Notification) error { notifications <- n; return nil }})
	t.Cleanup(free)
	stageArm(t, p, "s", 1, 1)
	h.fill(1, 1, 1)
	select {
	case <-started:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	m := p.client.cfg.StageMemory
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: h.id}}})
	stageBarrier(t, p) // No early SegmentReleased, and control remains live.
	free()
	if ack := stageRecv(t, p).GetSegmentReleased(); ack.GetSegmentId() != h.id {
		t.Fatal(ack)
	}
	if word := atomic.LoadUint64(h.word); word != 0x0002000100000001 || h.data[384] != 42 {
		t.Fatalf("held turn abandoned at retirement: %x", word)
	}
	select {
	case n := <-notifications:
		if n.SegmentID != h.id || !n.Mask.Has(0) {
			t.Fatal(n)
		}
	case <-p.ctx.Done():
		t.Fatal("retiring turn did not send D")
	}
	if _, err := m.Registry.Segment(h.id); !errors.Is(err, memory.ErrNotInstalled) {
		t.Fatal("registry not settled", err)
	}
}
