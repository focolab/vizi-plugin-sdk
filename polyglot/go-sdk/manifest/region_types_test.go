package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

func TestTextFormatNullIsNotOmission(t *testing.T) {
	for _, kind := range []string{"label", "button"} {
		for _, format := range []string{"", `,"text_format":null`, `,"text_format":"plain"`} {
			var w Widget
			if err := json.Unmarshal([]byte(`{"kind":"`+kind+`","id":"w","command":"go"`+format+`}`), &w); err != nil {
				t.Fatal(err)
			}
			m := valid()
			m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: w}}
			want := format == "" || kind == "label" && strings.Contains(format, `"plain"`)
			if err := m.Validate(); (err == nil) != want {
				t.Fatalf("%s %s: %v", kind, format, err)
			}
		}
	}
}

func TestPanelListsShareCellBudget(t *testing.T) {
	rows := 1024
	list := func(id string) Widget {
		return Widget{Kind: "list", ID: id, MaxRows: &rows, Row: []Widget{{Kind: "label", ID: id + "a"}, {Kind: "label", ID: id + "b"}}}
	}
	m := valid()
	m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: Widget{Kind: "column", ID: "root", Children: []Widget{list("one"), list("two")}}}}
	if err := m.Validate(); err != nil {
		t.Fatal(err)
	}
	m.Contributions.Panels[0].Root.Children[1].Row = append(m.Contributions.Panels[0].Root.Children[1].Row, Widget{Kind: "label", ID: "extra"})
	if err := m.Validate(); err == nil || err.Error() != "panel 'p''s lists may realize at most 4096 cells together (each list's max_rows × row cells), not 5120" {
		t.Fatal(err)
	}
	// The budget is per panel, not per manifest.
	m.Contributions.Panels = []Panel{{ID: "p", Title: "P", Root: list("one")}, {ID: "q", Title: "Q", Root: list("two")}, {ID: "r", Title: "R", Root: list("three")}}
	if err := m.Validate(); err != nil {
		t.Fatal(err)
	}
}

func TestRegionTypeValidationAndLaunch(t *testing.T) {
	for _, id := range []string{"box", "ellipsoid", "cylinder", "surfq6", "vizi.tetra", ".tetra", "bad/id", ""} {
		m := valid()
		m.Permissions = []string{"contribute_rois"}
		m.Contributions.RegionTypes = []RegionType{{ID: id}}
		if err := m.Validate(); err == nil {
			t.Fatal("accepted", id)
		}
	}
	m := valid()
	m.Permissions = []string{"contribute_rois"}
	m.Contributions = Contributions{RegionTypes: []RegionType{{ID: "tetra.volume"}}, Overlays: []Overlay{{ID: "overlay"}}}
	data, err := m.JSON()
	if err != nil {
		t.Fatal(err)
	}
	var decoded Manifest
	if err := json.Unmarshal(data, &decoded); err != nil {
		t.Fatal(err)
	}
	if len(decoded.Contributions.RegionTypes) != 1 || decoded.Contributions.RegionTypes[0].ID != "tetra.volume" {
		t.Fatal(string(data))
	}
}
