//go:build linux || darwin

package plugin

import (
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"net"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"sync/atomic"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

type chunkPeer struct {
	t           *testing.T
	ctx         context.Context
	stream      pb.Plugin_SessionClient
	messages    chan *pb.PluginMsg
	diagnostics chan error
	s           *control.Session
	o           *ChunkOverlay
	fd          int
	seq         uint32
	files       map[uint64]*os.File
}

func openChunkPeer(t *testing.T, source ChunkSource) *chunkPeer {
	return openChunkPeerWithSetup(t, source, nil)
}

func openChunkPeerWithSetup(t *testing.T, source ChunkSource, setup func() (control.Plugin, error)) *chunkPeer {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	t.Cleanup(cancel)
	typ := unix.SOCK_SEQPACKET
	if runtime.GOOS == "darwin" {
		typ = unix.SOCK_DGRAM
	}
	pair, err := unix.Socketpair(unix.AF_UNIX, typ, 0)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { unix.Close(pair[0]) })
	dir, err := os.MkdirTemp("", "vizi-chunks-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(dir) })
	path := filepath.Join(dir, "plugin.sock")
	t.Setenv("VIZI_PLUGIN_DOORBELL_FD", strconv.Itoa(pair[1]))
	t.Setenv("VIZI_PLUGIN_UDS", path)
	o, _ := NewChunkOverlay("o", source)
	p := &chunkPeer{t: t, ctx: ctx, o: o, fd: pair[0], messages: make(chan *pb.PluginMsg, 32), diagnostics: make(chan error, 32), files: make(map[uint64]*os.File)}
	var cfg control.Plugin
	if setup != nil {
		cfg, err = setup()
	} else {
		cfg.Control, err = (Handlers{ChunkOverlays: map[string]*ChunkOverlay{"o": o}}).Configure(control.Config{PluginID: "chunks.test", Diagnostic: func(err error) { p.diagnostics <- err }})
		cfg.Started = func(s *control.Session) {
			if err := o.Bind(s); err != nil {
				s.Client.Fail(err)
			}
		}
	}
	if err != nil {
		t.Fatal(err)
	}
	started := make(chan *control.Session, 1)
	onStarted := cfg.Started
	cfg.Started = func(s *control.Session) {
		if onStarted != nil {
			onStarted(s)
		}
		started <- s
	}
	done := make(chan error, 1)
	go func() { done <- control.ServeEnv(ctx, cfg) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(time.Second):
			t.Error("session did not stop")
		}
	})
	conn, err := grpc.NewClient("passthrough:///chunks", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", path)
	}))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	p.stream, err = pb.NewPluginClient(conn).Session(ctx, grpc.WaitForReady(true))
	if err != nil {
		t.Fatal(err)
	}
	go func() {
		defer close(p.messages)
		for {
			m, err := p.stream.Recv()
			if err != nil {
				return
			}
			select {
			case p.messages <- m:
			case <-ctx.Done():
				return
			}
		}
	}()
	p.send(&pb.HostMsg{Id: "Hello from chunk host 1.0", Msg: &pb.HostMsg_Hello{Hello: &pb.Hello{ProtocolMajor: 1}}})
	if p.recv().GetReady() == nil {
		t.Fatal("missing ready")
	}
	select {
	case p.s = <-started:
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	p.grant(1, 0)
	return p
}
func (p *chunkPeer) send(m *pb.HostMsg) {
	p.t.Helper()
	if err := p.stream.Send(m); err != nil {
		p.t.Fatal(err)
	}
}
func (p *chunkPeer) recv() *pb.PluginMsg {
	p.t.Helper()
	select {
	case m := <-p.messages:
		if m == nil {
			p.t.Fatal("stream ended")
		}
		return m
	case <-p.ctx.Done():
		p.t.Fatal(p.ctx.Err())
		return nil
	}
}
func (p *chunkPeer) quiet() {
	p.t.Helper()
	select {
	case m := <-p.messages:
		p.t.Fatal("unexpected output", m)
	case <-time.After(80 * time.Millisecond):
	}
}
func (p *chunkPeer) wants(ws ...*pb.ChunkWant) {
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_ChunksWanted{ChunksWanted: &pb.ChunksWanted{OverlayId: "o", Wants: ws}}})
}
func (p *chunkPeer) barrier() {
	p.send(&pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 1}}})
	if p.recv().GetGrantState() == nil {
		p.t.Fatal("missing receive barrier")
	}
}
func (p *chunkPeer) release(c *pb.OverlayChunk) {
	d := c.Labels.GetSegment()
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: d.SegmentId, Region: 0, Offset: d.Offset, Len: d.Len}}})
}
func (p *chunkPeer) grant(id, old uint64) {
	p.t.Helper()
	file, err := os.CreateTemp(p.t.TempDir(), "arena")
	if err != nil {
		p.t.Fatal(err)
	}
	p.t.Cleanup(func() { file.Close() })
	p.files[id] = file
	data := make([]byte, 256)
	binary.LittleEndian.PutUint64(data, id)
	copy(data[12:28], "chunk-proof-1234")
	binary.LittleEndian.PutUint32(data[28:32], crc32.ChecksumIEEE(data[:28]))
	if _, err := file.Write(data); err != nil {
		p.t.Fatal(err)
	}
	p.seq++
	raw, err := doorbell.EncodeMemory(doorbell.Memory{SegmentID: id, Size: 256, Permissions: doorbell.PluginRead | doorbell.PluginWrite | doorbell.HostRead}, p.seq)
	if err != nil {
		p.t.Fatal(err)
	}
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: &pb.InstallSegment{SegmentId: id, Supersedes: old, Purpose: "O", Subject: "o", Regions: []*pb.Region{{Index: 0, Offset: 64, Extent: 192, Discipline: pb.RegionDiscipline_REGION_DISCIPLINE_LOANED, Producer: pb.RegionProducer_REGION_PRODUCER_PLUGIN}}}}})
	if err := unix.Sendmsg(p.fd, raw[:], unix.UnixRights(int(file.Fd())), nil, 0); err != nil {
		p.t.Fatal(err)
	}
	if a := p.recv().GetGrantAck(); a.GetSegmentId() != id || a.GetInstalled() == nil {
		p.t.Fatal("grant", a)
	}
	if old != 0 {
		if r := p.recv().GetSegmentReleased(); r.GetSegmentId() != old {
			p.t.Fatal("supersede release", r)
		}
	}
}
func testChunk(value byte) *Chunk {
	v, _ := payload.Encode([]byte{value}, []uint32{1, 1, 1}, true)
	return &Chunk{Labels: v}
}
func (p *chunkPeer) chunk(tval uint32, value byte) *pb.OverlayChunk {
	p.t.Helper()
	c := p.recv().GetOverlayChunk()
	if c == nil || c.T == nil || *c.T != tval || c.Labels == nil || c.Labels.GetSegment() == nil {
		p.t.Fatal("chunk", c)
	}
	d := c.Labels.GetSegment()
	data := make([]byte, 1)
	if _, err := p.files[d.SegmentId].ReadAt(data, int64(d.Offset)); err != nil {
		p.t.Fatal(err)
	}
	if data[0] != value {
		p.t.Fatalf("chunk value %d, want %d", data[0], value)
	}
	return c
}
func timedWant(t uint32) *pb.ChunkWant { return &pb.ChunkWant{T: proto.Uint32(t)} }

