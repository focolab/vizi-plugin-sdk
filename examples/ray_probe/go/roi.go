package main

import (
	"math"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// ellipsoid uses the four-control representation supplied by SPEC.md.
// Display points exist only in the compile operator, never in Roi.Vertices.
func ellipsoid(c point, radius float64) *pb.Roi {
	r := &pb.Roi{TypeName: "ellipsoid", Compile: &pb.CompileOperator{Controls: []int32{0, 1, 2, 3}, NumDisplay: 18}}
	for i := 0; i < 4; i++ {
		p := c
		if i > 0 {
			p[i-1] += radius
		}
		r.Vertices = append(r.Vertices, &pb.Vertex{Position: &pb.Vec3{Z: p[0], Y: p[1], X: p[2]}})
	}
	var rows [][3]float64
	for a := 0; a < 3; a++ {
		for _, sign := range []float64{1, -1} {
			var row [3]float64
			row[a] = sign
			rows = append(rows, row)
		}
	}
	for _, pair := range [][2]int{{0, 1}, {0, 2}, {1, 2}} {
		a, b := pair[0], pair[1]
		tips := []int32{int32(2 * a), int32(2 * b), int32(2*a + 1), int32(2*b + 1)}
		for i, signs := range [][2]float64{{1, 1}, {-1, 1}, {-1, -1}, {1, -1}} {
			var row [3]float64
			row[a], row[b] = signs[0], signs[1]
			r.Edges = append(r.Edges, &pb.Edge{Source: tips[i], Target: tips[(i+1)%4], Control: int32(len(rows)), Weight: math.Sqrt2 / 2})
			rows = append(rows, row)
		}
	}
	for i, row := range rows {
		for column, coeff := range [4]float64{1 - row[0] - row[1] - row[2], row[0], row[1], row[2]} {
			if coeff != 0 {
				r.Compile.Triplets = append(r.Compile.Triplets, &pb.CompileTriplet{DisplayVertex: int32(i), Control: int32(column), Coeff: coeff})
			}
		}
	}
	return r
}
