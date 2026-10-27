package plugin

import (
	"context"
	"fmt"
	"slices"
	"strings"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestTableDeclaration(t *testing.T) {
	d := definition()
	pick := d.Command(CommandSpec{ID: "pick"}, noopCommand)
	columns := []TableColumn{StringColumn("name", "Region"), WithUnit(WithPrecision(NumberColumn("area", "Area"), 2), "µm²"), BoolColumn("kept", "")}
	table := Table(TableSpec{ID: "stats", Selectable: true, Multiple: true, Sortable: true, VisibleRows: 12, Command: pick}, columns...).MovesView()
	columns[0].ID = "changed" // the declaration owns its copy
	d.Panel(PanelSpec{ID: "p", Title: "Stats", ViewTargets: []string{"select_region"}, Root: Column("root", table, Table(TableSpec{ID: "plain"}, NumberColumn("n", "")))}, nil)
	body, err := buildDefinition(t, d).Manifest("plugin")
	if err != nil {
		t.Fatal(err)
	}
	m := decodeManifest(t, body)
	got := m.Contributions.Panels[0].Root.Children[0]
	if got.Kind != "table" || got.Command != "pick" || !*got.Selectable || !*got.Multiple || !*got.Sortable || *got.VisibleRows != 12 || !got.MovesView {
		t.Fatal(string(body))
	}
	if len(got.Columns) != 3 || got.Columns[0].ID != "name" || *got.Columns[1].Precision != 2 || *got.Columns[1].Unit != "µm²" || got.Columns[2].ColumnHeader() != "kept" {
		t.Fatal(got.Columns)
	}
	plain := m.Contributions.Panels[0].Root.Children[1]
	if plain.Selectable != nil || plain.Sortable != nil || plain.Command != "" || plain.Columns[0].ColumnPrecision() != manifest.TableDefaultPrecision {
		t.Fatal(string(body))
	}

	for name, build := range map[string]func(*Definition, CommandRef) Widget{
		"selectable without a command": func(*Definition, CommandRef) Widget {
			return Table(TableSpec{ID: "t", Selectable: true}, StringColumn("a", ""))
		},
		"a command without selectable": func(_ *Definition, c CommandRef) Widget {
			return Table(TableSpec{ID: "t", Command: c}, StringColumn("a", ""))
		},
		"multiple without selectable": func(*Definition, CommandRef) Widget {
			return Table(TableSpec{ID: "t", Multiple: true}, StringColumn("a", ""))
		},
		"a unit on a string column": func(*Definition, CommandRef) Widget {
			return Table(TableSpec{ID: "t"}, WithUnit(StringColumn("a", ""), "m"))
		},
		"no columns": func(*Definition, CommandRef) Widget { return Table(TableSpec{ID: "t"}) },
		"moves_view unselectable": func(*Definition, CommandRef) Widget {
			return Table(TableSpec{ID: "t"}, StringColumn("a", "")).MovesView()
		},
	} {
		d := definition()
		c := d.Command(CommandSpec{ID: "c"}, noopCommand)
		d.Panel(PanelSpec{ID: "p", Title: "P", ViewTargets: []string{"t"}, Root: build(d, c)}, nil)
		if _, err := d.Build(); err == nil {
			t.Error("accepted", name)
		}
	}
}

// applyTable is the host's reading of table ops (§8.4), keyed rows in order.
func applyTable(rows []*pb.TableRow, op *pb.PatchOp) []*pb.TableRow {
	if s := op.GetSetTable(); s != nil {
		return slices.Clone(s.Rows)
	}
	p := op.GetPatchTable()
	if p == nil {
		return rows
	}
	gone := make(map[string]bool, len(p.Delete))
	for _, key := range p.Delete {
		gone[key] = true
	}
	rows = slices.DeleteFunc(rows, func(r *pb.TableRow) bool { return gone[r.Key] })
	at := make(map[string]int, len(rows))
	for i, r := range rows {
		at[r.Key] = i
	}
	for _, u := range p.Upsert {
		if i, ok := at[u.Key]; ok {
			rows[i] = u
		} else {
			at[u.Key] = len(rows)
			rows = append(rows, u)
		}
	}
	return rows
}

func TestSetTableAndPatchTableSplitUnderOneMessage(t *testing.T) {
	d := definition()
	ref := d.Panel(PanelSpec{ID: "p", Title: "Big", Root: Table(TableSpec{ID: "big"}, StringColumn("name", ""), NumberColumn("n", ""), BoolColumn("ok", ""))}, nil)
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	client, stream := listTestSession(t, cfg.Control)
	panel := ref.Bind(client)
	// The rows the host holds after every message up to an ordered marker; the
	// marker drains whatever the outbox still holds ahead of it.
	var host []*pb.TableRow
	sync := func(marker string) {
		t.Helper()
		if err := client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: marker}}}); err != nil {
			t.Fatal(err)
		}
		for {
			m, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			if m.GetOverlayRemove().GetOverlayId() == marker {
				return
			}
			if size := proto.Size(m.GetPanelPatch()); size > manifest.TableMaxPatchBytes {
				t.Fatalf("a PanelPatch carrying a table op is %d bytes", size)
			}
			for _, op := range m.GetPanelPatch().GetOps() {
				host = applyTable(host, op)
			}
		}
	}
	text := strings.Repeat("r", 900)
	var rows []TableRow
	for i := range 3000 {
		if i%2 == 0 {
			rows = append(rows, Cells(fmt.Sprint("k", i), text, i, i%3 == 0))
		} else {
			rows = append(rows, TableRow{Key: fmt.Sprint("k", i), ByColumn: map[string]any{"n": float32(i), "name": text}})
		}
	}
	if err := panel.SetTable("big", rows...); err != nil {
		t.Fatal(err)
	}
	sync("after set")
	if len(host) != 3000 || host[2999].Key != "k2999" || host[3].Cells[1].GetF() != 3 || host[3].Cells[2].GetKind() != nil || host[0].Cells[2].GetB() != true {
		t.Fatal(len(host))
	}
	// Deletes go first, upserts replace in place or append.
	var remove []string
	for i := 0; i < 3000; i += 3 {
		remove = append(remove, fmt.Sprint("k", i))
	}
	upsert := []TableRow{Cells("k1", "one"), Cells("k0", "fresh"), Cells("new", text)}
	for i := range 1500 {
		upsert = append(upsert, Cells(fmt.Sprint("n", i), text))
	}
	if err := panel.PatchTable("big", upsert, remove); err != nil {
		t.Fatal(err)
	}
	sync("after patch")
	if len(host) != 2000+2+1500 || host[0].Key != "k1" || host[0].Cells[0].GetS() != "one" || host[2000].Key != "k0" || host[2001].Key != "new" {
		t.Fatal(len(host), host[0].Key)
	}
	if err := panel.SetTable("big"); err != nil {
		t.Fatal(err)
	}
	sync("after clear")
	if len(host) != 0 {
		t.Fatal(len(host))
	}
	if err := panel.PatchTable("big", nil, nil); err != nil {
		t.Fatal(err)
	}
	sync("nothing for an empty patch")
}

