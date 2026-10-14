package geometry

import (
	"math"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestGridPhaseAndIntegralHits(t *testing.T) {
	// §8.11's crop includes 25..1519 at level 0 and 12..759 at level 1.
	// Plain division incorrectly starts the latter at 13.
	b := Box{ZYX{24.5, 24.5, 24.5}, ZYX{1519.5, 1519.5, 1519.5}}
	for _, tc := range []struct {
		scale  float64
		lo, hi int64
	}{{1, 25, 1520}, {2, 12, 760}, {4, 6, 380}, {8, 3, 190}} {
		scaled, err := b.AtLevel(ZYX{tc.scale, tc.scale, tc.scale})
		if err != nil {
			t.Fatal(err)
		}
		got, err := scaled.Centres()
		if err != nil {
			t.Fatal(err)
		}
		if got.Min[0] != tc.lo || got.Max[0] != tc.hi {
			t.Fatalf("scale %v: %+v", tc.scale, got)
		}
	}
	// An exactly-integral upper bound includes that centre; negative coordinates
	// use ceil/floor, not Go's truncating float-to-int conversion.
	got, err := (Box{ZYX{-1.8, 2, 0.1}, ZYX{1, 2, 0.2}}).Centres()
	if err != nil || got.Min != ([3]int64{-1, 2, 1}) || got.Max != ([3]int64{2, 3, 1}) || !got.Empty() {
		t.Fatal(got, err)
	}
}

func TestOddDimensionUsesSamplingFactor(t *testing.T) {
	// A 31-voxel axis downsamples to 15 by dropping the odd tail. Its
	// sampling factor remains 2. A shape ratio of 31/15 shifts this crop's
	// first included centre from 1 to 0.
	g := &pb.Geometry{
		Crop:   &pb.EdgeBox{Z0: 0.51, Y0: 0.51, X0: 0.51, Z1: 28.5, Y1: 28.5, X1: 28.5},
		Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 2, Y: 2, X: 2}},
	}
	got, err := CropAtLevel(g, 1)
	if err != nil || got.Min != ([3]int64{1, 1, 1}) || got.Max != ([3]int64{15, 15, 15}) {
		t.Fatal(got, err)
	}
}

func TestCropUsesFrozenPerAxisFactors(t *testing.T) {
	// 3 x 1 x 32: Z stops at factor 2; Y was already a singleton and stays
	// at factor 1; X continues to factor 8. Shape clipping remains explicit.
	g := &pb.Geometry{
		Crop:   &pb.EdgeBox{Z0: -.5, Y0: -.5, X0: -.5, Z1: 2.5, Y1: .5, X1: 31.5},
		Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 2, Y: 1, X: 2}, {Z: 2, Y: 1, X: 4}, {Z: 2, Y: 1, X: 8}},
	}
	got, err := CropAtLevel(g, 3)
	if err != nil || got != (IndexBox{Max: [3]int64{2, 1, 4}}) {
		t.Fatalf("crop with declared factors: %v, %v", got, err)
	}
	got, err = got.Intersect(IndexBox{Max: [3]int64{1, 1, 4}})
	if err != nil || got != (IndexBox{Max: [3]int64{1, 1, 4}}) {
		t.Fatalf("shape-clipped crop: %v, %v", got, err)
	}
}

func TestAnisotropicCompositionAndExplicitIntersection(t *testing.T) {
	b := Box{ZYX{-3.5, 10.5, 24.5}, ZYX{15.5, 80.5, 1519.5}}
	once, _ := b.AtLevel(ZYX{4, 8, 2})
	step, _ := b.AtLevel(ZYX{2, 2, 2})
	twice, _ := step.AtLevel(ZYX{2, 4, 1})
	if once != twice {
		t.Fatal(once, twice)
	}
	indices, _ := b.Centres()
	if indices.Min[0] != -3 {
		t.Fatal("crop was silently clamped", indices)
	}
	clipped, err := indices.Intersect(IndexBox{Max: [3]int64{10, 20, 30}})
	if err != nil || clipped.Min != ([3]int64{0, 11, 25}) || clipped.Max != ([3]int64{10, 20, 30}) {
		t.Fatal(clipped, err)
	}
	for _, v := range []float64{0, -1, math.NaN(), math.Inf(1)} {
		if _, err := b.AtLevel(ZYX{v, 1, 1}); err == nil {
			t.Fatalf("accepted scale %v", v)
		}
	}
	if _, err := (Box{Max: ZYX{0x1p63, 0, 0}}).Centres(); err == nil {
		t.Fatal("accepted overflowing indices")
	}
}

func TestUnknownSpacingAndOwnedTime(t *testing.T) {
	fallback := &pb.Geometry{SpacingZ: 1, SpacingY: 1, SpacingX: 1, SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK}
	if _, _, known, err := PhysicalSpacing(fallback); known || err != nil {
		t.Fatal(known, err)
	}
	fallback.SpacingProvenance = pb.SpacingProvenance_SPACING_PROVENANCE_FILE
	if _, _, _, err := PhysicalSpacing(fallback); err == nil {
		t.Fatal("measurement with no unit accepted")
	}
	fallback.SpacingUnit = "um"
	if got, unit, known, err := PhysicalSpacing(fallback); got != (ZYX{1, 1, 1}) || unit != "um" || !known || err != nil {
		t.Fatal(got, unit, known, err)
	}
	unknown, err := NewTimeAxis(nil)
	if err != nil {
		t.Fatal(err)
	}
	if _, known, err := unknown.At(100); known || err != nil {
		t.Fatal("invented uniform time")
	}
	mapping := &pb.TimeMapping{Gen: 4, Revision: 1, T: []float64{0, 0.2, 0.9}}
	axis, err := NewTimeAxis(mapping)
	if err != nil {
		t.Fatal(err)
	}
	mapping.T[1] = 99
	if v, known, err := axis.At(1); v != 0.2 || !known || err != nil {
		t.Fatal(v, known, err)
	}
	if _, _, err := axis.At(3); err == nil {
		t.Fatal("out-of-range time accepted")
	}
	mapping.T = []float64{1, 0}
	if _, err := NewTimeAxis(mapping); err == nil {
		t.Fatal("descending time accepted")
	}
}
