package peakroi

import (
	"context"
	"errors"
	"math"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

type fakeHost struct {
	mu                                        sync.Mutex
	state                                     *pb.ViewState
	viewErr, readErr, contributeErr, patchErr error
	readHook                                  func(context.Context)
	generation                                uint64
	requests                                  []*pb.ReadDataVolume
	rois                                      []*pb.ContributeRois
	widgets                                   map[string]*pb.Value
	tensor                                    *payload.Tensor
}

func fixture() (*application, *fakeHost) {
	h := &fakeHost{state: &pb.ViewState{T: 1, VisibleChannels: []uint32{1}, Level: 3, OpenGeneration: 7, ShapeTczyx: []uint32{3, 2, 3, 3, 3}, HasCrop: true}, generation: 7, widgets: make(map[string]*pb.Value)}
	a := newApplication(context.Background(), h)
	a.extents = [3]string{"1", "1", "1"}
	a.panelOpen = true // Ordinary tests exercise an already-open panel.
	return a, h
}
func (h *fakeHost) view(context.Context) (*pb.ViewState, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	return proto.Clone(h.state).(*pb.ViewState), h.viewErr
}
func (h *fakeHost) read(ctx context.Context, q *pb.ReadDataVolume) (*payload.Volume, error) {
	h.mu.Lock()
	h.requests = append(h.requests, proto.Clone(q).(*pb.ReadDataVolume))
	hook := h.readHook
	h.mu.Unlock()
	if hook != nil {
		hook(ctx)
	}
	if ctx.Err() != nil {
		return nil, ctx.Err()
	}
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.readErr != nil {
		return nil, h.readErr
	}
	if h.tensor != nil {
		return &payload.Volume{Tensor: h.tensor, T: q.T, C: q.C, OpenGeneration: h.generation}, nil
	}
	data := make([]uint16, 27)
	data[int(q.T)*9+4] = 500
	tensor, err := payload.Encode(data, []uint32{3, 3, 3}, true)
	return &payload.Volume{Tensor: tensor, T: q.T, C: q.C, OpenGeneration: h.generation}, err
}
func (h *fakeHost) contribute(_ context.Context, q *pb.ContributeRois) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.rois = append(h.rois, proto.Clone(q).(*pb.ContributeRois))
	return h.contributeErr
}
func (h *fakeHost) patch(ops ...*pb.PatchOp) error {
	h.mu.Lock()
	defer h.mu.Unlock()
	for _, op := range ops {
		h.widgets[op.WidgetId] = proto.Clone(op.GetSetValue()).(*pb.Value)
	}
	return h.patchErr
}
func awaitJob(t *testing.T, a *application) {
	t.Helper()
	a.mu.Lock()
	done := a.done
	a.mu.Unlock()
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("worker did not finish")
	}
}

func TestRunModesAndStableContribution(t *testing.T) {
	a, h := fixture()
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitJob(t, a)
	if len(h.requests) != 1 || h.requests[0].T != 1 || h.requests[0].C != 1 || h.requests[0].Level != 0 || len(h.requests[0].RoiMinZyx) != 0 || len(h.requests[0].RoiMaxZyx) != 0 {
		t.Fatal(h.requests)
	}
	first := h.rois[0].Rois[0]
	if first.ContribId != "peak" || first.Roi.TStart != nil || first.Roi.Vertices[0].Position.Z != 1.5 {
		t.Fatal(first)
	}
	if err := a.setMode(plugin.Args{"index": plugin.Int(1), "value": plugin.String("All timepoints")}); err != nil {
		t.Fatal(err)
	}
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitJob(t, a)
	if len(h.requests) != 4 {
		t.Fatal(h.requests)
	}
	for i, q := range h.requests[1:] {
		if q.T != uint32(i) || q.C != 1 || q.Level != 0 {
			t.Fatal(q)
		}
	}
	last := h.rois[1].Rois[0]
	if last.ContribId != first.ContribId || !slices.Equal(last.Roi.PoseTranslation, []float64{0, 0, 0, 1, 0, 0, 2, 0, 0}) {
		t.Fatal(last)
	}
	if !strings.Contains(a.status, "channel 1") || !strings.HasPrefix(a.status, "Done:") {
		t.Fatal(a.status)
	}
	// No visible channels falls back to channel zero for the next run.
	h.state.VisibleChannels = nil
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitJob(t, a)
	for _, q := range h.requests[4:] {
		if q.C != 0 {
			t.Fatal(q)
		}
	}
	if !strings.Contains(a.status, "channel 0") {
		t.Fatal(a.status)
	}
}

