package manifest

import (
	"encoding/json"
	"strings"
	"testing"
)

// The table grammar of manifest_schema.md §3.3.1, with the Python SDK's
// refusal texts (Go quotes ids with single quotes where Python's repr does).
func TestTableGrammar(t *testing.T) {
	commands := map[string]bool{"pick": true}
	check := func(raw string) error {
		var w Widget
		if err := json.Unmarshal([]byte(raw), &w); err != nil {
			t.Fatal(err)
		}
		count := 0
		return widget(w, 1, &count, map[string]bool{}, commands)
	}
	for _, ok := range []string{
		`{"kind":"table","id":"t","columns":[{"id":"name"}]}`,
		`{"kind":"table","id":"t","selectable":true,"multiple":true,"command":"pick","sortable":true,"visible_rows":1024,"moves_view":true,
		  "columns":[{"id":"a","header":"A"},{"id":"n","type":"number","precision":0,"unit":"µm"},{"id":"n2","type":"number","precision":15},{"id":"b","type":"bool"}]}`,
	} {
		if err := check(ok); err != nil {
			t.Fatal(ok, err)
		}
	}
	thirtyThree := make([]string, 33)
	for i := range thirtyThree {
		thirtyThree[i] = `{"id":"c` + string(rune('a'+i%26)) + string(rune('a'+i/26)) + `"}`
	}
	for raw, want := range map[string]string{
		`{"kind":"table","id":"t"}`:                                                         "table 't' needs 1 to 32 columns, not 0",
		`{"kind":"table","id":"t","columns":[]}`:                                            "table 't' needs 1 to 32 columns, not 0",
		`{"kind":"table","id":"t","columns":[` + strings.Join(thirtyThree, ",") + `]}`:      "table 't' needs 1 to 32 columns, not 33",
		`{"kind":"table","id":"t","columns":[{"id":" "}]}`:                                  "table 't' columns[0]: id ' ' must be non-empty and distinct",
		`{"kind":"table","id":"t","columns":[{"id":"a"},{"id":"a"}]}`:                       "table 't' columns[1]: id 'a' must be non-empty and distinct",
		`{"kind":"table","id":"t","columns":[{"id":"a","type":"date"}]}`:                    "table 't' columns[0]: type must be one of ['string', 'number', 'bool']",
		`{"kind":"table","id":"t","columns":[{"id":"a","unit":""}]}`:                        "table 't' columns[0]: only a number column takes precision or unit",
		`{"kind":"table","id":"t","columns":[{"id":"a","type":"bool","precision":2}]}`:      "table 't' columns[0]: only a number column takes precision or unit",
		`{"kind":"table","id":"t","columns":[{"id":"a","type":"number","precision":16}]}`:   "table 't' columns[0]: precision must be an integer in [0, 15]",
		`{"kind":"table","id":"t","columns":[{"id":"a","type":"number","precision":-1}]}`:   "table 't' columns[0]: precision must be an integer in [0, 15]",
		`{"kind":"table","id":"t","command":"pick","columns":[{"id":"a"}]}`:                 "table 't' binds a command only when selectable",
		`{"kind":"table","id":"t","selectable":true,"columns":[{"id":"a"}]}`:                "table widget 't' must bind a declared command",
		`{"kind":"table","id":"t","multiple":true,"columns":[{"id":"a"}]}`:                  "table 't' takes multiple only when selectable",
		`{"kind":"table","id":"t","visible_rows":0,"columns":[{"id":"a"}]}`:                 "table 't' visible_rows must be an integer in [1, 1024]",
		`{"kind":"table","id":"t","visible_rows":1025,"columns":[{"id":"a"}]}`:              "table 't' visible_rows must be an integer in [1, 1024]",
		`{"kind":"table","id":"t","moves_view":true,"columns":[{"id":"a"}]}`:                "moves_view widget",
		`{"kind":"label","id":"l","columns":[{"id":"a"}]}`:                                  "label 'l' does not take columns or sortable",
		`{"kind":"list","id":"l","sortable":false,"row":[{"kind":"label","id":"x"}]}`:       "list 'l' does not take columns or sortable",
		`{"kind":"list","id":"l","row":[{"kind":"table","id":"x","columns":[{"id":"a"}]}]}`: "a row holds leaf widgets only",
	} {
		err := check(raw)
		if err == nil || !strings.Contains(err.Error(), want) {
			t.Errorf("%s: got %v; want %q", raw, err, want)
		}
	}
}

func TestTableColumnDefaultsAndRoundTrip(t *testing.T) {
	c := TableColumn{ID: "area"}
	if c.ColumnType() != "string" || c.ColumnHeader() != "area" || c.ColumnPrecision() != TableDefaultPrecision {
		t.Fatal(c)
	}
	if TableMaxRows != 100000 || TableMaxColumns != 32 || TableMaxPatchBytes != 1<<20 {
		t.Fatal("table bounds drifted from the spec")
	}
	zero, unit := 0, "µm²"
	w := Widget{Kind: "table", ID: "t", Columns: []TableColumn{{ID: "a", Type: "number", Precision: &zero, Unit: &unit}}}
	b, err := json.Marshal(w)
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(b), `"precision":0`) || !strings.Contains(string(b), `"unit":"µm²"`) || strings.Contains(string(b), "sortable") {
		t.Fatal(string(b))
	}
}