// 100,000 short-key rows fit in 1 MiB but not in one message: each message
// carries at most TableMaxRowsPerMessage rows and deletes together.
func TestTableSplitsByRowCountAsWellAsBytes(t *testing.T) {
	d := definition()
	ref := d.Panel(PanelSpec{ID: "p", Title: "Many", Root: Table(TableSpec{ID: "many"}, StringColumn("a", ""))}, nil)
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	client, stream := listTestSession(t, cfg.Control)
	panel := ref.Bind(client)
	var host []*pb.TableRow
	messages := 0
	var deleteRuns []int
	sync := func(marker string) {
		t.Helper()
		if err := client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: marker}}}); err != nil {
			t.Fatal(err)
		}
		for {
			m, err := stream.Recv()
			if err != nil {
				t.Fatal(err)
			}
			if m.GetOverlayRemove().GetOverlayId() == marker {
				return
			}
			messages++
			rows, deletes := 0, 0
			for _, op := range m.GetPanelPatch().GetOps() {
				rows += len(op.GetSetTable().GetRows()) + len(op.GetPatchTable().GetUpsert())
				deletes += len(op.GetPatchTable().GetDelete())
				host = applyTable(host, op)
			}
			if rows > manifest.TableMaxRowsPerMessage || deletes > manifest.TableMaxRowsPerMessage {
				t.Fatalf("a table message carries %d rows and %d deleted keys", rows, deletes)
			}
			deleteRuns = append(deleteRuns, deletes)
		}
	}
	rows := make([]TableRow, manifest.TableMaxRows)
	for i := range rows {
		rows[i] = Cells(fmt.Sprint(i))
	}
	if err := panel.SetTable("many", rows...); err != nil {
		t.Fatal(err)
	}
	sync("set")
	if len(host) != manifest.TableMaxRows || host[99999].Key != "99999" || messages < 4 {
		t.Fatal(len(host), messages)
	}
	// 40,000 deletes and a few upserts: the delete list is split by the same
	// cap on its own, deletes first.
	remove := make([]string, 40000)
	for i := range remove {
		remove[i] = fmt.Sprint(i)
	}
	deleteRuns = nil
	if err := panel.PatchTable("many", []TableRow{Cells("5"), Cells("u0"), Cells("u1")}, remove); err != nil {
		t.Fatal(err)
	}
	sync("patch")
	if len(host) != 60003 || host[0].Key != "40000" || host[59999].Key != "99999" || host[60000].Key != "5" || host[60002].Key != "u1" {
		t.Fatal(len(host))
	}
	if total := deleteRuns[0] + deleteRuns[len(deleteRuns)-1]; len(deleteRuns) > 2 || total != 40000 {
		// The outbox may merge the two runs only while each stays under the cap.
		t.Fatal(deleteRuns)
	}
	// 60,000 upserts alone split by rows.
	upsert := make([]TableRow, 60000)
	for i := range upsert {
		upsert[i] = Cells(fmt.Sprint("n", i))
	}
	if err := panel.PatchTable("many", upsert, nil); err != nil {
		t.Fatal(err)
	}
	sync("upserts")
	if len(host) != 120003 || host[120002].Key != "n59999" {
		t.Fatal(len(host))
	}
	tooMany := &pb.TablePatch{Delete: make([]string, manifest.TableMaxRowsPerMessage+1)}
	if err := panel.Patch(&pb.PatchOp{WidgetId: "many", Op: &pb.PatchOp_PatchTable{PatchTable: tooMany}}); err == nil {
		t.Fatal("accepted a raw table op past the row cap")
	}
}