func TestBusySnapshotAndCommandContext(t *testing.T) {
	a, h := fixture()
	entered, unblock := make(chan struct{}), make(chan struct{})
	h.readHook = func(ctx context.Context) {
		close(entered)
		select {
		case <-unblock:
		case <-ctx.Done():
		}
	}
	commandCtx, cancel := context.WithCancel(context.Background())
	if _, err := a.run(commandCtx); err != nil {
		t.Fatal(err)
	}
	cancel() // CommandDone's context must not kill the accepted job.
	select {
	case <-entered:
	case <-time.After(time.Second):
		t.Fatal("worker not started")
	}
	if _, err := a.run(context.Background()); err == nil || !strings.Contains(err.Error(), "in progress") {
		t.Fatal(err)
	}
	if err := a.setExtent(0, plugin.Args{"value": plugin.String("5")}); err != nil {
		t.Fatal(err)
	}
	if err := a.opened(); err != nil {
		t.Fatal(err)
	}
	h.mu.Lock()
	if h.widgets["ez"].GetS() != "5" {
		t.Fatal(h.widgets)
	}
	h.state.VisibleChannels = []uint32{0} // Visibility changes must not retarget the accepted job.
	h.mu.Unlock()
	close(unblock)
	awaitJob(t, a)
	if len(h.rois) != 1 {
		t.Fatal("command cancellation killed worker", a.status)
	}
	if h.requests[0].C != 1 || !strings.Contains(a.status, "channel 1") {
		t.Fatal("job used a later channel", h.requests, a.status)
	}
	r := h.rois[0].Rois[0].Roi
	if r.Vertices[1].Position.Z != r.Vertices[0].Position.Z+1 {
		t.Fatal("job used a later extent")
	}
}

func TestShutdownCancelsWorker(t *testing.T) {
	a, h := fixture()
	h.readHook = func(ctx context.Context) { <-ctx.Done() }
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := a.shutdown(ctx); err != nil {
		t.Fatal(err)
	}
	if len(h.rois) != 0 || a.busy {
		t.Fatal("shutdown left work running")
	}
	if _, err := a.run(context.Background()); err == nil {
		t.Fatal("accepted work after shutdown")
	}
}

func TestImmediateRefusals(t *testing.T) {
	for _, text := range []string{"bad", "0", "-0", "NaN", "Inf", "-Inf", "-1", "1e100"} {
		for axis, widget := range []string{"ez", "ey", "ex"} {
			a, h := fixture()
			if err := a.setExtent(axis, plugin.Args{"value": plugin.String(text)}); err == nil || !strings.Contains(err.Error(), text) {
				t.Fatalf("axis %d, value %q: error %v", axis, text, err)
			}
			if a.extents != [3]string{"1", "1", "1"} || h.widgets[widget].GetS() != "1" {
				t.Fatal("failed edit changed model or failed to restore widget")
			}
			if !strings.Contains(h.widgets["status"].GetS(), text) {
				t.Fatal("status omitted invalid value")
			}
		}
	}
	for _, shape := range [][]uint32{nil, {1, 1, 0, 1, 1}, {1, 1, 1}} {
		a, h := fixture()
		h.state.ShapeTczyx = shape
		if _, err := a.run(context.Background()); err == nil {
			t.Fatal("accepted missing dataset")
		}
		if a.busy || len(h.requests) != 0 {
			t.Fatal("refused job still running")
		}
	}
	a, h := fixture()
	h.viewErr = errors.New("host has no dataset")
	if _, err := a.run(context.Background()); err == nil {
		t.Fatal("ignored host error")
	}
	if err := a.setMode(plugin.Args{"value": plugin.String("All timepoints"), "index": plugin.Int(0)}); err == nil {
		t.Fatal("accepted mismatched mode")
	}
	if err := a.setExtent(0, plugin.Args{}); err == nil {
		t.Fatal("accepted missing value")
	}
}

