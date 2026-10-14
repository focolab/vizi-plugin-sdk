package main

import (
	"bytes"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"math/big"
	"math/bits"
	"slices"
	"strconv"
	"strings"
)

type measurement struct{ count, sum uint64 }
type row struct {
	id, name   string
	unmeasured bool
	measurement
}

func (r row) display() string {
	if r.name != "" {
		return r.name
	}
	return r.id
}

func meanText(m measurement) string {
	if m.count == 0 {
		return ""
	}
	// The sum fits u64; its scaled intermediate need not.
	n := new(big.Int).SetUint64(m.sum)
	n.Mul(n, big.NewInt(2000)).Add(n, new(big.Int).SetUint64(m.count))
	d := new(big.Int).SetUint64(m.count)
	d.Lsh(d, 1)
	n.Quo(n, d)
	whole, fraction := new(big.Int), new(big.Int)
	whole.QuoRem(n, big.NewInt(1000), fraction)
	return fmt.Sprintf("%s.%03d", whole.String(), fraction.Uint64())
}
func swatchIndex(m measurement) int {
	if m.count == 0 {
		return 0
	}
	return int(min(uint64(255), (m.sum/m.count)>>8))
}
func ordered(rows map[string]row) []row {
	out := make([]row, 0, len(rows))
	for _, r := range rows {
		out = append(out, r)
	}
	// UTF-8 byte order preserves code-point order for valid UTF-8 strings.
	slices.SortFunc(out, func(a, b row) int {
		if a.display() < b.display() {
			return -1
		}
		if a.display() > b.display() {
			return 1
		}
		if a.id < b.id {
			return -1
		}
		if a.id > b.id {
			return 1
		}
		return 0
	})
	return out
}
func greaterMean(a, b measurement) bool {
	ah, al := bits.Mul64(a.sum, b.count)
	bh, bl := bits.Mul64(b.sum, a.count)
	return ah > bh || ah == bh && al > bl
}
func bestFrame(frames []measurement) (uint32, bool) {
	best := -1
	for i, m := range frames {
		if m.count != 0 && (best < 0 || greaterMean(m, frames[best])) {
			best = i
		}
	}
	return uint32(best), best >= 0
}
func (r row) numbers() (string, string) {
	if r.unmeasured {
		return "", ""
	}
	return strconv.FormatUint(r.count, 10), meanText(r.measurement)
}
func csvField(s string) string {
	if strings.ContainsAny(s, ",\"\r\n") {
		return "\"" + strings.ReplaceAll(s, "\"", "\"\"") + "\""
	}
	return s
}
func tableCSV(rows []row, t, c uint32) ([]byte, error) {
	var out bytes.Buffer
	out.WriteString("region_id,name,t,channel,voxels,mean\n")
	for _, r := range rows {
		count, mean := r.numbers()
		fields := []string{r.id, r.name, strconv.FormatUint(uint64(t), 10), strconv.FormatUint(uint64(c), 10), count, mean}
		for i, field := range fields {
			if i != 0 {
				out.WriteByte(',')
			}
			out.WriteString(csvField(field))
		}
		out.WriteByte('\n')
	}
	return out.Bytes(), nil
}
func swatchPNG(rgb []byte) ([]byte, error) {
	img := image.NewNRGBA(image.Rect(0, 0, 8, 8))
	for y := range 8 {
		for x := range 8 {
			img.SetNRGBA(x, y, color.NRGBA{rgb[0], rgb[1], rgb[2], 255})
		}
	}
	var out bytes.Buffer
	err := png.Encode(&out, img)
	return out.Bytes(), err
}
