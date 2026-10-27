package control

import (
	"context"
	"fmt"
	"maps"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// tableClient is a client whose panel "p" declares table "t" with one column.
func tableClient(t *testing.T) *Client {
	c := stateClient(t)
	c.cfg.Panels = map[string]PanelLayout{"p": {Tables: map[string]int{"t": 1}}}
	return c
}

func tableRow(key string, cell string) *pb.TableRow {
	return &pb.TableRow{Key: key, Cells: []*pb.Value{{Kind: &pb.Value_S{S: cell}}}}
}
func setTable(rows ...*pb.TableRow) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_SetTable{SetTable: &pb.TableRows{Rows: rows}}}
}
func patchTable(remove []string, upsert ...*pb.TableRow) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_PatchTable{PatchTable: &pb.TablePatch{Delete: remove, Upsert: upsert}}}
}

// hostTable applies table ops as §8.4 says the host does: set_table replaces;
// patch_table removes its deletes, then upserts in place or appends.
func hostTable(rows []string, ops []*pb.PatchOp) []string {
	for _, op := range ops {
		if s := op.GetSetTable(); s != nil {
			rows = rows[:0:0]
			for _, r := range s.Rows {
				rows = append(rows, r.Key+"="+r.Cells[0].GetS())
			}
		}
		if p := op.GetPatchTable(); p != nil {
			gone := make(map[string]bool, len(p.Delete))
			for _, key := range p.Delete {
				gone[key] = true
			}
			rows = slices.DeleteFunc(rows, func(r string) bool { return gone[strings.SplitN(r, "=", 2)[0]] })
			at := make(map[string]int, len(rows))
			for i, r := range rows {
				at[strings.SplitN(r, "=", 2)[0]] = i
			}
			for _, u := range p.Upsert {
				if i, ok := at[u.Key]; ok {
					rows[i] = u.Key + "=" + u.Cells[0].GetS()
				} else {
					at[u.Key] = len(rows)
					rows = append(rows, u.Key+"="+u.Cells[0].GetS())
				}
			}
		}
	}
	return rows
}

func drainedOps(t *testing.T, c *Client) []*pb.PatchOp {
	t.Helper()
	var ops []*pb.PatchOp
	for _, m := range nextBatch(t, c) {
		ops = append(ops, m.GetPanelPatch().Ops...)
	}
	return ops
}

func TestTablePatchMergesIntoPendingSetTable(t *testing.T) {
	c := tableClient(t)
	// Stated empty first: no row could be selected when the statement lands,
	// so no merge below can keep a selection the ops in order would drop.
	stated(t, c)
	for _, op := range []*pb.PatchOp{
		setTable(tableRow("a", "1"), tableRow("b", "2"), tableRow("c", "3")),
		patchTable([]string{"b"}, tableRow("c", "30"), tableRow("d", "4")),
		patchTable(nil, tableRow("b", "20")),
	} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	ops := drainedOps(t, c)
	if len(ops) != 1 || ops[0].GetSetTable() == nil {
		t.Fatal("patches were not applied to the pending statement", ops)
	}
	if got := hostTable(nil, ops); !slices.Equal(got, []string{"a=1", "c=30", "d=4", "b=20"}) {
		t.Fatal(got)
	}
	if got := c.QueueStats().Merged; got != 2 {
		t.Fatal(got)
	}
}

// stated sends a set_table of rows and drains it, so the session knows what
// the table holds and later patches may fold.
func stated(t *testing.T, c *Client, rows ...*pb.TableRow) {
	t.Helper()
	if err := c.Send(patch("p", setTable(rows...))); err != nil {
		t.Fatal(err)
	}
	drainedOps(t, c)
}

func TestTablePatchesFoldAndSetTableSupersedes(t *testing.T) {
	c := tableClient(t)
	stated(t, c, tableRow("x", "0"), tableRow("b", "0"), tableRow("z", "0"))
	if err := c.Send(patch("p", patchTable([]string{"x"}, tableRow("a", "1"), tableRow("b", "2")))); err != nil {
		t.Fatal(err)
	}
	// The later patch deletes b, dropping the earlier upsert, and upserts y.
	if err := c.Send(patch("p", patchTable([]string{"b"}, tableRow("y", "9")))); err != nil {
		t.Fatal(err)
	}
	ops := drainedOps(t, c)
	if len(ops) != 1 {
		t.Fatal(ops)
	}
	p := ops[0].GetPatchTable()
	if !slices.Equal(p.Delete, []string{"x", "b"}) || len(p.Upsert) != 2 || p.Upsert[0].Key != "a" || p.Upsert[1].Key != "y" {
		t.Fatal(p)
	}
	before := []string{"x=0", "b=0", "z=0"}
	if got, want := hostTable(slices.Clone(before), ops), []string{"z=0", "a=1", "y=9"}; !slices.Equal(got, want) {
		t.Fatal(got, want)
	}

	// A row dropped by one patch and brought back by the next stays apart:
	// in order the host deselects it, and one merged patch would not.
	sendAll(t, c, patchTable([]string{"z"}), patchTable(nil, tableRow("z", "1")))
	if ops := opsUntil(t, c, 2); !slices.Equal(ops[0].GetPatchTable().GetDelete(), []string{"z"}) {
		t.Fatal(ops)
	}

	// A statement restates every pending table op, patches included.
	if err := c.Send(patch("p", patchTable(nil, tableRow("q", "1")))); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", setTable(tableRow("s", "1")))); err != nil {
		t.Fatal(err)
	}
	ops = drainedOps(t, c)
	if len(ops) != 1 || ops[0].GetSetTable() == nil || ops[0].GetSetTable().Rows[0].Key != "s" {
		t.Fatal(ops)
	}
}

