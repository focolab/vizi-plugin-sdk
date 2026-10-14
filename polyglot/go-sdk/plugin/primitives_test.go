package plugin

import (
	"math"
	"testing"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

func TestPrimitiveContracts(t *testing.T) {
	color := [4]uint8{10, 20, 30, 255}
	text, err := TextItem("label", "pick", geometry.ZYX{1, 2, 3}, TextOptions{ColorRGBA: &color})
	if err != nil {
		t.Fatal(err)
	}
	color[0] = 99
	wantText := &pb.OverlayItem{ItemId: "label", Transform: &pb.OverlayTransform{Frame: "voxel"}, Style: &pb.OverlayStyle{ColorRgba: []uint32{10, 20, 30, 255}}, Geometry: &pb.OverlayItem_Text{Text: &pb.OverlayText{Text: "pick", AnchorZyx: []float64{1, 2, 3}}}}
	if !proto.Equal(text, wantText) {
		t.Fatal("text must use anchor, default screen size, and copied style", text)
	}
	cube, err := MeshItem("box", Cube, geometry.ZYX{4, 5, 6}, MeshOptions{Frame: "world"})
	if err != nil {
		t.Fatal(err)
	}
	wantCube := &pb.OverlayItem{ItemId: "box", Transform: &pb.OverlayTransform{Frame: "world", PositionZyx: []float64{4, 5, 6}, ScaleZyx: []float64{1, 1, 1}}, Geometry: &pb.OverlayItem_Mesh{Mesh: &pb.OverlayMesh{Geometry: "cube"}}}
	if !proto.Equal(cube, wantCube) {
		t.Fatal("cube must use its centre and unit edge", cube)
	}
	for _, size := range []float64{-1, math.NaN(), math.Inf(1)} {
		if _, err := TextItem("t", "text", geometry.ZYX{}, TextOptions{SizePX: size}); err == nil {
			t.Fatal("accepted invalid text size", size)
		}
		if _, err := MeshItem("m", Sphere, geometry.ZYX{}, MeshOptions{Size: size}); err == nil {
			t.Fatal("accepted invalid mesh size", size)
		}
	}
	if _, err := MeshItem("m", "explicit", geometry.ZYX{}, MeshOptions{}); err == nil {
		t.Fatal("accepted unsupported explicit mesh")
	}
	if _, err := TextItem("t", "text", geometry.ZYX{}, TextOptions{Frame: "screen"}); err == nil {
		t.Fatal("accepted unknown coordinate frame")
	}
	if _, err := MeshItem("m", Sphere, geometry.ZYX{math.NaN()}, MeshOptions{}); err == nil {
		t.Fatal("accepted nonfinite position")
	}
}