func TestTableRowChecks(t *testing.T) {
	d := definition()
	ref := d.Panel(PanelSpec{ID: "p", Title: "T", Root: Table(TableSpec{ID: "t"}, StringColumn("a", ""), NumberColumn("b", ""))}, nil)
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	client, _ := listTestSession(t, cfg.Control)
	panel := ref.Bind(client)
	for want, call := range map[string]func() error{
		"must be distinct":           func() error { return panel.SetTable("t", Cells("a"), Cells("a")) },
		"non-empty and at most 128":  func() error { return panel.SetTable("t", Cells(strings.Repeat("é", 129))) },
		"non-empty and at most 128 ": func() error { return panel.PatchTable("t", []TableRow{Cells("")}, nil) },
		"has no column \"z\"":        func() error { return panel.SetTable("t", TableRow{Key: "k", ByColumn: map[string]any{"z": 1}}) },
		"both Cells and ByColumn": func() error {
			return panel.SetTable("t", TableRow{Key: "k", Cells: []any{1}, ByColumn: map[string]any{"a": 1}})
		},
		"has 2 columns": func() error { return panel.SetTable("t", Cells("k", 1, 2, 3)) },
		"not struct {}": func() error { return panel.SetTable("t", Cells("k", struct{}{})) },
		"declared columns": func() error {
			return Panel{ID: "p", Client: client}.SetTable("t", TableRow{Key: "k", ByColumn: map[string]any{"a": 1}})
		},
		"at most 32 cells":             func() error { return Panel{ID: "p", Client: client}.SetTable("t", Cells("k", make([]any, 33)...)) },
		"at most 100000 rows":          func() error { return panel.SetTable("t", make([]TableRow, manifest.TableMaxRows+1)...) },
		"addresses a table, not a row": func() error { return panel.Row("r").SetTable("t", Cells("k")) },
		"past one message":             func() error { return panel.SetTable("t", Cells("k", strings.Repeat("x", 2<<20))) },
		"is at most 1048576 bytes": func() error {
			return panel.Patch(&pb.PatchOp{WidgetId: "t", Op: &pb.PatchOp_SetTable{SetTable: &pb.TableRows{Rows: []*pb.TableRow{{Key: "k", Cells: []*pb.Value{String(strings.Repeat("x", 2<<20))}}}}}})
		},
	} {
		if err := call(); err == nil || !strings.Contains(err.Error(), strings.TrimSpace(want)) {
			t.Errorf("%s: %v", want, err)
		}
	}
}

func TestStringListValues(t *testing.T) {
	value := Strings("a", "b")
	wire, err := proto.Marshal(value)
	if err != nil {
		t.Fatal(err)
	}
	decoded := new(pb.Value)
	if err := proto.Unmarshal(wire, decoded); err != nil {
		t.Fatal(err)
	}
	got, err := (Args{"rows": decoded}).Strings("rows")
	if err != nil || !slices.Equal(got, []string{"a", "b"}) {
		t.Fatal(got, err)
	}
	got[0] = "changed"
	if decoded.GetStrings().Values[0] != "a" {
		t.Fatal("list aliases wire storage")
	}
	if got, err := (Args{"rows": Strings()}).Strings("rows"); err != nil || len(got) != 0 {
		t.Fatal(got, err)
	}
	for _, a := range []Args{nil, {"rows": String("a")}, {"rows": Ints(1)}} {
		if _, err := a.Strings("rows"); err == nil {
			t.Fatal("accepted a non-string-list", a)
		}
	}
}

