package plugin

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// LabelVolume is a reusable, CPU-owned voxel-frame item. Labels must be a ZYX
// u8/u16 tensor in the deployment's byte order. Colormap is the complete RGBA
// table (row zero is background). T nil displays at all times. Names are keyed
// by label ID, not LUT row. Do not mutate fields during a method call.
//
// A label volume is a level-0 voxel selection: frame voxel, unit scale in world
// scale mode, identity rotation, integer position, and the whole box inside the
// dataset. With a dataset open the host checks these rules at the push, refusing
// the whole push through host-only overlay_item_refused (28) if any item fails.
// Without a dataset the push is unchecked; compilation returns NOT_FOUND.
// Inline labels can display but never compile; this type uses segment loans.
//
// Each ReplaceLabelVolume/CompileLabelVolume call reserves fresh one-use labels
// and LUT loans in this overlay's arena. Keeping this value holds no arena space;
// replacing or clearing an uncommitted preview strands no future compile use.
// It deliberately exposes no borrowed descriptor to retain or accidentally reuse.
type LabelVolume struct {
	ID          string
	Labels      *payload.Tensor
	Colormap    [256][4]uint8
	PositionZYX geometry.ZYX
	T           *uint32
	Names       map[uint32]string
	// Visible overrides compiled region visibility by label ID. Absent IDs use
	// their LUT row's alpha. Voxel rendering always uses the LUT, not this map.
	Visible map[uint32]bool
}

func (v LabelVolume) item(compiling bool) (*pb.OverlayItem, error) {
	if v.Labels == nil || len(v.Labels.Shape()) != 3 || (v.Labels.DType() != pb.DType_DTYPE_U8 && v.Labels.DType() != pb.DType_DTYPE_U16) {
		return nil, errors.New("labels require a ZYX u8 or u16 tensor")
	}
	for _, p := range v.PositionZYX {
		if math.IsNaN(p) || math.IsInf(p, 0) {
			return nil, errors.New("label position must be finite")
		}
		if compiling && (p < 0 || p != math.Trunc(p)) {
			return nil, errors.New("compiled labels require nonnegative integer voxel positions")
		}
	}
	item, err := voxelItem(v.ID)
	if err != nil {
		return nil, err
	}
	item.Transform.PositionZyx = append([]float64(nil), v.PositionZYX[:]...)
	volume := &pb.OverlayLabelVolume{LabelNames: maps.Clone(v.Names), LabelVisible: maps.Clone(v.Visible)}
	if v.T != nil {
		volume.T = proto.Uint32(*v.T)
	}
	item.Geometry = &pb.OverlayItem_LabelVolume{LabelVolume: volume}
	return item, nil
}

func (o *Overlay) labelSession(s *control.Session) error {
	if s == nil || s.Client == nil || s.Memory == nil || s.Memory.Registry == nil || s.Shortfalls == nil || o.sender != s.Client {
		return errors.New("label publication requires this overlay's session")
	}
	return nil
}

// labelLoans is a transaction over two fresh reservations, with one outbound
// message. The internal send function reports historical admission, not success
// of a later request: trying ReleaseLoan after a host release could quarantine
// the arena or alias a reused offset. Never use ledger lookup as that history.
func (o *Overlay) labelLoans(s *control.Session, v LabelVolume, item *pb.OverlayItem, send func(*pb.OverlayItem) (bool, error)) (err error) {
	seg, err := s.Memory.Registry.FindContribution(memory.PurposeOverlay, o.id)
	if err != nil {
		return err
	}
	var deposits []memory.Deposit
	admitted := false
	defer func() {
		if !admitted {
			for _, d := range deposits {
				if releaseErr := seg.ReleaseLoan(d); releaseErr != nil && !errors.Is(releaseErr, memory.ErrRetired) {
					err = errors.Join(err, releaseErr)
				}
			}
		}
		if errors.Is(err, memory.ErrRetired) {
			next, lookupErr := s.Memory.Registry.FindContribution(memory.PurposeOverlay, o.id)
			if lookupErr == nil && next.ID() != seg.ID() {
				err = o.labelLoans(s, v, item, send)
			}
		}
	}()
	arena, err := seg.Arena()
	if err != nil {
		return err
	}
	if arena.EpochOffset != nil {
		return errors.New("label encoding of pane-associated arena requires an explicit binding")
	}
	buffer := func(data []byte, shape []uint32, dtype pb.DType) (*pb.GeomBuffer, error) {
		d, err := s.WriteLoan(seg.ID(), arena.Index, data)
		if err != nil {
			return nil, fmt.Errorf("label arena needs %d bytes: %w", len(data), err)
		}
		deposits = append(deposits, d)
		return &pb.GeomBuffer{Shape: shape, Dtype: dtype, Source: &pb.GeomBuffer_Segment{Segment: &pb.SegmentRef{SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length, Uses: 1, ShapeZyx: shape, Dtype: dtype}}}, nil
	}
	volume := item.GetLabelVolume()
	volume.Labels, err = buffer(v.Labels.Bytes(), v.Labels.Shape(), v.Labels.DType())
	if err != nil {
		return err
	}
	colors := make([]byte, 0, 1024)
	for _, rgba := range v.Colormap {
		colors = append(colors, rgba[:]...)
	}
	volume.Colormap, err = buffer(colors, []uint32{256, 4}, pb.DType_DTYPE_U8)
	if err != nil {
		return err
	}
	admitted, err = send(item)
	return err
}

// ReplaceLabelVolume replaces the entire reliable draw list with one
// segment-backed label item. It shares this handle's revision sequence with
// Replace, ReplaceLive and Clear. Allocation/build/admission failure rolls back
// both fresh loans; successful admission retains them until host release.
func (o *Overlay) ReplaceLabelVolume(s *control.Session, v LabelVolume) error {
	o.mu.Lock()
	defer o.mu.Unlock()
	if err := o.labelSession(s); err != nil {
		return err
	}
	item, err := v.item(false)
	if err != nil {
		return err
	}
	revision, err := o.next()
	if err != nil {
		return err
	}
	err = o.labelLoans(s, v, item, func(item *pb.OverlayItem) (bool, error) {
		err := s.Client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: o.id, Revision: revision, Items: []*pb.OverlayItem{item}}}})
		return err == nil, err
	})
	if s.Memory.DropRetiredPublication(err) {
		return nil
	}
	return err
}

// CompileLabelVolume creates native masks from a fresh copy of the typed item.
// It can follow a push, a Clear, or a previous compile; it never reuses a push's
// released descriptor. Host validation still owns dataset bounds, permissions
// and the 500-label limit. This blocking method does not hold the overlay lock
// while waiting, and does not clear the overlay or change application state.
// A timeout/host refusal after admission leaves disposal to the host.
func (o *Overlay) CompileLabelVolume(ctx context.Context, s *control.Session, v LabelVolume, replaceAll bool) (result *pb.LabelVolumeCompiled, err error) {
	if err = o.labelSession(s); err != nil {
		return nil, err
	}
	item, err := v.item(true)
	if err != nil {
		return nil, err
	}
	err = o.labelLoans(s, v, item, func(item *pb.OverlayItem) (bool, error) {
		r, admitted, err := s.Client.CallWithAdmission(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_CompileLabelVolume{CompileLabelVolume: &pb.CompileLabelVolume{OverlayId: o.id, Item: item, ReplaceAll: replaceAll}}})
		if err != nil {
			return admitted, err
		}
		result = r.Message.GetLabelVolumeCompiled()
		return admitted, r.Close()
	})
	return result, err
}
