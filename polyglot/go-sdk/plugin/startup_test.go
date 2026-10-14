package plugin

import (
	"bytes"
	"context"
	"encoding/json"
	"math"
	"os"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
)

func TestDeclaredStartupSecondsReachWrittenManifest(t *testing.T) {
	for _, seconds := range []*float64{nil, startupEstimate(.25), startupEstimate(120), startupEstimate(900)} {
		info := Info{ID: "org.example.startup", Name: "Startup", Version: "1", StartupSeconds: seconds}
		var want float64
		if seconds != nil {
			want = *seconds
		}
		app := buildDefinition(t, NewDefinition(info))
		// Build owns its snapshot, including optional pointers supplied in Info.
		if seconds != nil {
			*seconds = 0
		}
		var out bytes.Buffer
		dir := t.TempDir()
		if err := app.Run(context.Background(), []string{"--manifest=" + dir}, RunOptions{Output: &out, Runtime: func() (control.Plugin, error) {
			t.Fatal("manifest generation started a runtime")
			return control.Plugin{}, nil
		}}); err != nil {
			t.Fatal(err)
		}
		raw, err := os.ReadFile(dir + "/vizi-plugin.json")
		if err != nil {
			t.Fatal(err)
		}
		var doc struct {
			Entry map[string]json.RawMessage `json:"entry"`
		}
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatal(err)
		}
		field, exists := doc.Entry["startup_s"]
		if seconds == nil {
			if exists {
				t.Fatalf("unset startup emitted: %s", raw)
			}
			continue
		}
		var got float64
		if !exists {
			t.Fatalf("declared startup lost: %s", raw)
		}
		if err := json.Unmarshal(field, &got); err != nil {
			t.Fatal(err)
		}
		if got != want {
			t.Fatalf("startup_s = %v, want %v", got, want)
		}
	}
}

func TestDeclarationRefusesInvalidStartupSeconds(t *testing.T) {
	for _, seconds := range []float64{0, -1, math.NaN(), math.Inf(1), math.Inf(-1)} {
		d := NewDefinition(Info{ID: "org.example.startup", Name: "Startup", Version: "1", StartupSeconds: &seconds})
		if _, err := d.Build(); err == nil || err.Error() != "entry.startup_s must be a positive number of seconds" {
			t.Fatalf("startup %v: %v", seconds, err)
		}
	}
}
func startupEstimate(seconds float64) *float64 { return &seconds }
