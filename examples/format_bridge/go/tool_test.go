package main

import (
	"bytes"
	"hash/crc32"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestComparatorTiersAndExitCodes(t *testing.T) {
	cal := fixture("vizi-crate-cal.bin")
	for _, tc := range []struct {
		a, b, tier string
		code       int
	}{
		{"sample.crate", "sample.crate", "byte-equal", 0},
		{"sample.crate", "sample-calibrated.crate", "semantically equal", 0},
		{"sample.crate", "nospacing.crate", "voxel-equal", 0},
		{"sample.crate", "badcrc.crate", "different", 0},
		{"badcrc.crate", "badcrc.crate", "byte-equal", 0},
		{"unfinalized.crate", "unfinalized.crate", "", 2},
	} {
		var out, errs bytes.Buffer
		code := runTool([]string{"compare", fixture(tc.a), fixture(tc.b), "--calibration", cal}, &out, &errs)
		if code != tc.code || !strings.HasPrefix(out.String(), tc.tier) {
			t.Fatalf("%s/%s: %d %s %s", tc.a, tc.b, code, out.String(), errs.String())
		}
	}
	for _, args := range [][]string{{"compare"}, {"unknown"}, {"compare", "a", "b", "--bogus"}, {"recreate", "a", "b", "--order", "bad"}} {
		if code := runTool(args, &bytes.Buffer{}, &bytes.Buffer{}); code != 1 {
			t.Fatal(args, code)
		}
	}
	if code := runTool([]string{"compare", fixture("sample-calibrated.crate"), fixture("sample.crate")}, &bytes.Buffer{}, &bytes.Buffer{}); code != 2 {
		t.Fatal(code)
	}
	dst := filepath.Join(t.TempDir(), "out.crate")
	if code := runTool([]string{"recreate", fixture("sample.crate"), dst}, &bytes.Buffer{}, &bytes.Buffer{}); code != 0 {
		t.Fatal(code)
	}
	if v, err := compareCrates(dst, filepath.Join("..", "interop", "python.crate"), nil); err != nil || v.Tier != "byte-equal" {
		t.Fatal(v, err)
	}
	if err := recreateCrate(dst, dst, "ascending", nil); err == nil {
		t.Fatal("self truncation accepted")
	}
	if _, ok, err := checkCrate(fixture("sample-calibrated.crate")); err != nil || !ok {
		t.Fatal(ok, err)
	}
}

func TestCorruptIndexAndIgnoredIndexReserved(t *testing.T) {
	base, err := os.ReadFile(fixture("sample.crate"))
	if err != nil {
		t.Fatal(err)
	}
	write := func(b []byte) string {
		t.Helper()
		p := filepath.Join(t.TempDir(), "test.crate")
		if err := os.WriteFile(p, b, 0600); err != nil {
			t.Fatal(err)
		}
		return p
	}
	b := bytes.Clone(base)
	b[1986] ^= 1
	if _, err = openCrate(write(b), nil); err == nil || !strings.Contains(err.Error(), "index CRC") {
		t.Fatal(err)
	}
	b = bytes.Clone(base)
	b[1986+20] = 255
	le.PutUint32(b[2130+16:], crc32.ChecksumIEEE(b[1986:2130]))
	mustOpen(t, write(b), nil)
	b = bytes.Clone(base)
	le.PutUint64(b[1986+8:], 239)
	le.PutUint32(b[2130+16:], crc32.ChecksumIEEE(b[1986:2130]))
	if _, err = openCrate(write(b), nil); err == nil || !strings.Contains(err.Error(), "index inconsistent") {
		t.Fatal(err)
	}
}
