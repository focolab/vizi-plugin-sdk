# Typed plugin authoring

`plugin` adapts typed handlers to the 1.0 `control` engine. `payload`
adds checked arrays and owned host-data copies, and `geometry` implements the
published crop, scale and time rules. No additional dependencies are required.

The normative [manifest schema and fixtures](../../../docs/spec/manifest_schema.md)
define the manifest these declarations generate.

## Declare the interface once

`Definition` registers contributions with their handlers, then `Build` freezes
and validates the interface. The application produces both runtime dispatch and
`vizi-plugin.json` from that snapshot. No handwritten manifest or parallel
handler map is needed. The [inspect example](../examples/inspect/main.go) is a
complete executable using this API.

```go
info := plugin.Info{ID: "org.example.inspect", Name: "Inspector", Version: "0.1.0"}
d := plugin.NewDefinition(info)
d.Needs(plugin.DataAccess)
inspect := d.Command(plugin.CommandSpec{
    ID: "inspect", Title: "Inspect dataset", Menu: plugin.ToolsMenu(),
}, inspectHandler)
d.Panel(plugin.PanelSpec{
    ID: "controls", Title: "Inspector",
    Root: plugin.Column("root",
        plugin.Label("help", "Inspect the active dataset."),
        plugin.Button("inspect-button", "Inspect", inspect),
    ),
}, nil)
app, err := d.Build()
if err != nil {
    os.Exit(plugin.DeclarationExitCode(info, os.Args[1:], err, plugin.RunOptions{}))
}
// In main, after any required cleanup:
os.Exit(app.RunCLI(ctx, os.Args[1:], plugin.RunOptions{}))
```

A `CommandRef` belongs to its definition: a widget referencing another plugin's
command, including one with the same ID, fails at build. A zero command reference
also fails. `CommandSpec.Menu` and `ToolSpec.Menu` default to nil (hidden); use
`plugin.ToolsMenu()` to list them. Untitled commands use their ID as the title.
Contribution/action order is retained, and duplicate IDs fail rather than
silently replacing callbacks. Registration after a successful `Build` panics;
repeated `Build` returns the same application. Caller-owned declaration slices,
pointers and resource bytes cannot change a successfully built application.

For a heavy first start (such as loading a large model), set `seconds := 90.0`
and `plugin.Info{..., StartupSeconds: &seconds}` to emit `entry.startup_s`;
nil omits it. This positive, finite number of seconds is advisory: the host
may honour, cap or ignore it, and SDK startup does not depend on that estimate.

The remaining contributions follow the same pattern:

- `Reader(ReaderSpec, ReaderHandlers)` associates extensions with the
  reader callbacks. `Extensions` and `FallbackExtensions` are normalized to
  lowercase with one leading dot; compound suffixes such as `.ome.tif` work.
  At least one suffix across the lists is required, and the lists cannot overlap.
  Fallback suffixes offer a generic reader beside a specific one without
  automatically preferring the generic reader. The host chooses from declarations,
  never probes a file, and asks the person when reader preferences leave a tie.
- `Exporter(ExporterSpec, ExportHandlers)` requires Open, Chunk, Finish and Abort.
  Each contribution has its own local handle namespace. The generated router
  supplies unique wire handles, translates requests and replies, and aborts
  outstanding exports on graceful shutdown. Callback resources on an abrupt
  session failure remain the application's responsibility, as with the raw API.
- `Panel(PanelSpec, PanelOpenedHandler)` declares the tree and optional open hook,
  returning a `PanelRef`. `ref.Bind(client)` supplies the patch handle.
- `Overlay(OverlaySpec)` declares a drawing overlay and returns an `OverlayRef`.
  Bind once per session and share that runtime handle. `ChunkOverlay(spec,
  source)` declares a chunked overlay, creates fresh routing for each session,
  and binds its source automatically after the application's Started callback.
  `OnChunkOverlay(ref, callback)` supplies that session's fresh runtime handle
  just before its source is bound. Store it to update label tables or invalidate
  wants from commands; synchronize shared application state. The callback must
  be short, and each source accepts one callback. Build rejects foreign refs,
  overlays without a registered source, and nil/duplicate callbacks.
- `Tool(ToolSpec, ToolHandlers)` combines views, triggers, actions, and input and
  lifecycle callbacks. Each `ActionSpec` includes its own `Handle`; routing uses
  the host's current action assignment, not assumed indices. Event receives
  primary input, cancellations and unknown actions. It retains the complete
  frame including context diagnostics and implicit gesture closure. Tool and
  action callbacks run on the doorbell listener and must not block on host calls.
- `Asset(AssetSpec)` declares a host-provisioned immutable file.
  `Needs(...)` explicitly requests permissions; permissions are never inferred
  from handler code. Unknown future permission strings remain host warnings.
- `Subscribe(plugin.Epochs)` requests epoch arrangements even with no tools.
  The runtime installs and acknowledges them automatically. Unknown subscription
  names are preserved in the manifest; hosts warn and ignore names they do not know.

Panel constructors cover Label, Button, Checkbox, LineEdit, Dropdown, Slider,
Image, Progress, Row and Column. Interactive constructors take a `CommandRef`; Slider also
takes a `SliderSpec`. IDs, tree bounds, command references, numeric values and
options are checked. Widgets describe the initial tree; there is no automatic
state mirroring, typed change listener or replay on reopen.
Use command handlers, panel patches and PanelOpened callback for
runtime state. This does not require duplicating the panel tree in JSON.

`Label(id, text)` displays plain text, including any markup literally. To opt
into Qt's rich-text subset, use:

```go
plugin.LabelWithOptions("status", "<b>Ready</b>", plugin.LabelOptions{TextFormat: "rich"})
```

The format applies to the initial text and every later `SetValue` update,
including labels in list rows. Links in rich labels are styled but never opened.
`TextFormat` accepts `"plain"` or `"rich"`; an empty `LabelOptions.TextFormat`
uses the plain default without adding a manifest field. At the lower-level
manifest API, `Widget.TextFormat` is a `*string`: nil omits the field, while a
non-nil value must be one of those two strings and may appear only on a label.
`Definition.Build` validates the same rules.

`plugin.Progress("bar", 0, false)` declares a noninteractive progress indicator,
without a command reference. Its initial value must be finite and within `[0, 1]`.
Host pickers use `ChannelPicker`, `MultiChannelPicker`, `ColorPicker`,
`ColormapPicker` and `RoiPicker`, with a command reference from the same
Definition. Multi-channel choices arrive as `args.Ints("value")` (`[]int64`);
`plugin.Ints(values...)` constructs the same wire type for `Panel.SetValue`.
Both helpers copy their slice. Other pickers use `args.Int` or `args.String`;
ROI choices also include a `name` argument and do not change host selection.
The choices are supplied by the host, rather than a manifest options list.
Channel picker entries display file channel names, falling back to `Channel 1`,
`Channel 2`, and so on; their values remain zero-based channel indices.

Use `panelRef.Bind(client).SetProgress("bar", fraction, indeterminate)` to update
it. Updates use the dedicated `set_progress` operation, clamp values into [0,1],
show NaN as empty,
and never trigger a command echo. Indeterminate ignores the fraction; return to
`false` with a fraction when the amount of work becomes known.

`app.Configure(base)` produces `control.Plugin`, or `app.ServeEnv(ctx, base)`
configures and serves it. Plugin identity/version and O/P contribution membership
come from the definition. Conflicting identity, memory-membership or dispatch
configuration is rejected. Base still carries tuning and application lifecycle
hooks. Construct fresh application state per session; SDK routing is fresh per
Configure, but Go callback closures are not cloned.

### Generate or install

From `polyglot/go-sdk`:

```sh
CGO_ENABLED=0 go build -o /tmp/vizi-inspect ./examples/inspect
/tmp/vizi-inspect --manifest=/tmp/inspect-package
/tmp/vizi-inspect --install
# Or select a host scan directory explicitly:
/tmp/vizi-inspect --install=/tmp/vizi-plugins
```

In Windows shells set `CGO_ENABLED` to `0` using that shell's syntax, build an
`.exe`, and invoke it normally. Each OS/architecture needs its own executable.

`--manifest[=DIR]` writes `vizi-plugin.json` and declared resources (default: the
current directory). The launch command directly names the current executable
by absolute path. Use a durable `go build` output; a `go run` executable may
disappear. Programmatically, `app.Manifest(command...)` returns JSON without
filesystem effects, and `app.WriteManifest(dir, command...)` writes a package
with an explicit launch command. Generation always uses manifest version 1 and
protocol 1.0. The `manifest` package validates generated typed declarations;
it is not a parser/validator for arbitrary external JSON.

Plugin Manager lists invalid or incompatible manifests as **not loadable** with
the reason. A directory without `vizi-plugin.json` is not listed.

The manifest tests read §8 fixtures directly from
[`manifest_schema.md`](../../../docs/spec/manifest_schema.md).
Run `go test ./manifest -count=1` from the SDK directory after schema edits:
the document is outside the Go module and is not tracked by Go's result cache.

`--install[=DIR]` copies the current executable into `DIR/<plugin id>`, defaulting
to `$VIZI_PLUGINS_DIR`, otherwise `~/.vizi/plugins`. Copy-mode installation also
works with `CGO_ENABLED=0 go run ./examples/inspect --install=DIR`. Native-library
packaging is not implemented, so self-install checks embedded build metadata and
requires `CGO_ENABLED=0`. Other files the program needs must be explicitly
packaged or host-provisioned; the installer cannot discover runtime file access.

Add icons and supporting files with `Resource(path, bytes)` or
`Resources(embedFS, paths...)` before Build. Paths use `/` on all platforms.
Build checks pane icons' presence, 64 KiB limit and signatures, plus portable
paths and collisions. The host performs the final image-rendering check. `bin/`,
`vizi-plugin.json` and installer metadata paths are reserved. Large models should
use Asset declarations rather than embedding them as resources.

Installation refuses a nonempty directory it did not create unless `--force` is
supplied. Binary paths include their content hash, so an update can coexist with
an executable still running on Windows. Old binaries and unrelated files are
retained. Files are staged individually and the manifest is published last;
this is not a transaction over the whole directory, and an interrupted update
may leave new resources or an unused binary. A `.vizi-install-lock` left by a
crash must be removed before retrying. Filesystem failures are returned, including
Windows sharing restrictions. Installed sessions are not restarted; use the
host's plugin lifecycle controls and Rescan to discover the new installation.

