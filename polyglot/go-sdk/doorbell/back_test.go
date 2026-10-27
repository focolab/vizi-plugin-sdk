package doorbell

import (
	"math"
	"testing"
)

func pickedPosition(back float32) Position {
	return Position{Kind: RayAndPicked, Values: [12]float32{1, 2, 3, 9, 18, 27, 0.25, 0, 3, 6, 9, back}}
}

func TestPickedBackFractionWireAndAccessors(t *testing.T) {
	for _, tc := range []struct {
		name string
		frac float32
		back [3]float64
	}{
		// The picked front is a quarter of the way along the ray, at (3, 6, 9).
		// The back is measured from it: 0 is the front itself, and 0.75 is
		// three quarters of the three quarters that are left.
		{"no depth", 0, [3]float64{3, 6, 9}},
		{"extent", 0.75, [3]float64{7.5, 15, 22.5}},
		{"far", 1, [3]float64{9, 18, 27}},
		{"point", math.Float32frombits(0x7fc00001), [3]float64{}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			e := opening()
			e.Position = pickedPosition(tc.frac)
			r := inputRecord(t, e, 1)
			// back_frac occupies payload [88..92); only [92..100) is padding.
			if le.Uint32(r[96:100]) != math.Float32bits(tc.frac) || !zero(r[100:108]) {
				t.Fatalf("back/padding: %x", r[96:108])
			}
			s := state()
			f, out := apply(t, &s, r)
			if f.Disposition != Decoded || f.Violation != nil || len(out) != 0 {
				t.Fatal(f, out)
			}
			p := f.Input.Position
			fraction, picked := p.BackFraction()
			point := math.IsNaN(float64(tc.frac))
			if !picked || math.Float32bits(fraction) != math.Float32bits(tc.frac) || p.IsPoint() != point {
				t.Fatal(fraction, picked, p.IsPoint())
			}
			if back, ok := p.Back(); ok != !point || back != tc.back {
				t.Fatal(back, ok)
			}
			// The front is the voxel's, whether or not anything stood out.
			if frac, ok := p.FrontFraction(); !ok || frac != 0.25 {
				t.Fatal(frac, ok)
			}
			if front, ok := p.Front(); !ok || front != ([3]float64{3, 6, 9}) {
				t.Fatal(front, ok)
			}
			if pick, voxel, ok := f.Input.Pick(); !ok || pick.Kind != RayAndPicked || voxel != ([3]float32{3, 6, 9}) {
				t.Fatal(pick, voxel, ok)
			}
			if encoded := inputRecord(t, *f.Input, 1); encoded != r {
				t.Fatal("round trip changed wire bytes")
			}
			if repeated, _ := apply(t, &s, r); repeated.Disposition != Ignored || repeated.Violation != nil {
				t.Fatal("identical opening replay was rejected", repeated)
			}
		})
	}
	for kind := None; kind < RayAndPicked; kind++ {
		p := pickedPosition(float32(math.NaN()))
		p.Kind = kind
		if fraction, ok := p.BackFraction(); ok || fraction != 0 || p.IsPoint() {
			t.Fatal("other kind reported a ray pick", kind)
		}
		if back, ok := p.Back(); ok || back != ([3]float64{}) {
			t.Fatal("other kind reported a back", kind, back)
		}
		if front, ok := p.Front(); ok || front != ([3]float64{}) {
			t.Fatal("other kind reported a front", kind, front)
		}
		if frac, ok := p.FrontFraction(); ok || frac != 0 {
			t.Fatal("other kind reported a front fraction", kind, frac)
		}
	}
	// Opposite finite f32 endpoints must not overflow the derived point.
	p := pickedPosition(0.5)
	p.Values[0], p.Values[3] = -math.MaxFloat32, math.MaxFloat32
	if back, ok := p.Back(); !ok || math.IsInf(back[0], 0) || math.Abs(back[0]-math.MaxFloat32/2) > 1e30 {
		t.Fatal(back, ok)
	}
}

func pick(camera, far, picked [3]float32, back float32) Position {
	return Position{Kind: RayAndPicked, Values: [12]float32{
		camera[0], camera[1], camera[2], far[0], far[1], far[2], 0, 0, picked[0], picked[1], picked[2], back}}
}

