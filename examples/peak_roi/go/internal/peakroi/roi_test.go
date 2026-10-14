package peakroi

import (
	"math"
	"slices"
	"testing"
)

func TestEllipsoidGeometry(t *testing.T) {
	r := ellipsoid([][3]int{{10, 20, 30}}, [3]float64{2, 3, 4}, false)
	if r.TypeName != "ellipsoid" || r.Root != 0 || len(r.Vertices) != 4 || r.Compile.NumDisplay != 18 || len(r.Edges) != 12 {
		t.Fatal("incorrect ROI structure")
	}
	if !slices.Equal(r.Compile.Controls, []int32{0, 1, 2, 3}) {
		t.Fatal("control ordering")
	}
	wantControls := [][3]float64{{10.5, 20.5, 30.5}, {12.5, 20.5, 30.5}, {10.5, 23.5, 30.5}, {10.5, 20.5, 34.5}}
	for i, v := range r.Vertices {
		if [3]float64{v.Position.Z, v.Position.Y, v.Position.X} != wantControls[i] {
			t.Fatalf("control %d: %v", i, v)
		}
	}
	// Literal independent expectations, deliberately not generated from the
	// builder's ring-sign table or compile coefficients.
	want := [][3]float64{
		{12.5, 20.5, 30.5}, {8.5, 20.5, 30.5}, {10.5, 23.5, 30.5}, {10.5, 17.5, 30.5}, {10.5, 20.5, 34.5}, {10.5, 20.5, 26.5},
		{12.5, 23.5, 30.5}, {8.5, 23.5, 30.5}, {8.5, 17.5, 30.5}, {12.5, 17.5, 30.5},
		{12.5, 20.5, 34.5}, {8.5, 20.5, 34.5}, {8.5, 20.5, 26.5}, {12.5, 20.5, 26.5},
		{10.5, 23.5, 34.5}, {10.5, 17.5, 34.5}, {10.5, 17.5, 26.5}, {10.5, 23.5, 26.5},
	}
	display := make([][3]float64, 18)
	for _, term := range r.Compile.Triplets {
		if term.DisplayVertex < 0 || term.DisplayVertex >= 18 || term.Control < 0 || term.Control >= 4 {
			t.Fatal("compile bounds")
		}
		for a := range 3 {
			display[term.DisplayVertex][a] += term.Coeff * wantControls[r.Compile.Controls[term.Control]][a]
		}
	}
	if !slices.Equal(display, want) {
		t.Fatalf("display geometry: %v", display)
	}
	for _, edge := range r.Edges {
		if edge.Source < 0 || edge.Source >= 6 || edge.Target < 0 || edge.Target >= 6 || edge.Control < 6 || edge.Control >= 18 || edge.Weight != math.Sqrt2/2 {
			t.Fatal("invalid quarter arc", edge)
		}
		// Evaluate the rational quadratic midpoint: it must lie on the
		// ellipsoid, at 45 degrees between its two principal tips.
		q := 0.0
		for a := range 3 {
			p := (display[edge.Source][a] + 2*edge.Weight*display[edge.Control][a] + display[edge.Target][a]) / (2 + 2*edge.Weight)
			q += math.Pow((p-wantControls[0][a])/[]float64{2, 3, 4}[a], 2)
		}
		if math.Abs(q-1) > 1e-12 {
			t.Fatalf("arc midpoint off ellipsoid: %g", q)
		}
	}
	if r.TStart != nil || r.TEnd != nil || len(r.PoseTranslation) != 0 || len(r.DirectionSpecs) != 0 || len(r.Views) != 0 || r.Inclusion != nil {
		t.Fatal("unexpected optional fields")
	}
}

func TestDenseTimePose(t *testing.T) {
	r := ellipsoid([][3]int{{4, 5, 6}, {7, 3, 1}, {2, 5, 9}}, [3]float64{1, 2, 3}, true)
	if r.TStart == nil || *r.TStart != 0 || r.TEnd == nil || *r.TEnd != 2 || r.OutOfSpan != 0 {
		t.Fatal("time domain")
	}
	if !slices.Equal(r.PoseTranslation, []float64{0, 0, 0, 3, -2, -5, -2, 0, 3}) {
		t.Fatal(r.PoseTranslation)
	}
}
