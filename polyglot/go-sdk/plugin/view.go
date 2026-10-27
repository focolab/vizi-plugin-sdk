package plugin

import (
	"context"
	"errors"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type commandCauseKey struct{}

// CommandCause returns the host's view cause for this command (also available
// in its AfterReply callback), or zero for commands without a moves_view source.
func CommandCause(ctx context.Context) uint64 {
	cause, _ := ctx.Value(commandCauseKey{}).(uint64)
	return cause
}

type ViewTargets = control.ViewTargets

// MoveView answers the current command's cause. With no cause it refuses
// locally. Answer every cause with MoveView or DeclineView, preferably within
// one second. Inspect Outcome/Reason on the reply, including ordinary refusals.
func MoveView(ctx context.Context, c *control.Client, targets ViewTargets) (*pb.ViewRequestResult, error) {
	return c.RequestView(ctx, targets.Request(CommandCause(ctx)))
}

func DeclineView(ctx context.Context, c *control.Client) (*pb.ViewRequestResult, error) {
	return c.RequestView(ctx, &pb.ViewRequest{Cause: CommandCause(ctx), Decline: true})
}

// MoveToolView answers a FIRE's (tool, interaction) cause. Call on an application
// worker with a copied Input: doorbell callbacks must not block on host calls.
// PRESS, DRAG, RELEASE and cancellation are not view causes.
func MoveToolView(ctx context.Context, c *control.Client, event doorbell.Input, targets ViewTargets) (*pb.ViewRequestResult, error) {
	return requestToolView(ctx, c, event, targets.Request(0))
}

func DeclineToolView(ctx context.Context, c *control.Client, event doorbell.Input) (*pb.ViewRequestResult, error) {
	return requestToolView(ctx, c, event, &pb.ViewRequest{Decline: true})
}

func requestToolView(ctx context.Context, c *control.Client, event doorbell.Input, request *pb.ViewRequest) (*pb.ViewRequestResult, error) {
	if event.Phase != doorbell.Fire {
		return nil, errors.New("only a FIRE tool event is a view cause")
	}
	request.ToolCause = &pb.ToolCause{Tool: event.Tool, Interaction: event.Interaction}
	return c.RequestView(ctx, request)
}
