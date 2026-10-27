package main

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"math"
	"os"
	"slices"
	"strings"
	"sync/atomic"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type application struct {
	traces           *traceWorker
	ready            chan struct{}
	session          *control.Session // published by closing ready before any callback uses it
	panel            plugin.PanelRef
	gate             chan struct{} // all model changes, measuring and panel publication
	rows             map[string]row
	t, c             uint32
	selected         string
	haveDataset      bool
	generation       uint64
	frames, channels uint32
	dtype            pb.DType
	unsupported      atomic.Bool
	colormap         []byte
	pngs             map[int][]byte
	tourID           uint64
	lastTourRow      row
	haveTourRow      bool
}

func (a *application) lock(ctx context.Context) error {
	select {
	case <-a.ready:
	case <-ctx.Done():
		return ctx.Err()
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	select {
	case a.gate <- struct{}{}:
		if err := ctx.Err(); err != nil {
			a.unlock()
			return err
		}
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (a *application) unlock()              { <-a.gate }
func (a *application) handle() plugin.Panel { return a.panel.Bind(a.session.Client) }

const floatStatus = "Region Quant measures integer data; this dataset is f32"
const maxRows = 1024

func (a *application) status(text string) error {
	if a.unsupported.Load() && text != "No dataset is open" {
		text = floatStatus
	}
	return a.handle().SetValue("status", plugin.String(text))
}

func (a *application) measure(ctx context.Context, id string, t, c uint32) (measurement, error) {
	if t > math.MaxInt32 {
		return measurement{}, fmt.Errorf("frame exceeds inclusion API range")
	}
	opts := payload.CopyOptions{MaxBytes: 512 << 20, ByteOrder: payload.LittleEndian}
	mask, err := payload.ReadInclusion(ctx, a.session.Client, a.session.Memory, &pb.ReadRoiInclusion{RegionId: id, T: int32(t)}, opts)
	if err != nil {
		return measurement{}, err
	}
	if mask.Tensor == nil {
		return measurement{}, nil
	}
	shape := mask.Tensor.Shape()
	lo := mask.OriginZYX[:]
	hi := make([]uint32, 3)
	for i := range 3 {
		if shape[i] > math.MaxUint32-lo[i] {
			return measurement{}, fmt.Errorf("mask box overflow")
		}
		hi[i] = lo[i] + shape[i]
	}
	vol, err := payload.ReadVolume(ctx, a.session.Client, a.session.Memory, &pb.ReadDataVolume{T: t, C: c, RoiMinZyx: lo, RoiMaxZyx: hi}, opts)
	if err != nil {
		return measurement{}, err
	}
	if vol.OpenGeneration != mask.OpenGeneration || vol.T != t || vol.C != c || !slices.Equal(vol.Tensor.Shape(), shape) {
		return measurement{}, fmt.Errorf("inclusion and volume disagree")
	}
	membership, err := payload.Decode[uint8](mask.Tensor)
	if err != nil {
		return measurement{}, err
	}
	var result measurement
	add := func(i int, v uint64) error {
		if membership[i] == 0 {
			return nil
		}
		if v > math.MaxUint64-result.sum {
			return fmt.Errorf("integer sum overflow")
		}
		result.count++
		result.sum += v
		return nil
	}
	switch vol.Tensor.DType() {
	case pb.DType_DTYPE_U8:
		values, err := payload.Decode[uint8](vol.Tensor)
		if err != nil {
			return result, err
		}
		for i, v := range values {
			if err := add(i, uint64(v)); err != nil {
				return result, err
			}
		}
	case pb.DType_DTYPE_U16:
		values, err := payload.Decode[uint16](vol.Tensor)
		if err != nil {
			return result, err
		}
		for i, v := range values {
			if err := add(i, uint64(v)); err != nil {
				return result, err
			}
		}
	default:
		return result, fmt.Errorf("region-quant requires unsigned integer voxels, got %s", vol.Tensor.DType())
	}
	return result, nil
}
func (a *application) remeasure(ctx context.Context, rows map[string]row, t, c uint32) error {
	if a.unsupported.Load() {
		for id, r := range rows {
			r.measurement, r.unmeasured = measurement{}, true
			rows[id] = r
		}
		return nil
	}
	if err := measureRows(ctx, rows, t, c, a.measure); err != nil {
		if errors.Is(err, control.ErrSessionEnded) {
			return err // the session ended: no status for a panel that is going away, and the SDK logs nothing
		}
		return a.status("Measuring failed: " + errorMessage(err))
	}
	return nil
}

func measureRows(ctx context.Context, rows map[string]row, t, c uint32, measure func(context.Context, string, uint32, uint32) (measurement, error)) error {
	var failed error
	for id, r := range rows {
		m, err := measure(ctx, id, t, c)
		if regionMissing(err) {
			delete(rows, id)
			continue
		}
		if err != nil {
			failed = err
			continue
		}
		r.measurement, r.unmeasured = m, false
		rows[id] = r
	}
	return failed
}
func errorMessage(err error) string {
	var host *control.HostError
	if errors.As(err, &host) {
		return host.Message
	}
	return err.Error()
}
func regionMissing(err error) bool {
	var host *control.HostError
	return errors.As(err, &host) && host.Kind == pb.Error_NOT_FOUND
}

// Called under the model lock before processing either subscription push.
func (a *application) datasetOpened(ctx context.Context, generation uint64) (bool, error) {
	if generation == 0 || (a.haveDataset && generation == a.generation) {
		return false, nil
	}
	view, err := a.session.Client.GetViewState(ctx)
	if err != nil {
		return false, err
	}
	if len(view.ShapeTczyx) != 5 {
		return false, fmt.Errorf("view shape must be TCZYX")
	}
	a.frames, a.channels, a.dtype = view.ShapeTczyx[0], view.ShapeTczyx[1], view.Dtype
	a.generation, a.haveDataset = generation, true
	a.t, a.selected = view.T, view.SelectedRegionId
	wasUnsupported := a.unsupported.Swap(view.Dtype != pb.DType_DTYPE_U8 && view.Dtype != pb.DType_DTYPE_U16)
	// Cancel old reads before measuring the new dataset; stale successes cannot
	// publish into the replacement traces. Queued traces already know T.
	a.traces.datasetOpened(a.frames)
	if a.c >= a.channels {
		a.c = 0
		if err := a.handle().SetValue("channel", plugin.Int(0)); err != nil {
			return true, err
		}
	}
	if a.unsupported.Load() {
		return true, a.status(floatStatus)
	}
	if wasUnsupported {
		return true, a.status("Ready")
	}
	return true, nil
}

func (a *application) publish(ctx context.Context) error {
	rows := ordered(a.rows)
	if len(rows) > maxRows {
		if err := a.status(fmt.Sprintf("Showing %d of %d regions", maxRows, len(rows))); err != nil {
			return err
		}
		rows = rows[:maxRows]
	}
	keys := make([]string, len(rows))
	for i, r := range rows {
		keys[i] = r.id
	}
	p := a.handle()
	label := ""
	if len(rows) == 1 {
		label = "Tour 1 region"
	} else if len(rows) > 1 {
		label = fmt.Sprintf("Tour %d regions", len(rows))
	}
	if err := p.OfferFollow(label); err != nil {
		return err
	}
	if err := p.SetRows("regions", keys...); err != nil {
		return err
	}
	if a.colormap == nil {
		table, err := a.session.Client.GetColormap(ctx, "viridis")
		if err != nil {
			return err
		}
		a.colormap = table.Rgba
	}
	for _, r := range rows {
		cell := p.Row(r.id)
		count, mean := r.numbers()
		for _, v := range []struct{ id, text string }{{"name", r.display()}, {"voxels", count}, {"mean", mean}} {
			if err := cell.SetValue(v.id, plugin.String(v.text)); err != nil {
				return err
			}
		}
		{
			index := swatchIndex(r.measurement)
			data := a.pngs[index]
			if data == nil {
				var err error
				data, err = swatchPNG(a.colormap[4*index : 4*index+3])
				if err != nil {
					return err
				}
				a.pngs[index] = data
			}
			if err := cell.SetPNG(a.session, 0, "swatch", data); err != nil {
				return err
			}
		}
	}
	selection := a.selected
	if !slices.Contains(keys, selection) {
		selection = ""
	}
	return p.SetValue("regions", plugin.String(selection))
}
func (a *application) viewChanged(ctx context.Context, _ *control.Client, q *pb.ViewChanged) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := a.lock(ctx); err != nil {
		return err
	}
	defer a.unlock()
	opened, err := a.datasetOpened(ctx, q.OpenGeneration)
	if err != nil {
		return err
	}
	rows := maps.Clone(a.rows)
	if opened || q.T != a.t {
		if err := a.remeasure(ctx, rows, q.T, a.c); err != nil {
			return err
		}
	}
	a.rows, a.t, a.selected = rows, q.T, q.SelectedRegionId
	a.traces.syncRows(rows, a.c, nil, false)
	a.traces.selectRegion(a.selected)
	return a.publish(ctx)
}
func (a *application) regionsChanged(ctx context.Context, c *control.Client, q *pb.RegionsChanged) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := a.lock(ctx); err != nil {
		return err
	}
	defer a.unlock()
	opened, err := a.datasetOpened(ctx, q.OpenGeneration)
	if err != nil {
		return err
	}
	rows := maps.Clone(a.rows)
	if q.Reset_ {
		rows = map[string]row{}
	}
	if opened {
		if err := a.remeasure(ctx, rows, a.t, a.c); err != nil {
			return err
		}
	}
	var changed []string
	for _, r := range q.Changes {
		delete(rows, r.RegionId)
		if !r.Deleted {
			changed = append(changed, r.RegionId)
		}
	}
	if len(changed) > 0 {
		records, err := c.GetRois(ctx, changed...)
		if err != nil {
			return err
		}
		updates := make(map[string]row, len(records.Rois))
		for _, r := range records.Rois {
			previous := a.rows[r.RegionId]
			previous.id, previous.name = r.RegionId, r.Name
			updates[r.RegionId] = previous
		}
		if err := a.remeasure(ctx, updates, a.t, a.c); err != nil {
			return err
		}
		maps.Copy(rows, updates)
	}
	a.rows = rows
	replace := map[string]bool{}
	for _, change := range q.Changes {
		replace[change.RegionId] = true
	}
	a.traces.syncRows(rows, a.c, replace, q.Reset_)
	a.traces.selectRegion(a.selected)
	return a.publish(ctx)
}
func (a *application) opened(ctx context.Context, _ *control.Client) error {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	if err := a.lock(ctx); err != nil {
		return err
	}
	defer a.unlock()
	return a.publish(ctx)
}
func (a *application) channel(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
	ctx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	c, err := args.Int("value")
	if err != nil {
		return plugin.Result{}, err
	}
	if c < 0 || c > math.MaxUint32 {
		return plugin.Result{}, fmt.Errorf("invalid channel")
	}
	if err := a.lock(ctx); err != nil {
		return plugin.Result{}, err
	}
	defer a.unlock()
	if uint32(c) == a.c {
		return plugin.Result{}, nil
	}
	rows := maps.Clone(a.rows)
	if err := a.remeasure(ctx, rows, a.t, uint32(c)); err != nil {
		return plugin.Result{}, err
	}
	a.rows, a.c = rows, uint32(c)
	a.traces.syncRows(rows, a.c, nil, false)
	return plugin.Result{}, a.publish(ctx)
}

// Mark an attempted answer before sending: a timeout is unknown, so a failure
// must not send a different answer under a possibly consumed cause.
type causeAnswer struct {
	ctx       context.Context
	c         *control.Client
	attempted bool
}

func (v *causeAnswer) decline() (*pb.ViewRequestResult, error) {
	v.attempted = true
	return plugin.DeclineView(v.ctx, v.c)
}
func (v *causeAnswer) move(targets plugin.ViewTargets) (*pb.ViewRequestResult, error) {
	v.attempted = true
	return plugin.MoveView(v.ctx, v.c, targets)
}
func (v *causeAnswer) finish(err *error) {
	if !v.attempted && plugin.CommandCause(v.ctx) != 0 {
		_, e := v.decline()
		*err = errors.Join(*err, e)
	}
}
func viewStatus(r *pb.ViewRequestResult, applied string) string {
	if r.Outcome == pb.ViewOutcome_VIEW_OUTCOME_APPLIED {
		return applied
	}
	return "The view did not move: " + strings.TrimPrefix(r.Reason.String(), "VIEW_REFUSAL_")
}
func (a *application) pick(ctx context.Context, c *control.Client, args plugin.Args) (_ plugin.Result, err error) {
	if plugin.CommandCause(ctx) == 0 {
		select {
		case <-a.ready:
		case <-ctx.Done():
			return plugin.Result{}, ctx.Err()
		}
		return plugin.Result{}, a.status("No dataset is open")
	}
	answer := causeAnswer{ctx: ctx, c: c}
	defer answer.finish(&err)
	wait, cancel := context.WithTimeout(ctx, 900*time.Millisecond)
	defer cancel()
	if err = a.lock(wait); err != nil {
		return
	}
	defer a.unlock()
	id, e := args.String("row")
	if e != nil {
		err = e
		return
	}
	r, ok := a.rows[id]
	if !ok {
		_, err = answer.decline()
		return
	}
	result, e := answer.move(plugin.ViewTargets{SelectRegion: &id})
	if e != nil {
		err = e
		return
	}
	err = a.status(viewStatus(result, "Selected "+r.display()))
	return
}
func (a *application) brightest(ctx context.Context, c *control.Client, _ plugin.Args) (_ plugin.Result, err error) {
	if plugin.CommandCause(ctx) == 0 {
		select {
		case <-a.ready:
		case <-ctx.Done():
			return plugin.Result{}, ctx.Err()
		}
		return plugin.Result{}, a.status("No dataset is open")
	}
	answer := causeAnswer{ctx: ctx, c: c}
	defer answer.finish(&err)
	wait, cancel := context.WithTimeout(ctx, 900*time.Millisecond)
	defer cancel()
	if err = a.lock(wait); err != nil {
		return
	}
	defer a.unlock()
	// The trace worker maintains the answer; commands perform no host reads.
	tr := a.traces.snapshot()
	decline := func(message string) error {
		if _, e := answer.decline(); e != nil {
			return e
		}
		return a.status(message)
	}
	if a.unsupported.Load() {
		return plugin.Result{}, decline(floatStatus)
	}
	if !tr.selected {
		return plugin.Result{}, decline("Select a region first")
	}
	if !tr.complete {
		return plugin.Result{}, decline(fmt.Sprintf("Measuring… %d of %d frames", tr.done, tr.total))
	}
	if !tr.found {
		return plugin.Result{}, decline(tr.name + " covers no voxel")
	}
	result, e := answer.move(plugin.ViewTargets{T: &tr.best})
	if e != nil {
		return plugin.Result{}, e
	}
	return plugin.Result{}, a.status(viewStatus(result, fmt.Sprintf("Brightest frame for %s: t=%d", tr.name, tr.best)))
}
func (a *application) export(_ context.Context, _ *control.Client, _ plugin.Args) (plugin.Result, error) {
	return plugin.Result{AfterReply: a.exportFile}, nil
}
func (a *application) exportFile(ctx context.Context, c *control.Client) error {
	result, err := c.RequestSavePath(ctx, &pb.RequestSavePath{SuggestedName: "regions.csv", Extension: "csv", Title: "Export region means"})
	if errors.Is(err, control.ErrSessionEnded) {
		return err // the session ended while the picker was open: nothing to report to
	}
	if lockErr := a.lock(ctx); lockErr != nil {
		return lockErr
	}
	if err != nil {
		var host *control.HostError
		message := err.Error()
		if errors.As(err, &host) {
			message = host.Message
		}
		defer a.unlock()
		return a.status("Export failed: " + message)
	}
	if result.Declined {
		defer a.unlock()
		return a.status("Export canceled")
	}
	rows := ordered(a.rows)
	data, err := tableCSV(rows, a.t, a.c)
	a.unlock()
	if err == nil {
		err = os.WriteFile(result.Path, data, 0644)
	}
	if lockErr := a.lock(ctx); lockErr != nil {
		return lockErr
	}
	defer a.unlock()
	if err != nil {
		return a.status("Export failed: " + err.Error())
	}
	return a.status(fmt.Sprintf("Exported %d regions to %s", len(rows), result.Path))
}