func near(t *testing.T, got [3]float64, ok bool, want [3]float64) {
	t.Helper()
	for i := range got {
		if !ok || math.Abs(got[i]-want[i]) > 1e-5 {
			t.Fatal(got, ok, want)
		}
	}
}

// back = front + back_frac * (far - front).
func TestBackIsMeasuredFromThePickedFront(t *testing.T) {
	// A host-measured vector: a front at t = 0.2 and a back at t = 0.5,
	// encoded as 0.3 / 0.8.
	host := pick([3]float32{1, 2, 3}, [3]float32{9, 6, 43}, [3]float32{2.6, 2.8, 11}, 0.375)
	if frac, ok := host.FrontFraction(); !ok || math.Abs(frac-0.2) > 1e-6 {
		t.Fatal(frac, ok)
	}
	front, ok := host.Front()
	near(t, front, ok, [3]float64{2.6, 2.8, 11})
	back, ok := host.Back()
	near(t, back, ok, [3]float64{5, 4, 23})

	camera, far := [3]float32{}, [3]float32{0, 0, 10}
	// Zero is NOT a point: a real pick of no depth, its back at its front and
	// not at the camera -- what a sender that predates the field leaves.
	zeros := pick(camera, far, [3]float32{0, 0, 4}, 0)
	front, _ = zeros.Front()
	back, ok = zeros.Back()
	if zeros.IsPoint() || !ok || back != front || back != ([3]float64{0, 0, 4}) {
		t.Fatal(zeros.IsPoint(), back, front, ok)
	}
	// NaN is the other answer: nothing stood out, and there is no back.
	nothing := pick(camera, far, [3]float32{0, 0, 4}, float32(math.NaN()))
	if _, ok = nothing.Back(); !nothing.IsPoint() || ok {
		t.Fatal("NaN must be a point with no back")
	}
	if front, ok = nothing.Front(); !ok || front != ([3]float64{0, 0, 4}) {
		t.Fatal(front, ok)
	}

	// A voxel center off the ray is projected onto it.
	off := pick(camera, far, [3]float32{0, 3, 2}, 0.5)
	front, ok = off.Front()
	near(t, front, ok, [3]float64{0, 0, 2})
	back, ok = off.Back()
	near(t, back, ok, [3]float64{0, 0, 6})
	if back, ok = pick(camera, far, [3]float32{0, 0, 2}, 1).Back(); !ok || back != ([3]float64{0, 0, 10}) {
		t.Fatal(back, ok)
	}
	// The front is clamped to the segment; at far no ray is left.
	behind := pick(camera, far, [3]float32{0, 0, -3}, 0.5)
	if frac, _ := behind.FrontFraction(); frac != 0 {
		t.Fatal(frac)
	}
	if back, _ = behind.Back(); back != ([3]float64{0, 0, 5}) {
		t.Fatal(back)
	}
	past := pick(camera, far, [3]float32{0, 0, 12}, 0.5)
	if frac, _ := past.FrontFraction(); frac != 1 {
		t.Fatal(frac)
	}
	if back, _ = past.Back(); back != ([3]float64{0, 0, 10}) {
		t.Fatal(back)
	}
	// camera == far has no direction: the front fraction is 0. (The receiver
	// refuses such a record; the accessor still answers.)
	one := [3]float32{1, 1, 1}
	flat := pick(one, one, [3]float32{}, 0.5)
	frac, ok := flat.FrontFraction()
	back, _ = flat.Back()
	if !ok || frac != 0 || back != ([3]float64{1, 1, 1}) {
		t.Fatal(frac, ok, back)
	}
}