`RunCLI` is the recommended terminal entry point: it prints errors to
`RunOptions.ErrorOutput` (stderr by default) and returns 0 or 1 for main to
pass to `os.Exit` after cleanup. It prints no timestamp or stack trace.
`DeclarationExitCode` handles a declaration error before an Application exists,
using the same `Info` as the definition. `Run` remains available to callers
that want an error value; CLI errors contain the complete report and unwrap to
the original cause. `RunOptions.Output` selects stdout for help and success.

Failures start with `INSTALL FAILED:` or `MANIFEST FAILED:`, name the id,
version and destination, and give the operation, full path, cause and a separate
`To fix:` section. Usage errors name the offending argument; write
`--install=DIR`, not `--install DIR`. A cgo build refusal supplies sh, PowerShell
and cmd rebuild commands preserving the typed `--install` or `--install=folder`
option (including a quoted folder with spaces). Empty directory arguments,
including `--install ""`, are refused. Usage and build errors have no artificial
path on their `Why:` line; a usage error names a destination only when one was
explicitly supplied. Permission advice names the nearest existing directory,
and a failed first directory creation says that nothing was written. A partial
directory creation names the directory left behind. File/symlink conflicts have
a specific remedy, using the option for the requested operation. A held-lock failure gives the full lock path and a
removal command to use only after the other installer has stopped. Other lock
creation errors report their actual cause. Successful installation prints
`INSTALLED: <id> <version> at <full directory>`; manifest generation prints
`MANIFEST WRITTEN: <id> <version> at <full manifest path>`.

The installer publishes the manifest last. A failed write may leave directories,
resources or an unused executable; the report does not promise rollback of the
whole package. Use scratch directories when testing installation. Put session-dependent setup in `RunOptions.Runtime`, a factory called
only for normal serving. Defining the interface and writing its manifest must
not connect to the host or start application workers.

## Handler API

The low-level handler API serves applications maintaining their
own manifests and dispatch wiring.

### Register handlers

```go
handlers := plugin.Handlers{
    Commands: map[string]plugin.CommandHandler{
        "inspect": func(ctx context.Context, host *control.Client, args plugin.Args) (plugin.Result, error) {
            view, err := host.GetViewState(ctx)
            if err != nil {
                return plugin.Result{}, err
            }
            return plugin.Result{Message: view.DatasetName}, nil
        },
    },
}
cfg, err := handlers.Configure(control.Config{PluginID: "org.example.inspect"})
```

Pass `cfg` as `control.Plugin.Control` to `control.ServeEnv`. Nil `control.Plugin.Memory.Purposes` selects the specified G/V/O/P/X/S set.
`ServeEnv` supplies the handshake and memory integration; custom `NewServer`
embeddings still supply those hooks themselves.

The [inspect example](../examples/inspect/main.go) compiles as part of the SDK:

```sh
go build -o /tmp/vizi-inspect ./examples/inspect
```

The target host must launch it with its transport environment, a manifest naming
`org.example.inspect`, a command contribution named `inspect`, permission
`data_access`, and protocol 1.0 compatibility.
Running it without the host's launch environment cannot establish a session.

Plugins are trusted code running as the user; `data_access` is a capability
declaration, not a security boundary. Vizi does not gate fields to imply an
isolation it cannot provide; sandbox vizi and its plugins together if needed.

Write `vizi-plugin.json` using the [manifest schema](../../../docs/spec/manifest_schema.md).
Use `"manifest_version": 1`; the Definition API generates version 1. A tool
`binding` field is not supported.
Set `engines.vizi` to `"1.0"` for this protocol: incompatible manifests are
logged and skipped during host discovery. With this low-level API, keep the manifest contribution IDs consistent with
the handler registrations. The Definition API above generates both together.

### Modal tools and actions

One tool is engaged at a time, and the primary button belongs to that tool;
tools never name the primary button. Triggers engage a tool: toggle keys engage
and disengage on successive presses, while momentary keys or mouse chords
restore the previously engaged tool on release. A momentary engagement during
a gesture is dropped by the host. `session.State.Tool(handle)` exposes the
assigned `Triggers` subset and every declared `Actions` entry with its assigned
key (empty when none).

Register `Handlers.Activated` and `Handlers.Deactivated` maps by manifest tool
ID to observe engagement changes under `ServeEnv`. Each callback receives
`(context.Context, *control.Client, *protocol.ToolBinding)`; the binding is an
independent snapshot including assigned actions. Activation follows installation
and acknowledgement admission. The binding is therefore available at engagement:
report its honored triggers and action keys immediately, without waiting for an
input event or consulting the manifest's requested keys. Retirement follows
delivery of any gesture
cancellation and supplies the previous binding with `Retired` and the retirement
revision set, even if the retirement message omitted its ID or actions. Replays,
in-place binding updates and refused bindings do not trigger engagement hooks.
These callbacks share the doorbell listener with input: keep them short and
hand blocking host requests to an application worker. Returned errors are
diagnostic; panics follow the listener's terminal handler-panic path.

For custom dispatchers the equivalent fields are `control.Config.ToolActivated`
and `ToolDeactivated`. `ServeEnv` wires them to arrangements; a bare `NewServer`
embedding must provide its own arrangement integration. The bounded listener
queue refuses overflow rather than silently losing a lifecycle notification.

An action is a key or pane button fired into the engaged tool, including
mid-drag, carrying the pointer's current sample. It arrives as a decoded
`doorbell.Input` with `Phase == doorbell.Fire` and a nonzero `Action` index.
Use `session.State.ActionID(in)` to resolve that index to the manifest action
ID and dispatch it from your input callback. The helper returns `(id, true)`
for an installed action; primary input, unknown indices and inactive tools
return `("", false)` without a fault or diagnostic. An action FIRE leaves
the open gesture untouched: the following drag and release still belong to
the original press. Go exposes raw input callbacks; action handler routing
belongs to the application.

`Args.String`, `Int`, `Float` and `Bool` preserve protobuf types and distinguish
missing values from zero/false/empty. They return `*control.HostError` with
`INVALID_ARGUMENT` on missing or wrong types. `Has` supports optional arguments.
Use `Args.Number` when either an integer or floating-point value is appropriate.
It returns `float64`, refusing integer conversions that would round (for example,
`2^53 + 1`); `Int` still reads those original integers exactly. Exactly
representable larger integers, such as `2^60`, are accepted. Floating-point values
pass through unchanged, including NaN and infinities, just as with `Float`.
`plugin.String`, `Int`, `Float` and `Bool` construct corresponding wire values.
A command error becomes a failed `CommandDone`; a successful `Result.Message`
becomes its message. The reserved `CommandDone.result` has no host consumer and
is left to the raw API.

Commands may call `RequestAsset` or `RequestSavePath` and wait for the picker;
the SDK allows both. That wait holds the session's one request slot while the
person browses, so reads and other commands wait behind it. A plugin that also
serves other work should open pickers off the command path and return promptly,
using `Result.AfterReply` or an application-owned background worker/task.

For work that must follow acknowledgement, return `Result.AfterReply`:

```go
return plugin.Result{Message: "Choose a file", AfterReply: func(ctx context.Context, host *control.Client) error {
    chosen, err := host.RequestAsset(ctx, &protocol.RequestAsset{Slot: "input", ForceReprompt: true})
    // Process chosen and report the result through the panel.
    _ = chosen
    return err
}}, nil
```

The continuation runs on a separate worker after gRPC sends the successful
`CommandDone`; its requests cannot overtake the acknowledgement. A goroutine
started inside the command has no such ordering guarantee. Failed replies,
admission/send failures, and cancellation before starting suppress the work.
At most `MaxPendingRequests` continuations may be pending or running; excess
registration fails the command before acceptance. Continuations receive the
session context and must honor cancellation. Errors/panics become diagnostics
(except a returned cancellation error);
report user-visible outcomes through the panel and join resource-owning work
in the shutdown hook. The raw handler equivalent is `control.AfterReply(ctx, f)`.
This guarantees stream ordering, not that the host has applied the reply.

`Client.Call` caps ordinary requests at `Config.RequestTimeout` (default 30
seconds). `RequestAsset` and `RequestSavePath` instead use `Config.PickerTimeout`
(default 30 minutes), a dead-session backstop for requests answered by a person,
not a work budget. A shorter caller context deadline wins for either.

`ReaderHandlers` covers Open, Volume, Plane, Metadata and Close;
`ExportHandlers` covers Open, Chunk, Finish and Abort. Each callback receives a
typed request and returns its typed response. Omitted operations generally return
`UNSUPPORTED`, errors retain their existing control-plane translation, and nil
successful responses become errors. The engine supplies correlation IDs and
catches handler panics. `Handlers.Readers` registers ReaderHandlers by manifest
ID: OpenReader.reader_id selects the contribution; empty selects the sole reader
or is refused when several are registered. Each contribution's local handles
are wrapped in distinct wire handles and restored for later callbacks, including
Close. Wire handles are never reused within a session. Replacement follows
§1.2's handshake: Open a different URI first, then close the previous reader
only on success. A refused Open leaves that previous reader readable. A reload
of the same URI closes first to release exclusive file access; a failed reload
leaves no reader. This applies across contributions too.

Callbacks own the file objects. Local handles must distinguish the two files
that briefly overlap within a contribution, and Close must release only the
named file. A replaced reader's Close runs after its successor's Open: clear
shared panel state only if the named reader is still current. A replacement
Close error or panic is diagnosed without failing the successful Open; the old
binding is forgotten. Explicit Close also forgets its binding even if cleanup
fails, then reports that failure. Shutdown closes the remaining reader before
the configured shutdown hook. Provide Close when file cleanup is needed.

An unknown or already-closed wire handle closes successfully; an omitted Close
callback simply forgets the binding. Reads and metadata on inactive handles
return `NOT_FOUND` naming the handle and diagnose the host as out of step. The
router checks t/c/z against the returned five-axis shape. Both reader APIs
refuse nonzero levels with `OUT_OF_BOUNDS` before callbacks.

