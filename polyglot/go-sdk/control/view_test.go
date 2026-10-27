package control

import (
	"context"
	"errors"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestViewRequestLocalRefusals(t *testing.T) {
	// A nil client proves these requests never enter the transport.
	var c *Client
	for _, q := range []*pb.ViewRequest{nil, {}, {Decline: true}, {Cause: 1},
		{Cause: 1, Decline: true, T: proto.Uint32(0)},
		{Cause: 1, ToolCause: &pb.ToolCause{Tool: 2, Interaction: 3}, Decline: true},
		{ToolCause: &pb.ToolCause{Tool: 2}, Decline: true},
		{ToolCause: &pb.ToolCause{Interaction: 3}, Decline: true}} {
		if _, err := c.RequestView(context.Background(), q); err == nil {
			t.Fatal("accepted invalid view request", q)
		}
	}
}

func TestViewRequestsAndOrdinaryRefusals(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	requests := []*pb.ViewRequest{
		{Cause: 1, T: proto.Uint32(0), Z: proto.Uint32(0), SelectRegion: proto.String(""), FrameRegion: proto.String("region")},
		{Cause: 2, Decline: true},
		{ToolCause: &pb.ToolCause{Tool: 7, Interaction: 12}, LookAt: &pb.LookAt{TargetZyx: []float64{1, 2, 3}, FitExtent: 4, DirectionZyx: []float64{0, 0, 1}}, OrbitPoint: &pb.OrbitPoint{Zyx: []float64{3, 2, 1}}},
	}
	for reason := pb.ViewRefusal_VIEW_REFUSAL_NOT_ENABLED; reason <= pb.ViewRefusal_VIEW_REFUSAL_INVALID; reason++ {
		requests = append(requests, &pb.ViewRequest{Cause: uint64(reason) + 20, T: proto.Uint32(5)})
	}
	for i, q := range requests {
		want := &pb.ViewRequestResult{Cause: q.Cause, ToolCause: q.ToolCause, Outcome: pb.ViewOutcome_VIEW_OUTCOME_APPLIED}
		if q.Decline {
			want.Outcome = pb.ViewOutcome_VIEW_OUTCOME_DECLINED
		} else if i >= 2 {
			want.Outcome, want.Reason, want.Detail = pb.ViewOutcome_VIEW_OUTCOME_REFUSED, pb.ViewRefusal_VIEW_REFUSAL_UNSUPPORTED, "ordinary refusal"
			if i >= 3 {
				want.Reason = pb.ViewRefusal(i - 2)
			}
		}
		done := make(chan error, 1)
		go func() {
			got, err := p.client.RequestView(p.ctx, q)
			if err == nil && !proto.Equal(got, want) {
				err = errors.New("view result changed")
			}
			done <- err
		}()
		wire := p.recv(t)
		if !proto.Equal(wire.GetViewRequest(), q) || wire.Id == "" {
			t.Fatal(wire, q)
		}
		p.send(t, &pb.HostMsg{Id: wire.Id, Msg: &pb.HostMsg_ViewRequestResult{ViewRequestResult: want}})
		if err := <-done; err != nil {
			t.Fatal(err)
		}
	}
}

func TestViewReplyMustEchoCause(t *testing.T) {
	p := openPeer(t, Config{}, nil)
	done := make(chan error, 1)
	go func() { _, err := p.client.RequestView(p.ctx, &pb.ViewRequest{Cause: 5, Decline: true}); done <- err }()
	q := p.recv(t)
	p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ViewRequestResult{ViewRequestResult: &pb.ViewRequestResult{Cause: 6, Outcome: pb.ViewOutcome_VIEW_OUTCOME_DECLINED}}})
	var he *HostError
	if err := <-done; !errors.As(err, &he) || he.Kind != pb.Error_PROTOCOL {
		t.Fatal(err)
	}
}
