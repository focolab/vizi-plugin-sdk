// Package payload provides checked, contiguous array codecs for the published
// pixel and geometry schemas. It never chooses a segment purpose or invents an
// ownership handoff. Spatial arrays are ZYX; whole datasets are TCZYX.
package payload

import (
	"encoding/binary"
	"errors"
	"fmt"
	"math"
	"reflect"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Scalar is the protocol's numeric vocabulary. Named Go scalar types work too.
type Scalar interface {
	~uint8 | ~uint16 | ~float32 | ~float64
}

// ByteOrder is explicit where a descriptor does not carry it. The zero value
// is invalid: metadata's endianness does not establish application byte order.
type ByteOrder uint8

const (
	LittleEndian ByteOrder = iota + 1
	BigEndian
)

func (o ByteOrder) Validate() error {
	if o != LittleEndian && o != BigEndian {
		return errors.New("payload byte order must be specified")
	}
	return nil
}

func SizeOf(dtype pb.DType) (uint64, error) {
	switch dtype {
	case pb.DType_DTYPE_U8:
		return 1, nil
	case pb.DType_DTYPE_U16:
		return 2, nil
	case pb.DType_DTYPE_F32:
		return 4, nil
	case pb.DType_DTYPE_F64:
		return 8, nil
	default:
		return 0, fmt.Errorf("unsupported dtype %v", dtype)
	}
}

// ByteLen validates positive dimensions and multiplication before allocation.
// Empty draw lists are represented by no overlay items, not zero-sized tensors.
func ByteLen(shape []uint32, dtype pb.DType) (uint64, error) {
	n, err := SizeOf(dtype)
	if err != nil {
		return 0, err
	}
	if len(shape) == 0 {
		return 0, errors.New("array shape is absent")
	}
	for _, d := range shape {
		if d == 0 {
			return 0, errors.New("array dimensions must be positive")
		}
		if n > math.MaxUint64/uint64(d) {
			return 0, errors.New("array byte length overflows")
		}
		n *= uint64(d)
	}
	return n, nil
}

// Tensor owns its bytes and metadata. Accessors return copies; it is safe to
// retain after a host reply closes or to share between application workers.
type Tensor struct {
	data   []byte
	shape  []uint32
	dtype  pb.DType
	little bool
}

func New(data []byte, shape []uint32, dtype pb.DType, littleEndian bool) (*Tensor, error) {
	n, err := ByteLen(shape, dtype)
	if err != nil {
		return nil, err
	}
	if n != uint64(len(data)) {
		return nil, fmt.Errorf("array has %d bytes, shape and dtype require %d", len(data), n)
	}
	return &Tensor{append([]byte(nil), data...), append([]uint32(nil), shape...), dtype, littleEndian}, nil
}

func (t *Tensor) Bytes() []byte      { return append([]byte(nil), t.data...) }
func (t *Tensor) Shape() []uint32    { return append([]uint32(nil), t.shape...) }
func (t *Tensor) DType() pb.DType    { return t.dtype }
func (t *Tensor) LittleEndian() bool { return t.little }
func (t *Tensor) ByteLen() uint64    { return uint64(len(t.data)) }

func dtypeOf[T Scalar]() pb.DType {
	var zero T
	switch reflect.TypeOf(zero).Kind() {
	case reflect.Uint8:
		return pb.DType_DTYPE_U8
	case reflect.Uint16:
		return pb.DType_DTYPE_U16
	case reflect.Float32:
		return pb.DType_DTYPE_F32
	default:
		return pb.DType_DTYPE_F64
	}
}

func byteOrder(little bool) binary.ByteOrder {
	if little {
		return binary.LittleEndian
	}
	return binary.BigEndian
}

// Encode copies values into a checked tensor, without unsafe slice casts or
// assumptions about the machine's native byte order.
func Encode[T Scalar](values []T, shape []uint32, littleEndian bool) (*Tensor, error) {
	dtype := dtypeOf[T]()
	n, err := ByteLen(shape, dtype)
	if err != nil {
		return nil, err
	}
	size, _ := SizeOf(dtype)
	if n/size != uint64(len(values)) {
		return nil, errors.New("value count does not match shape")
	}
	if n > uint64(int(^uint(0)>>1)) {
		return nil, errors.New("array exceeds addressable slice size")
	}
	data := make([]byte, int(n))
	order := byteOrder(littleEndian)
	for i, v := range values {
		p := data[uint64(i)*size:]
		switch dtype {
		case pb.DType_DTYPE_U8:
			p[0] = byte(v)
		case pb.DType_DTYPE_U16:
			order.PutUint16(p, uint16(v))
		case pb.DType_DTYPE_F32:
			order.PutUint32(p, math.Float32bits(float32(v)))
		case pb.DType_DTYPE_F64:
			order.PutUint64(p, math.Float64bits(float64(v)))
		}
	}
	return &Tensor{data, append([]uint32(nil), shape...), dtype, littleEndian}, nil
}

// Decode requires the exact scalar dtype; it never silently truncates floats
// or narrows integers. The result is an independent contiguous slice.
func Decode[T Scalar](t *Tensor) ([]T, error) {
	if t == nil {
		return nil, errors.New("tensor is nil")
	}
	if t.dtype != dtypeOf[T]() {
		return nil, errors.New("requested Go scalar does not match tensor dtype")
	}
	size, _ := SizeOf(t.dtype)
	out := make([]T, uint64(len(t.data))/size)
	order := byteOrder(t.little)
	for i := range out {
		p := t.data[uint64(i)*size:]
		switch t.dtype {
		case pb.DType_DTYPE_U8:
			out[i] = T(p[0])
		case pb.DType_DTYPE_U16:
			out[i] = T(order.Uint16(p))
		case pb.DType_DTYPE_F32:
			out[i] = T(math.Float32frombits(order.Uint32(p)))
		case pb.DType_DTYPE_F64:
			out[i] = T(math.Float64frombits(order.Uint64(p)))
		}
	}
	return out, nil
}

// ValidateSegment checks the descriptor's schema and arithmetic. Capability,
// region, producer and lifetime validation remain with control.Memory.
func ValidateSegment(d *pb.SegmentRef) error {
	if d == nil || d.SegmentId == 0 {
		return errors.New("segment descriptor has no identity")
	}
	if len(d.ShapeZyx) != 3 {
		return errors.New("segment shape must have three ZYX dimensions")
	}
	n, err := ByteLen(d.ShapeZyx, d.Dtype)
	if err != nil {
		return err
	}
	if n != d.Len {
		return errors.New("segment length disagrees with shape and dtype")
	}
	if d.Offset > math.MaxUint64-d.Len {
		return errors.New("segment range overflows")
	}
	return nil
}

// InlineGeometry is the published GeomBuffer inline path, bounded to 8 MiB.
// It preserves bytes exactly. GeomBuffer has no byte-order field: the caller
// must encode in the order its host binding expects. No live slot is assumed.
func (t *Tensor) InlineGeometry() (*pb.GeomBuffer, error) {
	if t == nil {
		return nil, errors.New("tensor is nil")
	}
	n, err := ByteLen(t.shape, t.dtype)
	if err != nil {
		return nil, err
	}
	if n != uint64(len(t.data)) {
		return nil, errors.New("invalid tensor byte length")
	}
	if len(t.data) > 8<<20 {
		return nil, errors.New("inline geometry exceeds 8 MiB")
	}
	return &pb.GeomBuffer{Source: &pb.GeomBuffer_Inline{Inline: t.Bytes()}, Shape: t.Shape(), Dtype: t.dtype}, nil
}
