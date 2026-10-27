package payload

import (
	"bytes"
	"errors"
	"math"
	"reflect"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type intensity uint16

func TestEndianGoldenAndOwnedCopies(t *testing.T) {
	shape := []uint32{1, 1, 2}
	values := []intensity{0x1234, 0xabcd}
	tensor, err := Encode(values, shape, false)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(tensor.Bytes(), []byte{0x12, 0x34, 0xab, 0xcd}) {
		t.Fatal(tensor.Bytes())
	}
	values[0] = 0
	shape[2] = 100
	copyBytes := tensor.Bytes()
	copyBytes[0] = 0
	decoded, err := Decode[intensity](tensor)
	if err != nil || !reflect.DeepEqual(decoded, []intensity{0x1234, 0xabcd}) {
		t.Fatal(decoded, err)
	}
	if _, err := Decode[float32](tensor); err == nil {
		t.Fatal("silently converted dtype")
	}
	input := []byte{0x34, 0x12}
	little, err := New(input, []uint32{1}, pb.DType_DTYPE_U16, true)
	if err != nil {
		t.Fatal(err)
	}
	input[0] = 0
	if v, _ := Decode[uint16](little); v[0] != 0x1234 {
		t.Fatal(v)
	}
}

func TestFloatWireBitsAndBounds(t *testing.T) {
	v32 := []float32{float32(math.Copysign(0, -1)), 1.5, float32(math.Inf(1))}
	tensor, err := Encode(v32, []uint32{3}, true)
	if err != nil {
		t.Fatal(err)
	}
	want := []byte{0, 0, 0, 0x80, 0, 0, 0xc0, 0x3f, 0, 0, 0x80, 0x7f}
	if !bytes.Equal(tensor.Bytes(), want) {
		t.Fatal(tensor.Bytes())
	}
	v64 := []float64{math.Copysign(0, -1), math.Float64frombits(0x7ff8000000000012), -123.75}
	tensor, err = Encode(v64, []uint32{3}, false)
	if err != nil {
		t.Fatal(err)
	}
	back, err := Decode[float64](tensor)
	if err != nil {
		t.Fatal(err)
	}
	for i, v := range back {
		if math.Float64bits(v) != math.Float64bits(v64[i]) {
			t.Fatal(back)
		}
	}
	for _, shape := range [][]uint32{nil, {1, 0}, {math.MaxUint32, math.MaxUint32, math.MaxUint32}} {
		if _, err := ByteLen(shape, pb.DType_DTYPE_F64); err == nil {
			t.Fatal("invalid shape accepted", shape)
		}
	}
	if _, err := (&Tensor{}).InlineGeometry(); err == nil {
		t.Fatal("zero tensor accepted")
	}
	if _, err := New([]byte{1}, []uint32{1}, pb.DType_DTYPE_U16, true); err == nil {
		t.Fatal("short bytes accepted")
	}
}

type copyFunc func(*pb.SegmentRef, []byte) error

func (f copyFunc) CopyDescriptor(d *pb.SegmentRef, b []byte) error { return f(d, b) }

func TestCopyBudgetBeforeAccess(t *testing.T) {
	d := &pb.SegmentRef{SegmentId: 1, Offset: 32, Len: 4, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U16}
	calls := 0
	copier := copyFunc(func(_ *pb.SegmentRef, b []byte) error { calls++; copy(b, []byte{1, 0, 2, 0}); return nil })
	for _, opts := range []CopyOptions{{}, {4, 0}, {3, LittleEndian}} {
		if _, err := CopySegment(copier, d, opts); err == nil {
			t.Fatal("invalid copy options accepted", opts)
		}
	}
	if calls != 0 {
		t.Fatal("copied before admission")
	}
	tensor, err := CopySegment(copier, d, CopyOptions{4, LittleEndian})
	if err != nil || calls != 1 {
		t.Fatal(calls, err)
	}
	values, _ := Decode[uint16](tensor)
	if !reflect.DeepEqual(values, []uint16{1, 2}) {
		t.Fatal(values)
	}
	d.Offset = math.MaxUint64
	if err := ValidateSegment(d); err == nil {
		t.Fatal("range overflow accepted")
	}
}

func TestExportChannelConcatenationAndFailure(t *testing.T) {
	opened := &pb.OpenExport{ShapeTczyx: []uint32{2, 3, 1, 1, 2}, Dtype: pb.DType_DTYPE_U16, LittleEndian: false}
	chunk := &pb.ExportChunk{T: 1, Channels: []uint32{2, 0}, Source: &pb.ExportChunk_Inline{Inline: []byte{0, 1, 0, 2, 0, 3, 0, 4}}}
	tensor, err := CopyExportChunk(nil, opened, chunk, 8)
	if err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(tensor.Shape(), []uint32{2, 1, 1, 2}) || tensor.LittleEndian() {
		t.Fatal(tensor)
	}
	values, _ := Decode[uint16](tensor)
	if !reflect.DeepEqual(values, []uint16{1, 2, 3, 4}) {
		t.Fatal(values)
	}
	chunk.Install = &pb.InstallSegment{SegmentId: 1}
	if _, err := CopyExportChunk(nil, opened, chunk, 8); err == nil {
		t.Fatal("inline ride-along accepted")
	}
	chunk.Source = &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1, Offset: 32, Len: 8, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U16}}
	failure := errors.New("copy failed")
	copier := copyFunc(func(d *pb.SegmentRef, b []byte) error {
		if len(b) != 8 {
			t.Fatal(len(b))
		}
		return failure
	})
	if _, err := CopyExportChunk(copier, opened, chunk, 8); !errors.Is(err, failure) {
		t.Fatal(err)
	}
	chunk.Channels[0] = 3
	if _, err := CopyExportChunk(copier, opened, chunk, 8); !errors.Is(err, failure) {
		t.Fatal("source channel did not reach copier", err)
	}
	chunk.Channels = []uint32{0, 1, 2, 3}
	if _, err := CopyExportChunk(copier, opened, chunk, 16); errors.Is(err, failure) || err == nil {
		t.Fatal("excess channel count reached copier", err)
	}
}

func TestExportNonLeadingSourceSelection(t *testing.T) {
	opened := &pb.OpenExport{ShapeTczyx: []uint32{1, 1, 1, 1, 2}, Dtype: pb.DType_DTYPE_U8, LittleEndian: true}
	for _, segment := range []bool{false, true} {
		chunk := &pb.ExportChunk{T: 7, Channels: []uint32{3}, Source: &pb.ExportChunk_Inline{Inline: []byte{4, 5}}}
		if segment {
			chunk.Source = &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 1, Offset: 32, Len: 2, ShapeZyx: []uint32{1, 1, 2}, Dtype: pb.DType_DTYPE_U8}}
		}
		copier := copyFunc(func(_ *pb.SegmentRef, b []byte) error { copy(b, []byte{4, 5}); return nil })
		tensor, err := CopyExportChunk(copier, opened, chunk, 2)
		if err != nil {
			t.Fatal(err)
		}
		values, err := Decode[uint8](tensor)
		if err != nil || !reflect.DeepEqual(values, []uint8{4, 5}) {
			t.Fatal(values, err)
		}
	}
}
