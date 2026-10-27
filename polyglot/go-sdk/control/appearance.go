package control

import (
	"context"
	"sync"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// appearanceInbox keeps the newest HostAppearance. Its coalescing key is the
// session itself (§5.4), so there is one slot: a push heard while the
// callback runs replaces any still pending, and the callback reads the newest
// when it starts. Only the worker takes the pending flag; the routing loop's
// put never enters author code.
type appearanceInbox struct {
	mu      sync.Mutex
	latest  *pb.HostAppearance // nil until the first push
	pending bool
	wake    chan struct{}
}

func (a *appearanceInbox) put(m *pb.HostAppearance) {
	m = proto.Clone(m).(*pb.HostAppearance)
	a.mu.Lock()
	a.latest = m
	a.pending = true
	a.mu.Unlock()
	select {
	case a.wake <- struct{}{}:
	default:
	}
}

// Appearance is the newest HostAppearance heard (§8.4): the host's theme and
// the colors it draws panels with. It is false before the first push, and
// from a host that sends none. The host sends it once after Ready, before any
// panel opens, so a PanelOpened handler normally finds it here. The result is
// the caller's own copy. It is safe to call concurrently.
func (c *Client) Appearance() (*pb.HostAppearance, bool) {
	a := c.appearance
	if a == nil {
		return nil, false
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.latest == nil {
		return nil, false
	}
	return proto.Clone(a.latest).(*pb.HostAppearance), true
}

// appearanceWorker delivers the newest appearance to Config.Appearance on its
// own worker; several pushes heard while a callback runs reach it as the
// newest alone. Without the callback an appearance is only recorded, for
// Client.Appearance: a plugin that ignores it draws as it did (§8.4).
func (c *Client) appearanceWorker(ctx context.Context) *appearanceInbox {
	a := &appearanceInbox{wake: make(chan struct{}, 1)}
	c.appearance = a
	if c.cfg.Appearance == nil {
		return a
	}
	go func() {
		defer recoverFailure(c)
		for {
			select {
			case <-ctx.Done():
				return
			case <-a.wake:
			}
			a.mu.Lock()
			if !a.pending || ctx.Err() != nil {
				a.mu.Unlock()
				continue
			}
			a.pending = false
			m := proto.Clone(a.latest).(*pb.HostAppearance)
			a.mu.Unlock()
			if err := c.cfg.Appearance(ctx, c, m); err != nil {
				c.diagnostic(err)
			}
		}
	}()
	return a
}
