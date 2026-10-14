package plugin

import (
	"errors"
	"math"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// GeometryEncoder produces the published inline geometry shapes. ByteOrder is
// a deployment input because GeomBuffer has no endian field. Item transforms
// explicitly use voxel coordinates; the wire's empty frame means world.
type GeometryEncoder struct{ ByteOrder payload.ByteOrder }

func (e GeometryEncoder) points(points []geometry.ZYX) (*pb.GeomBuffer, error) {
	if err := e.ByteOrder.Validate(); err != nil {
		return nil, err
	}
	if len(points) == 0 || len(points) > (control.MaxInlineBytes/24) {
		return nil, errors.New("point array is empty or exceeds inline geometry limit")
	}
	values := make([]float64, 0, len(points)*3)
	for _, p := range points {
		for _, v := range p {
			if math.IsNaN(v) || math.IsInf(v, 0) {
				return nil, errors.New("geometry coordinates must be finite")
			}
		}
		values = append(values, p[:]...)
	}
	tensor, err := payload.Encode(values, []uint32{uint32(len(points)), 3}, e.ByteOrder == payload.LittleEndian)
	if err != nil {
		return nil, err
	}
	return tensor.InlineGeometry()
}

func voxelItem(id string) (*pb.OverlayItem, error) {
	if id == "" {
		return nil, errors.New("overlay item ID is empty")
	}
	return &pb.OverlayItem{ItemId: id, Transform: &pb.OverlayTransform{Frame: "voxel"}}, nil
}

func (e GeometryEncoder) Markers(id string, points []geometry.ZYX) (*pb.OverlayItem, error) {
	item, err := voxelItem(id)
	if err != nil {
		return nil, err
	}
	p, err := e.points(points)
	if err != nil {
		return nil, err
	}
	item.Geometry = &pb.OverlayItem_Marker{Marker: &pb.OverlayMarker{PositionsZyx: p}}
	return item, nil
}

type LineTopology string

const (
	Polyline LineTopology = "polyline"
	Segments LineTopology = "segments"
	Loop     LineTopology = "loop"
)

// Lines builds a line set. The host draws it in 3D with screen-pixel thickness,
// and projects it into every 2D slice, dashed outside its own slice, never
// sectioned. In 2D thickness and marker size_px are measured in voxels.
func (e GeometryEncoder) Lines(id string, points []geometry.ZYX, topology LineTopology) (*pb.OverlayItem, error) {
	if topology != "" && topology != Polyline && topology != Segments && topology != Loop {
		return nil, errors.New("unknown line topology")
	}
	if len(points) < 2 || (topology == Segments && len(points)%2 != 0) {
		return nil, errors.New("lines need at least two points and segments need pairs")
	}
	item, err := voxelItem(id)
	if err != nil {
		return nil, err
	}
	p, err := e.points(points)
	if err != nil {
		return nil, err
	}
	item.Geometry = &pb.OverlayItem_LineSet{LineSet: &pb.OverlayLineSet{PositionsZyx: p, Topology: string(topology)}}
	return item, nil
}

// Labels builds a categorical inline volume with the exact 256x4 RGBA table.
// Set GetLabelVolume().T for a time-scoped item; nil retains all-time display.
// Inline labels cannot be passed to CompileLabelVolume, which needs a segment.
// A label volume is a level-0 voxel selection: voxel frame, world scale mode
// with unit scale, identity rotation, integer position, and its entire box
// inside the dataset. With a dataset open the host checks this at the push;
// any violation refuses the whole push and reports overlay_item_refused (28).
// Without a dataset the push is unchecked; compilation returns NOT_FOUND.
func (e GeometryEncoder) Labels(id string, labels *payload.Tensor, lut [256][4]uint8) (*pb.OverlayItem, error) {
	if err := e.ByteOrder.Validate(); err != nil {
		return nil, err
	}
	if labels == nil || len(labels.Shape()) != 3 || (labels.DType() != pb.DType_DTYPE_U8 && labels.DType() != pb.DType_DTYPE_U16) {
		return nil, errors.New("labels require a ZYX u8 or u16 tensor")
	}
	if labels.DType() != pb.DType_DTYPE_U8 && labels.LittleEndian() != (e.ByteOrder == payload.LittleEndian) {
		return nil, errors.New("label byte order disagrees with geometry encoder")
	}
	item, err := voxelItem(id)
	if err != nil {
		return nil, err
	}
	volume, err := labels.InlineGeometry()
	if err != nil {
		return nil, err
	}
	colors := make([]uint8, 0, 1024)
	for _, rgba := range lut {
		colors = append(colors, rgba[:]...)
	}
	table, err := payload.Encode(colors, []uint32{256, 4}, true)
	if err != nil {
		return nil, err
	}
	colormap, err := table.InlineGeometry()
	if err != nil {
		return nil, err
	}
	item.Geometry = &pb.OverlayItem_LabelVolume{LabelVolume: &pb.OverlayLabelVolume{Labels: volume, Colormap: colormap}}
	return item, nil
}

// PushSender is implemented by control.Client. Send reports admission, not host
// application; on error the message was not accepted and remains caller-owned.
type PushSender interface{ Send(*pb.PluginMsg) error }

// Overlay owns one revision sequence across reliable, live and remove updates.
// Use exactly one handle per overlay ID; do not mix it with raw updates.
// Replace/ReplaceLive accept inline items only. ReplaceLabelVolume and
// CompileLabelVolume manage fresh segment-backed loans for a typed label item.
type Overlay struct {
	mu       sync.Mutex
	sender   PushSender
	id       string
	revision uint64
}

// NewOverlay creates a handle for one overlay ID. Keep exactly one handle per
// overlay ID per session: separate handles have independent revision sequences,
// and the host discards updates that do not advance its accepted revision.
func NewOverlay(sender PushSender, id string) (*Overlay, error) {
	if sender == nil || id == "" {
		return nil, errors.New("overlay sender and ID are required")
	}
	return &Overlay{sender: sender, id: id}, nil
}

func (o *Overlay) next() (uint64, error) {
	if o.revision == math.MaxUint64 {
		return 0, errors.New("overlay revision space exhausted")
	}
	o.revision++ // Admission refusal burns a revision; an old one is never reused.
	return o.revision, nil
}

func checkBuffer(b *pb.GeomBuffer) error {
	if b == nil {
		return errors.New("geometry buffer is absent")
	}
	source, ok := b.Source.(*pb.GeomBuffer_Inline)
	if !ok || source == nil {
		return errors.New("overlay helper accepts inline geometry only")
	}
	n, err := payload.ByteLen(b.Shape, b.Dtype)
	if err != nil {
		return err
	}
	if n != uint64(len(source.Inline)) {
		return errors.New("geometry buffer length disagrees with shape and dtype")
	}
	return nil
}

// Replace sends a whole draw list. An empty list is a valid empty replacement;
// Clear sends the distinct one-shot OverlayRemove operation.
func (o *Overlay) Replace(items ...*pb.OverlayItem) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	update, err := o.update(items)
	if err != nil {
		return err
	}
	return o.sender.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: update}})
}

