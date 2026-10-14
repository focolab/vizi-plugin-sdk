package main

import (
	"math"
	"testing"
)

func TestSpacingFieldsOnly(t *testing.T) {
	for _, micro := range []string{"\u00b5m", "\u03bcm"} {
		if got := convert(2, micro, false); got != 2 {
			t.Fatal(micro, got)
		}
	}
	s, src := exportSpacing([4]float64{1000, 0, math.NaN(), 500}, "nm", "ms")
	if s != [4]float64{1, 0, 0, .5} || src != [4]string{"fields", "neither", "neither", "fields"} {
		t.Fatal(s, src)
	}
	s, src = exportSpacing([4]float64{1, 2, 3, 4}, "furlong", "fortnight")
	if s != [4]float64{} || src != [4]string{"neither", "neither", "neither", "neither"} {
		t.Fatal(s, src)
	}
	for _, name := range []string{"spacing-conflict.crate", "nospacing.crate"} {
		c := mustOpen(t, fixture(name), nil)
		if name == "nospacing.crate" && (c.Header.Spacing != [4]float64{} || spacingLabel(c.Header.Spacing) != "(unknown)") {
			t.Fatal(c.Header)
		}
		if name == "spacing-conflict.crate" && c.Header.Spacing == [4]float64{1, 1, 1, .5} {
			t.Fatal("document overrode header", c.Header)
		}
		recreate(t, c, t.TempDir()+"/out", []int{0, 1, 2, 3, 4, 5})
	}
}
