package stage

import (
	"encoding/binary"
	"errors"
	"fmt"
	"maps"
	"math"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

var ErrSlotInvalid = errors.New("invalid processing-stage slot")
var ErrChannelAbsent = errors.New("processing-stage channel is absent")

type Outcome uint32

const (
	None Outcome = iota
	Processed
	Refused
	Failed
)

// Turn is a decoded snapshot of ONE atomic load, not three separate reads.
// DecodeTurn/Word perform no memory access and provide no acquire/release fence.
type Turn struct {
	Iteration uint32
	Pipeline  uint16
	Position  int16
}

func DecodeTurn(word uint64) Turn { return Turn{uint32(word), uint16(word >> 32), int16(word >> 48)} }
func (t Turn) Word() uint64 {
	return uint64(t.Iteration) | uint64(t.Pipeline)<<32 | uint64(uint16(t.Position))<<48
}
func (t Turn) IsNew(pipeline uint16, position int16, last uint32) bool {
	return pipeline != 0 && position > 0 && t.Pipeline == pipeline && t.Position == position && t.Iteration != 0 && t.Iteration != last
}

// HandoffWord preserves the volume and pipeline and publishes the next holder.
func (t Turn) HandoffWord(processed bool) uint64 {
	if processed {
		t.Position++
	} else {
		t.Position = -t.Position
	}
	return t.Word()
}

// Layout describes CURRENT packed planes, not the largest negotiated output.
// Channels[p] is the channel identity of plane p; it need not equal p.
type Layout struct {
	DType       pb.DType
	Channels    []uint16
	Allocation  [3]uint32    // Z, Y, X
	Extent      [3][2]uint32 // [start,end) per Z,Y,X axis
	Origin      [3]float64
	Rotation    [9]float64 // row-major, acting on column Z,Y,X vectors
	Translation [3]float64
}

// Slot is a borrowed typed view. The caller MUST already own its turn, retain
// the mapping, and stop using this value and all its slices before handing back.
// DecodeSlot does not claim a turn. Metadata outside Layout is observational; CommitLayout leaves it
// unchanged, along with the atomic turn word and all padding.
type Slot struct {
	// Params is an owned copy of this position's armed values, filled by the worker.
	Params                         Params
	Layout                         Layout
	Outcome                        Outcome
	Timepoint, DoneThrough, Reason uint32
	OpenGeneration                 uint64
	bytes                          []byte
	headerBytes                    int
	planeCap                       uint32
	capacity                       uint64
}

func HeaderBytes(planeCap uint32) (uint64, error) {
	if planeCap > MaxChannels {
		return 0, fmt.Errorf("%w: plane_cap exceeds 65536", ErrSlotInvalid)
	}
	return (256 + 2*uint64(planeCap) + 63) &^ 63, nil
}

func (l Layout) planeBytes() (uint64, error) {
	n := ItemSize(l.DType)
	if n == 0 {
		return 0, fmt.Errorf("%w: unknown dtype %d", ErrSlotInvalid, l.DType)
	}
	for _, dim := range l.Allocation {
		if dim == 0 {
			return 0, nil
		}
	}
	for _, dim := range l.Allocation {
		if n > math.MaxUint64/uint64(dim) {
			return 0, fmt.Errorf("%w: plane size overflows", ErrSlotInvalid)
		}
		n *= uint64(dim)
	}
	return n, nil
}

func (s *Slot) validateLayout() (uint64, error) {
	if uint64(len(s.Layout.Channels)) > uint64(s.planeCap) {
		return 0, fmt.Errorf("%w: planes exceeds plane_cap", ErrSlotInvalid)
	}
	seen := make(map[uint16]bool, len(s.Layout.Channels))
	for _, c := range s.Layout.Channels {
		if seen[c] {
			return 0, fmt.Errorf("%w: duplicate channel %d", ErrSlotInvalid, c)
		}
		seen[c] = true
	}
	n, err := s.Layout.planeBytes()
	if err != nil {
		return 0, err
	}
	if n != 0 && uint64(len(s.Layout.Channels)) > s.capacity/n {
		return 0, fmt.Errorf("%w: packed planes exceed capacity", ErrSlotInvalid)
	}
	for axis, extent := range s.Layout.Extent {
		if extent[0] > extent[1] || extent[1] > s.Layout.Allocation[axis] {
			return 0, fmt.Errorf("%w: extent outside allocation", ErrSlotInvalid)
		}
	}
	for _, values := range [][]float64{s.Layout.Origin[:], s.Layout.Rotation[:], s.Layout.Translation[:]} {
		for _, v := range values {
			if math.IsNaN(v) || math.IsInf(v, 0) {
				return 0, fmt.Errorf("%w: nonfinite origin or pose", ErrSlotInvalid)
			}
		}
	}
	return n, nil
}

// DecodeSlot implements §5's fixed offsets and §7's minimum validation. It
// never reads bytes [0,8): the turn word must only be accessed atomically.
func DecodeSlot(data []byte) (*Slot, error) {
	if len(data) < 256 {
		return nil, fmt.Errorf("%w: truncated fixed header", ErrSlotInvalid)
	}
	for _, span := range [][2]int{{12, 16}, {52, 56}, {216, 256}} {
		for _, b := range data[span[0]:span[1]] {
			if b != 0 {
				return nil, fmt.Errorf("%w: nonzero reserved field", ErrSlotInvalid)
			}
		}
	}
	le := binary.LittleEndian
	planes, cap := le.Uint32(data[32:36]), le.Uint32(data[36:40])
	h, err := HeaderBytes(cap)
	if err != nil {
		return nil, err
	}
	capacity := le.Uint64(data[56:64])
	if h > uint64(len(data)) || capacity > uint64(len(data))-h {
		return nil, fmt.Errorf("%w: header plus capacity exceeds region extent", ErrSlotInvalid)
	}
	if planes > cap {
		return nil, fmt.Errorf("%w: planes exceeds plane_cap", ErrSlotInvalid)
	}
	s := &Slot{Outcome: Outcome(le.Uint32(data[8:12])), Timepoint: le.Uint32(data[16:20]), DoneThrough: le.Uint32(data[20:24]), Reason: le.Uint32(data[24:28]), OpenGeneration: le.Uint64(data[208:216]), bytes: data, headerBytes: int(h), planeCap: cap, capacity: capacity}
	s.Layout.DType = pb.DType(le.Uint32(data[28:32]))
	s.Layout.Channels = make([]uint16, planes)
	for i := range s.Layout.Channels {
		s.Layout.Channels[i] = le.Uint16(data[256+2*i:])
	}
	for i := 0; i < 3; i++ {
		s.Layout.Allocation[i] = le.Uint32(data[40+4*i:])
		s.Layout.Extent[i] = [2]uint32{le.Uint32(data[64+8*i:]), le.Uint32(data[68+8*i:])}
		s.Layout.Origin[i] = math.Float64frombits(le.Uint64(data[88+8*i:]))
		s.Layout.Translation[i] = math.Float64frombits(le.Uint64(data[184+8*i:]))
	}
	for i := range s.Layout.Rotation {
		s.Layout.Rotation[i] = math.Float64frombits(le.Uint64(data[112+8*i:]))
	}
	if _, err := s.validateLayout(); err != nil {
		return nil, err
	}
	return s, nil
}

func (s *Slot) PlaneCapacity() uint32 { return s.planeCap }
func (s *Slot) Capacity() uint64      { return s.capacity }

// Data includes all reserved data space so an author can repack/add planes.
// Its capacity is clamped; appending past it cannot overwrite the next region.
func (s *Slot) Data() []byte {
	end := s.headerBytes + int(s.capacity)
	return s.bytes[s.headerBytes:end:end]
}
func (s *Slot) Plane(channel uint16) ([]byte, error) {
	n, err := s.validateLayout()
	if err != nil {
		return nil, err
	}
	for i, c := range s.Layout.Channels {
		if c == channel {
			start, end := uint64(i)*n, uint64(i+1)*n
			return s.Data()[start:end:end], nil
		}
	}
	return nil, ErrChannelAbsent
}

// CommitLayout writes the mutable fields of §5, after validating the entire
// new layout. It does not move pixels, write outcome/done_through, or hand off
// the turn. Authors must repack every plane themselves when layout changes.
// On validation failure no header byte is changed.
func (s *Slot) CommitLayout() error {
	if _, err := s.validateLayout(); err != nil {
		return err
	}
	le := binary.LittleEndian
	b := s.bytes
	le.PutUint32(b[28:32], uint32(s.Layout.DType))
	le.PutUint32(b[32:36], uint32(len(s.Layout.Channels)))
	for i := 0; i < 3; i++ {
		le.PutUint32(b[40+4*i:], s.Layout.Allocation[i])
		le.PutUint32(b[64+8*i:], s.Layout.Extent[i][0])
		le.PutUint32(b[68+8*i:], s.Layout.Extent[i][1])
		le.PutUint64(b[88+8*i:], math.Float64bits(s.Layout.Origin[i]))
		le.PutUint64(b[184+8*i:], math.Float64bits(s.Layout.Translation[i]))
	}
	for i, v := range s.Layout.Rotation {
		le.PutUint64(b[112+8*i:], math.Float64bits(v))
	}
	for i, c := range s.Layout.Channels {
		le.PutUint16(b[256+2*i:], c)
	}
	return nil
}

// ValidateShape checks the current layout against an armed maximum.
func (s *Slot) ValidateShape(shape *pb.StageShape) error {
	if err := ValidateShape(shape); err != nil {
		return err
	}
	if s.Layout.DType != shape.Dtype || s.Layout.Allocation[0] > shape.Z || s.Layout.Allocation[1] > shape.Y || s.Layout.Allocation[2] > shape.X {
		return fmt.Errorf("%w: layout exceeds armed shape or has different dtype", ErrSlotInvalid)
	}
	_, err := s.validateLayout()
	return err
}

// ComposePose applies relative motion using the original rotation for both terms.
func (l *Layout) ComposePose(rotation [9]float64, translation [3]float64) {
	old := l.Rotation
	for i := 0; i < 3; i++ {
		for j := 0; j < 3; j++ {
			l.Rotation[3*i+j] = 0
			for k := 0; k < 3; k++ {
				l.Rotation[3*i+j] += old[3*i+k] * rotation[3*k+j]
			}
		}
		for k := 0; k < 3; k++ {
			l.Translation[i] += old[3*i+k] * translation[k]
		}
	}
}

// Refusal is returned only by a preflight Check, before mutable memory is exposed.
// Reason is 1 (earlier state), 2 (missing declared channel), or 3 (other).
type Refusal struct {
	Reason uint32
	Detail string
}

func (r *Refusal) Error() string { return r.Detail }

// Info is an owned metadata snapshot for preflight checks. It has no pixel data.
type Info struct {
	Params                 Params
	Layout                 Layout
	Timepoint, DoneThrough uint32
	OpenGeneration         uint64
}

func (s *Slot) Info() Info {
	l := s.Layout
	l.Channels = append([]uint16(nil), l.Channels...)
	return Info{Layout: l, Timepoint: s.Timepoint, DoneThrough: s.DoneThrough, OpenGeneration: s.OpenGeneration, Params: maps.Clone(s.Params)}
}
