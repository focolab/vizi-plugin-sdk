package manifest

import (
	"strings"
	"testing"
)

// sameJSONField reports whether json.UnmarshalTypeError.Field names the expected
// field. How much of the path encoding/json reports depends on the Go release:
// Go 1.27 adds array indices, and the path starts over inside a type with its
// own UnmarshalJSON (contributions.panels.root.min arrives as "min"). So array
// indices are ignored, and the reported path must be the whole expected path or
// a tail of it that starts on a field boundary. The caller also checks the
// destination type, so the leaf field and its type are always pinned.
func sameJSONField(got, want string) bool {
	var kept []string
	for _, part := range strings.Split(got, ".") {
		if strings.Trim(part, "0123456789") != "" {
			kept = append(kept, part)
		}
	}
	got = strings.Join(kept, ".")
	return got != "" && (got == want || strings.HasSuffix(want, "."+got))
}

func TestSameJSONField(t *testing.T) {
	for _, c := range []struct {
		got, want string
		same      bool
	}{
		{"contributions.tools.wants_pick", "contributions.tools.wants_pick", true},   // through Go 1.26
		{"contributions.tools.0.wants_pick", "contributions.tools.wants_pick", true}, // Go 1.27: an index
		{"command.1", "entry.command", true},                                         // Go 1.27: restarted, then an index
		{"min", "contributions.panels.root.min", true},                               // Go 1.27: restarted
		{"fallback_extensions", "contributions.readers.fallback_extensions", true},   // Go 1.27: restarted
		{"in", "contributions.panels.root.min", false},                               // a tail of a name, not of the path
		{"max", "contributions.panels.root.min", false},                              // another field
		{"contributions.tools.wants_pick", "wants_pick", false},                      // longer than expected
		{"panels.root.min", "contributions.overlays.min", false},                     // the tail does not match
		{"0", "entry.command", false},                                                // nothing but an index
		{"", "entry.command", false},
	} {
		if got := sameJSONField(c.got, c.want); got != c.same {
			t.Errorf("sameJSONField(%q, %q) = %v, want %v", c.got, c.want, got, c.same)
		}
	}
}
