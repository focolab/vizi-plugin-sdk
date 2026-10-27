package doorbell

import (
	"encoding/binary"
	"errors"
	"hash/crc32"
	"math"
	"strings"
	"unicode/utf8"
)

var le = binary.LittleEndian

func checksum(r *RawRecord)      { le.PutUint32(r[108:], crc32.ChecksumIEEE(r[:108])) }
func header(r *RawRecord) Header { return Header{r[0], r[1], r[2], r[3], le.Uint32(r[4:8])} }
func blank(op byte, id uint32) RawRecord {
	var r RawRecord
	r[0], r[1] = op, Version
	le.PutUint32(r[4:8], id)
	return r
}
func zero(b []byte) bool {
	for _, v := range b {
		if v != 0 {
			return false
		}
	}
	return true
}
func diag(f *Frame, code string, count, expected, observed uint64) {
	f.Diagnostics = append(f.Diagnostics, Diagnostic{code, count, expected, observed})
}
func reject(f *Frame, where, route string) {
	f.Disposition = Rejected
	f.Violation = &Violation{Where: where, Route: route}
	if route == "grant_ack" {
		f.Violation.Class = "malformed"
	}
}

// EncodeNotification creates canonical wire bytes. ID allocation belongs to the
// serialized writer; normal plugins should call Transport.Notify instead.
func EncodeNotification(n Notification, id uint32) (RawRecord, error) {
	if n.SegmentID == 0 || (n.Epoch == 0 && n.ViewID != 0) {
		return RawRecord{}, errors.New("invalid notification identity or pane association")
	}
	r := blank(OpNotification, id)
	p := r[8:108]
	le.PutUint64(p, n.SegmentID)
	copy(p[8:72], n.Mask[:])
	le.PutUint32(p[72:76], n.Epoch)
	p[76] = n.ViewID
	checksum(&r)
	return r, nil
}

// EncodeMemory encodes a host grant for probes. Empty Name selects the Unix
// layout; a nonempty Name selects a Windows section and must be 1–80 UTF-8 bytes
// without NUL. Section creation/namespace/entropy policy belongs to the host.
// The plugin transport never sends M.
func EncodeMemory(m Memory, id uint32) (RawRecord, error) {
	if m.SegmentID == 0 || m.Size < 32 || m.Permissions&PluginRead == 0 || m.Permissions&0xf0 != 0 {
		return RawRecord{}, errors.New("invalid memory grant")
	}
	r := blank(OpMemory, id)
	p := r[8:108]
	le.PutUint64(p, m.SegmentID)
	le.PutUint64(p[8:], m.Size)
	p[16] = byte(m.Permissions)
	if m.Name != "" {
		if len(m.Name) > 80 || !utf8.ValidString(m.Name) || strings.ContainsRune(m.Name, 0) {
			return RawRecord{}, errors.New("invalid memory section name")
		}
		p[18] = byte(len(m.Name))
		copy(p[19:], m.Name)
	}
	checksum(&r)
	return r, nil
}

func decodeInput(p []byte) Input {
	e := Input{
		Generation: le.Uint32(p), Time: le.Uint32(p[4:]),
		Interaction: le.Uint32(p[8:]), Sequence: le.Uint32(p[12:]),
		Epoch: le.Uint32(p[16:]), Tool: le.Uint32(p[28:]),
		Channels: le.Uint16(p[32:]), Phase: Phase(p[34]),
		ViewID: p[35], Level: p[36], Modifiers: p[37], ClickCount: p[38],
		Target:   Target{TargetKind(p[39]), le.Uint32(p[20:]), le.Uint32(p[24:])},
		Position: Position{Kind: PositionKind(p[40]), Flags: p[41]},
		Action:   le.Uint16(p[42:44]),
	}
	for i := range e.Position.Values {
		e.Position.Values[i] = math.Float32frombits(le.Uint32(p[44+4*i:]))
	}
	return e
}

func positionSize(k PositionKind) int {
	switch k {
	case None:
		return 0
	case Voxel, Picked:
		return 12
	case ColumnYX, ColumnZX, ColumnZY:
		return 16
	case Ray:
		return 32
	case RayAndPicked:
		return 48
	}
	return -1
}

