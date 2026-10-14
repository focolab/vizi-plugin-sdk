package control

import (
	"context"
	"testing"

	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestSubscribedWorkersCoalesceWithoutBlockingHostCalls(t *testing.T) {
	for _, raw := range []bool{false, true} {
		name := "typed"
		if raw {
			name = "raw"
		}
		t.Run(name, func(t *testing.T) {
			views := make(chan *pb.ViewChanged, 2)
			regions := make(chan *pb.RegionsChanged, 2)
			release := make(chan struct{})
			viewHandler := func(ctx context.Context, c *Client, q *pb.ViewChanged) error {
				views <- q
				if q.T == 1 {
					select {
					case <-release:
					case <-ctx.Done():
						return ctx.Err()
					}
				}
				_, err := c.GetColormap(ctx, "gray")
				return err
			}
			regionHandler := func(ctx context.Context, c *Client, q *pb.RegionsChanged) error {
				regions <- q
				if q.OpenGeneration == 1 {
					select {
					case <-release:
					case <-ctx.Done():
						return ctx.Err()
					}
				}
				_, err := c.GetRois(ctx, "a", "b")
				return err
			}
			cfg := Config{ViewChanged: viewHandler, RegionsChanged: regionHandler, HandleRequest: func(context.Context, *Client, *pb.HostMsg) (*pb.PluginMsg, error) {
				return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{Ok: true}}}, nil
			}}
			if raw {
				cfg.ViewChanged, cfg.RegionsChanged = nil, nil
				cfg.OnMessage = func(ctx context.Context, c *Client, m *pb.HostMsg) error {
					if q := m.GetViewChanged(); q != nil {
						return viewHandler(ctx, c, q)
					}
					return regionHandler(ctx, c, m.GetRegionsChanged())
				}
			}
			p := openPeer(t, cfg, nil)
			sendView := func(q *pb.ViewChanged) { p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_ViewChanged{ViewChanged: q}}) }
			sendRegions := func(q *pb.RegionsChanged) { p.send(t, &pb.HostMsg{Msg: &pb.HostMsg_RegionsChanged{RegionsChanged: q}}) }
			sendView(&pb.ViewChanged{OpenGeneration: 1, T: 1})
			sendRegions(&pb.RegionsChanged{OpenGeneration: 1, Reset_: true})
			select {
			case <-views:
			case <-p.ctx.Done():
				t.Fatal(p.ctx.Err())
			}
			select {
			case <-regions:
			case <-p.ctx.Done():
				t.Fatal(p.ctx.Err())
			}
			// Both workers are parked. Arrival of the barrier reply proves all preceding
			// pushes were folded, without relying on timing or presumed scheduler speed.
			sendView(&pb.ViewChanged{OpenGeneration: 1, T: 2})
			sendView(&pb.ViewChanged{OpenGeneration: 2, T: 3, Z: 4, VisibleChannels: []uint32{1, 2}, SelectedRegionId: "b", Filter: &pb.AppliedFilter{StageId: "gain", Params: map[string]*pb.Value{"factor": {Kind: &pb.Value_F{F: 2}}}}})
			sendRegions(&pb.RegionsChanged{OpenGeneration: 1, Changes: []*pb.RegionChange{{RegionId: "discard", Revision: 7}}})
			sendRegions(&pb.RegionsChanged{OpenGeneration: 2, Reset_: true, Changes: []*pb.RegionChange{{RegionId: "a", Revision: 8}, {RegionId: "b", Revision: 9}}})
			sendRegions(&pb.RegionsChanged{OpenGeneration: 2, Changes: []*pb.RegionChange{{RegionId: "a", Revision: 10, Origin: "plugin:mine"}}})
			sendRegions(&pb.RegionsChanged{OpenGeneration: 2, Changes: []*pb.RegionChange{{RegionId: "b", Deleted: true}}})
			p.send(t, &pb.HostMsg{Id: "barrier", Msg: &pb.HostMsg_InvokeCommand{InvokeCommand: &pb.InvokeCommand{CommandId: "barrier"}}})
			if q := p.recv(t); q.Id != "barrier" || q.GetCommandDone() == nil {
				t.Fatal(q)
			}
			close(release)
			// Each worker calls the host once per callback, including its parked one.
			for range 4 {
				q := p.recv(t)
				if q.GetGetColormap() != nil {
					p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_ColormapTable{ColormapTable: &pb.ColormapTable{Name: "gray", Rgba: make([]byte, 1024)}}})
				} else if r := q.GetGetRois(); r != nil {
					if !proto.Equal(r, &pb.GetRois{RegionIds: []string{"a", "b"}}) {
						t.Fatal(r)
					}
					p.send(t, &pb.HostMsg{Id: q.Id, Msg: &pb.HostMsg_Rois{Rois: &pb.RoiList{}}})
				} else {
					t.Fatal(q)
				}
			}
			select {
			case q := <-views:
				if !proto.Equal(q, &pb.ViewChanged{OpenGeneration: 2, T: 3, Z: 4, VisibleChannels: []uint32{1, 2}, SelectedRegionId: "b", Filter: &pb.AppliedFilter{StageId: "gain", Params: map[string]*pb.Value{"factor": {Kind: &pb.Value_F{F: 2}}}}}) {
					t.Fatal(q)
				}
			case <-p.ctx.Done():
				t.Fatal(p.ctx.Err())
			}
			select {
			case q := <-regions:
				want := &pb.RegionsChanged{OpenGeneration: 2, Reset_: true, Changes: []*pb.RegionChange{{RegionId: "a", Revision: 10, Origin: "plugin:mine"}, {RegionId: "b", Deleted: true}}}
				if !proto.Equal(q, want) {
					t.Fatalf("got %v want %v", q, want)
				}
			case <-p.ctx.Done():
				t.Fatal(p.ctx.Err())
			}
		})
	}
}

func TestRegionMergeArrivalResetAndOwnership(t *testing.T) {
	box := &stateInbox{regions: true, wake: make(chan struct{}, 1)}
	put := func(generation uint64, reset bool, changes ...*pb.RegionChange) {
		box.put(&pb.HostMsg{Msg: &pb.HostMsg_RegionsChanged{RegionsChanged: &pb.RegionsChanged{OpenGeneration: generation, Reset_: reset, Changes: changes}}})
	}
	first := &pb.RegionChange{RegionId: "a", Revision: 5}
	put(1, false, first)
	first.Revision = 99
	if box.pending.GetRegionsChanged().Changes[0].Revision != 5 {
		t.Fatal("pending region aliases input")
	}
	put(1, false, &pb.RegionChange{RegionId: "a", Revision: 3}, &pb.RegionChange{RegionId: "b", Revision: 6})
	if q := box.pending.GetRegionsChanged(); q.Changes[0].Revision != 3 || len(q.Changes) != 2 {
		t.Fatal(q)
	}
	put(1, true, &pb.RegionChange{RegionId: "c", Revision: 7})
	put(1, false, &pb.RegionChange{RegionId: "d", Revision: 8})
	if q := box.pending.GetRegionsChanged(); !q.Reset_ || len(q.Changes) != 2 || q.Changes[0].RegionId != "c" {
		t.Fatal(q)
	}
	put(2, true)
	if q := box.pending.GetRegionsChanged(); !q.Reset_ || len(q.Changes) != 0 {
		t.Fatal(q)
	}
}
