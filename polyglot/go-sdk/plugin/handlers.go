// Package plugin adapts typed application handlers to control.Config. The
// control package owns workers, correlation, descriptor disposition and session
// lifetime. Definition generates manifests and contribution membership from
// declarations; Handlers remains available for manually authored interfaces.
package plugin

import (
	"context"
	"errors"
	"fmt"
	"maps"
	"strings"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	pb "github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/protocol"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/stage"
	"google.golang.org/protobuf/proto"
)

// RequestHandler sees a typed request and returns its typed response. The
// control worker catches panics, translates errors and assigns correlation IDs.
type RequestHandler[Request, Response proto.Message] func(context.Context, *control.Client, Request) (Response, error)

// ReaderHandlers supplies one contribution's callbacks. With Handlers.Readers,
// Open is selected by reader_id and later requests are routed by wire handle.
// Callback handles are local to the contribution and remain application-owned.
// Readers opens a different URI before closing the previous reader; a failed
// open preserves it. Reloading the same URI closes first. Local handles must
// distinguish overlapping readers within a contribution, and Close must close
// only its named handle. The router supplies unique session wire handles and
// also closes the remaining reader at shutdown.
// Read results name loans in the session's G arena; see reader_writer_contract.md.
// If Plane is nil, Volume supplies a fresh exclusive single-use loan; the SDK
// derives the requested plane in place. ServeEnv supplies the memory integration;
// custom control servers must set Config.ReaderMemory.
// Open receives the file's optional state folder through control.FileStateDir(q).
// control.SettingsDir is also available without a command context.
// OpenSeries alternatively returns ReaderOpenResult with a SeriesChoice; Open
// keeps its existing signature. A selected empty series ID is q.Series != nil
// with *q.Series == "", distinct from an unselected request.
type ReaderHandlers struct {
	// OpenSeries is an alternative to Open for multi-image files. q.Series
	// preserves absence versus an explicitly selected empty ID.
	OpenSeries ReaderOpenHandler
	Open       RequestHandler[*pb.OpenReader, *pb.ReaderOpened]
	Volume     RequestHandler[*pb.ReadVolume, *pb.SegmentRef]
	Plane      RequestHandler[*pb.ReadPlane, *pb.SegmentRef]
	Metadata   RequestHandler[*pb.Metadata, *pb.MetadataResult]
	Close      RequestHandler[*pb.CloseReader, *pb.Closed]
}

// StageHandlers declares planning, optional clean preflight, and borrowed-slot processing.
type StageHandlers = control.StageHandlers

// ExportHandlers leaves handle management and format selection to the
// application. A chunk's descriptor use ends when Chunk returns; copy it before
// retaining data for background work. Abort must abandon, not finalize, output.
// control.FileStateDir on Open's request names the SOURCE file's state folder,
// not the destination locator. SettingsDir creates its folder on first use;
// FileStateDir only returns the host-provided path.
// Open's Filter names the chosen filter; Chunk.FilteredChannels identifies which
// volumes actually passed through it. Other channels are raw, including failed
// or refused timepoints. payload.CopyExportVolumes preserves this per-volume
// provenance with the copied pixels; never infer it from ViewState.Filter.
type ExportHandlers struct {
	Open   RequestHandler[*pb.OpenExport, *pb.ExportOpened]
	Chunk  RequestHandler[*pb.ExportChunk, *pb.ChunkWritten]
	Finish RequestHandler[*pb.FinishExport, *pb.ExportDone]
	Abort  RequestHandler[*pb.AbortExport, *pb.ExportDone]
}

type Result struct {
	Message string
	// AfterReply runs on a separate worker after CommandDone has been sent.
	// Use it for pickers or work that must follow acknowledgement. The session
	// context owns its lifetime; see control.AfterReply for bounds and cleanup.
	AfterReply func(context.Context, *control.Client) error
}
type CommandHandler func(context.Context, *control.Client, Args) (Result, error)
type PanelOpenedHandler func(context.Context, *control.Client) error
type ViewChangedHandler = func(context.Context, *control.Client, *pb.ViewChanged) error
type RegionsChangedHandler = func(context.Context, *control.Client, *pb.RegionsChanged) error
type ShutdownHandler func(context.Context, *control.Client, *pb.Shutdown) error

