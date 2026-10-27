package control

import (
	"fmt"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestStageTurnErrorDiagnosedOnceAcrossPolls(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		c := stateClient(t)
		c.diagnostics = make(chan error, 8)
		r := &stageRuntime{c: c, ctx: c.ctx, wake: make(chan struct{}, 1), done: make(chan struct{}), arms: map[stageKey]*stageBinding{
			{pipeline: 1, position: 0}: {arm: &pb.StageArm{StageId: "broken"}, segment: &memory.Segment{}, last: []uint32{0}},
		}}
		go r.run()
		time.Sleep(200 * time.Millisecond)
		synctest.Wait()
		if len(c.diagnostics) != 1 {
			t.Fatalf("diagnostics across five polls: %d", len(c.diagnostics))
		}
		if err := <-c.diagnostics; !strings.Contains(err.Error(), "stage broken region 0") {
			t.Fatal(err)
		}
		c.cancel(nil)
		<-r.done
	})
}

func declaredListClient(t *testing.T) *Client {
	c := stateClient(t)
	c.cfg.Panels = map[string]PanelLayout{"p": {
		Lists: map[string]int{"rows": 1024, "other": 8},
		Cells: map[string]string{"name": "rows", "image": "rows", "other-name": "other"},
	}}
	return c
}

func TestRowsCoalesceSubsetsAndKeepRecreation(t *testing.T) {
	c := declaredListClient(t)
	for _, op := range []*pb.PatchOp{rowKeys("a", "b"), rowValue("a", "stale"), rowKeys("b"), rowKeys("a", "b")} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != 1 {
		t.Fatalf("removed cell survived: %v", batch)
	}
	ops := batch[0].GetPanelPatch().Ops
	if len(ops) != 2 || fmt.Sprint(ops[0].GetSetRows().Keys) != "[b]" || fmt.Sprint(ops[1].GetSetRows().Keys) != "[a b]" {
		t.Fatalf("removal/recreation lost: %v", ops)
	}
}

func TestListSelectionFollowsPendingRows(t *testing.T) {
	c := declaredListClient(t)
	selection := func(s string) *pb.PatchOp {
		return &pb.PatchOp{WidgetId: "rows", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_S{S: s}}}}
	}
	for _, op := range []*pb.PatchOp{selection("old"), rowKeys("a"), selection("a")} {
		if err := c.Send(patch("p", op)); err != nil {
			t.Fatal(err)
		}
	}
	ops := nextBatch(t, c)[0].GetPanelPatch().Ops
	if len(ops) != 2 || ops[0].GetSetRows() == nil || ops[1].GetSetValue().GetS() != "a" {
		t.Fatal(ops)
	}
}

func TestImageFreezesOwnCellAndRowsOnly(t *testing.T) {
	c := declaredListClient(t)
	c.out = make(chan outbound, 1)
	if err := c.Send(patch("p", rowKeys("a"))); err != nil {
		t.Fatal(err)
	}
	value := rowValue("a", "before")
	value.WidgetId = "image"
	if err := c.Send(patch("p", value)); err != nil {
		t.Fatal(err)
	}
	// More unrelated cells than the ordinary reliable queue's default capacity.
	for i := range 300 {
		if err := c.Send(patch("p", rowValue(fmt.Sprint(i), "old"))); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Send(patch("elsewhere", enabled("x", true))); err != nil {
		t.Fatal(err)
	}
	img := &pb.PatchOp{WidgetId: "image", RowKey: "a", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 7, Offset: 64, Len: 1}}}
	if err := c.Send(patch("p", img)); err != nil {
		t.Fatal(err)
	}
	if len(c.pending) != 301 {
		t.Fatalf("unrelated keys were frozen: %d", len(c.pending))
	}
	for i := range 300 {
		if err := c.Send(patch("p", rowValue(fmt.Sprint(i), "new"))); err != nil {
			t.Fatal(err)
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != 3 || batch[0].GetPanelPatch().Ops[0].GetSetRows() == nil || batch[1].GetPanelPatch().Ops[0].GetSetValue().GetS() != "before" || batch[2].GetPanelPatch().Ops[0].GetSetImage() == nil {
		t.Fatalf("image dependencies out of order: %v", batch)
	}
	batch = nextBatch(t, c)
	if len(batch) != 301 || c.coalescingCount != 0 {
		t.Fatal(len(batch), c.coalescingCount)
	}
	for _, msg := range batch[:300] {
		if msg.GetPanelPatch().Ops[0].GetSetValue().GetS() != "new" {
			t.Fatal(msg)
		}
	}
}

func TestRawPatchHonorsDeclaredMaxRowsAtomically(t *testing.T) {
	c := declaredListClient(t)
	c.cfg.Panels["p"].Lists["rows"] = 1
	if err := c.Send(patch("p", enabled("label", false), rowKeys("a", "b"))); err == nil {
		t.Fatal("over-limit rows admitted")
	}
	if len(c.order) != 0 || len(c.out) != 0 {
		t.Fatal("partial admission")
	}
	if err := c.Send(patch("p", rowKeys("a"))); err != nil {
		t.Fatal(err)
	}
}
