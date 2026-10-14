package main

import (
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
	"testing"
)

func TestGestureFirstUsableContextAndMissAnchor(t *testing.T) {
	g := &gesture{pair: memory.PaneEpoch{ViewID: 0, Epoch: 7}}
	in := doorbell.Input{Time: 2, Generation: 10, Channels: 1, Level: 3, Position: doorbell.Position{Kind: doorbell.Voxel, Values: [12]float32{8, 2, 3}}}
	g.take(in, false, nil)
	if g.contextSet || g.anchorSet || len(g.path) != 0 {
		t.Fatal("unusable press contributed")
	}
	in.Level = 0
	in.Position.Flags = doorbell.Miss
	in.Position.Values[0] = 6.5
	g.take(in, true, nil)
	if !g.contextSet || g.t != 2 || g.gen != 10 || g.z != 6 || !g.anchorSet || len(g.path) != 0 {
		t.Fatalf("miss context: %+v", g)
	}
	in.Time = 9
	in.Generation = 99
	in.Channels = 3
	in.Level = 1
	in.Position.Flags = 0
	in.Position.Values = [12]float32{3, 4, 5}
	g.take(in, true, nil)
	if g.t != 2 || g.gen != 10 || g.channel != 0 || g.level != 0 || g.z != 6 || len(g.path) != 1 || g.refusal != "" {
		t.Fatalf("later context replaced captured context: %+v", g)
	}
	item, err := trailItem(g)
	if err != nil {
		t.Fatal(err)
	}
	if item.GetMarker().SizePx != 5 || item.Transform.Frame != "voxel" {
		t.Fatal(item)
	}
}
func TestHighChannelsAndProjection(t *testing.T) {
	in := doorbell.Input{Channels: 1 << 15, Position: doorbell.Position{Kind: doorbell.Voxel}}
	pane := &control.PaneState{Pane: &pb.PaneEpoch{Epoch: 7, ChannelsHigh: []uint32{23}}, Dataset: &pb.DatasetState{Geometry: &pb.Geometry{Extent: &pb.Extent{ZN: 12, YN: 48, XN: 64}}}}
	g := &gesture{pair: memory.PaneEpoch{Epoch: 7}}
	g.take(in, true, pane)
	if g.channel != 23 || g.refusal != "" || g.shape != [3]int{12, 48, 64} {
		t.Fatalf("high channel resolution: %+v", g)
	}
	missing := &gesture{}
	missing.take(in, true, nil)
	if missing.refusal != msgChannels {
		t.Fatal(missing)
	}
	in.Position.Kind = doorbell.ColumnYX
	g.take(in, true, pane)
	if g.refusal != msgMIP {
		t.Fatal(g)
	}
}
func TestLongTrailFitsGrantedFixtureSlot(t *testing.T) {
	g := &gesture{z: 6, path: make([]point, 10001)}
	for i := range g.path {
		g.path[i] = point{float64(i % 48), float64(i % 64)}
	}
	item, err := trailItem(g)
	if err != nil {
		t.Fatal(err)
	}
	update := &pb.OverlayUpdate{OverlayId: "seg.trail", Revision: 10001, Items: []*pb.OverlayItem{item}}
	size := proto.Size(update)
	if item.GetMarker().PositionsZyx.Shape[0] != 512 || size > 16384-8 || len(g.path) != 10001 {
		t.Fatalf("size=%d path=%d", size, len(g.path))
	}
	t.Logf("10,001 full points -> 512 preview points -> %d serialized bytes / 16,376 granted bytes", size)
}
