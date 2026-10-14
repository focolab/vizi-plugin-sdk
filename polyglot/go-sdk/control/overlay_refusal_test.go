package control

import (
	"context"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/violations"
	"google.golang.org/protobuf/proto"
)

func TestReceivesHostPushRefusals(t *testing.T) {
	received := make(chan *pb.ContractViolation, 1)
	p := openPeer(t, Config{OnMessage: func(_ context.Context, _ *Client, m *pb.HostMsg) error {
		received <- m.GetContractViolation()
		return nil
	}}, nil)
	for _, tc := range []struct {
		where                      pb.ViolationSite
		number                     int32
		wireName, siteName, detail string
	}{
		{pb.ViolationSite_VIOLATION_SITE_OVERLAY_ITEM_REFUSED, 28, "VIOLATION_SITE_OVERLAY_ITEM_REFUSED", "overlay_item_refused", "overlay segmentation, item labels: position must be integer"},
		{pb.ViolationSite_VIOLATION_SITE_OVERLAY_CHUNK_REFUSED, 30, "VIOLATION_SITE_OVERLAY_CHUNK_REFUSED", "overlay_chunk_refused", "chunk bands: box outside wanted level"},
		{pb.ViolationSite_VIOLATION_SITE_PANEL_PATCH_REFUSED, 29, "VIOLATION_SITE_PANEL_PATCH_REFUSED", "panel_patch_refused", "panel profile, op set_image, widget plot: PNG does not decode"},
	} {
		t.Run(tc.siteName, func(t *testing.T) {
			want := &pb.ContractViolation{Where: tc.where, Detail: tc.detail, Count: 3}
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ContractViolation{ContractViolation: want}})
			select {
			case got := <-received:
				if !proto.Equal(got, want) || int32(got.Where) != tc.number {
					t.Fatalf("lost host refusal: %v", got)
				}
				if got.Where.String() != tc.wireName || violations.Site(got.Where).String() != tc.siteName {
					t.Fatalf("site %d is not named", tc.number)
				}
			case <-p.ctx.Done():
				t.Fatal("host refusal was not delivered", p.ctx.Err())
			}
		})
	}
}
