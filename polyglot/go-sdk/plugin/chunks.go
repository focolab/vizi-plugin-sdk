package plugin

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// Chunk owns labels on the wanted level's lattice. Nil Labels (or a nil Chunk)
// is empty state and reserves nothing. Labels are a ZYX u8/u16 tensor in the
// deployment's byte order; PositionZYX is in that level's voxel indices.
type Chunk struct {
	Labels      *payload.Tensor
	PositionZYX geometry.ZYX
}

// ChunkSource runs serially per overlay, off the dispatch path. It must honor
// cancellation. The returned value remains SDK-owned until published/discarded;
// do not mutate it. An error or panic is diagnosed and the next want is served.
type ChunkSource func(context.Context, *control.Session, *pb.ChunkWant) (*Chunk, error)

type chunkKey struct {
	level, t uint32
	hasT     bool
}

func wantKey(w *pb.ChunkWant) chunkKey {
	return chunkKey{w.GetLevel(), w.GetT(), w != nil && w.T != nil}
}

type membership struct{ served bool }

// ChunkOverlay owns a want set and an optional source worker for one session.
// Register it in Handlers.ChunkOverlays and Bind it in Plugin.Started. Sets that
// arrive before Bind are retained. A nil source supports Wants/PublishChunk by
// hand; the application then owns retrying ErrArenaFull.
type ChunkOverlay struct {
	mu      sync.Mutex
	id      string
	source  ChunkSource
	session *control.Session
	wants   []*pb.ChunkWant
	members map[chunkKey]*membership
	wake    chan struct{}
	cancel  context.CancelFunc
	done    chan struct{}
	stopped bool
}

func NewChunkOverlay(id string, source ChunkSource) (*ChunkOverlay, error) {
	if id == "" {
		return nil, errors.New("chunk overlay ID is required")
	}
	return &ChunkOverlay{id: id, source: source, members: make(map[chunkKey]*membership), wake: make(chan struct{}, 1)}, nil
}

func (o *ChunkOverlay) signal() {
	select {
	case o.wake <- struct{}{}:
	default:
	}
}

