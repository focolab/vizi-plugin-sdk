package manifest

import (
	"encoding/json"
	"math"
	"testing"
)

func TestEntryStartupSecondsJSON(t *testing.T) {
	for _, tc := range []struct {
		name, field string
		want        *float64
		invalid     bool
	}{
		{name: "absent"},
		{name: "null", field: `,"startup_s":null`},
		{name: "integer", field: `,"startup_s":120`, want: startupSeconds(120)},
		{name: "fractional below host minimum", field: `,"startup_s":0.25`, want: startupSeconds(.25)},
		{name: "above host cap", field: `,"startup_s":900`, want: startupSeconds(900)},
		{name: "zero", field: `,"startup_s":0`, invalid: true},
		{name: "negative", field: `,"startup_s":-5`, invalid: true},
		{name: "string", field: `,"startup_s":"60"`, invalid: true},
		{name: "true", field: `,"startup_s":true`, invalid: true},
		{name: "false", field: `,"startup_s":false`, invalid: true},
		{name: "array", field: `,"startup_s":[]`, invalid: true},
		{name: "object", field: `,"startup_s":{}`, invalid: true},
		{name: "overflow", field: `,"startup_s":1e309`, invalid: true},
	} {
		t.Run(tc.name, func(t *testing.T) {
			raw := `{"manifest_version":1,"id":"test","name":"Test","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["./plugin"]` + tc.field + `}}`
			var m Manifest
			err := json.Unmarshal([]byte(raw), &m)
			if err == nil {
				err = m.Validate()
			}
			if tc.invalid {
				if err == nil || err.Error() != "entry.startup_s must be a positive number of seconds" {
					t.Fatalf("got %v", err)
				}
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := m.JSON()
			if err != nil {
				t.Fatal(err)
			}
			var doc struct {
				Entry map[string]json.RawMessage `json:"entry"`
			}
			if err := json.Unmarshal(encoded, &doc); err != nil {
				t.Fatal(err)
			}
			value, present := doc.Entry["startup_s"]
			if tc.want == nil {
				if present {
					t.Fatalf("absent estimate emitted: %s", encoded)
				}
				return
			}
			var got float64
			if !present {
				t.Fatalf("estimate lost: %s", encoded)
			}
			if err := json.Unmarshal(value, &got); err != nil {
				t.Fatal(err)
			}
			if got != *tc.want {
				t.Fatalf("startup_s = %v; want %v", got, *tc.want)
			}
		})
	}
}

func TestEntryStartupSecondsTypedValidation(t *testing.T) {
	for _, value := range []float64{0, -1, math.NaN(), math.Inf(1), math.Inf(-1)} {
		m := valid()
		m.Entry.StartupSeconds = &value
		if _, err := m.JSON(); err == nil || err.Error() != "entry.startup_s must be a positive number of seconds" {
			t.Fatalf("startup %v: %v", value, err)
		}
	}
}
func startupSeconds(value float64) *float64 { return &value }
