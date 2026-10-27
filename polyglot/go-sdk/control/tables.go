package control

import (
	"maps"
	"slices"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// The outbox merges table ops only where the merged op reaches exactly what
// the host reaches applying them in order (§8.4):
//
//   - A table op never merges with, or supersedes, a table op queued before a
//     pending set_value of the same widget: the selection is applied to the
//     rows queued ahead of it, so it neither loses rows it names nor gains a
//     delete that would have come after it.
//   - A merge must leave the selection the ops in order leave, for any
//     selection. In order, a key one op removes stays unselected though a
//     later op brings its row back (a key a patch deletes and upserts is a
//     fresh row, unselected); one merged op may keep it selected. So ops merge
//     only when no row present before them is removed by one and present
//     after them (selectionKept). A later set_value supersedes the pending ones only
//     when the host applies it (setValueApplies); one it may refuse goes
//     behind them, so the earlier selection stands as it would.
//   - A merged op must itself be one the host accepts: at most 100,000 rows,
//     no more cells a row than the declared columns, valid keys distinct
//     within the op, at most 32,768 rows or deleted keys in a field, and
//     within the 1 MiB bound. Otherwise the ops are not merged.
//   - An op the host would refuse whole (a bad or repeated key, too many
//     cells, a set_table past 100,000 rows, or a patch that takes a table
//     this session has stated past 100,000 rows) goes on its own, behind what
//     is pending, so the host refuses it alone: nothing merges into it, and
//     no valid op is lost with it.
//
// The session simulates each table from the first set_table it queues for
// it: the row keys every op queued since would leave. Before that statement
// the table is unknown (a relaunched plugin's panel keeps the previous
// session's rows), and without the table's declared column count the session
// cannot tell which rows the host refuses; in either case table ops are never
// merged or superseded, and go in order. Whether a set_value has been sent
// does not matter.
//
// A widget's slot that holds 256 ops, or whose next op would take it past
// 1 MiB or 32,768 rows, goes out ahead of that op as its own message, and a
// new slot starts: a table op is never refused for it.

// tableKey names one table widget.
type tableKey struct{ panel, widget string }

func isTableOp(op *pb.PatchOp) bool { return op.GetSetTable() != nil || op.GetPatchTable() != nil }

// tableView is a table's contents over the tracked ones while a PanelPatch's
// ops are walked: the changes the ops make, over the session's stated keys.
type tableView struct {
	panel    string
	base     map[string]struct{}
	columns  int // declared; 0 when the table's declaration is not known
	known    bool
	replaced bool            // a set_table in this patch restated the table
	change   map[string]bool // key -> present, over base
	count    int
}

func (v *tableView) has(key string) bool {
	if present, ok := v.change[key]; ok {
		return present
	}
	if v.replaced {
		return false
	}
	_, ok := v.base[key]
	return ok
}

// apply walks op over the view and reports whether the host would refuse it
// whole; a refused op changes nothing. Its bytes are those of the PanelPatch
// it travels in alone.
func (v *tableView) apply(op *pb.PatchOp) (refused bool) {
	alone := proto.Size(&pb.PanelPatch{PanelId: v.panel, Ops: []*pb.PatchOp{op}})
	if alone > manifest.TableMaxPatchBytes || !tableOpFits(op, manifest.TableMaxPatchBytes, false, v.columns) {
		return true
	}
	if s := op.GetSetTable(); s != nil {
		v.known, v.replaced, v.base = true, true, nil
		v.change = make(map[string]bool, len(s.Rows))
		for _, row := range s.Rows {
			v.change[row.Key] = true
		}
		v.count = len(s.Rows)
		return false
	}
	if !v.known {
		return false
	}
	t := op.GetPatchTable()
	trial := make(map[string]bool, len(t.Delete)+len(t.Upsert))
	present := func(key string) bool {
		if p, ok := trial[key]; ok {
			return p
		}
		return v.has(key)
	}
	count := v.count
	for _, key := range t.Delete {
		if present(key) {
			count--
		}
		trial[key] = false
	}
	for _, row := range t.Upsert {
		if !present(row.Key) {
			count++
		}
		trial[row.Key] = true
	}
	if count > manifest.TableMaxRows {
		return true
	}
	for key, p := range trial {
		v.change[key] = p
	}
	v.count = count
	return false
}

// tableEffects walks p's table ops, in order, over what this session has
// stated each table holds. It returns the tables after them, to keep once p
// is queued; for each op of p, whether its table was known before it; and
// whether the host would refuse any of them whole; and for each set_value of
// p, whether the host applies it (setValueApplies). Called with queueMu held.
func (c *Client) tableEffects(p *pb.PanelPatch) (views map[tableKey]*tableView, knownBefore, applies []bool, refused bool) {
	knownBefore = make([]bool, len(p.Ops))
	applies = make([]bool, len(p.Ops))
	for i, op := range p.Ops {
		if v := op.GetSetValue(); v != nil && op.RowKey == "" {
			// Whether the host applies it over the rows the ops ahead of it leave.
			key := tableKey{p.PanelId, op.WidgetId}
			has := func(k string) bool { return false }
			if view := views[key]; view != nil && view.known {
				has = view.has
			} else if base, known := c.tables[key]; known && view == nil {
				has = func(k string) bool { _, ok := base[k]; return ok }
			}
			applies[i] = setValueApplies(v, has)
			continue
		}
		if !isTableOp(op) {
			continue
		}
		if op.RowKey != "" {
			refused = true // a table op addresses a table, not a row's cell
			continue
		}
		key := tableKey{p.PanelId, op.WidgetId}
		v := views[key]
		if v == nil {
			base, known := c.tables[key]
			v = &tableView{panel: p.PanelId, base: base, columns: c.tableColumns(key), known: known, change: map[string]bool{}, count: len(base)}
			if views == nil {
				views = make(map[tableKey]*tableView)
			}
			views[key] = v
		}
		knownBefore[i] = v.known && v.columns > 0
		if v.apply(op) {
			refused = true
		}
	}
	return views, knownBefore, applies, refused
}

// tableColumns is a table's declared column count, or 0 when it is not declared.
func (c *Client) tableColumns(key tableKey) int { return c.cfg.Panels[key.panel].Tables[key.widget] }

// mayMerge reports whether patches to a table may merge: the session has
// stated its rows with a set_table, and its columns are declared.
func (c *Client) mayMerge(key tableKey, views map[tableKey]*tableView) bool {
	if c.tableColumns(key) == 0 {
		return false
	}
	if v := views[key]; v != nil {
		return v.known
	}
	_, known := c.tables[key]
	return known
}

// keepTables records the tables a queued PanelPatch left. Called with
// queueMu held.
func (c *Client) keepTables(views map[tableKey]*tableView) {
	for key, v := range views {
		if !v.known {
			continue
		}
		keys := c.tables[key]
		if v.replaced || keys == nil {
			keys = make(map[string]struct{}, len(v.change))
		}
		for k, present := range v.change {
			if present {
				keys[k] = struct{}{}
			} else {
				delete(keys, k)
			}
		}
		if c.tables == nil {
			c.tables = make(map[tableKey]map[string]struct{})
		}
		c.tables[key] = keys
	}
}

// tableOpFits reports whether the host accepts op's own content: valid keys,
// distinct within the op, at most 100,000 rows in a set_table, no more cells
// a row than the declared columns (32 when undeclared), and op within limit
// bytes. merged also holds each repeated field to
// manifest.TableMaxRowsPerMessage, the bound a merged op keeps.
func tableOpFits(op *pb.PatchOp, limit int, merged bool, columns int) bool {
	rows := op.GetPatchTable().GetUpsert()
	if s := op.GetSetTable(); s != nil {
		rows = s.Rows
		if len(rows) > manifest.TableMaxRows {
			return false
		}
	}
	if merged && tableRowsOf(op) > manifest.TableMaxRowsPerMessage {
		return false
	}
	cells := manifest.TableMaxColumns
	if columns > 0 {
		cells = min(columns, cells)
	}
	seen := make(map[string]bool, len(rows))
	for _, row := range rows {
		key := row.GetKey()
		if key == "" || !utf8.ValidString(key) || utf8.RuneCountInString(key) > manifest.TableMaxKeyChars || len(row.GetCells()) > cells || seen[key] {
			return false
		}
		seen[key] = true
	}
	return proto.Size(op) <= limit
}

// tableMergeCtx is what merging one op into a table widget's pending slot
// needs to know about the table.
type tableMergeCtx struct {
	known   bool   // mayMerge's answer for the table
	columns int    // its declared column count, 0 when undeclared
	base    keySim // its keys before the slot's first op
	applies bool   // for a set_value: the host applies it (setValueApplies)
	isTable bool   // the widget is known to be a table
}

// keySim is a table's row keys as ops are walked over a starting set: base
// (when baseKnown) under the ops' changes. With no known base, a key no op
// has named may or may not be there.
type keySim struct {
	base      map[string]struct{}
	baseKnown bool
	replaced  bool // a set_table restated the table: base no longer counts
	over      map[string]bool
}

// state reports whether key is present, and whether that is certain.
func (s *keySim) state(key string) (present, sure bool) {
	if p, ok := s.over[key]; ok {
		return p, true
	}
	if s.replaced {
		return false, true
	}
	if s.baseKnown {
		_, ok := s.base[key]
		return ok, true
	}
	return false, false
}

func (s keySim) clone() keySim {
	s.over = maps.Clone(s.over)
	if s.over == nil {
		s.over = map[string]bool{}
	}
	return s
}

// apply walks one table op over s (which must own its over map).
func (s *keySim) apply(op *pb.PatchOp) {
	if t := op.GetSetTable(); t != nil {
		s.replaced = true
		s.over = make(map[string]bool, len(t.Rows))
		for _, row := range t.Rows {
			s.over[row.GetKey()] = true
		}
		return
	}
	t := op.GetPatchTable()
	for _, key := range t.GetDelete() {
		s.over[key] = false
	}
	for _, row := range t.GetUpsert() {
		s.over[row.GetKey()] = true
	}
}

// stateBefore is the table's keys before ops[index]: base under the table ops
// ahead of it in the slot.
func stateBefore(base keySim, ops []*pb.PatchOp, index int) keySim {
	s := base.clone()
	for _, op := range ops[:index] {
		if isTableOp(op) {
			s.apply(op)
		}
	}
	return s
}

// selectionKept reports whether run, applied as one merged op from start,
// leaves the selection that its ops applied in order do, whatever the
// selection at start (§8.4, the reference tables.py's apply_op). Each op keeps
// selected only the keys still in its rows, and a key a patch deletes loses
// its selection even when the same patch upserts it again: that is a fresh
// row. So in order a key that one op removes stays unselected even when a
// later op brings its row back, while one merged op may keep it selected. Any
// key present at start may be selected (a person's click selects without
// this end hearing of it in time), so the merge is allowed exactly when no
// key present at start is removed by one op of the run and present after the
// last. That is the only blocker of its kind, and it reads no selection.
func selectionKept(start keySim, run []*pb.PatchOp) bool {
	candidate := func(key string) bool {
		present, sure := start.state(key)
		return present || !sure
	}
	dropped := map[string]bool{}
	var kept map[string]bool // after a set_table: every candidate outside kept was dropped
	cur := start.clone()
	for _, op := range run {
		if t := op.GetSetTable(); t != nil {
			rows := make(map[string]bool, len(t.Rows))
			for _, row := range t.Rows {
				if kept == nil || kept[row.GetKey()] {
					rows[row.GetKey()] = true
				}
			}
			kept = rows
		} else if t := op.GetPatchTable(); t != nil {
			for _, key := range t.Delete {
				if candidate(key) {
					dropped[key] = true
				}
			}
		}
		cur.apply(op)
	}
	for key := range dropped {
		if present, _ := cur.state(key); present {
			return false
		}
	}
	if kept != nil {
		// A set_table in the run made cur certain: its keys are over's.
		for key, present := range cur.over {
			if present && !kept[key] && candidate(key) {
				return false
			}
		}
	}
	return true
}

// setValueApplies reports whether the host applies a table's set_value over
// the keys it will hold then (the reference's select): one key or none, every
// key present. Several keys are refused by a single-select table, which this
// end cannot tell, so they never count as applied.
func setValueApplies(v *pb.Value, has func(string) bool) bool {
	var keys []string
	switch k := v.GetKind().(type) {
	case *pb.Value_S:
		if k.S != "" {
			keys = []string{k.S}
		}
	case *pb.Value_Strings:
		for _, key := range k.Strings.GetValues() {
			if !slices.Contains(keys, key) {
				keys = append(keys, key)
			}
		}
	case *pb.Value_Ints:
		// The reference clears on an empty list of either kind: an SDK that
		// encodes a bare empty list by kind alone sends Value.ints.
		if len(k.Ints.GetValues()) != 0 {
			return false
		}
	default:
		return false
	}
	if len(keys) > 1 {
		return false
	}
	for _, key := range keys {
		if !has(key) {
			return false
		}
	}
	return true
}

// mergeTableOp merges a table op into a widget's pending ops. It touches only
// the table ops after the last pending set_value, and only when the table is
// known (mayMerge): a set_table supersedes them; a patch_table is applied to,
// or folded into, the last of them when the merged op fits. Either merges only where the
// selection comes out as it would in order (selectionKept). Anything else is
// appended, in order.
func mergeTableOp(ops []*pb.PatchOp, op *pb.PatchOp, ctx tableMergeCtx) []*pb.PatchOp {
	lastValue := -1
	for i, prev := range ops {
		if prev.GetSetValue() != nil {
			lastValue = i
		}
	}
	var group []int
	for i := lastValue + 1; i < len(ops); i++ {
		if isTableOp(ops[i]) {
			group = append(group, i)
		}
	}
	if len(group) == 0 {
		return append(ops, op)
	}
	last := group[len(group)-1]
	if !ctx.known {
		// Without the declared columns the host may refuse the op, and
		// without a statement the rows it lands on are unknown.
		return append(ops, op)
	}
	if op.GetSetTable() != nil {
		run := make([]*pb.PatchOp, 0, len(group)+1)
		for _, i := range group {
			run = append(run, ops[i])
		}
		if selectionKept(stateBefore(ctx.base, ops, group[0]), append(run, op)) {
			ops[group[0]] = op
			for j := len(group) - 1; j >= 1; j-- {
				ops = slices.Delete(ops, group[j], group[j]+1)
			}
			return ops
		}
		if selectionKept(stateBefore(ctx.base, ops, last), []*pb.PatchOp{ops[last], op}) {
			ops[last] = op
			return ops
		}
		return append(ops, op)
	}
	if merged := tableMerged(ops[last], op, ctx.known, ctx.columns); merged != nil &&
		selectionKept(stateBefore(ctx.base, ops, last), []*pb.PatchOp{ops[last], op}) {
		ops[last] = merged
		return ops
	}
	return append(ops, op)
}

// tableMerged is later merged into earlier, or nil when they must stay apart.
// known is mayMerge's answer for the table.
func tableMerged(earlier, later *pb.PatchOp, known bool, columns int) *pb.PatchOp {
	var merged *pb.PatchOp
	switch {
	case !known:
		return nil
	case later.GetSetTable() != nil:
		return later
	case earlier.GetSetTable() != nil:
		merged = tableApplied(earlier, later)
	default:
		merged = tableFolded(earlier, later)
	}
	if !tableOpFits(merged, TablePatchBudget, true, columns) {
		return nil
	}
	return merged
}

// supersedeSelections drops every pending set_value ahead of one the host will
// apply, which sets the whole selection whatever came before it, and merges
// the table ops that leaves side by side, in order, by tableMerged's rules.
// Rows are what they were; the selection is the new set_value's either way.
func supersedeSelections(ops []*pb.PatchOp, known bool, columns int) []*pb.PatchOp {
	ops = slices.DeleteFunc(ops, func(op *pb.PatchOp) bool { return op.GetSetValue() != nil })
	out := ops[:0]
	for _, op := range ops {
		if n := len(out); n > 0 && isTableOp(op) && isTableOp(out[n-1]) {
			if merged := tableMerged(out[n-1], op, known, columns); merged != nil {
				out[n-1] = merged
				continue
			}
		}
		out = append(out, op)
	}
	return out
}
