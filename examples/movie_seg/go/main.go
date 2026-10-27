package main

import (
	"context"
	"errors"
	"fmt"
	"math"
	"os"
	"os/signal"
	"slices"
	"strconv"
	"strings"
	"sync"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
)

type movie struct {
	patch     func(*control.Client, string, *pb.Value) error
	mu        sync.Mutex
	threshold float64
	hidden    [4]bool
	sent      map[string]bool
	overlays  map[string]*plugin.ChunkOverlay
}

func newMovie() (*movie, error) {
	a := &movie{patch: func(c *control.Client, widget string, value *pb.Value) error {
		return (plugin.Panel{ID: "movie_seg", Client: c}).SetValue(widget, value)
	}, threshold: 400, sent: make(map[string]bool), overlays: make(map[string]*plugin.ChunkOverlay)}
	for _, id := range []string{"bands", "footprint"} {
		o, err := plugin.NewChunkOverlay(id, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*plugin.Chunk, error) {
			return a.source(ctx, s, id, w)
		})
		if err != nil {
			return nil, err
		}
		a.overlays[id] = o
	}
	return a, nil
}
func (a *movie) tableLocked(id string) error {
	o := a.overlays[id]
	if id == "bands" {
		lut, visible := bandTable(a.hidden)
		return o.SetLabelTable(lut, bandNames, visible, &pb.OverlayStyle{Opacity: 0.6, DrawPriority: 1})
	}
	return o.SetLabelTable(footprintTable(), map[uint32]string{1: "ever"}, nil, &pb.OverlayStyle{Opacity: 0.3})
}
func (a *movie) parameters(id string) (float64, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if !a.sent[id] {
		if err := a.tableLocked(id); err != nil {
			return 0, err
		}
		a.sent[id] = true
	}
	return a.threshold, nil
}
func read(ctx context.Context, s *control.Session, t, level uint32) (*payload.Volume, error) {
	return payload.ReadVolume(ctx, s.Client, s.Memory, &pb.ReadDataVolume{T: t, C: 0, Level: level}, payload.CopyOptions{MaxBytes: 512 << 20, ByteOrder: payload.LittleEndian})
}
func (a *movie) source(ctx context.Context, s *control.Session, id string, w *pb.ChunkWant) (*plugin.Chunk, error) {
	threshold, err := a.parameters(id)
	if err != nil {
		return nil, err
	}
	if id == "bands" {
		if w.T == nil {
			return nil, errors.New("per_t want omitted t")
		}
		v, err := read(ctx, s, *w.T, w.Level)
		if err != nil {
			return nil, err
		}
		labels, err := labelTensor(v.Tensor, threshold)
		if err != nil {
			return nil, err
		}
		return tight(labels, v.Tensor.Shape())
	}
	if w.T != nil {
		return nil, errors.New("static want included t")
	}
	view, err := s.Client.GetViewState(ctx)
	if err != nil {
		return nil, err
	}
	if len(view.ShapeTczyx) != 5 || view.ShapeTczyx[0] == 0 {
		return nil, errors.New("dataset has no timepoints")
	}
	var union []byte
	var shape []uint32
	for t := uint32(0); t < view.ShapeTczyx[0]; t++ {
		v, err := read(ctx, s, t, w.Level)
		if err != nil {
			return nil, err
		}
		if v.OpenGeneration != view.OpenGeneration {
			return nil, errors.New("dataset changed during footprint read")
		}
		labels, err := labelTensor(v.Tensor, threshold)
		if err != nil {
			return nil, err
		}
		if t == 0 {
			shape = v.Tensor.Shape()
			union = make([]byte, len(labels))
		} else if !slices.Equal(shape, v.Tensor.Shape()) {
			return nil, errors.New("footprint frame shape changed")
		}
		if err := unionInto(union, labels); err != nil {
			return nil, err
		}
	}
	return tight(union, shape)
}
func thresholdArg(args plugin.Args) (float64, string, error) {
	// Integer arguments are compared as floats, but their reply retains the input.
	if i, err := args.Int("value"); err == nil {
		if i < 0 {
			return 0, "", errors.New("value must be finite and nonnegative")
		}
		return float64(i), strconv.FormatInt(i, 10), nil
	}
	f, err := args.Float("value")
	if text, textErr := args.String("value"); textErr == nil {
		f, err = strconv.ParseFloat(strings.TrimSpace(text), 64)
	}
	if err != nil {
		return 0, "", err
	}
	if math.IsNaN(f) || math.IsInf(f, 0) || f < 0 {
		return 0, "", errors.New("value must be finite and nonnegative")
	}
	if f == 0 {
		f = 0
	} // normalize negative zero
	format := byte('g')
	if f == math.Trunc(f) {
		format = 'f'
	}
	return f, strconv.FormatFloat(f, format, -1, 64), nil
}
func (a *movie) setThreshold(_ context.Context, c *control.Client, args plugin.Args) (plugin.Result, error) {
	value, spelling, err := thresholdArg(args)
	if err != nil {
		return plugin.Result{}, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.patch(c, "threshold", plugin.String(spelling)); err != nil {
		return plugin.Result{}, err
	}
	a.threshold = value
	wanted := 0
	for _, id := range []string{"bands", "footprint"} {
		o := a.overlays[id]
		wanted += len(o.Wants())
		o.InvalidateChunks()
	}
	return plugin.Result{Message: fmt.Sprintf("threshold %s; %d wanted volume(s) to serve again", spelling, wanted)}, nil
}
func (a *movie) setVisible(_ context.Context, c *control.Client, args plugin.Args) (plugin.Result, error) {
	band, err := args.Int("band")
	if err != nil {
		return plugin.Result{}, err
	}
	if band < 1 || band > 3 {
		return plugin.Result{}, errors.New("band must be 1, 2 or 3")
	}
	visible, err := args.Bool("visible")
	if err != nil {
		return plugin.Result{}, err
	}
	return a.setBand(c, band, visible)
}
func (a *movie) bandCommand(band int64) plugin.CommandHandler {
	return func(_ context.Context, c *control.Client, args plugin.Args) (plugin.Result, error) {
		visible, err := args.Bool("value")
		if err != nil {
			return plugin.Result{}, err
		}
		return a.setBand(c, band, visible)
	}
}
func (a *movie) setBand(c *control.Client, band int64, visible bool) (plugin.Result, error) {
	a.mu.Lock()
	defer a.mu.Unlock()
	if err := a.patch(c, fmt.Sprintf("band_%d", band), plugin.Bool(visible)); err != nil {
		return plugin.Result{}, err
	}
	a.hidden[band] = !visible
	if a.sent["bands"] {
		if err := a.tableLocked("bands"); err != nil {
			return plugin.Result{}, err
		}
	}
	state := "hidden"
	if visible {
		state = "visible"
	}
	return plugin.Result{Message: fmt.Sprintf("band %d (%s) %s", band, bandNames[uint32(band)], state)}, nil
}
func runtimeDeclaration() (*plugin.Application, control.Plugin, error) {
	a, err := newMovie()
	if err != nil {
		return nil, control.Plugin{}, err
	}
	d := plugin.NewDefinition(plugin.Info{ID: "org.example.movie_seg", Name: "Movie seg", Version: "0.1.0"})
	d.Needs(plugin.DataAccess)
	threshold := d.Command(plugin.CommandSpec{ID: "movie_seg.threshold", Title: "Set threshold"}, a.setThreshold)
	d.Command(plugin.CommandSpec{ID: "movie_seg.band_visible", Title: "Show or hide a band"}, a.setVisible)
	children := []plugin.Widget{plugin.Row("rt", plugin.Label("lt", "Threshold"), plugin.LineEdit("threshold", "400", "", threshold))}
	for band := int64(1); band <= 3; band++ {
		id := fmt.Sprintf("band_%d", band)
		name := fmt.Sprintf("Band %d (%s)", band, bandNames[uint32(band)])
		command := d.Command(plugin.CommandSpec{ID: "movie_seg." + id, Title: fmt.Sprintf("Show band %d (%s)", band, bandNames[uint32(band)])}, a.bandCommand(band))
		children = append(children, plugin.Checkbox(id, name, true, command))
	}
	d.Panel(plugin.PanelSpec{ID: "movie_seg", Title: "Movie seg", Root: plugin.Column("root", children...)}, nil)
	for _, spec := range []plugin.OverlaySpec{{ID: "bands", Name: "Intensity bands", Time: "per_t"}, {ID: "footprint", Name: "Footprint", Time: "static"}} {
		id := spec.ID
		ref := d.ChunkOverlay(spec, func(ctx context.Context, s *control.Session, w *pb.ChunkWant) (*plugin.Chunk, error) {
			return a.source(ctx, s, id, w)
		})
		d.OnChunkOverlay(ref, func(o *plugin.ChunkOverlay) {
			a.mu.Lock()
			defer a.mu.Unlock()
			a.overlays[id] = o
		})
	}
	app, err := d.Build()
	return app, control.Plugin{}, err
}
func runtimeConfig() (control.Plugin, error) {
	app, base, err := runtimeDeclaration()
	if err != nil {
		return control.Plugin{}, err
	}
	return app.Configure(base)
}
func main() {
	app, base, err := runtimeDeclaration()
	if err != nil {
		os.Exit(plugin.DeclarationExitCode(plugin.Info{ID: "org.example.movie_seg", Version: "0.1.0"}, os.Args[1:], err, plugin.RunOptions{}))
	}
	ctx, cancel := signal.NotifyContext(context.Background(), os.Interrupt)
	defer cancel()
	if code := app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{Runtime: func() (control.Plugin, error) { return base, nil }}); code != 0 {
		os.Exit(code)
	}
}
