package main

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
	"strings"
)

func sampleIndices(n int) []int {
	if n <= 0 {
		return nil
	}
	k := min(n, 1024)
	out := make([]int, k)
	for i := range out {
		out[i] = i * (n - 1) / max(1, k-1)
	}
	return out
}
func plotProfile(values, dist []float64, best int, axis string) ([]byte, error) {
	im := image.NewRGBA(image.Rect(0, 0, 480, 240))
	draw.Draw(im, im.Bounds(), &image.Uniform{color.White}, image.Point{}, draw.Src)
	ink, blue, orange := color.RGBA{30, 40, 50, 255}, color.RGBA{35, 100, 190, 255}, color.RGBA{240, 110, 0, 255}
	line := func(x0, y0, x1, y1 int, c color.RGBA) {
		n := max(int(math.Abs(float64(x1-x0))), int(math.Abs(float64(y1-y0))))
		for i := 0; i <= n; i++ {
			f := float64(i) / float64(max(1, n))
			im.SetRGBA(x0+int(math.Round(f*float64(x1-x0))), y0+int(math.Round(f*float64(y1-y0))), c)
		}
	}
	text := func(x, y int, s string) {
		for _, r := range strings.ToUpper(s) {
			for j, row := range glyphs[r] {
				for k := range 3 {
					if row&(1<<(2-k)) != 0 {
						im.SetRGBA(x+k, y+j, ink)
					}
				}
			}
			x += 5
		}
	}
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, v := range values {
		if finite(v) {
			lo = min(lo, v)
			hi = max(hi, v)
		}
	}
	if !finite(lo) {
		lo, hi = 0, 1
	}
	// Normalize before subtraction so even opposite finite F64 extremes
	// cannot overflow the range. A constant profile sits halfway up the axis.
	magnitude := max(math.Abs(lo), math.Abs(hi))
	if magnitude == 0 {
		magnitude = 1
	}
	low, span := lo/magnitude, hi/magnitude-lo/magnitude
	last := dist[len(dist)-1]
	if last == 0 {
		last = 1
	}
	px := func(i int) int { return 65 + int(math.Round(390*dist[i]/last)) }
	py := func(v float64) int {
		if math.IsInf(v, 1) {
			return 20
		}
		if math.IsInf(v, -1) {
			return 195
		}
		if span == 0 {
			return 108
		}
		return 195 - int(math.Round(175*(v/magnitude-low)/span))
	}
	line(65, 20, 65, 195, ink)
	line(65, 195, 455, 195, ink)
	text(5, 20, fmt.Sprintf("%.3G", hi))
	text(5, 190, fmt.Sprintf("%.3G", lo))
	text(65, 205, "0")
	text(400, 205, fmt.Sprintf("%.3G", dist[len(dist)-1]))
	text(160, 225, axis)
	prev := -1
	for _, i := range sampleIndices(len(values)) {
		if math.IsNaN(values[i]) {
			prev = -1
			continue
		}
		if prev >= 0 {
			line(px(prev), py(values[prev]), px(i), py(values[i]), blue)
		}
		im.SetRGBA(px(i), py(values[i]), blue)
		prev = i
	}
	if best >= 0 && best < len(values) {
		x, y := px(best), py(values[best])
		line(x, 20, x, 195, orange)
		draw.Draw(im, image.Rect(x-3, y-3, x+4, y+4), &image.Uniform{orange}, image.Point{}, draw.Src)
	}
	var out bytes.Buffer
	err := png.Encode(&out, im)
	return out.Bytes(), err
}

func init() {
	for r, g := range map[rune][5]byte{'S': {7, 4, 7, 1, 7}, 'C': {7, 4, 4, 4, 7}, 'L': {4, 4, 4, 4, 7}, 'V': {5, 5, 5, 5, 2}, 'O': {7, 5, 5, 5, 7}, 'U': {5, 5, 5, 5, 7}, '(': {1, 2, 2, 2, 1}, ')': {4, 2, 2, 2, 4}, 'R': {6, 5, 6, 5, 5}} {
		glyphs[r] = g
	}
}
