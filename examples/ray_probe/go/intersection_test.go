package main

import (
	"fmt"
	"math"
	"math/rand/v2"
	"testing"
)

// slabCells computes membership independently of the DDA: intersect the
// segment's parameter interval with each closed cube's three axis slabs.
// Values are the first meeting parameters, for checking traversal order too.
func slabCells(start, end point, clip box) map[cell]float64 {
	out := map[cell]float64{}
	for z := clip.Min[0]; z < clip.Max[0]; z++ {
		for y := clip.Min[1]; y < clip.Max[1]; y++ {
			for x := clip.Min[2]; x < clip.Max[2]; x++ {
				c := cell{z, y, x}
				enter, leave := 0.0, 1.0
				for a, k := range c {
					lo, hi := float64(k)-.5, float64(k)+.5
					d := end[a] - start[a]
					if d == 0 {
						if start[a] < lo || start[a] > hi {
							enter = math.Inf(1)
							break
						}
						continue
					}
					u, v := (lo-start[a])/d, (hi-start[a])/d
					enter = max(enter, min(u, v))
					leave = min(leave, max(u, v))
				}
				if enter <= leave+1e-12 {
					out[c] = enter
				}
			}
		}
	}
	return out
}

func checkSlabs(t *testing.T, start, end point, clip box) {
	t.Helper()
	want := slabCells(start, end, clip)
	got, err := traverse(start, end, clip)
	if len(want) == 0 {
		if err != errNothing || len(got) != 0 {
			t.Fatalf("empty intersection: %v, %v", got, err)
		}
		return
	}
	if err != nil {
		t.Fatal(err)
	}
	seen := map[cell]bool{}
	previous := 0.0
	for _, c := range got {
		parameter, ok := want[c]
		if !ok || seen[c] {
			t.Fatalf("unexpected or duplicate cell %v in %v", c, got)
		}
		if parameter < previous-1e-12 {
			t.Fatalf("first-contact order: %v at %.17g follows %.17g", c, parameter, previous)
		}
		previous = parameter
		seen[c] = true
	}
	for c := range want {
		if !seen[c] {
			t.Errorf("missing cell %v for %v -> %v, clip %v", c, start, end, clip)
		}
	}
}

func TestTraversalSlabRegressions(t *testing.T) {
	// Include the Python selftest's drift and multi-boundary cases.
	// Membership and order are independently computed, not copied DDA output.
	cases := [][2]point{
		{{0, 0, 0}, {1, 1, 0}},
		{{0, 0, 0}, {2.999, 2.999, 2.999}},
		{{.2, .3, .1}, {3.7, 2.9, 5.6}},
		{{.5, .5, .5}, {.5, .5, 2}},
		{{2, 0, 0}, {0, 2, 0}},
		{{0, 0, 0}, {7, 3, 5}},
		{{1, 2, 3}, {10, 5, 9}},
		{{0, 0, 0}, {9, 3, 3}},
		{{.5, .5, .5}, {12.5, 4.5, 8.5}},
		{{.5, .5, .5}, {.5, .5, .5}},
	}
	for i, pair := range cases {
		for _, reverse := range []bool{false, true} {
			t.Run(fmt.Sprintf("case_%d/reverse_%t", i, reverse), func(t *testing.T) {
				a, b := pair[0], pair[1]
				if reverse {
					a, b = b, a
				}
				checkSlabs(t, a, b, box{Min: cell{-1, -1, -1}, Max: cell{14, 14, 14}})
			})
		}
	}
}

func TestTraversalSlabsWithClipping(t *testing.T) {
	rng := rand.New(rand.NewPCG(22, 23))
	for i := range 400 {
		// Half-integer endpoints produce frequent exact faces, edges and corners.
		// The smaller crop also exercises new boundaries introduced by clipping.
		var a, b point
		for axis := range 3 {
			a[axis] = float64(rng.IntN(33)-4) / 2
			b[axis] = float64(rng.IntN(33)-4) / 2
		}
		t.Run(fmt.Sprint(i), func(t *testing.T) {
			checkSlabs(t, a, b, box{Min: cell{-3, -3, -3}, Max: cell{16, 16, 16}})
			checkSlabs(t, a, b, box{Min: cell{1, 2, 3}, Max: cell{8, 9, 10}})
		})
	}
}
