package control

import (
	"context"
	"errors"
	"strings"
	"sync"
	"testing"
	"time"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// checkSessionEnded asserts the error a plugin request sees once its session
// has ended: a *HostError of kind KIND_UNSPECIFIED whose message starts with
// prefix, matching ErrSessionEnded.
func checkSessionEnded(t *testing.T, err error, prefix string) {
	t.Helper()
	var host *HostError
	var ended *SessionEnded
	if !errors.As(err, &ended) || !errors.As(err, &host) || host.Kind != pb.Error_KIND_UNSPECIFIED || !strings.HasPrefix(host.Message, prefix) || !errors.Is(err, ErrSessionEnded) {
		t.Fatalf("want SessionEnded, a HostError(KIND_UNSPECIFIED, %q...), got %T %v", prefix, err, err)
	}
	if errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("a session end is not a timeout: %v", err)
	}
}

// §3.3: once the session ends the host answers no plugin request. A request
// pending then fails at once with HostError, not at its 30 s timeout, and a
// request made later fails the same way without being sent.
func TestARequestPendingWhenTheSessionEndsFailsAtOnce(t *testing.T) {
	cases := []struct {
		name   string
		end    func(t *testing.T, p *peer)
		reason string
	}{
		{"shutdown", func(t *testing.T, p *peer) {
			p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_Shutdown{Shutdown: &pb.Shutdown{Reason: pb.ShutdownReason_SHUTDOWN_REASON_HOST_EXIT, DeadlineMs: proto.Uint32(3000)}}})
		}, "shutdown began (SHUTDOWN_REASON_HOST_EXIT)"},
		{"stream end", func(t *testing.T, p *peer) {
			if err := p.stream.CloseSend(); err != nil {
				t.Fatal(err)
			}
		}, "the host closed its side of the stream"},
		{"fault", func(t *testing.T, p *peer) {
			p.client.Fail(errors.New("a test fault"))
		}, "a test fault"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var lock sync.Mutex
			hookErr := make(chan error, 1)
			p := openPeer(t, Config{Shutdown: func(ctx context.Context, c *Client, _ *pb.Shutdown) error {
				lock.Lock() // the caller's lock: free only once its pending request has failed
				lock.Unlock()
				_, err := c.GetViewState(ctx)
				hookErr <- err
				return nil
			}}, nil)
			failed := make(chan error, 1)
			go func() {
				lock.Lock()
				defer lock.Unlock()
				_, err := p.client.GetViewState(context.Background()) // never answered
				failed <- err
			}()
			if q := p.recv(t); q.GetGetViewState() == nil || q.Id == "" {
				t.Fatalf("expected the pending request, got %v", q)
			}
			start := time.Now()
			tc.end(t, p)
			select {
			case err := <-failed:
				checkSessionEnded(t, err, "the session ended: "+tc.reason)
			case <-time.After(5 * time.Second):
				t.Fatal("the pending request waited past the session's end")
			}
			if waited := time.Since(start); waited > time.Second {
				t.Fatalf("the pending request failed after %v", waited)
			}
			_, err := p.client.GetViewState(context.Background())
			checkSessionEnded(t, err, "the session has ended: "+tc.reason)
			if tc.name != "shutdown" {
				return
			}
			select {
			case err := <-hookErr:
				checkSessionEnded(t, err, "the session has ended: ")
				if !errors.Is(err, ErrShuttingDown) {
					t.Fatalf("a request in the shutdown window should match ErrShuttingDown: %v", err)
				}
			case <-time.After(time.Second):
				t.Fatal("the hook waited on the pending request's lock")
			}
			// Nothing the hook asked for was sent: the report is next.
			if m := p.recv(t); m.GetShutdownReport().GetOutcome() != pb.ShutdownReport_COMPLETED {
				t.Fatalf("want a COMPLETED report next, got %v", m)
			}
		})
	}
}
