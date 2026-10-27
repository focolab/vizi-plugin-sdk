package main

import (
	"bytes"
	"math"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func fixture(name string) string { return filepath.Join("..", "fixtures", name) }
func mustOpen(t *testing.T, name string, cal calibration) *crate {
	t.Helper()
	c, err := openCrate(name, cal)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}
func TestFixtures(t *testing.T) {
	c := mustOpen(t, fixture("sample.crate"), nil)
	crc := []uint32{0x8d53b95b, 0x1c3015ab, 0xd56706b3, 0x03eaaa2e, 0x5889bb44, 0x5aa02fdd}
	if c.Header.Shape != [5]uint32{3, 2, 4, 6, 5} || c.Header.Spacing != [4]float64{2, .325, .325, 1.5} {
		t.Fatal(c.Header)
	}
	for i, e := range c.Index {
		if e != (indexEntry{uint64(1746 - 240*i), 240, crc[i]}) {
			t.Fatalf("index %d: %+v", i, e)
		}
	}
	for ti := uint32(0); ti < 3; ti++ {
		for ch := uint32(0); ch < 2; ch++ {
			b, err := c.Volume(ti, ch, true)
			if err != nil {
				t.Fatal(err)
			}
			for z := 0; z < 4; z++ {
				for y := 0; y < 6; y++ {
					for x := 0; x < 5; x++ {
						want := uint16(10000*int(ti) + 1000*int(ch) + 100*z + 10*y + x)
						if got := le.Uint16(b[2*((z*6+y)*5+x):]); got != want {
							t.Fatalf("(%d,%d,%d,%d,%d)=%d want %d", ti, ch, z, y, x, got, want)
						}
					}
				}
			}
			p, err := c.Plane(ti, 2, ch)
			if err != nil || !bytes.Equal(p, b[120:180]) {
				t.Fatalf("plane: %v", err)
			}
		}
	}
	cal, err := loadCalibration(fixture("vizi-crate-cal.bin"))
	if err != nil {
		t.Fatal(err)
	}
	cc := mustOpen(t, fixture("sample-calibrated.crate"), cal)
	bad := mustOpen(t, fixture("badcrc.crate"), nil)
	for ti := uint32(0); ti < 3; ti++ {
		for ch := uint32(0); ch < 2; ch++ {
			b, _ := c.Volume(ti, ch, true)
			got, err := cc.Volume(ti, ch, true)
			if err != nil || !bytes.Equal(b, got) {
				t.Fatalf("calibration: %v", err)
			}
			_, err = bad.Volume(ti, ch, true)
			if (err != nil) != (ti == 1 && ch == 0) {
				t.Fatalf("badcrc (%d,%d): %v", ti, ch, err)
			}
		}
	}
	if _, err = openCrate(fixture("sample-calibrated.crate"), nil); err == nil || !strings.Contains(err.Error(), "Plugin Manager") {
		t.Fatal(err)
	}
	if _, err = openCrate(fixture("unfinalized.crate"), nil); err == nil || !strings.Contains(err.Error(), "unfinished") {
		t.Fatal(err)
	}
	interop := mustOpen(t, filepath.Join("..", "interop", "python.crate"), nil)
	for ti := uint32(0); ti < 3; ti++ {
		for ch := uint32(0); ch < 2; ch++ {
			a, _ := c.Volume(ti, ch, true)
			b, err := interop.Volume(ti, ch, true)
			if err != nil || !bytes.Equal(a, b) {
				t.Fatalf("interop: %v", err)
			}
		}
	}
}
func recreate(t *testing.T, src *crate, path string, order []int) {
	t.Helper()
	w, err := newWriter(path, src.Header)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { w.Abort() })
	for _, i := range order {
		ti, ch := uint32(i)/src.Header.Shape[1], uint32(i)%src.Header.Shape[1]
		b, err := src.Volume(ti, ch, true)
		if err != nil {
			t.Fatal(err)
		}
		if err = w.Write(ti, ch, b, true); err != nil {
			t.Fatal(err)
		}
	}
	if err = w.Finish(); err != nil {
		t.Fatal(err)
	}
}
func TestWriter(t *testing.T) {
	src := mustOpen(t, fixture("sample.crate"), nil)
	dir := t.TempDir()
	for _, name := range []string{"a", "b"} {
		recreate(t, src, filepath.Join(dir, name), []int{0, 1, 2, 3, 4, 5})
	}
	a, _ := os.ReadFile(filepath.Join(dir, "a"))
	b, _ := os.ReadFile(filepath.Join(dir, "b"))
	peer, _ := os.ReadFile(filepath.Join("..", "interop", "python.crate"))
	if !bytes.Equal(a, b) || !bytes.Equal(a, peer) {
		t.Fatal("writer is not byte-stable or differs from Python writer")
	}
	recreate(t, src, filepath.Join(dir, "scrambled"), []int{2, 5, 0, 4, 1, 3})
	got := mustOpen(t, filepath.Join(dir, "scrambled"), nil)
	for ti := uint32(0); ti < 3; ti++ {
		for ch := uint32(0); ch < 2; ch++ {
			a, _ := src.Volume(ti, ch, true)
			b, err := got.Volume(ti, ch, true)
			if err != nil || !bytes.Equal(a, b) {
				t.Fatalf("scrambled: %v", err)
			}
		}
	}
	for _, finish := range []bool{false, true} {
		path := filepath.Join(dir, "partial")
		w, err := newWriter(path, src.Header)
		if err != nil {
			t.Fatal(err)
		}
		if finish {
			if err = w.Finish(); err == nil || !strings.Contains(err.Error(), "6 missing") {
				t.Fatal(err)
			}
		} else if err = w.Abort(); err != nil {
			t.Fatal(err)
		}
		if _, err = os.Stat(path); !os.IsNotExist(err) {
			t.Fatal("partial file remains", err)
		}
	}
	for _, v := range []struct {
		dtype       uint16
		big, little []byte
	}{{2, []byte{0x12, 0x34, 0xAB, 0xCD}, []byte{0x34, 0x12, 0xCD, 0xAB}}, {3, []byte{0x3f, 0x80, 0, 0, 0xc0, 0, 0, 0}, []byte{0, 0, 0x80, 0x3f, 0, 0, 0, 0xc0}}} {
		path := filepath.Join(dir, "endian")
		w, err := newWriter(path, header{DType: v.dtype, Shape: [5]uint32{1, 1, 1, 1, 2}})
		if err != nil {
			t.Fatal(err)
		}
		if err = w.Write(0, 0, v.big, false); err != nil {
			t.Fatal(err)
		}
		if err = w.Finish(); err != nil {
			t.Fatal(err)
		}
		c := mustOpen(t, path, nil)
		b, err := c.Volume(0, 0, true)
		if err != nil || !bytes.Equal(b, v.little) {
			t.Fatalf("endian: %x %v", b, err)
		}
	}
}
func TestHeaderEdgeCases(t *testing.T) {
	base, err := os.ReadFile(fixture("sample.crate"))
	if err != nil {
		t.Fatal(err)
	}
	for _, v := range []float64{-1, math.Inf(1), math.NaN()} {
		b := bytes.Clone(base)
		le.PutUint64(b[48:], math.Float64bits(v))
		path := filepath.Join(t.TempDir(), "invalid-spacing")
		if err = os.WriteFile(path, b, 0600); err != nil {
			t.Fatal(err)
		}
		c := mustOpen(t, path, nil)
		if c.Header.Spacing[0] != 0 {
			t.Fatal(c.Header)
		}
		if _, err = c.Volume(0, 0, true); err != nil {
			t.Fatal(err)
		}
	}
	b := bytes.Clone(base)
	b[42] = 255
	b[88] = 255
	b[len(b)-12] = 255
	path := filepath.Join(t.TempDir(), "reserved")
	os.WriteFile(path, b, 0600)
	mustOpen(t, path, nil)
	b = bytes.Clone(base)
	b[96+7] = 255
	path = filepath.Join(t.TempDir(), "utf8")
	os.WriteFile(path, b, 0600)
	c := mustOpen(t, path, nil)
	if c.Header.Form != "" || c.Header.Text != "" {
		t.Fatal("invalid UTF-8 retained")
	}
	for i := 0; i < 5; i++ {
		b = bytes.Clone(base)
		le.PutUint32(b[16+i*4:], 0)
		path = filepath.Join(t.TempDir(), "zero")
		os.WriteFile(path, b, 0600)
		if _, err = openCrate(path, nil); err == nil || !strings.Contains(err.Error(), "zero") {
			t.Fatal(err)
		}
	}
	for _, form := range []string{"", "other-format"} {
		path = filepath.Join(t.TempDir(), "metadata")
		w, err := newWriter(path, header{DType: 1, Shape: [5]uint32{1, 1, 1, 1, 1}, Form: form})
		if err != nil {
			t.Fatal(err)
		}
		if err = w.Write(0, 0, []byte{42}, true); err != nil {
			t.Fatal(err)
		}
		if err = w.Finish(); err != nil {
			t.Fatal(err)
		}
		c := mustOpen(t, path, nil)
		if c.Header.Form != form {
			t.Fatal(c.Header)
		}
	}
}
