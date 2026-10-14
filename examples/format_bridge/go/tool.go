package main

import (
	"bytes"
	"errors"
	"fmt"
	"hash/crc32"
	"io"
	"math"
	"math/rand"
	"os"
	"slices"
	"strings"
)

type verdict struct {
	Tier  string
	Notes []string
}

func (v verdict) String() string {
	if len(v.Notes) == 0 {
		return v.Tier
	}
	return v.Tier + ": " + strings.Join(v.Notes, "; ")
}
func compareCrates(a, b string, cal calibration) (verdict, error) {
	x, err := openCrate(a, cal)
	if err != nil {
		return verdict{}, err
	}
	defer x.Close()
	y, err := openCrate(b, cal)
	if err != nil {
		return verdict{}, err
	}
	defer y.Close()
	equal, err := sameFiles(a, b)
	if err != nil {
		return verdict{}, err
	}
	v := verdict{Tier: "different"}
	if x.Header.DType != y.Header.DType {
		return verdict{"different", []string{"dtype differs"}}, nil
	}
	if x.Header.Shape != y.Header.Shape {
		return verdict{"different", []string{"shape differs"}}, nil
	}
	different := 0
	first := ""
	for i := range x.Index {
		ti, ch := uint32(i)/x.Header.Shape[1], uint32(i)%x.Header.Shape[1]
		for _, c := range []*crate{x, y} {
			raw, e := readAt(c.file, c.Index[i].Offset, c.Index[i].Length)
			if e != nil {
				return verdict{}, e
			}
			if crc32.ChecksumIEEE(raw) != c.Index[i].CRC {
				v.Notes = append(v.Notes, fmt.Sprintf("%s: (t=%d, c=%d) fails CRC; compared on its bytes anyway", c.Path, ti, ch))
			}
		}
		xb, e := x.Volume(ti, ch, false)
		if e != nil {
			return verdict{}, e
		}
		yb, e := y.Volume(ti, ch, false)
		if e != nil {
			return verdict{}, e
		}
		match := bytes.Equal(xb, yb)
		if x.Header.DType == 3 && !equal {
			match = true
			for j := 0; j < len(xb); j += 4 {
				if math.Float32frombits(le.Uint32(xb[j:])) != math.Float32frombits(le.Uint32(yb[j:])) {
					match = false
					break
				}
			}
		}
		if !match {
			if different == 0 {
				first = fmt.Sprintf("(t=%d, c=%d)", ti, ch)
			}
			different++
		}
	}
	if equal {
		v.Tier = "byte-equal"
		return v, nil
	}
	if different > 0 {
		v.Notes = append(v.Notes, fmt.Sprintf("%d of %d volumes differ; first: %s", different, len(x.Index), first))
		return v, nil
	}
	v.Tier = "semantically equal"
	if x.Header.Form != y.Header.Form || x.Header.Text != y.Header.Text {
		v.Tier = "voxel-equal"
		v.Notes = append(v.Notes, "metadata form or text differs byte-for-byte")
	}
	if x.Header.Spacing != y.Header.Spacing {
		v.Tier = "voxel-equal"
		v.Notes = append(v.Notes, "header spacing or time increment differs")
	}
	if !slices.Equal(canonicalNames(x.Header.Names), canonicalNames(y.Header.Names)) {
		v.Tier = "voxel-equal"
		v.Notes = append(v.Notes, "channel names differ")
	}
	if x.Header.Flags&2 != y.Header.Flags&2 {
		v.Notes = append(v.Notes, "DERIVED differs (not compared)")
	}
	if x.Header.Provenance != y.Header.Provenance {
		v.Notes = append(v.Notes, "provenance record differs (not compared)")
	}
	if x.Header.Flags&1 != y.Header.Flags&1 {
		v.Notes = append(v.Notes, "calibrated counts and decoded values agree once decoded")
	}
	return v, nil
}
func canonicalNames(names []string) []string {
	for len(names) > 0 && names[len(names)-1] == "" {
		names = names[:len(names)-1]
	}
	return names
}
func sameFiles(a, b string) (bool, error) {
	x, err := os.Open(a)
	if err != nil {
		return false, err
	}
	defer x.Close()
	y, err := os.Open(b)
	if err != nil {
		return false, err
	}
	defer y.Close()
	xb, yb := make([]byte, 64<<10), make([]byte, 64<<10)
	for {
		nx, ex := io.ReadFull(x, xb)
		ny, ey := io.ReadFull(y, yb)
		for _, e := range []error{ex, ey} {
			if e != nil && e != io.EOF && e != io.ErrUnexpectedEOF {
				return false, e
			}
		}
		if nx != ny || !bytes.Equal(xb[:nx], yb[:ny]) {
			return false, nil
		}
		if ex != nil || ey != nil {
			return ex == ey, nil
		}
	}
}
func recreateCrate(src, dst, order string, cal calibration) (err error) {
	a, err := os.Stat(src)
	if err != nil {
		return err
	}
	if b, e := os.Stat(dst); e == nil && os.SameFile(a, b) {
		return errors.New("source and target must be different files")
	}
	c, err := openCrate(src, cal)
	if err != nil {
		return err
	}
	defer c.Close()
	w, err := newWriter(dst, c.Header)
	if err != nil {
		return err
	}
	defer func() {
		if err != nil {
			err = errors.Join(err, w.Abort())
		}
	}()
	slots := make([]int, len(c.Index))
	for i := range slots {
		slots[i] = i
	}
	switch order {
	case "reverse":
		for a, b := 0, len(slots)-1; a < b; a, b = a+1, b-1 {
			slots[a], slots[b] = slots[b], slots[a]
		}
	case "scrambled":
		rand.New(rand.NewSource(0x5eed)).Shuffle(len(slots), func(i, j int) { slots[i], slots[j] = slots[j], slots[i] })
	}
	for _, i := range slots {
		ti, ch := uint32(i)/c.Header.Shape[1], uint32(i)%c.Header.Shape[1]
		b, e := c.Volume(ti, ch, true)
		if e != nil {
			return e
		}
		if e = w.Write(ti, ch, b, true); e != nil {
			return e
		}
	}
	return w.Finish()
}
func checkCrate(path string) (string, bool, error) {
	// Checking stored CRCs does not require decoding or a calibration table.
	c, err := openCrate(path, calibration{})
	if err != nil {
		return "", false, err
	}
	defer c.Close()
	n, first := 0, 0
	for i, e := range c.Index {
		b, err := readAt(c.file, e.Offset, e.Length)
		if err != nil {
			return "", false, err
		}
		if crc32.ChecksumIEEE(b) != e.CRC {
			if n == 0 {
				first = i
			}
			n++
		}
	}
	if n > 0 {
		return fmt.Sprintf("%s: %d of %d blocks failed CRC; first: (t=%d, c=%d)", path, n, len(c.Index), first/int(c.Header.Shape[1]), first%int(c.Header.Shape[1])), false, nil
	}
	return fmt.Sprintf("%s: all %d blocks passed CRC", path, len(c.Index)), true, nil
}
func runTool(args []string, out, errout io.Writer) int {
	usage := func() int {
		fmt.Fprintln(errout, "usage: format-bridge compare A B [--calibration TABLE] | recreate IN OUT [--order ascending|reverse|scrambled] [--calibration TABLE] | check FILE")
		return 1
	}
	if len(args) == 0 {
		return usage()
	}
	cmd := args[0]
	var pos []string
	calPath, order := "", "ascending"
	for i := 1; i < len(args); i++ {
		switch args[i] {
		case "--calibration", "--order":
			if i+1 == len(args) || cmd == "check" {
				return usage()
			}
			key := args[i]
			i++
			if key == "--calibration" {
				calPath = args[i]
			} else {
				if cmd != "recreate" {
					return usage()
				}
				order = args[i]
			}
		default:
			if strings.HasPrefix(args[i], "-") {
				return usage()
			}
			pos = append(pos, args[i])
		}
	}
	if (cmd == "check" && len(pos) != 1) || ((cmd == "compare" || cmd == "recreate") && len(pos) != 2) || (cmd != "compare" && cmd != "recreate" && cmd != "check") || (order != "ascending" && order != "reverse" && order != "scrambled") {
		return usage()
	}
	var cal calibration
	var err error
	if calPath != "" {
		cal, err = loadCalibration(calPath)
		if err != nil {
			fmt.Fprintln(errout, err)
			return 2
		}
	}
	switch cmd {
	case "compare":
		var v verdict
		v, err = compareCrates(pos[0], pos[1], cal)
		if err == nil {
			fmt.Fprintln(out, v)
		}
	case "recreate":
		err = recreateCrate(pos[0], pos[1], order, cal)
		if err == nil {
			fmt.Fprintln(out, "wrote", pos[1], "("+order+" block order)")
		}
	case "check":
		var s string
		var ok bool
		s, ok, err = checkCrate(pos[0])
		if err == nil {
			fmt.Fprintln(out, s)
			if !ok {
				return 3
			}
		}
	}
	if err != nil {
		fmt.Fprintln(errout, err)
		return 2
	}
	return 0
}
