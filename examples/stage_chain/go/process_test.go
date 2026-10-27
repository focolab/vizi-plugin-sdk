package main

import (
	"bytes"
	"context"
	"encoding/binary"
	"encoding/json"
	"errors"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
)

// Independent host header for offline borrowed-slot arithmetic tests.
func slot(t *testing.T, dtype pb.DType, alloc [3]uint32, channels []uint16, values ...[]float64) *stage.Slot {
	t.Helper()
	count := int(alloc[0]) * int(alloc[1]) * int(alloc[2])
	b := make([]byte, 320+4*count*8)
	le := binary.LittleEndian
	le.PutUint32(b[28:], uint32(dtype))
	le.PutUint32(b[32:], uint32(len(channels)))
	le.PutUint32(b[36:], 4)
	le.PutUint64(b[56:], uint64(len(b)-320))
	for axis, n := range alloc {
		le.PutUint32(b[40+4*axis:], n)
		le.PutUint32(b[68+8*axis:], n)
		le.PutUint64(b[112+32*axis:], math.Float64bits(1))
	}
	for i, c := range channels {
		le.PutUint16(b[256+2*i:], c)
	}
	s, err := stage.DecodeSlot(b)
	if err != nil {
		t.Fatal(err)
	}
	for p, vs := range values {
		plane, _ := s.Plane(channels[p])
		for i, v := range vs {
			put(plane, dtype, i, v)
		}
	}
	return s
}

func readPlane(t *testing.T, s *stage.Slot, c uint16) []float64 {
	t.Helper()
	b, err := s.Plane(c)
	if err != nil {
		t.Fatal(err)
	}
	vs := make([]float64, len(b)/int(stage.ItemSize(s.Layout.DType)))
	for i := range vs {
		vs[i] = value(b, s.Layout.DType, i)
	}
	return vs
}

func TestGainRoundingAndExtent(t *testing.T) {
	for _, tc := range []struct {
		dtype    pb.DType
		factor   float64
		in, want []float64
	}{
		{pb.DType_DTYPE_U16, 1.5, []float64{1, 3, 5, 7, 65535}, []float64{2, 4, 8, 10, 65535}},
		{pb.DType_DTYPE_U8, 0.5, []float64{1, 3, 5, 7, 255}, []float64{0, 2, 2, 4, 128}},
		{pb.DType_DTYPE_U8, 2, []float64{128}, []float64{255}},
		{pb.DType_DTYPE_F32, 1.5, []float64{0.5, -3, 1.25}, []float64{0.75, -4.5, 1.875}},
		{pb.DType_DTYPE_F64, 0.5, []float64{-5, 3}, []float64{-2.5, 1.5}},
	} {
		s := slot(t, tc.dtype, [3]uint32{1, 1, uint32(len(tc.in))}, []uint16{7}, tc.in)
		s.Params = stage.Params{"channel": int64(7), "factor": tc.factor, "stall_ms": int64(0)}
		if err := gain(context.Background(), nil, nil, s); err != nil {
			t.Fatal(err)
		}
		if got := readPlane(t, s, 7); !reflect.DeepEqual(got, tc.want) {
			t.Fatal(tc, got)
		}
	}
	s := slot(t, pb.DType_DTYPE_U8, [3]uint32{1, 1, 4}, []uint16{7}, []float64{10, 20, 30, 40})
	s.Layout.Extent[2] = [2]uint32{1, 3}
	s.Params = stage.Params{"channel": int64(7), "factor": 2.0, "stall_ms": int64(0)}
	if err := gain(context.Background(), nil, nil, s); err != nil {
		t.Fatal(err)
	}
	if got := readPlane(t, s, 7); !reflect.DeepEqual(got, []float64{10, 40, 60, 40}) {
		t.Fatal(got)
	}
}

func TestDifferenceAndZeroOutsideExtent(t *testing.T) {
	for _, dtype := range []pb.DType{pb.DType_DTYPE_U16, pb.DType_DTYPE_F32, pb.DType_DTYPE_F64} {
		for _, swap := range []bool{false, true} {
			s := slot(t, dtype, [3]uint32{1, 1, 4}, []uint16{7, 4}, []float64{50, 9, 2, 8}, []float64{1, 3, 8, 8})
			s.Layout.Extent[2] = [2]uint32{1, 4}
			// Spare space is deliberately dirty, so AddChannel must initialize it.
			for i := 8 * int(stage.ItemSize(dtype)); i < len(s.Data()); i++ {
				s.Data()[i] = 0x55
			}
			a, b := int64(7), int64(4)
			if swap {
				a, b = b, a
			}
			s.Params = stage.Params{"a": a, "b": b}
			before := bytes.Clone(s.Data()[:8*int(stage.ItemSize(dtype))])
			if err := diff(context.Background(), nil, &pb.StageArm{NewChannels: map[string]uint32{"absdiff": 9}}, s); err != nil {
				t.Fatal(err)
			}
			if got := readPlane(t, s, 9); !reflect.DeepEqual(got, []float64{0, 6, 6, 0}) {
				t.Fatal(got)
			}
			if !bytes.Equal(before, s.Data()[:len(before)]) {
				t.Fatal("changed input planes")
			}
		}
	}
	s := slot(t, pb.DType_DTYPE_U16, [3]uint32{1, 1, 2}, []uint16{7}, []float64{65535, 1})
	s.Params = stage.Params{"a": int64(7), "b": int64(7)}
	if err := diff(context.Background(), nil, &pb.StageArm{NewChannels: map[string]uint32{"absdiff": 9}}, s); err != nil {
		t.Fatal(err)
	}
	if got := readPlane(t, s, 9); !reflect.DeepEqual(got, []float64{0, 0}) {
		t.Fatal(got)
	}
}

