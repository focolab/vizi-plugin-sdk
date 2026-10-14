package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestReaderSuffixes(t *testing.T) {
	for _, tc := range []struct {
		name              string
		primary, fallback []string
		want              string
	}{
		{"fallback only", nil, []string{".h5"}, ""},
		{"compound", []string{".ome.tif"}, []string{".tif"}, ""},
		{"nothing", nil, nil, "extensions must be non-empty"},
		{"primary dot", []string{"..."}, []string{"h5"}, "each extension must be a non-empty string"},
		{"fallback dot", []string{"nwb"}, []string{"."}, "each fallback extension must be a non-empty string"},
		{"fallback empty", nil, []string{""}, "each fallback extension must be a non-empty string"},
		{"overlap", []string{"OME.TIF", ".H5", "tif"}, []string{"...h5", ".ome.tif", "H5"}, "a suffix is in both extensions and fallback_extensions: ['.h5', '.ome.tif']"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			m := valid()
			m.Contributions.Readers = []Reader{{ID: "r", Extensions: tc.primary, FallbackExtensions: tc.fallback}}
			_, err := m.JSON()
			if tc.want == "" && err != nil || tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)) {
				t.Fatalf("got %v; want %q", err, tc.want)
			}
		})
	}
	for _, raw := range []string{`{"extensions":"tif"}`, `{"fallback_extensions":"h5"}`, `{"extensions":[1]}`, `{"fallback_extensions":[1]}`} {
		var r Reader
		if err := json.Unmarshal([]byte(raw), &r); err == nil {
			t.Fatal("Go field types accepted non-list/non-string suffixes", raw)
		}
	}
}

func TestReaderSuffixCheckOrder(t *testing.T) {
	// Preserve the published two-fault fixture, including the wrong fallback type.
	for _, raw := range []string{
		`{"extensions":["."],"fallback_extensions":".h5"}`,
		`{"fallback_extensions":".h5","extensions":["."]}`,
		`{"extensions":[".x",". \t"],"fallback_extensions":[1]}`,
	} {
		var r Reader
		err := json.Unmarshal([]byte(raw), &r)
		if err == nil || err.Error() != "each extension must be a non-empty string" {
			t.Fatalf("%s: %v", raw, err)
		}
	}
}

func TestReaderSuffixASCIIWhitespace(t *testing.T) {
	for _, suffix := range []string{" ", "\t", "\r", "\n", "\v", "\f", "... \t\r\n\v\f"} {
		for _, fallback := range []bool{false, true} {
			r := Reader{ID: "r", Extensions: []string{suffix}}
			want := "each extension must be a non-empty string"
			if fallback {
				r.Extensions = []string{".x"}
				r.FallbackExtensions = []string{suffix}
				want = "each fallback extension must be a non-empty string"
			}
			m := valid()
			m.Contributions.Readers = []Reader{r}
			if err := m.Validate(); err == nil || !strings.Contains(err.Error(), want) {
				t.Fatalf("%q fallback=%t: %v", suffix, fallback, err)
			}
			data, err := json.Marshal(r)
			if err != nil {
				t.Fatal(err)
			}
			var decoded Reader
			err = json.Unmarshal(data, &decoded)
			if err == nil {
				err = decoded.validateSuffixes()
			}
			if err == nil || err.Error() != want {
				t.Fatalf("decoded %q fallback=%t: %v", suffix, fallback, err)
			}
		}
	}
	// Unicode whitespace is not ASCII blank. Nor may normalization trim a
	// suffix that has other characters: only its leading dots are removed.
	for _, suffix := range []string{".\u00a0", ".\u2003", " \t.", ". h5 ", "..TIF"} {
		r := Reader{ID: "r", Extensions: []string{suffix}}
		if err := r.validateSuffixes(); err != nil {
			t.Fatalf("%q: %v", suffix, err)
		}
		data, err := json.Marshal(r)
		if err != nil {
			t.Fatal(err)
		}
		var decoded Reader
		if err := json.Unmarshal(data, &decoded); err != nil {
			t.Fatal(err)
		}
		if len(decoded.Extensions) != 1 || decoded.Extensions[0] != suffix {
			t.Fatal("decoder changed suffix", decoded)
		}
	}
	if got := NormalizeSuffix(".. H5 "); got != ". h5 " {
		t.Fatal(got)
	}
}
