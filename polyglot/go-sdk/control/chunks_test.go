package control

import (
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestChunkAndTableCoalescingKeys(t *testing.T) {
	c := stateClient(t)
	chunk := func(level uint32, time *uint32, value byte) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_OverlayChunk{OverlayChunk: &pb.OverlayChunk{OverlayId: "o", Level: level, T: time, Labels: &pb.GeomBuffer{Source: &pb.GeomBuffer_Inline{Inline: []byte{value}}}}}}
	}
	for _, m := range []*pb.PluginMsg{
		chunk(0, nil, 1), chunk(0, proto.Uint32(0), 2), chunk(1, nil, 3), chunk(0, nil, 4),
		{Msg: &pb.PluginMsg_OverlayUpdate{OverlayUpdate: &pb.OverlayUpdate{OverlayId: "o", Revision: 1}}},
		{Msg: &pb.PluginMsg_OverlayLabelTable{OverlayLabelTable: &pb.OverlayLabelTable{OverlayId: "o", LabelNames: map[uint32]string{1: "old"}}}},
		{Msg: &pb.PluginMsg_OverlayLabelTable{OverlayLabelTable: &pb.OverlayLabelTable{OverlayId: "o", LabelNames: map[uint32]string{2: "new"}}}},
	} {
		if err := c.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != 5 {
		t.Fatal("keys collided", batch)
	}
	if v := batch[0].GetOverlayChunk(); v.T != nil || v.Labels.GetInline()[0] != 4 {
		t.Fatal(v)
	}
	if v := batch[1].GetOverlayChunk(); v.T == nil || *v.T != 0 || v.Labels.GetInline()[0] != 2 {
		t.Fatal(v)
	}
	if batch[2].GetOverlayChunk().Level != 1 || batch[3].GetOverlayUpdate() == nil {
		t.Fatal(batch)
	}
	if v := batch[4].GetOverlayLabelTable(); len(v.LabelNames) != 1 || v.LabelNames[2] != "new" {
		t.Fatal(v)
	}
}
