package plugin

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"slices"
	"strings"
	"unicode"

	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/control"
	"github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/manifest"
)

type Info struct {
	ID, Name, Version string
	// StartupSeconds optionally advertises expected spawn-to-Ready seconds.
	// It must be positive and finite. The host may honor, cap or ignore it.
	StartupSeconds *float64
	// Engines is engines.vizi, the oldest host protocol the plugin was tested
	// on (default "1.0"). Use Supports for anything newer.
	Engines string
	// MinVizi names the oldest vizi application release the plugin runs in, a
	// PEP 440 version (manifest min_vizi). Empty writes DefaultMinVizi.
	MinVizi string
}

// DefaultMinVizi is the min_vizi the writer emits when Info.MinVizi is empty:
// vizi 1.0.0, the lowest release that speaks protocol 1.0.
const DefaultMinVizi = "1.0.0"

type CommandSpec = manifest.Command
type ReaderSpec = manifest.Reader
type ExporterSpec = manifest.Exporter
type RegionTypeSpec = manifest.RegionType
type StageSpec = manifest.Stage
type StageParam = manifest.StageParam
type OverlaySpec = manifest.Overlay
type PaneButton = manifest.PaneButton
type Trigger = manifest.Trigger
type AssetSpec = manifest.Asset

const (
	DataAccess     = "data_access"
	ContributeROIs = "contribute_rois"
)

// ToolsMenu explicitly lists a command or tool; the default nil menu hides it.
func ToolsMenu() *string { return manifest.MenuTools() }

type definitionID struct{ marker byte }

// Definition collects a plugin's interface and callbacks together. Construct it
// on one goroutine without starting workers or connecting to the host. Build
// freezes a snapshot; further registration is a programming error and panics.
// Callback closures remain application-owned and must synchronize shared state.
type Definition struct {
	chunkBindings map[string]func(*ChunkOverlay)
	owner         *definitionID
	m             manifest.Manifest
	h             Handlers
	tools         []toolDefinition
	exporters     map[string]ExportHandlers
	resources     map[string][]byte
	err           error
	built         *Application
}

// Application is the immutable interface produced by Build. Configure creates
// fresh SDK routing state per session. Captured callback state is not cloned.
type Application struct {
	chunkBindings map[string]func(*ChunkOverlay)
	owner         *definitionID
	m             manifest.Manifest
	h             Handlers
	tools         []toolDefinition
	exporters     map[string]ExportHandlers
	resources     map[string][]byte
}

func NewDefinition(info Info) *Definition {
	engines := info.Engines
	if engines == "" {
		engines = "1.0"
	}
	minVizi := info.MinVizi
	if minVizi == "" {
		minVizi = DefaultMinVizi
	}
	return &Definition{owner: &definitionID{}, m: manifest.Manifest{ManifestVersion: manifest.Version, ID: info.ID, Name: info.Name, Version: info.Version, Engines: manifest.Engines{Vizi: engines}, MinVizi: minVizi, SDKVersion: control.SDKVersion, Entry: manifest.Entry{Command: []string{"<not installed>"}, StartupSeconds: info.StartupSeconds}},
		h: Handlers{Commands: map[string]CommandHandler{}, Readers: map[string]ReaderHandlers{}, Panels: map[string]PanelOpenedHandler{}}, exporters: map[string]ExportHandlers{}, resources: map[string][]byte{}}
}
func (d *Definition) open() {
	if d.built != nil {
		panic("plugin Definition is built; cannot register more contributions")
	}
}
func (d *Definition) fail(err error) { d.err = errors.Join(d.err, err) }

// CommandRef connects widgets to exactly one definition's registered command.
// Its zero value and references from another definition are refused by Build.
type CommandRef struct {
	owner *definitionID
	id    string
}

