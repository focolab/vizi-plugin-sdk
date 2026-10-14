package peakroi

import (
	"context"
	"errors"
	"fmt"
	"log"
	"slices"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type host interface {
	view(context.Context) (*pb.ViewState, error)
	read(context.Context, *pb.ReadDataVolume) (*payload.Volume, error)
	contribute(context.Context, *pb.ContributeRois) error
	patch(...*pb.PatchOp) error
}

type application struct {
	mu            sync.Mutex // Also orders panel snapshots against worker progress patches.
	host          host
	ctx           context.Context
	extents       [3]string
	all           bool
	status        string
	busy, stopped bool
	panelOpen     bool
	cancel        context.CancelFunc
	done          chan struct{}
}

func newApplication(ctx context.Context, h host) *application {
	return &application{host: h, ctx: ctx, extents: [3]string{"4", "8", "8"}, status: "Ready"}
}

func valueOp(id string, value *pb.Value) *pb.PatchOp {
	return &pb.PatchOp{WidgetId: id, Op: &pb.PatchOp_SetValue{SetValue: value}}
}

func (a *application) patchLocked(ops ...*pb.PatchOp) error {
	if !a.panelOpen {
		return nil
	}
	return a.host.patch(ops...)
}

func (a *application) opened() error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.panelOpen = true
	mode := int64(0)
	if a.all {
		mode = 1
	}
	return a.patchLocked(
		valueOp("ez", plugin.String(a.extents[0])),
		valueOp("ey", plugin.String(a.extents[1])),
		valueOp("ex", plugin.String(a.extents[2])),
		valueOp("mode", plugin.Int(mode)),
		valueOp("status", plugin.String(a.status)),
	)
}

func (a *application) setExtent(axis int, args plugin.Args) error {
	value, err := args.String("value")
	if err != nil {
		return err
	}
	parsed, err := strconv.ParseFloat(strings.TrimSpace(value), 64)
	if err != nil {
		return a.badExtent(axis, value, err)
	}
	extent := [3]float64{1, 1, 1}
	extent[axis] = parsed
	if err := validateExtents(extent); err != nil {
		return a.badExtent(axis, value, err)
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	// Invalid edits fail the invocation; only accepted edits change the model.
	a.extents[axis] = value
	return nil
}

func (a *application) badExtent(axis int, value string, err error) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	err = fmt.Errorf("Invalid %s extent %q: %w", []string{"Z", "Y", "X"}[axis], value, err)
	// Restore the accepted text as well as reporting why the edit was refused.
	// Otherwise a subsequent run could appear to use the rejected widget value.
	a.status = err.Error()
	patchErr := a.patchLocked(valueOp([]string{"ez", "ey", "ex"}[axis], plugin.String(a.extents[axis])), valueOp("status", plugin.String(a.status)))
	return errors.Join(err, patchErr)
}

