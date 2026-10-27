package manifest

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
)

// EnginesIncompatibleError refuses a manifest whose engines.vizi this SDK's
// protocol does not run: a different major, or a newer minor. It is returned
// before any other check, so a plugin built for a later 1.x is refused for
// that and not for the first thing that 1.x added (a widget kind, say).
// Error() is the sentence every implementation says.
type EnginesIncompatibleError struct {
	ID, Name, Version string
	// Engines is engines.vizi as written.
	Engines string
	// Major and Minor are the protocol this SDK speaks.
	Major, Minor int
}

func (e *EnginesIncompatibleError) Error() string {
	plugin := e.Name
	if plugin == "" {
		plugin = e.ID
	}
	return fmt.Sprintf("%s %s needs vizi protocol %s; this vizi speaks %d.%d", plugin, e.Version, e.Engines, e.Major, e.Minor)
}

// ParseEngines reads engines.vizi as the minimum protocol it names. The
// grammar is exact, with nothing trimmed: at most one leading prefix, exactly
// ">=", "^" or "~"; then a major of ASCII digits, optionally followed by one
// "." and a minor of ASCII digits or "x" ("1.0", "1", "1.x"). Leading zeros
// are allowed ("01.0" is 1.0). Each component is at most 4294967295; a larger
// one is not a version, never wrapped or clamped. Anything else, including
// whitespace, a sign, an underscore, a non-ASCII digit, a second prefix or a
// third component, is refused.
func ParseEngines(vizi string) (major, minor int, err error) {
	notVersion := fmt.Errorf("engines.vizi '%s' is not a version", vizi)
	version := vizi
	switch {
	case strings.HasPrefix(version, ">="):
		version = version[2:]
	case strings.HasPrefix(version, "^"), strings.HasPrefix(version, "~"):
		version = version[1:]
	}
	parts := strings.Split(version, ".")
	if len(parts) > 2 {
		return 0, 0, notVersion
	}
	var n [2]int
	for i, p := range parts {
		if i == 1 && p == "x" {
			continue
		}
		if len(p) > 1 && p[0] == '-' && asciiDigits(p[1:]) {
			return 0, 0, fmt.Errorf("engines.vizi '%s' has a negative component", vizi)
		}
		if !asciiDigits(p) {
			return 0, 0, notVersion
		}
		v, e := strconv.ParseUint(p, 10, 32)
		if e != nil {
			return 0, 0, notVersion
		}
		n[i] = int(v)
	}
	return n[0], n[1], nil
}

// asciiDigits reports whether s is one or more of the ASCII digits 0-9.
func asciiDigits(s string) bool {
	if s == "" {
		return false
	}
	for i := 0; i < len(s); i++ {
		if s[i] < '0' || s[i] > '9' {
			return false
		}
	}
	return true
}

// checkEngines is the engines-first check: a malformed engines.vizi is an
// invalid manifest, an incompatible one an *EnginesIncompatibleError.
func checkEngines(id, name, version, vizi string) error {
	major, minor, err := ParseEngines(vizi)
	if err != nil {
		return err
	}
	if major != doorbell.ProtocolMajor || minor > doorbell.ProtocolMinor {
		return &EnginesIncompatibleError{ID: id, Name: name, Version: version, Engines: vizi, Major: doorbell.ProtocolMajor, Minor: doorbell.ProtocolMinor}
	}
	return nil
}

// Parse reads a manifest document and validates it. engines.vizi is read
// first, whatever else the document holds: when it is a string, a malformed
// one is refused and one this protocol does not run is refused with an
// *EnginesIncompatibleError, before the document is decoded. An omitted
// engines reads as {"vizi": "1.0"}, the lowest floor; inside a present one, a
// missing or non-string engines.vizi takes the normal order. Parse also refuses what
// the typed decode cannot tell apart: a missing manifest_version, a min_vizi
// that is not a string, and an sdk_version that is not a non-empty string.
// Parse does not read files; use ValidateFiles on the result for pane icons.
func Parse(data []byte) (Manifest, error) {
	var top map[string]json.RawMessage
	// The probe reads only what it can; anything else is the decoder's to refuse.
	if json.Unmarshal(data, &top) == nil {
		var engines map[string]json.RawMessage
		if json.Unmarshal(top["engines"], &engines) == nil {
			if vizi, ok := rawString(engines["vizi"]); ok {
				if err := checkEngines(rawText(top["id"]), rawText(top["name"]), rawText(top["version"]), vizi); err != nil {
					return Manifest{}, err
				}
			}
		}
		if _, present := top["manifest_version"]; !present {
			return Manifest{}, fmt.Errorf("missing required field 'manifest_version'")
		}
		if raw, present := top["min_vizi"]; present {
			if _, ok := rawString(raw); !ok {
				return Manifest{}, errMinViziType
			}
		}
		if raw, present := top["sdk_version"]; present {
			if v, ok := rawString(raw); !ok || v == "" {
				return Manifest{}, errSDKVersion
			}
		}
	}
	var m Manifest
	if err := json.Unmarshal(data, &m); err != nil {
		return Manifest{}, err
	}
	if _, present := top["engines"]; !present {
		// An omitted engines reads as {"vizi": "1.0"}, the lowest floor.
		m.Engines.Vizi = "1.0"
	}
	if err := m.Validate(); err != nil {
		return Manifest{}, err
	}
	return m, nil
}

// rawText is a top-level string field for the engines sentence: the string
// itself, or "" when it is absent or not a string.
func rawText(raw json.RawMessage) string {
	s, _ := rawString(raw)
	return s
}

// rawString is a JSON string's value, and whether raw is one.
func rawString(raw json.RawMessage) (string, bool) {
	var s string
	if len(raw) == 0 || raw[0] != '"' || json.Unmarshal(raw, &s) != nil {
		return "", false
	}
	return s, true
}