func TestChunkSourceOrderMembershipAndInvalidate(t *testing.T) {
	calls := make(chan uint32, 32)
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		calls <- w.GetT()
		return testChunk(byte(w.GetT())), nil
	})
	p.wants(timedWant(2), timedWant(0))
	p.release(p.chunk(2, 2))
	p.release(p.chunk(0, 0))
	p.wants(timedWant(2), timedWant(0))
	p.quiet()
	p.wants()
	p.wants(timedWant(2), timedWant(0)) // closer than any worker tick
	p.release(p.chunk(2, 2))
	p.release(p.chunk(0, 0))
	p.o.InvalidateChunks()
	p.release(p.chunk(2, 2))
	p.release(p.chunk(0, 0))
	if len(calls) != 6 {
		t.Fatal("source not once per membership", len(calls))
	}
	p.wants(timedWant(0), timedWant(3))
	p.release(p.chunk(3, 3))
	p.quiet()
}

func TestChunkDeferredAnswerInvalidationAndLeaving(t *testing.T) {
	var version atomic.Uint32
	version.Store(1)
	computed := make(chan uint32, 32)
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		computed <- w.GetT()
		return testChunk(byte(version.Load())), nil
	})
	// Three 64-byte aligned loans exactly fill the arena. The fourth must wait.
	p.wants(timedWant(0), timedWant(1), timedWant(2), timedWant(3))
	held := []*pb.OverlayChunk{p.chunk(0, 1), p.chunk(1, 1), p.chunk(2, 1)}
	for i := 0; i < 4; i++ {
		select {
		case <-computed:
		case <-p.ctx.Done():
			t.Fatal(p.ctx.Err())
		}
	}
	p.quiet()
	seg, _ := p.s.Memory.Registry.Segment(1)
	if stats, _ := seg.LoanStats(0); stats.Refused != 0 {
		t.Fatal("deferral counted arena refusal", stats)
	}
	if p.s.Client.QueueStats().Refused != 0 {
		t.Fatal("deferral counted refusal")
	}
	version.Store(2)
	p.o.InvalidateChunks()
	// Invalidating while full must recompute, not publish the old deferred 3.
	select {
	case v := <-computed:
		if v != 0 {
			t.Fatal("invalidate order", v)
		}
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	p.quiet()
	for _, c := range held {
		p.release(c)
	}
	for i := uint32(0); i < 4; i++ {
		p.release(p.chunk(i, 2))
	}
	p.quiet()
	// Same answer is retained on a plain release, and a departed pending want
	// is not resurrected when its key comes back before the worker wakes.
	p.wants()
	p.wants(timedWant(4), timedWant(5), timedWant(6), timedWant(7))
	held = []*pb.OverlayChunk{p.chunk(4, 2), p.chunk(5, 2), p.chunk(6, 2)}
	p.quiet()
	version.Store(3)
	for _, c := range held {
		p.release(c)
	}
	p.release(p.chunk(7, 2))
	p.wants()
	p.wants(timedWant(0), timedWant(1), timedWant(2), timedWant(3))
	held = []*pb.OverlayChunk{p.chunk(0, 3), p.chunk(1, 3), p.chunk(2, 3)}
	p.quiet()
	p.wants(timedWant(0), timedWant(1), timedWant(2))
	p.barrier()
	for _, c := range held {
		p.release(c)
	}
	p.quiet()
	p.wants(timedWant(3))
	p.release(p.chunk(3, 3))
	select {
	case err := <-p.diagnostics:
		t.Fatal(err)
	default:
	}
}