// Bind attaches the handle once to its serving session, starting its worker.
func (o *ChunkOverlay) Bind(s *control.Session) error {
	if s == nil || s.Client == nil || s.Memory == nil || s.Memory.Registry == nil {
		return errors.New("chunk overlay requires a session")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.stopped {
		return control.ErrShuttingDown
	}
	if o.session != nil {
		return errors.New("chunk overlay already bound")
	}
	o.session = s
	if o.source != nil {
		ctx, cancel := context.WithCancel(s.Client.Context())
		o.cancel = cancel
		o.done = make(chan struct{})
		go func() { defer close(o.done); o.run(ctx, s) }()
	}
	return nil
}

// Stop cancels and joins the source before shutdown reports completion.
func (o *ChunkOverlay) Stop() {
	o.mu.Lock()
	o.stopped = true
	if o.cancel != nil {
		o.cancel()
	}
	done := o.done
	o.mu.Unlock()
	if done != nil {
		<-done
	}
}

func (o *ChunkOverlay) wanted(q *pb.ChunksWanted) error {
	if q == nil || q.OverlayId != o.id {
		return errors.New("chunk want overlay mismatch")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.stopped {
		return nil
	}
	next := make(map[chunkKey]*membership)
	wants := make([]*pb.ChunkWant, 0, min(len(q.Wants), 8))
	for _, w := range q.Wants[:min(len(q.Wants), 8)] {
		if w == nil {
			continue
		}
		key := wantKey(w)
		if next[key] != nil {
			continue
		}
		entry := o.members[key]
		if entry == nil {
			entry = &membership{}
		}
		next[key] = entry
		wants = append(wants, proto.Clone(w).(*pb.ChunkWant))
	}
	// Removing membership here also invalidates in-flight/deferred answers, even
	// when the identical key returns before the worker gets another turn.
	o.members, o.wants = next, wants
	o.signal()
	return nil
}

// Wants returns independent snapshots in the host's priority order.
func (o *ChunkOverlay) Wants() []*pb.ChunkWant {
	o.mu.Lock()
	defer o.mu.Unlock()
	out := make([]*pb.ChunkWant, len(o.wants))
	for i, w := range o.wants {
		out[i] = proto.Clone(w).(*pb.ChunkWant)
	}
	return out
}

// InvalidateChunks recomputes all current wants and discards any pending answer.
func (o *ChunkOverlay) InvalidateChunks() {
	o.mu.Lock()
	defer o.mu.Unlock()
	for key := range o.members {
		o.members[key] = &membership{}
	}
	o.signal()
}

var ErrChunkNotWanted = errors.New("chunk is no longer wanted")

// PublishChunk sends only a currently wanted key. It reserves in this overlay's
// arena and returns ErrArenaFull without a shortfall; capture LoanChanges before
// calling when managing deferral by hand. Successful admission transfers the loan.
func (o *ChunkOverlay) PublishChunk(want *pb.ChunkWant, chunk *Chunk) error {
	if want == nil {
		return errors.New("chunk want is required")
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	entry := o.members[wantKey(want)]
	if entry == nil {
		return ErrChunkNotWanted
	}
	if err := o.publish(want, chunk); err != nil {
		return err
	}
	entry.served = true
	return nil
}

func (o *ChunkOverlay) publish(w *pb.ChunkWant, chunk *Chunk) error {
	if o.stopped {
		return control.ErrShuttingDown
	}
	s := o.session
	if s == nil {
		return errors.New("chunk overlay is not bound")
	}
	out := &pb.OverlayChunk{OverlayId: o.id, Level: w.Level}
	if w.T != nil {
		out.T = proto.Uint32(*w.T)
	}
	msg := &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayChunk{OverlayChunk: out}}
	if chunk == nil || chunk.Labels == nil {
		return s.Client.Send(msg)
	}
	labels := chunk.Labels
	if len(labels.Shape()) != 3 || (labels.DType() != pb.DType_DTYPE_U8 && labels.DType() != pb.DType_DTYPE_U16) {
		return errors.New("chunk labels require a ZYX u8 or u16 tensor")
	}
	for _, p := range chunk.PositionZYX {
		if math.IsNaN(p) || math.IsInf(p, 0) || p < 0 || p > math.MaxUint32 || p != math.Trunc(p) {
			return errors.New("chunk position requires nonnegative integer level indices")
		}
	}
	out.PositionZyx = []uint32{uint32(chunk.PositionZYX[0]), uint32(chunk.PositionZYX[1]), uint32(chunk.PositionZYX[2])}
	seg, err := s.Memory.Registry.FindContribution(memory.PurposeOverlay, o.id)
	if err != nil {
		if s.Memory.DropRetiredPublication(err) {
			return nil
		}
		return err
	}
	return s.PublishChunkLoan(seg.ID(), labels.Bytes(), func(ref *pb.SegmentRef) (*pb.PluginMsg, error) {
		ref.Dtype, ref.ShapeZyx = labels.DType(), labels.Shape()
		out.Labels = &pb.GeomBuffer{Dtype: labels.DType(), Shape: labels.Shape(), Source: &pb.GeomBuffer_Segment{Segment: ref}}
		return msg, nil
	})
}

// SetLabelTable sends a whole inline 256x4 u8 RGBA table under its own coalescing
// key. Names/visibility are keyed by label ID. No ordering with chunks is promised.
func (o *ChunkOverlay) SetLabelTable(lut [256][4]uint8, names map[uint32]string, visible map[uint32]bool, style *pb.OverlayStyle) error {
	data := make([]byte, 0, 1024)
	for _, row := range lut {
		data = append(data, row[:]...)
	}
	table := &pb.OverlayLabelTable{OverlayId: o.id, Colormap: &pb.GeomBuffer{Dtype: pb.DType_DTYPE_U8, Shape: []uint32{256, 4}, Source: &pb.GeomBuffer_Inline{Inline: data}}, LabelNames: maps.Clone(names), LabelVisible: maps.Clone(visible)}
	if style != nil {
		table.Style = proto.Clone(style).(*pb.OverlayStyle)
	}
	o.mu.Lock()
	defer o.mu.Unlock()
	if o.stopped {
		return control.ErrShuttingDown
	}
	if o.session == nil {
		return errors.New("chunk overlay is not bound")
	}
	return o.session.Client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayLabelTable{OverlayLabelTable: table}})
}

func (o *ChunkOverlay) compute(ctx context.Context, s *control.Session, w *pb.ChunkWant) (chunk *Chunk, err error) {
	defer func() {
		if p := recover(); p != nil {
			err = fmt.Errorf("chunk source panic: %v", p)
		}
	}()
	return o.source(ctx, s, proto.Clone(w).(*pb.ChunkWant))
}

func (o *ChunkOverlay) run(ctx context.Context, s *control.Session) {
	for ctx.Err() == nil {
		o.mu.Lock()
		var w *pb.ChunkWant
		var entry *membership
		for _, candidate := range o.wants {
			if e := o.members[wantKey(candidate)]; !e.served {
				w, entry = candidate, e
				break
			}
		}
		o.mu.Unlock()
		if w == nil {
			select {
			case <-ctx.Done():
				return
			case <-o.wake:
			}
			continue
		}
		chunk, err := o.compute(ctx, s, w)
		if err != nil {
			o.mu.Lock()
			if o.members[wantKey(w)] == entry {
				entry.served = true
			}
			o.mu.Unlock()
			if ctx.Err() == nil {
				s.Client.Diagnose(fmt.Errorf("chunk source %s: %w", o.id, err))
			}
			continue
		}
		for ctx.Err() == nil {
			released := s.Memory.LoanChanges()
			o.mu.Lock()
			if o.members[wantKey(w)] != entry {
				o.mu.Unlock()
				break
			}
			if err == nil {
				err = o.publish(w, chunk)
			}
			full := errors.Is(err, memory.ErrArenaFull)
			if !full {
				entry.served = true
			}
			o.mu.Unlock()
			if !full {
				if err != nil {
					s.Client.Diagnose(fmt.Errorf("chunk source %s level=%d t=%v: %w", o.id, w.Level, w.T, err))
				}
				break
			}
			select {
			case <-ctx.Done():
				return
			case <-released:
			case <-o.wake:
			}
			err = nil // retry the same answer unless its membership was invalidated
		}
	}
}
