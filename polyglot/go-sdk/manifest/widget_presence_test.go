package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestWidgetPresenceMatchesReference(t *testing.T) {
	for _, field := range []string{"multiple", "selectable", "follows_selection", "moves_view"} {
		for _, value := range []string{"false", "true"} {
			var w Widget
			if err := json.Unmarshal([]byte(`{"kind":"label","id":"l","`+field+`":`+value+`}`), &w); err != nil {
				t.Fatal(err)
			}
			m := valid()
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
			if err := m.Validate(); (err != nil) != (value == "true") {
				t.Fatalf("%s=%s: %v", field, value, err)
			}
		}
	}
	for _, kind := range []string{"label", "roi_picker"} {
		for _, filter := range []string{"", "all", "mine", "bad"} {
			m := valid()
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: kind, ID: "w", Command: "go", Filter: &filter}}}
			want := kind == "roi_picker" && (filter == "all" || filter == "mine")
			if err := m.Validate(); (err == nil) != want {
				t.Fatalf("%s filter %q: %v", kind, filter, err)
			}
		}
	}
	m := valid()
	multiple := true
	m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "channel_picker", ID: "c", Command: "go", Multiple: &multiple, Value: 2}}}
	if err := m.Validate(); err != nil {
		t.Fatal("multiple picker accepts a scalar:", err)
	}
}

func TestListCellBudgetBoundary(t *testing.T) {
	m := valid()
	rows := 1024
	w := Widget{Kind: "list", ID: "rows", MaxRows: &rows, Row: []Widget{{Kind: "label", ID: "a"}, {Kind: "label", ID: "b"}, {Kind: "label", ID: "c"}, {Kind: "label", ID: "d"}}}
	m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
	if err := m.Validate(); err != nil {
		t.Fatal(err)
	}
	m.Contributions.Panels[0].Root.Row = append(w.Row, Widget{Kind: "image", ID: "e"})
	if err := m.Validate(); err == nil || !strings.Contains(err.Error(), "4096 cells") {
		t.Fatal(err)
	}
}

func TestMenuAndRowKindRejectWrongJSONTypes(t *testing.T) {
	for _, data := range []string{`{"contributions":{"commands":[{"menu":[]}]}}`, `{"contributions":{"tools":[{"menu":{}}]}}`, `{"contributions":{"panels":[{"root":{"kind":"list","row":[{"kind":[]}]}}]}}`} {
		var m Manifest
		if err := json.Unmarshal([]byte(data), &m); err == nil {
			t.Fatal("accepted", data)
		}
	}
}
