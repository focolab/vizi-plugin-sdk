package main

import (
	"math"
	"reflect"
	"strings"
	"testing"
)

func TestPolygon(t *testing.T) {
	square := []point{{0, 0}, {0, 4}, {4, 4}, {4, 0}}
	concave := []point{{0, 0}, {0, 4}, {1, 4}, {1, 1}, {4, 1}, {4, 0}}
	bow := []point{{0, 0}, {4, 4}, {0, 4}, {4, 0}}
	for _, c := range []struct {
		name string
		path []point
		p    point
		want bool
	}{
		{"convex inside", square, point{2, 2}, true}, {"outside", square, point{2, 5}, false},
		{"left edge", square, point{2, 0}, true}, {"right edge", square, point{2, 4}, false},
		{"concave arm", concave, point{3, .5}, true}, {"concave notch", concave, point{3, 3}, false},
		{"crossing left lobe", bow, point{2, .5}, true}, {"crossing upper gap", bow, point{.5, 2}, false},
	} {
		t.Run(c.name, func(t *testing.T) {
			if got := inside(c.path, c.p); got != c.want {
				t.Fatalf("got %v, want %v", got, c.want)
			}
		})
	}
	w, s, err := selection([]point{{-10, -10}, {-10, 2}, {2, 2}, {2, -10}}, [3]int{2, 4, 4})
	if err != nil || w != (box{[3]int{0, 0, 0}, [3]int{2, 4, 4}}) {
		t.Fatal(w, err)
	}
	n := 0
	for _, yes := range s {
		if yes {
			n++
		}
	}
	if n != 4 {
		t.Fatal(n)
	}
	_, _, err = selection([]point{{1, 1}, {1, 1}}, [3]int{1, 5, 5})
	if err == nil {
		t.Fatal("click accepted")
	}
	_, _, err = selection([]point{{-3, -3}, {-3, -1}, {-1, -1}, {-1, -3}}, [3]int{1, 5, 5})
	if err == nil {
		t.Fatal("outside loop accepted")
	}
}

func TestThreshold(t *testing.T) {
	for _, c := range []struct {
		name    string
		data    []float64
		shape   [3]int
		want    float64
		refusal bool
	}{
		{"even column maxima", []float64{1, 8, 3, 4, 5, 2, 7, 6}, [3]int{2, 2, 2}, 6.5, false},
		{"odd", []float64{2, 6, 9}, [3]int{1, 1, 3}, 6, false},
		{"drop NaN column", []float64{math.NaN(), 2, 8, math.NaN(), 4, 6}, [3]int{2, 1, 3}, 6, false},
		{"all NaN", []float64{math.NaN()}, [3]int{1, 1, 1}, 0, true},
		{"resolved all negative infinity", []float64{math.Inf(-1)}, [3]int{1, 1, 1}, 0, true},
		{"resolved mixed infinity", []float64{math.Inf(-1), math.Inf(1)}, [3]int{1, 1, 2}, math.Inf(1), false},
	} {
		t.Run(c.name, func(t *testing.T) {
			s := make([]bool, c.shape[1]*c.shape[2])
			for i := range s {
				s[i] = true
			}
			got, err := threshold(c.data, c.shape, s, 1)
			if c.refusal {
				if err == nil || err.Error() != "Nothing measurable under the loop." {
					t.Fatal(err)
				}
				return
			}
			if err != nil || got != c.want {
				t.Fatal(got, err)
			}
			got, err = threshold(c.data, c.shape, s, 2)
			if err != nil || got != 2*c.want {
				t.Fatal(got, err)
			}
		})
	}
}