func TestTableSelectionFollowsPendingTableOps(t *testing.T) {
	c := tableClient(t)
	value := &pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_Strings{Strings: &pb.StringList{Values: []string{"n"}}}}}}
	for _, op := range []*pb.PatchOp{value, setTable(tableRow("n", "1")), proto.Clone(value).(*pb.PatchOp)} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	ops := drainedOps(t, c)
	if len(ops) != 2 || ops[0].GetSetTable() == nil || ops[1].GetSetValue() == nil {
		t.Fatal("the selection must follow the rows it names", ops)
	}
}

// oracleTable is a table as the host holds it, by the reference's rules
// (tables.py: apply_op and select), the oracle the outbox is checked against.
type oracleTable struct {
	keys      []string
	cells     map[string]string
	selection []string
	multiple  bool
}

func (o *oracleTable) apply(op *pb.PatchOp) {
	switch {
	case op.GetSetTable() != nil:
		o.keys, o.cells = nil, map[string]string{}
		for _, row := range op.GetSetTable().GetRows() {
			o.keys = append(o.keys, row.Key)
			o.cells[row.Key] = row.Cells[0].GetS()
		}
	case op.GetPatchTable() != nil:
		p := op.GetPatchTable()
		o.keys = slices.DeleteFunc(o.keys, func(k string) bool { return slices.Contains(p.Delete, k) })
		cells := map[string]string{}
		for _, k := range o.keys {
			cells[k] = o.cells[k]
		}
		for _, row := range p.Upsert {
			if _, ok := cells[row.Key]; !ok {
				o.keys = append(o.keys, row.Key)
			}
			cells[row.Key] = row.Cells[0].GetS()
		}
		o.cells = cells
	case op.GetSetValue() != nil:
		var keys []string
		switch v := op.GetSetValue().GetKind().(type) {
		case *pb.Value_S:
			if v.S != "" {
				keys = []string{v.S}
			}
		case *pb.Value_Strings:
			for _, k := range v.Strings.Values {
				if !slices.Contains(keys, k) {
					keys = append(keys, k)
				}
			}
		case *pb.Value_Ints:
			if len(v.Ints.Values) != 0 {
				return // refused: a table takes keys
			}
		default:
			return
		}
		if len(keys) > 1 && !o.multiple {
			return // refused: the selection is unchanged
		}
		for _, k := range keys {
			if _, ok := o.cells[k]; !ok {
				return
			}
		}
		o.selection = keys
		return
	}
	// A key the op deletes loses its selection, even when the same patch
	// upserts it again: that is a fresh row.
	deleted := op.GetPatchTable().GetDelete()
	o.selection = slices.DeleteFunc(o.selection, func(k string) bool {
		_, ok := o.cells[k]
		return !ok || slices.Contains(deleted, k)
	})
}

func (o oracleTable) String() string {
	rows := make([]string, len(o.keys))
	for i, k := range o.keys {
		rows[i] = k + "=" + o.cells[k]
	}
	return fmt.Sprintf("rows %v selected %v", rows, o.selection)
}

// drainAllOps drains until nothing is queued.
func drainAllOps(t *testing.T, c *Client) []*pb.PatchOp {
	t.Helper()
	var ops []*pb.PatchOp
	for {
		c.queueMu.Lock()
		idle := len(c.out) == 0 && len(c.order) == 0
		c.queueMu.Unlock()
		if idle {
			return ops
		}
		ops = append(ops, drainedOps(t, c)...)
	}
}