// A person clearing a table's selection fires {"rows": []} as a Value.strings
// with no entries (wire 32 00). It reaches the table's command as a present,
// empty selection. An empty Value.ints (2a 00) is not a table selection.
func TestClearedTableSelection(t *testing.T) {
	d := definition()
	type picked struct {
		rows []string
		err  error
	}
	got := make(chan picked, 1)
	pick := d.Command(CommandSpec{ID: "pick"}, func(_ context.Context, _ *control.Client, args Args) (Result, error) {
		rows, err := args.Strings("rows")
		got <- picked{rows, err}
		return Result{}, err
	})
	d.Panel(PanelSpec{ID: "p", Title: "T", Root: Table(TableSpec{ID: "t", Selectable: true, Multiple: true, Command: pick}, StringColumn("name", "Name"))}, nil)
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	if got := cfg.Control.Panels["p"].Tables["t"]; got != 1 {
		t.Fatal("the outbox is not told the table's declared columns:", got)
	}
	for wire, accepted := range map[string]bool{"\x32\x00": true, "\x2a\x00": false} {
		value := new(pb.Value)
		if err := proto.Unmarshal([]byte(wire), value); err != nil {
			t.Fatal(err)
		}
		out, err := cfg.Control.HandleRequest(context.Background(), nil, &pb.HostMsg{Id: "r1", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: pick.ID(), Args: map[string]*pb.Value{"rows": value}}}})
		r := <-got
		if accepted && (err != nil || r.err != nil || len(r.rows) != 0 || !out.GetCommandDone().GetOk()) {
			t.Fatalf("% x: a cleared selection was not delivered empty: %v %v %v %v", wire, r.rows, r.err, out, err)
		}
		if !accepted && (r.err == nil || (err == nil && out.GetCommandDone().GetOk())) {
			t.Fatalf("% x: an empty integer list was taken as a selection: %v", wire, out)
		}
	}
}

func TestWidgetGeometryHandler(t *testing.T) {
	if w, h := (WidgetSize{WidthPx: 101, HeightPx: 51, DPR: 1.5}).Pixels(); w != 152 || h != 76 {
		t.Fatal("Pixels must round halves to even, as Python's round does:", w, h)
	}
	d := definition()
	ref := d.Panel(PanelSpec{ID: "p", Title: "Plot", Root: Image("plot")}, nil)
	heard := make(chan WidgetSize, 2)
	d.OnWidgetGeometry(ref, func(_ context.Context, _ *control.Client, size WidgetSize) error {
		heard <- size
		return nil
	})
	cfg, err := buildDefinition(t, d).Configure(control.Plugin{})
	if err != nil {
		t.Fatal(err)
	}
	hook := cfg.Control.WidgetGeometry
	if err := hook(context.Background(), nil, &pb.WidgetGeometry{PanelId: "other", WidgetId: "plot", WidthPx: 1}); err != nil {
		t.Fatal(err)
	}
	if err := hook(context.Background(), nil, &pb.WidgetGeometry{PanelId: "p", WidgetId: "plot", WidthPx: 640, HeightPx: 480}); err != nil {
		t.Fatal(err)
	}
	if got := <-heard; got != (WidgetSize{PanelID: "p", WidgetID: "plot", WidthPx: 640, HeightPx: 480, DPR: 1}) {
		t.Fatal("a missing dpr reads as 1:", got)
	}
	select {
	case got := <-heard:
		t.Fatal("another panel's geometry reached this handler", got)
	default:
	}

	twice := definition()
	r := twice.Panel(PanelSpec{ID: "p", Title: "P", Root: Image("plot")}, nil)
	twice.OnWidgetGeometry(r, func(context.Context, *control.Client, WidgetSize) error { return nil })
	twice.OnWidgetGeometry(r, func(context.Context, *control.Client, WidgetSize) error { return nil })
	if _, err := twice.Build(); err == nil {
		t.Fatal("accepted two handlers for one panel")
	}
	foreign := definition()
	foreign.OnWidgetGeometry(ref, func(context.Context, *control.Client, WidgetSize) error { return nil })
	if _, err := foreign.Build(); err == nil {
		t.Fatal("accepted another definition's panel")
	}
	if _, err := (Handlers{WidgetGeometry: map[string]WidgetGeometryHandler{"p": nil}}).Configure(control.Config{}); err == nil {
		t.Fatal("accepted a nil handler")
	}
}
