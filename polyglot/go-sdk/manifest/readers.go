package manifest

import (
	"encoding/json"
	"fmt"
	"slices"
	"strings"
	"unicode/utf8"
)

// NormalizeSuffix uses one leading dot and lowercase, including compound suffixes.
func NormalizeSuffix(s string) string { return "." + strings.ToLower(strings.TrimLeft(s, ".")) }

// Decode primary suffixes completely before attempting the fallback list's
// type. The schema fixes this error precedence even if JSON fields are reordered.
func (r *Reader) UnmarshalJSON(data []byte) error {
	type plain Reader
	var decoded plain
	primary := struct {
		*plain
		FallbackExtensions json.RawMessage `json:"fallback_extensions"`
	}{plain: &decoded}
	if err := json.Unmarshal(data, &primary); err != nil {
		return err
	}
	if err := validateSuffixList(decoded.Extensions, "extension"); err != nil {
		return err
	}
	var fallback struct {
		FallbackExtensions []string `json:"fallback_extensions"`
	}
	if err := json.Unmarshal(data, &fallback); err != nil {
		return err
	}
	decoded.FallbackExtensions = fallback.FallbackExtensions
	*r = Reader(decoded)
	return nil
}

type readerSuffixError struct{ kind string }

func (e *readerSuffixError) Error() string {
	return "each " + e.kind + " must be a non-empty string"
}

func validateSuffixList(suffixes []string, kind string) error {
	for _, suffix := range suffixes {
		if !validSuffix(suffix) {
			return &readerSuffixError{kind}
		}
	}
	return nil
}

func validSuffix(suffix string) bool {
	// Only leading dots are stripped. Non-ASCII whitespace is a suffix,
	// and whitespace around other suffix characters is preserved.
	return strings.Trim(strings.TrimLeft(suffix, "."), " \t\r\n\v\f") != "" && utf8.ValidString(suffix)
}

func (r Reader) validateSuffixes() error {
	if err := validateSuffixList(r.Extensions, "extension"); err != nil {
		return err
	}
	if err := validateSuffixList(r.FallbackExtensions, "fallback extension"); err != nil {
		return err
	}
	if len(r.Extensions)+len(r.FallbackExtensions) == 0 {
		return fmt.Errorf("extensions must be non-empty")
	}
	primary := make(map[string]bool)
	for _, suffix := range r.Extensions {
		primary[NormalizeSuffix(suffix)] = true
	}
	var overlap []string
	for _, suffix := range r.FallbackExtensions {
		suffix = NormalizeSuffix(suffix)
		if primary[suffix] {
			overlap = append(overlap, suffix)
		}
	}
	if len(overlap) > 0 {
		slices.Sort(overlap)
		overlap = slices.Compact(overlap)
		return fmt.Errorf("a suffix is in both extensions and fallback_extensions: ['%s']", strings.Join(overlap, "', '"))
	}
	return nil
}
