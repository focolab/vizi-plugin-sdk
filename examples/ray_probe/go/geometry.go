package main

import (
	"errors"
	"math"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type point = geometry.ZYX
type cell [3]int64
type box = geometry.IndexBox

const maxVoxels = 1 << 26

var errNothing = errors.New("Nothing under the pointer.")

func finite(v float64) bool { return !math.IsNaN(v) && !math.IsInf(v, 0) }
func level0(p, scale point) point {
	for a := range p {
		p[a] = (p[a]+.5)*scale[a] - .5
	}
	return p
}
func levelGeometry(g *pb.Geometry, level uint8) (point, box, box, error) {
	if g == nil || g.Extent == nil || int(level) >= len(g.Levels) || g.Levels[level] == nil {
		return point{}, box{}, box{}, errors.New("Level geometry is unavailable.")
	}
	s := g.Levels[level]
	scale := point{s.Z, s.Y, s.X}
	e := g.Extent
	extent := box{Min: [3]int64{int64(e.Z0), int64(e.Y0), int64(e.X0)}, Max: [3]int64{int64(e.ZN), int64(e.YN), int64(e.XN)}}
	shape := box{}
	for a, s := range scale {
		if !finite(s) || s <= 0 {
			return point{}, box{}, box{}, errors.New("Invalid sampling factor.")
		}
		shape.Min[a] = int64(math.Ceil(float64(extent.Min[a]) / s))
		shape.Max[a] = max(shape.Min[a]+1, int64(math.Floor(float64(extent.Max[a])/s)))
	}
	crop, err := geometry.CropAtLevel(g, uint32(level))
	if err != nil {
		return point{}, box{}, box{}, err
	}
	crop, err = crop.Intersect(shape)
	return scale, crop, extent, err
}
func contains(b box, c cell) bool {
	for a, v := range c {
		if v < b.Min[a] || v >= b.Max[a] {
			return false
		}
	}
	return true
}
func bounded(b box) error {
	if b.Empty() {
		return errNothing
	}
	n := int64(1)
	for a := range 3 {
		d := b.Max[a] - b.Min[a]
		if d > maxVoxels/n {
			return errors.New("Too large: the read exceeds 2^26 voxels.")
		}
		n *= d
	}
	return nil
}
func bounds(cells []cell) box {
	b := box{Min: cells[0], Max: cells[0]}
	for _, c := range cells {
		for a, v := range c {
			b.Min[a] = min(b.Min[a], v)
			b.Max[a] = max(b.Max[a], v)
		}
	}
	for a := range 3 {
		b.Max[a]++
	}
	return b
}
func blockOf(c cell, scale point, extent box) (box, error) {
	b := box{}
	for a, j := range c {
		b.Min[a] = int64(math.Ceil(float64(j) * scale[a]))
		b.Max[a] = int64(math.Ceil(float64(j+1) * scale[a]))
	}
	b, err := b.Intersect(extent)
	if err != nil {
		return b, err
	}
	return b, bounded(b)
}

// segment uses the record's level unchanged. A column joins its first and
// last included centers; rays use their entry fraction and ignore picked.
func segment(p doorbell.Position) (start, end point, ok bool) {
	v := p.Values
	switch p.Kind {
	case doorbell.Ray, doorbell.RayAndPicked:
		for a := range 3 {
			end[a] = float64(v[a+3])
			start[a] = float64(v[a]) + float64(v[6])*(end[a]-float64(v[a]))
		}
		return start, end, true
	case doorbell.ColumnYX, doorbell.ColumnZX, doorbell.ColumnZY:
		free, fixed := columnAxes(p.Kind)
		start[fixed[0]], start[fixed[1]] = float64(v[0]), float64(v[1])
		end = start
		start[free], end[free] = math.Ceil(float64(v[2])), math.Ceil(float64(v[3]))-1
		return start, end, start[free] <= end[free]
	}
	return
}
func columnAxes(k doorbell.PositionKind) (int, [2]int) {
	switch k {
	case doorbell.ColumnYX:
		return 0, [2]int{1, 2}
	case doorbell.ColumnZX:
		return 1, [2]int{0, 2}
	default:
		return 2, [2]int{0, 1}
	}
}
func candidates(p doorbell.Position, clip box) ([]cell, error) {
	if p.Kind == doorbell.None || p.Flags&doorbell.Miss != 0 || clip.Empty() {
		return nil, errNothing
	}
	var out []cell
	switch p.Kind {
	case doorbell.Voxel, doorbell.Picked:
		c := cell{}
		for a := range 3 {
			c[a] = int64(math.Floor(float64(p.Values[a]) + .5))
		}
		if contains(clip, c) {
			out = append(out, c)
		}
	case doorbell.ColumnYX, doorbell.ColumnZX, doorbell.ColumnZY:
		free, fixed := columnAxes(p.Kind)
		c := cell{}
		for i, a := range fixed {
			c[a] = int64(math.Floor(float64(p.Values[i]) + .5))
			if c[a] < clip.Min[a] || c[a] >= clip.Max[a] {
				return nil, errNothing
			}
		}
		lo, hi := max(clip.Min[free], int64(math.Ceil(float64(p.Values[2])))), min(clip.Max[free], int64(math.Ceil(float64(p.Values[3]))))
		if hi-lo > maxVoxels {
			return nil, errors.New("Too large: column exceeds 2^26 voxels.")
		}
		for i := lo; i < hi; i++ {
			c[free] = i
			out = append(out, c)
		}
	case doorbell.Ray, doorbell.RayAndPicked:
		s, e, _ := segment(p)
		return traverse(s, e, clip)
	}
	if len(out) == 0 {
		return nil, errNothing
	}
	return out, nil
}

// traverse is a supercover DDA of a closed segment. Simultaneous crossings
// emit all adjacent cells, including cells touched only at an edge or corner.
// A boundary-parallel axis emits both sides throughout. Equal-distance cells
// are ordered lexicographically; all others stay in camera-to-far order.
func traverse(start, end point, clip box) ([]cell, error) {
	if clip.Empty() {
		return nil, errNothing
	}
	d := point{}
	t0, t1 := 0.0, 1.0
	for a := range 3 {
		if !finite(start[a]) || !finite(end[a]) {
			return nil, errors.New("Nonfinite segment.")
		}
		d[a] = end[a] - start[a]
		lo, hi := float64(clip.Min[a])-.5, float64(clip.Max[a])-.5
		if d[a] == 0 {
			if start[a] < lo || start[a] > hi {
				return nil, errNothing
			}
			continue
		}
		u, v := (lo-start[a])/d[a], (hi-start[a])/d[a]
		if u > v {
			u, v = v, u
		}
		t0 = max(t0, u)
		t1 = min(t1, v)
	}
	if t0 > t1 {
		return nil, errNothing
	}
	for a := range 3 {
		end[a] = start[a] + t1*d[a]
		start[a] += t0 * d[a]
		d[a] = end[a] - start[a]
	}
	// The box is also the bound on allocation/traversal work; check it before
	// walking a potentially enormous record. Closed endpoints include both sides.
	b := box{}
	for a := range 3 {
		b.Min[a] = max(clip.Min[a], int64(math.Ceil(min(start[a], end[a])-.5)))
		b.Max[a] = min(clip.Max[a], int64(math.Floor(max(start[a], end[a])+.5))+1)
	}
	if err := bounded(b); err != nil {
		return nil, err
	}
	c := cell{}
	step := cell{}
	next := point{}
	stationary := []int{}
	for a := range 3 {
		c[a] = int64(math.Floor(start[a] + .5))
		next[a] = math.Inf(1)
		if d[a] < 0 {
			step[a] = -1
			if start[a]+.5 == math.Floor(start[a]+.5) {
				c[a]--
			}
		} else if d[a] > 0 {
			step[a] = 1
		} else if start[a]+.5 == math.Floor(start[a]+.5) {
			stationary = append(stationary, a)
		}
		if step[a] != 0 {
			next[a] = (float64(c[a]) + .5*float64(step[a]) - start[a]) / d[a]
		}
	}
	out := []cell{}
	seen := map[cell]bool{}
	emit := func(cells []cell) {
		for _, a := range stationary {
			base := slices.Clone(cells)
			for _, v := range base {
				v[a]--
				cells = append(cells, v)
			}
		}
		slices.SortFunc(cells, func(a, b cell) int {
			for i := range 3 {
				if a[i] < b[i] {
					return -1
				}
				if a[i] > b[i] {
					return 1
				}
			}
			return 0
		})
		for _, v := range cells {
			if contains(clip, v) && !seen[v] {
				seen[v] = true
				out = append(out, v)
			}
		}
	}
	initial := []cell{c}
	for a := range 3 {
		if step[a] != 0 && start[a]+.5 == math.Floor(start[a]+.5) {
			base := slices.Clone(initial)
			for _, v := range base {
				v[a] -= step[a]
				initial = append(initial, v)
			}
		}
	}
	emit(initial)
	for {
		u := min(next[0], next[1], next[2])
		if u > 1 || math.IsInf(u, 0) {
			break
		}
		crossing := []cell{c}
		for a := range 3 {
			if math.Abs(next[a]-u) > 1e-12 {
				continue
			}
			base := slices.Clone(crossing)
			for _, v := range base {
				v[a] += step[a]
				crossing = append(crossing, v)
			}
			c[a] += step[a]
			next[a] = (float64(c[a]) + .5*float64(step[a]) - start[a]) / d[a]
		}
		emit(crossing)
	}
	if len(out) == 0 {
		return nil, errNothing
	}
	return out, nil
}