func positionViolation(e Input, p []byte) string {
	n := positionSize(e.Position.Kind)
	if n < 0 {
		return ""
	}
	if !zero(p[44+n : 92]) {
		return "position_tail"
	}
	finiteCount := n / 4
	if e.Position.Kind == RayAndPicked {
		finiteCount-- // back_frac alone permits NaN, meaning a point pick.
		back := e.Position.Values[11]
		if !math.IsNaN(float64(back)) && (back < 0 || back > 1) {
			return "nonfinite_position"
		}
	}
	for _, v := range e.Position.Values[:finiteCount] {
		if math.IsNaN(float64(v)) || math.IsInf(float64(v), 0) {
			return "nonfinite_position"
		}
	}
	if e.Position.Kind == Ray || e.Position.Kind == RayAndPicked {
		v := e.Position.Values
		if v[6] < 0 || v[6] > 1 || (v[0] == v[3] && v[1] == v[4] && v[2] == v[5]) {
			return "nonfinite_position"
		}
	}
	if e.Position.Flags&Miss != 0 && (e.Position.Kind == Picked || e.Position.Kind == RayAndPicked) {
		return "miss_with_picked"
	}
	return ""
}
func targetValid(t Target) bool {
	return t.Kind > Handle || (t.Kind == NoTarget && t.ID == 0 && t.Part == 0) || (t.Kind != NoTarget && t.ID != 0)
}

// EncodeInput encodes record-local, valid host input. It does not assert that
// arrangements exist or that interaction sequencing is valid, and cannot send E.
func EncodeInput(e Input, id uint32) (RawRecord, error) {
	if e.Phase > Fire || e.Interaction == 0 || e.Tool == 0 || e.Generation == 0 || e.Epoch == 0 ||
		(e.Phase.opening() && e.Sequence != 0) || e.Modifiers&0xf0 != 0 || e.Position.Flags&0xfc != 0 ||
		(e.Phase != Fire && e.Action != 0) ||
		positionSize(e.Position.Kind) < 0 || e.Target.Kind > Handle || !targetValid(e.Target) {
		return RawRecord{}, errors.New("invalid input fields")
	}
	r := blank(OpInput, id)
	p := r[8:108]
	for i, v := range []uint32{e.Generation, e.Time, e.Interaction, e.Sequence, e.Epoch, e.Target.ID, e.Target.Part, e.Tool} {
		le.PutUint32(p[4*i:], v)
	}
	le.PutUint16(p[32:], e.Channels)
	copy(p[34:42], []byte{byte(e.Phase), e.ViewID, e.Level, e.Modifiers, e.ClickCount, byte(e.Target.Kind), byte(e.Position.Kind), e.Position.Flags})
	le.PutUint16(p[42:44], e.Action)
	for i, v := range e.Position.Values {
		le.PutUint32(p[44+4*i:], math.Float32bits(v))
	}
	if why := positionViolation(e, p); why != "" {
		return RawRecord{}, errors.New(why)
	}
	checksum(&r)
	return r, nil
}

// decoder contains only framing state, independent of sockets and coordination.
type decoder struct {
	expected uint32
	ordinal  uint64
	windows  bool // Platform capability rules, independent of stream assembly.
}