func (a *application) setMode(args plugin.Args) error {
	value, err := args.String("value")
	if err != nil {
		return err
	}
	index, err := args.Int("index")
	if err != nil {
		return err
	}
	if index < 0 || index > 1 || value != []string{"Current timepoint", "All timepoints"}[index] {
		return errors.New("unknown or inconsistent timepoint mode")
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.all = index == 1
	return nil
}

// Caller holds mu so a reopened panel can never overwrite a newer status.
func (a *application) statusLocked(status string) error {
	a.status = status
	return a.patchLocked(valueOp("status", plugin.String(status)))
}

func (a *application) progress(status string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	return a.statusLocked(status)
}

func (a *application) run(ctx context.Context) (plugin.Result, error) {
	a.mu.Lock()
	if a.stopped || a.ctx.Err() != nil {
		a.mu.Unlock()
		return plugin.Result{}, errors.New("session is shutting down")
	}
	if a.busy {
		a.mu.Unlock()
		return plugin.Result{}, errors.New("a run is already in progress")
	}
	var extent [3]float64
	var err error
	for axis, text := range a.extents {
		extent[axis], err = strconv.ParseFloat(strings.TrimSpace(text), 64)
		if err != nil {
			break
		}
	}
	if err == nil {
		err = validateExtents(extent)
	}
	if err != nil {
		patchErr := a.statusLocked("Error: invalid extent: " + err.Error())
		a.mu.Unlock()
		return plugin.Result{}, errors.Join(err, patchErr)
	}
	all := a.all
	// Include validation in the busy guard, and let shutdown cancel it too.
	a.busy = true
	jobCtx, cancel := context.WithCancel(a.ctx)
	a.cancel, a.done = cancel, make(chan struct{})
	done := a.done
	a.mu.Unlock()
	accepted := false
	defer func() {
		if !accepted {
			a.finish(cancel, done, err)
		}
	}()

	// Keep the command responsive while the host supervises it. The background worker
	// uses the session context, never the command's expiring context.
	checkCtx, checkCancel := context.WithTimeout(jobCtx, 5*time.Second)
	stop := context.AfterFunc(ctx, checkCancel)
	view, viewErr := a.host.view(checkCtx)
	stop()
	checkCancel()
	if viewErr != nil {
		err = fmt.Errorf("cannot inspect dataset: %w", viewErr)
	} else {
		err = validateView(view, all)
	}
	if err != nil {
		return plugin.Result{}, err
	}
	if err = a.progress(fmt.Sprintf("Started: channel %d", firstVisibleChannel(view))); err != nil {
		return plugin.Result{}, err
	}
	accepted = true
	go func() {
		err := a.analyze(jobCtx, view, extent, all)
		a.finish(cancel, done, err)
	}()
	return plugin.Result{Message: "Peak search started; see the Peak ROI panel."}, nil
}

func firstVisibleChannel(view *pb.ViewState) uint32 {
	if len(view.VisibleChannels) != 0 {
		return view.VisibleChannels[0]
	}
	return 0
}

func validateView(view *pb.ViewState, all bool) error {
	if view == nil || len(view.ShapeTczyx) != 5 || slices.Contains(view.ShapeTczyx, uint32(0)) {
		return errors.New("no dataset is open")
	}
	if view.T >= view.ShapeTczyx[0] || firstVisibleChannel(view) >= view.ShapeTczyx[1] {
		return errors.New("view timepoint or channel is outside the dataset")
	}
	if all && view.ShapeTczyx[0] > maxPoseFrames {
		return fmt.Errorf("ROI translation poses support at most %d frames per contribution", maxPoseFrames)
	}
	return nil
}

func (a *application) analyze(ctx context.Context, view *pb.ViewState, extent [3]float64, all bool) error {
	channel := firstVisibleChannel(view)
	count := uint32(1)
	if all {
		count = view.ShapeTczyx[0]
	}
	peaks := make([][3]int, 0, count)
	for i := uint32(0); i < count; i++ {
		if err := ctx.Err(); err != nil {
			return err
		}
		t := view.T
		if all {
			t = i
		}
		if err := a.progress(fmt.Sprintf("Finding peak %d/%d (t=%d, channel %d)", i+1, count, t, channel)); err != nil {
			return err
		}
		volume, err := a.host.read(ctx, &pb.ReadDataVolume{T: t, C: channel, Level: 0})
		if err != nil {
			return fmt.Errorf("read t=%d: %w", t, err)
		}
		if volume == nil || volume.Tensor == nil {
			return errors.New("empty volume reply")
		}
		if volume.OpenGeneration != view.OpenGeneration {
			return errors.New("dataset changed during analysis")
		}
		if volume.T != t || volume.C != channel || volume.Level != 0 {
			return errors.New("host served different volume indices")
		}
		if !slices.Equal(volume.Tensor.Shape(), view.ShapeTczyx[2:]) {
			return errors.New("host served a different volume shape")
		}
		peak, err := findPeak(ctx, volume.Tensor, extent)
		if err != nil {
			return err
		}
		peaks = append(peaks, peak)
	}
	// No conditional contribution exists in the protocol. Recheck immediately
	// before submission; a dataset swap after this check remains a host race.
	current, err := a.host.view(ctx)
	if err != nil {
		return err
	}
	if err := validateView(current, false); err != nil {
		return err
	}
	if current.OpenGeneration != view.OpenGeneration {
		return errors.New("dataset changed before ROI contribution")
	}
	if err := ctx.Err(); err != nil {
		return err
	}
	err = a.host.contribute(ctx, &pb.ContributeRois{Rois: []*pb.RoiContribution{{
		ContribId: "peak", Name: "Peak", Style: &pb.RoiStyle{Color: "#ffd24a"}, Roi: ellipsoid(peaks, extent, all),
	}}})
	if err != nil {
		return fmt.Errorf("contribute ROI: %w", err)
	}
	return a.progress(fmt.Sprintf("Done: Peak ROI, %d timepoint(s), channel %d; first peak ZYX=%v", count, channel, peaks[0]))
}

func (a *application) finish(cancel context.CancelFunc, done chan struct{}, err error) {
	cancel()
	a.mu.Lock()
	defer a.mu.Unlock()
	if err != nil {
		log.Printf("peak search: %v", err)
		if patchErr := a.statusLocked("Error: " + err.Error()); patchErr != nil {
			log.Printf("status patch: %v", patchErr)
		}
	}
	a.busy = false
	a.cancel = nil
	close(done)
}

func (a *application) shutdown(ctx context.Context) error {
	a.mu.Lock()
	a.stopped = true
	if a.cancel != nil {
		a.cancel()
	}
	done := a.done
	a.mu.Unlock()
	if done == nil {
		return nil
	}
	select {
	case <-done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