// ToolLifecycleHandler runs on ServeEnv's listener after an engagement change.
// The binding is a snapshot. Keep callbacks short and avoid blocking host calls.
type ToolLifecycleHandler func(context.Context, *control.Client, *pb.ToolBinding) error

type ShapeHandler = control.ShapeHandler
type ShapeEditEndedHandler = func(context.Context, *control.Client, *pb.ShapeEditEnded) error

type Handlers struct {
	Shapers        map[string]ShapeHandler
	ShapeEditEnded ShapeEditEndedHandler
	series         *seriesState
	Follows        map[string]FollowHandler
	Stages         map[string]StageHandlers
	ChunkOverlays  map[string]*ChunkOverlay
	Commands       map[string]CommandHandler
	Reader         ReaderHandlers // raw single-dispatcher escape hatch; exclusive with Readers
	Readers        map[string]ReaderHandlers
	Export         ExportHandlers
	Panels         map[string]PanelOpenedHandler
	ViewChanged    ViewChangedHandler
	RegionsChanged RegionsChangedHandler
	PeerViolation  func(context.Context, *control.Client, *pb.ContractViolation) error
	Shutdown       ShutdownHandler
	Activated      map[string]ToolLifecycleHandler
	Deactivated    map[string]ToolLifecycleHandler
}

