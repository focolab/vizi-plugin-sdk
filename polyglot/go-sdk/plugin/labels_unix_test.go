//go:build linux || darwin

package plugin

import (
	"context"
	"errors"
	"maps"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// Called by the real ServeEnv fixture with its independently granted O arena.
func testLabelVolumeLoans(t *testing.T, s *control.Session, o *Overlay, seg *memory.Segment, file *os.File, send func(*pb.HostMsg), recv func() *pb.PluginMsg) {
	t.Helper()
	outstanding := func(want uint64) {
		t.Helper()
		deadline := time.Now().Add(time.Second)
		for {
			stats, err := seg.LoanStats(1)
			if err != nil {
				t.Fatal(err)
			}
			if stats.Outstanding == want {
				return
			}
			if time.Now().After(deadline) {
				t.Fatalf("outstanding=%d, want %d", stats.Outstanding, want)
			}
			time.Sleep(time.Millisecond)
		}
	}
	release := func(item *pb.OverlayItem) {
		for _, b := range []*pb.GeomBuffer{item.GetLabelVolume().Labels, item.GetLabelVolume().Colormap} {
			d := b.GetSegment()
			send(&pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: d.SegmentId, Region: 1, Offset: d.Offset, Len: d.Len}}})
		}
	}
	labels, _ := payload.Encode([]uint16{1, 256}, []uint32{1, 1, 2}, true)
	v := LabelVolume{ID: "labels", Labels: labels, PositionZYX: geometry.ZYX{2, 3, 4}, T: proto.Uint32(5), Names: map[uint32]string{1: "one", 256: "high"}, Visible: map[uint32]bool{1: true, 256: false}}
	v.Colormap[1] = [4]uint8{230, 25, 75, 180}
	large := v
	large.Labels, _ = payload.Encode(make([]byte, 4096), []uint32{1, 1, 4096}, true)
	if err := o.ReplaceLabelVolume(s, large); !errors.Is(err, memory.ErrArenaFull) {
		t.Fatal("expected LUT allocation failure", err)
	}
	outstanding(0) // first reservation rolled back when second did not fit
	// Allocation failure also schedules a report on Shortfalls.Run's 100 ms
	// ticker. Observe it here: merely skipping it during subsequent reads can
	// leave it pending when this helper returns, racing the outer test's final
	// report assertion. No other outbound message is expected at this point.
	shortfallMessage := recv()
	shortfall := shortfallMessage.GetSegmentShortfall()
	if shortfall == nil || shortfall.SegmentId != seg.ID() || shortfall.Region != 1 || shortfall.WantedBytes != 1024 || shortfall.LargestFreeBytes != 896 || shortfall.Dropped != 1 {
		t.Fatal("missing allocation shortfall facts; received", shortfallMessage)
	}
	invalid := v
	invalid.Names = map[uint32]string{1: strings.Repeat("x", control.MaxInlineBytes+1)}
	if err := o.ReplaceLabelVolume(s, invalid); err == nil {
		t.Fatal("oversized label name passed outbound validation")
	}
	outstanding(0) // validation after both reservations reclaimed both locally
	priorRevision := o.revision
	if err := o.ReplaceLabelVolume(s, v); err != nil {
		t.Fatal(err)
	}
	pushed := recv().GetOverlayUpdate()
	if pushed == nil || pushed.Revision != priorRevision+1 {
		t.Fatal("label revision", pushed)
	}
	item := pushed.Items[0]
	if !maps.Equal(item.GetLabelVolume().LabelVisible, v.Visible) {
		t.Fatal("push lost per-label visibility", item)
	}
	refs := []*pb.SegmentRef{item.GetLabelVolume().Labels.GetSegment(), item.GetLabelVolume().Colormap.GetSegment()}
	if refs[0].Uses != 1 || refs[1].Uses != 1 || refs[0].SegmentId != seg.ID() || refs[1].SegmentId != seg.ID() || refs[0].Dtype != pb.DType_DTYPE_U16 || item.GetLabelVolume().GetT() != 5 || item.GetLabelVolume().LabelNames[256] != "high" {
		t.Fatal("bad label descriptor", item)
	}
	bytes := make([]byte, 4)
	if _, err := file.ReadAt(bytes, int64(refs[0].Offset)); err != nil {
		t.Fatal(err)
	}
	if string(bytes) != string([]byte{1, 0, 0, 1}) {
		t.Fatal("u16 bytes changed", bytes)
	}
	rgba := make([]byte, 4)
	if _, err := file.ReadAt(rgba, int64(refs[1].Offset+4)); err != nil {
		t.Fatal(err)
	}
	if string(rgba) != string([]byte{230, 25, 75, 180}) {
		t.Fatal("LUT changed", rgba)
	}
	outstanding(2)
	if err := o.Clear(); err != nil {
		t.Fatal(err)
	}
	if removed := recv().GetOverlayRemove(); removed.GetRevision() != pushed.Revision+1 {
		t.Fatal("clear sequence", removed)
	}
	// Retain the push loans while compiling: its descriptors must be distinct.
	type result struct {
		r   *pb.LabelVolumeCompiled
		err error
	}
	done := make(chan result, 1)
	go func() { r, e := o.CompileLabelVolume(s.Client.Context(), s, v, true); done <- result{r, e} }()
	q := recv()
	compile := q.GetCompileLabelVolume()
	if compile == nil || !compile.ReplaceAll || compile.OverlayId != o.id || compile.Item.GetLabelVolume().Labels.GetSegment().Offset == refs[0].Offset {
		t.Fatal("compile reused pushed loan", q)
	}
	if !maps.Equal(compile.Item.GetLabelVolume().LabelVisible, v.Visible) {
		t.Fatal("compile lost per-label visibility", q)
	}
	outstanding(4)
	release(compile.Item)
	outstanding(2) // release precedes reply; cleanup must not release twice
	send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_LabelVolumeCompiled{LabelVolumeCompiled: &pb.LabelVolumeCompiled{RegionCount: 2}}})
	got := <-done
	if got.err != nil || got.r.GetRegionCount() != 2 {
		t.Fatal(got)
	}
	release(item)
	outstanding(0)
	// Definitely unadmitted cancellation reclaims both reservations.
	ctx, cancel := context.WithCancel(s.Client.Context())
	cancel()
	if _, err := o.CompileLabelVolume(ctx, s, v, true); !errors.Is(err, context.Canceled) {
		t.Fatal(err)
	}
	outstanding(0)
	// Cancellation after admission keeps both for the host, without stranded uses.
	ctx, cancel = context.WithCancel(s.Client.Context())
	go func() { r, e := o.CompileLabelVolume(ctx, s, v, true); done <- result{r, e} }()
	q = recv()
	cancel()
	got = <-done
	if !errors.Is(got.err, context.Canceled) {
		t.Fatal(got)
	}
	outstanding(2)
	if stats, _ := seg.LoanUseStats(1); stats.Stranded != 0 || stats.Named != 2 {
		t.Fatal("timeout tried local rollback", stats)
	}
	release(q.GetCompileLabelVolume().Item)
	outstanding(0)
	// Host refusal after release also leaves the arena usable.
	go func() { r, e := o.CompileLabelVolume(s.Client.Context(), s, v, true); done <- result{r, e} }()
	q = recv()
	release(q.GetCompileLabelVolume().Item)
	outstanding(0)
	send(&pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_HostError{HostError: &pb.Error{Kind: pb.Error_INVALID_ARGUMENT, Message: "refused"}}})
	got = <-done
	var hostErr *control.HostError
	if !errors.As(got.err, &hostErr) {
		t.Fatal(got)
	}
	if err := o.ReplaceLabelVolume(s, v); err != nil {
		t.Fatal("host refusal quarantined arena", err)
	}
	release(recv().GetOverlayUpdate().Items[0])
	outstanding(0)
}
