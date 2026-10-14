package manifest

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"regexp"
	"strings"
	"testing"
	"testing/fstest"
)

// These two rejected JSON documents cannot be emitted by the typed generator:
// manifest_version is always serialized, and Tool has no binding field. They
// are explicitly accounted for, not counted as successful validator rejections.
// Pin the body as well as the label so a changed/new fixture cannot silently
// inherit an exemption that was reviewed for a different document.
var rawOnlySchemaFixtures = map[string]struct{ digest, reason string }{
	`rejected "missing required field 'manifest_version'"`: {
		"aefad7c7341d522f6b918a46ec233aaf6156a5c286097f58bd4bf257bad3b5b4",
		"Manifest.ManifestVersion is always emitted, including zero",
	},
	`rejected "binding is not a field"`: {
		"c0d0a431d767beefc4337e200c064fbd709804024569b51fbc85451298c43062",
		"Tool cannot emit the removed binding field",
	},
}

// Read the normative document rather than maintaining a second copy of its
// fixtures. A changed or newly added fixture is exercised on the next test run.
// This tests generated declarations and their Go field types, not a raw-JSON
// parser. Outcome labels are authoritative; Rejections must identify the intended rule, not an unrelated earlier failure.
// Run with -count=1: Go does not track test inputs outside this module in its
// result cache. CI uses -count=1 for the same reason.
func TestPublishedSchemaFixtures(t *testing.T) {
	spec, err := os.ReadFile("../../../docs/spec/manifest_schema.md")
	if err != nil {
		t.Fatal(err)
	}
	// Keep fixture fingerprints stable across Unix and Windows checkouts.
	spec = bytes.ReplaceAll(spec, []byte("\r\n"), []byte("\n"))
	markers := regexp.MustCompile(`(?m)^<!-- fixture:`).FindAll(spec, -1)
	fixtures := regexp.MustCompile("(?m)^<!-- fixture: (accepted|rejected \"[^\"]*\") -->\\r?\\n```json\\r?\\n([\\s\\S]*?)\\r?\\n```").FindAllSubmatch(spec, -1)
	if len(fixtures) != len(markers) || len(fixtures) < 152 {
		t.Fatalf("found %d fixtures for %d markers; expected at least 152", len(fixtures), len(markers))
	}
	icons := fstest.MapFS{
		"icons/tool.svg": {Data: []byte(`<svg xmlns="http://www.w3.org/2000/svg"/>`)},
		"icons/go.png":   {Data: []byte("\x89PNG\r\n\x1a\n")},
		"icons/big.png":  {Data: make([]byte, 65537)},
		"icons/fake.png": {Data: []byte("not an image")},
		"icons/fake.svg": {Data: []byte("not an image")},
	}
	var accepted, rejected, typeRejected int
	stageDuplicateSeen := false
	accounted := map[string]bool{}
	for i, f := range fixtures {
		label, body := string(f[1]), f[2]
		if label == `rejected "duplicate stage id 'stabilize'"` {
			stageDuplicateSeen = true
		}
		t.Run(fmt.Sprintf("%03d_%s", i+1, label), func(t *testing.T) {
			if !json.Valid(body) {
				t.Fatal("published fixture is not valid JSON")
			}
			var doc map[string]any
			decoder := json.NewDecoder(bytes.NewReader(body))
			decoder.UseNumber()
			if err := decoder.Decode(&doc); err != nil {
				t.Fatalf("published fixture is not a JSON object: %v", err)
			}
			typedJSON := schemaTypedJSON(t, doc)
			var m Manifest
			decodeErr := json.Unmarshal(typedJSON, &m)
			if exemption, ok := rawOnlySchemaFixtures[label]; ok {
				if got := fmt.Sprintf("%x", sha256.Sum256(body)); got != exemption.digest {
					t.Fatalf("raw-only fixture changed; reassess its applicability: %s", exemption.reason)
				}
				if accounted[label] {
					t.Fatal("duplicate raw-only fixture; reassess its applicability")
				}
				accounted[label] = true
				if decodeErr != nil {
					t.Fatal(decodeErr)
				}
				if err := m.ValidateFiles(icons); err != nil {
					t.Fatalf("raw-only fixture now has a typed validation failure; reassess exemption: %v", err)
				}
				generated, err := m.JSON()
				if err != nil {
					t.Fatal(err)
				}
				var out map[string]any
				if err = json.Unmarshal(generated, &out); err != nil {
					t.Fatal(err)
				}
				switch label {
				case `rejected "missing required field 'manifest_version'"`:
					if _, present := doc["manifest_version"]; present {
						t.Fatal("fixture no longer omits manifest_version")
					}
					if _, present := out["manifest_version"]; !present {
						t.Fatal("generator can omit required manifest_version")
					}
				case `rejected "binding is not a field"`:
					tool := out["contributions"].(map[string]any)["tools"].([]any)[0].(map[string]any)
					if _, present := tool["binding"]; present {
						t.Fatal("generator can emit removed binding field")
					}
				}
				t.Log("raw JSON outside the typed generator's domain:", exemption.reason)
				return
			}
			var suffixErr *readerSuffixError
			if decodeErr != nil && !errors.As(decodeErr, &suffixErr) && !errors.Is(decodeErr, errStartupSeconds) {
				var typeError *json.UnmarshalTypeError
				if !errors.As(decodeErr, &typeError) {
					t.Fatalf("unexpected decoding failure: %v", decodeErr)
				}
				if label == "accepted" {
					t.Fatalf("accepted fixture not representable: %v", decodeErr)
				}
				// Go rejects these before Validate. Pin the offending field and
				// destination type so an unrelated decoding error cannot pass.
				expectedTypes := map[string]struct{ field, typ string }{
					`rejected "field 'manifest_version' must be int"`:              {"manifest_version", "int"},
					`rejected "entry.command must be a non-empty list of strings"`: {"entry.command", "string"},
					`rejected "contributions must be an object"`:                   {"contributions", "manifest.Contributions"},
					`rejected "permissions must be a list of strings"`:             {"permissions", "[]string"},
					`rejected "contributions.readers must be a list"`:              {"contributions.readers", "[]manifest.Reader"},
					`rejected "fallback_extensions must be a list"`:                {"contributions.readers.fallback_extensions", "[]string"},
					`rejected "slider 's' min must be numeric"`:                    {"contributions.panels.root.min", "float64"},
					`rejected "slider 's' float must be a boolean"`:                {"contributions.panels.root.float", "bool"},
					`rejected "label 'l' text must be a string"`:                   {"contributions.panels.root.text", "string"},
					`rejected "progress 'bar' indeterminate must be a boolean"`:    {"contributions.panels.root.indeterminate", "bool"},
					`rejected "list 'cells' selectable must be a boolean"`:         {"contributions.panels.root.selectable", "bool"},
					`rejected "chunked must be a bool"`:                            {"contributions.overlays.chunked", "bool"},
					`rejected "cancel_on_camera_move must be a boolean"`:           {"contributions.tools.cancel_on_camera_move", "bool"},
					`rejected "wants_pick must be a boolean"`:                      {"contributions.tools.wants_pick", "bool"},
					`rejected "button must be a string"`:                           {"contributions.tools.triggers.button", "string"},
				}
				expected, ok := expectedTypes[label]
				if !ok || typeError.Field != expected.field || typeError.Type.String() != expected.typ {
					t.Fatalf("wrong typed rejection for %s: %v", label, decodeErr)
				}
				typeRejected++
				t.Log("Go field type prevents this malformed declaration:", decodeErr)
				return
			}
			// Reader suffix and entry startup decoding can return semantic errors.
			// Check those against the verbatim text, like a ValidateFiles error.
			err := decodeErr
			if err == nil {
				err = m.ValidateFiles(icons)
			}
			if got, want := err == nil, label == "accepted"; got != want {
				t.Fatalf("accepted=%t want=%t: %v\n%s", got, want, err, body)
			}
			if label == "accepted" {
				accepted++
				// Exercise the actual generator as well as the validator. The generated
				// representation must remain valid after its defaults/omitempty choices.
				generated, err := m.JSON()
				if err != nil {
					t.Fatal(err)
				}
				var roundtrip Manifest
				if err = json.Unmarshal(generated, &roundtrip); err != nil {
					t.Fatal(err)
				}
				if err = roundtrip.ValidateFiles(icons); err != nil {
					t.Fatal(err)
				}
			} else {
				want := strings.TrimSuffix(strings.TrimPrefix(label, `rejected "`), `"`)
				got := strings.ReplaceAll(err.Error(), `"`, `'`)
				if !strings.Contains(got, want) {
					t.Errorf("rejection text: got %q; want %q", got, want)
				}
				rejected++
			}
		})
	}
	if !stageDuplicateSeen {
		t.Error("published duplicate-stage fixture disappeared")
	}
	for label := range rawOnlySchemaFixtures {
		if !accounted[label] {
			t.Errorf("raw-only fixture disappeared: %s; remove or reassess exemption", label)
		}
	}
	t.Logf("%d published fixtures: %d accepted, %d rejected by validation, %d rejected by Go field types, %d raw-only cases checked for non-emission", len(fixtures), accepted, rejected, typeRejected, len(accounted))
}

// The schema accepts modifiers as a single string or a list. Go always emits
// a list. Translate only that documented shorthand; do not fill defaults,
// remove unknown fields, or repair malformed values in the fixtures. UseNumber
// above keeps integer sizes and invalid fractional integer fields unchanged.
func schemaTypedJSON(t *testing.T, doc map[string]any) []byte {
	t.Helper()
	contributions, _ := doc["contributions"].(map[string]any)
	tools, _ := contributions["tools"].([]any)
	for _, v := range tools {
		tool, _ := v.(map[string]any)
		triggers, _ := tool["triggers"].([]any)
		for _, v := range triggers {
			trigger, _ := v.(map[string]any)
			if mod, ok := trigger["modifiers"].(string); ok {
				trigger["modifiers"] = []string{mod}
			}
		}
	}
	b, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