The singular `Handlers.Reader` dispatcher is also available, exclusive with Readers.
It owns its handle routing, shape checks, replacement cleanup, idempotent Close
and shutdown cleanup, including the same open-then-close and same-URI rules.
Do not rely on receiving CloseReader: a replaced reader gets no CloseReader or
RetireSegment of its own. The next Open closes its file, and the next G grant
supersedes its arena; session shutdown also releases files.

### Processing stages

Vizi reuses saved filter output when the plugin ID, plugin `version`, stage and
parameters match. Change the plugin version whenever a stage's output changes,
including changes to settings kept outside `params` (for example, a model chosen
in a panel). Reinstalling a rebuilt executable alone does not invalidate that
saved output.

`Definition.Stage(StageSpec, StageHandlers)` declares its manifest entry and
callbacks together. Name defaults to ID. `StageSpec.Params` declares a flat
list of `plugin.StageParam` entries, used for both the manifest and runtime
validation. For example:

```go
plugin.StageSpec{ID: "blur", Params: []plugin.StageParam{
    {Key: "sigma", Name: "σ (voxels)", Type: "float", Value: 2.0, Min: 0.1, Max: 50},
    {Key: "edge", Type: "choice", Value: "reflect", Options: []string{"reflect", "constant"}},
}}
```

Types are `float`, `int`, `bool`, `choice`, and `string`. Defaults are required, including
false/zero; numeric bounds are inclusive, and choices are case-sensitive.
`int` defaults and bounds must fit signed 64-bit. Declarations use Go integers
or integer `json.Number` values; decoding
preserves integer precision. Only numeric types take Min/Max, and only choice
takes Options. Parameter names default to keys. The host sends every key; the
SDK rejects missing/extra keys, wrong wire kinds, nonfinite numbers and values
outside bounds/options. Bad planning params answer INVALID_ARGUMENT; bad arm
params report site 32 without an acknowledgment.

String parameters use `Value.s` and arrive as Go strings. `MaxLength` sets the
UTF-8 byte bound (1–4096); zero omits `max_length` and uses 4096. For example,
`plugin.StageParam{Key: "options", Type: "string", Value: "é", MaxLength: 2}`
accepts that two-byte default. Strings take no Min/Max/Options. Values are
preserved exactly, without trimming, case folding or reordering. Validate the
text's application-specific meaning in the planner and return UNSUPPORTED
with an explanation when it cannot be used.

Use `PlanWithParams(ctx, client, request, stage.Params)` to receive checked
plain Go values: float64, int64, bool, string. Set exactly one of PlanWithParams
and the Plan callback. Each Check receives `info.Params`, and Process
receives `slot.Params`, copied from that position's arm. These are owned maps;
changing one cannot affect another callback, turn or position. Planning can
choose output dimensions/dtype and dependencies based on params. Each accepted
arm captures its dependencies and values; changing params requires a new plan
and arm revision. No defaults are silently filled in.

`Handlers.Stages` also works with a manually authored manifest; supply its
schema in `StageHandlers.Params`. With Definition.Stage, declare parameters
only in StageSpec. Custom control dispatch can use `StageHandlers.PlanRequest`
to invoke either planner with checked values. See the buildable
[clamp example](../examples/stage-clamp/main.go).

- `Plan(ctx, client, *protocol.PlanStage)` returns `*protocol.StagePlanned`.
  Input/output indices must fit u16; output dimensions are at least 1 and cannot grow. Declare
  modified channels and their dependencies, and additions with distinct keys.
  The host assigns added channel indices and computes output channels. A stage
  that changes only extent, origin or pose may leave both lists empty, naming
  any input it reads in `StagePlanned.Reads` (for example `Reads: []uint32{0}`).
  Reads become required inputs without marking those channels as rewritten.
  Unknown stage IDs answer NOT_FOUND; invalid host inputs answer PROTOCOL.
  Invalid author plans answer INTERNAL, except explicit wire channel/count
  bounds (PROTOCOL). Return control.HostError to select a planning refusal kind.
- Optional `Check(ctx, client, arm, stage.Info)` gets **copied metadata only**.
  Return `&stage.Refusal{Reason: 1, Detail: "need earlier state"}` for a causal
  refusal, or reason 3 if your parameters no longer fit the armed plan. Missing
  declared input channels automatically refuse with reason 2 before Check.
- `Process(ctx, client, arm, *stage.Slot)` borrows writable pixel memory.
  `slot.Plane(channel)` finds a packed plane by channel identity; `slot.Data()`
  exposes the full bounded capacity for repacking. Update `slot.Layout` for
  crops, dtype changes, appended channels, origin or pose. Added identities come
  from `arm.NewChannels`. `slot.AddChannel(index)` appends and zeroes its plane;
  unused Data capacity is otherwise not promised zero.
  `slot.RelayoutFloat32(allocation, offset)` repacks **every plane** from a
  window of the current layout as unscaled float32, clips the extent and shifts
  origin to preserve placement. It supports all four input dtypes and uses
  scratch space equal to the output's packed bytes, bounded by slot capacity.
  Both helpers validate before writing; discard earlier pixel views after a
  relayout. Other output types/scaling remain author work through the raw API:
  repack every plane and update extent/origin explicitly when layout changes.
  `Layout.ComposePose` composes relative motion
  with the original rotation in both terms.

The SDK validates the owned slot against `arm.Input`, runs these callbacks,
checks the result against `arm.Output`, commits layout, and hands on the turn.
Input planes retain their identities/order; declared additions are appended;
spatial allocations cannot grow beyond the actual input. A nil Process result
means processed. Any Process error or panic means failed/internal-error, even
if it returns a Refusal: it may have written already. Failed volumes are
**discarded by the host**; no SDK backup or rollback is made.

Callbacks must stop all use of the slot and its slices before returning. Never
retain borrowed data or leave a goroutine writing it. To retain pixels, copy
them into owned storage. The worker serializes Check/Process across the session;
Plan/PlanWithParams, control calls and shutdown hooks may run concurrently. Processing can call
the host through Client, but must honor cancellation and cannot hold a volume
waiting for a future volume. The host owns deadlines and quarantine; the SDK
never times out a callback and reclaims its memory while it could still write.

`ServeEnv` installs S grants, arms by `(pipeline, position)`, and routes hints.
Polling defaults to 40 ms (`control.Config.StagePollInterval`) even without D;
hints accelerate checks with a 1 ms minimum interval. Successful stages release
straight to position + 1; refusal/failure releases to negative position. D goes
to the host in all cases. `Client.IgnoredStageHints()` counts hinted slots not
owned by this session. Retirement disarms the ring but preserves each arm's
revision identity; a replacement ring uses the next revision. An already-held
turn finishes before memory is unmapped and SegmentReleased is acknowledged.
Re-arming an occupied position is acknowledged immediately: the running turn
keeps its original revision, and each subsequent turn takes the new arm.
Slots may have trailing region padding; Data exposes only declared capacity,
and repeated channel indices in an input header are invalid.
Plan-only declarations
remain usable for negotiation, but require Process before an arm can be acked.
Custom control servers must wire `StageMemory`, `StageNotify` and `HintStages`.

### Settings and per-file state

`control.SettingsDir() (string, error)` reads `VIZI_PLUGIN_SETTINGS_DIR` and
creates the named settings directory on first use. It requires no Client or
command context, so readers and exporters can call it directly. Unset/empty
variables, non-directory paths and creation failures return
`*control.SettingsDirUnavailableError`; filesystem causes are available through
`errors.Is`/`errors.As`. The accessor does not invent a fallback.

`control.FileStateDir(message) *string` gives the optional per-file directory
from any of these generated messages:

| Message | File the directory belongs to |
| --- | --- |
| `protocol.ViewState` from `client.GetViewState(ctx)` | Current dataset |
| `protocol.OpenReader` received by `ReaderHandlers.Open` | File being opened |
| `protocol.OpenExport` received by `ExportHandlers.Open` | Export's source dataset, not its destination `Locator` |

Empty/absent values return **nil**, including for remote and plugin-specific
URIs. The generated protobuf `FileStateDir` fields remain raw wire strings;
use the accessor and check for nil before joining a filename. Joining onto an
empty string would write relative to the plugin's working directory.

```go
dir := control.FileStateDir(request) // OpenReader or OpenExport; also accepts ViewState
if dir != nil {
    // Only when the plugin actually has state to write:
    if err := os.MkdirAll(*dir, 0755); err != nil {
        return nil, err
    }
    if err := os.WriteFile(filepath.Join(*dir, "state.json"), data, 0600); err != nil {
        return nil, err
    }
}
```

The SDK and host name the per-file directory without creating it. Read its
value from the relevant request/view each time: the dataset can change during a
session. Settings live with the installed plugin, whereas per-file state moves
with the dataset's companion directory. The host may place settings in its own
data directory if the installation is unwritable; always use the accessor.
These locations are conventions, not filesystem confinement.

`settings/` beside the manifest is reserved: `Definition.Resource` cannot
package files there (case-insensitively). The SDK installer preserves existing
settings on update and does not create the settings folder itself.

### Reader and exporter contract

For multi-image files, use `ReaderHandlers.OpenSeries` instead of `Open`.
It receives the same `*protocol.OpenReader` and returns `ReaderOpenResult` with
exactly one of `Opened: *protocol.ReaderOpened` or `Choice: *plugin.SeriesChoice`.
`Open` callbacks can ignore the series
field. Check `request.Series == nil` to distinguish a request to open/list the
file from a selection; a non-nil pointer to `""` is a valid selected ID.

```go
// From an OpenSeries callback when request.Series == nil:
return plugin.ReaderOpenResult{Choice: &plugin.SeriesChoice{
    Entries: []plugin.SeriesEntry{
        {ID: "/images/first", Label: "First image"},
        {ID: "/images/second", Label: "Second image",
            ShapeTCZYX: []uint32{10, 1, 40, 512, 512}, DType: pb.DType_DTYPE_U16},
    },
}}, nil
```

