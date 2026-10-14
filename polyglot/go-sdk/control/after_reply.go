package control

import (
	"context"
	"errors"
	"fmt"
	"sync"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type afterReplyKey struct{}
type afterReplyScope struct {
	mu     sync.Mutex
	client *Client
	closed bool
	work   func(context.Context, *Client) error
}

// AfterReply registers one continuation on the current request handler context.
// It runs on a separate worker only after a successful reply has been sent by
// gRPC. Requests and pushes made by work therefore cannot overtake the reply.
// Failed/replaced replies, admission/send failures and session cancellation
// discard the continuation. Registration is bounded by MaxPendingRequests;
// exhaustion returns ErrQueueFull before the handler acknowledges acceptance.
//
// Call synchronously inside HandleRequest, not from a spawned goroutine. The
// continuation receives the session context, must honor cancellation, and owns
// any application cleanup/reporting. Returned errors (except ErrCancelled or
// HostError{CANCELLED}) and panics are diagnostics;
// they cannot change a reply already sent. Shutdown hooks must join application
// work when its resources require cleanup before the shutdown report.
func AfterReply(ctx context.Context, work func(context.Context, *Client) error) error {
	scope, _ := ctx.Value(afterReplyKey{}).(*afterReplyScope)
	if scope == nil || work == nil {
		return errors.New("AfterReply requires a request handler context and nonnil work")
	}
	scope.mu.Lock()
	defer scope.mu.Unlock()
	if scope.closed || scope.work != nil {
		return errors.New("AfterReply requires an active request with no registered continuation")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case scope.client.afterReplySlots <- struct{}{}:
		scope.work = work
		return nil
	default:
		return ErrQueueFull
	}
}

func (s *afterReplyScope) finish() func(context.Context, *Client) error {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.closed = true
	return s.work
}

func successfulReply(out *pb.PluginMsg) bool {
	if out == nil || out.GetError() != nil {
		return false
	}
	if done := out.GetCommandDone(); done != nil {
		return done.Ok
	}
	return true
}

func (c *Client) continueAfterReply(sent <-chan error, work func(context.Context, *Client) error) {
	defer func() {
		<-c.afterReplySlots
		if p := recover(); p != nil {
			c.diagnostic(fmt.Errorf("after-reply callback panic: %v", p))
		}
	}()
	select {
	case err := <-sent:
		if err != nil {
			return
		}
	case <-c.ctx.Done():
		return
	}
	if c.ctx.Err() != nil {
		return
	}
	if err := work(c.ctx, c); err != nil && !isWorkCancelled(err) {
		c.diagnostic(fmt.Errorf("after-reply callback: %w", err))
	}
}