func TestComponents(t *testing.T) {
	shape := [3]int{2, 4, 5}
	w := box{hi: shape}
	data := make([]float64, 40)
	// A brighter isolated voxel is the diagnostic maximum; the dimmer three
	// voxel component is larger and wins. Edge/corner contacts cannot join them.
	for i, v := range map[int]float64{0: 20, 6: 10, 7: 10, 27: 10} {
		data[i] = v
	}
	s := make([]bool, 20)
	s[0], s[6], s[7] = true, true, true
	for _, c := range []struct {
		three bool
		count int
	}{{true, 3}, {false, 2}} {
		got, err := grow(data, w, shape, s, 0, 1, 1, c.three)
		if err != nil || len(got.voxels) != c.count {
			t.Fatal(got, err)
		}
		for _, p := range got.voxels {
			if p == ([3]int{}) {
				t.Fatal("selected brightest instead of largest")
			}
		}
	}
	if diagnostic(data, shape, s) != 0 {
		t.Fatal("diagnostic")
	}
	_, err := grow(data, w, shape, s, 1, 1, 2, true)
	if err == nil || !strings.Contains(err.Error(), "plane 1") || !strings.Contains(err.Error(), "z=0") {
		t.Fatal(err)
	}
	_, err = grow(data, w, shape, s, 0, 4, 1, true)
	if err == nil || err.Error() != "No component of at least 4 voxels." {
		t.Fatal(err)
	}
}

func TestConnectivityTiesAndInfinity(t *testing.T) {
	shape := [3]int{2, 2, 2}
	w := box{hi: shape}
	s := []bool{true, true, true, true}
	for _, contact := range []int{3, 6, 7} {
		data := make([]float64, 8)
		data[0], data[contact] = 10, 10
		// Use selected columns only for the two peaks to avoid a background median.
		selected := make([]bool, 4)
		selected[0], selected[contact%4] = true, true
		got, err := grow(data, w, shape, selected, 0, 1, 1, true)
		if err != nil || len(got.voxels) != 1 || got.voxels[0] != ([3]int{}) {
			t.Fatal(contact, got, err)
		}
	}
	data := []float64{math.Inf(-1), math.Inf(1), math.NaN(), 0, 0, 0, 0, 0}
	got, err := grow(data, w, shape, s, 0, 1, 1, true)
	// Median is 0, but NaN and -Inf are still excluded.
	if err != nil {
		t.Fatal(err)
	}
	for _, p := range got.voxels {
		if p == ([3]int{}) || p == ([3]int{0, 1, 0}) {
			t.Fatal(p)
		}
	}
	got, err = grow([]float64{math.Inf(-1), math.Inf(1)}, box{hi: [3]int{1, 1, 2}}, [3]int{1, 1, 2}, []bool{true, true}, 0, 1, 1, true)
	if err != nil || !reflect.DeepEqual(got.voxels, [][3]int{{0, 0, 1}}) {
		t.Fatal(got, err)
	}
}

func TestWindowClipAndLabels(t *testing.T) {
	w := box{[3]int{0, 2, 2}, [3]int{1, 3, 4}}
	c, err := grow([]float64{5, 5}, w, [3]int{1, 8, 8}, []bool{true, true}, 0, 1, 1, true)
	if err != nil || !c.clipped {
		t.Fatal(c, err)
	}
	m, id, err := (labelMap{}).adding(c, false)
	if err != nil || id != 1 {
		t.Fatal(id, err)
	}
	m, id, err = m.adding(component{voxels: [][3]int{{0, 2, 3}, {0, 3, 3}}}, false)
	if err != nil || id != 2 {
		t.Fatal(id, err)
	}
	data, b, names, err := m.render()
	if err != nil || b != (box{[3]int{0, 2, 2}, [3]int{1, 4, 4}}) || !reflect.DeepEqual(data, []byte{1, 2, 0, 2}) || names[2] != "Region 2" {
		t.Fatal(data, b, names, err)
	}
	m, id, err = m.adding(component{voxels: [][3]int{{0, 3, 3}}}, true)
	if err != nil || id != 1 || len(m.voxels) != 1 {
		t.Fatal(m, id, err)
	}
	m.next = 256
	if _, _, err = m.adding(c, false); err == nil {
		t.Fatal("label cap")
	}
	lut := labelLUT()
	if len(lut) != 1024 || !reflect.DeepEqual(lut[:8], []byte{0, 0, 0, 0, 230, 25, 75, 180}) || lut[1023] != 180 {
		t.Fatal(lut[:8])
	}
}

func TestDecimation(t *testing.T) {
	path := make([]point, 10001)
	for i := range path {
		path[i] = point{float64(i), 0}
	}
	p := decimate(path)
	if len(p) != 512 || p[0] != path[0] || p[511] != path[10000] || p[1] != path[20] || len(path) != 10001 {
		t.Fatal(len(p), p[:2], p[len(p)-1])
	}
}
