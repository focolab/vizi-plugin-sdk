// Package geometry implements the coordinate and time rules of control-plane
// §8.11. Coordinates are ZYX. A crop describes clipping, not the lit voxel set
// or a restriction on what a plugin may read.
package geometry

import (
	"errors"
	"fmt"
	"math"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type ZYX [3]float64

// Box contains inclusive edge-coordinate bounds, usually in level-0 voxels.
// Equal bounds can include a pixel center; they are not automatically empty.
type Box struct{ Min, Max ZYX }

// IndexBox contains half-open integer bounds. Signed indices preserve crops
// outside the dataset; call Intersect explicitly when dataset clipping is wanted.
type IndexBox struct{ Min, Max [3]int64 }

func finite(x float64) bool { return !math.IsNaN(x) && !math.IsInf(x, 0) }

func (b Box) Validate() error {
	for i := range 3 {
		if !finite(b.Min[i]) || !finite(b.Max[i]) || b.Min[i] > b.Max[i] {
			return fmt.Errorf("invalid edge bounds on ZYX axis %d", i)
		}
	}
	return nil
}

func FromProto(b *pb.EdgeBox) (Box, error) {
	if b == nil {
		return Box{}, errors.New("crop is absent")
	}
	out := Box{ZYX{b.Z0, b.Y0, b.X0}, ZYX{b.Z1, b.Y1, b.X1}}
	return out, out.Validate()
}

// AtLevel maps edges with (e + 0.5)/scale - 0.5. Dividing e alone loses the
// pyramid grid's phase. Convert edges before rounding to centers.
// scale is the sampling factor in Geometry.levels, not the array-shape ratio:
// downsampling 31 voxels to 15 by discarding the odd tail still has scale 2.
func (b Box) AtLevel(scale ZYX) (Box, error) {
	if err := b.Validate(); err != nil {
		return Box{}, err
	}
	var out Box
	for i, s := range scale {
		if !finite(s) || s <= 0 {
			return Box{}, fmt.Errorf("invalid level scale on ZYX axis %d", i)
		}
		out.Min[i] = (b.Min[i]+0.5)/s - 0.5
		out.Max[i] = (b.Max[i]+0.5)/s - 0.5
	}
	return out, out.Validate()
}

// Centres converts ceil(lo)..floor(hi), inclusive, to a half-open index box.
// Nonintegral narrow intervals may contain no center. Bounds that cannot be
// represented as int64 are errors rather than implementation-dependent casts.
func (b Box) Centres() (IndexBox, error) {
	if err := b.Validate(); err != nil {
		return IndexBox{}, err
	}
	var out IndexBox
	for i := range 3 {
		lo, hi := math.Ceil(b.Min[i]), math.Floor(b.Max[i])
		if lo < -0x1p63 || lo >= 0x1p63 || hi < -0x1p63 || hi >= 0x1p63 {
			return IndexBox{}, errors.New("crop exceeds signed index range")
		}
		out.Min[i] = int64(lo)
		if hi < lo {
			out.Max[i] = out.Min[i]
		} else {
			if int64(hi) == math.MaxInt64 {
				return IndexBox{}, errors.New("exclusive crop bound overflows")
			}
			out.Max[i] = int64(hi) + 1
		}
	}
	return out, nil
}

func (b IndexBox) Validate() error {
	for i := range 3 {
		if b.Min[i] > b.Max[i] {
			return fmt.Errorf("reversed index bounds on ZYX axis %d", i)
		}
	}
	return nil
}

func (b IndexBox) Empty() bool {
	for i := range 3 {
		if b.Min[i] >= b.Max[i] {
			return true
		}
	}
	return false
}

func (b IndexBox) Intersect(other IndexBox) (IndexBox, error) {
	if err := b.Validate(); err != nil {
		return IndexBox{}, err
	}
	if err := other.Validate(); err != nil {
		return IndexBox{}, err
	}
	var out IndexBox
	for i := range 3 {
		out.Min[i] = max(b.Min[i], other.Min[i])
		out.Max[i] = max(out.Min[i], min(b.Max[i], other.Max[i]))
	}
	return out, nil
}

// CropAtLevel uses Geometry.levels' sampling factors and returns included
// centers without implicitly clipping to the downsampled array's extent.
// The declared factors are per axis, never inferred as 2^level: an axis that
// has reached one sample keeps the factor of its last fold. Both affine maps
// still apply unchanged at those levels, including a singleton level-0 axis.
func CropAtLevel(g *pb.Geometry, level uint32) (IndexBox, error) {
	if g == nil || uint64(level) >= uint64(len(g.Levels)) || g.Levels[level] == nil {
		return IndexBox{}, errors.New("level geometry is absent")
	}
	b, err := FromProto(g.Crop)
	if err != nil {
		return IndexBox{}, err
	}
	s := g.Levels[level]
	b, err = b.AtLevel(ZYX{s.Z, s.Y, s.X})
	if err != nil {
		return IndexBox{}, err
	}
	return b.Centres()
}

// PhysicalSpacing preserves unknown measurement rather than treating a display
// fallback of 1 as physical data. The unit is returned without conversion.
func PhysicalSpacing(g *pb.Geometry) (spacing ZYX, unit string, known bool, err error) {
	if g == nil || g.SpacingProvenance == pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK {
		return spacing, "", false, nil
	}
	if g.SpacingProvenance != pb.SpacingProvenance_SPACING_PROVENANCE_FILE && g.SpacingProvenance != pb.SpacingProvenance_SPACING_PROVENANCE_OVERRIDE {
		return spacing, "", false, errors.New("unknown spacing provenance")
	}
	spacing = ZYX{g.SpacingZ, g.SpacingY, g.SpacingX}
	if g.SpacingUnit == "" {
		return ZYX{}, "", false, errors.New("physical spacing has no unit")
	}
	for _, s := range spacing {
		if !finite(s) || s <= 0 {
			return ZYX{}, "", false, errors.New("physical spacing must be finite and positive")
		}
	}
	return spacing, g.SpacingUnit, true, nil
}

// TimeAxis owns a validated copy of an arranged index-to-time mapping. A nil
// axis means unknown time; it never invents uniform spacing.
type TimeAxis struct {
	gen      uint64
	revision uint32
	values   []float64
}

func NewTimeAxis(m *pb.TimeMapping) (*TimeAxis, error) {
	if m == nil {
		return nil, nil
	}
	if m.Revision == 0 {
		return nil, errors.New("time mapping revision is zero")
	}
	values := append([]float64(nil), m.T...)
	for i, v := range values {
		if !finite(v) || (i > 0 && v < values[i-1]) {
			return nil, errors.New("time values must be finite and ascending")
		}
	}
	return &TimeAxis{m.Gen, m.Revision, values}, nil
}

func (t *TimeAxis) At(index uint32) (value float64, known bool, err error) {
	if t == nil {
		return 0, false, nil
	}
	if uint64(index) >= uint64(len(t.values)) {
		return 0, false, errors.New("time index out of bounds")
	}
	return t.values[index], true, nil
}

func (t *TimeAxis) Identity() (gen uint64, revision uint32) {
	if t == nil {
		return 0, 0
	}
	return t.gen, t.revision
}

// PhysicalViewSpacing returns the polled ViewState's spacing and whether ANY
// axis is measured. Use PhysicalViewSpacingAxes first when using individual
// pitches: known does not mean all axes are measured and does not permit using
// every spacing value. One known axis says nothing about the rest. Unknown axes
// in a partially recorded file are NaN; when known is false, spacing is zero.
// Tools should use PhysicalSpacing with their arranged geometry.
func PhysicalViewSpacing(v *pb.ViewState) (spacing ZYX, unit string, known bool, err error) {
	spacing, unit, axes, err := PhysicalViewSpacingAxes(v)
	return spacing, unit, axes[0] || axes[1] || axes[2], err
}

// PhysicalViewSpacingAxes returns spacing and measurement flags in ZYX order.
// Check axisKnown[i] for EVERY axis you use before using spacing[i]: one known
// axis says nothing about the rest. A true flag means a positive, finite
// measurement in unit. A false flag means no measurement: spacing[i] is NaN in
// a partially recorded file, or zero when no axes are known. Thus spacing[i] is
// NaN or zero exactly where axisKnown[i] is false. Leave those axes in voxels.
// Absent views and display fallback (even with a unit) return zero values and
// no known axes. Errors also return zero values and no known axes; invalid
// input values never leak through as measurements.
// Unlike Geometry, ViewState permits a file to record only some axes.
func PhysicalViewSpacingAxes(v *pb.ViewState) (spacing ZYX, unit string, axisKnown [3]bool, err error) {
	if v == nil || v.SpacingProvenance == pb.SpacingProvenance_SPACING_PROVENANCE_DISPLAY_FALLBACK {
		return spacing, "", axisKnown, nil
	}
	if v.SpacingProvenance != pb.SpacingProvenance_SPACING_PROVENANCE_FILE && v.SpacingProvenance != pb.SpacingProvenance_SPACING_PROVENANCE_OVERRIDE {
		return ZYX{}, "", [3]bool{}, errors.New("unknown spacing provenance")
	}
	if len(v.SpacingZyx) != 3 {
		return ZYX{}, "", [3]bool{}, errors.New("physical view spacing must have three ZYX values")
	}
	if v.SpacingUnit == "" {
		return ZYX{}, "", [3]bool{}, errors.New("physical view spacing has no unit")
	}
	for i, s := range v.SpacingZyx {
		if math.IsNaN(s) && v.SpacingProvenance == pb.SpacingProvenance_SPACING_PROVENANCE_FILE {
			spacing[i] = s
			continue
		}
		if !finite(s) || s <= 0 {
			return ZYX{}, "", [3]bool{}, errors.New("measured view spacing must be finite and positive")
		}
		spacing[i], axisKnown[i] = s, true
	}
	if axisKnown == ([3]bool{}) {
		return ZYX{}, "", [3]bool{}, errors.New("physical view spacing has no measured axes")
	}
	return spacing, v.SpacingUnit, axisKnown, nil
}
