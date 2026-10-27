package main

import (
	"errors"
	"fmt"
	"slices"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/geometry"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

const maxReadVoxels = 16_000_000

var copyOptions = payload.CopyOptions{MaxBytes: maxReadVoxels * 8, ByteOrder: payload.LittleEndian}

func u32(v [3]int) []uint32 { return []uint32{uint32(v[0]), uint32(v[1]), uint32(v[2])} }
func convert[T payload.Scalar](t *payload.Tensor) ([]float64, error) {
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
		return convert[uint8](t)
	case pb.DType_DTYPE_U16:
		return convert[uint16](t)
	case pb.DType_DTYPE_F32:
		return convert[float32](t)
	case pb.DType_DTYPE_F64:
		return payload.Decode[float64](t)
	}
	return nil, errors.New("Unsupported intensity dtype.")
}
func (a *application) read(t, c uint32, w box) (*payload.Volume, error) {
	n := uint64(1)
	for _, v := range w.shape() {
		if v <= 0 || uint64(v) > maxReadVoxels/n {
			return nil, errors.New("Search window exceeds the 16-million-voxel application budget.")
		}
		n *= uint64(v)
	}
	v, err := payload.ReadVolume(a.ctx, a.session.Client, a.session.Memory, &pb.ReadDataVolume{T: t, C: c, Level: 0, RoiMinZyx: u32(w.lo), RoiMaxZyx: u32(w.hi)}, copyOptions)
	if err != nil {
		return nil, err
	}
	if v.T != t || v.C != c || v.Level != 0 || !slices.Equal(v.Tensor.Shape(), u32(w.shape())) {
		return nil, errors.New("Host returned mismatched volume indices or shape.")
	}
	return v, nil
}
func (a *application) segment(g *gesture) error {
	a.mu.Lock()
	cfg := a.settings
	a.mu.Unlock()
	if g.shape == [3]int{} {
		v, err := a.session.Client.GetViewState(a.ctx)
		if err != nil {
			return err
		}
		if len(v.ShapeTczyx) != 5 {
			return errors.New("Dataset shape unavailable.")
		}
		g.shape = [3]int{int(v.ShapeTczyx[2]), int(v.ShapeTczyx[3]), int(v.ShapeTczyx[4])}
	}
	w, selected, err := selection(g.path, g.shape)
	if err != nil {
		return err
	}
	a.say("Reading selection…")
	v, err := a.read(g.t, g.channel, w)
	if err != nil {
		return err
	}
	if uint32(v.OpenGeneration) != g.gen {
		return errors.New("Dataset changed during the gesture; draw again.")
	}
	data, err := values(v.Tensor)
	if err != nil {
		return err
	}
	if err = a.ctx.Err(); err != nil {
		return err
	}
	result, err := grow(data, w, g.shape, selected, g.z, cfg.minSize, cfg.scale, cfg.grow3D)
	if err != nil {
		return err
	}
	if err = a.ctx.Err(); err != nil {
		return err
	}
	// Resolve accumulation here, after the expensive work: reset can occur while
	// the read/grow is in flight and the result then starts a fresh label 1.
	a.mu.Lock()
	defer a.mu.Unlock()
	sc := scope{g.t, g.channel, v.OpenGeneration}
	changed := len(a.labels.voxels) > 0 && a.scope != sc
	base := a.labels
	if changed {
		base = labelMap{}
	}
	next, id, err := base.adding(result, cfg.mode == "Replace")
	if err != nil {
		return err
	}
	item, err := labelItem(next, sc.t)
	if err != nil {
		return err
	}
	err = a.labelOverlay.ReplaceLabelVolume(a.session, item)
	if err != nil {
		return err
	}
	a.labels, a.scope = next, sc
	if a.panelOpen.Load() {
		if err = a.panel.SetEnabled("commit", true); err != nil {
			return err
		}
	}
	status := fmt.Sprintf("Label %d: %d voxels; threshold %.6g; not clipped.", id, len(result.voxels), result.threshold)
	if result.clipped {
		status = fmt.Sprintf("Label %d: %d voxels; threshold %.6g; clipped by the search window.", id, len(result.voxels), result.threshold)
	}
	if changed {
		status = "New timepoint, channel or dataset: accumulation restarted. " + status
	}
	a.say(status)
	return nil
}

