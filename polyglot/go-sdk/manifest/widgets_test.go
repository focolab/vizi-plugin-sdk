package manifest

import "testing"

func TestListDeclarationLimitDiffersFromStructuralCount(t *testing.T) {
	w := Widget{Kind: "list", ID: "rows", Row: []Widget{{Kind: "label", ID: "a"}, {Kind: "image", ID: "b"}}}
	count := 0
	if err := widget(w, 1, &count, map[string]bool{}, nil); err != nil {
		t.Fatal(err)
	}
	if count != 3 || w.WidgetCount() != 513 {
		t.Fatal(count, w.WidgetCount())
	}
	maxRows, visibleRows := 1024, 1
	w.MaxRows, w.VisibleRows = &maxRows, &visibleRows
	parent := Widget{Kind: "column", ID: "root", Children: []Widget{w, {Kind: "label", ID: "footer"}}}
	m := Manifest{Contributions: Contributions{Panels: []Panel{{Root: parent}, {Root: Widget{Kind: "label"}}}}}
	if m.WidgetCount() != 2052 {
		t.Fatal(m.WidgetCount())
	}
	for _, bound := range []int{0, -1, 1025} {
		w.MaxRows, w.VisibleRows = &bound, nil
		count = 0
		if err := widget(w, 1, &count, map[string]bool{}, nil); err == nil {
			t.Fatal("accepted max_rows", bound)
		}
		w.MaxRows, w.VisibleRows = nil, &bound
		count = 0
		if err := widget(w, 1, &count, map[string]bool{}, nil); err == nil {
			t.Fatal("accepted visible_rows", bound)
		}
	}
}

func TestListCellsSharePanelIDsAndAreLeaves(t *testing.T) {
	for _, w := range []Widget{
		{Kind: "list", ID: "r", Row: []Widget{{Kind: "label", ID: "r"}}},
		{Kind: "column", ID: "p", Children: []Widget{{Kind: "label", ID: "cell"}, {Kind: "list", ID: "rows", Row: []Widget{{Kind: "label", ID: "cell"}}}}},
		{Kind: "list", ID: "r", Row: []Widget{{Kind: "list", ID: "nested"}}},
		{Kind: "list", ID: "r", Row: []Widget{{Kind: "column", ID: "nested"}}},
		{Kind: "list", ID: "r", Row: []Widget{{Kind: "button", ID: "b", Command: "undeclared"}}},
	} {
		count := 0
		if err := widget(w, 1, &count, map[string]bool{}, nil); err == nil {
			t.Fatal("accepted bad list", w)
		}
	}
	selectable := true
	count := 0
	if err := widget(Widget{Kind: "label", ID: "x", Selectable: &selectable}, 1, &count, map[string]bool{}, nil); err == nil {
		t.Fatal("selectable on a non-list")
	}
}
