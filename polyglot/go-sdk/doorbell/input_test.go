package doorbell

import "testing"

func state() inputState {
	s := newInputState()
	s.active[1] = true
	s.active[2] = true
	s.highTool = 2
	s.epochs[0] = 7
	s.epochs[1] = 7
	return s
}
func apply(t *testing.T, s *inputState, r RawRecord) (*Frame, []Event) {
	t.Helper()
	d := newDecoder()
	f, err := d.decode(r[:], 0, false)
	if err != nil {
		t.Fatal(err)
	}
	return f, s.apply(f)
}

func TestInputSequencing(t *testing.T) {
	s := state()
	e := opening()
	r := inputRecord(t, e, 1)
	f, out := apply(t, &s, r)
	if f.Disposition != Decoded || len(out) != 0 || len(s.open) != 1 {
		t.Fatal(f, out)
	}
	f, out = apply(t, &s, r)
	if f.Disposition != Ignored || len(out) != 0 || len(s.open) != 1 {
		t.Fatal("exact duplicate disturbed gesture")
	}
	e.Phase = Drag
	e.Sequence = 3
	f, _ = apply(t, &s, inputRecord(t, e, 2))
	if diagnostic(f, "sequence_gap") != 2 {
		t.Fatal(f.Diagnostics)
	}
	f, _ = apply(t, &s, inputRecord(t, e, 3))
	if f.Disposition != Ignored {
		t.Fatal("seq duplicate")
	}
	e.Phase = Release
	e.Sequence = 4
	f, _ = apply(t, &s, inputRecord(t, e, 4))
	if f.Disposition != Decoded || len(s.open) != 0 {
		t.Fatal(f)
	}
	e.Phase = Cancel
	e.Sequence = 5
	f, out = apply(t, &s, inputRecord(t, e, 5))
	if f.Disposition != Ignored || len(out) != 0 {
		t.Fatal("late cancel")
	}
	e = opening()
	e.Interaction = 2
	e.Phase = Fire
	r = inputRecord(t, e, 6)
	apply(t, &s, r)
	f, _ = apply(t, &s, r)
	if s.watermark != 2 || len(s.open) != 0 || f.Disposition != Ignored {
		t.Fatal("fire dedup/state")
	}
}

func TestActionFireInsideOpenGesture(t *testing.T) {
	for _, action := range []uint16{1, 0xffff} {
		s := state()
		press := opening()
		fire := press
		fire.Phase, fire.Interaction, fire.Action = Fire, 2, action
		drag := press
		drag.Phase, drag.Sequence = Drag, 1
		release := drag
		release.Phase, release.Sequence = Release, 2
		d := newDecoder()
		for i, in := range []Input{press, fire, drag, release} {
			r := inputRecord(t, in, uint32(i+1))
			f, fault := d.decode(r[:], 0, false)
			if fault != nil {
				t.Fatal(fault)
			}
			out := s.apply(f)
			if f.Disposition != Decoded || f.Violation != nil || len(f.Diagnostics) != 0 || f.ImplicitlyClosed != 0 || len(out) != 0 || f.Input.Action != in.Action {
				t.Fatalf("action %d step %d: frame=%+v outcomes=%+v", action, i, f, out)
			}
			if i < 3 && (len(s.open) != 1 || s.byTool[press.Tool] != press.Interaction) {
				t.Fatalf("step %d disturbed gesture: %+v", i, s)
			}
		}
		if len(s.open) != 0 || s.watermark != 2 {
			t.Fatalf("release/watermark: %+v", s)
		}
	}
}

func TestConcurrentToolsAndImplicitClose(t *testing.T) {
	s := state()
	a := opening()
	apply(t, &s, inputRecord(t, a, 1))
	b := a
	b.Tool = 2
	b.Interaction = 2
	apply(t, &s, inputRecord(t, b, 2))
	a.Interaction = 3
	f, out := apply(t, &s, inputRecord(t, a, 3))
	if f.ImplicitlyClosed != 1 || len(out) != 0 || len(s.open) != 2 {
		t.Fatal(f, out)
	}
	a.Phase = Drag
	a.Sequence = 1
	a.Tool = 2
	f, out = apply(t, &s, inputRecord(t, a, 4))
	if f.Violation.Where != "interaction_mismatch" || len(out) != 1 || out[0].Cancellation.Opening.Interaction != 3 || s.byTool[2] != 2 {
		t.Fatal(f, out)
	}
}

