package control

import (
	"context"
	"errors"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// ViewTargets is one atomic move. Pointers preserve zero frames and empty region
// selections. The host checks declarations, bounds, freshness and user consent.
type ViewTargets struct {
	T, Z                      *uint32
	SelectRegion, FrameRegion *string
	LookAt                    *pb.LookAt
	OrbitPoint                *pb.OrbitPoint
}

// Request constructs a command-caused request. RequestView snapshots it before
// sending; do not mutate targets concurrently with that call.
func (v ViewTargets) Request(cause uint64) *pb.ViewRequest {
	return &pb.ViewRequest{Cause: cause, T: v.T, Z: v.Z, SelectRegion: v.SelectRegion,
		FrameRegion: v.FrameRegion, LookAt: v.LookAt, OrbitPoint: v.OrbitPoint}
}

// RequestView answers a user cause with a move or decline. REFUSED (including
// UNSUPPORTED) is an ordinary result, not an error. The reply acknowledges the
// move; ViewChanged does not. Answer within 1 s; causes expire after 2 s.
// Identical retries are idempotent at the host. A timeout leaves the outcome
// unknown: never retry a different move under that cause.
func (c *Client) RequestView(ctx context.Context, request *pb.ViewRequest) (*pb.ViewRequestResult, error) {
	if request == nil || (request.Cause != 0) == (request.ToolCause != nil) {
		return nil, errors.New("view request requires exactly one cause")
	}
	if t := request.ToolCause; t != nil && (t.Tool == 0 || t.Interaction == 0) {
		return nil, errors.New("tool cause requires a tool and interaction")
	}
	hasTargets := request.T != nil || request.Z != nil || request.SelectRegion != nil || request.FrameRegion != nil || request.LookAt != nil || request.OrbitPoint != nil
	if request.Decline == hasTargets {
		return nil, errors.New("view request requires targets or a decline without targets")
	}
	request = proto.Clone(request).(*pb.ViewRequest)
	r, err := c.Call(ctx, &pb.PluginMsg{Msg: &pb.PluginMsg_ViewRequest{ViewRequest: request}})
	if err != nil {
		return nil, err
	}
	defer r.Close()
	result := r.Message.GetViewRequestResult()
	if result == nil || result.Cause != request.Cause || !proto.Equal(result.ToolCause, request.ToolCause) {
		return nil, &HostError{Kind: pb.Error_PROTOCOL, Message: "view result does not echo its cause"}
	}
	return result, nil
}