The IDs are yours and opaque to the host. When one is selected, return its
ReaderOpened; an unknown ID should return `control.HostError{Kind: pb.Error_NOT_FOUND}`.
An empty choice is refused. A one-entry choice opens directly by calling its
opener with that ID; it is never sent as a list. Answering a selected ID with
another list is INVALID_ARGUMENT. Optional shape is five positive TCZYX sizes;
optional dtype is U8, U16 or F32 (DTYPE_UNSPECIFIED omits it). Labels default to
the ID in the host's picker. Entries are sent in order, trimmed to the 32 MiB
message ceiling minus 64 bytes of envelope, with `truncated` set if needed.

If listing opens a resource worth keeping, supply `SeriesChoice.Open` as a
continuation and `SeriesChoice.Close` to release it. Both can capture that
instance. The SDK holds one listing per session, keyed by URI and the request's
reader_id, and invokes the continuation for a matching named-series open.
Without a continuation it calls OpenSeries again. Any other open, ten minutes
idle, or session end closes the listing; close errors/panics are diagnosed.
With a continuation, successful ReaderOpened transfers ownership to the normal
reader Close callback, so the listing closer is not called. Without one, the
listing closer runs once the fresh OpenSeries returns its own handle. On refusal
the listing closer runs. OpenSeries must also open a named ID on a fresh call, since recent files
can reopen a series without listing first.

With `Definition.Reader` / `Handlers.Readers`, a list leaves the existing reader
untouched and creates no handle or G grant. As with other opens, requesting the
same URI closes the current reader first to release exclusive file access.
The raw singular `Handlers.Reader` retains its application-owned handle and
replacement lifecycle. `GetViewState` exposes the current ID as `DatasetSeries`: a
`*string`, nil for a single-image file, and nonnil (including `""`) for a series.
A list with a duplicate ID is refused and its listing instance closed.

The normative [reader/exporter contract](../../../docs/spec/reader_writer_contract.md)
defines the application buffer exchange:

- The plugin-produced Loaned G arena arrives **after ReaderOpened**. The host
  waits for its installed GrantAck before reading. If a read arrives without G,
  answer an Error naming the missing purpose. Return a SegmentRef into G, with
  uses unset (one), full volume `shape_zyx = [Z,Y,X]` or plane `[1,Y,X]`, and
  exact `len = product(shape_zyx) × sizeof(dtype)`. There is no inline reader
  reply. Bytes are contiguous C order, X fastest, little-endian and unpadded.
- If Plane is nil and Volume is present, the SDK calls Volume for the same
  handle/t/c/level and derives the requested Z plane from its fresh G loan.
  It moves the plane bytes to the reservation's aligned start and changes the
  release key to the plane length; it needs no second allocation and preserves
  byte order. Volume must return an exclusive, unpublished loan with uses 0/1.
  Validation failures reclaim that loan. An explicit Plane handler takes priority;
  only a reader with neither callback answers UNSUPPORTED. ServeEnv supplies the
  required memory integration; custom NewServer users set Config.ReaderMemory
  to the same Memory used by their AdmitOutbound hook.
- Serve level 0 only. Out-of-range t/c/z and any nonzero level are
  `OUT_OF_BOUNDS`. G holds one volume; keep lent bytes untouched until
  ReleaseLoan. A second read while full must fail and report SegmentShortfall.
  Never call data_access on the dataset this reader serves.
- Metadata returns `ome-xml`, `ome-ngff` or an empty form for no document.
  ReaderOpened's spacing/time fields take precedence over metadata. Declare only
  known calibration: zero/empty means unknown; never invent a spacing of 1.0.
  When the file records only some axes, the host's `ViewState` exposes measured
  axes in `SpacingUnit` and unknown axes as NaN with `FILE` provenance. Use
  `geometry.PhysicalViewSpacingAxes` and check `axisKnown[i]` for every axis
  before using its pitch: one known axis says nothing about the rest. A true
  flag means positive, finite spacing; a false flag means no measurement, NaN
  in a partial file or zero when no axes are known (including on error).
  NaN or zero occurs exactly where the flag is false. `PhysicalViewSpacing`'s
  `known` means **any** axis, not all, and does not permit using every pitch. Tool
  `Geometry` keeps finite spacing with `DISPLAY_FALLBACK` in that case.
  Set `ReaderOpened.ChannelNames` by channel index, using `""` for unnamed
  channels; the list may stop short of C. More than C names is INVALID_ARGUMENT;
  the SDK closes the new instance before replacing the previous reader.
- Host-produced Loaned X is granted before OpenExport, without waiting for an
  acknowledgement. The control engine holds chunks until both grant halves land.
  Consume or copy chunk bytes before returning from Chunk: the host reuses X
  as soon as ChunkWritten or Error arrives. The engine also sends ReleaseDeposit
  when the descriptor use ends.
- OpenExport's shape is the artifact's shape, with T/C selection counts.
  Chunk channels are source indices, in artifact order. Write only calibration
  supplied in OpenExport; zero/empty is unknown. Never infer missing artifact
  calibration from the source document.
  Finish completes the artifact; Abort abandons it and answers ExportDone (an
  unknown handle may instead return Error). Later chunks return NOT_FOUND.

`Geometry.Channels` (tool sessions) and `ViewState.Channels` (panels and other
host clients) contain `protocol.ChannelInfo`, also named `protocol.ChannelName`.
Each has `Index`, `Name`, `Source`, and `Declared()`, true only for FILE or USER.
A PLACEHOLDER such as "Channel 1" can label a trace, but must never be written
as a file's declared name. Unknown sources retain their enum number;
`Source.String()` prints that number. ViewState lists all channels in index
order and has none when no dataset is open.

The export callback receives `OpenExport.SourceMetadata`, `Channels`, and `Provenance`. SourceMetadata is the SOURCE's document:
embed it only as provenance, never as the artifact's own description. Channels
are in artifact C order, with each `Index` still naming the SOURCE channel. Write
a name only when `channel.Declared()` is true. Provenance is the host's raw JSON
string, preserved without parsing or validation so even a malformed/non-object
record cannot fail an export. Embed it in the format's extra metadata when
possible; it describes the selection at open, while each chunk's
`FilteredChannels` identifies which volumes were actually filtered.

Raw reader allocation and reply-admission cleanup remain application-owned; see
the raw-loan rules below. No 1.0 application protocol uses OWNED, so no automatic
Owned transfer binding is required.

Use `session.Memory.Registry.FindPurpose("G")` to find the active reader arena,
then `segment.Arena()` and `session.WriteLoan` to allocate. Purpose lookup
refuses missing or ambiguous grants and excludes retired/superseded ones.

`Panels` maps panel IDs to optional open callbacks. `PeerViolation` receives typed
host reports; without it, reports become local diagnostics. `Shutdown` adapts to
the dedicated shutdown hook. Request callbacks run on the control
worker; panel/violation callbacks and shutdown run on separate workers. A command
may call the host without blocking reply routing. Synchronize shared application
state and honor cancellation, as with `control.Config` directly.

`Configure` snapshots registration maps and refuses to overwrite existing
application dispatch hooks. It preserves the transport, resource and lifecycle
hooks in the supplied config. It does not validate registrations against a
manifest; both must name the same IDs.

## Work with owned arrays

`payload.Encode[T]` and `Decode[T]` support U8, U16, F32 and F64, including named Go
scalar types. Tensors own contiguous C-order bytes and copy their shape metadata;
accessors return copies. Dimension products and lengths are checked before
allocation, and decoding requires an exact scalar dtype. Byte order is explicit.

```go
tensor, err := payload.Encode([]uint16{10, 20}, []uint32{1, 1, 2}, true)
values, err := payload.Decode[uint16](tensor)
```

Use `payload.ReadVolume` / `ReadMask` with `session.Client`, `session.Memory`, the
typed request, and `payload.CopyOptions{MaxBytes: ..., ByteOrder: ...}`. These
helpers copy and close the reply on every disposition, including malformed shape,
budget refusal, copy failure and successful copy. If copying succeeds but closing
fails, the complete Volume/Mask is returned **alongside the error**, so callers can
still use the owned data for local work. A non-nil result with a non-nil error
means only cleanup failed; it does not mean the host reclaimed its reservation.
Validation and copy failures return nil data, with any additional close error
joined to the original error (`errors.Is`/`errors.As` can inspect both).
The returned Volume/Mask retains the served indices, generation, and mask identity
or origin; its Tensor survives reply release. `MaxBytes` must be positive and the
byte order must explicitly be `payload.LittleEndian` or `BigEndian`.

`Volume.Filter` is an owned `*protocol.AppliedFilter` snapshot for those pixels:
plugin ID/version, stage ID, exact params and rewritten channels. Nil means
the file's own voxels. A raw channel, a pyramid level above zero, or a refused,
failed or timed-out stage turn carries no filter even if earlier reads did.
Pass `&protocol.ReadDataVolume{Raw: true}` to `payload.ReadVolume` to request
the file's own voxels even with *Plugins See Filtered Data* enabled; the reply
then has no `Volume.Filter`.
`Client.GetViewState().Filter` and subscribed `ViewChanged.Filter` describe the
session's applied filter, including at a frame the stage refused; never use it to
infer read provenance. The lower-level `ReadDataVolume` reply exposes the same
per-reply field as `reply.Volume.Filter`.

```go
volume, err := payload.ReadVolume(ctx, session.Client, session.Memory, request, options)
if volume != nil {
    // The complete owned copy is usable even if reply cleanup failed.
    processLocal(volume.Tensor)
}
if err != nil {
    return err // Preserve the failure even when local processing was possible.
}
```

The reader/exporter contract specifies little-endian payloads. Supply that order
explicitly; other raw payload APIs still require their application byte order.
`CopySegment` is available when a caller already owns a reply; that lower-level
helper does not close the reply. The built-in `control.Memory` copier supports
host-produced Loaned descriptors. No Owned handoff is inferred.