// Retain task data, not shared-memory descriptors. The SDK reserves a fresh
// pair of one-use loans for every push and compile.
func labelItem(labels labelMap, t uint32) (plugin.LabelVolume, error) {
	data, w, names, err := labels.render()
	if err != nil {
		return plugin.LabelVolume{}, err
	}
	tensor, err := payload.New(data, u32(w.shape()), pb.DType_DTYPE_U8, true)
	if err != nil {
		return plugin.LabelVolume{}, err
	}
	var lut [256][4]uint8
	colors := labelLUT()
	for i := range lut {
		copy(lut[i][:], colors[i*4:(i+1)*4])
	}
	return plugin.LabelVolume{ID: "labels", Labels: tensor, Colormap: lut, PositionZYX: geometry.ZYX{float64(w.lo[0]), float64(w.lo[1]), float64(w.lo[2])}, T: proto.Uint32(t), Names: names}, nil
}
func (a *application) removeLabelsLocked() error { return a.labelOverlay.Clear() }
func (a *application) commit() error {
	a.mu.Lock()
	item, err := labelItem(a.labels, a.scope.t)
	a.mu.Unlock()
	if err != nil {
		return err
	}
	reply, err := a.labelOverlay.CompileLabelVolume(a.ctx, a.session, item, true)
	if err != nil {
		return err
	}
	a.mu.Lock()
	err = a.removeLabelsLocked()
	a.mu.Unlock()
	if err != nil {
		return err
	}
	if err = a.refreshRegions(); err != nil {
		return err
	}
	a.say(fmt.Sprintf("Saved %d regions to annotations.", reply.RegionCount))
	return nil
}
func (a *application) refreshRegions() error {
	reply, err := a.session.Client.GetRois(a.ctx)
	if err != nil {
		return err
	}
	var regions []*pb.RoiRecord
	for _, r := range reply.Rois {
		if r.Origin == "plugin:org.example.interactive_seg" {
			regions = append(regions, r)
		}
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.regions = regions
	a.region = 0
	return a.refreshPanelLocked()
}
func (a *application) measure() error {
	a.mu.Lock()
	i := a.region
	var id string
	if i >= 0 && i < len(a.regions) {
		id = a.regions[i].RegionId
	}
	a.mu.Unlock()
	if id == "" {
		if err := a.refreshRegions(); err != nil {
			return err
		}
		return errors.New("Selected region is stale; refreshed regions. Select a region again.")
	}
	mask, err := payload.ReadMask(a.ctx, a.session.Client, a.session.Memory, &pb.ReadMaskVoxels{RegionId: id}, copyOptions)
	if err != nil {
		refreshErr := a.refreshRegions()
		return errors.Join(fmt.Errorf("Could not read selected region; refreshed regions: %w", err), refreshErr)
	}
	if mask.RegionID != id || mask.Tensor.DType() != pb.DType_DTYPE_U8 || len(mask.Tensor.Shape()) != 3 {
		return errors.New("Invalid mask reply.")
	}
	membership, err := payload.Decode[uint8](mask.Tensor)
	if err != nil {
		return err
	}
	count := 0
	for _, v := range membership {
		if v != 0 {
			count++
		}
	}
	if count == 0 {
		return errors.New("Selected mask is empty.")
	}
	view, err := a.session.Client.GetViewState(a.ctx)
	if err != nil {
		return err
	}
	if len(view.ShapeTczyx) != 5 || view.OpenGeneration != mask.OpenGeneration {
		return errors.New("Dataset changed while measuring.")
	}
	channel := uint32(0)
	if len(view.VisibleChannels) > 0 {
		channel = view.VisibleChannels[0]
	}
	var w box
	for j, n := range mask.Tensor.Shape() {
		w.lo[j] = int(mask.OriginZYX[j])
		w.hi[j] = w.lo[j] + int(n)
	}
	if view.ShapeTczyx[0] > 1_000_000 {
		return errors.New("Too many timepoints to plot.")
	}
	means := make([]float64, int(view.ShapeTczyx[0]))
	for t := range means {
		a.say(fmt.Sprintf("Measuring %d / %d", t+1, len(means)))
		v, err := a.read(uint32(t), channel, w)
		if err != nil {
			return err
		}
		if v.OpenGeneration != mask.OpenGeneration {
			return errors.New("Dataset changed while measuring.")
		}
		data, err := values(v.Tensor)
		if err != nil {
			return err
		}
		sum := 0.
		for j, on := range membership {
			if on != 0 {
				sum += data[j]
			}
		}
		means[t] = sum / float64(count)
	}
	png, err := plotMeans(means)
	if err != nil {
		return err
	}
	// Serialize model publication with PanelOpened so an opening panel cannot
	// miss a result computed before its first snapshot.
	a.mu.Lock()
	a.plot = png
	if a.panelOpen.Load() {
		err = a.panel.SetPNG(a.session, 0, "plot", png)
	}
	a.mu.Unlock()
	if err != nil {
		return err
	}
	a.say(fmt.Sprintf("Measured %d voxels across %d timepoints.", count, len(means)))
	return nil
}
