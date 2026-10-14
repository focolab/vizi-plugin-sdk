package control

import (
	"context"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestFollowCoalescingIsWholeStateKeyedAndEndIsOrdered(t *testing.T) {
	c := stateClient(t)
	offer := func(panel, label string) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_OfferFollow{OfferFollow: &pb.OfferFollow{PanelId: panel, Label: label}}}
	}
	state := func(id, seq uint64, v *uint32, region *string) *pb.PluginMsg {
		return &pb.PluginMsg{Msg: &pb.PluginMsg_FollowState{FollowState: &pb.FollowState{FollowId: id, Seq: seq, T: v, FrameRegion: region}}}
	}
	want := []*pb.PluginMsg{offer("p", ""), offer("other", "kept"), state(1, 2, nil, proto.String("new")), state(2, 1, proto.Uint32(0), nil), {Msg: &pb.PluginMsg_EndFollow{EndFollow: &pb.EndFollow{FollowId: 1}}}}
	for _, m := range []*pb.PluginMsg{offer("p", "old"), want[1], state(1, 1, proto.Uint32(5), nil), want[3], want[0], want[2], want[4]} {
		if err := c.Send(m); err != nil {
			t.Fatal(err)
		}
	}
	batch := nextBatch(t, c)
	if len(batch) != len(want) {
		t.Fatal(batch)
	}
	for i, m := range batch {
		if !proto.Equal(m, want[i]) {
			t.Fatalf("item %d: got %v want %v", i, m, want[i])
		}
	}
	if c.QueueStats().Merged != 2 {
		t.Fatal(c.QueueStats())
	}
	for _, m := range []*pb.HostMsg{{Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{}}}, {Msg: &pb.HostMsg_FollowEnded{FollowEnded: &pb.FollowEnded{}}}} {
		if HostClass(m) != OneWay {
			t.Fatal(m)
		}
	}
}

func TestRawFollowNoticesReachOnMessageInOrder(t *testing.T) {
	notices := make(chan *pb.HostMsg, 2)
	p := openPeer(t, Config{OnMessage: func(_ context.Context, _ *Client, m *pb.HostMsg) error { notices <- m; return nil }}, nil)
	for _, m := range []*pb.HostMsg{
		{Id: "not a request", Msg: &pb.HostMsg_FollowStarted{FollowStarted: &pb.FollowStarted{FollowId: 1, PanelId: "p", Targets: []string{"t"}}}},
		{Id: "not a reply", Msg: &pb.HostMsg_FollowEnded{FollowEnded: &pb.FollowEnded{FollowId: 1, Reason: pb.FollowEndReason_FOLLOW_END_STOPPED}}},
	} {
		p.send(t, m)
		select {
		case got := <-notices:
			if !proto.Equal(got, m) {
				t.Fatal(got, m)
			}
		case <-p.ctx.Done():
			t.Fatal(p.ctx.Err())
		}
	}
}
