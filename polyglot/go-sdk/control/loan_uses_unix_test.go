//go:build linux || darwin

package control

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

func TestPushAndCompileShareSenderUsesInEitherOrder(t *testing.T) {
	for _, requestFirst := range []bool{false, true} {
		t.Run(map[bool]string{false: "push-first", true: "request-first"}[requestFirst], func(t *testing.T) {
			m, _, _ := memoryPeerFor(t, true, memory.PurposeOverlay, pb.RegionProducer_REGION_PRODUCER_PLUGIN)
			seg, _ := m.Registry.Segment(17)
			key, err := seg.WriteLoan(0, []byte{1, 2, 3, 4})
			if err != nil {
				t.Fatal(err)
			}
			ref := &pb.SegmentRef{SegmentId: 17, Offset: key.Offset, Len: key.Length, Uses: 2, Dtype: pb.DType_DTYPE_U8, ShapeZyx: []uint32{1, 1, 4}}
			item := &pb.OverlayItem{ItemId: "labels", Geometry: &pb.OverlayItem_LabelVolume{LabelVolume: &pb.OverlayLabelVolume{
				Labels:   &pb.GeomBuffer{Shape: []uint32{1, 1, 4}, Dtype: pb.DType_DTYPE_U8, Source: &pb.GeomBuffer_Segment{Segment: ref}},
				Colormap: &pb.GeomBuffer{Shape: []uint32{256, 4}, Dtype: pb.DType_DTYPE_U8, Source: &pb.GeomBuffer_Inline{Inline: make([]byte, 1024)}},
			}}}
			p := openPeer(t, Config{AdmitOutbound: m.AdmitOutbound, SentOutbound: m.SentOutbound, Arrange: m.Apply}, nil)
			push := &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: "o", Revision: 1, Items: []*pb.OverlayItem{item}}}}
			request := &pb.CompileLabelVolume{OverlayId: "o", Item: item}
			call := func() {
				t.Helper()
				done := make(chan error, 1)
				ctx, cancel := context.WithCancel(p.ctx)
				defer cancel()
				go func() { _, err := p.client.CompileLabelVolume(ctx, request); done <- err }()
				got := p.recv(t)
				if got.GetCompileLabelVolume().GetItem().GetLabelVolume().GetLabels().GetSegment().GetUses() != 2 {
					t.Fatal(got)
				}
				// Cancellation after the request crossed the wire cannot reclaim it.
				cancel()
				if err := <-done; !errors.Is(err, context.Canceled) {
					t.Fatal(err)
				}
			}
			if requestFirst {
				call()
			}
			if err := p.client.Send(push); err != nil {
				t.Fatal(err)
			}
			if p.recv(t).GetOverlayUpdate() == nil {
				t.Fatal("missing push")
			}
			if !requestFirst {
				call()
			}
			if err := p.client.Send(push); !errors.Is(err, memory.ErrUsesExceeded) {
				t.Fatal("excess push", err)
			}
			if _, err := p.client.CompileLabelVolume(p.ctx, request); !errors.Is(err, memory.ErrUsesExceeded) {
				t.Fatal("excess request", err)
			}
			if err := seg.ReleaseLoan(key); !errors.Is(err, memory.ErrLoanAdmitted) {
				t.Fatal(err)
			}
			if stats, _ := seg.LoanUseStats(0); stats.Named != 2 || stats.Stranded != 1 {
				t.Fatal(stats)
			}
			// A later ordinary push is a wire barrier: neither refused use escaped.
			if err := p.client.Send(&pb.PluginMsg{Msg: &pb.PluginMsg_OverlayRemove{OverlayRemove: &pb.OverlayRemove{OverlayId: "barrier"}}}); err != nil {
				t.Fatal(err)
			}
			if p.recv(t).GetOverlayRemove().GetOverlayId() != "barrier" {
				t.Fatal("excess descriptor escaped")
			}
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ReleaseLoan{ReleaseLoan: &pb.ReleaseLoan{SegmentId: 17, Region: 0, Offset: key.Offset, Len: key.Length}}})
			deadline := time.NewTimer(time.Second)
			defer deadline.Stop()
			for {
				stats, _ := seg.LoanStats(0)
				if stats.Outstanding == 0 {
					break
				}
				select {
				case <-deadline.C:
					t.Fatal("host release did not settle reservation")
				case <-time.After(time.Millisecond):
				}
			}
		})
	}
}
