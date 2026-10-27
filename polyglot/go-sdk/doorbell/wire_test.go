package doorbell

import (
	"bytes"
	"hash/crc32"
	"math"
	"testing"
)

func note() Notification {
	var m Mask
	m.Set(0)
	m.Set(7)
	m.Set(8)
	m.Set(63)
	m.Set(64)
	m.Set(511)
	return Notification{SegmentID: 0x0102030405060708, Mask: m, Epoch: 0x11223344, ViewID: 0}
}
func notification(t *testing.T, id uint32) RawRecord {
	t.Helper()
	r, e := EncodeNotification(note(), id)
	if e != nil {
		t.Fatal(e)
	}
	return r
}
func opening() Input {
	return Input{Generation: 1, Interaction: 1, Epoch: 7, Tool: 1, Phase: Press, Position: Position{Kind: Voxel, Values: [12]float32{1.5, 2.5, 3.5}}}
}
func inputRecord(t *testing.T, e Input, id uint32) RawRecord {
	t.Helper()
	r, err := EncodeInput(e, id)
	if err != nil {
		t.Fatal(err)
	}
	return r
}
func diagnostic(f *Frame, code string) uint64 {
	for _, d := range f.Diagnostics {
		if d.Code == code {
			return d.Count
		}
	}
	return 0
}

func TestDocumentedLayout(t *testing.T) {
	if got := crc32.ChecksumIEEE([]byte("123456789")); got != 0xcbf43926 {
		t.Fatalf("CRC primitive %x", got)
	}
	r := notification(t, 0x12345678)
	if !bytes.Equal(r[:16], []byte{'D', 2, 0, 0, 0x78, 0x56, 0x34, 0x12, 8, 7, 6, 5, 4, 3, 2, 1}) {
		t.Fatalf("header/segment: %x", r[:16])
	}
	for i := 0; i < 64; i++ {
		want := byte(0)
		switch i {
		case 0:
			want = 0x81
		case 1, 8:
			want = 1
		case 7, 63:
			want = 0x80
		}
		if r[16+i] != want {
			t.Errorf("mask byte %d = %x", i, r[16+i])
		}
	}
	if !bytes.Equal(r[80:85], []byte{0x44, 0x33, 0x22, 0x11, 0}) || !zero(r[85:108]) {
		t.Fatalf("D epoch/padding: %x", r[80:108])
	}
	if note().Mask.Has(512) {
		t.Fatal("mask out of range")
	}
	m := Mask{}
	if m.Set(512) == nil {
		t.Fatal("accepted out of range")
	}
	e := opening()
	e.Time = 0x12345678
	e.Target = Target{ROI, 9, 10}
	e.Channels = 0x8001
	e.ViewID = 4
	e.Level = 5
	e.Modifiers = Shift | Meta
	e.ClickCount = 2
	r = inputRecord(t, e, 1)
	if le.Uint32(r[12:16]) != e.Time || le.Uint32(r[24:28]) != 7 || le.Uint32(r[28:32]) != 9 || le.Uint32(r[36:40]) != 1 || !bytes.Equal(r[40:50], []byte{1, 128, 0, 4, 5, 9, 2, 2, 1, 0}) {
		t.Fatalf("E field offsets: %x", r)
	}
	if le.Uint32(r[52:56]) != 0x3fc00000 || !zero(r[64:108]) {
		t.Fatalf("position/padding: %x", r[52:108])
	}
	mr, err := EncodeMemory(Memory{SegmentID: 0x100000001, Size: 4096, Permissions: PluginRead | HostWrite}, 9)
	if err != nil {
		t.Fatal(err)
	}
	if le.Uint64(mr[8:16]) != 0x100000001 || le.Uint64(mr[16:24]) != 4096 || mr[24] != 9 || !zero(mr[25:108]) {
		t.Fatalf("M offsets: %x", mr)
	}
}

func TestInputActionWireLayout(t *testing.T) {
	e := opening()
	e.Phase, e.Action = Fire, 0xabcd
	r := inputRecord(t, e, 1)
	if r[50] != 0xcd || r[51] != 0xab || !zero(r[100:108]) {
		t.Fatalf("action/padding offsets: %x", r)
	}
	d := newDecoder()
	f, fault := d.decode(r[:], 0, false)
	if fault != nil || f.Input == nil || *f.Input != e {
		t.Fatalf("action round trip: frame=%+v fault=%+v", f, fault)
	}
	for offset := 100; offset < 108; offset++ {
		bad := r
		bad[offset] = 1
		checksum(&bad)
		d := newDecoder()
		_, fault := d.decode(bad[:], 0, false)
		if fault == nil || fault.Class != "bad_padding" {
			t.Fatalf("padding at %d: %+v", offset, fault)
		}
	}
	for _, phase := range []Phase{Press, Drag, Release, Cancel} {
		e.Phase = phase
		if _, err := EncodeInput(e, 1); err == nil {
			t.Fatalf("encoded action on phase %d", phase)
		}
	}
}

