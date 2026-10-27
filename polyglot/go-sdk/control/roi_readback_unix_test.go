//go:build linux || darwin

package control

import (
	"strings"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestRoiInclusionDepositUsesAndCopyFailure(t *testing.T) {
	memoryPeerFor(t, false, "V", pb.RegionProducer_REGION_PRODUCER_HOST, func(m *Memory, install *pb.InstallSegment, grant func(*Client)) {
		p := openPeer(t, Config{Admit: m.Admit, Prepare: m.WaitDescriptor, Release: m.Release, Arrange: m.Apply}, nil)
		grant(p.client)
		p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_InstallSegment{InstallSegment: install}})
		if p.recv(t).GetGrantAck().GetInstalled() == nil {
			t.Fatal("missing grant acknowledgement")
		}
		replies := make(chan *RoiInclusionReply, 1)
		errs := make(chan error, 1)
		read := func() *RoiInclusionReply {
			t.Helper()
			go func() {
				r, err := p.client.ReadRoiInclusion(p.ctx, &pb.ReadRoiInclusion{RegionId: "region", T: 2})
				replies <- r
				errs <- err
			}()
			request := p.recv(t)
			if request.GetReadRoiInclusion() == nil {
				t.Fatal(request)
			}
			// Both replies own a use of the same one-voxel U8 deposit.
			expected := &pb.RoiInclusion{Segment: &pb.SegmentRef{SegmentId: 17, Offset: 128, Len: 1, Uses: 2, ShapeZyx: []uint32{1, 1, 1}, Dtype: pb.DType_DTYPE_U8}, OriginZyx: []uint32{4, 5, 6}, RegionId: "region", T: 2, OpenGeneration: 19, Present: true, VoxelCount: 1}
			p.send(t, &pb.HostMsg{Id: request.Id, Msg: &pb.HostMsg_RoiInclusion{RoiInclusion: expected}})
			r, err := <-replies, <-errs
			if err != nil || r == nil || !proto.Equal(r.Inclusion, expected) {
				t.Fatal(r, err)
			}
			return r
		}
		first, second := read(), read()
		bytes := make([]byte, 1)
		if err := m.CopyDescriptor(first.Inclusion.Segment, bytes); err != nil || bytes[0] != 1 {
			t.Fatal(bytes, err)
		}
		if err := first.Close(); err != nil {
			t.Fatal(err)
		}
		if err := first.Close(); err != nil {
			t.Fatal(err)
		}
		// A barrier proves the first close (even twice) did not release the shared use.
		p.send(t, &pb.HostMsg{Id: freshID("barrier"), Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
		if got := p.recv(t); !strings.HasPrefix(got.Id, "barrier") || got.GetGrantState() == nil {
			t.Fatal("early release", got)
		}
		if err := m.CopyDescriptor(second.Inclusion.Segment, nil); err == nil {
			t.Fatal("short destination accepted")
		}
		if err := second.Close(); err != nil {
			t.Fatal(err)
		}
		if err := second.Close(); err != nil {
			t.Fatal(err)
		}
		release := p.recv(t).GetReleaseDeposit()
		if release == nil || release.SegmentId != 17 || release.Region != 0 || release.Offset != 128 || release.Len != 1 {
			t.Fatal(release)
		}
		p.send(t, &pb.HostMsg{Id: "after-release", Msg: &pb.HostMsg_GrantStateQuery{GrantStateQuery: &pb.GrantStateQuery{SegmentId: 17}}})
		if got := p.recv(t); got.Id != "after-release" || got.GetGrantState() == nil {
			t.Fatal("duplicate release", got)
		}
	})
}
