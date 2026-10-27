package control

import (
	"context"
	"errors"
	"fmt"
	"math"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// DeriveReaderPlane implements the reader contract's volume-only fallback.
// ServeEnv supplies ReaderMemory; custom servers must supply it explicitly.
// readVolume transfers an exclusive, fresh, single-use G loan to this method.
// Success replaces it with the requested plane at the same aligned offset.
// Failure reclaims an unpublished volume; already-admitted loans are untouched.
func (c *Client) DeriveReaderPlane(ctx context.Context, q *pb.ReadPlane, readVolume func(context.Context, *Client, *pb.ReadVolume) (*pb.SegmentRef, error)) (_ *pb.SegmentRef, err error) {
	if q == nil || readVolume == nil {
		return nil, errors.New("plane request and volume handler are required")
	}
	if q.Level != 0 {
		return nil, &HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "reader supports only level 0"}
	}
	if c == nil || c.cfg.ReaderMemory == nil || c.cfg.ReaderMemory.Registry == nil {
		return nil, errors.New("volume-derived planes require Config.ReaderMemory (set automatically by ServeEnv)")
	}
	d, err := readVolume(ctx, c, &pb.ReadVolume{Handle: q.Handle, T: q.T, C: q.C, Level: q.Level})
	if err != nil {
		return nil, err
	}
	if d == nil {
		return nil, errors.New("read_volume handler returned no response")
	}
	seg, err := c.cfg.ReaderMemory.Registry.Segment(d.SegmentId)
	if err != nil {
		return nil, err
	}
	if seg.Purpose() != memory.PurposeReader {
		return nil, errors.New("reader volume must name a G segment")
	}
	arena, err := seg.Arena()
	if err != nil {
		return nil, err
	}
	key := memory.Deposit{SegmentID: d.SegmentId, Region: arena.Index, Offset: d.Offset, Length: d.Len}
	defer func() {
		if err != nil && !errors.Is(err, memory.ErrLoanAdmitted) {
			reclaim := seg.ReleaseLoan(key)
			if !errors.Is(reclaim, memory.ErrRetired) {
				err = errors.Join(err, reclaim)
			}
		}
	}()
	if d.Uses > 1 {
		return nil, errors.New("reader volume must be an exclusive single-use loan")
	}
	if len(d.ShapeZyx) != 3 {
		return nil, errors.New("reader volume must have three ZYX dimensions")
	}
	var bytes uint64
	switch d.Dtype {
	case pb.DType_DTYPE_U8:
		bytes = 1
	case pb.DType_DTYPE_U16:
		bytes = 2
	case pb.DType_DTYPE_F32:
		bytes = 4
	case pb.DType_DTYPE_F64:
		bytes = 8
	default:
		return nil, errors.New("reader volume has unsupported dtype")
	}
	for _, n := range d.ShapeZyx {
		if n == 0 || bytes > math.MaxUint64/uint64(n) {
			return nil, errors.New("reader volume shape is empty or overflows")
		}
		bytes *= uint64(n)
	}
	if bytes != d.Len {
		return nil, errors.New("reader volume length disagrees with shape and dtype")
	}
	if q.Z >= d.ShapeZyx[0] {
		return nil, &HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "plane z outside volume"}
	}
	planeBytes := bytes / uint64(d.ShapeZyx[0])
	// Clone before mutation so allocation failure cannot strand a rekeyed loan.
	out := proto.Clone(d).(*pb.SegmentRef)
	sliced, err := seg.SliceLoan(key, uint64(q.Z)*planeBytes, planeBytes)
	if err != nil {
		return nil, err
	}
	out.Len = sliced.Length
	out.ShapeZyx[0] = 1
	out.Uses = 0
	return out, nil
}

// ValidateReaderReply checks a freshly produced reader loan against the open
// handle's geometry before publication. A rejected, never-admitted G loan is
// reclaimed when ReaderMemory is configured (automatically by ServeEnv).
// Already-admitted loans remain owned by the host, even on validation failure.
func (c *Client) ValidateReaderReply(d *pb.SegmentRef, opened *pb.ReaderOpened, plane bool) error {
	want := slices.Clone(opened.GetShapeTczyx())
	if len(want) == 5 {
		want = want[2:]
		if plane {
			want[0] = 1
		}
	} else {
		want = nil
	}
	if d != nil && len(want) == 3 && slices.Equal(d.ShapeZyx, want) && d.Dtype == opened.GetDtype() {
		return nil
	}
	kind := "read_volume"
	if plane {
		kind = "read_plane"
	}
	err := fmt.Errorf("%s reply geometry for reader %q: got shape_zyx=%v dtype=%s; expected shape_zyx=%v dtype=%s from ReaderOpened shape_tczyx=%v", kind, opened.GetHandle(), d.GetShapeZyx(), d.GetDtype(), want, opened.GetDtype(), opened.GetShapeTczyx())
	if c == nil || d == nil || c.cfg.ReaderMemory == nil || c.cfg.ReaderMemory.Registry == nil {
		return err
	}
	seg, lookupErr := c.cfg.ReaderMemory.Registry.Segment(d.SegmentId)
	if lookupErr != nil || seg.Purpose() != memory.PurposeReader {
		return err
	}
	arena, arenaErr := seg.Arena()
	if arenaErr != nil {
		return errors.Join(err, arenaErr)
	}
	reclaim := seg.ReleaseLoan(memory.Deposit{SegmentID: d.SegmentId, Region: arena.Index, Offset: d.Offset, Length: d.Len})
	if errors.Is(reclaim, memory.ErrRetired) {
		reclaim = nil
	}
	return errors.Join(err, reclaim)
}
