package main

import (
	"context"
	"encoding/binary"
	"errors"
	"fmt"
	"log"
	"math"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"sync"
	"time"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/payload"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/plugin"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"google.golang.org/protobuf/proto"
)

const maxPayload = 256 << 20

type exportJob struct {
	writer          *crateWriter
	opened          *pb.OpenExport
	times, channels []uint32
}
type application struct {
	mu                          sync.Mutex
	ready                       chan struct{}
	session                     *control.Session
	ctx                         context.Context
	cancel                      context.CancelFunc
	workers                     sync.WaitGroup
	closing, busy, verify       bool
	panelOpen                   bool
	reader                      *crate
	readerHandle                string
	readers                     map[string]*crate
	nextReader                  uint64
	exports                     map[string]*exportJob
	nextExport                  uint64
	cal, override               calibration
	overrideName                string
	head, vox, calLabel, status string
}

func newApplication() *application {
	return &application{ready: make(chan struct{}), verify: true, readers: make(map[string]*crate), exports: make(map[string]*exportJob), head: "No crate open", vox: "(unknown)", calLabel: "(not provisioned)", status: "Ready"}
}
func (a *application) wait(ctx context.Context) error {
	select {
	case <-a.ready:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}
func (a *application) pushLocked() error {
	if a.session == nil || !a.panelOpen {
		return nil
	}
	ops := []*pb.PatchOp{}
	for _, v := range [][2]string{{"head", a.head}, {"vox", a.vox}, {"cal", a.calLabel}, {"status", a.status}} {
		ops = append(ops, &pb.PatchOp{WidgetId: v[0], Op: &pb.PatchOp_SetValue{SetValue: plugin.String(v[1])}})
	}
	ops = append(ops, &pb.PatchOp{WidgetId: "verify", Op: &pb.PatchOp_SetValue{SetValue: plugin.Bool(a.verify)}})
	return (plugin.Panel{ID: "crate", Client: a.session.Client}).Patch(ops...)
}
func (a *application) note(s string) error {
	a.mu.Lock()
	defer a.mu.Unlock()
	a.status = s
	return a.pushLocked()
}
func fail(kind pb.Error_Kind, err error) error {
	if err == nil {
		return nil
	}
	return &control.HostError{Kind: kind, Message: err.Error()}
}
func notFound(handle string) error {
	return fail(pb.Error_NOT_FOUND, fmt.Errorf("unknown handle %q", handle))
}
func (a *application) declarationRuntime() (*plugin.Application, control.Plugin, error) {
	h := plugin.Handlers{
		Readers: map[string]plugin.ReaderHandlers{"crate": {
			Open: a.openReader, Volume: a.readVolume, Plane: a.readPlane, Metadata: a.metadata, Close: a.closeReader,
		}},
		Export: plugin.ExportHandlers{Open: a.openExport, Chunk: a.exportChunk, Finish: a.finishExport, Abort: a.abortExport},
		Commands: map[string]plugin.CommandHandler{
			"crate.set_verify": a.setVerify,
			"crate.describe": func(ctx context.Context, c *control.Client, _ plugin.Args) (plugin.Result, error) {
				return a.schedule(ctx, "Reading active dataset…", a.describe)
			},
			"crate.pick_calibration": func(ctx context.Context, c *control.Client, _ plugin.Args) (plugin.Result, error) {
				return a.schedule(ctx, "Choose a calibration table", a.pickCalibration)
			},
			"crate.check_file": func(ctx context.Context, c *control.Client, _ plugin.Args) (plugin.Result, error) {
				return a.schedule(ctx, "Choose a crate to check", a.checkFile)
			},
		},
		Panels: map[string]plugin.PanelOpenedHandler{"crate": func(ctx context.Context, _ *control.Client) error {
			if err := a.wait(ctx); err != nil {
				return err
			}
			a.mu.Lock()
			defer a.mu.Unlock()
			a.panelOpen = true
			return a.pushLocked()
		}},
		Shutdown: func(ctx context.Context, _ *control.Client, _ *pb.Shutdown) error { return a.shutdown(ctx) },
	}
	base := control.Plugin{Started: func(s *control.Session) {
		a.mu.Lock()
		defer a.mu.Unlock()
		a.session = s
		a.ctx, a.cancel = context.WithCancel(s.Client.Context())
		assets, err := control.AssetsFromEnv()
		if err == nil {
			var path string
			path, err = control.AssetPath(assets, "calibration")
			if err == nil {
				a.cal, err = loadCalibration(path)
				if err == nil {
					a.calLabel = "manifest asset"
				}
			}
		}
		if err != nil && !errors.Is(err, control.ErrAssetUndeclared) {
			a.status = "Calibration unavailable: " + err.Error()
		}
		close(a.ready)
	}}
	app, err := declare(h)
	return app, base, err
}
func (a *application) openReader(ctx context.Context, _ *control.Client, q *pb.OpenReader) (*pb.ReaderOpened, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	cal := a.cal
	if a.override != nil {
		cal = a.override
	}
	c, err := openCrate(q.Uri, cal)
	if err != nil {
		kind := pb.Error_IO_ERROR
		if errors.Is(err, os.ErrNotExist) {
			kind = pb.Error_NOT_FOUND
		} else if strings.Contains(err.Error(), "unsupported dtype") {
			kind = pb.Error_UNSUPPORTED
		} else if strings.Contains(err.Error(), "not a crate") || strings.Contains(err.Error(), "zero ") {
			kind = pb.Error_INVALID_ARGUMENT
		}
		a.status = err.Error()
		if e := a.pushLocked(); e != nil {
			log.Print(e)
		}
		return nil, fail(kind, err)
	}
	previous, previousHandle := a.reader, a.readerHandle
	previousHead, previousVox, previousStatus := a.head, a.vox, a.status
	a.nextReader++
	handle := fmt.Sprintf("crate-%d", a.nextReader)
	a.reader, a.readerHandle = c, handle
	a.head = c.Path
	a.vox = spacingLabel(c.Header.Spacing)
	a.status = "Opened " + c.Path
	if len(c.Notes) > 0 {
		a.status += "; " + strings.Join(c.Notes, "; ")
	}
	if err = a.pushLocked(); err != nil {
		c.Close()
		a.reader, a.readerHandle = previous, previousHandle
		a.head, a.vox, a.status = previousHead, previousVox, previousStatus
		return nil, err
	}
	a.readers[handle] = c
	s := c.Header.Spacing
	return &pb.ReaderOpened{Handle: handle, ShapeTczyx: slices.Clone(c.Header.Shape[:]), Dtype: pb.DType(c.Header.DType), LittleEndian: true, AxisInference: "crate", SpacingZ: s[0], SpacingY: s[1], SpacingX: s[2], SpacingUnit: "um", TimeIncrement: s[3], TimeUnit: "s", ChannelNames: slices.Clone(c.Header.Names)}, nil
}
func (a *application) deposit(b []byte, shape []uint32, dtype uint16) (*pb.SegmentRef, error) {
	seg, err := a.session.Memory.Registry.FindPurpose("G")
	if err != nil {
		return nil, fmt.Errorf("no usable segment with purpose G: %w", err)
	}
	r, err := seg.Arena()
	if err != nil {
		return nil, err
	}
	// Validate metadata before allocating: a returned loan belongs to the host.
	n, err := payload.ByteLen(shape, pb.DType(dtype))
	if err != nil || n != uint64(len(b)) {
		return nil, errors.New("reader payload disagrees with shape")
	}
	d, err := a.session.WriteLoan(seg.ID(), r.Index, b)
	if err != nil {
		return nil, err
	}
	return &pb.SegmentRef{SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length, ShapeZyx: shape, Dtype: pb.DType(dtype)}, nil
}
func (a *application) readVolume(ctx context.Context, _ *control.Client, q *pb.ReadVolume) (*pb.SegmentRef, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.reader == nil || q.Handle != a.readerHandle {
		return nil, notFound(q.Handle)
	}
	c := a.reader
	n, _ := c.Header.blockSize()
	if n > maxPayload {
		return nil, fail(pb.Error_IO_ERROR, fmt.Errorf("%s: (t=%d, c=%d) volume exceeds %d-byte copy budget", c.Path, q.T, q.C, maxPayload))
	}
	b, err := c.Volume(q.T, q.C, a.verify)
	if err == nil {
		var ref *pb.SegmentRef
		ref, err = a.deposit(b, slices.Clone(c.Header.Shape[2:]), c.Header.DType)
		if err == nil {
			return ref, nil
		}
	}
	return nil, fail(pb.Error_IO_ERROR, fmt.Errorf("%s: (t=%d, c=%d): %w", c.Path, q.T, q.C, err))
}
func (a *application) readPlane(ctx context.Context, _ *control.Client, q *pb.ReadPlane) (*pb.SegmentRef, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.reader == nil || q.Handle != a.readerHandle {
		return nil, notFound(q.Handle)
	}
	c := a.reader
	n, _ := c.Header.blockSize()
	if n/uint64(c.Header.Shape[2]) > maxPayload {
		return nil, fail(pb.Error_IO_ERROR, fmt.Errorf("%s: (t=%d, c=%d) plane exceeds copy budget", c.Path, q.T, q.C))
	}
	b, err := c.Plane(q.T, q.Z, q.C)
	if err == nil {
		var ref *pb.SegmentRef
		ref, err = a.deposit(b, []uint32{1, c.Header.Shape[3], c.Header.Shape[4]}, c.Header.DType)
		if err == nil {
			return ref, nil
		}
	}
	return nil, fail(pb.Error_IO_ERROR, fmt.Errorf("%s: (t=%d, c=%d): %w", c.Path, q.T, q.C, err))
}
func (a *application) metadata(ctx context.Context, _ *control.Client, q *pb.Metadata) (*pb.MetadataResult, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.reader == nil || q.Handle != a.readerHandle {
		return nil, notFound(q.Handle)
	}
	if a.reader.Header.Flags&2 != 0 {
		return &pb.MetadataResult{}, nil
	}
	return &pb.MetadataResult{Form: a.reader.Header.Form, Text: a.reader.Header.Text}, nil
}
func (a *application) closeReader(ctx context.Context, _ *control.Client, q *pb.CloseReader) (*pb.Closed, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	c := a.readers[q.Handle]
	if c == nil {
		return &pb.Closed{Handle: q.Handle}, nil
	}
	delete(a.readers, q.Handle)
	err := c.Close()
	if q.Handle == a.readerHandle {
		a.reader, a.readerHandle = nil, ""
		a.head = "No crate open"
		a.vox = "(unknown)"
		err = errors.Join(err, a.pushLocked())
	}
	if err != nil {
		return nil, err
	}
	return &pb.Closed{Handle: q.Handle}, nil
}
func (a *application) openExport(ctx context.Context, _ *control.Client, q *pb.OpenExport) (*pb.ExportOpened, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if q.ExporterId != "crate" {
		return nil, notFound(q.ExporterId)
	}
	if len(q.ShapeTczyx) != 5 {
		return nil, fail(pb.Error_INVALID_ARGUMENT, errors.New("export requires TCZYX shape"))
	}
	h := header{DType: uint16(q.Dtype), Flags: 2, Form: q.GetSourceMetadata().GetForm(), Text: q.GetSourceMetadata().GetText(), Provenance: q.Provenance}
	for _, channel := range q.Channels {
		name := ""
		if channel.Declared() {
			name = channel.Name
		}
		h.Names = append(h.Names, name)
	}
	copy(h.Shape[:], q.ShapeTczyx)
	if _, err := itemSize(h.DType); err != nil {
		return nil, fail(pb.Error_UNSUPPORTED, fmt.Errorf("%s: %w", q.Locator, err))
	}
	var sources [4]string
	h.Spacing, sources = exportSpacing([4]float64{q.SpacingZ, q.SpacingY, q.SpacingX, q.TimeIncrement}, q.SpacingUnit, q.TimeUnit)
	if h.Spacing != [4]float64{} && sources != [4]string{"fields", "fields", "fields", "fields"} {
		log.Printf("%s: export calibration Z/Y/X/time=%v, sources=%v", q.Locator, h.Spacing, sources)
	}
	// Do not truncate an input file or another active export through path aliases.
	if dest, err := os.Stat(q.Locator); err == nil {
		if a.reader != nil {
			src, e := a.reader.file.Stat()
			if e == nil && os.SameFile(src, dest) {
				return nil, fail(pb.Error_INVALID_ARGUMENT, errors.New("export target is the open input file"))
			}
		}
		for _, e := range a.exports {
			src, x := e.writer.file.Stat()
			if x == nil && os.SameFile(src, dest) {
				return nil, fail(pb.Error_INVALID_ARGUMENT, errors.New("export target already has an active export"))
			}
		}
	}
	w, err := newWriter(q.Locator, h)
	if err != nil {
		return nil, fail(pb.Error_IO_ERROR, err)
	}
	a.nextExport++
	handle := fmt.Sprintf("export-%d", a.nextExport)
	a.exports[handle] = &exportJob{writer: w, opened: proto.Clone(q).(*pb.OpenExport)}
	return &pb.ExportOpened{Handle: handle}, nil
}
func rankIndices(old []uint32, in []uint32, limit uint32, axis string) ([]uint32, []uint32, error) {
	next := slices.Clone(old)
	ranks := make([]uint32, len(in))
	for i, n := range in {
		p := slices.Index(next, n)
		if p < 0 {
			if uint64(len(next)) >= uint64(limit) {
				return nil, nil, fail(pb.Error_OUT_OF_BOUNDS, fmt.Errorf("%s source index %d exceeds declared distinct %s count %d", axis, n, axis, limit))
			}
			p = len(next)
			next = append(next, n)
		}
		ranks[i] = uint32(p)
	}
	return next, ranks, nil
}
func (a *application) exportChunk(ctx context.Context, _ *control.Client, q *pb.ExportChunk) (*pb.ChunkWritten, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	job := a.exports[q.Handle]
	if job == nil {
		return nil, notFound(q.Handle)
	}
	ts, tr, err := rankIndices(job.times, []uint32{q.T}, job.writer.Header.Shape[0], "t")
	if err != nil {
		return nil, err
	}
	cs, cr, err := rankIndices(job.channels, q.Channels, job.writer.Header.Shape[1], "c")
	if err != nil {
		return nil, err
	}
	tensor, err := payload.CopyExportChunk(a.session.Memory, job.opened, q, maxPayload)
	if err != nil {
		return nil, fail(pb.Error_INVALID_ARGUMENT, fmt.Errorf("%s: (t=%d, channels=%v): %w", job.writer.Path, q.T, q.Channels, err))
	}
	b := tensor.Bytes()
	n, _ := job.writer.Header.blockSize()
	for i, ch := range cr {
		if err = job.writer.Write(tr[0], ch, b[uint64(i)*n:uint64(i+1)*n], job.opened.LittleEndian); err != nil {
			return nil, fail(pb.Error_IO_ERROR, err)
		}
	}
	job.times, job.channels = ts, cs
	return &pb.ChunkWritten{Handle: q.Handle, TotalBytes: uint64(len(b))}, nil
}
func (a *application) finishExport(ctx context.Context, _ *control.Client, q *pb.FinishExport) (*pb.ExportDone, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	job := a.exports[q.Handle]
	if job == nil {
		return nil, notFound(q.Handle)
	}
	delete(a.exports, q.Handle)
	if err := job.writer.Finish(); err != nil {
		a.status = "Export failed: " + err.Error()
		return nil, fail(pb.Error_IO_ERROR, errors.Join(err, a.pushLocked()))
	}
	blockUnit := "blocks"
	if len(job.writer.Index) == 1 {
		blockUnit = "block"
	}
	a.status = fmt.Sprintf("Exported %s (%d %s)", job.writer.Path, len(job.writer.Index), blockUnit)
	if job.writer.Header.Spacing == [4]float64{} {
		a.status += " without voxel size — the host supplied no spacing."
	} else {
		a.status += "; voxel size " + spacingLabel(job.writer.Header.Spacing)
	}
	if err := a.pushLocked(); err != nil {
		return nil, err
	}
	return &pb.ExportDone{Handle: q.Handle}, nil
}
func (a *application) abortExport(ctx context.Context, _ *control.Client, q *pb.AbortExport) (*pb.ExportDone, error) {
	if err := a.wait(ctx); err != nil {
		return nil, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if job := a.exports[q.Handle]; job != nil {
		delete(a.exports, q.Handle)
		if err := job.writer.Abort(); err != nil {
			return nil, fail(pb.Error_IO_ERROR, err)
		}
	}
	return &pb.ExportDone{Handle: q.Handle}, nil
}
func (a *application) setVerify(ctx context.Context, _ *control.Client, args plugin.Args) (plugin.Result, error) {
	if err := a.wait(ctx); err != nil {
		return plugin.Result{}, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	v, err := args.Bool("value")
	if err == nil {
		a.verify = v
	}
	patch := a.pushLocked()
	return plugin.Result{}, errors.Join(err, patch)
}
func (a *application) schedule(ctx context.Context, message string, work func(context.Context, *control.Client) error) (plugin.Result, error) {
	if err := a.wait(ctx); err != nil {
		return plugin.Result{}, err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	if a.closing {
		return plugin.Result{}, errors.New("shutting down")
	}
	if a.busy {
		return plugin.Result{}, errors.New("busy — a job is already running")
	}
	err := control.AfterReply(ctx, func(_ context.Context, c *control.Client) (result error) {
		a.mu.Lock()
		if a.closing {
			a.mu.Unlock()
			return nil
		}
		a.workers.Add(1)
		workctx := a.ctx
		a.mu.Unlock()
		defer a.workers.Done()
		defer func() { a.mu.Lock(); a.busy = false; a.mu.Unlock() }()
		defer func() {
			if p := recover(); p != nil {
				result = fmt.Errorf("job panic: %v", p)
			}
			if result != nil && workctx.Err() == nil {
				result = errors.Join(result, a.note("Failed: "+result.Error()))
			}
		}()
		return work(workctx, c)
	})
	if err != nil {
		return plugin.Result{}, err
	}
	a.busy = true
	return plugin.Result{Message: message}, nil
}
func (a *application) describe(ctx context.Context, c *control.Client) error {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()
	v, err := c.GetViewState(ctx)
	if err != nil {
		return err
	}
	channel := uint32(0)
	if len(v.VisibleChannels) != 0 {
		channel = v.VisibleChannels[0]
	}
	volume, err := payload.ReadVolume(ctx, c, a.session.Memory, &pb.ReadDataVolume{T: v.T, C: channel, Level: 0}, payload.CopyOptions{MaxBytes: maxPayload, ByteOrder: payload.LittleEndian})
	var b []byte
	dtype := uint16(v.Dtype)
	path := "read via host data access"
	vox := "unknown — the host publishes no voxel size to a plugin without a tool"
	if err != nil {
		var he *control.HostError
		if !errors.As(err, &he) || he.Kind != pb.Error_UNSUPPORTED {
			return err
		}
		a.mu.Lock()
		if a.reader == nil {
			a.mu.Unlock()
			return errors.New("host data access unavailable and no crate is open")
		}
		n, _ := a.reader.Header.blockSize()
		if n > maxPayload {
			a.mu.Unlock()
			return errors.New("direct read exceeds copy budget")
		}
		b, err = a.reader.Volume(v.T, channel, a.verify)
		dtype = a.reader.Header.DType
		vox = spacingLabel(a.reader.Header.Spacing)
		a.mu.Unlock()
		if err != nil {
			return err
		}
		path = "read directly — host data access is unavailable for a dataset this plugin serves"
	} else {
		if volume.OpenGeneration != v.OpenGeneration {
			return errors.New("dataset changed during describe")
		}
		b = volume.Tensor.Bytes()
		dtype = uint16(volume.Tensor.DType())
	}
	min, max, mean, err := statistics(b, dtype, true)
	if err != nil {
		return err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.vox = vox
	a.status = fmt.Sprintf("min %.9g, max %.9g, mean %.9g; %s", min, max, mean, path)
	return a.pushLocked()
}
func statistics(b []byte, dtype uint16, little bool) (min, max, mean float64, err error) {
	n, err := itemSize(dtype)
	if dtype == 4 {
		n = 8
		err = nil
	}
	if err != nil {
		return 0, 0, 0, err
	}
	if len(b) == 0 || uint64(len(b))%n != 0 {
		return 0, 0, 0, errors.New("invalid statistics payload")
	}
	var order binary.ByteOrder = le
	if !little {
		order = binary.BigEndian
	}
	min, max = math.Inf(1), math.Inf(-1)
	sum := 0.0
	for i := 0; i < len(b); i += int(n) {
		var v float64
		switch dtype {
		case 1:
			v = float64(b[i])
		case 2:
			v = float64(order.Uint16(b[i:]))
		case 3:
			v = float64(math.Float32frombits(order.Uint32(b[i:])))
		case 4:
			v = math.Float64frombits(order.Uint64(b[i:]))
		}
		if math.IsNaN(v) {
			return math.NaN(), math.NaN(), math.NaN(), nil
		}
		if v < min {
			min = v
		}
		if v > max {
			max = v
		}
		sum += v
	}
	return min, max, sum / float64(uint64(len(b))/n), nil
}
func (a *application) pickCalibration(ctx context.Context, c *control.Client) error {
	picked, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "calibration_override", ForceReprompt: true})
	if err != nil {
		return err
	}
	if picked.Declined {
		return nil
	}
	cal, err := loadCalibration(picked.Path)
	if err != nil {
		return err
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	a.override = cal
	a.overrideName = filepath.Base(picked.Path)
	a.calLabel = "override: " + a.overrideName
	a.status = "Calibration selected: " + picked.Path
	return a.pushLocked()
}
func (a *application) checkFile(ctx context.Context, c *control.Client) error {
	picked, err := c.RequestAsset(ctx, &pb.RequestAsset{Slot: "crate_check_file", ForceReprompt: true})
	if err != nil {
		return err
	}
	if picked.Declined {
		return nil
	}
	s, _, err := checkCrate(picked.Path)
	if err != nil {
		return err
	}
	return a.note(s)
}
func (a *application) shutdown(ctx context.Context) error {
	a.mu.Lock()
	a.closing = true
	if a.cancel != nil {
		a.cancel()
	}
	a.mu.Unlock()
	done := make(chan struct{})
	go func() { a.workers.Wait(); close(done) }()
	select {
	case <-done:
	case <-ctx.Done():
		return ctx.Err()
	}
	a.mu.Lock()
	defer a.mu.Unlock()
	var err error
	for handle, c := range a.readers {
		err = errors.Join(err, c.Close())
		delete(a.readers, handle)
	}
	a.reader, a.readerHandle = nil, ""
	for h, j := range a.exports {
		err = errors.Join(err, j.writer.Abort())
		delete(a.exports, h)
	}
	return err
}

func (a *application) runtime() (control.Plugin, error) {
	app, base, err := a.declarationRuntime()
	if err != nil {
		return control.Plugin{}, err
	}
	return app.Configure(base)
}
