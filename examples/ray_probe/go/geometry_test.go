package main

import (
	"math"
	"slices"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestLevelMaps(t *testing.T) {
	edges := geometry.Box{Min: point{24.5, 24.5, 24.5}, Max: point{1519.5, 1519.5, 1519.5}}
	b, err := edges.AtLevel(point{2, 2, 2})
	if err != nil || b.Min != (point{12, 12, 12}) || b.Max != (point{759.5, 759.5, 759.5}) {
		t.Fatalf("edges: %v %v", b, err)
	}
	centres, err := b.Centres()
	if err != nil || centres != (box{Min: cell{12, 12, 12}, Max: cell{760, 760, 760}}) {
		t.Fatalf("centers: %v %v", centres, err)
	}
	if got := level0(point{2, 3, 4}, point{3, 2, 5}); got != (point{7, 6.5, 22}) {
		t.Fatal(got)
	}
	g := &pb.Geometry{Extent: &pb.Extent{ZN: 31, YN: 32, XN: 35}, Crop: &pb.EdgeBox{Z0: -.5, Y0: -.5, X0: -.5, Z1: 30.5, Y1: 31.5, X1: 34.5}, Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 3, Y: 2, X: 5}}}
	s, clip, _, err := levelGeometry(g, 1)
	if err != nil || s != (point{3, 2, 5}) || clip != (box{Max: cell{10, 16, 7}}) {
		t.Fatalf("level: %v %v %v", s, clip, err)
	}
}

func TestTraversal(t *testing.T) {
	clip := box{Max: cell{10, 10, 10}}
	for _, tc := range []struct {
		name string
		a, b point
		want []cell
	}{
		{"axis", point{1, 2, 3}, point{4, 2, 3}, []cell{{1, 2, 3}, {2, 2, 3}, {3, 2, 3}, {4, 2, 3}}},
		{"reverse", point{4, 2, 3}, point{1, 2, 3}, []cell{{4, 2, 3}, {3, 2, 3}, {2, 2, 3}, {1, 2, 3}}},
		{"same cell", point{1.1, 2.1, 3.1}, point{1.2, 2.2, 3.2}, []cell{{1, 2, 3}}},
		{"face endpoint", point{1, 2, 3}, point{1.5, 2, 3}, []cell{{1, 2, 3}, {2, 2, 3}}},
		{"face parallel", point{1.5, 2, 3}, point{1.5, 3, 3}, []cell{{1, 2, 3}, {2, 2, 3}, {1, 3, 3}, {2, 3, 3}}},
		{"corner", point{1, 1, 1}, point{2, 2, 2}, []cell{{1, 1, 1}, {1, 1, 2}, {1, 2, 1}, {1, 2, 2}, {2, 1, 1}, {2, 1, 2}, {2, 2, 1}, {2, 2, 2}}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			got, err := traverse(tc.a, tc.b, clip)
			if err != nil || !slices.Equal(got, tc.want) {
				t.Fatalf("%v, %v; want %v", got, err, tc.want)
			}
		})
	}
	// Independent densely sampled expectation: this oblique segment has no
	// simultaneous crossings, so nearest-center sampling captures every cell.
	a, b := point{.12, .23, .34}, point{8.21, 7.32, 6.43}
	var want []cell
	for i := 0; i <= 100000; i++ {
		var c cell
		for d := range 3 {
			c[d] = int64(math.Floor(a[d] + (b[d]-a[d])*float64(i)/100000 + .5))
		}
		if len(want) == 0 || c != want[len(want)-1] {
			want = append(want, c)
		}
	}
	got, err := traverse(a, b, clip)
	if err != nil || !slices.Equal(got, want) {
		t.Fatalf("oblique %v %v; want %v", got, err, want)
	}
	// Pixels 25..1519 at factor 2 include 12..759, clipping both endpoints.
	got, err = traverse(point{10, 12, 12}, point{15, 12, 12}, box{Min: cell{12, 12, 12}, Max: cell{760, 760, 760}})
	if err != nil || !slices.Equal(got, []cell{{12, 12, 12}, {13, 12, 12}, {14, 12, 12}, {15, 12, 12}}) {
		t.Fatalf("crop %v %v", got, err)
	}
	if _, err = traverse(point{}, point{1000, 1000, 1000}, box{Max: cell{1001, 1001, 1001}}); err == nil {
		t.Fatal("unbounded traversal accepted")
	}
}