func TestFramingValidation(t *testing.T) {
	for _, tc := range []struct {
		name   string
		mutate func(*RawRecord)
		count  int
		trunc  bool
		class  string
	}{
		{"crc", func(r *RawRecord) { r[1] = 99 }, 0, false, "bad_crc"},
		{"version", func(r *RawRecord) { r[1] = 1; checksum(r) }, 0, false, "bad_ver"},
		{"flags", func(r *RawRecord) { r[2] = 1; checksum(r) }, 0, false, "bad_flags"},
		{"reserved", func(r *RawRecord) { r[3] = 1; checksum(r) }, 0, false, "bad_reserved"},
		{"reserved op", func(r *RawRecord) { r[0] = 255; checksum(r) }, 0, false, "reserved_op"},
		{"padding", func(r *RawRecord) { r[85] = 1; checksum(r) }, 0, false, "bad_padding"},
		{"fd count", func(r *RawRecord) {}, 1, false, "wrong_fd_count"},
		{"unknown cap", func(r *RawRecord) { r[0] = 0x99; checksum(r) }, 2, false, "wrong_fd_count"},
		{"ancillary first", func(r *RawRecord) { r[0] = 0x99 }, 0, true, "ctrunc"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			d := newDecoder()
			r := notification(t, 1)
			tc.mutate(&r)
			f, err := d.decode(r[:], tc.count, tc.trunc)
			if err == nil || err.Class != tc.class {
				t.Fatalf("fault: %+v", err)
			}
			if f.Disposition != FramingRejected || err.Ordinal == nil || *err.Ordinal != 1 || err.ExpectedCRC == nil {
				t.Fatalf("missing fault facts: %+v", err)
			}
		})
	}
	for _, size := range []int{0, 1, 111, 113, 120} {
		d := newDecoder()
		_, f := d.decode(make([]byte, size), 0, false)
		if f.Class != "bad_length" || *f.ObservedLength != size || len(f.Capture) != size || f.Capture == nil {
			t.Fatalf("size %d: %+v", size, f)
		}
		if (f.ExpectedCRC != nil) != (size >= 112) {
			t.Fatalf("fabricated/missing CRC: %+v", f)
		}
	}
	d := newDecoder()
	marker := bytes.Repeat([]byte{255}, 112)
	_, f := d.decode(marker, 0, false)
	if f.Class != "bad_crc" {
		t.Fatalf("marker: %+v", f)
	}
}

func TestDiagnosticIDBoundary(t *testing.T) {
	d := newDecoder()
	for _, tc := range []struct {
		id       uint32
		gap, reg uint64
		expected uint32
	}{{3, 2, 0, 4}, {1, 0, 1, 4}, {4, 0, 0, 5}, {7, 2, 0, 8}} {
		r := notification(t, tc.id)
		f, err := d.decode(r[:], 0, false)
		if err != nil {
			t.Fatal(err)
		}
		if diagnostic(f, "id_gap") != tc.gap || diagnostic(f, "id_regression") != tc.reg || d.expected != tc.expected {
			t.Fatalf("id %d: %+v, next %d", tc.id, f.Diagnostics, d.expected)
		}
	}
	d.expected = math.MaxUint32
	for _, id := range []uint32{math.MaxUint32, 0, 1} {
		r := notification(t, id)
		f, _ := d.decode(r[:], 0, false)
		if len(f.Diagnostics) != 0 {
			t.Fatalf("wrap %d: %+v", id, f.Diagnostics)
		}
	}
	d = newDecoder()
	r := notification(t, 1)
	r[0] = 0x90
	r[85] = 0xff
	checksum(&r)
	f, err := d.decode(r[:], 1, false)
	if err != nil || f.Disposition != UnknownOpcode || d.expected != 2 {
		t.Fatal(f, err)
	}
	r = notification(t, 2)
	le.PutUint64(r[8:], 0)
	checksum(&r)
	f, err = d.decode(r[:], 0, false)
	if err != nil || f.Disposition != Rejected || d.expected != 3 {
		t.Fatal(f, err)
	}
	r = notification(t, 50)
	r[108] ^= 1
	d.decode(r[:], 0, false)
	if d.expected != 3 {
		t.Fatal("bad CRC fed id tracker")
	}
}