// Merging is sound: any sequence of set_table, patch_table and set_value,
// drained merged, reaches the rows AND the selection the ops reach one by one
// (§8.4), whatever the host held and had selected before them, and whether
// the table is single- or multi-select. A table the session has stated and one
// it has not (a relaunched plugin's panel keeps rows and a selection) both.
func TestTableMergeMatchesSequentialApplication(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	keys := []string{"a", "b", "c", "d", "e"}
	subset := func() []string {
		var out []string
		for _, i := range r.Perm(len(keys)) {
			if r.IntN(2) == 0 {
				out = append(out, keys[i])
			}
		}
		return out
	}
	sentOps, drained := 0, 0
	for trial := range 4000 {
		c := tableClient(t)
		start := oracleTable{cells: map[string]string{}, multiple: r.IntN(2) == 0}
		var rows []*pb.TableRow
		for _, k := range subset() {
			start.keys = append(start.keys, k)
			start.cells[k] = "s"
			rows = append(rows, tableRow(k, "s"))
		}
		if r.IntN(4) != 0 {
			stated(t, c, rows...)
		}
		// What a person (or an earlier session) left selected.
		for _, k := range start.keys {
			if r.IntN(3) == 0 && (start.multiple || len(start.selection) == 0) {
				start.selection = append(start.selection, k)
			}
		}
		want := start
		want.keys, want.selection = slices.Clone(start.keys), slices.Clone(start.selection)
		want.cells = maps.Clone(start.cells)
		var sent []*pb.PatchOp
		for n := range 1 + r.IntN(8) {
			var op *pb.PatchOp
			cell := fmt.Sprint(trial, n)
			switch r.IntN(6) {
			case 0:
				var rows []*pb.TableRow
				for _, k := range subset() {
					rows = append(rows, tableRow(k, cell))
				}
				op = setTable(rows...)
			case 1:
				pick := append([]string{""}, keys...)
				op = &pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_S{S: pick[r.IntN(len(pick))]}}}}
			case 2:
				chosen := subset()
				op = selection(chosen[:min(len(chosen), r.IntN(3))]...)
			default:
				var remove []string
				var upsert []*pb.TableRow
				for _, i := range r.Perm(len(keys)) {
					k := keys[i]
					switch r.IntN(4) {
					case 0:
						remove = append(remove, k)
					case 1:
						upsert = append(upsert, tableRow(k, cell))
					case 2:
						remove = append(remove, k)
						upsert = append(upsert, tableRow(k, cell))
					}
				}
				op = patchTable(remove, upsert...)
			}
			sent = append(sent, op)
			want.apply(op)
			if err := c.Send(patch("p", op)); err != nil {
				t.Fatal(err)
			}
		}
		got := start
		got.keys, got.selection = slices.Clone(start.keys), slices.Clone(start.selection)
		got.cells = maps.Clone(start.cells)
		ops := drainAllOps(t, c)
		for _, op := range ops {
			got.apply(op)
		}
		sentOps, drained = sentOps+len(sent), drained+len(ops)
		if got.String() != want.String() {
			t.Fatalf("trial %d from %v (multiple %t):\nsent   %v\nmerged %v\ngot    %v\nwant   %v", trial, start, start.multiple, sent, ops, got, want)
		}
	}
	if drained >= sentOps*9/10 {
		t.Fatalf("too little merged to test merging: %d ops sent, %d drained", sentOps, drained)
	}
	t.Logf("%d ops sent, %d drained", sentOps, drained)
}

// §8.4: a patch that would merge the slot past 1 MiB is queued behind it, in
// order, and is never a fault or a refusal.
func TestTableMergeStopsAtTheMessageBound(t *testing.T) {
	c := tableClient(t)
	big := strings.Repeat("x", 600<<10)
	if err := c.Send(patch("p", setTable(tableRow("a", big)))); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", patchTable(nil, tableRow("b", big)))); err != nil {
		t.Fatal("a patch past the bound must queue, not fail:", err)
	}
	// Later small patches merge into the second slot, behind the first.
	if err := c.Send(patch("p", patchTable(nil, tableRow("c", "small")))); err != nil {
		t.Fatal(err)
	}
	var messages []*pb.PluginMsg
	for len(messages) < 3 {
		batch := nextBatch(t, c)
		messages = append(messages, batch...)
		if len(batch) == 0 {
			break
		}
	}
	if len(messages) != 3 {
		t.Fatal(len(messages))
	}
	var ops []*pb.PatchOp
	for _, m := range messages {
		if size := proto.Size(m); size > TablePatchBudget+1024 {
			t.Fatal("a drained table message is past 1 MiB:", size)
		}
		ops = append(ops, m.GetPanelPatch().Ops...)
	}
	if ops[0].GetSetTable() == nil || ops[1].GetPatchTable().Upsert[0].Key != "b" || ops[2].GetPatchTable().Upsert[0].Key != "c" {
		t.Fatal(ops)
	}
	if got := hostTable(nil, ops); len(got) != 3 || !strings.HasPrefix(got[2], "c=") {
		t.Fatal(len(got))
	}
	if c.ctx.Err() != nil || c.QueueStats().Refused != 0 {
		t.Fatal("bound handling faulted or refused", c.QueueStats())
	}
}

