package plugin

import (
	"maps"
	"math"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"google.golang.org/protobuf/proto"
)

func TestLabelVolumeValidationAndSnapshot(t *testing.T) {
	tensor, _ := payload.Encode([]uint16{1, 2}, []uint32{1, 1, 2}, true)
	v := LabelVolume{ID: "labels", Labels: tensor, PositionZYX: geometry.ZYX{1, 2, 3}, T: proto.Uint32(4), Names: map[uint32]string{1: "first"}, Visible: map[uint32]bool{1: true, 256: false}}
	item, err := v.item(true)
	if err != nil {
		t.Fatal(err)
	}
	*v.T = 9
	v.Names[1] = "changed"
	v.Visible[1] = false
	delete(v.Visible, 256)
	v.Visible[2] = true
	if !maps.Equal(item.GetLabelVolume().LabelVisible, map[uint32]bool{1: true, 256: false}) {
		t.Fatal("visibility lost or aliases caller", item)
	}
	if item.GetLabelVolume().GetT() != 4 || item.GetLabelVolume().LabelNames[1] != "first" {
		t.Fatal("metadata aliases caller")
	}
	for _, change := range []func(*LabelVolume){func(v *LabelVolume) { v.ID = "" }, func(v *LabelVolume) { v.Labels = nil }, func(v *LabelVolume) { v.Labels, _ = payload.Encode([]float32{1}, []uint32{1, 1, 1}, true) }, func(v *LabelVolume) { v.Labels, _ = payload.Encode([]uint8{1}, []uint32{1, 1}, true) }, func(v *LabelVolume) { v.PositionZYX[0] = math.NaN() }} {
		bad := v
		change(&bad)
		if _, err := bad.item(false); err == nil {
			t.Fatal("invalid labels accepted")
		}
	}
	v.PositionZYX[0] = .5
	if _, err := v.item(false); err != nil {
		t.Fatal("fractional display position refused", err)
	}
	if _, err := v.item(true); err == nil {
		t.Fatal("fractional compile position accepted")
	}
}