func TestFrozenAxisAndCentredWireRay(t *testing.T) {
	g := &pb.Geometry{
		Extent: &pb.Extent{ZN: 3, YN: 1, XN: 32},
		Crop:   &pb.EdgeBox{Z0: -.5, Y0: -.5, X0: -.5, Z1: 2.5, Y1: .5, X1: 31.5},
		Levels: []*pb.LevelScale{{Z: 1, Y: 1, X: 1}, {Z: 2, Y: 1, X: 2}, {Z: 2, Y: 1, X: 4}, {Z: 2, Y: 1, X: 8}},
	}
	scale, clip, extent, err := levelGeometry(g, 3)
	if err != nil || scale != (point{2, 1, 8}) || clip != (box{Max: cell{1, 1, 4}}) {
		t.Fatalf("thin level: %v %v %v", scale, clip, err)
	}
	if got := level0(point{}, scale); got != (point{.5, 0, 3.5}) {
		t.Fatalf("frozen-axis center: %v", got)
	}
	if got, err := blockOf(cell{}, scale, extent); err != nil || got != (box{Max: cell{2, 1, 8}}) {
		t.Fatalf("frozen-axis refinement: %v %v", got, err)
	}
	// The wire endpoints for a 100-voxel Z axis. These coordinates
	// are already centered; shifting them again would lose the last voxel.
	p := doorbell.Position{Kind: doorbell.Ray}
	copy(p.Values[:], []float32{-.5, 0, 0, 99.5, 0, 0, 0})
	got, err := candidates(p, box{Max: cell{100, 1, 1}})
	if err != nil || len(got) != 100 || got[0] != (cell{0, 0, 0}) || got[99] != (cell{99, 0, 0}) {
		t.Fatalf("centered ray: %v %v", got, err)
	}
}

func TestShapesAndBlocks(t *testing.T) {
	clip := box{Max: cell{10, 10, 10}}
	for _, tc := range []struct {
		k    doorbell.PositionKind
		want []cell
	}{
		{doorbell.ColumnYX, []cell{{2, 1, 2}, {3, 1, 2}}},
		{doorbell.ColumnZX, []cell{{1, 2, 2}, {1, 3, 2}}},
		{doorbell.ColumnZY, []cell{{1, 2, 2}, {1, 2, 3}}},
	} {
		p := doorbell.Position{Kind: tc.k}
		copy(p.Values[:], []float32{1.2, 2.2, 2, 4})
		got, err := candidates(p, clip)
		if err != nil || !slices.Equal(got, tc.want) {
			t.Fatalf("column %v %v", got, err)
		}
	}
	p := doorbell.Position{Kind: doorbell.RayAndPicked}
	copy(p.Values[:], []float32{-1, 2, 3, 3, 2, 3, .5, 9, 9, 9})
	a, b, ok := segment(p)
	if !ok || a != (point{1, 2, 3}) || b != (point{3, 2, 3}) {
		t.Fatalf("ray entry %v %v", a, b)
	}
	for _, tc := range []struct {
		c      cell
		s      point
		extent box
		want   box
	}{
		{cell{2, 3, 4}, point{2, 2, 2}, box{Max: cell{20, 20, 20}}, box{Min: cell{4, 6, 8}, Max: cell{6, 8, 10}}},
		{cell{2, 3, 4}, point{4, 4, 4}, box{Max: cell{10, 15, 19}}, box{Min: cell{8, 12, 16}, Max: cell{10, 15, 19}}},
	} {
		got, err := blockOf(tc.c, tc.s, tc.extent)
		if err != nil || got != tc.want {
			t.Fatalf("block %v %v", got, err)
		}
	}
}

func TestMaximumAndDistances(t *testing.T) {
	for _, tc := range []struct {
		v    []float64
		want int
	}{
		{[]float64{math.NaN(), 5, 5, 4}, 1},
		{[]float64{math.NaN(), math.Inf(-1), math.Inf(-1)}, 1},
		{[]float64{1, math.Inf(1), math.Inf(1)}, 1},
	} {
		got, err := firstMax(tc.v)
		if err != nil || got != tc.want {
			t.Fatalf("argmax %d %v", got, err)
		}
	}
	if _, err := firstMax([]float64{math.NaN()}); err != errNothing {
		t.Fatal(err)
	}
	b := box{Min: cell{3, 4, 5}, Max: cell{5, 6, 7}}
	c, v, err := blockMax([]float64{0, 8, 8, 0, 8, 0, 0, 0}, b)
	if err != nil || c != (cell{3, 4, 6}) || v != 8 {
		t.Fatalf("block tie %v %v %v", c, v, err)
	}
	if got := distances([]cell{{0, 0, 0}, {1, 0, 0}, {1, 1, 0}}, point{3, 2, 1}); !slices.Equal(got, []float64{0, 3, 5}) {
		t.Fatal(got)
	}
}