func (r CommandRef) ID() string { return r.id }
func (d *Definition) Command(spec CommandSpec, handler CommandHandler) CommandRef {
	d.open()
	if handler == nil {
		d.fail(fmt.Errorf("command %q needs a handler", spec.ID))
	}
	if spec.Title == "" {
		spec.Title = spec.ID
	}
	d.m.Contributions.Commands = append(d.m.Contributions.Commands, spec)
	d.h.Commands[spec.ID] = handler
	return CommandRef{d.owner, spec.ID}
}
func (d *Definition) Reader(spec ReaderSpec, handlers ReaderHandlers) {
	d.open()
	if (handlers.Open == nil) == (handlers.OpenSeries == nil) {
		d.fail(fmt.Errorf("reader %q needs exactly one of Open or OpenSeries", spec.ID))
	}
	spec.Extensions = slices.Clone(spec.Extensions)
	for i, e := range spec.Extensions {
		spec.Extensions[i] = manifest.NormalizeSuffix(e)
	}
	spec.FallbackExtensions = slices.Clone(spec.FallbackExtensions)
	for i, e := range spec.FallbackExtensions {
		spec.FallbackExtensions[i] = manifest.NormalizeSuffix(e)
	}
	d.m.Contributions.Readers = append(d.m.Contributions.Readers, spec)
	d.h.Readers[spec.ID] = handlers
}

// RegionType declares an asked type and its optional shaper. A nil shaper
// explicitly rejects commits until one is supplied. Request ContributeROIs too.
func (d *Definition) RegionType(spec RegionTypeSpec, handler ShapeHandler) {
	d.open()
	if spec.Name == "" {
		spec.Name = spec.ID
	}
	if d.h.Shapers == nil {
		d.h.Shapers = map[string]ShapeHandler{}
	}
	d.m.Contributions.RegionTypes = append(d.m.Contributions.RegionTypes, spec)
	d.h.Shapers[spec.ID] = handler
}

// OnShapeEditEnded observes the host's COMMITTED, REVERTED or CANCELLED outcome.
func (d *Definition) OnShapeEditEnded(handler ShapeEditEndedHandler) {
	d.open()
	if handler == nil || d.h.ShapeEditEnded != nil {
		d.fail(errors.New("shape end hook requires one nonnil handler"))
		return
	}
	d.h.ShapeEditEnded = handler
}

// Stage declares a processing stage, its parameters and its callbacks together.
// Parameters in spec supply both the manifest and runtime validation.
func (d *Definition) Stage(spec StageSpec, handlers StageHandlers) {
	d.open()
	if (handlers.Plan == nil) == (handlers.PlanWithParams == nil) {
		d.fail(fmt.Errorf("stage %q needs exactly one of Plan or PlanWithParams", spec.ID))
	}
	if spec.Name == "" {
		spec.Name = spec.ID
	}
	if d.h.Stages == nil {
		d.h.Stages = map[string]StageHandlers{}
	}
	if len(handlers.Params) > 0 {
		d.fail(fmt.Errorf("stage %q: declare params only in StageSpec", spec.ID))
	}
	spec.Params = manifest.CloneStageParams(spec.Params)
	handlers.Params = manifest.CloneStageParams(spec.Params)
	d.m.Contributions.Stages = append(d.m.Contributions.Stages, spec)
	d.h.Stages[spec.ID] = handlers
}
func (d *Definition) Exporter(spec ExporterSpec, handlers ExportHandlers) {
	d.open()
	if handlers.Open == nil || handlers.Chunk == nil || handlers.Finish == nil || handlers.Abort == nil {
		d.fail(fmt.Errorf("exporter %q needs Open, Chunk, Finish and Abort", spec.ID))
	}
	spec.Extension = manifest.NormalizeSuffix(spec.Extension)
	d.m.Contributions.Exporters = append(d.m.Contributions.Exporters, spec)
	d.exporters[spec.ID] = handlers
}

// PanelRef binds the declared panel ID to a runtime patch handle.
type PanelRef struct {
	owner  *definitionID
	id     string
	lists  map[string]int
	cells  map[string]string
	tables map[string][]string
}

func (r PanelRef) ID() string { return r.id }
func (r PanelRef) Bind(c *control.Client) Panel {
	return Panel{ID: r.id, Client: c, lists: r.lists, cells: r.cells, tables: r.tables}
}

type PanelSpec struct {
	FollowTargets        []string
	ViewTargets          []string
	ID, Title, MenuLabel string
	Root                 Widget
}