func TestRejectedInputCancellation(t *testing.T) {
	for _, tc := range []struct {
		name   string
		phase  Phase
		mutate func(*RawRecord)
		why    string
		cancel bool
	}{
		{"terminal tail", Release, func(r *RawRecord) { r[64] = 1 }, "position_tail", true},
		{"zero interaction", Release, func(r *RawRecord) { le.PutUint32(r[16:], 0) }, "zero_id", true},
		{"unknown nonzero", Release, func(r *RawRecord) { le.PutUint32(r[16:], 999); le.PutUint32(r[8:], 0) }, "zero_id", false},
		{"bad press", Press, func(r *RawRecord) { le.PutUint32(r[16:], 2); le.PutUint32(r[20:], 1) }, "seq_on_open", true},
		{"bad fire", Fire, func(r *RawRecord) { le.PutUint32(r[16:], 2); le.PutUint32(r[20:], 1) }, "seq_on_open", false},
		{"future epoch", Release, func(r *RawRecord) { le.PutUint32(r[24:], 8) }, "epoch_mismatch", true},
		{"target", Release, func(r *RawRecord) { le.PutUint32(r[32:], 1) }, "invalid_target", true},
		{"nonfinite", Release, func(r *RawRecord) { le.PutUint32(r[52:], 0x7f800000) }, "nonfinite_position", true},
		{"miss picked", Release, func(r *RawRecord) { r[48] = byte(Picked); r[49] = Miss }, "miss_with_picked", true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			s := state()
			e := opening()
			apply(t, &s, inputRecord(t, e, 1))
			e.Phase = tc.phase
			if !e.Phase.opening() {
				e.Sequence = 1
			}
			r := inputRecord(t, e, 2)
			tc.mutate(&r)
			checksum(&r)
			f, out := apply(t, &s, r)
			if f.Violation == nil || f.Violation.Where != tc.why || ((len(out) == 1) != tc.cancel) {
				t.Fatalf("%+v %+v", f, out)
			}
			if s.watermark != 1 {
				t.Fatal("rejection advanced watermark")
			}
		})
	}
}

func TestInputPrecedenceAndExtensions(t *testing.T) {
	s := state()
	e := opening()
	r := inputRecord(t, e, 1)
	apply(t, &s, r)
	// A changed live replay is not an innocuous watermark duplicate.
	r[46] = 2
	checksum(&r)
	f, out := apply(t, &s, r)
	if f.Violation.Where != "interaction_mismatch" || len(out) != 1 {
		t.Fatal(f, out)
	}
	// A closed id is still a duplicate even if its record is malformed.
	r[8] = 0
	checksum(&r)
	f, out = apply(t, &s, r)
	if f.Disposition != Ignored || len(out) != 0 {
		t.Fatal(f, out)
	}
	e.Interaction = 2
	r = inputRecord(t, e, 2)
	r[42] = 99
	r[8] = 0
	checksum(&r)
	f, _ = apply(t, &s, r)
	if diagnostic(f, "unknown_phase") != 1 || f.Violation != nil {
		t.Fatal(f)
	}
	r = inputRecord(t, e, 3)
	r[48] = 99
	for i := 52; i < 100; i++ {
		r[i] = 255
	}
	r[47] = 99
	r[45] = 0xff
	r[49] = 0xff
	checksum(&r)
	f, _ = apply(t, &s, r)
	if f.Disposition != Decoded || f.Input.Position.Kind != None || f.Input.Target != (Target{}) || f.Input.Modifiers != 15 {
		t.Fatal(f)
	}
	// Stale PRESS does not close a current gesture or advance watermark.
	e.Interaction = 3
	e.Epoch = 6
	f, out = apply(t, &s, inputRecord(t, e, 4))
	if diagnostic(f, "stale_epoch") != 1 || len(out) != 0 || s.byTool[1] != 2 || s.watermark != 2 {
		t.Fatal(f, out)
	}
	// Retired tools differ from a handle never registered.
	delete(s.active, 2)
	e.Epoch = 7
	e.Tool = 2
	f, _ = apply(t, &s, inputRecord(t, e, 5))
	if diagnostic(f, "retired_tool") != 1 {
		t.Fatal(f)
	}
	e.Tool = 3
	f, _ = apply(t, &s, inputRecord(t, e, 6))
	if f.Violation.Where != "unknown_tool" {
		t.Fatal(f)
	}
}

func FuzzInputState(f *testing.F) {
	r, _ := EncodeInput(opening(), 1)
	f.Add(r[:])
	f.Fuzz(func(t *testing.T, b []byte) {
		if len(b) != 112 {
			return
		}
		var r RawRecord
		copy(r[:], b)
		r[0] = 'E'
		r[1] = 2
		r[2] = 0
		r[3] = 0
		clear(r[50:52])
		clear(r[100:108])
		checksum(&r)
		s := state()
		d := newDecoder()
		frame, fault := d.decode(r[:], 0, false)
		if fault != nil {
			t.Fatal(fault)
		}
		s.apply(frame)
		s.apply(frame)
		if len(s.open) > len(s.active) {
			t.Fatal("unbounded state")
		}
	})
}