func TestWidenWindowPlacement(t *testing.T) {
	for _, e := range [][3][2]uint32{{{0, 1}, {2, 4}, {1, 5}}, {{0, 1}, {0, 5}, {0, 5}}} {
		vs := make([]float64, 25)
		for i := range vs {
			vs[i] = float64(i)
		}
		s := slot(t, pb.DType_DTYPE_U16, [3]uint32{1, 5, 5}, []uint16{9}, vs)
		s.Layout.Extent = e
		s.Layout.Origin = [3]float64{5, 10, 20}
		s.Params = stage.Params{"crop": int64(1)}
		if err := widen(context.Background(), nil, nil, s); err != nil {
			t.Fatal(err)
		}
		if got := readPlane(t, s, 9); !reflect.DeepEqual(got, []float64{6, 7, 8, 11, 12, 13, 16, 17, 18}) {
			t.Fatal(got)
		}
		wantExtent := [3][2]uint32{{0, 1}, {1, 3}, {0, 3}}
		wantOrigin := [3]float64{5, 10, 20}
		if e[1][0] == 0 {
			wantExtent = [3][2]uint32{{0, 1}, {0, 3}, {0, 3}}
			wantOrigin = [3]float64{5, 11, 21}
		}
		if s.Layout.Extent != wantExtent || s.Layout.Origin != wantOrigin || s.Layout.DType != pb.DType_DTYPE_F32 {
			t.Fatal(s.Layout)
		}
	}
}

func TestDifferenceFloat32FractionAndRounding(t *testing.T) {
	s := slot(t, pb.DType_DTYPE_F32, [3]uint32{1, 1, 2}, []uint16{7, 4},
		[]float64{0.25, 16777216}, []float64{-0.125, -1})
	s.Params = stage.Params{"a": int64(7), "b": int64(4)}
	if err := diff(context.Background(), nil, &pb.StageArm{NewChannels: map[string]uint32{"absdiff": 9}}, s); err != nil {
		t.Fatal(err)
	}
	if got := readPlane(t, s, 9); !reflect.DeepEqual(got, []float64{0.375, 16777216}) {
		t.Fatal(got)
	}
}

func TestDriftCausalityReferencesAndArms(t *testing.T) {
	d := driftState{arms: make(map[armKey]history)}
	a := &pb.StageArm{Pipeline: 1, Position: 2}
	b := &pb.StageArm{Pipeline: 1, Position: 3}
	makeSlot := func(time uint32, values []float64) *stage.Slot {
		s := slot(t, pb.DType_DTYPE_U16, [3]uint32{1, 1, 4}, []uint16{7}, values)
		s.Layout.Extent[2] = [2]uint32{1, 4}
		s.Layout.Origin = [3]float64{5, 10, 20}
		s.Timepoint = time
		s.Params = stage.Params{"channel": int64(7)}
		return s
	}
	run := func(a *pb.StageArm, s *stage.Slot) {
		t.Helper()
		if err := d.check(context.Background(), nil, a, s.Info()); err != nil {
			t.Fatal(err)
		}
		if err := d.process(context.Background(), nil, a, s); err != nil {
			t.Fatal(err)
		}
	}
	run(a, makeSlot(0, []float64{999, 1, 3, 0})) // centroid x = 20.75
	run(b, makeSlot(5, []float64{999, 0, 0, 0})) // no reference, independent timeline
	s := makeSlot(1, []float64{999, 0, 1, 3})
	run(a, s)
	if s.Layout.Translation != [3]float64{0, 0, -1} {
		t.Fatal(s.Layout)
	}
	// A gap refuses without pixel/pose writes, and forgets this arm only.
	s = makeSlot(3, []float64{999, 3, 0, 1})
	before := bytes.Clone(s.Data())
	pose := s.Layout
	err := d.check(context.Background(), nil, a, s.Info())
	var refusal *stage.Refusal
	if !errors.As(err, &refusal) || refusal.Reason != 1 || !bytes.Equal(before, s.Data()) || !reflect.DeepEqual(pose, s.Layout) {
		t.Fatal(err)
	}
	s = makeSlot(4, []float64{999, 0, 0, 1})
	run(a, s)
	if s.Layout.Translation != [3]float64{} {
		t.Fatal("restart retained reference")
	}
	s = makeSlot(6, []float64{999, 0, 0, 1})
	run(b, s)
	if s.Layout.Translation != [3]float64{} {
		t.Fatal("empty first volume acquired later reference")
	}
	// A ring/revision change keeps per-arm state, and composes with old R/T.
	a.Revision = 2
	a.SegmentId = 55
	s = makeSlot(5, []float64{999, 0, 1, 0})
	s.Layout.Rotation = [9]float64{0, -1, 0, 1, 0, 0, 0, 0, 1}
	s.Layout.Translation = [3]float64{2, 3, 4}
	run(a, s)
	if s.Layout.Translation != [3]float64{2, 3, 5} {
		t.Fatal(s.Layout)
	}
}

func TestManifestMatchesDeclarations(t *testing.T) {
	app, err := definition()
	if err != nil {
		t.Fatal(err)
	}
	data, err := app.Manifest("sh", "./launch.sh")
	if err != nil {
		t.Fatal(err)
	}
	checked, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var parsed manifest.Manifest
	if err := json.Unmarshal(checked, &parsed); err != nil {
		t.Fatal(err)
	}
	if err := parsed.Validate(); err != nil {
		t.Fatal(err)
	}
	var got, want any
	if err := json.Unmarshal(data, &got); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(checked, &want); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(got, want) {
		t.Fatal("checked-in manifest differs from declarations")
	}
}
