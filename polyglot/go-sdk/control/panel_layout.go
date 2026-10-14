package control

import (
	"fmt"
	"maps"
	"unicode/utf8"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// PanelLayout contains the manifest's list bounds and cell-to-list mapping.
// Supply it for custom control servers; NewServer snapshots both maps.
type PanelLayout struct {
	Lists map[string]int
	Cells map[string]string
}

func clonePanelLayouts(in map[string]PanelLayout) map[string]PanelLayout {
	out := make(map[string]PanelLayout, len(in))
	for id, p := range in {
		out[id] = PanelLayout{Lists: maps.Clone(p.Lists), Cells: maps.Clone(p.Cells)}
	}
	return out
}

func (c *Client) validatePanelRows(p *pb.PanelPatch) error {
	if p == nil {
		return nil
	}
	for _, op := range p.Ops {
		if rows := op.GetSetRows(); rows != nil {
			limit := 1024
			if layout, declared := c.cfg.Panels[p.PanelId]; declared {
				var ok bool
				limit, ok = layout.Lists[op.WidgetId]
				if !ok {
					return fmt.Errorf("set_rows names undeclared list %q", op.WidgetId)
				}
			}
			if op.RowKey != "" || len(rows.Keys) > limit {
				return fmt.Errorf("set_rows for %q requires no row_key and at most max_rows %d keys", op.WidgetId, limit)
			}
			seen := make(map[string]bool, len(rows.Keys))
			for _, key := range rows.Keys {
				if key == "" || !utf8.ValidString(key) || utf8.RuneCountInString(key) > 128 || seen[key] {
					return fmt.Errorf("set_rows requires distinct nonempty UTF-8 keys of at most 128 characters")
				}
				seen[key] = true
			}
		}
	}
	return nil
}
