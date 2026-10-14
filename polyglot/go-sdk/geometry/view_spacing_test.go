package geometry

import (
	"math"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestPhysicalViewSpacing(t *testing.T) {
	for _, tc := range []struct {
		name           string
		view           *pb.ViewState
		known, invalid bool
	}{
		{"absent", nil, false, false},
		{"display fallback", &pb.ViewState{SpacingZyx: []float64{1, 1, 1}, SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK}, false, false},
		{"file", &pb.ViewState{SpacingZyx: []float64{3, 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, true, false},
		{"override", &pb.ViewState{SpacingZyx: []float64{3, 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_OVERRIDE}, true, false},
		{"missing unit", &pb.ViewState{SpacingZyx: []float64{3, 2, 1}, SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"bad length", &pb.ViewState{SpacingZyx: []float64{1, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"all nan", &pb.ViewState{SpacingZyx: []float64{math.NaN(), math.NaN(), math.NaN()}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"override nan", &pb.ViewState{SpacingZyx: []float64{math.NaN(), 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_OVERRIDE}, false, true},
		{"infinity", &pb.ViewState{SpacingZyx: []float64{math.Inf(1), 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"zero", &pb.ViewState{SpacingZyx: []float64{0, 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"negative", &pb.ViewState{SpacingZyx: []float64{3, -2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}, false, true},
		{"future provenance", &pb.ViewState{SpacingZyx: []float64{3, 2, 1}, SpacingUnit: "um", SpacingProvenance: 99}, false, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			spacing, unit, known, err := PhysicalViewSpacing(tc.view)
			if known != tc.known || (err != nil) != tc.invalid {
				t.Fatal(spacing, unit, known, err)
			}
			if known {
				if spacing != (ZYX{3, 2, 1}) || unit != "um" {
					t.Fatal(spacing, unit)
				}
			} else if spacing != (ZYX{}) || unit != "" {
				t.Fatal("invented calibration", spacing, unit)
			}
			axesSpacing, axesUnit, axes, axesErr := PhysicalViewSpacingAxes(tc.view)
			wantAxes := [3]bool{tc.known, tc.known, tc.known}
			if axesSpacing != spacing || axesUnit != unit || axes != wantAxes || (axesErr != nil) != tc.invalid {
				t.Fatal(axesSpacing, axesUnit, axes, axesErr)
			}
		})
	}
}

func TestPartiallyRecordedViewSpacing(t *testing.T) {
	// Every combination of one or two measured axes, including missing Z.
	for mask := 1; mask < 7; mask++ {
		view := &pb.ViewState{SpacingZyx: []float64{3, 2, 1}, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}
		var wantAxes [3]bool
		for i := range 3 {
			wantAxes[i] = mask&(1<<i) != 0
			if !wantAxes[i] {
				view.SpacingZyx[i] = math.NaN()
			}
		}
		wire, err := proto.Marshal(view)
		if err != nil {
			t.Fatal(err)
		}
		var received pb.ViewState
		if err := proto.Unmarshal(wire, &received); err != nil {
			t.Fatal(err)
		}
		spacing, unit, axes, err := PhysicalViewSpacingAxes(&received)
		if err != nil || unit != "um" || axes != wantAxes {
			t.Fatalf("mask %d: %v %q %v %v", mask, spacing, unit, axes, err)
		}
		legacySpacing, legacyUnit, known, err := PhysicalViewSpacing(&received)
		if err != nil || !known || legacyUnit != unit {
			t.Fatal(legacySpacing, legacyUnit, known, err)
		}
		for i, measured := range axes {
			if measured {
				if spacing[i] != view.SpacingZyx[i] || legacySpacing[i] != spacing[i] {
					t.Fatal("changed measured axis", i, spacing, legacySpacing)
				}
			} else if !math.IsNaN(spacing[i]) || !math.IsNaN(legacySpacing[i]) {
				t.Fatal("invented unknown axis", i, spacing, legacySpacing)
			}
		}
	}
}

func TestPhysicalViewSpacingAxesMeasurementInvariant(t *testing.T) {
	file := pb.SpacingProvenance_SPACING_PROVENANCE_FILE
	fallback := pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK
	for _, tc := range []struct {
		name    string
		view    *pb.ViewState
		axes    [3]bool
		invalid bool
	}{
		{"partial file", &pb.ViewState{SpacingZyx: []float64{math.NaN(), 0.5, 0.25}, SpacingUnit: "um", SpacingProvenance: file}, [3]bool{false, true, true}, false},
		{"all axes", &pb.ViewState{SpacingZyx: []float64{2, 0.5, 0.25}, SpacingUnit: "um", SpacingProvenance: file}, [3]bool{true, true, true}, false},
		{"display fallback", &pb.ViewState{SpacingZyx: []float64{1, 1, 1}, SpacingProvenance: fallback}, [3]bool{}, false},
		{"fallback with unit", &pb.ViewState{SpacingZyx: []float64{1, 1, 1}, SpacingUnit: "um", SpacingProvenance: fallback}, [3]bool{}, false},
		{"file without unit", &pb.ViewState{SpacingZyx: []float64{2, 0.5, 0.25}, SpacingProvenance: file}, [3]bool{}, true},
		{"garbage file", &pb.ViewState{SpacingZyx: []float64{0, -1, math.Inf(1)}, SpacingUnit: "um", SpacingProvenance: file}, [3]bool{}, true},
		{"invalid after valid axis", &pb.ViewState{SpacingZyx: []float64{2, -1, 0.25}, SpacingUnit: "um", SpacingProvenance: file}, [3]bool{}, true},
		{"absent", nil, [3]bool{}, false},
	} {
		t.Run(tc.name, func(t *testing.T) {
			spacing, unit, axes, err := PhysicalViewSpacingAxes(tc.view)
			if axes != tc.axes || (err != nil) != tc.invalid {
				t.Fatal(spacing, unit, axes, err)
			}
			anyKnown := axes[0] || axes[1] || axes[2]
			for i, known := range axes {
				absent := math.IsNaN(spacing[i]) || spacing[i] == 0
				if absent != !known {
					t.Fatalf("axis %d: absent = %v, known = %v, spacing = %v", i, absent, known, spacing[i])
				}
				if known && (spacing[i] <= 0 || math.IsNaN(spacing[i]) || math.IsInf(spacing[i], 0) || unit != "um" || spacing[i] != tc.view.SpacingZyx[i]) {
					t.Fatalf("axis %d: invalid or changed measurement %v %q", i, spacing[i], unit)
				}
				if !known && anyKnown && !math.IsNaN(spacing[i]) {
					t.Fatalf("axis %d: partial file lost unknown NaN", i)
				}
			}
			if !anyKnown && (spacing != (ZYX{}) || unit != "") {
				t.Fatal("unmeasured input leaked through", spacing, unit)
			}
			_, _, any, anyErr := PhysicalViewSpacing(tc.view)
			if any != anyKnown || (anyErr != nil) != tc.invalid {
				t.Fatal("whole-pitch helper must mean any axis", any, anyErr)
			}
		})
	}
}

func TestGeometrySpacingRemainsFinite(t *testing.T) {
	for _, value := range []float64{math.NaN(), math.Inf(1), math.Inf(-1)} {
		g := &pb.Geometry{SpacingZ: value, SpacingY: 2, SpacingX: 1, SpacingUnit: "um", SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_FILE}
		if _, _, known, err := PhysicalSpacing(g); err == nil || known {
			t.Fatal("accepted nonfinite Geometry measurement", value, known, err)
		}
	}
	// A partly recorded file reaches tools as finite display fallback.
	g := &pb.Geometry{SpacingZ: 1, SpacingY: 2, SpacingX: 1, SpacingProvenance: pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK}
	if spacing, unit, known, err := PhysicalSpacing(g); err != nil || known || unit != "" || spacing != (ZYX{}) {
		t.Fatal("treated fallback as measured Geometry", spacing, unit, known, err)
	}
}
