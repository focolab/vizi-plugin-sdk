package payload

import (
	"context"
	"errors"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// Copier is implemented by control.Memory. CopyDescriptor must copy exactly the
// descriptor's bytes while its reply still owns the use; it does not release it.
type Copier interface {
	CopyDescriptor(*pb.SegmentRef, []byte) error
}

type CopyOptions struct {
	MaxBytes  uint64
	ByteOrder ByteOrder
}

// CopySegment copies a currently owned descriptor. MaxBytes is an explicit
// application allocation budget, not a wire limit. The caller still owns the
// reply and must close it. The deployment supplies its payload byte order;
// shared-memory §1 specifies metadata structures, not arbitrary pixel bytes.
func CopySegment(memory Copier, d *pb.SegmentRef, options CopyOptions) (*Tensor, error) {
	if err := ValidateSegment(d); err != nil {
		return nil, err
	}
	if err := options.ByteOrder.Validate(); err != nil {
		return nil, err
	}
	if memory == nil {
		return nil, errors.New("descriptor copier is nil")
	}
	if options.MaxBytes == 0 || d.Len > options.MaxBytes || d.Len > uint64(int(^uint(0)>>1)) {
		return nil, errors.New("segment exceeds application copy budget")
	}
	data := make([]byte, int(d.Len))
	if err := memory.CopyDescriptor(d, data); err != nil {
		return nil, err
	}
	return &Tensor{data, append([]uint32(nil), d.ShapeZyx...), d.Dtype, options.ByteOrder == LittleEndian}, nil
}

type Volume struct {
	Tensor         *Tensor
	T, C, Level    uint32
	OpenGeneration uint64
	// Filter is an owned snapshot of this volume's provenance. Nil means the
	// file's own voxels, even if ViewState shows a filter or an earlier read
	// was filtered. Params preserves every wire Value without normalization.
	Filter *pb.AppliedFilter
}

// ReadVolume returns an owned copy and closes the reply on success, validation
// failure and copy failure. Echoed indices/generation are preserved, not inferred
// from an earlier ViewState. The built-in copier supports host-produced Loaned.
// Set request.Raw to request the file's own voxels even with Plugins See
// Filtered Data enabled. Such a reply has no Filter; ViewState.Filter still
// describes the filter shown in the views, not the returned pixels.
// If copying succeeds but closing fails, the owned Volume is returned alongside
// the close error. A non-nil result is complete and remains usable for local work;
// it does not mean the host reclaimed the reservation. Validation/copy failures
// return nil, with any close error joined to the original error.
func ReadVolume(ctx context.Context, c *control.Client, memory Copier, request *pb.ReadDataVolume, options CopyOptions) (result *Volume, err error) {
	if c == nil || request == nil {
		return nil, errors.New("client and volume request are required")
	}
	reply, err := c.ReadDataVolume(ctx, request)
	if err != nil {
		return nil, err
	}
	defer func() {
		err = errors.Join(err, reply.Close())
	}()
	v := reply.Volume
	if v == nil {
		return nil, errors.New("volume reply is empty")
	}
	tensor, err := CopySegment(memory, v.Segment, options)
	if err != nil {
		return nil, err
	}
	return &Volume{Tensor: tensor, T: v.T, C: v.C, Level: v.Level, OpenGeneration: v.OpenGeneration, Filter: cloneFilter(v.Filter)}, nil
}

func cloneFilter(filter *pb.AppliedFilter) *pb.AppliedFilter {
	if filter == nil {
		return nil
	}
	return proto.Clone(filter).(*pb.AppliedFilter)
}

type Mask struct {
	Tensor         *Tensor
	OriginZYX      [3]uint32
	RegionID       string
	OpenGeneration uint64
}

// ReadMask copies mask bytes and metadata and closes the reply on every path.
// Like ReadVolume, it returns the complete owned Mask alongside a cleanup-only
// error. Validation/copy failures return nil, joining any additional close error.
func ReadMask(ctx context.Context, c *control.Client, memory Copier, request *pb.ReadMaskVoxels, options CopyOptions) (result *Mask, err error) {
	if c == nil || request == nil {
		return nil, errors.New("client and mask request are required")
	}
	reply, err := c.ReadMaskVoxels(ctx, request)
	if err != nil {
		return nil, err
	}
	defer func() {
		err = errors.Join(err, reply.Close())
	}()
	v := reply.Voxels
	if v == nil || len(v.OriginZyx) != 3 {
		return nil, errors.New("mask origin must have three ZYX coordinates")
	}
	tensor, err := CopySegment(memory, v.Segment, options)
	if err != nil {
		return nil, err
	}
	return &Mask{tensor, [3]uint32(v.OriginZyx), v.RegionId, v.OpenGeneration}, nil
}