For host-computed ROI membership, use `payload.ReadInclusion(ctx,
session.Client, session.Memory, &pb.ReadRoiInclusion{RegionId: id, T: frame},
options)`. It returns an owned `InclusionMask` with `Tensor`, `OriginZYX`,
`OpenGeneration`, `RegionID`, signed `T`, `Present` and `VoxelCount`. Hidden and
empty regions have no tensor; `Present` distinguishes them. The helper checks
the host's count against the copied U8 0/1 mask, returns a `control.HostError`
on mismatch, and releases the deposit even on failure. The lower-level
`Client.ReadRoiInclusion` still returns a reply owner: defer `Close`, copy before
closing, and check counts yourself. The host supplies posed, parent-transformed,
level-0 membership; the plugin need not reproduce its rasterizer. Declare
`plugin.DataAccess` for this request.

Inside an exporter Chunk callback, `payload.CopyExportChunk` validates against
the corresponding `OpenExport`: channel count, full spatial slab shape,
dtype, byte count and inline/descriptor consistency. It returns an owned
`[channel-in-chunk,Z,Y,X]` Tensor in `OpenExport.little_endian` order. The engine
releases the descriptor when the callback returns. Channels remain in the chunk's
declared source-index order, and `ChunkWritten.total_bytes` should report bytes
actually committed by the exporter. Channel values are not bounded against the
artifact's C count. Chunk T is also a source index, so this helper does not bound
it against the artifact's T count. Its artifact index is its rank among distinct
source timepoints; the host sends those in ascending order. For example, source
timepoints 40–49 become artifact indices 0–9.

`OpenExport.Filter` names the selected filter, but does not promise that every
chunk is filtered. `ExportChunk.FilteredChannels` names the channels actually
filtered in that chunk; every other channel is raw. By default, vizi retries a
failed filter timepoint once, then aborts the export, possibly after successful
chunks. Raw fallback on filter failure requires the user's explicit opt-in.
Abort must clean up the partial artifact regardless of how many chunks arrived.

The session rejects contradictory provenance before any raw or typed Chunk
callback: filtered channels require a filter at open, and must belong to that
chunk's channels. It replies INVALID_ARGUMENT, releases the chunk's descriptor,
and refuses further chunks or Finish for that export. Abort still reaches the
author's cleanup callback. To retain valid provenance
beside the pixels, use `payload.CopyExportVolumes(memory, opened, chunk, maxBytes)`
inside Chunk. It returns `[]*payload.Volume`, one owned ZYX volume per channel
in chunk order, with source T/C and an independent Filter snapshot only for
filtered channels. Level/OpenGeneration are zero (no generation is supplied by
exports). It supports inline and segment sources and makes one slab copy;
immutable tensors share that copied storage. The CopyExportChunk API
is array-only, so its users must inspect FilteredChannels explicitly.

`GeometryEncoder.Lines` accepts `Polyline` (or the empty topology) for an open
polyline, `Segments` for independent pairs, and `Loop` for a closed loop.

## Serve chunked overlays

An overlay is drawn only while its plugin runs. A manifest declaring overlays
must also declare a panel, reader, exporter, stage, or a command/tool with a
Tools-menu entry or pane button. A keybinding or trigger alone does not count:
another binding can take it. `Definition.Build` rejects an overlay with no
starter; a panel is the usual place for its controls.

The host layers label overlays by `OverlayStyle.DrawPriority`, higher on top.
In 3D, an enclosing label overlay hides an enclosed one unless the enclosing
overlay has the lower priority. Lower opacity does not solve this: 30% opacity
per voxel accumulates to opaque through a thick region. For example, Movie seg
puts its enclosing footprint at priority 0 and its bands at priority 1.

The host draws label voxels using the LUT. To hide a label, keep its colour and
set its LUT row's alpha to 0. `label_visible` governs only whether a compiled
label becomes a visible annotation region; keep it consistent with the LUT
when both should be hidden. These rendering rules also apply to reliable label
overlays below; chunked overlays themselves never compile.

Declare `chunked: true` and `time: "per_t"` or `"static"` in the manifest;
`segment_mib` is refused on chunked overlays. The host sizes each overlay's
`O` arena from the dataset, and re-grants it on dataset changes.
`Definition.Build` validates the declaration. With `Definition`, use `ChunkOverlay` and `OnChunkOverlay`
for automatic lifecycle handling, as in the
[movie_seg example](../../../examples/movie_seg/go/main.go). With the raw
handler API, create one handle per overlay per session:

```go
chunks, err := plugin.NewChunkOverlay("bands",
    func(ctx context.Context, s *control.Session, want *pb.ChunkWant) (*plugin.Chunk, error) {
        // Read through s.Client/s.Memory at want.Level; want.T preserves presence.
        return computeChunk(ctx, s, want)
    })
cfg, err := (plugin.Handlers{
    ChunkOverlays: map[string]*plugin.ChunkOverlay{"bands": chunks},
}).Configure(control.Config{PluginID: "my.plugin"})
p := control.Plugin{Control: cfg, Started: func(s *control.Session) {
    if err := chunks.Bind(s); err != nil { s.Client.Fail(err) }
}}
```

The source runs serially per overlay, off dispatch, in host priority order.
It must honor context cancellation. Return a `Chunk` with ZYX u8/u16
`Labels` and integer `PositionZYX` on the wanted level's lattice; a nil answer
sends an empty chunk. Labels use the contribution's segment arena. Arena
exhaustion keeps the computed answer and retries on a host release or set
change, with no shortfall and no refusal count. No polling timer is involved.
Every received set updates membership immediately: a key that leaves and
returns is served again, even between worker turns. `InvalidateChunks()`
serves every current want again and discards stale in-flight/deferred answers.
Source errors/panics are diagnosed and skipped until that want is invalidated
or leaves and returns. Shutdown cancels and joins the workers before its report.

`Wants()` returns independent snapshots. `SetLabelTable(lut, names, visible,
style)` sends an inline 256×4 RGBA table, coalescing separately from chunks.
A `nil` source leaves serving to the application: `PublishChunk(want, chunk)`
checks current membership and returns `memory.ErrArenaFull` on exhaustion.
Capture `session.Memory.LoanChanges()` **before** attempting publication,
then wait on that channel, cancellation, or your own set-change notification.
The broadcast may be for another arena; retry your own allocation. Manual
workers own stale-answer detection and their shutdown. `control.Config.ChunksWanted`
is the bounded receive hook for a custom integration; it must never read the host.

A chunked overlay takes `OverlayChunk` and `OverlayLabelTable` only;
`OverlayUpdate` to one is refused on site 30 (`overlay_chunk_refused`).
It never compiles: `CompileLabelVolume` returns `INVALID_ARGUMENT`.
`OverlayRemove` clears both its chunks and table. A superseding grant sends
`GrantAck(new)` followed by `SegmentReleased(old)`; retiring the old ID again
replays the release, and subsequent publications select the new arena.

## Build reliable inline overlays

```go
encoder := plugin.GeometryEncoder{ByteOrder: payload.LittleEndian} // match the target host
item, err := encoder.Markers("points", []geometry.ZYX{{2, 4, 8}})
overlay, err := plugin.NewOverlay(session.Client, "results")
err = overlay.Replace(item)
err = overlay.Clear()
```

`Markers`, `Lines` and `Labels` build the published shapes with an explicit voxel
frame. Lines use `plugin.Segments` or `Loop`. A line set is drawn in 3D and
projected into every 2D slice, dashed outside its own slice, never sectioned.
Thickness is screen pixels in 3D; in 2D a pixel is a voxel for both `thickness`
and marker `size_px`. Labels take a ZYX U8/U16 Tensor and
an exact `[256][4]uint8` LUT. Row 0 belongs to label 0 alone; foreground id `id`
uses row `1 + (id - 1) % 255`. `LabelNames` is a map keyed by label id, not LUT
row. The voxel frame always names the dataset's level-0 lattice; convert geometry
computed at another level before sending. A label volume is a voxel selection:
`frame = "voxel"`, unit scale in `"world"` scale mode, identity rotation,
integer position, and the whole box inside the dataset. With a dataset open,
the host checks this at the push; any invalid item refuses the entire push and
reports host-only `overlay_item_refused` (site 28), with overlay/item/rule detail.
Handle received reports with `Handlers.PeerViolation`; its default produces a
diagnostic containing the named report.
With no dataset open the push is unchecked and compile returns `NOT_FOUND`.
Inline geometry can display but never compile; compile requires segment loans.
Generated items remain editable for style, transform
and optional time scope before sending. Empty replacements and one-shot removal
are both supported. Each buffer is bounded by the 8 MiB inline limit, and the
complete message is checked against control-plane bounds.

Keep exactly one Overlay handle per ID. It serializes revisions and sends,
copies submitted items, propagates admission errors, and never reuses a revision
after refusal. Use its ReplaceLive method for live updates so both lanes share
that revision owner; do not mix in raw updates for the same ID. `Replace` refuses
segment-backed and unknown-field items because it does not own their loans.
Use the typed label methods below, or the raw control API with an explicit
ownership contract for other segment-backed geometry.
Inline labels cannot be used with `CompileLabelVolume`, which requires a segment.

## Text and primitive meshes

```go
label, err := plugin.TextItem("pick-label", "Picked voxel", geometry.ZYX{3, 20, 40},
    plugin.TextOptions{SizePX: 16, ColorRGBA: &[4]uint8{255, 200, 0, 255}})
sphere, err := plugin.MeshItem("pick-sphere", plugin.Sphere, geometry.ZYX{3, 20, 40},
    plugin.MeshOptions{Size: 2})
err = overlay.Replace(label, sphere)
```

These builders need no byte order. Their `Frame` option defaults to `"voxel"`
(level-0 coordinates); `"world"` is also accepted. Nil color uses opaque white.
Text is anchored directly at `anchor_zyx`; position, rotation, scale and
`scale_mode` are ignored. `SizePX` is always screen pixels (zero uses the host's
12-pixel default). Color and depth cue come from the item's style.

Meshes support `plugin.Sphere` and `plugin.Cube`; explicit vertices are refused.
The centre uses the selected frame, but `Size` is isotropic in world units:
sphere radius or cube edge length. Zero selects size 1. The host reads the first
scale component and ignores rotation and `scale_mode`.

Text and meshes render only in the 3D pane. The 2D panes draw markers, line sets
and label volumes. Both helpers return raw protobuf items
that can also be passed to `ReplaceLive`.

## Push and compile segment-backed labels