func (o *Overlay) update(items []*pb.OverlayItem) (*pb.OverlayUpdate, error) {
	snapshot := make([]*pb.OverlayItem, len(items))
	seen := make(map[string]bool, len(items))
	for i, item := range items {
		if item == nil || item.ItemId == "" || item.Geometry == nil || seen[item.ItemId] {
			return nil, errors.New("overlay requires distinct nonempty item IDs and geometry")
		}
		seen[item.ItemId] = true
		// Reflect over the item so an overlooked geometry arm cannot carry a loan
		// through a helper that promises no ownership transfer.
		if err := inlineItem(item); err != nil {
			return nil, err
		}
		snapshot[i] = proto.Clone(item).(*pb.OverlayItem)
	}
	revision, err := o.next()
	if err != nil {
		return nil, err
	}
	msg := &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: o.id, Revision: revision, Items: snapshot}}}
	if err := control.ValidateSize(msg); err != nil {
		return nil, err
	}
	return msg.GetOverlayUpdate(), nil
}

func (o *Overlay) Clear() error {
	o.mu.Lock()
	defer o.mu.Unlock()
	revision, err := o.next()
	if err != nil {
		return err
	}
	return o.sender.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: o.id, Revision: revision}}})
}

// ReplaceLive publishes a whole inline draw list, sharing Replace/Clear's revision
// sequence. Segment zero selects the installed O segment for this overlay ID.
// A nonzero sample means committed; a notification error does not undo it.
func (o *Overlay) ReplaceLive(session *control.Session, segment uint64, pair *memory.PaneEpoch, items ...*pb.OverlayItem) (memory.Sample, error) {
	o.mu.Lock()
	defer o.mu.Unlock()
	if session == nil || session.Memory == nil || session.Memory.Registry == nil {
		return memory.Sample{}, errors.New("live overlay requires a session")
	}
	if o.sender != session.Client {
		return memory.Sample{}, errors.New("live and reliable overlay must use the same session client")
	}
	if segment == 0 {
		seg, err := session.Memory.Registry.FindContribution(memory.PurposeOverlay, o.id)
		if err != nil {
			if session.Memory.DropRetiredPublication(err) {
				return memory.Sample{}, nil
			}
			return memory.Sample{}, err
		}
		segment = seg.ID()
	}
	update, err := o.update(items)
	if err != nil {
		return memory.Sample{}, err
	}
	return session.PublishLiveOverlay(segment, update, pair)
}