func TestMemoryAndNotificationSemantics(t *testing.T) {
	for _, tc := range []struct {
		name         string
		mutate       func(*RawRecord)
		where, route string
	}{
		{"zero id", func(r *RawRecord) { le.PutUint64(r[8:], 0) }, "zero_id", "contract_violation"},
		{"short size", func(r *RawRecord) { le.PutUint64(r[16:], 31) }, "size_below_preamble", "grant_ack"},
		{"write only", func(r *RawRecord) { r[24] = 2 }, "plugin_read_required", "grant_ack"},
		{"reserved permissions", func(r *RawRecord) { r[24] |= 128 }, "reserved_permissions", "grant_ack"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
			tc.mutate(&r)
			checksum(&r)
			d := newDecoder()
			f, err := d.decode(r[:], 1, false)
			if err != nil || f.Violation == nil || f.Violation.Where != tc.where || f.Violation.Route != tc.route {
				t.Fatal(f, err)
			}
		})
	}
	r, _ := EncodeMemory(Memory{SegmentID: 1, Size: 32, Permissions: PluginRead}, 1)
	for i := 26; i < 108; i++ {
		r[i] = 255
	}
	checksum(&r)
	d := newDecoder()
	f, err := d.decode(r[:], 1, false)
	if err != nil || f.Disposition != Decoded || diagnostic(f, "unix_name_ignored") != 1 {
		t.Fatal(f, err)
	}
	r[25] = 1
	checksum(&r)
	_, err = d.decode(r[:], 1, false)
	if err.Class != "bad_padding" {
		t.Fatal(err)
	}
	r = notification(t, 3)
	le.PutUint32(r[80:], 0)
	r[84] = 7
	checksum(&r)
	f, err = d.decode(r[:], 0, false)
	if err != nil || f.Disposition != Decoded || f.Notification.ViewID != 0 || diagnostic(f, "view_without_epoch") != 1 {
		t.Fatal(f, err)
	}
	r = notification(t, 4)
	clear(r[16:80])
	checksum(&r)
	f, err = d.decode(r[:], 0, false)
	if err != nil || f.Disposition != Ignored || diagnostic(f, "empty_mask") != 1 {
		t.Fatal(f, err)
	}
}

func TestPositionLayouts(t *testing.T) {
	for k := None; k <= RayAndPicked; k++ {
		t.Run(string(rune('0'+k)), func(t *testing.T) {
			e := opening()
			e.Position = Position{Kind: k}
			n := positionSize(k)
			for i := 0; i < n/4; i++ {
				e.Position.Values[i] = float32(i + 1)
			}
			if k == Ray || k == RayAndPicked {
				e.Position.Values[6] = 0.5
			}
			if k == RayAndPicked {
				e.Position.Values[11] = 0.75
			}
			r := inputRecord(t, e, 1)
			if got := decodeInput(r[8:108]); got != e {
				t.Fatalf("decode differs: %+v", got)
			}
			if n < 48 {
				r[52+n] = 1
				checksum(&r)
				if why := positionViolation(decodeInput(r[8:108]), r[8:108]); why != "position_tail" {
					t.Fatal(why)
				}
			}
			if n > 0 {
				r = inputRecord(t, e, 1)
				le.PutUint32(r[52:], 0x7fc00000)
				if why := positionViolation(decodeInput(r[8:108]), r[8:108]); why != "nonfinite_position" {
					t.Fatal(why)
				}
			}
		})
	}
}

func FuzzFrameDecode(f *testing.F) {
	r, _ := EncodeNotification(note(), 1)
	f.Add(r[:], uint8(0), false)
	f.Add([]byte{}, uint8(2), true)
	f.Fuzz(func(t *testing.T, b []byte, count uint8, truncated bool) {
		if len(b) > 120 {
			b = b[:120]
		}
		d := newDecoder()
		frame, fault := d.decode(b, int(count%4), truncated)
		if fault == nil && frame == nil {
			t.Fatal("no outcome")
		}
		if fault != nil && fault.Capture != nil && len(fault.Capture) != len(b) {
			t.Fatal("capture mismatch")
		}
	})
}