```go
labels, err := payload.Encode([]uint8{1, 0, 1, 1}, []uint32{1, 2, 2}, true)
var lut [256][4]uint8
lut[1] = [4]uint8{230, 25, 75, 180}
volume := plugin.LabelVolume{
    ID: "labels", Labels: labels, Colormap: lut,
    PositionZYX: geometry.ZYX{3, 20, 40}, T: proto.Uint32(0),
    Names: map[uint32]string{1: "Region 1"},
}
overlay, err := plugin.NewOverlay(session.Client, "segmentation")
err = overlay.ReplaceLabelVolume(session, volume)
// Later, on a worker (never the doorbell listener):
compiled, err := overlay.CompileLabelVolume(ctx, session, volume, true)
if err == nil {
    err = overlay.Clear() // native annotations now draw the same voxels
}
```

`LabelVolume` retains owned CPU data, not lent descriptors. The methods select
the installed O arena by this handle's ID and copy labels and the full LUT into
two fresh one-use reservations for each operation. Thus clearing, replacing or
never committing a preview pins no unused second use. Reusing the typed value
for another compile is safe after the previous loans have been released.
`ReplaceLabelVolume` replaces the whole draw list with this one item and shares
the handle's revision sequence. It rolls back partial allocation and admission
failures. Compilation reports whether admission happened internally: a timeout
or host refusal after admission never triggers local release. The host can
release before replying without causing double release or offset-reuse races.
Compile waits without holding the overlay lock; application job/state
coordination remains yours, as does deciding when to clear after success.

Labels accept ZYX U8/U16 tensors in the deployment's byte order, names keyed by
label ID and optional time scope. Optional `Visible map[uint32]bool` overrides
compiled region visibility by label ID; absent IDs use their LUT row's alpha.
For example, `Visible: map[uint32]bool{1: true, 256: false}` gives two labels
sharing a LUT row independent compiled visibility. Voxel rendering still uses
the LUT: hide a label's voxels by setting its row's alpha to 0, even when
`Visible` is false. Names and visibility are copied when building each item.
The frame is voxel with default world scale mode, unit scale and identity
rotation. With a dataset open, both display and compile require nonnegative
integer positions and an in-bounds box. Push admission is not confirmation of
host application: a fractional position can pass the builder's finite-value
check and still be refused by the host through site 28. Dataset bounds,
permissions and the compile
label-count limit remain host checks. Pane-associated arenas are refused because
this encoding does not bind a pane header. Raw `Replace`/`ReplaceLive`
reject segment descriptors; the typed label methods own their reservations themselves.

For custom raw compositions that reuse pushed reservations, set `SegmentRef.Uses = 2` on
both labels and LUT descriptors **in both messages**. Every message naming a
reservation carries the same declared use count; zero/one declares one use.
Under-declaration allows premature reuse, and over-declaration pins bytes until
retirement. Use WriteLoan and retain those reservations through release.
ServeEnv counts uses at outbound admission for pushes, requests and replies.
An excess use or changed declaration is refused before sending. A refused message
consumes no new use; an already-admitted loan remains the host's to release.

## Panel updates and loan-backed pushes

The host keeps non-image state sent before a declared panel opens and applies it
at open, with options before values. Re-sending state on `PanelOpened` is optional.
`SetPNG` before the P grant keeps an owned copy of the latest image per widget
and row, without allocating shared memory. Installation of the P segment flushes
those images; `PanelOpened` also retries before the application callback. An
admitted `SetRows` drops held images only from that list's removed rows.
`Application.Configure` supplies the manifest's list membership, including for
raw Panel handles; custom control servers provide `Config.Panels`. Closing the dock retains its lease, so background work can survive
reopening. Held bytes are cleared when the session ends.

```go
panel := plugin.Panel{ID: "results", Client: session.Client}
err = panel.SetValue("status", plugin.String("Ready"))
err = panel.SetOptions("mode", "fast", "precise")
err = panel.SetValue("mode", plugin.Int(1))
err = panel.SetPNG(session, panelSegmentID, "preview", encodedPNG)
```

Patch, SetEnabled and SetVisible are also available. Handle every returned error:
a refusal means the message was not admitted. Dropdown patches retain operation
order. SetPNG validates the PNG header, uses the segment's single P arena and
holds published bytes until ReleaseLoan. A write in progress and its unpublished
reservation delay retirement until every declared message is sent or the loan is
released. Arrived deposits hold retirement until their Reply is closed. Normal
retirement without a successor keeps panel images for the next panel grant.
Held images have a 16 MiB budget per panel, including all rows; replacing an image
reclaims its old held bytes. They flush when the P segment installs, even without
`PanelOpened`, and also retry before the panel-opened handler.
Label/chunk pushes and live overlays are quietly dropped and increment
`session.Memory.DroppedPublications()`; allocation pressure and invalid arguments
remain errors.
Cached explicit arena IDs follow an installed `supersedes` chain.

### Lists and row-addressed updates

`ListRows(ListSpec, cells...)` declares a list with a leaf-only row template.
Cell IDs share the panel's ID namespace. `MaxRows` defaults to 256 and
`VisibleRows` to 8; explicit bounds are 1–1024. A selectable list requires a
`CommandRef` from the same definition; a nonselectable list forbids one.
Cell widgets use their usual command references.

```go
selected := definition.Command(plugin.CommandSpec{ID: "select-row"},
    func(ctx context.Context, client *control.Client, args plugin.Args) (plugin.Result, error) {
        row, err := args.String("row")
        return plugin.Result{Message: row}, err
    })
rows := definition.Panel(plugin.PanelSpec{ID: "results", Title: "Results",
    Root: plugin.ListRows(plugin.ListSpec{ID: "items", MaxRows: 64,
        Selectable: true, Command: selected}, plugin.Label("name", "")),
}, nil)
// After building the application and obtaining its client:
panel := rows.Bind(client)
if err := panel.SetRows("items", "first", "second"); err != nil { return err }
if err := panel.Row("second").SetValue("name", plugin.String("Result")); err != nil { return err }
```

`SetRows` replaces the whole ordered key list; an empty list removes all rows.
Keys must be distinct, nonempty UTF-8 strings of at most 128 characters. Bound
`PanelRef` handles check the declared maximum before sending; raw `Panel`
handles also check declared bounds when their client comes from `Application.Configure`.
Custom control servers supply `Config.Panels`; without declarations only the
absolute 1024 ceiling can be checked.
Removing a row drops its cell state; recreating it starts from the template.

`Row(key)` returns a separate value handle: all panel operations, including
`Patch`, `SetOptions`, `SetProgress` and `SetPNG`, carry that row key. The widget
ID is still the template cell's ID. The host checks that the cell and row exist.
Cell command arguments include `row` alongside their usual arguments; selecting
a selectable list sends `row` alone. Both use `args.String("row")`.

The sender coalesces by (panel, widget, row), so different rows remain independent.
With panel declarations, a subset `set_rows` supersedes its pending predecessor
and drops removed rows' pending cells. A returning key preserves the removal
and recreation in order. A newer selection follows its pending rows. An image
freezes its own keys and the panel's pending rows; unrelated cells keep coalescing.
Mixed row patches preserve their operation order, and custom servers without
panel metadata retain ordered row boundaries. Manifest `WidgetCount()`
methods expose the structural count: a list contributes `1 + cells × max_rows`.
This is separate from the 256-node declaration limit, which counts each template
cell once. The Go sender's local queue capacity remains configurable independently.

The host renders lists including image cells and rows set before opening the
panel.

Session.PublishLoan offers the same ownership handling for one-use O bulk pushes.
Its builder receives a SegmentRef, adds shape/dtype metadata and returns one
OverlayUpdate naming it exactly once. Keep revisions serialized with other
updates to that overlay. This API neither guesses a segment ID nor shares a
reservation across messages. Pane-associated application encodings remain an
explicit binding, so this helper rejects arenas with epoch_offset. Use lower-level
WriteLoan for contracts requiring reader replies or multiple declared uses.

## Coordinates and time

`geometry.Box.AtLevel` applies `(e+0.5)/scale-0.5` to edges.
`Box.Centres` converts `ceil(lo)..floor(hi)` to a half-open signed `IndexBox`;
integral bounds include their centre, and narrow intervals may contain none.
`CropAtLevel` applies these operations to arranged `protocol.Geometry`.
It reads `Geometry.levels[level]` per axis, never an assumed `2^level`.
Clamping to another index box is explicit with `Intersect`.

Level scales are sampling factors: downsampling a 31-voxel axis to 15 still uses
2, not the shape ratio 31/15. Once an axis reaches one sample, its declared
factor stops at its last fold: vizi uses `2^min(L, floor(log2(n0)))`. Both
coordinate maps apply unchanged; a three-sample axis frozen at factor 2 maps
its lone coarse centre to 0.5, the centre of the covered block `[0, 2)`, not
the centre of the whole original axis. An axis starting at one sample keeps
factor 1. Read the table even when other axes keep shrinking.

All continuous input coordinates use voxel centres: cell `i` spans
`[i - 0.5, i + 0.5]`. Use nearest-centre rounding, not truncation. A ray through
100 voxels enters at -0.5 and exits at 99.5. The host's encoder shifts
continuous ray/MISS coordinates into this convention; do not shift received
coordinates again. In-image VOXEL indices, column integer ranges and entry
fractions are unchanged.

`Input.Generation` is the low 32 bits of the full
generation in the pane's dataset snapshot.
`PhysicalSpacing` preserves display-fallback spacing as unknown rather than a
measurement. For a polled `ViewState`, use `geometry.PhysicalViewSpacingAxes(view)` for
`(spacing, unit, axisKnown, err)`, where `axisKnown` is a `[3]bool` in ZYX order.
Check `axisKnown[i]` for every axis you use before using `spacing[i]`: one known
axis says nothing about the rest. A true flag means a positive, finite
measurement in `unit`. A false flag means no measurement: NaN in a partly
recorded file, or zero when no axes are known, including on error. NaN or zero
occurs exactly where the flag is false; neither is a measurement. Scale only
axes whose flag is true; leave other axes in voxels:

```go
spacing, unit, axisKnown, err := geometry.PhysicalViewSpacingAxes(view)
if err != nil {
    return err
}
for axis, measured := range axisKnown {
    if measured {
        // spacing[axis] is a physical measurement in unit.
        recordPitch(axis, spacing[axis], unit)
    }
}
```

