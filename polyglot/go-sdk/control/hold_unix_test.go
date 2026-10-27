//go:build linux || darwin

package control

import (
	"context"
	"fmt"
	"strings"
	"sync/atomic"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestDescriptorsWaitForEitherHalfOverSession(t *testing.T) {
	for _, order := range []string{"install-first", "capability-first", "neither-install-first", "neither-capability-first", "embedded"} {
		t.Run(order, func(t *testing.T) {
			memoryPeerFor(t, false, "X", pb.RegionProducer_REGION_PRODUCER_HOST, func(m *Memory, install *pb.InstallSegment, grant func(*Client)) {
				var handled atomic.Int32
				p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply,
					HandleRequest: func(_ context.Context, _ *Client, msg *pb.HostMsg) (*pb.PluginMsg, error) {
						data := make([]byte, 4)
						if err := m.CopyDescriptor(msg.GetExportChunk().GetSegment(), data); err != nil {
							return nil, err
						}
						if string(data) != string([]byte{1, 3, 5, 7}) {
							return nil, fmt.Errorf("wrong payload: %v", data)
						}
						return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: &pb.ChunkWritten{TotalBytes: uint64(handled.Add(1))}}}, nil
					}}, nil)
				installNow := func() { p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: install}}) }
				if order == "install-first" {
					installNow()
				} else if order == "capability-first" {
					grant(p.client)
				}
				for i := range 3 {
					chunk := &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4, Uses: 3}}}
					if order == "embedded" && i == 0 {
						chunk.Install = install
					}
					p.send(t, &pb.HostMsg{Id: fmt.Sprint(i), Msg: &pb.HostMsg_ExportChunk{ExportChunk: chunk}})
				}
				// A query behind the chunks proves routing has admitted all three
				// while their segment remains unavailable; no reply/read is owed yet.
				p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
				if got := p.recv(t); !strings.HasPrefix(got.Id, "barrier") || got.GetGrantState() == nil || handled.Load() != 0 {
					t.Fatal("descriptor dispatched before installation", got)
				}
				switch order {
				case "install-first", "embedded":
					grant(p.client)
				case "capability-first":
					installNow()
				case "neither-install-first":
					installNow()
					grant(p.client)
				case "neither-capability-first":
					grant(p.client)
					installNow()
				}
				acks, replies, releases := 0, 0, 0
				for range 5 {
					got := p.recv(t)
					switch {
					case got.GetGrantAck() != nil:
						if got.GetGrantAck().GetInstalled() == nil {
							t.Fatal(got)
						}
						acks++
					case got.GetReleaseDeposit() != nil:
						if replies != 2 {
							t.Fatal("deposit released before final use", got)
						}
						releases++
					case got.GetChunkWritten() != nil:
						if got.Id != fmt.Sprint(replies) || got.GetChunkWritten().TotalBytes != uint64(replies+1) {
							t.Fatal("dispatch order changed", got)
						}
						replies++
					default:
						t.Fatal(got)
					}
				}
				if acks != 1 || replies != 3 || releases != 1 || m.DroppedDescriptors() != 0 {
					t.Fatal(acks, replies, releases)
				}
			})
		})
	}
}

func TestPendingDescriptorRepliesAreHeldAndDisposed(t *testing.T) {
	memoryPeerFor(t, false, "V", pb.RegionProducer_REGION_PRODUCER_HOST, func(m *Memory, install *pb.InstallSegment, grant func(*Client)) {
		p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply}, nil)
		d := &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4, Uses: 3}
		// Late/unmatched replies still hold and own their descriptor uses.
		p.send(t, &pb.HostMsg{Id: "late-volume", Msg: &pb.HostMsg_DataVolume{DataVolume: &pb.DataVolume{Segment: d}}})
		p.send(t, &pb.HostMsg{Id: "late-mask", Msg: &pb.HostMsg_MaskVoxels{MaskVoxels: &pb.MaskVoxels{Segment: d}}})
		p.send(t, &pb.HostMsg{Id: "late-inclusion", Msg: &pb.HostMsg_RoiInclusion{RoiInclusion: &pb.RoiInclusion{Segment: d, Present: true, VoxelCount: 4}}})
		p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
		if got := p.recv(t); !strings.HasPrefix(got.Id, "barrier") {
			t.Fatal(got)
		}
		grant(p.client)
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: install}})
		acks, releases := 0, 0
		for range 2 {
			got := p.recv(t)
			if got.GetGrantAck().GetInstalled() != nil {
				acks++
			} else if r := got.GetReleaseDeposit(); r != nil && r.SegmentId == 17 && r.Len == 4 {
				releases++
			} else {
				t.Fatal(got)
			}
		}
		if acks != 1 || releases != 1 || m.DroppedDescriptors() != 0 {
			t.Fatal(acks, releases)
		}
	})
}

func TestHeldDescriptorsDropWithGrant(t *testing.T) {
	for _, fail := range []bool{false, true} {
		t.Run(fmt.Sprint("failure=", fail), func(t *testing.T) {
			r, _ := memory.New(memory.Config{})
			defer r.Close()
			m := NewMemory(r)
			var handled atomic.Int32
			p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply,
				HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) { handled.Add(1); return nil, nil },
			}, nil)
			p.send(t, &pb.HostMsg{Id: "held", Msg: &pb.HostMsg_ExportChunk{ExportChunk: &pb.ExportChunk{Source: &pb.ExportChunk_Segment{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 4}}}}})
			if fail {
				p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: &pb.InstallSegment{SegmentId: 17, Purpose: "unsupported"}}})
				if p.recv(t).GetGrantAck().GetFailed() == nil {
					t.Fatal("grant did not fail")
				}
			} else {
				p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RetireSegment{RetireSegment: &pb.RetireSegment{SegmentId: 17}}})
				if p.recv(t).GetSegmentReleased() == nil {
					t.Fatal("grant did not retire")
				}
			}
			// The descriptor is discarded, but §11a still requires a request
			// answer. Shutdown waits for the pending preparation and that answer.
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{DeadlineMs: proto.Uint32(1000)}}})
			if got := p.recv(t); got.Id != "held" || got.GetError().GetKind() != pb.Error_NOT_FOUND || !strings.Contains(got.GetError().GetMessage(), "segment 17") {
				t.Fatal("dropped request must receive an error naming its segment", got)
			}
			if got := p.recv(t); got.GetShutdownReport() == nil {
				t.Fatal("drop prevented shutdown", got)
			}
			if handled.Load() != 0 || m.DroppedDescriptors() != 1 {
				t.Fatal(handled.Load(), m.DroppedDescriptors())
			}
		})
	}
}
