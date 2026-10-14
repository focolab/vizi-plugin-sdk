package main

import (
	"errors"
	"fmt"
	"math"
	"sort"
)

const maxComponent = 8_000_000
const liveMaxPoints = 512

type point struct{ y, x float64 }
type box struct{ lo, hi [3]int }

func (b box) shape() [3]int {
	return [3]int{b.hi[0] - b.lo[0], b.hi[1] - b.lo[1], b.hi[2] - b.lo[2]}
}

// Strict crossing test intentionally preserves SPEC §4's asymmetric edge rule.
func inside(path []point, p point) bool {
	yes := false
	for j, a := range path {
		b := path[(j+1)%len(path)]
		if (a.y > p.y) != (b.y > p.y) && p.x < a.x+(p.y-a.y)*(b.x-a.x)/(b.y-a.y) {
			yes = !yes
		}
	}
	return yes
}

func selection(path []point, shape [3]int) (box, []bool, error) {
	lo, hi := point{math.Inf(1), math.Inf(1)}, point{math.Inf(-1), math.Inf(-1)}
	for _, p := range path {
		lo.y, lo.x = math.Min(lo.y, p.y), math.Min(lo.x, p.x)
		hi.y, hi.x = math.Max(hi.y, p.y), math.Max(hi.x, p.x)
	}
	if len(path) < 3 {
		return box{}, nil, errors.New("Drag a closed loop around a region.")
	}
	// Clip before converting to integers so far-outside loops stay bounded.
	y0, x0 := int(math.Max(0, math.Min(float64(shape[1]), math.Floor(lo.y)))), int(math.Max(0, math.Min(float64(shape[2]), math.Floor(lo.x))))
	y1, x1 := int(math.Max(0, math.Min(float64(shape[1]), math.Ceil(hi.y)))), int(math.Max(0, math.Min(float64(shape[2]), math.Ceil(hi.x))))
	selected := make([][2]int, 0)
	sy0, sx0, sy1, sx1 := shape[1], shape[2], 0, 0
	for y := y0; y < y1; y++ {
		for x := x0; x < x1; x++ {
			if inside(path, point{float64(y) + .5, float64(x) + .5}) {
				selected = append(selected, [2]int{y, x})
				sy0, sx0, sy1, sx1 = min(sy0, y), min(sx0, x), max(sy1, y+1), max(sx1, x+1)
			}
		}
	}
	if len(selected) == 0 {
		return box{}, nil, errors.New("Drag a closed loop around a region.")
	}
	my, mx := max(8, int(math.RoundToEven(.25*float64(sy1-sy0)))), max(8, int(math.RoundToEven(.25*float64(sx1-sx0))))
	w := box{[3]int{0, max(0, sy0-my), max(0, sx0-mx)}, [3]int{shape[0], min(shape[1], sy1+my), min(shape[2], sx1+mx)}}
	s := w.shape()
	mask := make([]bool, s[1]*s[2])
	for _, p := range selected {
		mask[(p[0]-w.lo[1])*s[2]+p[1]-w.lo[2]] = true
	}
	return w, mask, nil
}

func decimate(path []point) []point {
	k := min(len(path), liveMaxPoints)
	if k <= 1 {
		return append([]point(nil), path...)
	}
	out := make([]point, 0, k)
	last := -1
	for i := range k {
		j := int(math.RoundToEven(float64(i) * float64(len(path)-1) / float64(k-1)))
		if j != last {
			out = append(out, path[j])
			last = j
		}
	}
	return out
}

func threshold(data []float64, shape [3]int, selected []bool, scale float64) (float64, error) {
	if scale <= 0 || math.IsNaN(scale) || math.IsInf(scale, 0) {
		return 0, errors.New("threshold scale must be positive and finite")
	}
	maxima := make([]float64, 0)
	stride := shape[1] * shape[2]
	for i, yes := range selected {
		if !yes {
			continue
		}
		m := math.Inf(-1)
		for z := range shape[0] {
			v := data[z*stride+i]
			if v > m { // NaN and -Inf never contribute; +Inf does.
				m = v
			}
		}
		if !math.IsInf(m, -1) {
			maxima = append(maxima, m)
		}
	}
	if len(maxima) == 0 {
		return 0, errors.New("Nothing measurable under the loop.")
	}
	sort.Float64s(maxima)
	m := maxima[len(maxima)/2]
	if len(maxima)%2 == 0 {
		m = maxima[len(maxima)/2-1]/2 + m/2
	}
	return scale * m, nil
}

// diagnostic ignores NaN and searches selected columns only, preserving raster ties.
func diagnostic(data []float64, shape [3]int, selected []bool) int {
	best, value := -1, math.Inf(-1)
	for i, v := range data {
		if selected[i%(shape[1]*shape[2])] && !math.IsNaN(v) && (best < 0 || v > value) {
			best, value = i, v
		}
	}
	return best / (shape[1] * shape[2])
}