`geometry.PhysicalViewSpacing(view)` returns `(spacing, unit, known, err)` with
`known` meaning **any** axis is measured, not all. It does not permit using every
spacing value: a partial file still has NaN on unknown axes. Use
`PhysicalViewSpacingAxes` to check each axis.

An absent view or display fallback, even one carrying a unit, returns no
measured axes. Errors return zero spacing and false flags as well; invalid
input values never leak through as measurements. Tools continue to
use their arranged epoch `Geometry`, whose spacing stays finite; a partly
recorded file is `DISPLAY_FALLBACK` there. `NewTimeAxis` validates and copies arranged time values; a nil axis
reports unknown time instead of inventing a uniform step. Neither package
interprets the crop as the lit set or a restriction on data access.
The control state installs and acknowledges non-finite or descending TimeMapping
values with a local diagnostic; raw `PaneState.Time.T` preserves index-wise
lookup even when the stricter NewTimeAxis helper rejects them.

EpochChange is arranged for sessions declaring a tool or subscribing to
`"epochs"`. Without that subscription, panels, readers and exporters can poll
`GetViewState()` for the host-wide view, which may be a tick stale.
`ViewState.VisibleChannels` is the ascending set the view composites (one channel under solo);
`ViewState.C` is deprecated and always 0: vizi has no active-channel control.
For a single-channel operation, choose a channel from `VisibleChannels` as
appropriate for the plugin; the peak ROI and format bridge examples use its
first entry, falling back to 0 when empty.

ServeEnv checks accepted input against its acknowledged pane's level, mapped time
index and crop converted to that level per axis. A non-MISS voxel, pick or the
pick of RAY_AND_PICKED must be inside that crop; ray eye/far points are exempt.
`Position.BackFraction()` returns a `RAY_AND_PICKED` pick's back fraction and a
bool indicating that this kind carries a pick: how far the picked structure
runs behind its front, as a fraction of the ray left there.
`Position.Front()` is the picked voxel's centre projected onto camera → far
(`FrontFraction()` is its fraction, clamped to `[0, 1]`).
`Position.Back()` returns the back point in ZYX voxel coordinates at the input's
level, computed as `front + back_frac * (far - front)`, and false when there
is no back (a point pick or another position kind). A finite fraction is in
`[0, 1]`; zero is a real pick of no depth, its back at its front.
`Position.IsPoint()` is true when the fraction is NaN: nothing stood out, and
there is no back. `Input.Ray()` reads the ray from either ray kind and
`Input.Pick()` reports whether a pick came, so a handler need not branch on
which arrived.

`ToolSpec.WantsPick` set to false asks the host not to compute its pick for
this tool's 3D presses (`wants_pick`, `manifest_version` 1). It is a request
about what the host computes, not a guarantee about what arrives: a host that
predates the field still sends `RAY_AND_PICKED`, and a host that picks still
sends a bare `RAY` when it found nothing. Handle both whichever you declared.
When constructing `Position{Kind: doorbell.RayAndPicked}` for a host probe,
set `Values[11]` explicitly to the fraction or `float32(math.NaN())` for a point.
The union is 48 bytes (`Values [12]float32`); only payload `[92..100)` is fixed
padding. Receivers accept `RAY` and `RAY_AND_PICKED` on any phase: pick presence
is the host's opinion, and its picking policy is off the wire. `MISS` with a
pick is always invalid.

Without a TimeMapping, time bounds are unknown and unchecked. The local
`Frame.InputContextError` is non-nil when context is unusable. The record is still
delivered and `unusable_context` is reported; honor terminal closure even when
its coordinate cannot be used. A standalone doorbell transport does not perform
this check: custom supervisors should use `State.CheckInput` and report the site.

## Verification and reply checks

The tests exercise endian wire bytes, arithmetic bounds, coordinate conversion,
argument presence, typed dispatch over gRPC, callbacks during a host call,
release on failed reads, export channel concatenation and concurrent overlay
revision ordering/refusal. They are not host interoperability certification.

Reader reply shape and dtype are checked against the handle's ReaderOpened:
volumes use (Z,Y,X), planes use (1,Y,X), and both use the declared dtype. A
mismatch returns an error showing both geometries before publication. The
volume-only plane fallback also checks the complete source volume before
slicing. ServeEnv reclaims a rejected unpublished G loan; custom control servers
must supply ReaderMemory for that cleanup. Already-admitted loans remain held.