// A merge also stops at the row cap, even far under 1 MiB: the host bounds
// repeated fields whatever the bytes. Rows and deleted keys count on their own.
func TestTableMergeStopsAtTheRowCap(t *testing.T) {
	rows := func(prefix string, n int) []*pb.TableRow {
		out := make([]*pb.TableRow, n)
		for i := range out {
			out[i] = tableRow(fmt.Sprint(prefix, i), "")
		}
		return out
	}
	keys := func(prefix string, n int) []string {
		out := make([]string, n)
		for i := range out {
			out[i] = fmt.Sprint(prefix, i)
		}
		return out
	}
	drainAll := func(t *testing.T, c *Client, want int) []*pb.PatchOp {
		t.Helper()
		var messages []*pb.PluginMsg
		for len(messages) < want {
			messages = append(messages, nextBatch(t, c)...)
		}
		var ops []*pb.PatchOp
		for _, m := range messages {
			for _, op := range m.GetPanelPatch().Ops {
				if n := tableRowsOf(op); n > manifest.TableMaxRowsPerMessage {
					t.Fatal("a drained table op carries", n)
				}
				ops = append(ops, op)
			}
		}
		if c.QueueStats().Refused != 0 || c.ctx.Err() != nil {
			t.Fatal("the cap refused or faulted", c.QueueStats())
		}
		return ops
	}
	t.Run("rows", func(t *testing.T) {
		c := tableClient(t)
		// 30,000 rows pending; 3,000 more would pass 32,768, so they queue behind.
		if err := c.Send(patch("p", setTable(rows("a", 30000)...))); err != nil {
			t.Fatal(err)
		}
		if err := c.Send(patch("p", patchTable(nil, rows("b", 3000)...))); err != nil {
			t.Fatal(err)
		}
		ops := drainAll(t, c, 2)
		if len(ops) != 2 || ops[0].GetSetTable() == nil || len(ops[1].GetPatchTable().GetUpsert()) != 3000 {
			t.Fatal(len(ops))
		}
		if got := hostTable(nil, ops); len(got) != 33000 {
			t.Fatal(len(got))
		}
	})
	t.Run("deletes on their own", func(t *testing.T) {
		c := tableClient(t)
		stated(t, c)
		// 20,000 deletes and 20,000 upserts fold (each field under the cap,
		// though 40,000 together); 20,000 more deletes would pass it.
		if err := c.Send(patch("p", patchTable(keys("x", 20000), rows("u", 20000)...))); err != nil {
			t.Fatal(err)
		}
		if err := c.Send(patch("p", patchTable(keys("y", 10)))); err != nil {
			t.Fatal(err)
		}
		if err := c.Send(patch("p", patchTable(keys("z", 20000)))); err != nil {
			t.Fatal(err)
		}
		ops := drainAll(t, c, 2)
		if len(ops) != 2 || len(ops[0].GetPatchTable().Delete) != 20010 || len(ops[1].GetPatchTable().Delete) != 20000 {
			t.Fatal(len(ops))
		}
	})
}

func selection(keys ...string) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_Strings{Strings: &pb.StringList{Values: keys}}}}}
}

// opsUntil drains until want ops have gone out.
func opsUntil(t *testing.T, c *Client, want int) []*pb.PatchOp {
	t.Helper()
	var ops []*pb.PatchOp
	for len(ops) < want {
		ops = append(ops, drainedOps(t, c)...)
	}
	if len(ops) != want {
		t.Fatalf("drained %d ops, want %d: %v", len(ops), want, ops)
	}
	return ops
}

