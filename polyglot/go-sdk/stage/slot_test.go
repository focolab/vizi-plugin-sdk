package stage

import (
	"bytes"
	"encoding/binary"
	"errors"
	"math"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// A host double built from the offset table, without SDK encoders. Three plane
// slots put data at 320; the two present channels are deliberately noncontiguous.
func hostSlot() []byte {
	b := make([]byte, 448)
	le := binary.LittleEndian
	le.PutUint64(b[0:], 0x00020003ffffffff)
	le.PutUint32(b[16:], 7)
	le.PutUint32(b[20:], 1)
	le.PutUint32(b[28:], 2) // U16
	le.PutUint32(b[32:], 2)
	le.PutUint32(b[36:], 3)
	le.PutUint32(b[40:], 1)
	le.PutUint32(b[44:], 2)
	le.PutUint32(b[48:], 3)
	le.PutUint64(b[56:], 128)
	le.PutUint32(b[68:], 1)
	le.PutUint32(b[76:], 2)
	le.PutUint32(b[84:], 3)
	for _, offset := range []int{112, 144, 176} {
		le.PutUint64(b[offset:], math.Float64bits(1))
	}
	le.PutUint64(b[208:], 99)
	le.PutUint16(b[256:], 4)
	le.PutUint16(b[258:], 65535)
	for i := 0; i < 12; i++ {
		le.PutUint16(b[320+2*i:], uint16(100+i))
	}
	return b
}

func TestSlotCurrentLayoutAndRepack(t *testing.T) {
	b := hostSlot()
	before := bytes.Clone(b)
	s, err := DecodeSlot(b)
	if err != nil {
		t.Fatal(err)
	}
	if s.Timepoint != 7 || s.OpenGeneration != 99 || s.DoneThrough != 1 || s.PlaneCapacity() != 3 || s.Capacity() != 128 {
		t.Fatal(s)
	}
	second, err := s.Plane(65535)
	if err != nil || len(second) != 12 || binary.LittleEndian.Uint16(second) != 106 {
		t.Fatal(second, err)
	}
	if _, err := s.Plane(1); !errors.Is(err, ErrChannelAbsent) {
		t.Fatal(err)
	}
	if cap(second) != len(second) || len(s.Data()) != 128 || cap(s.Data()) != 128 {
		t.Fatal("borrow exceeds bounds")
	}
	// Author repacks ALL planes, including a new channel, before committing.
	s.Layout.DType = pb.DType_DTYPE_F32
	s.Layout.Allocation = [3]uint32{1, 1, 2}
	s.Layout.Extent = [3][2]uint32{{0, 1}, {0, 1}, {0, 2}}
	s.Layout.Channels = append(s.Layout.Channels, 9)
	s.Layout.Origin = [3]float64{0.5, 1, 2}
	s.Layout.Translation = [3]float64{2, 3, 4}
	for i := 0; i < 6; i++ {
		binary.LittleEndian.PutUint32(s.Data()[4*i:], math.Float32bits(float32(i)+0.25))
	}
	if err := s.CommitLayout(); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(before[:28], b[:28]) || !bytes.Equal(before[36:40], b[36:40]) || !bytes.Equal(before[52:64], b[52:64]) || !bytes.Equal(before[208:256], b[208:256]) {
		t.Fatal("commit changed turn, outcome or fixed metadata")
	}
	if binary.LittleEndian.Uint32(b[28:]) != 3 || binary.LittleEndian.Uint32(b[32:]) != 3 || binary.LittleEndian.Uint32(b[44:]) != 1 || binary.LittleEndian.Uint16(b[260:]) != 9 {
		t.Fatal("incorrect committed offsets")
	}
	if math.Float64frombits(binary.LittleEndian.Uint64(b[88:])) != 0.5 || math.Float64frombits(binary.LittleEndian.Uint64(b[200:])) != 4 {
		t.Fatal("placement writeback")
	}
	added, err := s.Plane(9)
	if err != nil || len(added) != 8 || math.Float32frombits(binary.LittleEndian.Uint32(added)) != 4.25 {
		t.Fatal(added, err)
	}
	before = bytes.Clone(b)
	s.Layout.Allocation = [3]uint32{math.MaxUint32, math.MaxUint32, math.MaxUint32}
	if err := s.CommitLayout(); !errors.Is(err, ErrSlotInvalid) || !bytes.Equal(before, b) {
		t.Fatal("invalid layout partly written", err)
	}
}

func TestInvalidSlotHeaders(t *testing.T) {
	le := binary.LittleEndian
	for _, tc := range []struct {
		name    string
		corrupt func([]byte) []byte
	}{
		{"reserved12", func(b []byte) []byte { b[12] = 1; return b }},
		{"reserved52", func(b []byte) []byte { b[52] = 1; return b }},
		{"reserved216", func(b []byte) []byte { b[255] = 1; return b }},
		{"fixed header", func(b []byte) []byte { return b[:255] }},
		{"channel array", func(b []byte) []byte { le.PutUint32(b[36:], 65536); return b }},
		{"plane cap", func(b []byte) []byte { le.PutUint32(b[36:], 65537); return b }},
		{"capacity overflow", func(b []byte) []byte { le.PutUint64(b[56:], math.MaxUint64); return b }},
		{"duplicate channel", func(b []byte) []byte { le.PutUint16(b[258:], 4); return b }},
		{"plane count", func(b []byte) []byte { le.PutUint32(b[32:], 4); return b }},
		{"dtype", func(b []byte) []byte { le.PutUint32(b[28:], 0); return b }},
		{"packed bytes", func(b []byte) []byte { le.PutUint32(b[48:], 100); return b }},
		{"multiplication", func(b []byte) []byte {
			for _, p := range []int{40, 44, 48} {
				le.PutUint32(b[p:], math.MaxUint32)
			}
			return b
		}},
		{"extent", func(b []byte) []byte { le.PutUint32(b[64:], 2); return b }},
		{"allocation extent", func(b []byte) []byte { le.PutUint32(b[84:], 4); return b }},
		{"origin", func(b []byte) []byte { le.PutUint64(b[88:], math.Float64bits(math.NaN())); return b }},
		{"rotation", func(b []byte) []byte { le.PutUint64(b[112:], math.Float64bits(math.Inf(1))); return b }},
		{"translation", func(b []byte) []byte { le.PutUint64(b[184:], math.Float64bits(math.Inf(-1))); return b }},
	} {
		t.Run(tc.name, func(t *testing.T) {
			b := tc.corrupt(hostSlot())
			before := bytes.Clone(b)
			if _, err := DecodeSlot(b); !errors.Is(err, ErrSlotInvalid) {
				t.Fatal(err)
			}
			if !bytes.Equal(b, before) {
				t.Fatal("validation mutated input")
			}
		})
	}
}

func TestLargestChannelArray(t *testing.T) {
	h, err := HeaderBytes(65536)
	if err != nil || h != 131328 {
		t.Fatal(h, err)
	}
	b := make([]byte, 131328+65536)
	le := binary.LittleEndian
	le.PutUint32(b[28:], 1)
	le.PutUint32(b[32:], 65536)
	le.PutUint32(b[36:], 65536)
	for _, offset := range []int{40, 44, 48, 68, 76, 84} {
		le.PutUint32(b[offset:], 1)
	}
	le.PutUint64(b[56:], 65536)
	for i := 0; i < 65536; i++ {
		le.PutUint16(b[256+2*i:], uint16(i))
	}
	b[len(b)-1] = 123
	s, err := DecodeSlot(b)
	if err != nil {
		t.Fatal(err)
	}
	p, err := s.Plane(65535)
	if err != nil || len(p) != 1 || p[0] != 123 {
		t.Fatal(p, err)
	}
}

func TestTurnEqualityAndWrap(t *testing.T) {
	top := DecodeTurn(0x00020003ffffffff)
	if top.Iteration != math.MaxUint32 || top.Pipeline != 3 || top.Position != 2 || top.Word() != 0x00020003ffffffff {
		t.Fatal(top)
	}
	if top.IsNew(3, 2, math.MaxUint32) || top.IsNew(4, 2, 1) || top.IsNew(3, 1, 1) {
		t.Fatal("wrong or repeated turn")
	}
	wrapped := DecodeTurn(0x0002000300000001)
	if !wrapped.IsNew(3, 2, math.MaxUint32) || wrapped.HandoffWord(true) != 0x0003000300000001 || wrapped.HandoffWord(false) != 0xfffe000300000001 {
		t.Fatal("wrap/return erased volume identity")
	}
	if (Turn{Pipeline: 3, Position: 2}).IsNew(3, 2, 5) {
		t.Fatal("acted on never-filled slot")
	}
}

func TestPoseCompositionUsesOldRotation(t *testing.T) {
	l := Layout{Rotation: [9]float64{0, -1, 0, 1, 0, 0, 0, 0, 1}, Translation: [3]float64{10, 20, 30}}
	l.ComposePose([9]float64{0, -1, 0, 1, 0, 0, 0, 0, 1}, [3]float64{2, 3, 4})
	if l.Rotation != ([9]float64{-1, 0, 0, 0, -1, 0, 0, 0, 1}) || l.Translation != ([3]float64{7, 22, 34}) {
		t.Fatal(l)
	}
}

func TestSlotAllowsTrailingRegionPadding(t *testing.T) {
	original := hostSlot()
	padding := bytes.Repeat([]byte{0xa5}, 64)
	b := append(original, padding...)
	s, err := DecodeSlot(b)
	if err != nil {
		t.Fatal(err)
	}
	if len(s.Data()) != 128 || cap(s.Data()) != 128 {
		t.Fatal("region padding exposed as data", len(s.Data()), cap(s.Data()))
	}
	clear(s.Data())
	if err := s.CommitLayout(); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(b[len(original):], padding) {
		t.Fatal("padding modified")
	}
}
