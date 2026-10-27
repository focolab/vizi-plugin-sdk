package control

import (
	"fmt"
	"math"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
)

// CheckInput checks accepted input against the acknowledged pane without copying
// its potentially large time table. It never rejects input or changes interaction
// state: callers must still deliver terminal records and their closure.
func (s *State) CheckInput(in doorbell.Input) error {
	s.mu.RLock()
	defer s.mu.RUnlock()
	p := s.panes[in.ViewID]
	if p == nil || p.Pane.Epoch != in.Epoch || uint32(p.Pane.Gen) != in.Generation {
		return fmt.Errorf("input pane=%d epoch=%d generation=%d has no matching arrangement", in.ViewID, in.Epoch, in.Generation)
	}
	g := p.Dataset.GetGeometry()
	if g == nil || int(in.Level) >= len(g.Levels) || g.Levels[in.Level] == nil {
		return fmt.Errorf("input level=%d is not arranged for pane=%d epoch=%d", in.Level, in.ViewID, in.Epoch)
	}
	if p.Time != nil && uint64(in.Time) >= uint64(len(p.Time.T)) {
		return fmt.Errorf("input t=%d exceeds arranged time table length=%d", in.Time, len(p.Time.T))
	}
	if in.Position.Flags&doorbell.Miss != 0 {
		return nil
	}
	start := 0
	switch in.Position.Kind {
	case doorbell.Voxel, doorbell.Picked:
	case doorbell.RayAndPicked:
		start = 8 // Only picked ZYX: eye, far, entry, roll and back_frac are exempt.
	default:
		return nil // no voxel coordinate: NONE and RAY are exempt
	}
	box, err := geometry.FromProto(g.Crop)
	if err != nil {
		return fmt.Errorf("input crop: %w", err)
	}
	scale := g.Levels[in.Level]
	box, err = box.AtLevel(geometry.ZYX{scale.Z, scale.Y, scale.X})
	if err != nil {
		return fmt.Errorf("input crop at level %d: %w", in.Level, err)
	}
	for axis := range 3 {
		v := float64(in.Position.Values[start+axis])
		if math.IsNaN(v) || math.IsInf(v, 0) || v < box.Min[axis] || v > box.Max[axis] {
			return fmt.Errorf("input coordinate axis=%d value=%g lies outside crop [%g,%g] at level=%d", axis, v, box.Min[axis], box.Max[axis], in.Level)
		}
	}
	return nil
}
