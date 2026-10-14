package doorbell

import (
	"errors"
	"math"
	"sync"
)

const (
	RecordSize          = 112
	Version             = 2
	ProtocolMajor       = 1
	ProtocolMinor       = 0
	OpMemory       byte = 'M'
	OpInput        byte = 'E'
	OpNotification byte = 'D'
)

var (
	ErrClosed    = errors.New("doorbell is closed")
	ErrHandshake = errors.New("doorbell requires completed protocol 1.0 handshake")
	ErrLifecycle = errors.New("invalid or stale lifecycle input")
	ErrQueueFull = errors.New("doorbell lifecycle outcome queue is full; input was not applied")
	ErrToolLimit = errors.New("doorbell active tool limit reached; activation was not applied")
)

type RawRecord [RecordSize]byte

type Header struct {
	Op, Version, Flags, Reserved byte
	ID                           uint32
}

type Permissions byte

const (
	PluginRead Permissions = 1 << iota
	PluginWrite
	HostRead
	HostWrite
)

type Memory struct {
	SegmentID, Size uint64
	Permissions     Permissions
	// Name is the validated Windows section name. Empty on Unix, where the
	// Frame.Descriptor is the capability. Mapping remains the installer's job.
	Name string
}

// Mask uses LSB-first region indices, 0 through 511. Bits are hints, not state.
type Mask [64]byte

func (m *Mask) Set(region uint16) error {
	if region >= 512 {
		return errors.New("region index exceeds 511")
	}
	m[region/8] |= 1 << (region % 8)
	return nil
}
func (m Mask) Has(region uint16) bool { return region < 512 && m[region/8]&(1<<(region%8)) != 0 }
func (m *Mask) Merge(other Mask) {
	for i := range m {
		m[i] |= other[i]
	}
}

// Notification.Epoch == 0 means no pane association; ViewID must then be 0.
// ViewID == 0 is a valid pane when Epoch != 0.
type Notification struct {
	SegmentID uint64
	Mask      Mask
	Epoch     uint32
	ViewID    uint8
}

type Phase byte

const (
	Press Phase = iota
	Drag
	Release
	Cancel
	Fire
)

func (p Phase) opening() bool { return p == Press || p == Fire }

type PositionKind byte

const (
	None PositionKind = iota
	Voxel
	Picked
	ColumnYX
	ColumnZX
	ColumnZY
	Ray
	RayAndPicked
)

type TargetKind byte

const (
	NoTarget TargetKind = iota
	Annotation
	ROI
	Handle
)
const (
	Shift    byte = 1
	Ctrl     byte = 2
	Alt      byte = 4
	Meta     byte = 8
	Miss     byte = 1
	RollRefY byte = 2
)

// Position.Values follows the union's documented order: z,y,x for points;
// the two named axes then range min,max for columns; camera z,y,x, far z,y,x,
// entry fraction, roll for rays, followed by picked z,y,x and back fraction
// for RayAndPicked. Set Values[11] to NaN for a point pick; zero is a real pick
// of no depth, its back at its front. BackFraction, IsPoint, FrontFraction,
// Front and Back expose the pick's extent.
// Kind None represents absence. Unknown wire kinds normalize to None.
type Position struct {
	Kind   PositionKind
	Flags  byte
	Values [12]float32
}

// BackFraction returns RayAndPicked's back fraction, or false for other kinds:
// how far the picked structure runs behind its front, as a fraction of the ray
// left there. A finite value is in [0, 1], and 0 is a structure of no depth,
// which is also what a sender that predates the field leaves. NaN says nothing
// stood out. Pick presence is independent of the input's phase.
func (p Position) BackFraction() (float32, bool) {
	if p.Kind != RayAndPicked {
		return 0, false
	}
	return p.Values[11], true
}

// IsPoint reports whether this is a RayAndPicked where nothing stood out along
// the ray (NaN back fraction): the picked voxel is a likely one with no
// structure around it. A zero-depth pick (0) is not a point. Other position
// kinds return false.
func (p Position) IsPoint() bool {
	fraction, picked := p.BackFraction()
	return picked && math.IsNaN(float64(fraction))
}

// FrontFraction returns where RayAndPicked's picked voxel centre projects onto
// camera -> far, as a fraction of it clamped to [0, 1]; 0 when the camera and
// far coincide. It returns false for other kinds. Arithmetic uses float64 to
// avoid intermediate overflow for finite f32 endpoints.
func (p Position) FrontFraction() (float64, bool) {
	if p.Kind != RayAndPicked {
		return 0, false
	}
	var dot, dd float64
	for i := 0; i < 3; i++ {
		camera := float64(p.Values[i])
		d := float64(p.Values[i+3]) - camera
		dot += (float64(p.Values[i+8]) - camera) * d
		dd += d * d
	}
	if dd <= 0 {
		return 0, true
	}
	return math.Min(1, math.Max(0, dot/dd)), true
}

// Front returns the picked front on the ray, in ZYX voxel coordinates at the
// input's level: the picked voxel's centre projected onto camera -> far. It
// returns false for any kind other than RayAndPicked.
func (p Position) Front() (zyx [3]float64, ok bool) {
	t, picked := p.FrontFraction()
	if !picked {
		return zyx, false
	}
	return p.onRay(t), true
}

// Back returns the picked structure's back point in ZYX voxel coordinates at
// the input's level: front + back_frac * (far - front). For a structure of no
// depth that is the front itself. It returns false for a point pick or any
// kind other than RayAndPicked. As with Values, callers should use a validated
// input.
func (p Position) Back() (zyx [3]float64, ok bool) {
	fraction, picked := p.BackFraction()
	if !picked || math.IsNaN(float64(fraction)) {
		return zyx, false
	}
	t, _ := p.FrontFraction()
	return p.onRay(t + float64(fraction)*(1-t)), true
}

