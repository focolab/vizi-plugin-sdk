package control

import (
	"context"
	"sync"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// geometryKey is WidgetGeometry's coalescing key (§8.4): panel and widget.
type geometryKey struct{ panel, widget string }

// geometryInbox keeps the newest WidgetGeometry per widget and the order in
// which widgets first became pending. Only the worker takes pending entries;
// the routing loop's put never enters author code.
type geometryInbox struct {
	mu      sync.Mutex
	latest  map[geometryKey]*pb.WidgetGeometry // every widget heard, newest
	pending map[geometryKey]bool
	order   []geometryKey
	wake    chan struct{}
}

func (g *geometryInbox) put(m *pb.WidgetGeometry) {
	m = proto.Clone(m).(*pb.WidgetGeometry)
	key := geometryKey{m.PanelId, m.WidgetId}
	g.mu.Lock()
	g.latest[key] = m
	if !g.pending[key] {
		g.pending[key] = true
		g.order = append(g.order, key)
	}
	g.mu.Unlock()
	select {
	case g.wake <- struct{}{}:
	default:
	}
}

// WidgetGeometry is the newest WidgetGeometry heard for an image widget
// (§8.4), or false when the host has sent none this session. The result is the
// caller's own copy. It is safe to call concurrently.
func (c *Client) WidgetGeometry(panelID, widgetID string) (*pb.WidgetGeometry, bool) {
	g := c.geometries
	if g == nil {
		return nil, false
	}
	g.mu.Lock()
	defer g.mu.Unlock()
	m, ok := g.latest[geometryKey{panelID, widgetID}]
	if !ok {
		return nil, false
	}
	return proto.Clone(m).(*pb.WidgetGeometry), true
}

// geometryWorker delivers each pending widget's newest geometry to
// Config.WidgetGeometry, one widget at a time in the order they became
// pending; several changes heard while a callback runs reach it as the newest
// alone. Without the callback a geometry is only recorded: a plugin that
// ignores it is shown scaled (§8.4), which is not a fault.
func (c *Client) geometryWorker(ctx context.Context) *geometryInbox {
	g := &geometryInbox{latest: map[geometryKey]*pb.WidgetGeometry{}, pending: map[geometryKey]bool{}, wake: make(chan struct{}, 1)}
	c.geometries = g
	if c.cfg.WidgetGeometry == nil {
		return g
	}
	go func() {
		defer recoverFailure(c)
		for {
			select {
			case <-ctx.Done():
				return
			case <-g.wake:
			}
			for ctx.Err() == nil {
				g.mu.Lock()
				if len(g.order) == 0 {
					g.mu.Unlock()
					break
				}
				key := g.order[0]
				g.order = g.order[1:]
				delete(g.pending, key)
				m := proto.Clone(g.latest[key]).(*pb.WidgetGeometry)
				g.mu.Unlock()
				if err := c.cfg.WidgetGeometry(ctx, c, m); err != nil {
					c.diagnostic(err)
				}
			}
		}
	}()
	return g
}