// Configure snapshots registration maps and fills application dispatch hooks.
// It refuses to overwrite an existing dispatcher or shutdown hook. Other control
// configuration, including lifecycle integration, is preserved. Build one config
// per session; callbacks must synchronize any application state they share with
// panel notifications, shutdown or background work.
func (h Handlers) Configure(base control.Config) (control.Config, error) {
	if base.HandleRequest != nil || base.OnMessage != nil || (h.Shutdown != nil && base.Shutdown != nil) {
		return control.Config{}, errors.New("typed handlers would replace an existing application hook")
	}
	if (h.ViewChanged != nil && base.ViewChanged != nil) || (h.RegionsChanged != nil && base.RegionsChanged != nil) {
		return control.Config{}, errors.New("typed handlers would replace subscribed state hooks")
	}
	if h.ViewChanged != nil {
		base.ViewChanged = h.ViewChanged
	}
	if h.RegionsChanged != nil {
		base.RegionsChanged = h.RegionsChanged
	}
	if len(h.Shapers) > 0 && len(base.Shapers) > 0 {
		return control.Config{}, errors.New("typed shapers would replace existing shapers")
	}
	if h.ShapeEditEnded != nil && base.ShapeEditEnded != nil {
		return control.Config{}, errors.New("typed shape end hook would replace existing hook")
	}
	if len(h.Shapers) > 0 {
		base.Shapers = maps.Clone(h.Shapers)
	}
	if h.ShapeEditEnded != nil {
		base.ShapeEditEnded = h.ShapeEditEnded
	}
	h.ChunkOverlays = maps.Clone(h.ChunkOverlays)
	if len(h.ChunkOverlays) > 0 && base.ChunksWanted != nil {
		return control.Config{}, errors.New("typed handlers would replace chunk want hook")
	}
	for id, o := range h.ChunkOverlays {
		if o == nil || id != o.id {
			return control.Config{}, errors.New("chunk overlays require matching IDs and handles")
		}
	}
	h.Readers = maps.Clone(h.Readers)
	var stageErr error
	h.Stages, stageErr = control.CloneStageHandlers(h.Stages)
	if stageErr != nil {
		return control.Config{}, stageErr
	}
	if len(h.Stages) > 0 && len(base.Stages) > 0 {
		return control.Config{}, errors.New("typed stages would replace existing stage handlers")
	}
	if len(h.Stages) > 0 {
		base.Stages, stageErr = control.CloneStageHandlers(h.Stages)
		if stageErr != nil {
			return control.Config{}, stageErr
		}
	}
	for id, handlers := range h.Stages {
		if strings.TrimSpace(id) == "" || (handlers.Plan == nil) == (handlers.PlanWithParams == nil) {
			return control.Config{}, errors.New("stages require nonblank IDs and exactly one of Plan or PlanWithParams")
		}
	}
	h.Commands = maps.Clone(h.Commands)
	h.Panels = maps.Clone(h.Panels)
	h.Activated = maps.Clone(h.Activated)
	h.Deactivated = maps.Clone(h.Deactivated)
	if (len(h.Activated) > 0 && base.ToolActivated != nil) || (len(h.Deactivated) > 0 && base.ToolDeactivated != nil) {
		return control.Config{}, errors.New("typed handlers would replace an existing tool lifecycle hook")
	}
	for _, hooks := range []map[string]ToolLifecycleHandler{h.Activated, h.Deactivated} {
		for id, f := range hooks {
			if id == "" || f == nil {
				return control.Config{}, errors.New("tool lifecycle hooks require nonempty IDs and handlers")
			}
		}
	}
	if len(h.Activated) > 0 {
		base.ToolActivated = func(ctx context.Context, c *control.Client, b *pb.ToolBinding) error {
			if f := h.Activated[b.ToolId]; f != nil {
				return f(ctx, c, b)
			}
			return nil
		}
	}
	if len(h.Deactivated) > 0 {
		base.ToolDeactivated = func(ctx context.Context, c *control.Client, b *pb.ToolBinding) error {
			if f := h.Deactivated[b.ToolId]; f != nil {
				return f(ctx, c, b)
			}
			return nil
		}
	}
	for id, f := range h.Commands {
		if id == "" || f == nil {
			return control.Config{}, errors.New("commands require nonempty IDs and handlers")
		}
	}
	for id, f := range h.Panels {
		if id == "" || f == nil {
			return control.Config{}, errors.New("panels require nonempty IDs and handlers")
		}
	}
	if len(h.Readers) == 0 {
		if h.Reader.Open != nil && h.Reader.OpenSeries != nil {
			return control.Config{}, errors.New("Open and OpenSeries are mutually exclusive")
		}
		h.Reader = trackReaderGeometry(h.Reader)
		h.series = &seriesState{}
	}
	base.HandleRequest = h.handle
	if len(h.Readers) > 0 {
		if h.Reader.Open != nil || h.Reader.OpenSeries != nil || h.Reader.Volume != nil || h.Reader.Plane != nil || h.Reader.Metadata != nil || h.Reader.Close != nil {
			return control.Config{}, errors.New("Reader and Readers are mutually exclusive")
		}
		for id, handlers := range h.Readers {
			if id == "" || (handlers.Open == nil) == (handlers.OpenSeries == nil) {
				return control.Config{}, errors.New("readers require nonempty IDs and exactly one of Open or OpenSeries")
			}
		}
		router := &readerRouter{readers: h.Readers, handles: make(map[string]readerBinding)}
		base.HandleRequest = func(ctx context.Context, c *control.Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
			if in != nil {
				switch in.Msg.(type) {
				case *pb.HostMsg_OpenReader, *pb.HostMsg_ReadVolume, *pb.HostMsg_ReadPlane, *pb.HostMsg_Metadata, *pb.HostMsg_CloseReader:
					return router.handle(ctx, c, in)
				}
			}
			return h.handle(ctx, c, in)
		}
		shutdown := base.Shutdown
		if h.Shutdown != nil {
			shutdown = h.Shutdown
		}
		base.Shutdown = func(ctx context.Context, c *control.Client, q *pb.Shutdown) error {
			err := router.closeAll(ctx, c)
			if shutdown != nil {
				err = errors.Join(err, shutdown(ctx, c, q))
			}
			return err
		}
	}
	base.OnMessage = h.notice
	if h.Shutdown != nil && len(h.Readers) == 0 {
		base.Shutdown = h.Shutdown
	}
	if len(h.Readers) == 0 && h.Reader.OpenSeries != nil {
		shutdown := base.Shutdown
		base.Shutdown = func(ctx context.Context, c *control.Client, q *pb.Shutdown) error {
			h.series.take(c, nil)
			if shutdown != nil {
				return shutdown(ctx, c, q)
			}
			return nil
		}
	}
	if len(h.ChunkOverlays) > 0 {
		base.ChunksWanted = func(_ *control.Client, q *pb.ChunksWanted) error {
			if o := h.ChunkOverlays[q.GetOverlayId()]; o != nil {
				return o.wanted(q)
			}
			return notFound("chunk overlay", q.GetOverlayId())
		}
		stop := func() {
			for _, o := range h.ChunkOverlays {
				o.Stop()
			}
		}
		shutdown, end := base.Shutdown, base.End
		base.Shutdown = func(ctx context.Context, c *control.Client, q *pb.Shutdown) error {
			stop()
			if shutdown != nil {
				return shutdown(ctx, c, q)
			}
			return nil
		}
		base.End = func(err error) {
			stop()
			if end != nil {
				end(err)
			}
		}
	}
	return configureFollows(base, h.Follows)
}

