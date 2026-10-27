package control

import (
	"context"
	"errors"
	"io"
	"math"
	"sync"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

func overlay(id string, revision uint64) *pb.PluginMsg {
	return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: id, Revision: revision}}}
}
func patch(panel string, ops ...*pb.PatchOp) *pb.PluginMsg {
	return &pb.PluginMsg{Msg: &pb.PluginMsg_PanelPatch{PanelPatch: &pb.PanelPatch{PanelId: panel, Ops: ops}}}
}
func enabled(widget string, value bool) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: widget, Op: &pb.PatchOp_SetEnabled{SetEnabled: value}}
}
func nextBatch(t *testing.T, c *Client) []*pb.PluginMsg {
	t.Helper()
	x, ok := c.nextOutbound()
	if !ok {
		t.Fatal(context.Cause(c.ctx))
	}
	if x.msg != nil {
		x.before = append(x.before, x.msg)
	}
	for _, msg := range x.before {
		if c.cfg.SentOutbound != nil {
			c.cfg.SentOutbound(msg)
		}
	}
	return x.before
}

func TestCoalescingAtReliableCapacityKeepsOtherKeysAndSession(t *testing.T) {
	c := stateClient(t)
	c.out = make(chan outbound, 1)
	c.cfg.CoalescingCapacity = 2
	if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "removed"}}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(overlay("quiet", 1)); err != nil {
		t.Fatal(err)
	}
	for i := uint64(1); i <= 1000; i++ {
		if err := c.Send(overlay("busy", i)); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Send(overlay("third", 1)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	if c.ctx.Err() != nil {
		t.Fatal("coalescing pressure killed session")
	}
	if nextBatch(t, c)[0].GetOverlayRemove() == nil {
		t.Fatal("one-shot lost")
	}
	batch := nextBatch(t, c)
	if len(batch) != 2 || batch[0].GetOverlayUpdate().OverlayId != "quiet" || batch[1].GetOverlayUpdate().Revision != 1000 {
		t.Fatal(batch)
	}
	if got := c.QueueStats(); got.Merged != 999 || got.Refused != 1 {
		t.Fatal(got)
	}
}

func TestPanelOptionsKeepOrderAcrossDrainsAndBypasses(t *testing.T) {
	for _, bypass := range []bool{false, true} {
		c := stateClient(t)
		options := func(values ...string) *pb.PatchOp {
			return &pb.PatchOp{WidgetId: "dropdown", Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: values}}}
		}
		first := patch("p", options("a", "b"))
		if bypass {
			first.ProtoReflect().SetUnknown([]byte{0xa0, 0x06, 1})
		}
		if err := c.Send(first); err != nil {
			t.Fatal(err)
		}
		_ = nextBatch(t, c) // an ordered options patch is a batch boundary, including unknown fields
		value := &pb.PatchOp{WidgetId: "dropdown", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_I{I: 1}}}}
		if err := c.Send(patch("p", value, options("b", "a"))); err != nil {
			t.Fatal(err)
		}
		ops := nextBatch(t, c)[0].GetPanelPatch().Ops
		if len(ops) != 2 || ops[0].GetSetValue() == nil || ops[1].GetSetOptions() == nil {
			t.Fatal(ops)
		}
		// Applying these operations selects b, then keeps b when options reorder.
		values := []string{"a", "b"}
		selected := "a"
		for _, op := range ops {
			if v := op.GetSetValue(); v != nil {
				selected = values[v.GetI()]
			}
			if v := op.GetSetOptions(); v != nil {
				values = v.Options
			}
		}
		if selected != "b" {
			t.Fatal(selected)
		}
		c.cfg.CoalescingCapacity = 1
		if err := c.Send(patch("p", enabled("quiet", true))); err != nil {
			t.Fatal(err)
		}
		if err := c.Send(patch("p", enabled("quiet", false), enabled("extra", true))); !errors.Is(err, ErrQueueFull) {
			t.Fatal(err)
		}
		if !nextBatch(t, c)[0].GetPanelPatch().Ops[0].GetSetEnabled() {
			t.Fatal("refusal partially applied")
		}
	}
}