func (d *Definition) Panel(spec PanelSpec, opened PanelOpenedHandler) PanelRef {
	d.open()
	root, err := spec.Root.declaration(d.owner, 1)
	if err != nil {
		d.fail(err)
	}
	d.m.Contributions.Panels = append(d.m.Contributions.Panels, manifest.Panel{ID: spec.ID, Title: spec.Title, MenuLabel: spec.MenuLabel, Root: root, ViewTargets: slices.Clone(spec.ViewTargets), FollowTargets: slices.Clone(spec.FollowTargets)})
	if opened != nil {
		d.h.Panels[spec.ID] = opened
	}
	lists := map[string]int{}
	cells := map[string]string{}
	tables := map[string][]string{}
	var collect func(manifest.Widget)
	collect = func(w manifest.Widget) {
		if w.Kind == "table" {
			for _, col := range w.Columns {
				tables[w.ID] = append(tables[w.ID], col.ID)
			}
		}
		if w.Kind == "list" {
			lists[w.ID] = w.RowLimit()
			for _, cell := range w.Row {
				cells[cell.ID] = w.ID
			}
		}
		for _, child := range w.Children {
			collect(child)
		}
	}
	collect(root)
	return PanelRef{owner: d.owner, id: spec.ID, lists: lists, cells: cells, tables: tables}
}

// OverlayRef creates the handle for a declared drawing overlay. As with
// NewOverlay, bind exactly once per session and share the returned handle.
type OverlayRef struct {
	owner   *definitionID
	id      string
	chunked bool
}

func (r OverlayRef) ID() string { return r.id }
func (r OverlayRef) Bind(c PushSender) (*Overlay, error) {
	if r.owner == nil || r.chunked {
		return nil, errors.New("drawing handle needs a declared non-chunked overlay")
	}
	return NewOverlay(c, r.id)
}
func (d *Definition) Overlay(spec OverlaySpec) OverlayRef {
	d.open()
	d.m.Contributions.Overlays = append(d.m.Contributions.Overlays, spec)
	return OverlayRef{d.owner, spec.ID, spec.Chunked}
}

// ChunkOverlay declares the overlay and registers a source. Each Configure
// creates its own ChunkOverlay, bound automatically when the session starts.
func (d *Definition) ChunkOverlay(spec OverlaySpec, source ChunkSource) OverlayRef {
	d.open()
	spec.Chunked = true
	if source == nil {
		d.fail(fmt.Errorf("chunk overlay %q needs a source", spec.ID))
	}
	r := d.Overlay(spec)
	if d.h.ChunkOverlays == nil {
		d.h.ChunkOverlays = map[string]*ChunkOverlay{}
	}
	o, _ := NewChunkOverlay(spec.ID, source)
	d.h.ChunkOverlays[spec.ID] = o
	return r
}

// OnChunkOverlay supplies the fresh session handle before its source starts.
// Use it to publish label tables or invalidate wants from application commands.
// The callback runs after the application's Started hook; keep it short.
func (d *Definition) OnChunkOverlay(ref OverlayRef, bind func(*ChunkOverlay)) {
	d.open()
	if ref.owner != d.owner || !ref.chunked || d.h.ChunkOverlays[ref.id] == nil || bind == nil {
		d.fail(errors.New("chunk binding requires this definition's chunk source and a callback"))
		return
	}
	if d.chunkBindings == nil {
		d.chunkBindings = map[string]func(*ChunkOverlay){}
	}
	if d.chunkBindings[ref.id] != nil {
		d.fail(errors.New("duplicate chunk binding"))
		return
	}
	d.chunkBindings[ref.id] = bind
}

func (d *Definition) Needs(permissions ...string) {
	d.open()
	for _, p := range permissions {
		if !slices.Contains(d.m.Permissions, p) {
			d.m.Permissions = append(d.m.Permissions, p)
		}
	}
}

// Epochs subscribes a session, including one with no tools, to EpochChange.
const Epochs = "epochs"
const View = "view"
const Regions = "regions"