func TestChunkManualStaticTableAndSupersede(t *testing.T) {
	p := openChunkPeer(t, nil)
	p.wants(&pb.ChunkWant{Level: 2})
	p.barrier()
	ws := p.o.Wants()
	if len(ws) != 1 || ws[0].T != nil {
		t.Fatal(ws)
	}
	ws[0].Level = 9
	if err := p.o.PublishChunk(&pb.ChunkWant{Level: 2}, nil); err != nil {
		t.Fatal(err)
	}
	if c := p.recv().GetOverlayChunk(); c == nil || c.Level != 2 || c.T != nil || c.Labels != nil || len(c.PositionZyx) != 0 {
		t.Fatal(c)
	}
	lut := [256][4]uint8{}
	lut[1] = [4]uint8{10, 20, 30, 40}
	if err := p.o.SetLabelTable(lut, map[uint32]string{1: "one"}, map[uint32]bool{1: false}, &pb.OverlayStyle{Opacity: 0.6}); err != nil {
		t.Fatal(err)
	}
	table := p.recv().GetOverlayLabelTable()
	if table == nil || len(table.Colormap.GetInline()) != 1024 || table.Colormap.GetInline()[4] != 10 || table.LabelNames[1] != "one" || table.LabelVisible[1] || table.Style.GetOpacity() != 0.6 {
		t.Fatal(table)
	}
	p.wants()
	p.barrier()
	p.grant(2, 1)
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 1}}})
	if r := p.recv().GetSegmentReleased(); r.GetSegmentId() != 1 {
		t.Fatal("supersede retirement replay", r)
	}
	p.wants(timedWant(0))
	p.barrier()
	if err := p.o.PublishChunk(timedWant(0), testChunk(7)); err != nil {
		t.Fatal(err)
	}
	c := p.chunk(0, 7)
	if c.Labels.GetSegment().SegmentId != 2 {
		t.Fatal("reserved in superseded arena")
	}
	p.release(c)
	p.wants()
	p.barrier()
	if err := p.o.PublishChunk(timedWant(0), nil); !errors.Is(err, ErrChunkNotWanted) {
		t.Fatal(err)
	}
}