func newDecoder() decoder { return decoder{expected: 1} }
func (d *decoder) decode(data []byte, fdCount int, ctrunc bool) (*Frame, *Fault) {
	d.ordinal++
	n := len(data)
	fault := func(class string) *Fault {
		f := &Fault{
			Class: class, Capture: append([]byte{}, data...), ObservedLength: &n,
			Ordinal: ptr(d.ordinal), ControlTruncated: &ctrunc, Direction: "host_to_plugin",
		}
		if n >= RecordSize {
			f.ExpectedCRC = ptr(crc32.ChecksumIEEE(data[:108]))
			f.ObservedCRC = ptr(le.Uint32(data[108:112]))
			if *f.ExpectedCRC == *f.ObservedCRC {
				var raw RawRecord
				copy(raw[:], data)
				f.Header = ptr(header(&raw))
			}
		}
		return f
	}
	var f *Frame
	if n == RecordSize {
		f = &Frame{Ordinal: d.ordinal, Disposition: FramingRejected, HasRecord: true}
		copy(f.Raw[:], data)
	}
	if ctrunc {
		return f, fault("ctrunc")
	}
	if fdCount > 1 || (d.windows && fdCount != 0) {
		return f, fault("wrong_fd_count")
	}
	if n != RecordSize {
		return f, fault("bad_length")
	}
	if crc32.ChecksumIEEE(data[:108]) != le.Uint32(data[108:]) {
		return f, fault("bad_crc")
	}
	f.Header = header(&f.Raw)
	f.HasHeader = true
	if f.Header.Version != Version {
		return f, fault("bad_ver")
	}
	if f.Header.Flags != 0 {
		return f, fault("bad_flags")
	}
	if f.Header.Reserved != 0 {
		return f, fault("bad_reserved")
	}
	delta := f.Header.ID - d.expected
	if delta < 1<<31 {
		if delta != 0 {
			diag(f, "id_gap", uint64(delta), uint64(d.expected), uint64(f.Header.ID))
		}
		d.expected = f.Header.ID + 1
	} else {
		diag(f, "id_regression", 1, uint64(d.expected), uint64(f.Header.ID))
	}
	op := f.Header.Op
	if op == 0xff {
		return f, fault("reserved_op")
	}
	if op != OpMemory && op != OpInput && op != OpNotification {
		f.Disposition = UnknownOpcode
		diag(f, "unknown_opcode", 1, 0, uint64(op))
		return f, nil
	}
	want := 0
	if op == OpMemory && !d.windows {
		want = 1
	}
	if fdCount != want {
		return f, fault("wrong_fd_count")
	}
	p := f.Raw[8:108]
	if (op == OpMemory && p[17] != 0) ||
		(op == OpInput && !zero(p[92:100])) ||
		(op == OpNotification && !zero(p[77:100])) {
		return f, fault("bad_padding")
	}
	f.Disposition = Decoded
	switch op {
	case OpMemory:
		m := &Memory{SegmentID: le.Uint64(p), Size: le.Uint64(p[8:]), Permissions: Permissions(p[16])}
		f.Memory = m
		if !d.windows && !zero(p[18:]) {
			diag(f, "unix_name_ignored", 1, 0, uint64(p[18]))
		}
		switch {
		case m.SegmentID == 0:
			reject(f, "zero_id", "contract_violation")
		case m.Size < 32:
			reject(f, "size_below_preamble", "grant_ack")
		case m.Permissions&PluginRead == 0:
			reject(f, "plugin_read_required", "grant_ack")
		case m.Permissions&0xf0 != 0:
			reject(f, "reserved_permissions", "grant_ack")
		}
		if d.windows && f.Disposition == Decoded {
			name, why := decodeSectionName(p)
			if why != "" {
				reject(f, why, "grant_ack")
			} else {
				m.Name = name
			}
		}
	case OpNotification:
		a := &Notification{SegmentID: le.Uint64(p), Epoch: le.Uint32(p[72:]), ViewID: p[76]}
		copy(a.Mask[:], p[8:72])
		f.Notification = a
		if a.Epoch == 0 && a.ViewID != 0 {
			diag(f, "view_without_epoch", 1, 0, uint64(a.ViewID))
			a.ViewID = 0
		}
		if a.SegmentID == 0 {
			reject(f, "zero_id", "contract_violation")
		} else if a.Mask == (Mask{}) {
			f.Disposition = Ignored
			diag(f, "empty_mask", 1, 0, 0)
		}
	case OpInput:
		e := decodeInput(p)
		f.Input = &e
	}
	return f, nil
}
func ptr[T any](v T) *T { return &v }

func decodeSectionName(p []byte) (string, string) {
	n := int(p[18])
	// Validate the bound and terminator before slicing a peer-sized field.
	if n > 80 {
		return "", "name_too_long"
	}
	if p[19+n] != 0 {
		return "", "name_terminator"
	}
	if n == 0 {
		return "", "empty_name"
	}
	name := p[19 : 19+n]
	if !zero(p[20+n:]) {
		return "", "name_tail"
	}
	if strings.IndexByte(string(name), 0) >= 0 {
		return "", "name_contains_nul"
	}
	if !utf8.Valid(name) {
		return "", "name_utf8"
	}
	return string(name), ""
}