// OnViewChanged declares the subscription and data_access permission with its
// callback. Pending views are newest-wins; the callback may call the host.
func (d *Definition) OnViewChanged(handler ViewChangedHandler) {
	d.open()
	if handler == nil || d.h.ViewChanged != nil {
		d.fail(errors.New("view subscription needs one nonnil handler"))
		return
	}
	d.Subscribe(View)
	d.Needs(DataAccess)
	d.h.ViewChanged = handler
}

// OnRegionsChanged declares region updates and data_access together. Pending
// updates merge by region; reset invalidates earlier pending changes.
func (d *Definition) OnRegionsChanged(handler RegionsChangedHandler) {
	d.open()
	if handler == nil || d.h.RegionsChanged != nil {
		d.fail(errors.New("regions subscription needs one nonnil handler"))
		return
	}
	d.Subscribe(Regions)
	d.Needs(DataAccess)
	d.h.RegionsChanged = handler
}

// Subscribe declares host pushes. Unknown names are retained for future hosts;
// hosts warn and ignore names they do not recognize. Epoch acknowledgements are
// handled by the session runtime independently of tool registration.
func (d *Definition) Subscribe(names ...string) {
	d.open()
	for _, name := range names {
		if !slices.Contains(d.m.Subscriptions, name) {
			d.m.Subscriptions = append(d.m.Subscriptions, name)
		}
	}
}

func (d *Definition) Asset(spec AssetSpec) { d.open(); d.m.Assets = append(d.m.Assets, spec) }

// Resource includes an icon or other small supporting file in generated packages.
// Bytes are copied now; paths use forward slashes on every platform. Large
// host-provisioned assets belong in Asset declarations, not Resource.
func (d *Definition) Resource(name string, data []byte) {
	d.open()
	if !manifest.LocalPath(name) {
		d.fail(fmt.Errorf("invalid resource path %q", name))
		return
	}
	if _, ok := d.resources[name]; ok {
		d.fail(fmt.Errorf("duplicate resource %q", name))
		return
	}
	d.resources[name] = slices.Clone(data)
}

// Resources copies named files (for example from an embed.FS) during declaration.
func (d *Definition) Resources(files fs.FS, names ...string) {
	d.open()
	for _, name := range names {
		if files == nil {
			d.fail(errors.New("nil resource filesystem"))
			return
		}
		b, err := fs.ReadFile(files, name)
		if err != nil {
			d.fail(err)
		} else {
			d.Resource(name, b)
		}
	}
}
func (d *Definition) OnShutdown(handler ShutdownHandler) {
	d.open()
	if d.h.Shutdown != nil {
		d.fail(errors.New("shutdown already registered"))
	}
	d.h.Shutdown = handler
}

// Build validates declarations and packaged icons without calling any handlers.
// Both runtime routing and generated manifests use the resulting snapshot.
func (d *Definition) Build() (*Application, error) {
	if d.built != nil {
		return d.built, nil
	}
	if d.err != nil {
		return nil, d.err
	}
	for _, r := range d.m.ID + " " + d.m.Version {
		if unicode.IsControl(r) {
			return nil, errors.New("plugin ID and version cannot contain control characters in the handshake identity")
		}
	}
	b, err := d.m.JSON()
	if err != nil {
		return nil, err
	}
	var m manifest.Manifest
	if err = json.Unmarshal(b, &m); err != nil {
		return nil, err
	}
	if err = m.ValidateFiles(resourceFS(d.resources)); err != nil {
		return nil, err
	}
	// Detect colliding paths before an installer creates any directory.
	names := map[string]bool{}
	for name := range d.resources {
		lower := strings.ToLower(name)
		if lower == "settings" || strings.HasPrefix(lower, "settings/") {
			return nil, fmt.Errorf("reserved resource path %q: settings belongs to runtime plugin settings", name)
		}
		if lower == "vizi-plugin.json" || strings.HasPrefix(lower, "vizi-plugin.json/") || lower == ".vizi-go-sdk" || strings.HasPrefix(lower, ".vizi-go-sdk/") || lower == ".vizi-install-lock" || strings.HasPrefix(lower, ".vizi-install-lock/") || lower == "bin" || strings.HasPrefix(lower, "bin/") {
			return nil, fmt.Errorf("reserved resource path %q", name)
		}
		if names[lower] {
			return nil, fmt.Errorf("case-colliding resource %q", name)
		}
		names[lower] = true
	}
	for name := range names {
		for prefix := name; strings.Contains(prefix, "/"); {
			prefix = prefix[:strings.LastIndex(prefix, "/")]
			if names[prefix] {
				return nil, fmt.Errorf("resource is both file and directory: %q", prefix)
			}
		}
	}
	a := &Application{chunkBindings: d.chunkBindings, owner: d.owner, m: m, h: d.h, tools: slices.Clone(d.tools), exporters: d.exporters, resources: d.resources}
	// Tool metadata is taken from the same serialized snapshot as the manifest.
	for i := range a.tools {
		a.tools[i].spec = m.Contributions.Tools[i]
		a.tools[i].actions = slices.Clone(d.tools[i].actions)
	}
	d.built = a
	return a, nil
}