func TestChunkSourceFailureAndShutdownJoin(t *testing.T) {
	entered := make(chan struct{})
	exited := make(chan struct{})
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		switch w.GetT() {
		case 0:
			return nil, memory.ErrArenaFull
		case 1:
			panic("bad source")
		case 2:
			return nil, nil
		default:
			close(entered)
			<-ctx.Done()
			close(exited)
			return nil, ctx.Err()
		}
	})
	p.wants(timedWant(0), timedWant(1), timedWant(2), timedWant(3))
	if c := p.recv().GetOverlayChunk(); c == nil || c.GetT() != 2 || c.Labels != nil {
		t.Fatal(c)
	}
	select {
	case <-entered:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	for i := 0; i < 2; i++ {
		select {
		case <-p.diagnostics:
		case <-p.ctx.Done():
			t.Fatal("missing source diagnostic")
		}
	}
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	if r := p.recv().GetShutdownReport(); r.GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(r)
	}
	select {
	case <-exited:
	default:
		t.Fatal("reported shutdown before source exited")
	}
}

// A source whose host request fails because Shutdown arrived returns the
// SessionEnded error; the worker ends that want quietly -- no diagnostic, as
// for any handler a session's end stops.
func TestChunkSourceEndingWithTheSessionIsQuiet(t *testing.T) {
	returned := make(chan error, 1)
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		// Not the source's ctx: Stop cancels that, and the request must fail by
		// the session's end, not by cancellation.
		_, err := s.Client.GetViewState(context.Background())
		returned <- err
		return nil, err
	})
	p.wants(timedWant(0))
	if q := p.recv(); q.GetGetViewState() == nil {
		t.Fatal("want the source's GetViewState, got", q)
	}
	p.send(&pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
	select {
	case err := <-returned:
		if !errors.Is(err, control.ErrSessionEnded) {
			t.Fatalf("the source's request failed with %v, want SessionEnded", err)
		}
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	if r := p.recv().GetShutdownReport(); r.GetOutcome() != pb.ShutdownReport_COMPLETED {
		t.Fatal(r)
	}
	select {
	case err := <-p.diagnostics:
		t.Fatal("a session-ended source was diagnosed:", err)
	case <-time.After(80 * time.Millisecond):
	}
}

func TestChunkEagerMembershipWhileSourceBlocked(t *testing.T) {
	entered, unblock := make(chan struct{}), make(chan struct{})
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		if w.GetT() == 1 {
			close(entered)
			select {
			case <-unblock:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return testChunk(byte(w.GetT())), nil
	})
	p.wants(timedWant(0))
	p.release(p.chunk(0, 0))
	p.wants(timedWant(0), timedWant(1))
	select {
	case <-entered:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	// Worker cannot observe the empty set: both restatements are applied while
	// it is held in the source, and the receive barrier proves dispatch continued.
	p.wants()
	p.wants(timedWant(0))
	p.barrier()
	close(unblock)
	p.release(p.chunk(0, 0))
	p.quiet()
}

func TestChunkHostWantBoundDiagnosedWithoutFault(t *testing.T) {
	p := openChunkPeer(t, nil)
	wants := make([]*pb.ChunkWant, 9)
	for i := range wants {
		wants[i] = timedWant(uint32(i))
	}
	p.wants(wants...)
	p.barrier()
	if got := p.o.Wants(); len(got) != 8 || got[7].GetT() != 7 {
		t.Fatal("want bound", got)
	}
	select {
	case <-p.diagnostics:
	case <-p.ctx.Done():
		t.Fatal("missing over-bound diagnostic")
	}
	if err := p.o.PublishChunk(timedWant(0), nil); err != nil {
		t.Fatal("host defect faulted session", err)
	}
	if c := p.recv().GetOverlayChunk(); c == nil || c.T == nil || *c.T != 0 {
		t.Fatal(c)
	}
}

// Unlike the earlier eager-membership test, the SAME key leaves while its
// source is running and returns before that source finishes (dataset change).
func TestChunkWithdrawnRunningWantIsRecomputedWhenItReturns(t *testing.T) {
	entered, unblock := make(chan struct{}), make(chan struct{})
	var calls atomic.Uint32
	p := openChunkPeer(t, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*Chunk, error) {
		n := calls.Add(1)
		if n == 1 {
			close(entered)
			select {
			case <-unblock:
			case <-ctx.Done():
				return nil, ctx.Err()
			}
		}
		return testChunk(byte(n)), nil
	})
	p.wants(timedWant(0))
	select {
	case <-entered:
	case <-p.ctx.Done():
		t.Fatal(p.ctx.Err())
	}
	p.wants()
	p.wants(timedWant(0))
	p.barrier() // both sets applied while the old source is still blocked
	close(unblock)
	p.release(p.chunk(0, 2)) // value 1 must never cross the wire
	p.quiet()
	if calls.Load() != 2 {
		t.Fatal("returning want was not recomputed", calls.Load())
	}
}

func TestDefinitionChunkBindingUsesFreshRoutedHandle(t *testing.T) {
	d := NewDefinition(Info{ID: "chunks.test", Name: "Chunks", Version: "1"})
	d.Command(CommandSpec{ID: "start", Title: "Start", Menu: ToolsMenu()}, noopCommand)
	ref := d.ChunkOverlay(OverlaySpec{ID: "o", Time: "static"}, func(context.Context, *control.Session, *pb.ChunkWant) (*Chunk, error) { return nil, nil })
	bound := make(chan *ChunkOverlay, 1)
	var started atomic.Bool
	d.OnChunkOverlay(ref, func(o *ChunkOverlay) {
		if !started.Load() {
			t.Error("binding preceded application startup")
		}
		if o.session != nil {
			t.Error("worker started before binding callback")
		}
		bound <- o
	})
	app := buildDefinition(t, d)
	var previous *ChunkOverlay
	for _, name := range []string{"first", "second"} {
		t.Run(name, func(t *testing.T) {
			started.Store(false)
			p := openChunkPeerWithSetup(t, nil, func() (control.Plugin, error) {
				return app.Configure(control.Plugin{Started: func(*control.Session) { started.Store(true) }})
			})
			var handle *ChunkOverlay
			select {
			case handle = <-bound:
			case <-p.ctx.Done():
				t.Fatal("no bound handle")
			}
			if handle == previous || handle == d.h.ChunkOverlays["o"] {
				t.Fatal("session reused an overlay handle")
			}
			previous = handle
			p.wants(&pb.ChunkWant{Level: 0})
			if m := p.recv(); m.GetOverlayChunk() == nil {
				t.Fatalf("missing chunk: %v", m)
			}
			if len(handle.Wants()) != 1 {
				t.Fatal("callback handle does not own routed wants")
			}
			handle.InvalidateChunks()
			if m := p.recv(); m.GetOverlayChunk() == nil {
				t.Fatalf("invalidation did not rerun source: %v", m)
			}
		})
	}
}
