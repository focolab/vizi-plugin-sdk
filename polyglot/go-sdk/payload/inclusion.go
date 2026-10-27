package payload

import (
	"context"
	"errors"
	"fmt"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// InclusionMask owns the host's bbox-local U8 membership mask. Tensor is nil
// for hidden or empty regions; Present distinguishes those outcomes.
type InclusionMask struct {
	Tensor         *Tensor
	OriginZYX      [3]uint32
	RegionID       string
	T              int32
	OpenGeneration uint64
	Present        bool
	VoxelCount     uint64
}

// ReadInclusion copies ROI membership, checks VoxelCount against the actual
// 0/1 mask, and closes the reply on every path, including count mismatches.
// Like ReadVolume, a cleanup-only failure returns the owned result with error.
func ReadInclusion(ctx context.Context, c *control.Client, memory Copier, request *pb.ReadRoiInclusion, options CopyOptions) (result *InclusionMask, err error) {
	if c == nil || request == nil {
		return nil, errors.New("client and inclusion request are required")
	}
	reply, err := c.ReadRoiInclusion(ctx, request)
	if err != nil {
		return nil, err
	}
	defer func() { err = errors.Join(err, reply.Close()) }()
	v := reply.Inclusion
	if v == nil {
		return nil, inclusionError("empty inclusion reply")
	}
	mask := &InclusionMask{RegionID: v.RegionId, T: v.T, OpenGeneration: v.OpenGeneration, Present: v.Present, VoxelCount: v.VoxelCount}
	if len(v.OriginZyx) == 3 {
		mask.OriginZYX = [3]uint32(v.OriginZyx)
	} else if v.Segment != nil || len(v.OriginZyx) != 0 {
		return nil, inclusionError("inclusion origin must have three ZYX coordinates")
	}
	if v.Segment == nil {
		if v.VoxelCount != 0 {
			return nil, inclusionError("inclusion voxel_count is nonzero without a mask")
		}
		return mask, nil
	}
	if !v.Present || v.VoxelCount == 0 {
		return nil, inclusionError("hidden or empty inclusion must not carry a mask")
	}
	if v.Segment.Dtype != pb.DType_DTYPE_U8 || len(v.Segment.ShapeZyx) != 3 {
		return nil, inclusionError("inclusion mask must be a three-dimensional U8 tensor")
	}
	tensor, err := CopySegment(memory, v.Segment, options)
	if err != nil {
		return nil, err
	}
	var count uint64
	for _, value := range tensor.data {
		if value > 1 {
			return nil, inclusionError("inclusion mask values must be 0 or 1")
		}
		count += uint64(value)
	}
	if count != v.VoxelCount {
		return nil, inclusionError(fmt.Sprintf("inclusion voxel_count %d differs from mask count %d", v.VoxelCount, count))
	}
	mask.Tensor = tensor
	return mask, nil
}

func inclusionError(message string) error {
	return &control.HostError{Kind: pb.Error_PROTOCOL, Message: message}
}
