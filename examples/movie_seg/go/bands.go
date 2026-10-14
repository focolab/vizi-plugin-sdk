package main

import (
	"errors"
	"math"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func band(v, threshold float64) byte {
	if math.IsNaN(v) || v < threshold {
		return 0
	}
	d := (v - threshold) / 200
	if d >= 2 {
		return 3
	}
	return byte(1 + math.Floor(d))
}

// labelTensor handles every published numeric dtype without converting the
// input to a second float64 volume. The comparison itself is always floating.
func labelTensor(v *payload.Tensor, threshold float64) ([]byte, error) {
	switch v.DType() {
	case pb.DType_DTYPE_U8:
		return labelValues[uint8](v, threshold)
	case pb.DType_DTYPE_U16:
		return labelValues[uint16](v, threshold)
	case pb.DType_DTYPE_F32:
		return labelValues[float32](v, threshold)
	case pb.DType_DTYPE_F64:
		return labelValues[float64](v, threshold)
	default:
		return nil, errors.New("unsupported input dtype")
	}
}
func labelValues[T payload.Scalar](v *payload.Tensor, threshold float64) ([]byte, error) {
	values, err := payload.Decode[T](v)
	if err != nil {
		return nil, err
	}
	out := make([]byte, len(values))
	for i, v := range values {
		out[i] = band(float64(v), threshold)
	}
	return out, nil
}

// tight retains zero voxels inside the smallest foreground box. Empty means
// no labels on the wire, not a zero-sized tensor.
func tight(labels []byte, shape []uint32) (*plugin.Chunk, error) {
	if len(shape) != 3 {
		return nil, errors.New("volume shape must be ZYX")
	}
	n, err := payload.ByteLen(shape, pb.DType_DTYPE_U8)
	if err != nil {
		return nil, err
	}
	if n != uint64(len(labels)) {
		return nil, errors.New("label length disagrees with shape")
	}
	lo := [3]uint32{shape[0], shape[1], shape[2]}
	hi := [3]uint32{}
	found := false
	for i, v := range labels {
		if v == 0 {
			continue
		}
		found = true
		pos := [3]uint32{uint32(uint64(i) / (uint64(shape[1]) * uint64(shape[2]))), uint32(uint64(i) / uint64(shape[2]) % uint64(shape[1])), uint32(uint64(i) % uint64(shape[2]))}
		for a := 0; a < 3; a++ {
			lo[a] = min(lo[a], pos[a])
			hi[a] = max(hi[a], pos[a]+1)
		}
	}
	if !found {
		return nil, nil
	}
	box := []uint32{hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]}
	data := make([]byte, 0, int(uint64(box[0])*uint64(box[1])*uint64(box[2])))
	for z := lo[0]; z < hi[0]; z++ {
		for y := lo[1]; y < hi[1]; y++ {
			start := (uint64(z)*uint64(shape[1])+uint64(y))*uint64(shape[2]) + uint64(lo[2])
			data = append(data, labels[start:start+uint64(box[2])]...)
		}
	}
	tensor, err := payload.Encode(data, box, true)
	if err != nil {
		return nil, err
	}
	return &plugin.Chunk{Labels: tensor, PositionZYX: geometry.ZYX{float64(lo[0]), float64(lo[1]), float64(lo[2])}}, nil
}

func unionInto(dst, labels []byte) error {
	if len(dst) != len(labels) {
		return errors.New("footprint frame shape changed")
	}
	for i, v := range labels {
		if v != 0 {
			dst[i] = 1
		}
	}
	return nil
}

var bandNames = map[uint32]string{1: "dim", 2: "mid", 3: "bright"}

func bandTable(hidden [4]bool) ([256][4]uint8, map[uint32]bool) {
	lut := [256][4]uint8{}
	lut[1] = [4]uint8{60, 120, 255, 255}
	lut[2] = [4]uint8{255, 200, 0, 255}
	lut[3] = [4]uint8{255, 60, 60, 255}
	for band := 1; band <= 3; band++ {
		if hidden[band] {
			lut[band][3] = 0
		}
	}
	return lut, map[uint32]bool{1: !hidden[1], 2: !hidden[2], 3: !hidden[3]}
}
func footprintTable() [256][4]uint8 {
	lut := [256][4]uint8{}
	lut[1] = [4]uint8{200, 200, 200, 255}
	return lut
}
