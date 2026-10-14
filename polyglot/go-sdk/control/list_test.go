package control

import (
	"errors"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func rowValue(row, value string) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: "name", RowKey: row, Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_S{S: value}}}}
}
func rowKeys(keys ...string) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: "rows", Op: &pb.PatchOp_SetRows{SetRows: &pb.RowKeys{Keys: keys}}}
}

func TestPanelCoalescingSeparatesRowsAndPreservesProperties(t *testing.T) {
	c := stateClient(t) // No writer runs until nextBatch: coalescing is deterministic.
	c.cfg.CoalescingCapacity = 2
	for _, op := range []*pb.PatchOp{rowValue("a", "old"), rowValue("b", "B"), rowValue("a", "A"), {WidgetId: "name", RowKey: "a", Op: &pb.PatchOp_SetEnabled{SetEnabled: false}}} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Send(patch("p", rowValue("c", "C"))); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	batch := nextBatch(t, c)
	if len(batch) != 2 {
		t.Fatal(batch)
	}
	a, b := batch[0].GetPanelPatch().Ops, batch[1].GetPanelPatch().Ops
	if len(a) != 2 || a[0].RowKey != "a" || a[0].GetSetValue().GetS() != "A" || a[1].GetSetEnabled() || len(b) != 1 || b[0].RowKey != "b" || b[0].GetSetValue().GetS() != "B" {
		t.Fatal(a, b)
	}
}

func TestSetRowsDoesNotMoveCellWritesAcrossRemovalAndRecreation(t *testing.T) {
	c := stateClient(t)
	for _, op := range []*pb.PatchOp{rowKeys("a", "b"), rowValue("a", "old"), rowKeys("b"), rowKeys("a", "b"), rowValue("b", "new")} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	// Apply the emitted stream to a minimal list model. Re-created rows start
	// with the template, never a stale value moved across their lifetime boundary.
	rows := map[string]string{}
	for len(c.out) > 0 || len(c.order) > 0 {
		for _, m := range nextBatch(t, c) {
			for _, op := range m.GetPanelPatch().Ops {
				if r := op.GetSetRows(); r != nil {
					next := map[string]string{}
					for _, key := range r.Keys {
						value, ok := rows[key]
						if !ok {
							value = "template"
						}
						next[key] = value
					}
					rows = next
				} else {
					if _, ok := rows[op.RowKey]; !ok {
						t.Fatalf("cell arrived before row: %v", op)
					}
					rows[op.RowKey] = op.GetSetValue().GetS()
				}
			}
		}
	}
	if len(rows) != 2 || rows["a"] != "template" || rows["b"] != "new" {
		t.Fatal(rows)
	}
}

func TestDropdownOperationLimitIsPerRow(t *testing.T) {
	c := stateClient(t)
	var ops []*pb.PatchOp
	for _, row := range []string{"a", "b"} {
		ops = append(ops, &pb.PatchOp{WidgetId: "choice", RowKey: row, Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: []string{"one"}}}})
		for range 255 {
			ops = append(ops, &pb.PatchOp{WidgetId: "choice", RowKey: row, Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_S{S: "one"}}}})
		}
	}
	if err := c.Send(patch("p", ops...)); err != nil {
		t.Fatal(err)
	}
	if got := nextBatch(t, c)[0].GetPanelPatch().Ops; len(got) != 512 {
		t.Fatal(len(got))
	}
	ops = append(ops, ops[0])
	if err := c.Send(patch("p", ops...)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
}

// The coalescer freezes pending cells before a set_rows replacement. Prove
// that both lists' shared row key
// and descriptor-bearing cells retain their valid delivery order.
func TestRowRemovalKeepsEarlierCellsBeforeItAndOtherListsIntact(t *testing.T) {
	c := stateClient(t)
	rows := func(id string, keys ...string) *pb.PatchOp {
		return &pb.PatchOp{WidgetId: id, Op: &pb.PatchOp_SetRows{SetRows: &pb.RowKeys{Keys: keys}}}
	}
	value := func(id string) *pb.PatchOp {
		op := rowValue("shared", "value")
		op.WidgetId = id
		return op
	}
	for _, op := range []*pb.PatchOp{
		rows("first", "shared"), rows("second", "shared"), value("first-name"),
		{WidgetId: "first-image", RowKey: "shared", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 7, Offset: 64, Len: 8}}},
		value("second-name"), rows("first"),
	} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	active := map[string]map[string]bool{}
	owner := map[string]string{"first-name": "first", "first-image": "first", "second-name": "second"}
	seen := map[string]int{}
	for len(c.out) > 0 || len(c.order) > 0 {
		for _, m := range nextBatch(t, c) {
			for _, op := range m.GetPanelPatch().Ops {
				if r := op.GetSetRows(); r != nil {
					active[op.WidgetId] = map[string]bool{}
					for _, key := range r.Keys {
						active[op.WidgetId][key] = true
					}
				} else {
					if !active[owner[op.WidgetId]][op.RowKey] {
						t.Fatalf("cell arrived after its row was removed: %v", op)
					}
					seen[op.WidgetId]++
				}
			}
		}
	}
	if len(active["first"]) != 0 || !active["second"]["shared"] || seen["first-name"] != 1 || seen["first-image"] != 1 || seen["second-name"] != 1 {
		t.Fatal(active, seen)
	}
}
