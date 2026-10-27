package plugin

import (
	"errors"
	"fmt"
	"slices"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// TableRow is one row of a table widget: its key and its cells. Give Cells,
// the values in the declared column order (a missing trailing cell is blank),
// or ByColumn, values by column id (an absent column is blank), not both. A
// cell is a string, a Go number (sent as a float, as the host shows numbers),
// a bool, or nil for a blank cell.
type TableRow struct {
	Key      string
	Cells    []any
	ByColumn map[string]any
}

// Cells is a TableRow of values in column order.
func Cells(key string, cells ...any) TableRow { return TableRow{Key: key, Cells: cells} }

// tableCell converts one cell as the Python SDK's _table_cell does, except
// that a type that is not text, a number or a flag is an error rather than
// its string form.
func tableCell(v any) (*pb.Value, error) {
	switch x := v.(type) {
	case nil:
		return &pb.Value{}, nil
	case string:
		return String(x), nil
	case bool:
		return Bool(x), nil
	case float64:
		return Float(x), nil
	case float32:
		return Float(float64(x)), nil
	case int:
		return Float(float64(x)), nil
	case int8:
		return Float(float64(x)), nil
	case int16:
		return Float(float64(x)), nil
	case int32:
		return Float(float64(x)), nil
	case int64:
		return Float(float64(x)), nil
	case uint:
		return Float(float64(x)), nil
	case uint8:
		return Float(float64(x)), nil
	case uint16:
		return Float(float64(x)), nil
	case uint32:
		return Float(float64(x)), nil
	case uint64:
		return Float(float64(x)), nil
	}
	return nil, fmt.Errorf("a table cell is a string, a number, a bool or nil, not %T", v)
}

// tableRows builds the wire rows, checking each key and cell count.
func (p Panel) tableRows(table string, rows []TableRow) ([]*pb.TableRow, error) {
	columns, declared := p.tables[table]
	out := make([]*pb.TableRow, 0, len(rows))
	for _, row := range rows {
		if row.Key == "" || !utf8.ValidString(row.Key) || utf8.RuneCountInString(row.Key) > manifest.TableMaxKeyChars {
			return nil, fmt.Errorf("table row keys are non-empty and at most %d characters, not %q", manifest.TableMaxKeyChars, row.Key)
		}
		cells := row.Cells
		if row.ByColumn != nil {
			if row.Cells != nil {
				return nil, fmt.Errorf("table row %q gives both Cells and ByColumn", row.Key)
			}
			if !declared {
				return nil, fmt.Errorf("cells by column id need table %q's declared columns", table)
			}
			unknown := make([]string, 0)
			for id := range row.ByColumn {
				if !slices.Contains(columns, id) {
					unknown = append(unknown, id)
				}
			}
			if len(unknown) > 0 {
				slices.Sort(unknown)
				return nil, fmt.Errorf("table %q has no column %q", table, unknown[0])
			}
			cells = make([]any, len(columns))
			for i, id := range columns {
				cells[i] = row.ByColumn[id]
			}
		}
		if len(cells) > manifest.TableMaxColumns {
			return nil, fmt.Errorf("a table row has at most %d cells, not %d", manifest.TableMaxColumns, len(cells))
		}
		if declared && len(cells) > len(columns) {
			return nil, fmt.Errorf("table %q has %d columns; row %q has %d cells", table, len(columns), row.Key, len(cells))
		}
		values := make([]*pb.Value, len(cells))
		for i, cell := range cells {
			v, err := tableCell(cell)
			if err != nil {
				return nil, err
			}
			values[i] = v
		}
		out = append(out, &pb.TableRow{Key: row.Key, Cells: values})
	}
	return out, nil
}

// tableBudget is what one table op's rows may encode to: under the 1 MiB
// bound for the whole PanelPatch, ids and framing included.
func (p Panel) tableBudget(table string) int {
	return manifest.TableMaxPatchBytes - 1024 - len(p.ID) - len(table)
}

// tableChunks splits rows, in order, into runs whose encoding stays under
// budget and whose count is at most manifest.TableMaxRowsPerMessage (100,000
// short-key rows fit in 1 MiB but not under the host's repeated-field bound);
// the first run is under first bytes when it shares its message with deletes,
// which are a separate field and counted on their own. A row larger than
// budget alone is an error before anything is sent.
func tableChunks(rows []*pb.TableRow, budget, first int) ([][]*pb.TableRow, error) {
	chunks := [][]*pb.TableRow{nil}
	size := 0
	for _, row := range rows {
		cost := proto.Size(row) + 8 // its tag and length in the message around it
		if cost > budget {
			return nil, fmt.Errorf("table row %q encodes to %d bytes, past one message's %d", row.Key, cost, budget)
		}
		limit := budget
		if len(chunks) == 1 {
			limit = first
		}
		if ((len(chunks[len(chunks)-1]) > 0 || limit < cost) && size+cost > limit) || len(chunks[len(chunks)-1]) >= manifest.TableMaxRowsPerMessage {
			chunks = append(chunks, nil)
			size = 0
		}
		chunks[len(chunks)-1] = append(chunks[len(chunks)-1], row)
		size += cost
	}
	return chunks, nil
}

// SetTable states a table widget's rows, whole and in order, replacing the
// last. Keys are distinct, non-empty and at most 128 characters; a table holds
// at most 100,000 rows. A table larger than one message (1 MiB) goes as a
// set_table followed by appending patch_tables, which the host applies in
// order. Each op is its own Send; an error part-way (a full queue) leaves the
// ops already queued in place, and a later SetTable restates them.
func (p Panel) SetTable(table string, rows ...TableRow) error {
	if len(rows) > manifest.TableMaxRows {
		return fmt.Errorf("a table holds at most %d rows, not %d", manifest.TableMaxRows, len(rows))
	}
	built, err := p.tableRows(table, rows)
	if err != nil {
		return err
	}
	seen := make(map[string]bool, len(built))
	for _, row := range built {
		if seen[row.Key] {
			return errors.New("table row keys must be distinct")
		}
		seen[row.Key] = true
	}
	budget := p.tableBudget(table)
	chunks, err := tableChunks(built, budget, budget)
	if err != nil {
		return err
	}
	if err := p.Patch(&pb.PatchOp{WidgetId: table, Op: &pb.PatchOp_SetTable{SetTable: &pb.TableRows{Rows: chunks[0]}}}); err != nil {
		return err
	}
	for _, chunk := range chunks[1:] {
		if err := p.Patch(&pb.PatchOp{WidgetId: table, Op: &pb.PatchOp_PatchTable{PatchTable: &pb.TablePatch{Upsert: chunk}}}); err != nil {
			return err
		}
	}
	return nil
}

// PatchTable changes a table's rows by key: every key in remove goes, then
// each row in upsert replaces the row with its key in place or is appended,
// so a key removed and upserted in one call is appended fresh. Rows take the
// same form as SetTable's. The host refuses, whole, a patch that would take
// the table past 100,000 rows. A patch larger than one message is split, the
// deletes first; the last run of deletes shares its message with the first
// run of upserts. Nothing is sent for an empty patch.
func (p Panel) PatchTable(table string, upsert []TableRow, remove []string) error {
	built, err := p.tableRows(table, upsert)
	if err != nil {
		return err
	}
	budget := p.tableBudget(table)
	deleting := [][]string{nil}
	size := 0
	for _, key := range remove {
		cost := len(key) + 4
		if len(deleting[len(deleting)-1]) > 0 && (size+cost > budget || len(deleting[len(deleting)-1]) >= manifest.TableMaxRowsPerMessage) {
			deleting = append(deleting, nil)
			size = 0
		}
		deleting[len(deleting)-1] = append(deleting[len(deleting)-1], key)
		size += cost
	}
	upserting := [][]*pb.TableRow{nil}
	if len(built) > 0 {
		if upserting, err = tableChunks(built, budget, budget-size); err != nil {
			return err
		}
	}
	send := func(keys []string, rows []*pb.TableRow) error {
		return p.Patch(&pb.PatchOp{WidgetId: table, Op: &pb.PatchOp_PatchTable{PatchTable: &pb.TablePatch{Delete: keys, Upsert: rows}}})
	}
	for _, keys := range deleting[:len(deleting)-1] {
		if err := send(keys, nil); err != nil {
			return err
		}
	}
	for i, run := range upserting {
		var keys []string
		if i == 0 {
			keys = deleting[len(deleting)-1]
		}
		if len(keys) > 0 || len(run) > 0 {
			if err := send(keys, run); err != nil {
				return err
			}
		}
	}
	return nil
}

// checkTableOp is Patch's local check of a table op, raw or built: no row key,
// at most 100,000 rows in a statement, and valid row keys.
func checkTableOp(op *pb.PatchOp) error {
	var rows []*pb.TableRow
	switch x := op.Op.(type) {
	case *pb.PatchOp_SetTable:
		if x.SetTable == nil {
			return errors.New("set_table requires TableRows")
		}
		if len(x.SetTable.Rows) > manifest.TableMaxRows {
			return fmt.Errorf("a table holds at most %d rows, not %d", manifest.TableMaxRows, len(x.SetTable.Rows))
		}
		rows = x.SetTable.Rows
	case *pb.PatchOp_PatchTable:
		if x.PatchTable == nil {
			return errors.New("patch_table requires a TablePatch")
		}
		rows = x.PatchTable.Upsert
	default:
		return nil
	}
	if op.RowKey != "" {
		return errors.New("a table op addresses a table, not a row cell")
	}
	if n := max(len(rows), len(op.GetPatchTable().GetDelete())); n > manifest.TableMaxRowsPerMessage {
		return fmt.Errorf("one table message carries at most %d rows, or deleted keys, not %d; SetTable and PatchTable split", manifest.TableMaxRowsPerMessage, n)
	}
	for _, row := range rows {
		key := row.GetKey()
		if key == "" || !utf8.ValidString(key) || utf8.RuneCountInString(key) > manifest.TableMaxKeyChars {
			return fmt.Errorf("table row keys are non-empty and at most %d characters, not %q", manifest.TableMaxKeyChars, key)
		}
		if len(row.GetCells()) > manifest.TableMaxColumns {
			return fmt.Errorf("a table row has at most %d cells, not %d", manifest.TableMaxColumns, len(row.GetCells()))
		}
	}
	return nil
}