// Manifest returns independent, validated JSON for the supplied direct launch
// command. Installation supplies its own command; serving requires no JSON file.
func (a *Application) Manifest(command ...string) ([]byte, error) {
	m := a.m
	m.Entry.Command = slices.Clone(command)
	return m.JSON()
}

// Configure adapts the definition to the existing runtime. Interface-owned
// fields may not be overridden, avoiding a second source of truth. Base still
// supplies transport tuning, Started and application-level lifecycle hooks.
func (a *Application) Configure(base control.Plugin) (control.Plugin, error) {
	if (base.Control.PluginID != "" && base.Control.PluginID != a.m.ID) || base.Control.BuildIdentity != "" || base.Memory.Contributions != nil {
		return control.Plugin{}, errors.New("definition owns PluginID, BuildIdentity and memory Contributions")
	}
	base.Control.PluginID = a.m.ID
	base.Control.BuildIdentity = a.m.ID + " " + a.m.Version
	base.Control.Engines = a.m.Engines.Vizi
	h := a.h
	h.ChunkOverlays = map[string]*ChunkOverlay{}
	for id, o := range a.h.ChunkOverlays {
		if o != nil {
			h.ChunkOverlays[id], _ = NewChunkOverlay(id, o.source)
		}
	}
	var err error
	base.Control, err = h.Configure(base.Control)
	if err != nil {
		return control.Plugin{}, err
	}
	base.Memory.Contributions = map[string][]string{"O": {}, "P": {}}
	for _, o := range a.m.Contributions.Overlays {
		base.Memory.Contributions["O"] = append(base.Memory.Contributions["O"], o.ID)
	}
	base.Control.Panels = make(map[string]control.PanelLayout)
	for _, p := range a.m.Contributions.Panels {
		layout := control.PanelLayout{Lists: make(map[string]int), Cells: make(map[string]string), Tables: make(map[string]int)}
		var walk func(manifest.Widget)
		walk = func(w manifest.Widget) {
			if w.Kind == "table" {
				layout.Tables[w.ID] = len(w.Columns)
			}
			if w.Kind == "list" {
				layout.Lists[w.ID] = w.RowLimit()
				for _, cell := range w.Row {
					layout.Cells[cell.ID] = w.ID
				}
			}
			for _, child := range w.Children {
				walk(child)
			}
		}
		walk(p.Root)
		base.Control.Panels[p.ID] = layout
		base.Memory.Contributions["P"] = append(base.Memory.Contributions["P"], p.ID)
	}
	started := base.Started
	base.Started = func(s *control.Session) {
		// Initialize application state before a chunk source can run.
		if started != nil {
			started(s)
		}
		for id, o := range h.ChunkOverlays {
			if bind := a.chunkBindings[id]; bind != nil {
				bind(o)
			}
			if err := o.Bind(s); err != nil {
				s.Client.Fail(err)
			}
		}
	}
	if len(a.exporters) > 0 {
		base.Control = configureExporters(base.Control, a.exporters)
	}
	if err = a.configureTools(&base); err != nil {
		return control.Plugin{}, err
	}
	return base, nil
}
func (a *Application) ServeEnv(ctx context.Context, base control.Plugin) error {
	cfg, err := a.Configure(base)
	if err != nil {
		return err
	}
	return control.ServeEnv(ctx, cfg)
}
