package stage

import (
	"encoding/binary"
	"fmt"
	"math"
	"slices"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// AddChannel appends a zeroed plane at the index assigned by arm.NewChannels.
// The worker checks that the addition was declared. All checks precede writes;
// an error leaves pixels and Layout unchanged. The returned bytes are borrowed
// until the callback returns or a relayout invalidates their interpretation.
func (s *Slot) AddChannel(channel uint16) ([]byte, error) {
	if _, err := s.validateLayout(); err != nil {
		return nil, err
	}
	candidate := *s
	candidate.Layout.Channels = append(slices.Clone(s.Layout.Channels), channel)
	n, err := candidate.validateLayout()
	if err != nil {
		return nil, err
	}
	start := uint64(len(s.Layout.Channels)) * n
	pixels := s.Data()[start : start+n : start+n]
	clear(pixels)
	s.Layout = candidate.Layout
	return pixels, nil
}

// RelayoutFloat32 repacks every plane as float32, taking a window at offset
// in the old allocation. Conversion is unscaled, round-to-nearest-even. The
// extent is clipped to the window and origin is shifted to preserve placement.
// It uses scratch storage equal to the output's packed bytes, bounded by the
// slot's capacity. Validation errors leave pixels and metadata unchanged.
// Earlier Plane/Data slices must no longer be used with their old layout.
// This helper handles U8/U16/F32/F64 inputs; integer output conversion/scaling
// remains an explicit author decision, available through the raw Layout API.
func (s *Slot) RelayoutFloat32(allocation, offset [3]uint32) error {
	oldBytes, err := s.validateLayout()
	if err != nil {
		return err
	}
	candidate := *s
	candidate.Layout.DType = pb.DType_DTYPE_F32
	candidate.Layout.Allocation = allocation
	for axis, n := range allocation {
		old := s.Layout.Allocation[axis]
		if n == 0 || offset[axis] > old || n > old-offset[axis] {
			return fmt.Errorf("%w: relayout window outside input allocation", ErrSlotInvalid)
		}
		e := s.Layout.Extent[axis]
		for edge, v := range e {
			candidate.Layout.Extent[axis][edge] = uint32(max(0, min(int64(n), int64(v)-int64(offset[axis]))))
		}
		shift := int64(candidate.Layout.Extent[axis][0]) + int64(offset[axis]) - int64(e[0])
		candidate.Layout.Origin[axis] += float64(shift)
	}
	n, err := candidate.validateLayout()
	if err != nil {
		return err
	}
	output := make([]byte, n*uint64(len(s.Layout.Channels)))
	width := int(ItemSize(s.Layout.DType))
	oldY, oldX := uint64(s.Layout.Allocation[1]), uint64(s.Layout.Allocation[2])
	data := s.Data()
	next := 0
	for plane := range s.Layout.Channels {
		base := uint64(plane) * oldBytes
		for z := uint32(0); z < allocation[0]; z++ {
			for y := uint32(0); y < allocation[1]; y++ {
				for x := uint32(0); x < allocation[2]; x++ {
					i := ((uint64(z+offset[0])*oldY+uint64(y+offset[1]))*oldX + uint64(x+offset[2])) * uint64(width)
					b := data[base+i:]
					var v float32
					switch s.Layout.DType {
					case pb.DType_DTYPE_U8:
						v = float32(b[0])
					case pb.DType_DTYPE_U16:
						v = float32(binary.LittleEndian.Uint16(b))
					case pb.DType_DTYPE_F32:
						v = math.Float32frombits(binary.LittleEndian.Uint32(b))
					case pb.DType_DTYPE_F64:
						v = float32(math.Float64frombits(binary.LittleEndian.Uint64(b)))
					}
					binary.LittleEndian.PutUint32(output[next:], math.Float32bits(v))
					next += 4
				}
			}
		}
	}
	copy(data, output)
	s.Layout = candidate.Layout
	return nil
}
