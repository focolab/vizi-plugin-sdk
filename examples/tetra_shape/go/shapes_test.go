package main

import (
	"context"
	"encoding/json"
	"math"
	"os"
	"reflect"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func near(t *testing.T, got, want float64) {
	t.Helper()
	if math.IsNaN(got) || math.IsInf(got, 0) || math.Abs(got-want) > 1e-9 {
		t.Fatalf("got %.16g want %.16g", got, want)
	}
}
func nearPoint(t *testing.T, got, want vec) {
	t.Helper()
	for i := range got {
		near(t, got[i], want[i])
	}
}
func tetraVolume(p [5]vec) float64 {
	return math.Abs(p[1].sub(p[0]).dot(p[2].sub(p[0]).cross(p[3].sub(p[0])))) / 6
}
func positions(r *pb.Roi) (p [5]vec) {
	for i, v := range r.Vertices {
		p[i] = fromWire(v.Position)
	}
	return
}

func TestScaleBindings(t *testing.T) {
	for _, offset := range []float64{0, 40, 120} {
		p := restPoints(offset)
		bindings := scaleBindings(p)
		if len(bindings) != 12 {
			t.Fatal(len(bindings))
		}
		index := 0
		for target := 0; target < 4; target++ {
			radial := p[target].sub(p[4])
			r2 := radial.dot(radial)
			for moved := 0; moved < 4; moved++ {
				if target == moved {
					continue
				}
				b := bindings[index]
				index++
				if b.Target != int32(target) || b.Moved != int32(moved) || len(b.M) != 9 {
					t.Fatal(b)
				}
				other := p[moved].sub(p[4])
				for row := 0; row < 3; row++ {
					for col := 0; col < 3; col++ {
						near(t, b.M[3*row+col], other[row]*radial[col]/r2)
					}
				}
				// Test the actual one-level movement for outward and inward drags.
				for _, factor := range []float64{1.5, .25} {
					delta := radial.mul(factor - 1)
					var propagated vec
					for row := 0; row < 3; row++ {
						for col := 0; col < 3; col++ {
							propagated[row] += b.M[3*row+col] * delta[col]
						}
					}
					nearPoint(t, p[moved].add(propagated), p[4].add(other.mul(factor)))
				}
			}
		}
	}
}

func TestContributedGeometry(t *testing.T) {
	q := contributions()
	if !q.ReplaceAll || len(q.Rois) != 4 {
		t.Fatal(q)
	}
	for i, id := range []string{"free", "scale", "volume", "unshaped"} {
		item := q.Rois[i]
		r := item.Roi
		if item.ContribId != id || item.Style != nil || r.TypeName != "tetra."+id || r.Root != 4 || len(r.Vertices) != 5 {
			t.Fatal(item)
		}
		p := restPoints(float64(i) * 40)
		for j, v := range r.Vertices {
			nearPoint(t, fromWire(v.Position), p[j])
			if j == 4 {
				if v.Enclosing || v.Handle != nil {
					t.Fatal("center is a surface point or handle")
				}
				continue
			}
			if !v.Enclosing || v.Handle == nil || !reflect.DeepEqual(v.Handle.Frame, []float64{1, 0, 0, 0, 1, 0, 0, 0, 1}) {
				t.Fatal(v)
			}
			h := v.Handle
			if id == "free" || id == "unshaped" {
				if h.Mode != pb.DofMode_ALL || h.DofMask != 7 || h.MinExtent != nil || h.ExtentAnchor != 0 || h.AxisSource != pb.AxisSource_AXIS_FRAME {
					t.Fatal(h)
				}
			} else {
				if h.Mode != pb.DofMode_PRIMARY || h.DofMask != 0 || h.ExtentAnchor != 4 || h.AxisSource != pb.AxisSource_AXIS_RADIAL || (h.MinExtent != nil) != (id == "scale") {
					t.Fatal(h)
				}
				if h.MinExtent != nil {
					near(t, *h.MinExtent, 1)
				}
			}
			if h.MaxExtent != nil {
				t.Fatal(h)
			}
		}
		if id == "scale" {
			if r.AutoSurface != pb.AutoSurface_AUTO_SURFACE_STAR || len(r.Bindings) != 12 {
				t.Fatal(r)
			}
		} else {
			if r.AutoSurface != pb.AutoSurface_AUTO_SURFACE_HULL || len(r.Bindings) != 0 {
				t.Fatal(r)
			}
		}
		// Clearing the specified fields must leave no accidental geometry or placement.
		stripped := proto.Clone(r).(*pb.Roi)
		stripped.TypeName = ""
		stripped.Root = 0
		stripped.Vertices = nil
		stripped.Bindings = nil
		stripped.AutoSurface = 0
		if !proto.Equal(stripped, &pb.Roi{}) {
			t.Fatal("unspecified fields", stripped)
		}
	}
	// Repeat commands rebuild independent rest shapes.
	q.Rois[0].Roi.Vertices[0].Position.Z = 999
	near(t, contributions().Rois[0].Roi.Vertices[0].Position.Z, 10)
}

func TestVolumeRule(t *testing.T) {
	for h := 0; h < 4; h++ {
		for _, height := range []float64{2, .5, 1.0 / 32, 0, -1} {
			r := contributions().Rois[2].Roi
			p := positions(r)
			var face []int
			var sum vec
			for j := 0; j < 4; j++ {
				if j != h {
					face = append(face, j)
					sum = sum.add(p[j])
				}
			}
			g := sum.mul(1.0 / 3)
			target := g.add(p[h].sub(g).mul(height))
			edit := &pb.ShapeEdit{Move: &pb.ConstrainMoveRequest{Roi: r, Handle: int32(h), P1: target.wire()}}
			saved := proto.Clone(edit)
			answer := volumeShape(edit)
			if !proto.Equal(edit, saved) {
				t.Fatal("mutated input")
			}
			if height < 1.0/16 {
				if answer.Resolution != pb.Resolution_REJECTED || answer.Message == "" || len(answer.Positions) != 0 {
					t.Fatal(answer)
				}
				continue
			}
			if answer.Resolution != pb.Resolution_OK || len(answer.Positions) != 5 {
				t.Fatal(answer)
			}
			var after [5]vec
			for j, v := range answer.Positions {
				if v == nil {
					t.Fatal("nil position")
				}
				after[j] = fromWire(v)
				if !after[j].finite() {
					t.Fatal(v)
				}
			}
			nearPoint(t, after[h], target)
			nearPoint(t, after[4], p[4])
			beforeVolume, afterVolume := tetraVolume(p), tetraVolume(after)
			if math.Abs(afterVolume-beforeVolume) > 1e-9*beforeVolume {
				t.Fatal(beforeVolume, afterVolume)
			}
			for _, j := range face {
				nearPoint(t, after[j], g.add(p[j].sub(g).mul(1/math.Sqrt(height))))
			}
		}
	}
}

func TestVolumeUsesEditedInputWithoutCachedState(t *testing.T) {
	r := contributions().Rois[2].Roi
	// A copied, already edited region has no contribution ID and differs from rest.
	r.Vertices[0].Position.Z += 3
	r.Vertices[1].Position.X -= 2
	for step := 0; step < 3; step++ {
		p := positions(r)
		h := step % 4
		target := p[4].add(p[h].sub(p[4]).mul(1.2))
		edit := &pb.ShapeEdit{RegionId: "copy", TypeName: "tetra.volume", Move: &pb.ConstrainMoveRequest{Roi: r, Handle: int32(h), P1: target.wire()}}
		answer := volumeShape(edit)
		if answer.Resolution != pb.Resolution_OK || len(answer.Positions) != 5 {
			t.Fatal(answer)
		}
		next := proto.Clone(r).(*pb.Roi)
		for i, v := range answer.Positions {
			next.Vertices[i].Position = v
		}
		before, after := tetraVolume(p), tetraVolume(positions(next))
		if math.Abs(before-after) > 1e-9*before {
			t.Fatal(before, after)
		}
		r = next
	}
}

func TestVolumeRejectsInvalidInput(t *testing.T) {
	for _, h := range []int32{-1, 4, 8} {
		r := volumeShape(&pb.ShapeEdit{Move: &pb.ConstrainMoveRequest{Handle: h}})
		if r.Resolution != pb.Resolution_REJECTED || r.Message == "" || len(r.Positions) != 0 {
			t.Fatal(r)
		}
	}
	for _, bad := range []*pb.ShapeEdit{nil, {Move: &pb.ConstrainMoveRequest{}}, {Move: &pb.ConstrainMoveRequest{Roi: contributions().Rois[2].Roi, P1: &pb.Vec3{Z: math.NaN()}}}} {
		if r := volumeShape(bad); r.Resolution != pb.Resolution_REJECTED || len(r.Positions) != 0 {
			t.Fatal(r)
		}
	}
}

func TestManifestAndSDKRegistration(t *testing.T) {
	app, err := declaration()
	if err != nil {
		t.Fatal(err)
	}
	generated, err := app.Manifest("sh", "./launch.sh")
	if err != nil {
		t.Fatal(err)
	}
	checked, err := os.ReadFile("vizi-plugin.json")
	if err != nil {
		t.Fatal(err)
	}
	var a, b any
	if err := json.Unmarshal(generated, &a); err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(checked, &b); err != nil {
		t.Fatal(err)
	}
	if !reflect.DeepEqual(a, b) {
		t.Fatalf("manifest differs from declaration:\n%s", generated)
	}
	runtime, err := app.Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	cfg := runtime.Control
	if len(cfg.Shapers) != 2 || cfg.Shapers["tetra.volume"] == nil || cfg.Shapers["tetra.unshaped"] != nil || cfg.ShapeEditEnded == nil {
		t.Fatal("wrong SDK registrations")
	}
	edit := &pb.ShapeEdit{Move: &pb.ConstrainMoveRequest{Roi: contributions().Rois[2].Roi, Handle: 3, P1: &pb.Vec3{Z: 16, Y: 20.5, X: 119.5}}}
	preview, err := cfg.Shapers["tetra.volume"](context.Background(), nil, edit, false)
	if err != nil {
		t.Fatal(err)
	}
	commit, err := cfg.Shapers["tetra.volume"](context.Background(), nil, edit, true)
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(preview, commit) {
		t.Fatal("preview and commit compute different shapes")
	}
}
