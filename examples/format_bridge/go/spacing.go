package main

import (
	"fmt"
	"strconv"
)

func convert(v float64, unit string, temporal bool) float64 {
	units := map[string]float64{"m": 1e6, "cm": 1e4, "mm": 1e3, "µm": 1, "μm": 1, "um": 1, "micron": 1, "nm": 1e-3, "Å": 1e-4, "angstrom": 1e-4}
	if temporal {
		units = map[string]float64{"s": 1, "sec": 1, "ms": 1e-3, "min": 60, "h": 3600, "hr": 3600}
	}
	return known(known(v) * units[unit])
}
func exportSpacing(fields [4]float64, lengthUnit, timeUnit string) (out [4]float64, sources [4]string) {
	for i, v := range fields {
		u := lengthUnit
		if i == 3 {
			u = timeUnit
		}
		out[i] = convert(v, u, i == 3)
		sources[i] = "fields"
		if out[i] == 0 {
			sources[i] = "neither"
		}
	}
	return
}
func spacingLabel(s [4]float64) string {
	if s == [4]float64{} {
		return "(unknown)"
	}
	v := [4]string{"?", "?", "?", "?"}
	for i, n := range s {
		if n > 0 {
			v[i] = fmt.Sprintf("%.3f", n)
			if i == 3 {
				v[i] = strconv.FormatFloat(n, 'g', -1, 64)
			}
		}
	}
	return fmt.Sprintf("%s × %s × %s µm (z × y × x), Δt %s s", v[0], v[1], v[2], v[3])
}