type component struct {
	voxels    [][3]int // absolute dataset coordinates
	threshold float64
	clipped   bool
}

func grow(data []float64, w box, volume [3]int, selected []bool, zg, minSize int, scale float64, grow3D bool) (component, error) {
	s := w.shape()
	if len(data) != s[0]*s[1]*s[2] || len(selected) != s[1]*s[2] || zg < 0 || zg >= s[0] {
		return component{}, errors.New("invalid segmentation volume or anchor")
	}
	t, err := threshold(data, s, selected, scale)
	if err != nil {
		return component{}, err
	}
	seen := make([]bool, len(data))
	var best []int
	stride := s[1] * s[2]
	var queue []int
	for start, v := range data {
		if seen[start] || !(v >= t) || (!grow3D && start/stride != zg) {
			continue
		}
		queue = append(queue[:0], start)
		seen[start] = true
		anchored := false
		for head := 0; head < len(queue); head++ {
			i := queue[head]
			z, y, x := i/stride, i/s[2]%s[1], i%s[2]
			anchored = anchored || (z == zg && selected[i%stride])
			visit := func(j int) {
				if !seen[j] && data[j] >= t {
					seen[j] = true
					queue = append(queue, j)
				}
			}
			if x > 0 {
				visit(i - 1)
			}
			if x+1 < s[2] {
				visit(i + 1)
			}
			if y > 0 {
				visit(i - s[2])
			}
			if y+1 < s[1] {
				visit(i + s[2])
			}
			if grow3D && z > 0 {
				visit(i - stride)
			}
			if grow3D && z+1 < s[0] {
				visit(i + stride)
			}
		}
		if anchored && len(queue) > len(best) {
			best = append(best[:0], queue...)
		}
	}
	if len(best) == 0 {
		return component{}, fmt.Errorf("Nothing above the threshold on plane %d. The brightest voxel you enclosed is at z=%d — scroll there, or lower Threshold ×.", zg, diagnostic(data, s, selected))
	}
	if len(best) < minSize {
		return component{}, fmt.Errorf("No component of at least %d voxels.", minSize)
	}
	if len(best) > maxComponent {
		return component{}, errors.New("Selection too permissive — try a tighter loop or a higher Threshold ×.")
	}
	c := component{threshold: t, voxels: make([][3]int, len(best))}
	for j, i := range best {
		p := [3]int{i/stride + w.lo[0], i/s[2]%s[1] + w.lo[1], i%s[2] + w.lo[2]}
		c.voxels[j] = p
		for a := range 3 {
			if (p[a] == w.lo[a] && w.lo[a] > 0) || (p[a] == w.hi[a]-1 && w.hi[a] < volume[a]) {
				c.clipped = true
			}
		}
	}
	return c, nil
}

type labelMap struct {
	voxels map[[3]int]byte
	next   int
}

func (m labelMap) adding(c component, replace bool) (labelMap, int, error) {
	n := labelMap{voxels: make(map[[3]int]byte), next: 1}
	if !replace {
		for p, id := range m.voxels {
			n.voxels[p] = id
		}
		if m.next > 0 {
			n.next = m.next
		}
	}
	if n.next > 255 {
		return m, 0, errors.New("At most 255 labels — commit or clear first.")
	}
	id := n.next
	n.next++
	for _, p := range c.voxels {
		n.voxels[p] = byte(id)
	}
	return n, id, nil
}

func (m labelMap) render() ([]byte, box, map[uint32]string, error) {
	if len(m.voxels) == 0 {
		return nil, box{}, nil, errors.New("No segmentation to commit.")
	}
	b := box{lo: [3]int{math.MaxInt, math.MaxInt, math.MaxInt}}
	names := make(map[uint32]string)
	for p, id := range m.voxels {
		for a := range 3 {
			b.lo[a], b.hi[a] = min(b.lo[a], p[a]), max(b.hi[a], p[a]+1)
		}
		names[uint32(id)] = fmt.Sprintf("Region %d", id)
	}
	s := b.shape()
	// The declared 64 MiB arena cannot hold a larger single label array.
	if uint64(s[0])*uint64(s[1])*uint64(s[2]) > 64<<20 {
		return nil, box{}, nil, errors.New("Label bounding box exceeds the 64 MiB overlay arena.")
	}
	data := make([]byte, s[0]*s[1]*s[2])
	for p, id := range m.voxels {
		data[((p[0]-b.lo[0])*s[1]+p[1]-b.lo[1])*s[2]+p[2]-b.lo[2]] = id
	}
	return data, b, names, nil
}

func labelLUT() []byte {
	palette := [8][3]byte{{230, 25, 75}, {60, 180, 75}, {67, 99, 216}, {245, 130, 49}, {145, 30, 180}, {66, 212, 244}, {240, 50, 230}, {191, 239, 69}}
	data := make([]byte, 1024)
	for i := 1; i < 256; i++ {
		copy(data[4*i:], palette[(i-1)%8][:])
		data[4*i+3] = 180
	}
	return data
}
