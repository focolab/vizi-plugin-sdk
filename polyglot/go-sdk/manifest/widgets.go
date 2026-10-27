package manifest

// RowLimit returns the declared maximum rows, or the list default of 256.
// Call only for a list in a validated manifest.
func (w Widget) RowLimit() int {
	if w.MaxRows != nil {
		return *w.MaxRows
	}
	return 256
}

// WidgetCount is the structural queue-sizing count for a validated tree.
// A list counts itself plus each leaf cell once per possible row. This differs
// from the 256-node declaration limit, which counts template nodes only once.
func (w Widget) WidgetCount() int {
	if w.Kind == "list" {
		return 1 + len(w.Row)*w.RowLimit()
	}
	count := 1
	for _, child := range w.Children {
		count += child.WidgetCount()
	}
	return count
}
func (p Panel) WidgetCount() int { return p.Root.WidgetCount() }
func (m Manifest) WidgetCount() int {
	count := 0
	for _, panel := range m.Contributions.Panels {
		count += panel.WidgetCount()
	}
	return count
}
