package stage

import (
	"bytes"
	"encoding/binary"
	"math"
	"reflect"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestAddChannelZeroesAndRejectsWithoutWrites(t *testing.T) {
	b := hostSlot()
	for i := 344; i < len(b); i++ {
		b[i] = 0xa5
	}
	s, err := DecodeSlot(b)
	if err != nil {
		t.Fatal(err)
	}
	pixels, err := s.AddChannel(9)
	if err != nil || len(pixels) != 12 || cap(pixels) != 12 || !bytes.Equal(pixels, make([]byte, 12)) {
		t.Fatal(pixels, err)
	}
	if b[343] != 0 || b[356] != 0xa5 {
		t.Fatal("changed adjacent bytes")
	}
	before := bytes.Clone(b)
	for _, c := range []uint16{9, 10} {
		if _, err := s.AddChannel(c); err == nil || !bytes.Equal(before, b) || len(s.Layout.Channels) != 3 {
			t.Fatal("invalid append changed slot", c, err)
		}
	}
	// Capacity can run out before the channel table fills.
	b = hostSlot()
	binary.LittleEndian.PutUint64(b[56:], 24)
	s, _ = DecodeSlot(b)
	before = bytes.Clone(b)
	if _, err := s.AddChannel(9); err == nil || !bytes.Equal(b, before) {
		t.Fatal("capacity overrun", err)
	}
}

func TestRelayoutFloat32WindowAndPlacement(t *testing.T) {
	b := hostSlot()
	s, _ := DecodeSlot(b)
	s.Layout.Extent = [3][2]uint32{{0, 1}, {1, 2}, {0, 3}}
	s.Layout.Origin = [3]float64{5, 10, 20}
	s.Layout.Translation = [3]float64{1, 2, 3}
	if err := s.RelayoutFloat32([3]uint32{1, 1, 2}, [3]uint32{0, 1, 1}); err != nil {
		t.Fatal(err)
	}
	if s.Layout.Extent != [3][2]uint32{{0, 1}, {0, 1}, {0, 2}} || s.Layout.Origin != [3]float64{5, 10, 21} || s.Layout.Translation != [3]float64{1, 2, 3} {
		t.Fatal(s.Layout)
	}
	for i, want := range []float32{104, 105, 110, 111} {
		if got := math.Float32frombits(binary.LittleEndian.Uint32(s.Data()[i*4:])); got != want {
			t.Fatal(i, got, want)
		}
	}
	if err := s.CommitLayout(); err != nil {
		t.Fatal(err)
	}
	decoded, err := DecodeSlot(b)
	if err != nil || !reflect.DeepEqual(decoded.Layout, s.Layout) {
		t.Fatal(decoded, err)
	}
}

func TestRelayoutFloat32RejectsBeforeWriting(t *testing.T) {
	for _, tc := range []struct {
		name          string
		alloc, offset [3]uint32
		capacity      uint64
	}{
		{"zero", [3]uint32{1, 0, 1}, [3]uint32{}, 128},
		{"growth", [3]uint32{2, 2, 3}, [3]uint32{}, 128},
		{"offset overflow", [3]uint32{1, 1, 1}, [3]uint32{0, 0, math.MaxUint32}, 128},
		{"capacity", [3]uint32{1, 2, 3}, [3]uint32{}, 24},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := hostSlot()
			binary.LittleEndian.PutUint64(b[56:], tc.capacity)
			s, _ := DecodeSlot(b)
			before := bytes.Clone(b)
			layout := s.Info().Layout
			if err := s.RelayoutFloat32(tc.alloc, tc.offset); err == nil || !bytes.Equal(b, before) || !reflect.DeepEqual(layout, s.Layout) {
				t.Fatal("rejected relayout mutated slot", err)
			}
		})
	}
}

func TestRelayoutFloat32Conversions(t *testing.T) {
	for _, dtype := range []pb.DType{pb.DType_DTYPE_U8, pb.DType_DTYPE_U16, pb.DType_DTYPE_F32, pb.DType_DTYPE_F64} {
		t.Run(dtype.String(), func(t *testing.T) {
			b := hostSlot()
			binary.LittleEndian.PutUint32(b[28:], uint32(dtype))
			s, _ := DecodeSlot(b)
			for i := 0; i < 12; i++ {
				x := s.Data()[i*int(ItemSize(dtype)):]
				switch dtype {
				case pb.DType_DTYPE_U8:
					x[0] = 255
				case pb.DType_DTYPE_U16:
					binary.LittleEndian.PutUint16(x, 65535)
				case pb.DType_DTYPE_F32:
					binary.LittleEndian.PutUint32(x, math.Float32bits(1.25))
				case pb.DType_DTYPE_F64:
					binary.LittleEndian.PutUint64(x, math.Float64bits(1+math.Ldexp(1, -24)))
				}
			}
			if err := s.RelayoutFloat32(s.Layout.Allocation, [3]uint32{}); err != nil {
				t.Fatal(err)
			}
			want := map[pb.DType]float32{pb.DType_DTYPE_U8: 255, pb.DType_DTYPE_U16: 65535, pb.DType_DTYPE_F32: 1.25, pb.DType_DTYPE_F64: 1}[dtype]
			for i := 0; i < 12; i++ {
				if got := math.Float32frombits(binary.LittleEndian.Uint32(s.Data()[4*i:])); got != want {
					t.Fatal(i, got, want)
				}
			}
		})
	}
}
