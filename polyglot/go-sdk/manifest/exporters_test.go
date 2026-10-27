package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestExporterSuffixRule(t *testing.T) {
	for _, tc := range []struct {
		suffix string
		valid  bool
	}{
		{"", false}, {".", false}, {"...", false},
		{" ", false}, {"\t", false}, {"\r", false}, {"\n", false},
		{"\v", false}, {"\f", false}, {"... \t\r\n\v\f", false},
		{"\u00a0", true}, {".\u2003", true}, {" \t.", true},
		{".. H5 ", true}, {"...OME.TIF", true},
	} {
		m := valid()
		m.Contributions.Exporters = []Exporter{{ID: "e", Extension: tc.suffix}}
		data, err := json.Marshal(m)
		if err != nil {
			t.Fatal(err)
		}
		var decoded Manifest
		if err := json.Unmarshal(data, &decoded); err != nil {
			t.Fatal(err)
		}
		for _, candidate := range []Manifest{m, decoded} {
			err := candidate.Validate()
			if (err == nil) != tc.valid || err != nil && !strings.Contains(err.Error(), "extension must be non-empty") {
				t.Fatalf("suffix %q: %v; want valid=%t", tc.suffix, err, tc.valid)
			}
		}
		if got := decoded.Contributions.Exporters[0].Extension; got != tc.suffix {
			t.Fatalf("suffix changed from %q to %q", tc.suffix, got)
		}
	}
}