func TestDescriptorPushesAreNeverCoalescedOrLocallyReleased(t *testing.T) {
	for _, kind := range []string{"panel", "overlay"} {
		t.Run(kind, func(t *testing.T) {
			c := stateClient(t)
			c.out = make(chan outbound, 2)
			makeMsg := func(offset uint64) *pb.PluginMsg {
				if kind == "panel" {
					return patch("p", &pb.PatchOp{WidgetId: "image", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 17, Offset: offset, Len: 4}}})
				}
				m := overlay("o", offset)
				m.GetOverlayUpdate().Items = []*pb.OverlayItem{{Geometry: &pb.OverlayItem_Marker{Marker: &pb.OverlayMarker{PositionsZyx: &pb.GeomBuffer{Source: &pb.GeomBuffer_Segment{Segment: &pb.SegmentRef{SegmentId: 17, Offset: offset, Len: 4, Uses: 2}}}}}}}
				return m
			}
			for i := uint64(1); i <= 2; i++ {
				if err := c.Send(makeMsg(i)); err != nil {
					t.Fatal(err)
				}
			}
			if err := c.Send(makeMsg(3)); !errors.Is(err, ErrQueueFull) {
				t.Fatal(err)
			}
			if c.ctx.Err() != nil {
				t.Fatal("resource admission refusal killed session")
			}
			for i := uint64(1); i <= 2; i++ {
				batch := nextBatch(t, c)
				if len(batch) != 1 {
					t.Fatal(batch)
				}
				if kind == "panel" {
					if batch[0].GetPanelPatch().Ops[0].GetSetImage().Offset != i {
						t.Fatal(batch)
					}
				} else {
					d := batch[0].GetOverlayUpdate().Items[0].GetMarker().PositionsZyx.GetSegment()
					if d.Offset != i || d.Uses != 2 {
						t.Fatal("shared descriptor altered", d)
					}
				}
			}
		})
	}
}

func TestCoalescingPreservesOrderAcrossOrderedMessages(t *testing.T) {
	c := stateClient(t)
	if err := c.Send(patch("p", enabled("w", true))); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", enabled("w", false), &pb.PatchOp{WidgetId: "image", Op: &pb.PatchOp_SetImage{SetImage: &pb.ImageRef{SegmentId: 1}}})); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", enabled("w", true))); err != nil {
		t.Fatal(err)
	}
	batch := nextBatch(t, c)
	if len(batch) != 2 || !batch[0].GetPanelPatch().Ops[0].GetSetEnabled() || batch[1].GetPanelPatch().Ops[0].GetSetEnabled() {
		t.Fatal(batch)
	}
	if !nextBatch(t, c)[0].GetPanelPatch().Ops[0].GetSetEnabled() {
		t.Fatal("later property lost")
	}
}

func TestShortfallQueuePreservesLargestSnapshotAndCount(t *testing.T) {
	c := stateClient(t)
	msg := func(wanted uint64, count uint32) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_SegmentShortfall{SegmentShortfall: &pb.SegmentShortfall{SegmentId: 1, WantedBytes: wanted, LargestFreeBytes: wanted / 2, Dropped: count}}}
	}
	for _, m := range []*pb.PluginMsg{msg(256, 2), msg(64, 3)} {
		if err := c.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	if err := c.Send(msg(512, math.MaxUint32)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	f := nextBatch(t, c)[0].GetSegmentShortfall()
	if f.WantedBytes != 256 || f.LargestFreeBytes != 128 || f.Dropped != 5 || c.ctx.Err() != nil {
		t.Fatal(f)
	}
}

func TestConcurrentCoalescingAndShutdownFlushBothQueues(t *testing.T) {
	p := openPeer(t, Config{Shutdown: func(_ context.Context, c *Client, _ *pb.Shutdown) error {
		var wg sync.WaitGroup
		for i := 0; i < 8; i++ {
			wg.Add(1)
			go func() {
				defer wg.Done()
				for n := 0; n < 100; n++ {
					if err := c.Send(patch("p", enabled("w", true))); err != nil {
						t.Error(err)
						return
					}
				}
			}()
		}
		wg.Wait()
		if err := c.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_DoorbellFinalReport{DoorbellFinalReport: &pb.DoorbellFinalReport{Detail: "saved"}}}); err != nil {
			return err
		}
		return c.Send(overlay("final", 17))
	}}, nil)
	p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(2000)}}})
	sawPanel, sawFinal, sawOverlay := false, false, false
	for {
		m, err := p.stream.Recv()
		if errors.Is(err, io.EOF) {
			break
		}
		if err != nil {
			t.Fatal(err)
		}
		switch {
		case m.GetPanelPatch() != nil:
			if sawFinal {
				t.Fatal("patch overtook final report")
			}
			sawPanel = true
		case m.GetDoorbellFinalReport() != nil:
			sawFinal = true
		case m.GetOverlayUpdate() != nil:
			if !sawFinal {
				t.Fatal("overlay overtook final report")
			}
			sawOverlay = true
		}
	}
	if !sawPanel || !sawFinal || !sawOverlay {
		t.Fatal("shutdown lost accepted output")
	}
	if err := p.client.Send(overlay("too-late", 18)); err == nil {
		t.Fatal("admission after flush")
	}
}

func TestFutureFieldsRetainOrderedDelivery(t *testing.T) {
	c := stateClient(t)
	m := overlay("future", 1)
	m.GetOverlayUpdate().ProtoReflect().SetUnknown(protowire.AppendVarint(protowire.AppendTag(nil, 100, protowire.VarintType), 42))
	if err := c.Send(m); err != nil {
		t.Fatal(err)
	}
	m.GetOverlayUpdate().Revision = 2
	if err := c.Send(m); err != nil {
		t.Fatal(err)
	}
	for i := uint64(1); i <= 2; i++ {
		batch := nextBatch(t, c)
		if len(batch) != 1 || batch[0].GetOverlayUpdate().Revision != i || len(batch[0].GetOverlayUpdate().ProtoReflect().GetUnknown()) == 0 {
			t.Fatal(batch)
		}
	}
}

