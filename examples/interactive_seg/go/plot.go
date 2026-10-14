package main

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/draw"
	"image/png"
	"math"
)

// A small raster chart keeps this example free of plotting dependencies.
// Nonfinite means are gaps, not invented values; axes use finite observations.
func plotMeans(means []float64) ([]byte, error) {
	im := image.NewRGBA(image.Rect(0, 0, 640, 320))
	draw.Draw(im, im.Bounds(), &image.Uniform{color.White}, image.Point{}, draw.Src)
	ink := color.RGBA{35, 40, 50, 255}
	blue := color.RGBA{40, 100, 210, 255}
	line := func(x0, y0, x1, y1 int, c color.RGBA) {
		n := max(abs(x1-x0), abs(y1-y0))
		for i := 0; i <= n; i++ {
			f := float64(i) / float64(max(1, n))
			im.SetRGBA(x0+int(math.Round(f*float64(x1-x0))), y0+int(math.Round(f*float64(y1-y0))), c)
		}
	}
	text := func(x, y int, s string) {
		for _, c := range s {
			rows := glyphs[c]
			for j, row := range rows {
				for k := 0; k < 3; k++ {
					if row&(1<<(2-k)) != 0 {
						draw.Draw(im, image.Rect(x+2*k, y+2*j, x+2*k+2, y+2*j+2), &image.Uniform{ink}, image.Point{}, draw.Src)
					}
				}
			}
			x += 8
		}
	}
	lo, hi := math.Inf(1), math.Inf(-1)
	for _, v := range means {
		if !math.IsNaN(v) && !math.IsInf(v, 0) {
			lo = math.Min(lo, v)
			hi = math.Max(hi, v)
		}
	}
	if math.IsInf(lo, 0) {
		lo, hi = 0, 1
	}
	if hi == lo {
		hi = lo + 1
	}
	line(80, 25, 80, 270, ink)
	line(80, 270, 610, 270, ink)
	text(12, 5, "MEAN")
	text(290, 296, "TIME INDEX")
	text(4, 28, fmt.Sprintf("%.2G", hi))
	text(4, 251, fmt.Sprintf("%.2G", lo))
	text(80, 278, "0")
	text(580, 278, fmt.Sprint(max(0, len(means)-1)))
	lastX, lastY, valid := 0, 0, false
	for t, v := range means {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			valid = false
			continue
		}
		x := 80 + int(530*float64(t)/float64(max(1, len(means)-1)))
		y := 270 - int(245*(v-lo)/(hi-lo))
		if valid {
			line(lastX, lastY, x, y, blue)
		}
		draw.Draw(im, image.Rect(x-2, y-2, x+3, y+3), &image.Uniform{blue}, image.Point{}, draw.Src)
		lastX, lastY, valid = x, y, true
	}
	var out bytes.Buffer
	err := png.Encode(&out, im)
	return out.Bytes(), err
}
func abs(n int) int {
	if n < 0 {
		return -n
	}
	return n
}

var glyphs = map[rune][5]byte{
	'0': {7, 5, 5, 5, 7}, '1': {2, 6, 2, 2, 7}, '2': {7, 1, 7, 4, 7}, '3': {7, 1, 7, 1, 7}, '4': {5, 5, 7, 1, 1}, '5': {7, 4, 7, 1, 7}, '6': {7, 4, 7, 5, 7}, '7': {7, 1, 1, 1, 1}, '8': {7, 5, 7, 5, 7}, '9': {7, 5, 7, 1, 7}, '.': {0, 0, 0, 0, 2}, '-': {0, 0, 7, 0, 0}, '+': {0, 2, 7, 2, 0},
	'M': {5, 7, 7, 5, 5}, 'E': {7, 4, 7, 4, 7}, 'A': {2, 5, 7, 5, 5}, 'N': {5, 7, 7, 7, 5}, 'T': {7, 2, 2, 2, 2}, 'I': {7, 2, 2, 2, 7}, 'D': {6, 5, 5, 5, 6}, 'X': {5, 5, 2, 5, 5},
}