func TestNonFiniteVolumeOutcomeAndRecovery(t *testing.T) {
	a, h := fixture()
	values := make([]float32, 27)
	for i := range values {
		values[i] = float32(math.NaN())
	}
	var err error
	h.tensor, err = payload.Encode(values, []uint32{3, 3, 3}, true)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitJob(t, a)
	if len(h.rois) != 0 || !strings.Contains(a.status, "no finite") {
		t.Fatalf("status %q, contributions %d", a.status, len(h.rois))
	}
	// A single finite sample makes the next run valid, with +Inf preserved.
	values[0], values[13], values[26] = -3, float32(math.Inf(1)), float32(math.Inf(-1))
	h.tensor, err = payload.Encode(values, []uint32{3, 3, 3}, true)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := a.run(context.Background()); err != nil {
		t.Fatal(err)
	}
	awaitJob(t, a)
	if len(h.rois) != 1 || !strings.HasPrefix(a.status, "Done:") {
		t.Fatalf("status %q, contributions %d", a.status, len(h.rois))
	}
}

func TestWorkerFailuresAndRecovery(t *testing.T) {
	for _, failure := range []string{"read", "generation", "before-contribution", "contribution", "panel"} {
		t.Run(failure, func(t *testing.T) {
			a, h := fixture()
			switch failure {
			case "read":
				h.readErr = errors.New("read refused")
			case "generation":
				h.generation = 8
			case "before-contribution":
				h.readHook = func(context.Context) { h.mu.Lock(); h.state.OpenGeneration = 8; h.mu.Unlock() }
			case "contribution":
				h.contributeErr = errors.New("ROI refused")
			case "panel":
				h.readHook = func(context.Context) { h.mu.Lock(); h.patchErr = errors.New("patch refused"); h.mu.Unlock() }
			}
			if _, err := a.run(context.Background()); err != nil {
				t.Fatal(err)
			}
			awaitJob(t, a)
			if a.busy || !strings.HasPrefix(a.status, "Error:") {
				t.Fatal(a.status)
			}
			if failure != "contribution" && failure != "panel" && len(h.rois) != 0 {
				t.Fatal("contributed after failure")
			}
			h.readErr, h.contributeErr, h.patchErr, h.readHook = nil, nil, nil, nil
			h.generation, h.state.OpenGeneration = 7, 7
			if _, err := a.run(context.Background()); err != nil {
				t.Fatal(err)
			}
			awaitJob(t, a)
			if !strings.HasPrefix(a.status, "Done:") {
				t.Fatal(a.status)
			}
		})
	}
}

func TestPanelStateWaitsForEveryOpen(t *testing.T) {
	a, h := fixture()
	a.panelOpen = false
	if err := a.progress("computed before open"); err != nil {
		t.Fatal(err)
	}
	if len(h.widgets) != 0 {
		t.Fatal("patched before PanelOpened", h.widgets)
	}
	for range 2 {
		if err := a.opened(); err != nil {
			t.Fatal(err)
		}
		if h.widgets["status"].GetS() != "computed before open" || len(h.widgets) != 5 {
			t.Fatal(h.widgets)
		}
		h.widgets = make(map[string]*pb.Value)
	}
}

func TestFrameLimitFollowsPoseWireBound(t *testing.T) {
	view := &pb.ViewState{ShapeTczyx: []uint32{maxPoseFrames, 1, 1, 1, 1}}
	if err := validateView(view, true); err != nil {
		t.Fatal(err)
	}
	view.ShapeTczyx[0]++
	if err := validateView(view, true); err == nil {
		t.Fatal("oversized pose contribution accepted")
	}
	if err := validateView(view, false); err != nil {
		t.Fatal("current frame incorrectly restricted", err)
	}
}
