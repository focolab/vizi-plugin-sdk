package main

import (
	"fmt"
	"math"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// Coordinates and matrix components are always ZYX.
type vec [3]float64

func (a vec) add(b vec) vec     { return vec{a[0] + b[0], a[1] + b[1], a[2] + b[2]} }
func (a vec) sub(b vec) vec     { return vec{a[0] - b[0], a[1] - b[1], a[2] - b[2]} }
func (a vec) mul(s float64) vec { return vec{a[0] * s, a[1] * s, a[2] * s} }
func (a vec) dot(b vec) float64 { return a[0]*b[0] + a[1]*b[1] + a[2]*b[2] }
func (a vec) cross(b vec) vec {
	return vec{a[1]*b[2] - a[2]*b[1], a[2]*b[0] - a[0]*b[2], a[0]*b[1] - a[1]*b[0]}
}
func (a vec) norm() float64   { return math.Sqrt(a.dot(a)) }
func (a vec) wire() *pb.Vec3  { return &pb.Vec3{Z: a[0], Y: a[1], X: a[2]} }
func fromWire(p *pb.Vec3) vec { return vec{p.GetZ(), p.GetY(), p.GetX()} }
func (a vec) finite() bool {
	for _, v := range a {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return false
		}
	}
	return true
}

func restPoints(offset float64) [5]vec {
	return [5]vec{{10, 10, 10 + offset}, {10, 30, 12 + offset}, {28, 18, 14 + offset}, {16, 20, 32 + offset}, {16, 19.5, 17 + offset}}
}

// Each matrix is the row-major outer product (v_j-c) * u_i^T / r_i.
// The host propagates one level; these twelve bindings intentionally form cycles.
func scaleBindings(points [5]vec) []*pb.LinearBinding {
	bindings := make([]*pb.LinearBinding, 0, 12)
	for i := 0; i < 4; i++ {
		radial := points[i].sub(points[4])
		radius := radial.norm()
		unit := radial.mul(1 / radius)
		for j := 0; j < 4; j++ {
			if i == j {
				continue
			}
			delta := points[j].sub(points[4])
			matrix := make([]float64, 9)
			for row := 0; row < 3; row++ {
				for col := 0; col < 3; col++ {
					matrix[3*row+col] = delta[row] * unit[col] / radius
				}
			}
			bindings = append(bindings, &pb.LinearBinding{Moved: int32(j), Target: int32(i), M: matrix})
		}
	}
	return bindings
}

func contributions() *pb.ContributeRois {
	out := &pb.ContributeRois{ReplaceAll: true}
	for i, spec := range []struct{ id, name string }{{"free", "Free corners"}, {"scale", "Scale"}, {"volume", "Keeps its volume"}, {"unshaped", "No shaper"}} {
		points := restPoints(float64(i) * 40)
		roi := &pb.Roi{TypeName: "tetra." + spec.id, Root: 4}
		for index, point := range points {
			vertex := &pb.Vertex{Position: point.wire()}
			if index < 4 {
				vertex.Enclosing = true
				h := &pb.HandleSpec{Frame: []float64{1, 0, 0, 0, 1, 0, 0, 0, 1}}
				if spec.id == "scale" || spec.id == "volume" {
					h.Mode = pb.DofMode_PRIMARY
					h.AxisSource = pb.AxisSource_AXIS_RADIAL
					h.ExtentAnchor = 4
					if spec.id == "scale" {
						minimum := 1.0
						h.MinExtent = &minimum
					}
				} else {
					h.Mode = pb.DofMode_ALL
					h.DofMask = 7
				}
				vertex.Handle = h
			}
			roi.Vertices = append(roi.Vertices, vertex)
		}
		if spec.id == "scale" {
			roi.AutoSurface = pb.AutoSurface_AUTO_SURFACE_STAR
			roi.Bindings = scaleBindings(points)
		}
		out.Rois = append(out.Rois, &pb.RoiContribution{ContribId: spec.id, Name: spec.name, Roi: roi})
	}
	return out
}

func rejectShape(message string) *pb.ShapeResult {
	return &pb.ShapeResult{Resolution: pb.Resolution_REJECTED, Message: message}
}

// volumeShape follows SPEC §3, using only this edit, for both previews and commits.
// Moving the corner changes height; scaling its opposite face by sqrt(h0/h1)
// preserves area*height and therefore tetrahedron volume. The center stays put.
func volumeShape(edit *pb.ShapeEdit) *pb.ShapeResult {
	move := edit.GetMove()
	h := int(move.GetHandle())
	if h < 0 || h > 3 {
		return rejectShape(fmt.Sprintf("vertex %d is not a corner", h))
	}
	vertices := move.GetRoi().GetVertices()
	if len(vertices) != 5 || move.GetP1() == nil {
		return rejectShape("expected five vertices and a target position")
	}
	var points [5]vec
	for i, v := range vertices {
		if v.GetPosition() == nil {
			return rejectShape("a vertex has no position")
		}
		points[i] = fromWire(v.Position)
		if !points[i].finite() {
			return rejectShape("positions must be finite")
		}
	}
	target := fromWire(move.P1)
	if !target.finite() {
		return rejectShape("target must be finite")
	}
	face := make([]int, 0, 3)
	for i := 0; i < 4; i++ {
		if i != h {
			face = append(face, i)
		}
	}
	a, b, d := points[face[0]], points[face[1]], points[face[2]]
	g := a.add(b).add(d).mul(1.0 / 3)
	normal := b.sub(a).cross(d.sub(a))
	length := normal.norm()
	if length == 0 || math.IsInf(length, 0) || math.IsNaN(length) {
		return rejectShape("the opposite face is degenerate")
	}
	normal = normal.mul(1 / length)
	s0, s1 := normal.dot(points[h].sub(g)), normal.dot(target.sub(g))
	if s0*s1 <= 0 || math.Abs(s1) < math.Abs(s0)/16 {
		return rejectShape("the corner would come too close to or cross the opposite face")
	}
	k := math.Sqrt(math.Abs(s0) / math.Abs(s1))
	result := &pb.ShapeResult{Resolution: pb.Resolution_OK}
	for i, p := range points {
		switch {
		case i == h:
			p = target
		case i < 4:
			p = g.add(p.sub(g).mul(k))
		}
		if !p.finite() {
			return rejectShape("the computed shape is not finite")
		}
		result.Positions = append(result.Positions, p.wire())
	}
	return result
}