func TestShortfallReporterRetriesLocalAdmissionWithoutLosingCounts(t *testing.T) {
	c := stateClient(t)
	c.cfg.CoalescingCapacity = 1
	if err := c.Send(overlay("occupied", 1)); err != nil {
		t.Fatal(err)
	}
	s := NewShortfalls()
	if err := s.Observe(memory.Shortfall{SegmentID: 17, Region: 0, WantedBytes: 256}); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(c.ctx, 2*time.Second)
	defer cancel()
	done := make(chan error, 1)
	go func() { done <- s.Run(ctx, c) }()
	for c.QueueStats().Refused == 0 {
		select {
		case err := <-done:
			t.Fatal("reporter stopped", err)
		case <-ctx.Done():
			t.Fatal(ctx.Err())
		case <-time.After(time.Millisecond):
		}
	}
	if c.ctx.Err() != nil {
		t.Fatal("refusal killed session")
	}
	nextBatch(t, c) // Free the sender key; the rejected report must still exist.
	batch := make(chan []*pb.PluginMsg, 1)
	go func() {
		x, ok := c.nextOutbound()
		if ok {
			batch <- x.before
		}
	}()
	select {
	case msgs := <-batch:
		if len(msgs) != 1 || msgs[0].GetSegmentShortfall().GetWantedBytes() != 256 || msgs[0].GetSegmentShortfall().GetDropped() != 1 {
			t.Fatal(msgs)
		}
	case err := <-done:
		t.Fatal("reporter lost refused report", err)
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	cancel()
	<-done
}

func TestDropdownUndrainedOrderAndBoundedRefusal(t *testing.T) {
	c := stateClient(t)
	c.out = make(chan outbound, 2)
	options := func(v ...string) *pb.PatchOp {
		return &pb.PatchOp{WidgetId: "menu", Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: v}}}
	}
	value := &pb.PatchOp{WidgetId: "menu", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_I{I: 1}}}}
	if err := c.Send(patch("p", options("a", "b"), value)); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", options("b", "a"))); err != nil {
		t.Fatal(err)
	}
	if err := c.Send(patch("p", options("c"))); !errors.Is(err, ErrQueueFull) {
		t.Fatal("unbounded sensitive queue", err)
	}
	if c.ctx.Err() != nil {
		t.Fatal("sender refusal killed session")
	}
	first := nextBatch(t, c)[0].GetPanelPatch().Ops
	second := nextBatch(t, c)[0].GetPanelPatch().Ops
	if len(first) != 2 || len(second) != 1 || first[0].GetSetOptions().Options[0] != "a" || first[1].GetSetValue().GetI() != 1 || second[0].GetSetOptions().Options[0] != "b" {
		t.Fatal(first, second)
	}
}

func TestDropdownValuesMergeAfterOptionsBoundary(t *testing.T) {
	c := stateClient(t)
	options := &pb.PatchOp{WidgetId: "menu", Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: []string{"a", "b", "c"}}}}
	value := func(i int64) *pb.PatchOp {
		return &pb.PatchOp{WidgetId: "menu", Op: &pb.PatchOp_SetValue{SetValue: &pb.Value{Kind: &pb.Value_I{I: i}}}}
	}
	for _, m := range []*pb.PluginMsg{patch("p", value(0)), patch("p", options), patch("p", value(1)), patch("p", value(2))} {
		if err := c.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	first := nextBatch(t, c)
	if len(first) != 2 || first[0].GetPanelPatch().Ops[0].GetSetValue().GetI() != 0 || first[1].GetPanelPatch().Ops[0].GetSetOptions() == nil {
		t.Fatal("options crossed older value", first)
	}
	last := nextBatch(t, c)
	if len(last) != 1 || len(last[0].GetPanelPatch().Ops) != 1 || last[0].GetPanelPatch().Ops[0].GetSetValue().GetI() != 2 {
		t.Fatal("values after options did not merge", last)
	}
	if c.QueueStats().Merged != 1 {
		t.Fatal(c.QueueStats())
	}
}

func TestHeldWholePatchCeilingRefusesWithoutLosingPreviousBatch(t *testing.T) {
	c := stateClient(t)
	ops := []*pb.PatchOp{{WidgetId: "menu", Op: &pb.PatchOp_SetOptions{SetOptions: &pb.Options{Options: []string{"a"}}}}}
	for range 255 {
		ops = append(ops, enabled("menu", true))
	}
	if err := c.Send(patch("p", ops...)); err != nil {
		t.Fatal(err)
	}
	ops = append(ops, enabled("menu", false))
	if err := c.Send(patch("p", ops...)); !errors.Is(err, ErrQueueFull) {
		t.Fatal(err)
	}
	if got := nextBatch(t, c)[0].GetPanelPatch().Ops; len(got) != 256 {
		t.Fatal("refusal changed accepted batch", len(got))
	}
	if c.ctx.Err() != nil {
		t.Fatal("sender refusal faulted session")
	}
}
