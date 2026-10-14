package control

import (
	"context"
	"errors"
	"math"
	"sync"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type arenaKey struct {
	segment uint64
	region  uint32
}
type shortfallEntry struct {
	snapshot memory.Shortfall
	dropped  uint64
}

// Shortfalls merges allocation failures without flooding the reliable channel.
// Local policy is one report per 100 ms, up to 1024 pending arena keys. The
// largest-free snapshot stays paired with the largest failed allocation. A tie
// retains the held observation, as with the sender coalescer.
type Shortfalls struct {
	mu      sync.Mutex
	entries map[arenaKey]*shortfallEntry
	order   []arenaKey
}

func NewShortfalls() *Shortfalls { return &Shortfalls{entries: make(map[arenaKey]*shortfallEntry)} }
func (s *Shortfalls) Observe(f memory.Shortfall) error {
	if f.SegmentID == 0 || f.WantedBytes == 0 {
		return errors.New("shortfall requires nonzero segment and wanted bytes")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	key := arenaKey{f.SegmentID, f.Region}
	e := s.entries[key]
	if e == nil {
		if len(s.entries) >= 1024 {
			return ErrQueueFull
		}
		e = &shortfallEntry{snapshot: f}
		s.entries[key] = e
		s.order = append(s.order, key)
	}
	if e.dropped == math.MaxUint64 {
		return errors.New("shortfall count exhausted")
	}
	if f.WantedBytes > e.snapshot.WantedBytes {
		e.snapshot = f
	}
	e.dropped++
	return nil
}
func (s *Shortfalls) next() *pb.PluginMsg {
	s.mu.Lock()
	defer s.mu.Unlock()
	if len(s.order) == 0 {
		return nil
	}
	key := s.order[0]
	s.order = s.order[1:]
	e := s.entries[key]
	n := min(e.dropped, uint64(math.MaxUint32))
	e.dropped -= n
	if e.dropped == 0 {
		delete(s.entries, key)
	} else {
		s.order = append(s.order, key)
	}
	f := e.snapshot
	return &pb.PluginMsg{Msg: &pb.PluginMsg_SegmentShortfall{SegmentShortfall: &pb.SegmentShortfall{SegmentId: f.SegmentID, Region: f.Region, WantedBytes: f.WantedBytes, LargestFreeBytes: f.LargestFreeBytes, Dropped: uint32(n)}}}
}
func (s *Shortfalls) Run(ctx context.Context, c *Client) error {
	tick := time.NewTicker(100 * time.Millisecond)
	defer tick.Stop()
	var pending *pb.PluginMsg
	for {
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-tick.C:
			if pending == nil {
				pending = s.next()
			}
			if pending != nil {
				if err := c.Send(pending); errors.Is(err, ErrQueueFull) {
					continue // not accepted; retain counts and retry after the writer drains
				} else if err != nil {
					return err
				}
				pending = nil
			}
		}
	}
}