func unsupported(name string) error {
	return &control.HostError{Kind: pb.Error_UNSUPPORTED, Message: "no handler registered for " + name}
}
func notFound(kind, id string) error {
	return &control.HostError{Kind: pb.Error_NOT_FOUND, Message: fmt.Sprintf("unknown %s %q", kind, id)}
}

func invoke[Q, R proto.Message](ctx context.Context, c *control.Client, q Q, f RequestHandler[Q, R], name string, wrap func(R) *pb.PluginMsg) (*pb.PluginMsg, error) {
	if !q.ProtoReflect().IsValid() {
		return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "empty " + name + " request"}
	}
	switch read := proto.Message(q).(type) {
	case *pb.ReadVolume:
		if read.Level != 0 {
			return nil, &control.HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "reader supports only level 0"}
		}
	case *pb.ReadPlane:
		if read.Level != 0 {
			return nil, &control.HostError{Kind: pb.Error_OUT_OF_BOUNDS, Message: "reader supports only level 0"}
		}
	}
	if f == nil {
		return nil, unsupported(name)
	}
	r, err := f(ctx, c, q)
	if err != nil {
		return nil, err
	}
	if !r.ProtoReflect().IsValid() {
		return nil, errors.New(name + " handler returned no response")
	}
	return wrap(r), nil
}

