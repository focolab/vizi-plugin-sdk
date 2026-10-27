package main

import (
	"context"
	"encoding/binary"
	"errors"
	"math"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
)

// Pixel arithmetic uses explicit little-endian loads, independent of alignment.
func value(b []byte, dtype pb.DType, index int) float64 {
	b = b[index*int(stage.ItemSize(dtype)):]
	switch dtype {
	case pb.DType_DTYPE_U8:
		return float64(b[0])
	case pb.DType_DTYPE_U16:
		return float64(binary.LittleEndian.Uint16(b))
	case pb.DType_DTYPE_F32:
		return float64(math.Float32frombits(binary.LittleEndian.Uint32(b)))
	default:
		return math.Float64frombits(binary.LittleEndian.Uint64(b))
	}
}

func put(b []byte, dtype pb.DType, index int, v float64) {
	b = b[index*int(stage.ItemSize(dtype)):]
	switch dtype {
	case pb.DType_DTYPE_U8:
		b[0] = byte(max(0, min(255, math.RoundToEven(v))))
	case pb.DType_DTYPE_U16:
		binary.LittleEndian.PutUint16(b, uint16(max(0, min(65535, math.RoundToEven(v)))))
	case pb.DType_DTYPE_F32:
		binary.LittleEndian.PutUint32(b, math.Float32bits(float32(v)))
	case pb.DType_DTYPE_F64:
		binary.LittleEndian.PutUint64(b, math.Float64bits(v))
	}
}

func voxels(ctx context.Context, l stage.Layout, visit func(int, [3]uint32)) error {
	for z := l.Extent[0][0]; z < l.Extent[0][1]; z++ {
		for y := l.Extent[1][0]; y < l.Extent[1][1]; y++ {
			if err := ctx.Err(); err != nil {
				return err
			}
			for x := l.Extent[2][0]; x < l.Extent[2][1]; x++ {
				i := (uint64(z)*uint64(l.Allocation[1])+uint64(y))*uint64(l.Allocation[2]) + uint64(x)
				visit(int(i), [3]uint32{z, y, x})
			}
		}
	}
	return nil
}

func gain(ctx context.Context, _ *control.Client, _ *pb.StageArm, s *stage.Slot) error {
	if delay := s.Params["stall_ms"].(int64); delay > 0 {
		timer := time.NewTimer(time.Duration(delay) * time.Millisecond)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-timer.C:
		}
	}
	pixels, err := s.Plane(uint16(s.Params["channel"].(int64)))
	if err != nil {
		return err
	}
	factor := s.Params["factor"].(float64)
	return voxels(ctx, s.Layout, func(i int, _ [3]uint32) {
		p := float64(value(pixels, s.Layout.DType, i) * factor)
		put(pixels, s.Layout.DType, i, p)
	})
}

func diff(ctx context.Context, _ *control.Client, arm *pb.StageArm, s *stage.Slot) error {
	a, err := s.Plane(uint16(s.Params["a"].(int64)))
	if err != nil {
		return err
	}
	b, err := s.Plane(uint16(s.Params["b"].(int64)))
	if err != nil {
		return err
	}
	channel, ok := arm.NewChannels["absdiff"]
	if !ok {
		return errors.New("arm did not assign absdiff")
	}
	out, err := s.AddChannel(uint16(channel))
	if err != nil {
		return err
	}
	return voxels(ctx, s.Layout, func(i int, _ [3]uint32) {
		av, bv := value(a, s.Layout.DType, i), value(b, s.Layout.DType, i)
		v := av - bv
		if s.Layout.DType == pb.DType_DTYPE_F32 {
			v = float64(float32(av) - float32(bv)) // subtraction must round in f32
		}
		put(out, s.Layout.DType, i, math.Abs(v))
	})
}

func widen(ctx context.Context, _ *control.Client, _ *pb.StageArm, s *stage.Slot) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	crop := uint32(s.Params["crop"].(int64))
	a := s.Layout.Allocation
	if 2*crop >= a[1] || 2*crop >= a[2] {
		return errors.New("crop leaves no y or x voxels")
	}
	return s.RelayoutFloat32([3]uint32{a[0], a[1] - 2*crop, a[2] - 2*crop}, [3]uint32{0, crop, crop})
}

type armKey struct{ pipeline, position uint32 }
type history struct {
	t            uint32
	reference    [3]float64
	hasReference bool
}

// Check/Process are serialized on the SDK's stage worker. Plans never touch
// this map. Identity deliberately survives a revision change or ring replacement.
type driftState struct{ arms map[armKey]history }

func key(a *pb.StageArm) armKey { return armKey{a.Pipeline, a.Position} }

func (d *driftState) check(_ context.Context, _ *control.Client, a *pb.StageArm, info stage.Info) error {
	if old, ok := d.arms[key(a)]; ok && uint64(info.Timepoint) != uint64(old.t)+1 {
		delete(d.arms, key(a))
		return &stage.Refusal{Reason: 1, Detail: "nonconsecutive timepoint; forgetting reference"}
	}
	return nil
}

func (d *driftState) process(ctx context.Context, _ *control.Client, a *pb.StageArm, s *stage.Slot) error {
	pixels, err := s.Plane(uint16(s.Params["channel"].(int64)))
	if err != nil {
		return err
	}
	var sum uint64
	var weighted [3]uint64
	if err := voxels(ctx, s.Layout, func(i int, xyz [3]uint32) {
		v := uint64(value(pixels, s.Layout.DType, i))
		sum += v
		for axis := range weighted {
			weighted[axis] += v * uint64(xyz[axis])
		}
	}); err != nil {
		return err
	}
	var centroid [3]float64
	if sum != 0 {
		for axis := range centroid {
			quotient := float64(weighted[axis]) / float64(sum)
			delta := float64(quotient - float64(s.Layout.Extent[axis][0]))
			centroid[axis] = s.Layout.Origin[axis] + delta
		}
	}
	h, exists := d.arms[key(a)]
	if !exists {
		h.reference, h.hasReference = centroid, sum != 0
	}
	if sum != 0 && h.hasReference {
		var shift [3]float64
		for axis := range shift {
			shift[axis] = h.reference[axis] - centroid[axis]
		}
		s.Layout.ComposePose([9]float64{1, 0, 0, 0, 1, 0, 0, 0, 1}, shift)
	}
	h.t = s.Timepoint
	d.arms[key(a)] = h
	return nil
}
