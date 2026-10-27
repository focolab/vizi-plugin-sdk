package manifest

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

type enginesFixtures struct {
	Protocol string `json:"protocol"`
	Cases    []struct {
		Name     string          `json:"name"`
		JSON     json.RawMessage `json:"json"`
		Expected struct {
			Kind    string `json:"kind"`
			Message string `json:"message"`
		} `json:"expected"`
	} `json:"cases"`
}

// The repository's manifest fixtures (polyglot/manifest_fixtures/*.json),
// which scripts/check_manifest_fixtures.py also runs through the reference
// parser.
func TestRepositoryManifestFixtures(t *testing.T) {
	files, err := filepath.Glob("../../manifest_fixtures/*.json")
	if err != nil {
		t.Fatal(err)
	}
	if len(files) < 2 {
		t.Fatalf("found %d fixture files", len(files))
	}
	for _, file := range files {
		raw, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		var doc enginesFixtures
		if err := json.Unmarshal(raw, &doc); err != nil {
			t.Fatal(err)
		}
		if want := fmt.Sprintf("%d.%d", doorbell.ProtocolMajor, doorbell.ProtocolMinor); doc.Protocol != want {
			t.Fatalf("%s is written for protocol %s; this SDK speaks %s", file, doc.Protocol, want)
		}
		if len(doc.Cases) < 4 {
			t.Fatalf("%s: found %d cases", file, len(doc.Cases))
		}
		runManifestFixtures(t, filepath.Base(file), doc)
	}
}

func runManifestFixtures(t *testing.T, file string, doc enginesFixtures) {
	for _, c := range doc.Cases {
		t.Run(file+"/"+c.Name, func(t *testing.T) {
			_, err := Parse(c.JSON)
			var engines *EnginesIncompatibleError
			switch c.Expected.Kind {
			case "accepted":
				if err != nil {
					t.Fatalf("refused: %v", err)
				}
			case "engines_incompatible":
				if !errors.As(err, &engines) {
					t.Fatalf("got %v; want an EnginesIncompatibleError", err)
				}
				if err.Error() != c.Expected.Message {
					t.Fatalf("got %q; want %q", err, c.Expected.Message)
				}
			case "invalid":
				if err == nil || errors.As(err, &engines) {
					t.Fatalf("got %v; want an invalid manifest", err)
				}
				if got := strings.ReplaceAll(err.Error(), `"`, `'`); !strings.Contains(got, c.Expected.Message) {
					t.Fatalf("got %q; want it to contain %q", got, c.Expected.Message)
				}
			default:
				t.Fatalf("unknown expected kind %q", c.Expected.Kind)
			}
		})
	}
}

// Validate on a generated manifest reads engines first too.
func TestValidateReadsEnginesFirst(t *testing.T) {
	m := valid()
	m.Engines.Vizi = "1.1"
	m.ManifestVersion = 7
	var engines *EnginesIncompatibleError
	if err := m.Validate(); !errors.As(err, &engines) || err.Error() != "Test 1 needs vizi protocol 1.1; this vizi speaks 1.0" {
		t.Fatalf("got %v", err)
	}
	m.Name = ""
	if err := m.Validate(); err == nil || err.Error() != "org.example.test 1 needs vizi protocol 1.1; this vizi speaks 1.0" {
		t.Fatalf("got %v", err)
	}
	m.Engines.Vizi = "1.0.3"
	if err := m.Validate(); err == nil || errors.As(err, &engines) || err.Error() != "engines.vizi '1.0.3' is not a version" {
		t.Fatalf("got %v", err)
	}
}

// The engines.vizi grammar: at most one leading prefix (">=", "^" or "~"),
// then ASCII digits, optionally "." and ASCII digits or "x", with nothing
// trimmed. A component above 4294967295 is not a version; leading zeros are
// allowed.
func TestEnginesGrammar(t *testing.T) {
	for spec, want := range map[string][2]int{
		"1.0": {1, 0}, "1": {1, 0}, "1.x": {1, 0}, ">=1.0": {1, 0}, "^1.0": {1, 0}, "~1.0": {1, 0},
		">=1.x": {1, 0}, "^1": {1, 0}, "01.0": {1, 0}, "1.02": {1, 2}, "0.9": {0, 9},
		"4294967295.4294967295": {4294967295, 4294967295},
	} {
		major, minor, err := ParseEngines(spec)
		if err != nil || major != want[0] || minor != want[1] {
			t.Errorf("%q: got %d.%d, %v; want %d.%d", spec, major, minor, err, want[0], want[1])
		}
	}
	for _, spec := range []string{
		"", "^^1.0", "^~1.0", "~>=1.0", ">=^1.0", "+1.0", "1.+0", "=1.0", ">1.0",
		" 1.0", "1.0 ", "1. 0", "1 .0", "^ 1.0", ">= 1.0", "\t1.0",
		"1.0_0", "1_0", "1.0_1", "١.٠", "１.0", "1.0.3", "1.*", "1.", ".0", "1..0", "x", "x.0", "1.X",
		"one.zero", "4294967296.0", "1.4294967296", "99999999999999999999.0", "0x1.0", "1e1.0",
	} {
		if _, _, err := ParseEngines(spec); err == nil || err.Error() != fmt.Sprintf("engines.vizi '%s' is not a version", spec) {
			t.Errorf("%q: got %v; want it refused as not a version", spec, err)
		}
	}
	for _, spec := range []string{"1.-1", "-1.0", "-1", "^-1.0"} {
		if _, _, err := ParseEngines(spec); err == nil || err.Error() != fmt.Sprintf("engines.vizi '%s' has a negative component", spec) {
			t.Errorf("%q: got %v; want it refused as a negative component", spec, err)
		}
	}
	if _, _, err := ParseEngines("1.0"); err != nil {
		t.Fatal(err)
	}
	if _, _, err := ParseEngines("1.-x"); err == nil || err.Error() != "engines.vizi '1.-x' is not a version" {
		t.Errorf("1.-x: %v", err)
	}
	if _, _, err := EnginesMinimum(" 1.0"); err == nil {
		t.Error("EnginesMinimum trims what ParseEngines refuses")
	}
}

// manifest_version is the integer 1; the JSON true is not 1.
func TestManifestVersionTrueRefused(t *testing.T) {
	for _, v := range []string{"true", "false", `"1"`, "1.5", "null"} {
		doc := `{"manifest_version": ` + v + `, "id": "org.example.x", "name": "X", "version": "1", "engines": {"vizi": "1.0"}, "entry": {"command": ["python3"]}}`
		if _, err := Parse([]byte(doc)); err == nil {
			t.Errorf("manifest_version %s accepted", v)
		}
	}
	if _, err := Parse([]byte(`{"manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": {"vizi": "1.0"}, "entry": {"command": ["python3"]}}`)); err != nil {
		t.Fatalf("manifest_version 1 refused: %v", err)
	}
}
