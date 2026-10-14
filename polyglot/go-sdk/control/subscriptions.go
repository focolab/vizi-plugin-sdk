package control

import (
	"context"
	"fmt"
	"sync"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

// One pending snapshot per subscription, independent of the callback in flight.
// Only a worker takes pending; receive-order puts never enter author code.
type stateInbox struct {
	mu      sync.Mutex
	pending *pb.HostMsg
	wake    chan struct{}
	regions bool
}

func (s *stateInbox) put(m *pb.HostMsg) {
	m = proto.Clone(m).(*pb.HostMsg)
	s.mu.Lock()
	if s.regions && s.pending != nil {
		m.Msg = &pb.HostMsg_RegionsChanged{RegionsChanged: mergeRegions(s.pending.GetRegionsChanged(), m.GetRegionsChanged())}
	}
	s.pending = m
	s.mu.Unlock()
	select {
	case s.wake <- struct{}{}:
	default:
	}
}

func mergeRegions(old, next *pb.RegionsChanged) *pb.RegionsChanged {
	if next.Reset_ || old.OpenGeneration != next.OpenGeneration {
		return next
	}
	next.Reset_ = old.Reset_
	indices := make(map[string]int, len(old.Changes))
	changes := old.Changes
	for i, change := range changes {
		indices[change.RegionId] = i
	}
	for _, change := range next.Changes {
		if i, ok := indices[change.RegionId]; ok {
			// Arrival order is authoritative, including revision-zero deletions.
			changes[i] = change
		} else {
			indices[change.RegionId] = len(changes)
			changes = append(changes, change)
		}
	}
	next.Changes = changes
	return next
}

func (c *Client) stateWorker(ctx context.Context, regions bool) *stateInbox {
	s := &stateInbox{wake: make(chan struct{}, 1), regions: regions}
	go func() {
		defer recoverFailure(c)
		for {
			select {
			case <-ctx.Done():
				return
			case <-s.wake:
			}
			if ctx.Err() != nil {
				return
			}
			s.mu.Lock()
			m := s.pending
			s.pending = nil
			s.mu.Unlock()
			if m == nil {
				continue
			}
			var err error
			switch {
			case regions && c.cfg.RegionsChanged != nil:
				err = c.cfg.RegionsChanged(ctx, c, m.GetRegionsChanged())
			case !regions && c.cfg.ViewChanged != nil:
				err = c.cfg.ViewChanged(ctx, c, m.GetViewChanged())
			case c.cfg.OnMessage != nil:
				err = c.cfg.OnMessage(ctx, c, m)
			default:
				err = fmt.Errorf("unhandled host %s", kind(m.ProtoReflect()))
			}
			if err != nil {
				c.diagnostic(err)
			}
		}
	}()
	return s
}
