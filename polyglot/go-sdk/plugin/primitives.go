package plugin

import (
	"errors"
	"math"
	"unicode/utf8"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

// TextOptions controls a 3D label. Frame defaults to voxel (level-0 coordinates);
// world is also accepted. Nil ColorRGBA uses the host's opaque white.
type TextOptions struct {
	Frame     string
	SizePX    float64 // screen pixels; zero uses the host's default (12)
	ColorRGBA *[4]uint8
}

// MeshOptions controls a 3D primitive. Frame defaults to voxel (level-0
// coordinates); world is also accepted. Nil ColorRGBA uses opaque white.
type MeshOptions struct {
	Frame     string
	Size      float64 // world units: sphere radius or cube edge; zero selects 1
	ColorRGBA *[4]uint8
}

type MeshGeometry string

const (
	Sphere MeshGeometry = "sphere"
	Cube   MeshGeometry = "cube"
)

func primitiveItem(id, frame string, point geometry.ZYX, color *[4]uint8) (*pb.OverlayItem, error) {
	if frame == "" {
		frame = "voxel"
	}
	if frame != "voxel" && frame != "world" {
		return nil, errors.New("overlay frame must be voxel or world")
	}
	for _, v := range point {
		if math.IsNaN(v) || math.IsInf(v, 0) {
			return nil, errors.New("geometry coordinates must be finite")
		}
	}
	item, err := voxelItem(id)
	if err != nil {
		return nil, err
	}
	item.Transform.Frame = frame
	if color != nil {
		item.Style = &pb.OverlayStyle{ColorRgba: []uint32{uint32(color[0]), uint32(color[1]), uint32(color[2]), uint32(color[3])}}
	}
	return item, nil
}

// TextItem builds a label at anchor in the selected frame, drawn only in the
// 3D pane. The host ignores the rest of its transform (position, scale, rotation
// and scale_mode); size is always in screen pixels. Style controls color and
// depth cue. This contains no geometry byte buffer and needs no byte order.
func TextItem(id, text string, anchor geometry.ZYX, opts TextOptions) (*pb.OverlayItem, error) {
	if !utf8.ValidString(text) || len(text) > control.MaxInlineBytes {
		return nil, errors.New("overlay text must be UTF-8 within the inline byte limit")
	}
	if opts.SizePX < 0 || math.IsNaN(opts.SizePX) || math.IsInf(opts.SizePX, 0) {
		return nil, errors.New("text size must be finite and nonnegative")
	}
	item, err := primitiveItem(id, opts.Frame, anchor, opts.ColorRGBA)
	if err != nil {
		return nil, err
	}
	item.Geometry = &pb.OverlayItem_Text{Text: &pb.OverlayText{Text: text, AnchorZyx: append([]float64(nil), anchor[:]...), SizePx: opts.SizePX}}
	return item, nil
}

// MeshItem builds a sphere or cube drawn only in the 3D pane. Its center uses
// the selected frame, but size is isotropic in world units (sphere radius or
// cube edge). The host reads scale_zyx[0] and ignores rotation and scale_mode.
// Explicit vertex meshes are unsupported. No geometry byte order is needed.
func MeshItem(id string, shape MeshGeometry, position geometry.ZYX, opts MeshOptions) (*pb.OverlayItem, error) {
	if shape != Sphere && shape != Cube {
		return nil, errors.New("mesh geometry must be sphere or cube; explicit vertices are unsupported")
	}
	size := opts.Size
	if size == 0 {
		size = 1
	}
	if size < 0 || math.IsNaN(size) || math.IsInf(size, 0) {
		return nil, errors.New("mesh size must be finite and positive")
	}
	item, err := primitiveItem(id, opts.Frame, position, opts.ColorRGBA)
	if err != nil {
		return nil, err
	}
	item.Transform.PositionZyx = append([]float64(nil), position[:]...)
	item.Transform.ScaleZyx = []float64{size, size, size}
	item.Geometry = &pb.OverlayItem_Mesh{Mesh: &pb.OverlayMesh{Geometry: string(shape)}}
	return item, nil
}