func sendAll(t *testing.T, c *Client, ops ...*pb.PatchOp) {
	t.Helper()
	for _, op := range ops {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
}

// A set_table never overtakes a pending set_value, nor supersedes the table
// ops queued before it: the selection is applied to the rows ahead of it.
func TestSetTableNeverOvertakesPendingSetValue(t *testing.T) {
	t.Run("behind a patch", func(t *testing.T) {
		c := tableClient(t)
		stated(t, c)
		sendAll(t, c, patchTable(nil, tableRow("k", "1")), selection("k"), setTable(tableRow("k", "2"), tableRow("m", "3")))
		ops := opsUntil(t, c, 3)
		if ops[0].GetPatchTable() == nil || ops[1].GetSetValue() == nil || ops[2].GetSetTable() == nil {
			t.Fatal(ops)
		}
	})
	t.Run("behind a set_table", func(t *testing.T) {
		c := tableClient(t)
		sendAll(t, c, setTable(tableRow("k", "1")), selection("k"), setTable(tableRow("n", "2")))
		ops := opsUntil(t, c, 3)
		if ops[0].GetSetTable().GetRows()[0].GetKey() != "k" || ops[1].GetSetValue() == nil || ops[2].GetSetTable().GetRows()[0].GetKey() != "n" {
			t.Fatal(ops)
		}
	})
}

// A patch never merges into a table op ahead of a pending set_value: a delete
// stays after the selection it follows. Table ops after the set_value merge
// with each other, and a superseded set_value lets the ops around it merge.
func TestNoTableMergeAcrossPendingSetValue(t *testing.T) {
	c := tableClient(t)
	stated(t, c, tableRow("k", "0"))
	sendAll(t, c, patchTable(nil, tableRow("a", "1")), selection("k"), patchTable([]string{"k"}), patchTable(nil, tableRow("b", "2")))
	ops := opsUntil(t, c, 3)
	if ops[0].GetPatchTable() == nil || ops[1].GetSetValue() == nil || !slices.Equal(ops[2].GetPatchTable().GetDelete(), []string{"k"}) || len(ops[2].GetPatchTable().GetUpsert()) != 1 {
		t.Fatal(ops)
	}
	sendAll(t, c, patchTable(nil, tableRow("c", "1")), selection("a"), patchTable(nil, tableRow("d", "2")), selection("c"))
	ops = opsUntil(t, c, 2)
	if len(ops[0].GetPatchTable().GetUpsert()) != 2 || !slices.Equal(ops[1].GetSetValue().GetStrings().GetValues(), []string{"c"}) {
		t.Fatal(ops)
	}
	// Several keys may be refused by a single-select table, leaving the
	// earlier selection standing, so they supersede nothing; nor does a key
	// the table will not hold.
	for _, later := range []*pb.PatchOp{selection("c", "d"), selection("nope")} {
		sendAll(t, c, patchTable(nil, tableRow("e", "1")), selection("a"), patchTable(nil, tableRow("f", "2")), later)
		if ops := opsUntil(t, c, 4); ops[1].GetSetValue() == nil || ops[3].GetSetValue() == nil {
			t.Fatal(ops)
		}
	}
}

// A set_value on a table the panel declares goes with the table's ops from
// the start, before any table op: kept in order, and superseded only by one
// the host would apply. A relaunched panel holds k3 and not k2; the
// set_value(k2) after set_value(k3) is refused, so k3 stays selected.
func TestDeclaredTableSelectionBeforeAnyTableOp(t *testing.T) {
	c := tableClient(t)
	sendAll(t, c, selection("k3"), selection("k2"))
	ops := drainAllOps(t, c)
	host := oracleTable{keys: []string{"k3"}, cells: map[string]string{"k3": "0"}}
	for _, op := range ops {
		host.apply(op)
	}
	if !slices.Equal(host.selection, []string{"k3"}) {
		t.Fatal("the refused selection superseded the one before it:", ops, host)
	}
}

// An op the host would refuse is refused alone, never folded into a valid
// op: past 100,000 rows of a table the session has stated, a bad key, or too
// many cells. The valid ops around it reach the host intact.
func TestTableOpRefusedAlone(t *testing.T) {
	rows := func(prefix string, n int) []*pb.TableRow {
		out := make([]*pb.TableRow, n)
		for i := range out {
			out[i] = tableRow(fmt.Sprint(prefix, i), "")
		}
		return out
	}
	t.Run("past 100,000 rows", func(t *testing.T) {
		c := tableClient(t)
		stated(t, c, rows("a", 30000)...)
		for _, prefix := range []string{"b", "c", "d"} {
			sendAll(t, c, patchTable(nil, rows(prefix, 23332)...))
			drainedOps(t, c)
		}
		// 99,996 rows held; 3 more fit, then 10 more would pass the cap.
		sendAll(t, c, patchTable(nil, rows("e", 3)...), patchTable(nil, rows("f", 10)...))
		ops := opsUntil(t, c, 2)
		if len(ops[0].GetPatchTable().GetUpsert()) != 3 || len(ops[1].GetPatchTable().GetUpsert()) != 10 {
			t.Fatal(len(ops[0].GetPatchTable().GetUpsert()), len(ops[1].GetPatchTable().GetUpsert()))
		}
		// The refused patch left 99,999 rows: two deleted and two added fit,
		// and fold (past 100,000 they would go one by one).
		sendAll(t, c, patchTable([]string{"a0", "a1"}, rows("g", 1)...), patchTable(nil, rows("h", 1)...))
		if ops := opsUntil(t, c, 1); len(ops[0].GetPatchTable().GetUpsert()) != 2 {
			t.Fatal("the refused patch was counted as applied", ops)
		}
	})
	t.Run("a pending set_table", func(t *testing.T) {
		c := tableClient(t)
		sendAll(t, c, setTable(tableRow("a", "1")), patchTable(nil, tableRow("", "x")), patchTable(nil, tableRow("b", "2")))
		ops := opsUntil(t, c, 3)
		if len(ops[0].GetSetTable().GetRows()) != 1 || ops[1].GetPatchTable().GetUpsert()[0].GetKey() != "" || ops[2].GetPatchTable().GetUpsert()[0].GetKey() != "b" {
			t.Fatal(ops)
		}
	})
	t.Run("bad content", func(t *testing.T) {
		wide := &pb.TableRow{Key: "w"}
		for range manifest.TableMaxColumns + 1 {
			wide.Cells = append(wide.Cells, &pb.Value{Kind: &pb.Value_S{S: ""}})
		}
		two := &pb.TableRow{Key: "two", Cells: []*pb.Value{{Kind: &pb.Value_S{S: ""}}, {Kind: &pb.Value_S{S: ""}}}}
		for name, bad := range map[string]*pb.PatchOp{
			"empty key":                  patchTable(nil, tableRow("", "x")),
			"long key":                   patchTable(nil, tableRow(strings.Repeat("k", manifest.TableMaxKeyChars+1), "x")),
			"33 cells":                   patchTable(nil, wide),
			"more cells than columns":    patchTable(nil, two),
			"a key twice in one patch":   patchTable(nil, tableRow("d", "1"), tableRow("d", "2")),
			"a key twice in a set_table": setTable(tableRow("d", "1"), tableRow("d", "2")),
		} {
			c := tableClient(t)
			stated(t, c)
			// Its neighbors merge on either side; the bad op goes alone.
			sendAll(t, c, patchTable(nil, tableRow("a", "1")), patchTable(nil, tableRow("b", "1")), bad, patchTable(nil, tableRow("c", "1")), patchTable(nil, tableRow("e", "1")))
			ops := opsUntil(t, c, 3)
			if len(ops[0].GetPatchTable().GetUpsert()) != 2 || !proto.Equal(ops[1], bad) || len(ops[2].GetPatchTable().GetUpsert()) != 2 {
				t.Fatal(name, ops)
			}
		}
	})
}

// Patches merge only once the session has stated the table with a set_table:
// before it, and for a table whose columns are not declared, they go in order.
func TestTablePatchesMergeOnlyWithKnowledge(t *testing.T) {
	two := []*pb.PatchOp{patchTable(nil, tableRow("a", "1")), patchTable(nil, tableRow("b", "2"))}
	t.Run("before the first set_table", func(t *testing.T) {
		c := tableClient(t)
		sendAll(t, c, two...)
		opsUntil(t, c, 2)
	})
	t.Run("after it", func(t *testing.T) {
		c := tableClient(t)
		stated(t, c)
		sendAll(t, c, two...)
		opsUntil(t, c, 1)
	})
	t.Run("without declared columns", func(t *testing.T) {
		c := stateClient(t)
		sendAll(t, c, setTable(tableRow("s", "0")))
		drainedOps(t, c)
		sendAll(t, c, two...)
		opsUntil(t, c, 2)
		// Nor is a patch applied to a pending set_table.
		sendAll(t, c, setTable(tableRow("s", "0")), two[0])
		opsUntil(t, c, 2)
	})
}

// Until the session states a table whole, its rows are unknown (a relaunched
// plugin's panel keeps the last session's), so patches are not folded: whether
// the pair crosses 100,000 rows depends on rows it cannot see.
func TestUnknownTableNeverFoldsPatches(t *testing.T) {
	c := tableClient(t)
	sendAll(t, c, patchTable(nil, tableRow("a", "1")), patchTable(nil, tableRow("b", "2")))
	if ops := opsUntil(t, c, 2); ops[0].GetPatchTable().GetUpsert()[0].GetKey() != "a" {
		t.Fatal(ops)
	}
}

// A key deleted and upserted in one patch is a fresh row: it loses its
// selection, as two patches would. So that patch merges with nothing when the
// key stood before it, either way round; a key that did not stand before
// (added within the pending ops) blocks nothing.
func TestTableFreshRowInOnePatch(t *testing.T) {
	fresh := func() *pb.PatchOp { return patchTable([]string{"a"}, tableRow("a", "2")) }
	for name, run := range map[string][]*pb.PatchOp{
		"fresh, then another": {fresh(), patchTable(nil, tableRow("b", "1"))},
		"another, then fresh": {patchTable(nil, tableRow("b", "1")), fresh()},
		"fresh into a stated": {setTable(tableRow("a", "1")), fresh()},
	} {
		c := tableClient(t)
		stated(t, c, tableRow("a", "0"))
		sendAll(t, c, run...)
		if ops := drainAllOps(t, c); len(ops) != 2 {
			t.Fatal(name, ops)
		}
	}
	c := tableClient(t)
	stated(t, c, tableRow("a", "0"))
	sendAll(t, c, patchTable(nil, tableRow("n", "1")), patchTable([]string{"n"}, tableRow("n", "2")), patchTable(nil, tableRow("b", "1")))
	if ops := drainAllOps(t, c); len(ops) != 1 {
		t.Fatal("a row added within the pending ops blocked a merge", ops)
	}
}

// The merge criterion, and nothing stricter: ops merge unless a row present
// before them is removed by one and present after them. Each run here meets
// it and merges to one op; each blocked run breaks it.
func TestTableMergeCriterionIsExact(t *testing.T) {
	merges := map[string][]*pb.PatchOp{
		"a removal that stays":               {patchTable([]string{"a"}), patchTable(nil, tableRow("b", "1"))},
		"replaced in place":                  {patchTable(nil, tableRow("a", "1")), patchTable(nil, tableRow("a", "2"))},
		"a new row removed and back":         {patchTable(nil, tableRow("n", "1")), patchTable([]string{"n"}), patchTable(nil, tableRow("n", "2"))},
		"a statement keeping its rows":       {patchTable(nil, tableRow("b", "1")), setTable(tableRow("a", "1"), tableRow("c", "1"))},
		"a statement dropping a row":         {patchTable(nil, tableRow("b", "1")), setTable(tableRow("c", "1"))},
		"a statement, then a removal":        {setTable(tableRow("a", "1")), patchTable([]string{"a"})},
		"removed by a statement, not back":   {setTable(tableRow("c", "1")), setTable(tableRow("d", "1"))},
		"a delete of a row that is not held": {patchTable([]string{"z"}), patchTable(nil, tableRow("z", "1"))},
	}
	blocked := map[string][]*pb.PatchOp{
		"removed by a patch, back by a patch":     {patchTable([]string{"a"}), patchTable(nil, tableRow("a", "1"))},
		"removed by a patch, back by a statement": {patchTable([]string{"a"}), setTable(tableRow("a", "1"))},
		"removed by a statement, back":            {setTable(tableRow("c", "1")), patchTable(nil, tableRow("a", "1"))},
		"removed by a statement, restated":        {setTable(tableRow("c", "1")), setTable(tableRow("a", "1"))},
	}
	for name, run := range merges {
		c := tableClient(t)
		stated(t, c, tableRow("a", "0"))
		sendAll(t, c, run...)
		if ops := drainAllOps(t, c); len(ops) != 1 {
			t.Error("did not merge:", name, ops)
		}
	}
	for name, run := range blocked {
		c := tableClient(t)
		stated(t, c, tableRow("a", "0"))
		sendAll(t, c, run...)
		if ops := drainAllOps(t, c); len(ops) != 2 {
			t.Error("merged:", name, ops)
		}
	}
}

// Patches merge once the columns are declared and a set_table has been sent,
// with no set_value ever sent; a burst of 1,000 small patches is never
// refused, merged or not.
func TestTablePatchBurstIsNeverRefused(t *testing.T) {
	t.Run("merging", func(t *testing.T) {
		c := tableClient(t)
		stated(t, c)
		for i := range 1000 {
			sendAll(t, c, patchTable(nil, tableRow(fmt.Sprint("k", i), "1")))
		}
		ops := drainAllOps(t, c)
		if len(ops) != 1 || len(ops[0].GetPatchTable().GetUpsert()) != 1000 {
			t.Fatal(len(ops))
		}
		if c.QueueStats().Refused != 0 || c.ctx.Err() != nil {
			t.Fatal(c.QueueStats())
		}
	})
	t.Run("apart", func(t *testing.T) {
		// Each patch makes a held row fresh, so none merges.
		c := tableClient(t)
		stated(t, c, tableRow("a", "0"))
		var sent []*pb.PatchOp
		for i := range 1000 {
			op := patchTable([]string{"a"}, tableRow("a", fmt.Sprint(i)))
			sent = append(sent, op)
			sendAll(t, c, op)
		}
		ops := drainAllOps(t, c)
		if len(ops) != 1000 || c.QueueStats().Refused != 0 || c.ctx.Err() != nil {
			t.Fatal(len(ops), c.QueueStats())
		}
		if got, want := hostTable([]string{"a=0"}, ops), hostTable([]string{"a=0"}, sent); !slices.Equal(got, want) {
			t.Fatal(got, want)
		}
	})
}

// A table's slot that holds 256 ops goes out as its own message when the next
// op arrives, and a new slot starts: never a refusal. A set_value counts too.
func TestFullTableSlotFlushes(t *testing.T) {
	c := tableClient(t)
	stated(t, c, tableRow("a", "0"))
	var sent []*pb.PatchOp
	for i := range 300 {
		op := patchTable([]string{"a"}, tableRow("a", fmt.Sprint(i)))
		if i%2 == 1 {
			// Two keys, which a single-select table refuses: it supersedes
			// nothing, and is held in order.
			op = selection("a", "b")
		}
		sent = append(sent, op)
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(i, err)
		}
	}
	var messages []*pb.PluginMsg
	for {
		c.queueMu.Lock()
		idle := len(c.out) == 0 && len(c.order) == 0
		c.queueMu.Unlock()
		if idle {
			break
		}
		messages = append(messages, nextBatch(t, c)...)
	}
	var ops []*pb.PatchOp
	for _, m := range messages {
		if n := len(m.GetPanelPatch().GetOps()); n > tableHeldCeiling {
			t.Fatal("a drained table message carries", n, "ops")
		}
		ops = append(ops, m.GetPanelPatch().GetOps()...)
	}
	if len(messages) < 2 || len(messages[0].GetPanelPatch().GetOps()) != tableHeldCeiling {
		t.Fatal(len(messages))
	}
	if len(ops) != len(sent) || c.QueueStats().Refused != 0 || c.ctx.Err() != nil {
		t.Fatal(len(ops), c.QueueStats())
	}
	for i := range ops {
		if !proto.Equal(ops[i], sent[i]) {
			t.Fatal("out of order at", i)
		}
	}
}

