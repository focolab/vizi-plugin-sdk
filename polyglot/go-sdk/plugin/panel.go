package plugin

import (
	"bytes"
	"errors"
	"fmt"
	"image/png"
	"math"
	"slices"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// Panel sends updates to a manifest-declared panel. control.Client preserves
// dropdown order and returns admission refusals; callers must handle errors.
// Non-image state sent before first open is kept by the host. Re-sending on
// PanelOpened is optional. Images sent before the P grant are held (latest per
// widget/row), bounded at 16 MiB per panel, and flushed on P installation or
// before the panel-opened handler.
type Panel struct {
	ID     string
	Client *control.Client
	rowKey string
	cells  map[string]string // row widget -> declaring list
	lists  map[string]int    // immutable declaration snapshot, when bound through PanelRef
}

func (p Panel) Patch(ops ...*pb.PatchOp) error {
	if p.ID == "" || p.Client == nil {
		return errors.New("panel requires an ID and client")
	}
	prepared := make([]*pb.PatchOp, 0, len(ops))
	for _, op := range ops {
		if op == nil || op.WidgetId == "" || op.Op == nil {
			return errors.New("patch requires widget IDs and operations")
		}
		if p.rowKey != "" {
			if op.RowKey != "" && op.RowKey != p.rowKey {
				return errors.New("patch row conflicts with panel row handle")
			}
			op = proto.Clone(op).(*pb.PatchOp)
			op.RowKey = p.rowKey
		}
		if op.RowKey != "" {
			if err := validRowKey(op.RowKey); err != nil {
				return err
			}
		}
		if rows, ok := op.Op.(*pb.PatchOp_SetRows); ok {
			if op.RowKey != "" {
				return errors.New("set_rows addresses a list, not a row cell")
			}
			if rows.SetRows == nil {
				return errors.New("set_rows requires RowKeys")
			}
			limit := 1024
			if n, ok := p.lists[op.WidgetId]; ok {
				limit = n
			}
			if err := validateRowKeys(rows.SetRows.Keys, limit); err != nil {
				return err
			}
		}
		prepared = append(prepared, op)
	}
	return p.Client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_PanelPatch{PanelPatch: &pb.PanelPatch{PanelId: p.ID, Ops: prepared}}})
}

// Row returns a value handle for one row's cells. Every operation, including
// Patch and SetPNG, carries that row key. It does not change the original panel.
// Empty selects ordinary non-cell widgets; the host validates cell membership.
func (p Panel) Row(key string) Panel { p.rowKey = key; return p }

// SetRows replaces the whole ordered key list, without truncation. Keys are
// nonempty, distinct UTF-8 strings of at most 128 characters. PanelRef bindings
// check the declared max_rows; raw handles also use the client's Config.Panels
// when supplied. Without that metadata only the absolute 1024 ceiling is known.
func (p Panel) SetRows(widget string, keys ...string) error {
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetRows{SetRows: &pb.RowKeys{Keys: slices.Clone(keys)}}})
}

func validRowKey(key string) error {
	if key == "" || !utf8.ValidString(key) || utf8.RuneCountInString(key) > 128 {
		return errors.New("row keys must be nonempty UTF-8 strings of at most 128 characters")
	}
	return nil
}
func validateRowKeys(keys []string, limit int) error {
	if len(keys) > limit {
		return fmt.Errorf("set_rows has %d keys, exceeds max_rows %d", len(keys), limit)
	}
	seen := make(map[string]bool, len(keys))
	for _, key := range keys {
		if err := validRowKey(key); err != nil {
			return err
		}
		if seen[key] {
			return fmt.Errorf("duplicate row key %q", key)
		}
		seen[key] = true
	}
	return nil
}

func (p Panel) SetValue(widget string, value *pb.Value) error {
	if value == nil || value.Kind == nil {
		return errors.New("missing widget value")
	}
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetValue{SetValue: value}})
}

// SetProgress updates a noninteractive indicator through set_progress. Values
// clamp to [0, 1], NaN shows empty, and indeterminate ignores value entirely.
func (p Panel) SetProgress(widget string, value float64, indeterminate bool) error {
	if indeterminate || math.IsNaN(value) {
		value = 0
	} else {
		value = max(0, min(1, value))
	}
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetProgress{SetProgress: &pb.Progress{Value: value, Indeterminate: indeterminate}}})
}
func (p Panel) SetEnabled(widget string, value bool) error {
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetEnabled{SetEnabled: value}})
}
func (p Panel) SetVisible(widget string, value bool) error {
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetVisible{SetVisible: value}})
}
func (p Panel) SetOptions(widget string, values ...string) error {
	return p.Patch(&pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: values}}})
}

// SetPNG publishes encoded PNG bytes to a P segment. The installed region table
// selects the arena; optional Region.id and declaration order do not. The host
// validates the full PNG on decode; this method checks its header before reserving.
// Segment zero selects the installed P segment whose subject is this panel ID.
func (p Panel) SetPNG(session *control.Session, segment uint64, widget string, data []byte) error {
	if session == nil || session.Client != p.Client || p.ID == "" || widget == "" {
		return errors.New("image requires this panel's session and widget ID")
	}
	if p.rowKey != "" {
		if err := validRowKey(p.rowKey); err != nil {
			return err
		}
	}
	if _, err := png.DecodeConfig(bytes.NewReader(data)); err != nil {
		return err
	}
	if session.Memory == nil || session.Memory.Registry == nil {
		return errors.New("image requires session memory")
	}
	return session.PublishPanelImage(control.PanelImageKey{Panel: p.ID, Widget: widget, Row: p.rowKey, List: p.cells[widget]}, segment, data)
}
