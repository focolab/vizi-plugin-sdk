package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

// wants_pick is a boolean, default true.
func TestToolWantsPick(t *testing.T) {
	parse := func(version int, field string) (Manifest, error) {
		var m Manifest
		err := json.Unmarshal([]byte(`{"manifest_version":`+string(rune('0'+version))+`,"id":"org.example.x","name":"X","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["x"]},"contributions":{"tools":[{"id":"t"`+field+`}]}}`), &m)
		if err == nil {
			err = m.Validate()
		}
		return m, err
	}
	for field, want := range map[string]*bool{``: nil, `,"wants_pick":true`: ptr(true), `,"wants_pick":false`: ptr(false)} {
		m, err := parse(1, field)
		if err != nil {
			t.Fatal(field, err)
		}
		got := m.Contributions.Tools[0].WantsPick
		if (got == nil) != (want == nil) || got != nil && *got != *want {
			t.Fatal(field, got)
		}
		// Absent is not written: every tool before the field wanted the pick.
		out, err := m.JSON()
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(out), "wants_pick") != (want != nil) {
			t.Fatalf("%q wrote %s", field, out)
		}
		if want != nil && !strings.Contains(strings.ReplaceAll(string(out), " ", ""), `"wants_pick":`+map[bool]string{true: "true", false: "false"}[*want]) {
			t.Fatalf("%q wrote %s", field, out)
		}
	}
	// Go's field type refuses a non-boolean before Validate runs.
	for _, bad := range []string{`"no"`, `0`, `[false]`} {
		var typeError *json.UnmarshalTypeError
		_, err := parse(1, `,"wants_pick":`+bad)
		if !asTypeError(err, &typeError) || typeError.Field != "contributions.tools.wants_pick" {
			t.Fatal(bad, err)
		}
	}
}

func ptr(b bool) *bool { return &b }

func asTypeError(err error, target **json.UnmarshalTypeError) bool {
	typeError, ok := err.(*json.UnmarshalTypeError)
	*target = typeError
	return ok
}