func TestWidgetGeometryNewestPerWidgetOverWire(t *testing.T) {
	heard := make(chan *pb.WidgetGeometry, 8)
	release := make(chan struct{})
	cfg := Config{
		WidgetGeometry: func(ctx context.Context, c *Client, g *pb.WidgetGeometry) error {
			heard <- g
			if g.WidthPx == 1 {
				select {
				case <-release:
				case <-ctx.Done():
				}
			}
			return nil
		},
		HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
		},
	}
	p := openPeer(t, cfg, nil)
	send := func(widget string, w uint32) {
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_WidgetGeometry{WidgetGeometry: &pb.WidgetGeometry{PanelId: "p", WidgetId: widget, WidthPx: w, HeightPx: 10, Dpr: 2}}})
	}
	send("plot", 1)
	if g := <-heard; g.WidthPx != 1 {
		t.Fatal(g)
	}
	// The handler is parked: these coalesce to the newest per widget.
	send("plot", 2)
	send("other", 7)
	send("plot", 3)
	p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "barrier"}}})
	if q := p.recv(t); q.GetCommandDone() == nil {
		t.Fatal(q)
	}
	if g, ok := p.client.WidgetGeometry("p", "plot"); !ok || g.WidthPx != 3 || g.Dpr != 2 {
		t.Fatal(g, ok)
	}
	if _, ok := p.client.WidgetGeometry("p", "absent"); ok {
		t.Fatal("a geometry never sent was reported")
	}
	close(release)
	var got []string
	for range 2 {
		g := <-heard
		got = append(got, fmt.Sprint(g.WidgetId, g.WidthPx))
	}
	if !slices.Equal(got, []string{"plot3", "other7"}) {
		t.Fatal(got)
	}
	select {
	case g := <-heard:
		t.Fatal("a superseded geometry was delivered", g)
	default:
	}
}

