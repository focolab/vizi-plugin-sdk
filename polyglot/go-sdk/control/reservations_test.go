package control

import (
	"errors"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"testing"
)

func TestDuplicateReservationRefusedWholeAndCounted(t *testing.T) {
	c := stateClient(t)
	op := func(widget string, offset uint64) *pb.PatchOp {
		return &pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 9, Offset: offset, Len: 12}}}
	}
	if err := c.Send(patch("p", op("a", 32), op("b", 32))); !errors.Is(err, ErrDuplicateReservation) {
		t.Fatal(err)
	}
	if len(c.out) != 0 || c.QueueStats().Refused != 1 || c.ctx.Err() != nil {
		t.Fatal("refusal was partial, silent, or fatal")
	}
	if err := c.Send(patch("p", op("a", 32), op("b", 64))); err != nil {
		t.Fatal(err)
	}
	if len(nextBatch(t, c)[0].GetPanelPatch().Ops) != 2 {
		t.Fatal("distinct loans lost")
	}
}