func (p Position) onRay(t float64) (zyx [3]float64) {
	for i := range zyx {
		camera, far := float64(p.Values[i]), float64(p.Values[i+3])
		zyx[i] = camera + t*(far-camera)
	}
	return zyx
}

type Target struct {
	Kind     TargetKind
	ID, Part uint32
}

// Input.Generation is the low 32 bits of the dataset's open generation.
// The full generation and geometry are supplied by the arranged pane state.
type Input struct {
	Generation, Time, Interaction, Sequence, Epoch, Tool uint32
	Channels                                             uint16
	// Action is the 1-based ToolBinding action index on FIRE, or zero for
	// primary input. Unknown indices remain deliverable for compatibility.
	Action                               uint16
	Phase                                Phase
	ViewID, Level, Modifiers, ClickCount uint8
	Target                               Target
	Position                             Position
}

// Ray returns the 3D ray this input carries, whether or not a pick came with
// it (Ray or RayAndPicked): camera, far, entry fraction and roll. It returns
// false for every other kind. A tool reads the ray here and does not branch on
// which of the two arrived.
func (e Input) Ray() (camera, far [3]float32, entryFraction, roll float32, ok bool) {
	if e.Position.Kind != Ray && e.Position.Kind != RayAndPicked {
		return camera, far, 0, 0, false
	}
	v := e.Position.Values
	return [3]float32{v[0], v[1], v[2]}, [3]float32{v[3], v[4], v[5]}, v[6], v[7], true
}

// Pick returns the host's pick along the ray, when one came: the RayAndPicked
// position, on which Front, Back and IsPoint answer, and the picked voxel in
// ZYX. It returns false on a bare Ray (the tool declared wants_pick false, or
// the host found nothing to send) and on every non-ray kind.
func (e Input) Pick() (pick Position, picked [3]float32, ok bool) {
	if e.Position.Kind != RayAndPicked {
		return Position{}, picked, false
	}
	v := e.Position.Values
	return e.Position, [3]float32{v[8], v[9], v[10]}, true
}

type Disposition string

const (
	Decoded         Disposition = "decoded"
	UnknownOpcode   Disposition = "unknown_opcode"
	Rejected        Disposition = "semantic_rejection"
	Ignored         Disposition = "ignored"
	FramingRejected Disposition = "framing_rejection"
)

// Diagnostic is local and countable, never a ContractViolation by itself.
type Diagnostic struct {
	Code               string
	Count              uint64
	Expected, Observed uint64
}

// Violation.Route is "contract_violation" or "grant_ack". Grant failures here
// always have Class "malformed". The supervisor supplies control-plane messages.
type Violation struct{ Where, Route, Class string }

type Frame struct {
	Raw RawRecord
	// HasRecord is false for a short/oversized receive; consult Fault.Capture.
	HasRecord bool
	Header    Header
	// HasHeader is true only after CRC validation.
	HasHeader   bool
	Ordinal     uint64
	Disposition Disposition
	Diagnostics []Diagnostic
	Violation   *Violation
	Memory      *Memory
	Input       *Input
	// InputContextError is set locally by the control supervisor when accepted
	// input cannot be reconciled with its acknowledged pane. The record is still
	// delivered, including terminal closure. A standalone transport does not
	// check arranged context; nil there does not establish context validity.
	InputContextError error
	Notification      *Notification
	Descriptor        *Descriptor
	// ImplicitlyClosed identifies the prior gesture ended by an accepted PRESS.
	// It has no separate cancellation event. Zero means none.
	ImplicitlyClosed uint32
}

// Cancellation carries the original opening context, so the consumer can finish
// a partial stroke against its old epoch. No synthetic CANCEL record is made.
type Cancellation struct {
	Opening Input
	Reason  string
}

// Fault uses pointers for absent facts, as required by protocol section 12.
// Capture is nil when no received bytes exist (an empty datagram uses []byte{}).
type Fault struct {
	Class, Detail, Transport, Direction string
	Capture                             []byte
	ObservedLength                      *int
	Ordinal                             *uint64
	ExpectedCRC, ObservedCRC            *uint32
	ControlTruncated                    *bool
	OSError                             *int
	Header                              *Header // present only after CRC passes
	Socket                              SocketInfo
}
type SocketInfo struct {
	Type, SendBuffer, ReceiveBuffer int
	Nonblocking                     bool
}
type Terminal struct {
	Reason  string
	Fault   *Fault
	OSError *int
	// Capture retains a partial TCP record on EOF, error, or local stop. Nil
	// when none was buffered. Partial EOF is teardown, not a framing fault.
	Capture []byte
}

// Exactly one member is populated. Frame rejection and terminal fault are
// separate events; order is frame, cancellations, terminal.
type Event struct {
	Frame        *Frame
	Cancellation *Cancellation
	Terminal     *Terminal
	dispatch     func() // internal supervisor work; never delivered to Handler
}

type SendResult uint8

const (
	Sent SendResult = iota
	Dropped
)

// Stats is a copy. Counters aggregate all diagnostics without per-record logging.
type Stats struct {
	Received, Sent, Refused, Suppressed uint64
	Diagnostics                         map[string]uint64
}

// Descriptor is implemented on supported platforms. The mutex also prevents a
// delayed Claim from taking an fd already released after callback completion.
type Descriptor struct {
	mu        sync.Mutex
	fd        int
	available bool
}

// CompatibleVersion implements control-plane §3.1: stable majors accept newer
// minor additions; pre-1.0 versions require an exact pair.
func CompatibleVersion(major, minor uint32) bool {
	return major == ProtocolMajor && (major != 0 || minor == ProtocolMinor)
}
