package control

import (
	"math"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestInputContextChecksArrangedLevelCropAndTime(t *testing.T) {
	s := NewState(&lifecycle{}, nil)
	c := stateClient(t)
	const gen = uint64(3)<<32 | 7
	if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_TimeMapping{TimeMapping: &pb.TimeMapping{Gen: gen, Revision: 1, T: []float64{0, 1, 2}}}}); err != nil {
		t.Fatal(err)
	}
	if err := s.Apply(c, &pb.HostMsg{Msg: &pb.HostMsg_EpochChange{EpochChange: &pb.EpochChange{
		Datasets: []*pb.DatasetState{{Gen: gen, TimeMappingRevision: 1, Geometry: &pb.Geometry{Gen: gen,
			Crop:   &pb.EdgeBox{Z0: 1.5, Z1: 9.5, Y0: 3.5, Y1: 19.5, X0: 7.5, X1: 39.5},
			Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 2, Y: 4, X: 8}},
		}}}, Panes: []*pb.PaneEpoch{{ViewId: 0, Epoch: 1, Gen: gen}},
	}}}); err != nil {
		t.Fatal(err)
	}
	base := doorbell.Input{Generation: 7, Epoch: 1, Time: 2, Phase: doorbell.Release, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{2, 4, 8}}}
	for _, tc := range []struct {
		name     string
		change   func(*doorbell.Input)
		unusable bool
	}{
		{"inside", func(*doorbell.Input) {}, false},
		{"anisotropic lower edge", func(e *doorbell.Input) { e.Level = 1; e.Position.Values = [12]float32{0.5, 0.5, 0.5} }, false},
		{"anisotropic upper edge", func(e *doorbell.Input) { e.Level = 1; e.Position.Values = [12]float32{4.5, 4.5, 4.5} }, false},
		{"affine z outside", func(e *doorbell.Input) { e.Level = 1; e.Position.Values = [12]float32{4.625, 1, 1} }, true},
		{"anisotropic y outside", func(e *doorbell.Input) { e.Level = 1; e.Position.Values = [12]float32{1, 5, 1} }, true},
		{"anisotropic x outside", func(e *doorbell.Input) { e.Level = 1; e.Position.Values = [12]float32{1, 1, 5} }, true},
		{"bad level", func(e *doorbell.Input) { e.Level = 2 }, true},
		{"bad time", func(e *doorbell.Input) { e.Time = 3 }, true},
		{"outside voxel", func(e *doorbell.Input) { e.Position.Values[2] = 40 }, true},
		{"outside pick", func(e *doorbell.Input) { e.Position.Kind = doorbell.Picked; e.Position.Values[2] = 40 }, true},
		{"miss", func(e *doorbell.Input) { e.Position.Values[2] = 40; e.Position.Flags = doorbell.Miss }, false},
		{"miss with bad time", func(e *doorbell.Input) { e.Position.Flags = doorbell.Miss; e.Time = 3 }, true},
		{"ray outside", func(e *doorbell.Input) {
			e.Position.Kind = doorbell.Ray
			e.Position.Values = [12]float32{100, 100, 100, -100, -100, -100}
		}, false},
		{"ray pick inside", func(e *doorbell.Input) {
			e.Position.Kind = doorbell.RayAndPicked
			e.Position.Values = [12]float32{100, 100, 100, -100, -100, -100, 0.5, 0, 2, 4, 8}
		}, false},
		{"point pick inside", func(e *doorbell.Input) {
			e.Position.Kind = doorbell.RayAndPicked
			e.Position.Values = [12]float32{100, 100, 100, -100, -100, -100, 0.5, 0, 2, 4, 8, float32(math.NaN())}
		}, false},
		{"ray pick outside", func(e *doorbell.Input) {
			e.Position.Kind = doorbell.RayAndPicked
			e.Position.Values = [12]float32{2, 4, 8, 2, 4, 8, 0.5, 0, 2, 4, 40}
		}, true},
		{"absent position", func(e *doorbell.Input) { e.Position.Kind = doorbell.None }, false},
		{"old epoch", func(e *doorbell.Input) { e.Epoch = 2 }, true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			in := base
			tc.change(&in)
			if err := s.CheckInput(in); (err != nil) != tc.unusable {
				t.Fatal(in, err)
			}
		})
	}
	// Only table bounds are checked, not the contents of an acknowledged mapping.
	s.panes[0].Time.T[2] = math.NaN()
	if err := s.CheckInput(base); err != nil {
		t.Fatal(err)
	}
	s.panes[0].Time = nil
	base.Time = math.MaxUint32
	if err := s.CheckInput(base); err != nil {
		t.Fatal("unarranged time is unknown, not out of bounds", err)
	}
}