For raw WriteLoan, successful allocation is only a reservation. If building or
admitting its first descriptor fails, reclaim it locally with Segment.ReleaseLoan
or retry admission; no host release will arrive. After any use is admitted, keep
it until host release or retirement, even if another send fails. Reader callbacks
cannot observe reply admission after returning, and a Call timeout does not prove
non-admission. See the [raw-loan ownership rules and example](../control/README.md#raw-loans-reservation-is-not-admission)
before using this escape hatch.

## Live overlays and contribution subjects

When manifest declarations are available, enable membership checking by listing
the same overlay/panel IDs in the memory configuration:

```go
runtime := control.Plugin{
    Control: cfg,
    Memory: memory.Config{Contributions: map[string][]string{
        "O": {"selection", "preview"},
        "P": {"settings"},
    }},
}
```

Nil Contributions trusts a nonempty O/P subject when the manifest is unknown.
A nonnil map checks membership; an empty map declares none, and an undeclared
subject fails its grant. Reader registration uses `Handlers.Readers` and does not add G subjects,
because G/V/X/S segments belong to exchanges and must carry an empty subject.

After the host installs the segment, create one Overlay per ID and use that same
object for all three operations:

```go
overlay, err := plugin.NewOverlay(session.Client, "selection")
// Check err, then publish items built with GeometryEncoder.
sample, err := overlay.ReplaceLive(session, 0, nil, items...)
```

Segment 0 selects by subject, refusing missing/ambiguous grants. Supply an explicit
segment ID to choose a particular installed grant. For a pane-associated lane,
replace nil with `&memory.PaneEpoch{ViewID: view, Epoch: epoch}` from the captured
arrangement. `sample.Revision != 0` means the slot committed; an error alongside
it is an advisory-notification failure and does not undo publication. A zero
sample with nil error means coalesced. Oversize payloads and non-inline geometry
are refused, never split or truncated.

`Replace`, `ReplaceLive` and `Clear` share the overlay counter; the returned
Sample.Revision is the separate memory slot counter. `Panel.SetPNG` similarly
accepts segment 0 for automatic P-subject selection. Pane-associated Loaned image
encodings still require a separate application contract.

The handshake automatically includes Go's embedded build provenance.
`Control.BuildIdentity` is an optional human label; main/SDK provenance is still
appended. Dirty revisions and missing metadata are explicit. Use
`session.Client.HostIdentity()` for the host identity and `Client.BuildInfo()`
(or `go version -m ./plugin`) for full build metadata. See
[build identity and reproduction](../control/README.md#build-identity).

## Subscribed view and region state

`Definition.OnViewChanged(handler)` and `OnRegionsChanged(handler)` register
both the callback and the required `view`/`regions` subscription plus
`data_access`. Manual interfaces can set `Handlers.ViewChanged` and
`Handlers.RegionsChanged`, or the corresponding `control.Config` hooks, while
including both the subscription and permission in their manifest. Bare
`Subscribe(plugin.View, plugin.Regions)` is also available. The host warns and
ignores either subscription when `data_access` is absent.

Each callback has its own worker and receives an owned protobuf snapshot.
A pending view is replaced by the newest view. Pending region batches merge
by region ID, keeping the newer push's entry regardless of revision (including
a deletion with revision 0); reset discards earlier pending changes and remains set through later
merges. An in-flight callback finishes its own snapshot. Workers may call the
host, run independently of commands and panel callbacks, and are canceled at
shutdown; synchronize shared application state and honor the callback context.
Raw `OnMessage` receives the same coalesced messages when typed hooks are absent.

`ViewChanged` exposes generation, T/Z, visible channels and selected region ID.
`RegionsChanged` gives region IDs, revision, origin and deletion status, not
geometry. Re-read just the affected IDs with `client.GetRois(ctx, ids...)` or
`GetRoisWindow(ctx, start, end, ids...)`; omitted IDs reads all. A missing region
is absent from the reply. `RoiRecord.Revision` identifies changes within the
session; `Name` plus `ContentHash` can key saved computations across sessions.
`ViewState` also exposes `SelectedRegionId`, `TimeIncrement` and `TimeUnit`;
zero/empty frame interval fields mean unknown, never measured zero.

`client.GetColormap(ctx, name)` returns the host's 256-entry RGBA table, checking
its 1024-byte length. `client.RequestSavePath(ctx, &protocol.RequestSavePath{
SuggestedName: "measurements.csv", Extension: ".csv", Title: "Save results"})`
returns a path or `Declined`; the plugin writes the file. Neither call needs a
permission. The save dialog shares RequestAsset's one-picker-at-a-time lifecycle
and separate `Config.PickerTimeout` backstop. SuggestedName must be a filename,
not a path; host validation failures remain `control.HostError`.
Both picker calls work from a command handler, but waiting holds its request
slot; plugins serving reads or other commands
should use `AfterReply` or a background worker/task and return promptly.

## Moving the view in response to a person

A panel or tool declares `ViewTargets: []string{"t", "z", "select_region",
"frame_region", "look_at", "orbit_point"}` (only include the targets it needs).
Use `.MovesView()` on an interactive widget, selectable list or clickable image
whose interaction may move the view. This returns a copy; the containing panel
must declare targets, including for widgets inside list rows. The host draws the
"Let <plugin> move the view" switch as the first row of the plugin's panel. It is
off every time the panel opens and counts only while that panel is open and
showing. It governs both the panel's widgets and the plugin's tools. A tool with
ViewTargets therefore needs a panel that also declares ViewTargets. The switch
is not a widget in your tree: it has no ID and is never reported on the wire.
The plugin learns only what a NOT_ENABLED refusal says. The host judges every
request.

Command handlers use the usual `(context.Context, *control.Client, Args)`
signature. `CommandCause(ctx)` exposes the host's token; `MoveView` and
`DeclineView` supply it automatically. They refuse locally if it is zero.
The token is separate from `Args` and is preserved in `Result.AfterReply` callbacks.
For example, a command bound to a `moves_view` button can jump to frame zero:

```go
func jump(ctx context.Context, client *control.Client, args plugin.Args) (plugin.Result, error) {
    frame := uint32(0) // pointers preserve zero as an explicit target
    reply, err := plugin.MoveView(ctx, client, plugin.ViewTargets{T: &frame})
    if err != nil { return plugin.Result{}, err }
    if reply.Outcome != pb.ViewOutcome_VIEW_OUTCOME_APPLIED {
        return plugin.Result{Message: reply.Outcome.String() + ": " + reply.Detail}, nil
    }
    return plugin.Result{Message: "Moved to frame zero"}, nil
}
```

Answer **every cause** with a move or a `DeclineView(ctx, client)` when there is
nothing to move to (for example, a click in an empty part of an image). Answer
within one second when possible; causes expire two seconds after the interaction.
The SDK does not automatically decline on handler return, since a handler may
schedule its answer with `AfterReply`. Long computation should prepare results
before a click, rather than hold its cause. A held slider supplies a fresh cause
for each value change at up to 30 Hz; do not reuse an older token for new input.

A move applies all targets or none. The **reply** acknowledges it; `ViewChanged`
is not an acknowledgement. Inspect `Outcome`, `Reason` and `Detail`: refusals
such as `NOT_ENABLED`, `SUPERSEDED`, `STALE`, `EXPORT_ACTIVE`, and `UNSUPPORTED`
are normal results, not Go errors. Identical retries under a cause are idempotent
at the host; changing a spent request produces `CONSUMED`. A transport timeout
leaves the outcome unknown. Never invent a cause or issue an unrelated retry.

`MoveToolView(ctx, client, input, targets)` and `DeclineToolView(ctx, client, input)`
use a copied `doorbell.Input`'s `(Tool, Interaction)` pair. They refuse every
phase except FIRE, including PRESS, DRAG and RELEASE. Both primary FIREs and
actions' FIREs qualify. Call these blocking helpers on an application worker,
not the doorbell listener. Vizi supports both command and tool
causes for every target, including `look_at` and `orbit_point`. The host keeps
the orbit point through Fly mode. A tool's switch must be on in an open, showing
panel. A host can still refuse normally.

`ClickableImage(id, command)` binds an image's click to a registered command.
The handler gets `x`/`y` in image pixel coordinates (origin at the top left),
`button` (`left`, `middle`, `right`), `modifiers` (such as `shift+ctrl`), and
`image` (the displayed ImageRef's offset, identifying which published image was
clicked). Image cells also carry `row`. Add `.MovesView()` to obtain a cause.
Plain `Image(id)` stays noninteractive.

`RoiPickerWithOptions(id, value, command, RoiPickerOptions{FollowsSelection: true,
Filter: "mine"})` follows the host's selected region and limits choices to this
plugin's regions. Filter defaults to `all`; only `all` and `mine` are valid.
Selection through this host-drawn control is the person's own action and needs
no view cause. `RoiPicker` uses the defaults.


### Follow: timed movement started by the person

Declare `PanelSpec.FollowTargets` using the same six names as `ViewTargets`,
then register `Definition.OnFollow(panelRef, handler)`. The declaration generates
`follow_targets` in the manifest. A raw `Handlers` interface instead supplies
`Follows: map[string]FollowHandler` keyed by panel ID.

```go
panel := d.Panel(plugin.PanelSpec{
    ID: "tour", Title: "Tour", FollowTargets: []string{"t"},
    Root: plugin.Label("status", "Ready"),
}, nil)
d.OnFollow(panel, func(ctx context.Context, c *control.Client, f *plugin.Follow) error {
    for frame := uint32(0); frame < 5; frame++ {
        moved, err := f.Move(plugin.ViewTargets{T: &frame})
        if err != nil || !moved { return err }
        if !f.Wait(500 * time.Millisecond) { return nil }
    }
    return nil // Automatically sends EndFollow.
})
// Once connected, offer panel state, including before the panel first opens:
err := panel.Bind(session.Client).OfferFollow("Five frames")
// WithdrawFollow() sends the empty label. The person alone can start the tour.
```

Each start runs on its own goroutine. `ID()`, `PanelID()`, `Targets()` and
`Resumed()` describe that run. `Move` takes a whole state, requires at least one
target, rejects targets absent from the host's start locally, and assigns rising
sequence numbers. Pointers retain explicit zero frames and empty selections.
A successful move means queue admission, not host acknowledgement: a bad state
ends the Follow `INVALID`, with a host detail. Camera easing belongs to the host.

`Wait(duration)` returns false on end, as does `Move` (without sending).
`Context()` and the handler's context are canceled on end or session loss.
`Reason()` and `Detail()` expose the host's end notice; the unspecified reason
means no host notice, including on session loss. `End()` requests an idempotent
local stop; its `PLUGIN_ENDED` acknowledgement updates the reason when received.
Returning, returning an error, or panicking also ends the run. Errors and panics
go to `Config.Diagnostic`. A start without a handler is ended immediately.

Honor cancellation and synchronize application state shared with other handlers.
An end is processed independently of panel callbacks, but Go cannot forcibly stop
a handler that ignores cancellation. Session loss sends nothing. A resumed run
is fresh, with `Resumed() == true`; the application retains its own resume cursor.
While a Follow owns a target, conflicting `MoveView` requests are ordinarily
refused with `FOLLOW_ACTIVE`.

### Long-running requests and background tasks

All command, reader and exporter handlers receive a context carrying the current
request's work. Use `control.ReportProgress(ctx, &fraction, "step text")`, or nil
instead of a fraction for indeterminate progress. Check `control.Cancelled(ctx)`
between steps. Reports coalesce per request; text is clipped to 256 UTF-8 bytes.
Outside a request, ReportProgress is a no-op and Cancelled is false.

When stopping, call `control.StopWork(ctx, "stopped at step 3")` on the handler's
own goroutine. It unwinds the handler, runs deferred cleanup, and answers
`Error{CANCELLED}` without diagnosing a plugin fault. The stop signal is not an
ordinary error; its outcome survives even a broad recovery wrapper. Returning
`control.ErrCancelled` (or wrapping it with `%w`) also answers CANCELLED, including
for commands, which normally convert errors to CommandDone with ok=false.
Cancellation is cooperative: the request keeps its slot until the handler returns,
and a handler may instead finish normally. A queued request cancelled before it
starts is answered CANCELLED without calling application code. Session teardown
is separately signalled by `ctx.Done()`.

The SDK places no deadline on incoming handlers. A supervised host lets the
person decide how long work runs. `Config.RequestTimeout` still bounds the
plugin's ordinary outgoing calls to the host, while `Config.PickerTimeout` bounds
its two picker calls. The host's shutdown deadline still bounds teardown.
`control.ShutdownReasonName` spells the `USER_STOPPED` reason `user_stopped`;
it uses the same shutdown path as `not_needed`.

For work that should not hold the session's request slot, start a task with
`client.StartTask("Index the file")`, return promptly, and run it on an
application-owned goroutine (or use `control.AfterReply` to start after the
acknowledgement). A task is cancellable by default; pass false as the second
argument to hide the host's Cancel action. This is UI advice: if CancelWork arrives
anyway, the cancelled flag is set without refusal, diagnostic or violation. The
task may ignore it and complete normally. The task handle has `ID()`,
`Progress(fraction, text)`, `Cancelled()`, `Done(message)`, and `Finish(err)`.
There are at most 64 open tasks; ending one permits another. For example, inside
a worker with a named error result:

```go
func index(ctx context.Context, task *control.TaskHandle) (err error) {
    defer func() { _ = task.Finish(err) }()
    for step := 0; step < 100; step++ {
        if task.Cancelled() {
            return control.ErrCancelled
        }
        if err := ctx.Err(); err != nil {
            return err
        }
        fraction := float64(step) / 100
        if err := task.Progress(&fraction, "Indexing"); err != nil {
            return err
        }
        // Perform one bounded step here.
    }
    return nil
}
```

Finish reports success, "cancelled", or "failed: <message>". Done is idempotent;
reports after it are ignored. Send failures remain errors, so callers can retry
Done when admission was refused. Session end retires all handles, sending final
task outcomes before a graceful stream close when the queue admits them. The
application still owns its goroutines and cleanup; a task handle never stops a
worker forcibly. Use the session context for background work, and join workers
in the shutdown hook when their resources require it.

## Declaring a shaped region type

Declare a type and its callback together:

```go
d.Needs(plugin.ContributeROIs)
d.RegionType(plugin.RegionTypeSpec{ID: "tetra.volume", Name: "Fixed-volume tetrahedron"},
    func(ctx context.Context, c *control.Client, edit *protocol.ShapeEdit, commit bool) (*protocol.ShapeResult, error) {
        // Compute every vertex from edit.Move.Roi and edit.Move.P1.
        return &protocol.ShapeResult{
            Resolution: protocol.Resolution_REJECTED,
            Message: "This position cannot preserve the volume",
        }, nil
    })
d.OnShapeEditEnded(func(ctx context.Context, c *control.Client, end *protocol.ShapeEditEnded) error {
    // Observe end.Outcome and end.Detail; the host decides whether it committed.
    return nil
})
```

A nil callback is allowed and explicitly rejects commits for that type. A type
ID is unique within the plugin, starts alphanumeric, then uses letters, digits,
`.`, `_` or `-`; built-ins and `vizi.` are reserved. An omitted name defaults to
the ID. The host lazily starts the enabled plugin when a region of that type is
selected, including a copy the person made.

`Handlers.Shapers` and `Handlers.ShapeEditEnded` are the corresponding lower-level
registration fields. Preview callbacks and commits can run concurrently; keep
shared state synchronized and honor callback context cancellation. The SDK
supersedes pending previews, runs commits independently of ordinary requests,
validates every returned position and sends the appropriate proposal or answer.
Return one finite control position per input vertex for OK/CLAMPED (the host
compiles display positions), or no positions and a message for REJECTED. See `control/README.md` for worker bounds and lifecycle.