// A tool takes either record whichever it declared.
func TestRayAndPickReadEitherKind(t *testing.T) {
	values := [12]float32{0, 0, 0, 10, 0, 0, 0.25, 1.5}
	bare := Input{Position: Position{Kind: Ray, Values: values}}
	values[8], values[11] = 5, 0.5
	picked := Input{Position: Position{Kind: RayAndPicked, Values: values}}
	voxel := Input{Position: Position{Kind: Voxel, Values: [12]float32{1, 2, 3}}}

	c1, f1, e1, r1, ok1 := bare.Ray()
	c2, f2, e2, r2, ok2 := picked.Ray()
	if !ok1 || !ok2 || c1 != c2 || f1 != f2 || e1 != e2 || r1 != r2 || f1 != ([3]float32{10, 0, 0}) || e1 != 0.25 || r1 != 1.5 {
		t.Fatal("the ray differs between the two kinds")
	}
	if _, _, ok := bare.Pick(); ok {
		t.Fatal("a bare ray has no pick")
	}
	pick, at, ok := picked.Pick()
	if !ok || at != ([3]float32{5, 0, 0}) {
		t.Fatal(at, ok)
	}
	if back, ok := pick.Back(); !ok || back != ([3]float64{7.5, 0, 0}) {
		t.Fatal(back, ok)
	}
	if _, _, _, _, ok := voxel.Ray(); ok {
		t.Fatal("a voxel is not a ray")
	}
	if _, _, ok := voxel.Pick(); ok {
		t.Fatal("a voxel has no pick")
	}
}

func TestPickedBackFractionValidation(t *testing.T) {
	for _, fraction := range []float32{-0.5, 1.25, float32(math.Inf(1)), float32(math.Inf(-1))} {
		e := opening()
		e.Position = pickedPosition(fraction)
		if _, err := EncodeInput(e, 1); err == nil || err.Error() != "nonfinite_position" {
			t.Fatal("encoded invalid back fraction", fraction, err)
		}
		e.Position = pickedPosition(0.5)
		r := inputRecord(t, e, 1)
		le.PutUint32(r[96:100], math.Float32bits(fraction))
		checksum(&r)
		s := state()
		f, _ := apply(t, &s, r)
		if f.Disposition != Rejected || f.Violation == nil || f.Violation.Where != "nonfinite_position" {
			t.Fatal("invalid back fraction was not a semantic rejection", fraction, f)
		}
	}
	// NaN's exception belongs only to back_frac, never to another ray/pick field.
	for i := 0; i < 11; i++ {
		e := opening()
		e.Position = pickedPosition(float32(math.NaN()))
		r := inputRecord(t, e, 1)
		le.PutUint32(r[52+4*i:], math.Float32bits(float32(math.NaN())))
		checksum(&r)
		s := state()
		f, _ := apply(t, &s, r)
		if f.Violation == nil || f.Violation.Where != "nonfinite_position" {
			t.Fatal("accepted NaN outside back_frac", i, f)
		}
	}
	for _, fraction := range []float32{0.5, float32(math.NaN())} {
		e := opening()
		e.Position = pickedPosition(fraction)
		r := inputRecord(t, e, 1)
		r[49] = Miss
		checksum(&r)
		s := state()
		f, _ := apply(t, &s, r)
		if f.Violation == nil || f.Violation.Where != "miss_with_picked" {
			t.Fatal("MISS carried a pick", f)
		}
	}
}

func TestRayPickIsIndependentOfPhase(t *testing.T) {
	for phase := Press; phase <= Fire; phase++ {
		for _, kind := range []PositionKind{Ray, RayAndPicked} {
			s := state()
			e := opening()
			if !phase.opening() {
				apply(t, &s, inputRecord(t, e, 1))
				e.Sequence = 1
			}
			e.Phase = phase
			e.Position = pickedPosition(0.75)
			e.Position.Kind = kind
			if kind == Ray {
				clear(e.Position.Values[8:])
			}
			f, out := apply(t, &s, inputRecord(t, e, 2))
			if f.Disposition != Decoded || f.Violation != nil || len(out) != 0 {
				t.Fatal("phase constrained position kind", phase, kind, f, out)
			}
		}
	}
}

func TestExtendedPositionTailIsNotFixedPadding(t *testing.T) {
	for kind := None; kind < RayAndPicked; kind++ {
		for offset := 96; offset < 100; offset++ {
			e := opening()
			e.Position = Position{Kind: kind}
			if kind == Ray {
				e.Position.Values[3] = 1
			}
			r := inputRecord(t, e, 1)
			r[offset] = 1
			checksum(&r)
			s := state()
			f, _ := apply(t, &s, r) // A framing fault fails here before apply.
			if f.Disposition != Rejected || f.Violation == nil || f.Violation.Where != "position_tail" {
				t.Fatal(kind, offset, f)
			}
		}
	}
}
