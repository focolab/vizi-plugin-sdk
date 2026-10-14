package manifest

import (
	"encoding/json"
	"fmt"
	"strings"
	"testing"
)

func TestLabelTextFormat(t *testing.T) {
	for _, kind := range []string{"label", "button", "image", "column", "list"} {
		for _, format := range []string{"plain", "rich", "html", "", "PLAIN"} {
			t.Run(kind+"/"+format, func(t *testing.T) {
				m := valid()
				w := Widget{Kind: kind, ID: "a", TextFormat: &format}
				m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
				_, err := m.JSON()
				var want string
				if format != "plain" && format != "rich" {
					want = fmt.Sprintf("panel %q: %s 'a' text_format must be one of ['plain', 'rich']", "p", kind)
				} else if kind != "label" {
					want = fmt.Sprintf("panel %q: %s 'a' does not take text_format; only a label does", "p", kind)
				}
				if want == "" {
					if err != nil {
						t.Fatal(err)
					}
				} else if err == nil || err.Error() != want {
					t.Fatalf("got %v; want %s", err, want)
				}
			})
		}
	}
}

func TestLabelTextFormatJSONPresence(t *testing.T) {
	for _, declaration := range []string{
		`{"kind":"label","id":"a","text":"<b>literal</b>"}`,
		`{"kind":"label","id":"a","text_format":"plain"}`,
		`{"kind":"label","id":"a","text_format":"rich"}`,
	} {
		var w Widget
		if err := json.Unmarshal([]byte(declaration), &w); err != nil {
			t.Fatal(err)
		}
		m := valid()
		m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
		data, err := m.JSON()
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(data), `"text_format"`) != strings.Contains(declaration, `"text_format"`) {
			t.Fatalf("changed field presence: %s", data)
		}
		var roundTrip Manifest
		if err := json.Unmarshal(data, &roundTrip); err != nil {
			t.Fatal(err)
		}
		got := roundTrip.Contributions.Panels[0].Root
		if got.Text != w.Text || (w.TextFormat != nil && (got.TextFormat == nil || *got.TextFormat != *w.TextFormat)) {
			t.Fatalf("changed label contents: %s", data)
		}
	}
}
