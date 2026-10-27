package main

import (
	"errors"
	"fmt"
	"log"
	"math"
	"slices"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type pick struct {
	number   int
	position point
	value    float64
	t, c     uint32
	level    uint8
	refined  bool
	seconds  *float64
}

func (p pick) text(spacing point, unit string) string {
	s := fmt.Sprintf("Pick %d: (%g, %g, %g)", p.number, p.position[0], p.position[1], p.position[2])
	if unit != "" {
		s += fmt.Sprintf(" = (%g, %g, %g) %s", p.position[0]*spacing[0], p.position[1]*spacing[1], p.position[2]*spacing[2], unit)
	}
	s += fmt.Sprintf(", value %g, c=%d, t=%d", p.value, p.c, p.t)
	if p.seconds != nil {
		s += fmt.Sprintf(" (%g s)", *p.seconds)
	}
	return s
}
func asU32(c [3]int64) []uint32 { return []uint32{uint32(c[0]), uint32(c[1]), uint32(c[2])} }
func converted[T payload.Scalar](t *payload.Tensor) ([]float64, error) {
	v, err := payload.Decode[T](t)
	if err != nil {
		return nil, err
	}
	out := make([]float64, len(v))
	for i, x := range v {
		out[i] = float64(x)
	}
	return out, nil
}
func values(t *payload.Tensor) ([]float64, error) {
	switch t.DType() {
	case pb.DType_DTYPE_U8:
		return converted[uint8](t)
	case pb.DType_DTYPE_U16:
		return converted[uint16](t)
	case pb.DType_DTYPE_F32:
		return converted[float32](t)
	case pb.DType_DTYPE_F64:
		return payload.Decode[float64](t)
	}
	return nil, errors.New("Unsupported intensity dtype.")
}
func firstMax(v []float64) (int, error) {
	best := -1
	for i, x := range v {
		if !math.IsNaN(x) && (best < 0 || x > v[best]) {
			best = i
		}
	}
	if best < 0 {
		return 0, errNothing
	}
	return best, nil
}
func blockMax(v []float64, b box) (cell, float64, error) {
	i, err := firstMax(v)
	if err != nil {
		return cell{}, 0, err
	}
	nx, ny := int(b.Max[2]-b.Min[2]), int(b.Max[1]-b.Min[1])
	return cell{b.Min[0] + int64(i/(nx*ny)), b.Min[1] + int64(i/nx%ny), b.Min[2] + int64(i%nx)}, v[i], nil
}
func distances(cells []cell, spacing point) []float64 {
	d := make([]float64, len(cells))
	for i := 1; i < len(cells); i++ {
		n := 0.0
		for a := range 3 {
			v := float64(cells[i][a]-cells[i-1][a]) * spacing[a]
			n += v * v
		}
		d[i] = d[i-1] + math.Sqrt(n)
	}
	return d
}
func (a *application) read(in doorbell.Input, c, level uint32, b box) ([]float64, error) {
	if err := bounded(b); err != nil {
		return nil, err
	}
	q := &pb.ReadDataVolume{T: in.Time, C: c, Level: level, RoiMinZyx: asU32(b.Min), RoiMaxZyx: asU32(b.Max)}
	v, err := payload.ReadVolume(a.ctx, a.session.Client, a.session.Memory, q, payload.CopyOptions{MaxBytes: maxVoxels * 8, ByteOrder: payload.LittleEndian})
	if err != nil {
		return nil, err
	}
	if uint32(v.OpenGeneration) != in.Generation {
		return nil, errors.New("The dataset changed during the read; no pick made.")
	}
	shape := []uint32{uint32(b.Max[0] - b.Min[0]), uint32(b.Max[1] - b.Min[1]), uint32(b.Max[2] - b.Min[2])}
	if v.T != in.Time || v.C != c || v.Level != level || !slices.Equal(v.Tensor.Shape(), shape) {
		return nil, errors.New("Host returned mismatched read indices or shape.")
	}
	log.Printf("read t=%d c=%d level=%d roi=%v..%v", in.Time, c, level, b.Min, b.Max)
	return values(v.Tensor)
}
func (a *application) search(in doorbell.Input, pane *control.PaneState, c uint32) error {
	started := time.Now()
	g := pane.Dataset.GetGeometry()
	scale, clip, extent, err := levelGeometry(g, in.Level)
	if err != nil {
		return err
	}
	cells, err := candidates(in.Position, clip)
	if err != nil {
		return err
	}
	b := bounds(cells)
	v, err := a.read(in, c, uint32(in.Level), b)
	if err != nil {
		return err
	}
	profile := make([]float64, len(cells))
	ny, nx := b.Max[1]-b.Min[1], b.Max[2]-b.Min[2]
	for i, p := range cells {
		profile[i] = v[((p[0]-b.Min[0])*ny+p[1]-b.Min[1])*nx+p[2]-b.Min[2]]
	}
	best, err := firstMax(profile)
	if err != nil {
		return err
	}
	coarse := cells[best]
	position := level0(point{float64(coarse[0]), float64(coarse[1]), float64(coarse[2])}, scale)
	value := profile[best]
	refined := in.Level > 0 && a.refine
	reads := 1
	if refined {
		block, err := blockOf(coarse, scale, extent)
		if err != nil {
			return err
		}
		v, err := a.read(in, c, 0, block)
		if err != nil {
			return err
		}
		reads++
		fine, x, err := blockMax(v, block)
		if err != nil {
			return err
		}
		position = point{float64(fine[0]), float64(fine[1]), float64(fine[2])}
		value = x
	}
	if err := a.ctx.Err(); err != nil {
		return err
	}
	p := pick{number: len(a.picks) + 1, position: position, value: value, t: in.Time, c: c, level: in.Level, refined: refined}
	if pane.Time != nil && uint64(in.Time) < uint64(len(pane.Time.T)) {
		v := pane.Time.T[in.Time]
		p.seconds = &v
	}
	spacing, unit, known, err := geometry.PhysicalSpacing(g)
	if err != nil {
		return err
	}
	metric := point{1, 1, 1}
	axis := "distance (level voxels)"
	if known {
		for d := range 3 {
			metric[d] = spacing[d] * scale[d]
		}
		axis = "distance (" + unit + ")"
	} else {
		unit = ""
	}
	plot, err := plotProfile(profile, distances(cells, metric), best, axis)
	if err != nil {
		return err
	}
	// Publication order is SPEC §5: count, reliable marks, image, label, status.
	a.picks = append(a.picks, p)
	if err := a.showPicks(); err != nil {
		return err
	}
	a.plot = plot
	a.label = p.text(spacing, unit)
	if err := a.refresh("profile"); err != nil {
		a.session.Client.Diagnose(fmt.Errorf("profile panel update: %w", err))
	}
	a.say(fmt.Sprintf("Pick %d found value %g.", p.number, p.value))
	log.Printf("pick=%d level=%d refined=%t reads=%d elapsed=%s", p.number, in.Level, refined, reads, time.Since(started))
	return nil
}