func TestWidgetGeometryWithoutHandlerIsRecordedQuietly(t *testing.T) {
	diagnostics := make(chan error, 4)
	p := openPeer(t, Config{Diagnostic: func(err error) { diagnostics <- err }, HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
	}}, nil)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_WidgetGeometry{WidgetGeometry: &pb.WidgetGeometry{PanelId: "p", WidgetId: "plot", WidthPx: 4, HeightPx: 5, Dpr: 1}}})
	p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "barrier"}}})
	if q := p.recv(t); q.GetCommandDone() == nil {
		t.Fatal(q)
	}
	if g, ok := p.client.WidgetGeometry("p", "plot"); !ok || g.HeightPx != 5 {
		t.Fatal(g, ok)
	}
	select {
	case err := <-diagnostics:
		t.Fatal("an unhandled geometry is not a fault:", err)
	default:
	}
}

// An empty selection clears on the host whichever list kind carries it (the
// reference's select), so it counts as applied; a non-empty Value.ints never
// does.
func TestTableEmptySelectionApplies(t *testing.T) {
	none := func(string) bool { return false }
	for _, v := range []*pb.Value{
		{Kind: &pb.Value_Strings{Strings: &pb.StringList{}}},
		{Kind: &pb.Value_Ints{Ints: &pb.IntList{}}},
		{Kind: &pb.Value_S{S: ""}},
	} {
		if !setValueApplies(v, none) {
			t.Fatal("an empty selection does not apply:", v)
		}
	}
	if setValueApplies(&pb.Value{Kind: &pb.Value_Ints{Ints: &pb.IntList{Values: []int64{1}}}}, func(string) bool { return true }) {
		t.Fatal("a non-empty Value.ints applies to a table")
	}
}

// An empty selection goes out as an empty Value.strings, after the rows.
func TestTableEmptySelectionGoesOutAsStrings(t *testing.T) {
	c := tableClient(t)
	for _, op := range []*pb.PatchOp{setTable(tableRow("n", "1")), selection("n"), selection()} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	ops := drainedOps(t, c)
	last := ops[len(ops)-1].GetSetValue()
	if v, ok := last.GetKind().(*pb.Value_Strings); !ok || len(v.Strings.GetValues()) != 0 {
		t.Fatal("an empty selection must be an empty Value.strings:", ops)
	}
}