func (h Handlers) handle(ctx context.Context, c *control.Client, in *pb.HostMsg) (*pb.PluginMsg, error) {
	if in == nil {
		return nil, errors.New("nil host request")
	}
	switch x := in.Msg.(type) {
	case *pb.HostMsg_PlanStage:
		q := x.PlanStage
		handlers, declared := h.Stages[q.GetStageId()]
		if !declared {
			return nil, &control.HostError{Kind: pb.Error_NOT_FOUND, Message: "undeclared processing stage: " + q.GetStageId()}
		}
		if err := stage.ValidateShape(q.GetInput()); err != nil {
			return nil, &control.HostError{Kind: pb.Error_PROTOCOL, Message: err.Error()}
		}
		input := proto.Clone(q.Input).(*pb.StageShape)
		plan, err := handlers.PlanRequest(ctx, c, q)
		if err != nil {
			return nil, err
		}
		if err := stage.ValidatePlan(input, plan); err != nil {
			return nil, &control.HostError{Kind: stage.PlanErrorKind(err), Message: err.Error()}
		}
		return &pb.PluginMsg{Msg: &pb.PluginMsg_StagePlanned{StagePlanned: plan}}, nil
	case *pb.HostMsg_InvokeCommand:
		if x.InvokeCommand == nil {
			return nil, &control.HostError{Kind: pb.Error_INVALID_ARGUMENT, Message: "empty command request"}
		}
		f := h.Commands[x.InvokeCommand.CommandId]
		if f == nil {
			return nil, notFound("command", x.InvokeCommand.CommandId)
		}
		ctx = context.WithValue(ctx, commandCauseKey{}, x.InvokeCommand.Cause)
		result, err := f(ctx, c, Args(x.InvokeCommand.Args))
		if err != nil {
			return nil, err
		}
		if result.AfterReply != nil {
			after := result.AfterReply
			cause := x.InvokeCommand.Cause
			if err := control.AfterReply(ctx, func(ctx context.Context, c *control.Client) error {
				return after(context.WithValue(ctx, commandCauseKey{}, cause), c)
			}); err != nil {
				return nil, err
			}
		}
		return &pb.PluginMsg{Msg: &pb.PluginMsg_CommandDone{CommandDone: &pb.CommandDone{CommandId: x.InvokeCommand.CommandId, Ok: true, Message: result.Message}}}, nil
	case *pb.HostMsg_OpenReader:
		if h.Reader.OpenSeries != nil && h.series == nil {
			return nil, errors.New("OpenSeries requires Handlers.Configure")
		}
		state := h.series
		if state == nil {
			state = &seriesState{}
		}
		return state.open(ctx, c, h.Reader, x.OpenReader, nil)
	case *pb.HostMsg_ReadVolume:
		return invoke(ctx, c, x.ReadVolume, h.Reader.Volume, "read_volume", func(r *pb.SegmentRef) *pb.PluginMsg { return &pb.PluginMsg{Msg: &pb.PluginMsg_Segment{Segment: r}} })
	case *pb.HostMsg_ReadPlane:
		plane := h.Reader.Plane
		if plane == nil && h.Reader.Volume != nil {
			plane = func(ctx context.Context, c *control.Client, q *pb.ReadPlane) (*pb.SegmentRef, error) {
				return c.DeriveReaderPlane(ctx, q, h.Reader.Volume)
			}
		}
		return invoke(ctx, c, x.ReadPlane, plane, "read_plane", func(r *pb.SegmentRef) *pb.PluginMsg { return &pb.PluginMsg{Msg: &pb.PluginMsg_Segment{Segment: r}} })
	case *pb.HostMsg_Metadata:
		return invoke(ctx, c, x.Metadata, h.Reader.Metadata, "metadata", func(r *pb.MetadataResult) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_Metadata{Metadata: r}}
		})
	case *pb.HostMsg_CloseReader:
		return invoke(ctx, c, x.CloseReader, h.Reader.Close, "close_reader", func(r *pb.Closed) *pb.PluginMsg { return &pb.PluginMsg{Msg: &pb.PluginMsg_Closed{Closed: r}} })
	case *pb.HostMsg_OpenExport:
		return invoke(ctx, c, x.OpenExport, h.Export.Open, "open_export", func(r *pb.ExportOpened) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportOpened{ExportOpened: r}}
		})
	case *pb.HostMsg_ExportChunk:
		return invoke(ctx, c, x.ExportChunk, h.Export.Chunk, "export_chunk", func(r *pb.ChunkWritten) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ChunkWritten{ChunkWritten: r}}
		})
	case *pb.HostMsg_FinishExport:
		return invoke(ctx, c, x.FinishExport, h.Export.Finish, "finish_export", func(r *pb.ExportDone) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportDone{ExportDone: r}}
		})
	case *pb.HostMsg_AbortExport:
		return invoke(ctx, c, x.AbortExport, h.Export.Abort, "abort_export", func(r *pb.ExportDone) *pb.PluginMsg {
			return &pb.PluginMsg{Msg: &pb.PluginMsg_ExportDone{ExportDone: r}}
		})
	default:
		return nil, unsupported("host request kind")
	}
}

func (h Handlers) notice(ctx context.Context, c *control.Client, in *pb.HostMsg) error {
	if in == nil {
		return errors.New("nil host notice")
	}
	switch x := in.Msg.(type) {
	case *pb.HostMsg_PanelOpened:
		if x.PanelOpened == nil {
			return errors.New("empty panel-open notice")
		}
		if f := h.Panels[x.PanelOpened.PanelId]; f != nil {
			return f(ctx, c)
		}
		// Registering a panel-open hook is optional; a panel can be entirely static.
		return nil
	case *pb.HostMsg_ContractViolation:
		if x.ContractViolation == nil {
			return errors.New("empty peer violation")
		}
		if h.PeerViolation != nil {
			return h.PeerViolation(ctx, c, x.ContractViolation)
		}
		return fmt.Errorf("host reported contract violation: %s", x.ContractViolation)
	default:
		return unsupported("host notice kind")
	}
}
