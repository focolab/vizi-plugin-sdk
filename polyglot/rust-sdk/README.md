# Vizi Rust SDK

A plugin-side SDK for **Vizi protocol 1.x**, implemented from the published
specifications and protobuf IDL. The Cargo package name is **`vizi-doorbell`**.

## What it provides

- A single-session gRPC server on a Unix socket, or authenticated loopback TCP
  on Windows. Protocol 1.x minors interoperate; other majors fail explicitly.
- A command/reader/exporter registry, panel and input callbacks, and a dedicated
  shutdown hook. Implement `plugin::Plugin` directly for a custom dispatcher.
- Typed host helpers for view state, volumes, ROIs, and asset pickers, plus
  message-level requests for every generated `proto` operation.
- Grant installation in either arrival order, preamble identity verification,
  replayable acknowledgements, recovery queries, supersession, and retirement.
- Constant reads, explicit Owned authorization, Pulsed publication/consumption,
  and a shared loan allocator. Payload reads return private `Vec<u8>` copies.
- Panel image loans, bulk geometry loans, inline/live overlay updates, and
  advisory doorbell notifications.
- Manifest schema 0/1 validation, including every fixture in the published schema.
- Typed interface declarations that generate the manifest alongside handler
  registration, plus installation that points to the running executable.

The Session RPC returns response headers as soon as the call opens, before
receiving `Hello`. The SDK still validates `Hello` (including Windows
authentication) before sending `Ready`, then reads requests immediately.
Handshake failures arrive as a gRPC stream status after the headers; headers
alone do not mean the plugin is ready. The plugin's 10-second wait for the host's
`Hello` is independent of the host's launch deadline. The host must send `Hello`
without waiting for response headers, as
[control_plane_protocol.md §3.1](../../docs/spec/control_plane_protocol.md#31-the-handshake)
requires; eager headers also accommodate clients that wait.

## Build and test

```sh
cargo test --locked --all-targets
cargo clippy --locked --all-targets -- -D warnings
cargo fmt --check
cargo doc --locked --no-deps
cargo build --locked --example command_plugin
```

Run these from `polyglot/rust-sdk`. Socket tests need permission to create Unix
sockets and loopback connections. `tests/crosscheck.rs` is an opt-in
cross-language adapter, visibly ignored in ordinary runs. Set `VIZI_V2_CORPUS`
and run `cargo test --locked --test crosscheck -- --ignored` to replay it;
an explicit run without a corpus fails. The crosscheck CI job does this and
requires the resulting verdict file.

Rust 1.88 is the declared minimum. The crate generates bindings from its bundled
IDL at build time, using a vendored `protoc`; no system protobuf compiler or
Python installation is needed. `Cargo.lock` pins the development build.

The default `install` feature adds filesystem manifest writing, installation,
and `ManagementCommand`, using `tempfile` for atomic publication. Applications
that only need declarations and serving can omit that runtime dependency:

```toml
vizi = { package = "vizi-doorbell", path = "/path/to/vizi-plugin-sdk/polyglot/rust-sdk", default-features = false }
```

`PluginBuilder`, validation, `DeclaredPlugin::manifest()` and the runtime remain
available. Enable `features = ["install"]` to restore `write_manifest`, `install`
and the CLI parser. The `command_plugin` example requires that feature.
`tempfile` is still used by tests and the protobuf build tools; disabling the
feature removes it from the SDK's normal runtime dependency graph.

The Linux host-loss process check uses the repository Python environment and
shared emulator. From the repository root:

```sh
cargo build --locked --manifest-path polyglot/rust-sdk/Cargo.toml --example command_plugin
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python polyglot/rust-sdk/tests/host_loss.py
```

It installs a shared-memory grant, kills the emulator without `Shutdown`, and
checks that the plugin exits by itself. It needs local socket permissions.

## A minimal plugin

In your Cargo manifest you can give the package a shorter local name:

```toml
[dependencies]
vizi = { package = "vizi-doorbell", path = "/path/to/vizi-plugin-sdk/polyglot/rust-sdk" }
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
```

```rust,ignore
use vizi::{declare::{report_exit, CommandSpec, ManagementCommand, PluginBuilder}, proto};

#[tokio::main]
async fn main() -> std::process::ExitCode {
    report_exit(run().await)
}

async fn run() -> Result<(), Box<dyn std::error::Error>> {
    const ID: &str = "org.example.hello";
    let Some(plugin) = ManagementCommand::prepare(
        ID, env!("CARGO_PKG_VERSION"), std::env::args_os().skip(1), || {
            let mut builder = PluginBuilder::new(ID, "Hello", env!("CARGO_PKG_VERSION"));
            builder.command(CommandSpec::new("hello", "Say hello"), |_ctx, _command| {
                Ok(proto::CommandDone {
                    ok: true,
                    message: "Hello from Rust".into(),
                    ..Default::default()
                })
            });
            builder.build()
        },
    )? else { return Ok(()); };

    // On Unix this adopts the launcher's inherited fd exactly once.
    let runtime = unsafe { plugin.into_runtime_from_env() }?;
    runtime.serve_env().await?;
    Ok(())
}
```

The same registration supplies the command's manifest entry and handler. The
complete [example](examples/command_plugin.rs) adds a panel. Both handle
`--manifest`, `--install`, and `--help`. No handwritten manifest is required.

### Declaring the interface

`declare::PluginBuilder` owns metadata and the registry. `build(self)` consumes
the builder, validates its schema-1 manifest, and returns a `DeclaredPlugin`
that supplies both to the runtime. The manifest is also available through
`plugin.manifest()`. Serving uses the declarations compiled into the executable;
it does not load a second interface from the working directory.

Use `builder.startup_s(90.0)` to emit optional `entry.startup_s` for a cold JVM or
heavy first start; it is advisory, and plugin behavior must not depend on the
host honouring it.

Commands return a `CommandRef` for use in widgets:

```rust,ignore
let run = builder.command(CommandSpec::new("run", "Run").hidden(), run_handler);
builder.panel(PanelSpec::new("controls", "Controls", Widget::column("root", [
    Widget::label("status", "Ready"),
    Widget::button("run-button", "Run", &run),
])));
```

Use `declare::{PanelSpec, Widget}` for the tree. All supported widget kinds have
typed enum variants; constructors cover labels, buttons, images and containers.
The host creates the panel; update it with the `Context::patch` and
`push_image` APIs. There is no additional runtime UI framework. A widget's
command reference must come from this builder, even if another builder has
registered a command with the same ID.

`reader(ReaderSpec, factory)` and `exporter(ExporterSpec, factory)` associate
extensions with the same IDs used for dispatch. `chunked_overlay` associates
its declaration and source; `overlay(OverlaySpec)` declares a regular overlay.
`tool(ToolSpec)` declares views, triggers, actions and pane buttons, using the
plugin-wide input and lifecycle callbacks (`on_input`, `on_cancel`,
`on_activated`, `on_deactivated`). Read `Input::binding` and `Input::action()`
for the host's tool/action identities. `on_panel_opened` and `on_shutdown` are
plugin-wide callbacks as well.

The spec structs have public typed fields for optional settings; constructors
provide ordinary defaults. `permission` declares each requested capability;
`asset` declares files for the host to provision. Neither is inferred from
handler code. Handlers and factories are not invoked while building metadata.
Keep declarations deterministic across installation and normal execution.

Plugins are trusted code running as the user: `Permission::DataAccess` is a
capability declaration, not a sandbox or an isolation boundary. Vizi does not
add access gates to filter identity, version or parameters to imply isolation
it cannot provide; sandbox vizi and its plugins together if isolation is needed.

Supply pane icons explicitly, usually by embedding them:

```rust,ignore
builder.icon("icons/run.svg", include_bytes!("../icons/run.svg"));
let mut command = CommandSpec::new("run", "Run");
command.pane = Some(PaneButton::new("icons/run.svg"));
builder.command(command, run_handler);
```

Missing/invalid icons, duplicate contribution IDs, invalid panel trees, and
foreign command references fail `build()`. The same schema validator checks
generated and externally supplied manifests. For custom dispatchers,
`plugin::Registry`, the `plugin::Plugin` trait and
`Runtime::new(manifest, application, ...)` are available.

### Writing and installing the manifest

From `polyglot/rust-sdk`, build and run the example:

```sh
cargo build --locked --release --example command_plugin
./target/release/examples/command_plugin --install
./target/release/examples/command_plugin --manifest /path/to/output
```

On Windows run `target\release\examples\command_plugin.exe` with the same
arguments. `--install [DIR]` writes `DIR/<plugin-id>/vizi-plugin.json`; without
`DIR`, it uses `VIZI_PLUGINS_DIR`, then the user's `.vizi/plugins` directory.
An explicit root may be relative. `--install=DIR` and `--manifest=DIR` also work.
These commands run without a Vizi session or a preexisting manifest. The example
uses `ManagementCommand::prepare` with a declaration closure, and main returns
`report_exit(run().await)`. This includes parser and build errors in the report;
help does not build the plugin. Supply the same id and version as the builder.
Applications with another CLI can still call `ManagementCommand::parse`,
`plugin.write_manifest(directory)` and `plugin.install(&options)` directly.

A failed command exits nonzero and writes an `INSTALL FAILED` or `MANIFEST FAILED`
headline to stderr, naming the plugin and destination, followed by the cause,
publication state and a separate `To fix:` block. `report_exit` prints Display
text without Rust's Debug quotes or escaped Windows paths. Success prints one
`INSTALLED: <id> <version> in <directory>` line, including the launch executable,
or a `MANIFEST WRITTEN` line to stdout.
Both success destinations and the paths returned by `install`/`write_manifest`
are absolute paths preserving the supplied directory spelling. Canonicalization
for filesystem operations does not replace aliases or introduce Windows's
verbatim path prefix into these destinations.
Unknown flags get a correction or the accepted options; an unrecognized action
is reported as a command-line failure. I/O errors give a plain cause without OS
error numbers or internal temporary filenames. Recovery advice names the relevant
path and the option for the requested mode. A remaining install lock gets a
platform-specific removal command; first confirm no installer is running. A failure before publication leaves the previous manifest unchanged;
directories or immutable binary generations created earlier may remain, and a
failure after publication says so. `prepare_with_resources` can package additional
application files before success is printed; its failure reports an already
published manifest. It does not roll back that publication. Unexpected management
panics use the same report without a stack trace: unwinding is caught, while an
abort build prints the report from its panic hook before terminating. A scoped
thread-local hook proxy delegates other threads and later application panics to
the previously installed hook.

For installation tests, always pass a scratch `--install=DIR` or set
`VIZI_PLUGINS_DIR` to a scratch directory. Do not test against your real plugins
root or unset HOME to try to disable its fallback.

By default `--install` copies the running executable to
`bin/plugin-<sha256>` (`.exe` on Windows) inside the plugin directory and publishes
that relative launch path. Identical builds reuse the same verified copy;
changed builds get a new name so Windows can keep an old process running.
Old copies remain available until you remove them when no host uses them.
Cleaning or moving the checkout does not break an installed copy. Rebuild and
reinstall after code/interface changes, then rescan plugins or restart Vizi.
Runtime libraries are not bundled; they must remain available to the installed binary.

Use `--install --copy=false` (or `InstallOptions::copy_executable = false`) for
development when the next rebuild should be used without reinstalling. This
retains the declared command, by default the running executable's absolute path;
moving/deleting it breaks that launch. `--manifest` always retains that command
and only writes the manifest and icons.

For another launch arrangement, set `builder.launch_command([executable, arg,
...])` before `build()` and install with `--copy=false`. Copy mode checks that
the command names the running executable and preserves its arguments. Arguments are separate strings, without shell quoting;
relative executable paths containing a path separator resolve in the
**installed plugin directory**. A bare executable name such as `myplugin` is
looked up on **PATH**; use `./myplugin` to name an executable in the plugin
directory. Installation does not execute or probe a custom command. On Unix a
wrapper must preserve the inherited doorbell descriptor, usually by `exec`
replacing itself with the plugin, and preserve stdout/stderr. See the manifest
specification §2.2.

Installation refuses unrelated nonempty directories unless `--force` is given.
It preserves unrelated files, checks the plugin ID is a portable directory name,
and serializes installers with an exclusive lock file. A crash can leave
`.vizi-rust-install.lock`; the error names its full path and explains how to
recover: confirm no installer is running, remove that file, and retry. The SDK
does not infer staleness or let `--force` bypass a lock.
An install reuses a resource directory referenced by the current manifest when
all required icon paths and bytes match, so repeated installs and metadata-only
changes do not accumulate directories. Changed icons go into a fresh directory,
and the manifest is published atomically after validating the complete layout.
Old `.vizi-rust-icons-*`
directories remain available to hosts that already read an older manifest; they
can be removed when no host uses them. The installer cannot determine when that
is safe, so it does not prune old generations automatically. No library bundling or automatic host restart is performed.

Command handlers return `Result<proto::CommandDone>` explicitly. The Python
SDK's return-value coercions (including integer counts as silent success)
do not apply to Rust; set `ok` and `message` in the typed reply.

### Processing stages

Implement `stage::Stage` and register it with
`PluginBuilder::stage(StageSpec::new("filter"), Arc::new(MyStage))`. This
generates the `stages` manifest entry and registers the implementation together.
`Registry::stage` and `Plugin::stages` are available for manually managed
manifests. See [processing_stage.rs](examples/processing_stage.rs) for a complete
u8 inversion plugin with manifest-writing and installation commands.

The host offers stages through **Tools → Apply Filter**. Bump the plugin's
manifest `version` whenever stage output changes: vizi reuses saved output
when plugin ID, version, stage and parameters match. Settings outside `params`
(for example, a model selected in a panel) are invisible to that comparison;
rebuilding or reinstalling alone does not invalidate saved output.

`Stage::plan(ctx, input, params)` receives the offered `StageShape` and checked
per-position `Params`, and returns `StagePlanned`:
the largest output allocation and dtype, the existing channels modified and
their dependencies, any new channels to append, and read-only dependencies in
`reads`. A pose-only stage can name its source channels in `reads` without
claiming to rewrite them. Spatial dimensions must be positive and cannot
grow. Channel indices are identities, not plane numbers. The host assigns new
indices in `StageArm.new_channels`; the process callback receives that arm.
Arms and revisions are keyed by `(pipeline, position)`: one stage can run at
several positions and in several pipelines. Each arm's `output` is its accepted
maximum shape. Plans are matched by input **and parameters**; arming captures
their dependencies and addition keys for that position. Later planning does
not change existing arms. Retirement makes an arm inert but preserves
its revision: reusing that position with a new ring takes the next revision.
Undeclared stages answer `NOT_FOUND`; malformed offered shapes answer
`PROTOCOL`. Author errors retain their chosen kind; panics and invalid author
plans answer `INTERNAL`, except out-of-range channel indices, which §4
explicitly classifies as `PROTOCOL`.

Declare position-specific settings in `StageSpec.params`. They become part of
the generated manifest and are checked by the same parser as hand-written
manifests:

```rust
use vizi_doorbell::{declare::StageSpec, stage::{ParamSpec, ParamType}};
let mut spec = StageSpec::new("blur");
spec.params.push(ParamSpec::new("sigma", ParamType::Float {
    value: 2.0, min: Some(0.5), max: Some(50.0),
}));
spec.params.push(ParamSpec::new("label", ParamType::String {
    value: "run 1".into(), max_length: Some(64),
}));
```

The five kinds are `Float`, `Int`, `Bool`, `Choice`, and `String`; `ParamSpec::new`
defaults the display name to the key. The host sends **every key**, even when
its value equals the default. Defaults are never silently substituted for
missing values. Wrong keys, wire kinds, ranges or choices answer
`INVALID_ARGUMENT` while planning and site 32 without acknowledgement while
arming. Float values are finite, bounds inclusive, and choices case-sensitive.
Integer defaults and bounds must fit signed 64-bit integers, like `Value.i`.
An integral float still uses `Value.f`, not `Value.i`.
Strings use `Value.s` and have a UTF-8 byte limit: `max_length` is 1–4096,
or `None` for the default of 4096 (`STRING_PARAM_MAX_BYTES`). Thus `"é"` fits
2 bytes, but not 1. Empty strings are valid. Text is preserved exactly,
including whitespace and Unicode composition; no trimming or case folding.
Defaults must fit the same limit. String declarations do not accept `min`,
`max` or `options`. Excess length or a wrong wire kind is rejected before
author code, on both planning and arming. Validate the text's meaning in
`plan` and return `Error::unsupported` if it cannot be used.

Authors receive a `Params` map of `ParamValue::{Float(f64), Int(i64), Bool(bool),
Choice(String), String(String)}`. During processing use `slot.params()`: it is a read-only
snapshot from **that position's arm**, not a stage-global setting or a lookup
in the plan cache. Changing a position's parameters requires a new plan and
arm revision. The example's `enabled` boolean demonstrates the full declaration,
planning and processing path. Parameterless stages receive an empty map.

`Stage::process` runs on its own stage worker with a `Context` and `&mut Slot`.
It can make blocking host data requests without blocking the control
dispatcher. `slot.layout` describes the **current** channels, allocation and
dtype; `extent`, `origin` and `pose` describe validity and placement. The SDK
checks the incoming header, including finite pose/origin and capacity bounds,
then checks its dtype and allocation against the arm's input.
`read_voxel`/`write_voxel` use channel IDs and typed `Voxel` values. For bulk
work, use bounded `read_bytes`/`write_bytes` with data-relative offsets and
author-owned scratch. These operations access the shared mapping; no whole
volume is copied automatically, and no borrowed memory survives retirement.

A crop or dtype change must repack **all** planes into the new C-order layout.
Update `layout`, extent, origin and pose to describe the result; appending a
channel also appends its assigned index to `layout.channels`. On `Processed`,
the SDK checks the result and writes these header changes before returning
the turn. The header pose is absolute; `Pose::compose` composes relative
output-to-input physical motion using the old rotation for both rotation and translation products. It does not resample
voxels. Rotations must remain proper rigid rotations; validation checks
finiteness, not an application-specific numerical orthogonality tolerance.

Return `Outcome::Refused` **before writing any data**, as required by
[processing_stages.md §6.3](../../docs/spec/processing_stages.md#63-the-outcome).
Use `Refusal::Other` if changed parameters no longer fit the armed plan, until
the host plans and arms it again. Missing declared input channels are refused
before calling the author, including each channel named in `modifies` or `reads`.
A processed result with a dtype different from the armed output, or an
allocation larger than it, becomes `Failed(Internal)` (reason 2) without
publishing the changed header. Errors
and caught panics return a failed outcome;
**the host discards that volume**, and the SDK does not snapshot or undo writes.
The host controls deadlines and quarantines overdue slots until the turn reaches
a position the host holds, or the holding session ends. After session
teardown, further shared-memory accesses through a Slot fail.

Workers respond to doorbell hints and independently re-check every 10 ms.
One acquire-load establishes pipeline, position and iteration; processing is
once per iteration per slot **at each armed position**, including wrap.
Iteration zero is ignored. Success release-stores `position + 1`; refusal or
failure stores `−position`, preserving pipeline and iteration. Consecutive
stages can therefore run before the host relays D. The SDK still sends each D
to the host; it never signals another plugin directly. Positions without an
armed stage belong to the host, which can observe intermediate results by
placing stages at odd positions.

An S ring has an empty subject and can serve several stages in one session.
The host grants the same mapping under the same segment ID to every session.
Installation validates the region table; variable headers are read and checked
only after acquiring a turn. Unusable arms receive site 32 reports and no ack;
they are never saved to wait for a future grant. The shared
[stage-chain example](../../examples/stage_chain/rust/README.md) chains
two Rust processes, and pairs Rust with the other SDKs in both directions.

### Plugin-shaped region types

Declare `contributions.region_types` with `contribute_rois`, then register a
computation with `Registry::shaper(type_name, handler)`. `PluginBuilder::region_type`
combines declaration and registration; `Plugin::shapers` supports custom plugins.
Use `PluginBuilder::region_type_declaration(id, name)` to declare a type without
registering a shaper. It has the same manifest validation; the SDK answers commits
for that type with REJECTED and a missing-shaper message.

```rust,ignore
builder.permission(Permission::ContributeRois);
builder.region_type("tetra.volume", Some("Volume-preserving tetrahedron".into()),
    |_ctx, edit| {
        let positions = reshape(edit)?;
        Ok(vizi_doorbell::shape::Shape::ok(positions))
    });
```

The callback receives the whole `ShapeEdit`, including the rest-form ROI and
handle movement in voxel-index ZYX before pose, placement and parent transforms.
Return `Shape::ok(positions)`, `Shape::clamped(positions)`, or
`Shape::rejected(message)`. Every accepted result needs one finite position per
input control vertex (`Roi.vertices`), in order; the host applies any compile
operator to obtain display positions. A rejection has none. Errors, invalid
results, panics under `panic="unwind"`, and missing shapers become REJECTED with
a message.
The SDK sets `drag_id` and `seq` and sends the appropriate message.

The same callback serves previews (`seq > 0`) and commits (`seq == 0`). One
preview worker per drag retains only the newest pending edit; superseded results
are discarded. A commit runs independently of previews and ordinary commands.
Callbacks may overlap, including a commit and an earlier preview for the same
drag, so compute from the message and synchronize any shared application state.
Once commit or end arrives, no further proposal for that drag is sent. Running
application computations are not forcibly interrupted; their obsolete output is
discarded, including during session teardown.

`Registry::on_shape_edit_ended` and `PluginBuilder::on_shape_edit_ended` register
an end hook; custom plugins override `Plugin::shape_edit_ended`. The hook receives
the host's outcome and detail on the event worker, including ends for which no
proposal was produced. COMMITTED, REVERTED and CANCELLED are distinct outcomes.
An error in this notification hook is logged as a callback error.

The generated ROI model carries `RoiRecord.type_owner`, `AutoSurface::Star`, and
`InclusionMode::IncludeIntersected`. Non-built-in types absent from `region_types`
are declared geometry, enacted by the host without a shaper. Construct geometry
according to §8.8's rules, which read display space. With a compile operator,
every control marked `enclosing` must be exposed by a display row whose only
nonzero coefficient is exactly 1 for that control; an unexposed enclosing control
is refused in every inclusion mode. Closure, STAR surface derivation and voxel
inclusion remain the host's responsibility. Region types also provide a
lazy launch trigger when a region of that type is selected.

### Long-running work

Command contexts provide `ctx.report_progress(Some(fraction), text)` and
`ctx.cancelled()`. Pass `None` for indeterminate progress. Check cancellation
between steps with `ctx.check_cancelled()?;` to answer the original request
with `Error{CANCELLED}`. To stop with a specific message, return
`Err(control::Error::cancelled("stopped at step 3"))`. These are ordinary
`Result` errors and work with both `panic = "unwind"` and `panic = "abort"`.
Cancellation is cooperative: the request keeps its slot until its handler returns, and a handler may finish
normally if it already has a result. CANCELLED is not diagnosed as a fault.

Reader open/read/metadata and exporter chunk handlers use the same APIs via
`vizi::work::report_progress(...)`, `cancelled()` and `check_cancelled()?` on
the thread serving the request. Outside a handler, reporting is a no-op and
the flag is false. The Context method is named `report_progress` to avoid
shadowing application extension traits named `progress`. A command
context can also be cloned to a worker when that worker is still part of the
request. The SDK registers work before starting
its handler; cancellation received while it is waiting prevents execution.

Propagate cancellation with `?`; an error adapter that replaces every error
kind will also replace `Cancelled`. `work::Cancelled::new(message)` is a public
value convertible into `control::Error`, so it can also be propagated with `?`.

For code with no `Result` in reach, `work::cancel_unwinding(message)` is an
opt-in convenience that requires `panic = "unwind"`. It uses a distinct
payload, bypassing ordinary `Result` adapters without invoking the panic hook.
With `panic = "abort"`, it prints a line to stderr naming that setting and
aborts the process. Check after releasing mutex guards when using this helper:
unwinding through a guard poisons its mutex. Deliberate `catch_unwind` wrappers
must resume a `work::Cancelled` payload unchanged. The SDK recognizes it at
its request boundaries, including reader-open cleanup.

Work that should release the session's request slot belongs on a plugin-owned
thread. Start a task with `ctx.start_task("Index file")`, then move its handle
into that worker. The handle has `progress`, `cancelled`, `check_cancelled`,
and `done(message)`; use `task.check_cancelled()?;` inside the worker.
`done` reports the outcome. `start_task_with_cancellation(title, false)` hides
the cancel affordance for a task that cannot cooperate. This is UI advice:
if the host sends `CancelWork` anyway, the SDK sets the task's cancelled flag
without a diagnostic or contract violation, and the task may ignore it.
Starting a task does not spawn a thread. Keep the handle alive while work runs:
dropping it ends the task. Bare `Drop` reports `cancelled` when the flag is set,
`failed: task unwound` during unwinding, and an empty outcome otherwise; it
cannot infer whether the computation actually succeeded. Use `done(message)`
or `run` when the actual result matters.
`task.run(|task| -> control::Result<_> { ... })` also records errors as `failed: ...` and a `Cancelled` error as `cancelled` before ending it:

```rust,ignore
let task = ctx.start_task("Index file")?;
std::thread::spawn(move || task.run(|task| {
    for chunk in chunks {
        task.check_cancelled()?;
        index(chunk)?;
    }
    Ok(())
}));
```

The opt-in unwind payload also ends `run` with `cancelled`, then continues
unwinding to the enclosing SDK boundary or the caller's `catch_unwind`.

A session allows at most 64 open tasks. Progress coalesces by request/task ID,
and titles and text are clipped to 256 UTF-8 bytes at a character boundary.
Session end cancels and ends all open tasks; retained handles cannot restart
them. `ShutdownReason::UserStopped` (`user_stopped`) uses the ordinary shutdown
path, like `NotNeeded`, even while a request is still busy.

Commands, readers and exporters have no SDK handler deadline. The host decides
how long supervised work may run and can offer Stop plugin after an unanswered
cancel. Timeouts on the plugin's own calls to the host remain dead-session
backstops; shutdown still honors the host's deadline.

### Readers and exporters

`proto::ViewState`, `proto::OpenReader` and `proto::OpenExport` each provide
`file_state_dir() -> Option<&Path>`. Use this accessor for filesystem work:
the raw protobuf field is a wire string, and the accessor maps an absent or
empty value to `None`, never an empty path. On view state it names the open
dataset's folder; on a reader factory's request it names the file being
opened; on an export factory's descriptor it names the **source dataset**,
not the output `locator`. Read it from each new message, since the dataset
can change within a session. The folder need not exist: the plugin creates
it on its first write, and the SDK never creates it on open.

`vizi_doorbell::settings_dir()` is available without a command context,
including from readers and exporters. It reads `VIZI_PLUGIN_SETTINGS_DIR`
and creates that directory on first use, returning its `PathBuf`. It returns
`SettingsDirUnavailableError` when unset, empty, relative, or the directory
cannot be created or accessed. It chooses no fallback. Handle this error if settings are optional for your
plugin. Neither folder is a confinement boundary.

The settings folder usually lives beside the manifest and lasts until the
plugin is removed; the host may choose a fallback when that location is not
writable. `settings/` is reserved: ship no files there, and preserve it on
updates. The SDK's installer preserves existing settings. Per-file state
lives in the dataset's companion directory and moves or is deleted with it;
datasets without a local path have no per-file state folder.

Register a factory with `Registry::reader` or `Registry::exporter`. A `Reader`
returns its `ReaderOpened` metadata and `Volume` values containing bytes, a ZYX
shape, and a dtype. The SDK allocates the reader's `G` deposit and keeps its bytes
reserved until `ReleaseLoan`. The host grants `G` after `ReaderOpened`, waits
for `GrantAck`, and serializes reads into its one-volume arena. Reader replies
always use shared memory. Only level 0 is supported in protocol 1.0; other
levels return `OUT_OF_BOUNDS` before calling the reader. By default,
`read_plane` extracts a plane from `read_volume`, and metadata is empty.

For a file containing multiple images, override `Reader::open(&OpenReader)`
and return `ReaderOpen::Series(SeriesChoice(Vec<SeriesEntry>))` when
`request.series` is `None`. Return `ReaderOpen::Opened(ReaderOpened)` for a
selected image. The factory creates the instance; this hook opens or lists it.
Series IDs belong to the reader and are opaque to the host: `Some("")` is a
selection, distinct from `None`. Entry shape may be empty and dtype may be
`DtypeUnspecified` when a listing cannot cheaply learn them; otherwise shape
is five positive TCZYX sizes and dtype is U8, U16 or F32. Unknown IDs should
return `NOT_FOUND`. A reader implementing only `info` ignores series through
the default `open` hook.

The SDK opens a one-entry list directly, validates listings, and trims large
lists to an ordered prefix with `truncated` set. A listing creates no handle
and leaves a different URI's current reader usable. The listing instance is
retained for the next matching URI/reader ID with a series selected; its
`open` hook runs again without another factory call. A different open,
ten minutes idle, or session end closes it. A selected series must also work
on a fresh instance, as recent-file reopens skip listing.
`Context::view_state()` returns `proto::ViewState.dataset_series`, the open
image's opaque ID as `Option<String>`: `None` for a single-image file;
`Some("")` is a present, empty series ID. Duplicate IDs in a listing are
refused with `INVALID_ARGUMENT`, closing the candidate instance.

Use `ReaderSpec::new("r", "Reader", [".nwb"]).fallback_extensions([".h5"])`
to offer a generic reader alongside a more specific reader for that suffix.
The builder accepts both the resulting `ReaderDeclaration` and existing
`ReaderSpec` values or struct literals. At least one suffix is required across
the two lists; overlap after normalizing case and leading dots is refused.
Compound suffixes such as `.ome.tif` are supported. The host chooses from
declarations without probing files, honors the person's defaults, and asks
on a tie; a fallback never automatically displaces a primary reader.

A session has one current reader, with a handle never reused in that session.
For a different URI, `OpenReader` runs the new factory and validates its metadata
before closing the previous reader. A failed factory or metadata validation
leaves the previous reader readable. Reloading the exact same URI closes the
previous reader first to release exclusive file locks, after validating the
reader ID and finding its registered factory. Bad, ambiguous or unregistered
reader selections preserve the current reader; a valid reload whose factory
fails leaves no reader. Cleanup errors are diagnosed without undoing a successful replacement.

A replaced reader receives no `CloseReader`: the host grants the new `G` with
`supersedes`, waits for its acknowledgement, and then starts reading the new
handle. Reads are refused while more than one `G` is installed. `CloseReader`
is idempotent and closes only its named handle; closing a replaced handle leaves
its successor alone. Reads and metadata for a closed/replaced handle return
`NOT_FOUND` naming it and log that the host is out of step. The host may also
retire `G` without a close announcement, so the registry closes the current
reader during shutdown before the optional application hook, and also on an
unannounced session end through `Plugin::session_ended`. Make `Reader::close`
safe alongside an active callback during shutdown. A close hook updating shared
UI state must check it still owns that state: the next reader's factory may
already have populated the panel.

Voxel payloads use C order with X fastest, little-endian elements, and no row
padding. A reader with a different source layout converts before returning a
`Volume`; its `ReaderOpened.little_endian` must be `true`.

Declare channel names in `ReaderOpened.channel_names`, by index, with `""`
for unnamed channels; the list may stop short of C. The SDK refuses more
than C names with `INVALID_ARGUMENT` and closes the candidate reader.

Tool sessions see `Geometry.channels`; panel plugins can read the same
names through `Context::view_state()?.channels`, in dataset index order
(empty with no dataset). Each `ChannelName` (an alias for `proto::ChannelInfo`)
retains `index`, `name`, and the raw `source` number. `source_name()` returns
`file`, `placeholder`, `user`, `unspecified`, or an unknown number as a string;
`declared()` is true only for FILE or USER. A PLACEHOLDER may label a trace,
but must never be written as a file's declared channel name.

Declare known calibration in `ReaderOpened.spacing_z/y/x`, `spacing_unit`,
`time_increment`, and `time_unit`. Zero and empty strings mean unknown; never
invent unit spacing. Export factories receive the corresponding fields in
`OpenExport`: write only the calibration you were given. Explicit fields take
precedence over a disagreeing reader metadata document per axis; the host can
use that document for axes the reader leaves unknown. Exporters use only the
artifact calibration fields, never the source document to fill unknown axes.

An export factory receives `OpenExport.source_metadata`: the SOURCE's tagged
document. Embed it only as
provenance, never as the artifact's own description. `OpenExport.channels`
is in artifact C order and retains SOURCE indices; write each name only when
`declared()` is true. `OpenExport.provenance` is the host's raw JSON record,
which can be embedded as received. Empty, malformed or non-object JSON does
not fail an export; parsing is the exporter's choice.

An `Exporter` receives each chunk as copied bytes with its time/channel indices.
Implement `write_chunk(&plugin::ExportChunk<'_>)` to record provenance. The
callback's `filter_for(source_channel)` returns the `proto::AppliedFilter`
that produced that channel's volume, or `None` for the file's own voxels.
It combines the filter supplied at `OpenExport` with **this chunk's**
`filtered_channels`. By default, a failed filter timepoint is retried once and
then aborts the export. If the user enables “Write unfiltered data if filter
fails to run,” that timepoint can instead be raw even when the export opened
with a filter. Channels in one chunk can differ. Check each
channel in each callback, rather than copying the open's filter onto every
volume. `AppliedFilter` retains plugin id/version, stage id, all parameters
as protobuf `Value`s, and the filter's rewritten source-channel indices.
The SDK refuses contradictory chunks with `INVALID_ARGUMENT` before either
write callback: `filtered_channels` must be empty without an opened filter,
and every filtered channel must occur in this chunk's `channels`. No bytes
from a rejected chunk are written; the handle remains available for the host
to abort and clean up the partial artifact.

The default `write_chunk` delegates to `write(t, channels, bytes)`. An
exporter implements either callback
and `finish`; implementing neither write callback returns `UNSUPPORTED`.
`OpenExport.shape_tczyx` describes the artifact: T and C are selection counts.
Chunk channels are source indices, so `[3]` is valid when C is 1. The SDK checks
the channel count and payload geometry, without bounding source indices by C.
The time index also names the source and passes through unchanged. Exporters
map each distinct ascending `t` to its rank in the artifact's time axis, and
each distinct source channel to its rank in the artifact's channel axis.

The host grants `X` before `OpenExport` without waiting for an acknowledgement;
the runtime holds chunks until their segment is installed. The SDK copies and
consumes a chunk before `ChunkWritten`, when the host may reuse the arena, and
still sends the required `ReleaseDeposit`.
Both write callbacks return the number of bytes actually consumed. `finish` finalizes;
`abort` abandons and must clean up the partial artifact even after successful
chunks. A known handle's abort always answers `ExportDone`, including
when cleanup reports an error. A failed `FinishExport` consumes its handle,
just as a successful finish does; later writes or finish attempts return
`NOT_FOUND`.

The SDK does not impose a local deadline on command, reader or exporter
handlers; the host decides how long to wait. A running request retains its
slot until it is answered, including after CancelWork. The SDK does not
announce a failed replacement while the same factory is still running, and
never discards a successful handler result solely because it took too long.
Report progress and use cooperative cancellation as described above.

### Calling the host

Inside a handler, `Context::request` sends a typed plugin request and returns an
owning `HostReply`. For example:

```rust,ignore
let mut reply = ctx.request(proto::plugin_msg::Msg::ReadDataVolume(
    proto::ReadDataVolume { t: 0, c: 0, level: 0, ..Default::default() },
))?;
let voxels = reply.copy_payload()?; // private bytes; deposit release is queued
```

A request defaults to 30 seconds; `request_timeout` selects another finite
window. These are **blocking calls for application workers**. From your own
async task, use `tokio::task::spawn_blocking`. Never hold a plugin lock needed by
another callback while waiting for it.

Dropping an unread reply returns its deposit. Timed-out callers never receive a
late payload: the dispatcher retains an uninstalled descriptor until its grant
completes, then returns the reservation. Late or unknown reply IDs are logged
and dropped without faulting the session.
An unrecognized host message kind matching a pending request completes that
waiter with `Unsupported`. Other nonempty IDs receive `Error{UNSUPPORTED}`;
an empty-ID push is diagnosed locally without a reply or session fault.
`Context::asset` reads the launch-time asset-id-to-absolute-path mapping;
runtime `RequestAsset` is a separate request/reply operation.

When a command accepts work that must begin **after its acknowledgement**, use
`Context::after_reply` inside the handler:

```rust,ignore
ctx.after_reply(|ctx| {
    let reply = ctx.request_timeout(
        proto::plugin_msg::Msg::RequestAsset(proto::RequestAsset {
            slot: "input_file".into(), force_reprompt: true,
        }),
        std::time::Duration::from_secs(30 * 60),
    )?;
    // Inspect AssetResult and report the outcome through your panel.
    Ok(())
})?;
Ok(proto::CommandDone { ok: true, ..Default::default() })
```

The SDK sends the successful reply into the outgoing stream before starting
the action on a separate worker. Its requests cannot overtake the reply, even
through different priority queues. Merely spawning a thread inside a handler
does not establish that order. One action may be registered per request;
cloned handler contexts stop accepting registrations when the handler returns.
Event callbacks and `Runtime::context()` have no reply to order against.
Error replies, `CommandDone{ok:false}`, invalid/cancelled requests and failed
reply sends discard the action. Shutdown discards actions not yet started;
running callbacks cannot be forcibly stopped, and their host requests fail
when the session ends. At most 32 registered or running actions are allowed
per session. Callback errors/panics are diagnosed locally, since the command
has already replied; report user-facing outcomes through the panel.

Command handlers may open a picker and wait, but that holds the session's one
request slot while the person browses. Plugins serving reads or other commands
should use the `after_reply` pattern above and return promptly.

`GetViewState` returns `visible_channels`, the channels enabled for display
host-wide; the 3D pane composites at most eight of them. `ViewState.c` is
deprecated and always zero: Vizi has no host-wide active channel.
Authors needing one channel can choose the first entry of `visible_channels`,
handling an empty list explicitly. `ViewState::measured_spacing_zyx()` returns
`[Option<f64>; 3]`. Check `view.axis_known_zyx()[i]` for **every axis you use**
before using its pitch: one known axis says nothing about the rest.
`view.measured_spacing_zyx()[i]` is `None` exactly where that axis is not known,
and `Some` with a **positive, finite** pitch elsewhere. Measurements require
file/override provenance and a nonempty unit. A partly recorded file has NaN
for unrecorded axes on the wire; leave those unknown axes in voxels.
`spacing_known()` answers whether **any** axis is measured; a true result does
not license using every axis. `ViewState::physical_spacing()` answers for
**all three** axes: `Some([z, y, x])` only when all are known, positive and finite,
otherwise `None`. `Geometry::physical_spacing()` and
`PaneState::physical_spacing()` differ: tools keep finite geometry, and
a partly recorded file reaches them as `DISPLAY_FALLBACK`, not calibration.

`ViewState` also exposes `selected_region_id` (empty for none) and
`time_increment`/`time_unit` (zero/empty means unknown). To follow changes,
declare `Subscription::View` and/or `Subscription::Regions`, together with
`Permission::DataAccess`, then register `on_view_changed` or
`on_regions_changed` on the builder or registry. Direct `Plugin`
implementations override `view_changed`/`regions_changed`. Missing permission
warns and disables these subscriptions.

Each subscription has its own worker and may call `Context` host helpers.
View updates arriving while its callback runs keep only the latest state.
The callback receives `proto::ViewChanged.filter: Option<proto::AppliedFilter>`;
`None` means no filter. A generation-zero close clears the view and filter.
Pending region updates merge by region ID in arrival order: an entry in a
later push replaces the earlier entry, including a deletion with revision zero.
This relies on the control stream's ordered delivery; revisions are not a
complete sequence for all changes because deletions carry zero. A reset clears
earlier pending changes and remains set through subsequent merged updates.
Callbacks are serialized within each subscription but can run concurrently
with each other and with other application callbacks. Session shutdown drops
pending state and wakes host-request waiters; running application code must
cooperate with shutdown as with other callbacks.

`ctx.get_rois_selected(&region_ids, pose_t_start, pose_t_end)` reads named
regions with optional inclusive pose bounds. Empty IDs means all regions;
missing IDs are omitted. Returned `RoiRecord`s preserve `revision` and
`content_hash`: revision detects changes within a session, while name plus
content hash identifies saved results across sessions.
Tools read calibration from their pane epoch. A tool-less plugin can either
poll view state or opt into epochs with
`builder.subscription(Subscription::Epochs)` (top-level `subscriptions:
["epochs"]`). The runtime acknowledges time mappings and epochs and exposes
them through `ctx.pane(view_id)`, independently of tool registration.
Epochs carry geometry, visible channels and time mapping, but never the current
frame `t`; read `ViewState` for the frame on screen.

### Panels, overlays, and input

Labels display plain text by default, including strings that look like HTML.
Use `Widget::label_with_format("help", "<b>Help</b>", TextFormat::Rich)` (from
`declare`) to request Qt's rich-text subset. `Widget::label` and the
`Widget::Label { id, text }` form are plain; an explicit `TextFormat::Plain`
also omits the default field from the manifest. The format applies to the
initial text and all later `panel.set_value` updates, including list cells.
Rich-text links are styled but never opened. Only labels accept `text_format`;
the wire values are `"plain"` and `"rich"`, not `"html"`.

An overlay needs a way to start its plugin. Manifest validation requires a
panel, reader, exporter, stage, or a command or tool with a Tools-menu entry
or pane button whenever overlays are declared. Keybindings and tool triggers
alone do not count, because another binding can take them. Opening a panel is a usual entry point.

`Context::patch` sends panel operations. A host refusal of an individual op is
reported as `PANEL_PATCH_REFUSED` (site 29), with the panel, op, widget and reason
in its detail. Neighboring ops in the patch still apply. This host-to-plugin
report is logged by name, number and count, including failures such as an
undeclared widget, unreadable image, invalid PNG or unsupported widget operation.
`push_image` reserves PNG bytes from the panel's `P` arena. Before the arena is
installed, the SDK holds the latest bytes per `(panel, widget, row)` and returns
normally. `PanelOpened` flushes them before invoking the application hook, so
new state from that hook wins. Retirement also keeps the latest image while
the panel awaits a replacement arena. Whenever a P grant completes, held images
flush automatically, including after a crash relaunch with no `PanelOpened`.
This does not invoke the application hook. `set_rows` removes held images for removed rows
of that list, including through raw patches. Held images have a session-wide
64 MiB / 65,536-entry budget; overflow is refused locally. Before a grant, a single
panel holds at most 16 MiB across all image keys, matching vizi's P arena budget;
replacing a key subtracts its old bytes before checking the limit. During replay a failed image is
dropped with a diagnostic; other images and the panel-open hook still run.
For a list, `ctx.push_image_rows(panel, widget, &rows)` takes `(String, Vec<u8>)`
row-key/PNG pairs and publishes up to 64 images per patch. Held-image replay
uses the same batch size. Each image keeps its own loan; an allocation or
admission failure cancels unsent reservations. Earlier batches may already have
been published if a later batch fails.
`set_overlay` sends a full overlay update; bulk geometry can use
`memory().try_reserve_push` and `push_with_loans` to return unsent reservations
if admission fails. `try_reserve_push` follows `supersedes`; it returns `None`
quietly if retirement has no successor (counted by `retirement_drops()`), or if
the arena is full (counted by `SegmentShortfall`). Once sent, the host's
`ReleaseLoan` controls reuse while the segment remains live.
`set_overlay_live` selects and publishes into the current live arena atomically;
it returns `false` quietly when retirement leaves no replacement.

For segment-backed geometry, set `SegmentRef.uses` to the total number of
messages that will name the reservation. Pushing a label volume and then
compiling it uses the labels and LUT twice: when that compile is certain, set
`uses: 2` in both descriptors and keep that same count in both messages.
If compilation may never happen, use `uses: 1` for the push and reserve again
for compilation; a declared use cannot be withdrawn. Zero and one both mean
a single use.

The descriptor is ordinary generated Rust data, so the count is explicit:

```rust,ignore
let labels_ref = proto::SegmentRef {
    segment_id: labels_loan.segment_id,
    offset: labels_loan.offset,
    len: labels_loan.len,
    shape_zyx: vec![z, y, x],
    dtype: proto::DType::DtypeU16 as i32,
    uses: 2, // overlay push + CompileLabelVolume
    ..Default::default()
};
```

Use the payload's actual shape and dtype when composing the geometry. Reserve
once for each payload and reuse its descriptor. `push_with_loans` and
`cancel_unsent` may reclaim a reservation only if **no message naming it has
been admitted**; a later failed send cannot roll back an earlier accepted use.
The SDK counts admitted messages for each reservation and refuses excess uses,
inconsistent declarations, and duplicate references within one message before
enqueueing it. Image references have one use. Failed messages consume no uses;
`push_with_loans` attempts cleanup of every supplied loan and returns the
original send error. Every reserved loan must be published or cancelled with
`cancel_unsent`; forgetting an unsent reservation holds retirement back.
Once admitted, every declared use must be sent. If a later publication fails,
the reservation becomes stranded: its bytes stay lent, but once already-admitted
messages reach transport it holds no retirement claim. A retry can reacquire
that claim while the segment remains live. A reservation with missing uses stays
outstanding until the host releases it or the segment/session closes.

The SDK exposes `proto::OverlayLabelVolume` directly, with no label-volume
builder. A custom builder making multiple reservations must validate first
and call `memory().cancel_unsent` on every earlier reservation if a later step
fails, before returning the error. Never fabricate a `ReleaseLoan` to reclaim
unsent bytes. The interactive-seg example's `overlay::label_item` follows this
rule and tests failure of its second (LUT) reservation. Its optional `label_visible` map overrides visibility per label id
for compiled regions; an absent id falls back to its LUT row's alpha. The map
does not change the displayed voxels, which still use the LUT. To hide a label
on screen, set its LUT row's alpha to zero as well.

Higher `OverlayStyle.draw_priority` draws on top in both 2D and 3D. In 3D, give
an enclosing label overlay a lower priority than the labels inside it, or it
will hide them. Reducing opacity does not solve this: 30% opacity per voxel
accumulates to an opaque result through a thick region.

`OverlayLabelVolume.label_names` is a map keyed by label ID, not LUT row. LUT
row zero belongs only to label zero; a foreground label uses row
`1 + (id - 1) % 255`. An overlay's `frame = "voxel"` names the dataset's
**level-0 lattice**. Convert geometry computed at another pyramid level before
sending it; an item scale is not a pyramid-level declaration. A label volume
must be a voxel selection: voxel frame, unit scale in world scale mode, identity
rotation, integer position, and its whole box (including zero labels) inside
the dataset. The host checks this at the push and refuses the whole update on
failure, reporting `OVERLAY_ITEM_REFUSED` (site 28). With no dataset open, the
push's box check is deferred and compilation returns `NOT_FOUND`. Inline label
geometry can display but cannot compile; compilation requires segment-backed
geometry. The manifest's fixed overlay arena must fit the labels and LUT.
Received host violations are logged with their site name, number, count and
detail; unknown future sites retain their number and detail.

Text and mesh overlays use the generated messages directly; no separate
builders are required. Both render in the 3D pane and remain 3D-only.
`OverlayLineSet` also draws in 2D: projected into every slice, solid when both
endpoints round into the slice and dashed by arc length otherwise, never
sectioned. In 2D, `thickness` and marker `size_px` are level-0 voxels and follow
zoom; lines thinner than a voxel draw one voxel wide. In 3D they are screen
pixels, including line thickness.

- `OverlayText.anchor_zyx` is a point in `transform.frame` (level-0 coordinates
  for `"voxel"`); the rest of the transform is ignored. `size_px` is screen
  pixels, with zero selecting the host's 12-pixel default. Colour and depth cue
  come from the item's style.
- `OverlayMesh.geometry` selects `"sphere"` or `"cube"` (empty means cube).
  `transform.position_zyx` places its centre in the declared frame, while
  `scale_zyx[0]` sets an isotropic size in world units: sphere radius or cube
  edge length. Other scale components, rotation, and scale mode are ignored.
  Explicit vertices are deferred and refused by the host as a wire error.

`set_overlay_live` serializes a self-contained `OverlayUpdate` into its `O`
segment's Pulsed lane. Geometry must be inline. Supply the pane/epoch pair when
the installed lane declares a header. **Your overlay revision must increase
across both control and live updates**; it is independent of the lane's internal
publication revision.

`on_input` receives the decoded event, its tool binding, and available pane
state. Coordinates retain the event's level and ZYX order. `NONE`, unknown time,
and display-fallback spacing stay absent. Check `usable_context` before using
coordinates; a terminal still closes its interaction when context is unusable.
The SDK reports site 27 with the failed context clause. Ray eye/far endpoints
are exempt from crop checks; a ray's picked voxel is checked. Without an
arranged time table, the time index remains unchecked.

`Position::RayAndPicked { ray, picked, back_frac }` carries the host's picked
voxel, the front of the structure it found, and how far that structure runs
behind its front. `position.front()` is the picked voxel's centre projected
onto `camera → far` (`front_frac()` is its fraction, clamped to `[0, 1]`), and
`position.back()` returns `front + back_frac * (far - front)` in the event's
level and ZYX order. `back_frac` is in `[0, 1]`: `0.0` is a real pick of no
depth, its back at its front. NaN is different: nothing stood out,
`position.is_point()` is true and `back()` is `None`. All three return `None`
or false for other kinds. `event.ray()` is the ray under either ray kind and
`event.pick()` the `RayAndPicked` position or `None`, so a handler need not
branch on which arrived.

`ToolSpec::wants_pick = false` asks the host not to compute its pick for this
tool's 3D presses. It is a request about what the host computes, not a
guarantee about what arrives: a host that predates the field still sends
`RayAndPicked`, and a host that picks still sends a bare `Ray` when it found
nothing. Handle both whichever you declared. Both ray kinds are accepted in any
phase: the host's choice to attach a pick is its policy. `MISS` cannot carry
a pick.

Voxel `i` is centred on `i`, occupying the closed cell `[i - 0.5, i + 0.5]`.
For example, a ray through an axis of length 100 enters at -0.5 and exits at
99.5. Continuous MISS coordinates use the same convention; integer VOXEL and
COLUMN indices and the ray's entry fraction do not need an offset.
`edge_at_level(edge, scale)` maps a level-0 edge by `(edge + 0.5)/scale - 0.5`.
Take each axis's scale from the captured `Geometry.levels[event.level]`, as the
SDK's own context check does, then use ceil/floor for included centres. Never
substitute `2^level` or a ratio of array shapes. The host's factor stops when an
axis reaches one sample (`2^min(L, floor(log2(n0)))`); the same affine map still
applies without a special case. Converting back uses `(c + 0.5)*scale - 0.5`.
`on_cancel` handles local cancellations separately from wire events. A snapshot
that an application has retained remains valid after a newer epoch is installed.

Tools are modal: the engaged tool owns the primary pointer button. Manifest
`triggers` request keys or mouse chords that engage it; `actions` declare keys
or pane buttons that fire into it, including mid-drag with the pointer's current
sample. `ToolBinding.triggers` contains the assigned subset, and `actions`
contains the action indices, manifest IDs, and assigned keys. A refused action
key is empty; the action may still be reachable through its pane button.
`InputEvent.action` is the wire's u16 index. In `on_input`, use
`input.action()` to resolve an action FIRE to its manifest ID. Zero, unknown
indices, and non-FIRE events return `None`. Every FIRE leaves the open gesture
intact, and unknown actions are delivered without a diagnostic.

The manifest `binding` field is rejected. `mode`, `cancel_on_camera_move`, `wants_pick`,
`triggers`, `actions`, and `pane` require manifest version 1. Pane declarations
validate relative `.svg`/`.png` paths, the 64 KiB size limit, and file signatures;
the host checks rendering. `Manifest::load` resolves icons beside the manifest.
For JSON strings, `Manifest::parse_in_dir` supplies that directory explicitly;
`Manifest::parse` uses the working directory.

Unusual time mappings and undeclared tool bindings are installed and
acknowledged with a local diagnostic. Non-finite time values remain available
as supplied; check a value before using it as a physical time. Input for an
undeclared tool is diagnosed and dropped. Duplicate dataset entries keep the
last entry, duplicate pane entries keep the first, and panes above 255 are
available through `pane_by_id` but receive no doorbell input. Zero identities,
revision regressions, and changed content under the same revision are still
refused under the protocol's arrangement rules.

### Chunked overlays

Declare an overlay with `"chunked": true` and `"time": "per_t"` (the default)
or `"static"`. Its arena is sized by the host; `segment_mib` is refused.
Register a source on `Registry`:

```rust,ignore
use vizi_doorbell::plugin::{Chunk, Registry, Volume};
let plugin = Registry::new().chunk_source("labels", |ctx, want| {
    let input = ctx.read_volume(want.t.unwrap(), 0, want.level, None)?;
    let labels: Vec<u8> = input.data.into_f32().into_iter()
        .map(|v| u8::from(v >= 400.0)).collect();
    Ok(Some(Chunk {
        labels: Volume { bytes: labels, shape_zyx: input.shape_zyx,
                         dtype: vizi_doorbell::proto::DType::DtypeU8 },
        position_zyx: [0, 0, 0],
    }))
});
```

The SDK runs one worker per source. It serves each want once per continuous
membership, in host order, and applies every restated set on receipt. `None`
returns an empty chunk without a loan. `Chunk` carries u8/u16 labels on the
wanted level's lattice, always sent through that overlay's arena. A full arena
retains the answer and waits for its `ReleaseLoan` or a set change, without
counting a shortfall. Leaving and returning gets a fresh answer even if it
happens while the source is running. Errors and panics are diagnosed locally
and the next want is served.

`ctx.wants(id)` returns the latest ordered set. `want.t` is `Option<u32>`:
`None` for a static overlay is distinct from `Some(0)`. Use
`ctx.invalidate_chunks(id)` after changing results; it re-serves the current
set and discards any answer still computing or waiting for room.
`ctx.publish_chunk(id, &want, chunk.as_ref())` supports manual publication;
it rejects a departed want and returns an arena-full error without a
shortfall. Registered sources own the release-driven retry automatically.

`ctx.set_label_table(id, &LabelTable { colormap, label_names, label_visible,
style })` sends the `(256, 4)` u8 LUT inline, with names and visibility keyed
by label id. The table is coalescing state per overlay at the host; this SDK
keeps application pushes in order without merging them. The generic loan
walker also covers raw segment-backed chunk labels and table colormaps,
including admission accounting and `push_with_loans` failure cleanup.

A chunked overlay accepts `OverlayChunk` and `OverlayLabelTable` only;
`OverlayUpdate` is refused on site 30 (`OVERLAY_CHUNK_REFUSED`). It never
compiles (`CompileLabelVolume` returns `INVALID_ARGUMENT`). `OverlayRemove`
clears its chunks and table. On a dataset change the host sends an empty want
set before superseding its arena; the SDK acknowledges the replacement,
sends `SegmentReleased` for the old arena after any pending publications finish,
and answers a later retirement of the old id again. New reservations use the replacement. Shutdown wakes chunk
workers, cancels their pending host requests and joins them before the shutdown
hook and its report (subject to the host's shutdown deadline).

## Scheduling, memory, and bounds

The runtime starts the doorbell listener **before** exposing the gRPC endpoint.
Requests, input/panel callbacks, memory polling, and shutdown have separate
workers. A blocked command cannot block arrangements or its own host replies.
Keep input callbacks short; offload substantial computation to a bounded worker
of your own. Event backlog beyond 256 is a visible session fault, not a silent
loss of a press, release, or cancellation.

The memory poller checks host-produced Pulsed regions every 10 ms after the
previous scan completes, whether any `D` arrives or not. It delivers values to
`Plugin::pulsed`; do not compete with it by independently consuming the same lane.
The low-level `Memory` API leaves scheduling to its embedder.

Local policy bounds include 32 pending plugin requests, 32 deferred descriptors,
256 messages per outbound application/one-shot queue, a 64 MiB queued-byte
ceiling, 1024 open exports, 65,536 segment/tool/pane identities per session,
and a configurable 4 GiB mapped-memory ceiling. Queue overflow on a one-shot
path faults the session; a refused application state push returns an error.
Application state pushes use a FIFO without sender-side coalescing, so rapid
overlay or panel updates can fill the 256-message queue. Callers can throttle
updates or retain the latest value for retry. For large lists, batch descriptor-free
ops in `Context::patch`: 256 rows × three per-cell calls plus `set_rows` would
need 769 queue entries. One `set_rows` patch, eight patches each containing 32
rows × three cell ops, and a final list selection need only ten entries. Keep
each batch within the wire bounds (at most 65,536 repeated elements and 32 MiB
per message). `push_image_rows` batches image messages too: 1024 swatches occupy 16 queue
entries, while retaining separate loans. Pace repeated publications if needed. The FIFO preserves each row reset,
selection and image dependency; it performs no image freeze or replacement of
pending `set_rows`, including subset updates. The writer allows five seconds
per message for output progress before ending the session; during teardown,
queued messages share a five-second drain window.
There is one cursor per loan arena, with 64-byte reservation padding. It rewinds
when all loans have returned, so fragmentation can refuse a push before the
entire arena is occupied. Shortfalls are counted and reported for host policy.

An operation never exposes a borrowed slice into a mapping. A single memory
mutex covers raw copies, Pulsed claims, and unmapping. A shared in-flight count
is taken before the authoritative retirement check. Retirement also waits
for outstanding Owned authorizations, STAGED turns, and delivered deposits
until `HostReply` copies or discards them. Loan reservations pin the mapping
through all declared descriptor publications entering the ordered transport;
`SegmentReleased` cannot overtake those messages. A rejected unsent publication
can cancel its reservation even while retiring. The release follows final
unmapping and handle closure, without waiting for already-published loans to be
returned by the host.
Ending the last claim settles retirement immediately; the runtime sends the
receipt without needing another incoming message. Concurrent settlement cannot
emit it twice (an explicit repeated `RetireSegment` still replays its receipt).
At unmap, the SDK remembers each outstanding loan's `(region, mapping offset,
length)`. A late `ReleaseLoan` matching one settles it once and increments
`memory().crossed_releases()`. Unknown IDs, mismatched tuples, loans returned
before unmap, and duplicate returns report `unknown_release`, including after
release. Deposit claims start on descriptor arrival, before delivery to a
request waiter or callback; a released mapping can never be read.

Panel-image and chunk helpers select the live arena and reserve atomically.
Panel images wait for a replacement; other best-effort work without a successor
is dropped quietly and counted by `retirement_drops()`. `try_reserve_chunk`
returns `None` for either arena exhaustion or retirement without a successor,
so the caller can retain the answer for retry. Exact-ID Pulsed reads, Pulsed
publishes and new STAGED turns also quietly decline retired segments. For custom
best-effort geometry use `try_reserve_push` as above. The strict low-level
`reserve` and exact-ID read methods still return checked errors for unavailable
segments; they do not fabricate a loan or successful read. There is no
timeout-based reuse of someone else's bytes. Session close may invalidate
leases, but serializes against every copy, so subsequent accesses fail safely.

Field bounds are checked reflectively in both directions. Each check re-encodes
and dynamically decodes the message, adding copying and allocation costs for
large inline buffers and ROI lists. The 8 MiB field limit applies to all byte
and UTF-8 string values, including list elements and map keys/values.

Shutdown refuses new requests, runs the hook on a dedicated thread, reports the
hook outcome, then closes the stream. It stops the doorbell immediately, even
without a preceding `DoorbellShutdown`. The host's `deadline_ms` is honored,
including values above five seconds; absence defaults to five seconds and zero
allows no grace. A Rust callback cannot be forcibly stopped:
on timeout its worker is detached, and subsequent SDK operations fail after
session teardown. The host remains responsible for process termination.
Standalone `InstallSegment` arrangements are still applied and acknowledged
inside that window; installations riding on refused new requests are ignored.

Request admission and the final handoff to gRPC are serialized with shutdown.
The SDK drops requests still queued locally, including a writer's batch waiting
for output capacity, and wakes pending host calls with errors. Requests already
handed to gRPC may still reach the host; cancellation cannot retract their work.
Replies, releases and other cleanup messages can still drain. Errors returned by
ordinary callbacks, subscription handlers, after-reply actions and chunk sources
during teardown are treated as cancellation rather than logged as failures.
The shutdown hook's errors and panics still produce a failed shutdown report.

For application-owned workers, check `ctx.is_shutting_down()` after a failed
host call and before starting another job. The flag is set before pending calls
are cancelled; no error-message matching is needed. An SDK call can race the
check, so still handle its `Result`. In `on_shutdown`, signal your own stop flag,
wake any idle condition variables/channels, and join cooperative workers. The
hook runs after SDK host calls have been unblocked. Keep the same stop/join path
after serving returns to cover host loss without a shutdown hook. See
region_quant's `Traces::stop_and_join` for a thread/condition-variable example.
The SDK cannot safely interrupt arbitrary Rust code or wake an application-owned
wait; a worker blocked on unrelated I/O still needs its own cancellation policy.

One plugin process serves one session. `Runtime::serve_env` / `serve_unix`
return after the shutdown report, a session fault, or loss of the control
stream, including a host crash with no `Shutdown`. End `main` when serving
returns; do not wait for another session. Teardown releases mapped segments
and wakes pending SDK requests. Application-owned workers must also be able
to stop; an unbounded join after serving returns can still keep a process alive.

A doorbell that closes ends its session (§8.13). The runtime reports the fault
and keeps control open for the host's `DoorbellShutdown` followed by
`Shutdown{session_fault}`. Doorbell EOF likewise waits for host-directed
shutdown; the plugin does not close the control stream on its own.
Loss of the control stream ends the session regardless of doorbell state.

## Lower-level transport

The doorbell handle/listener API is available on its own. Its transport-only
contract is documented in [DOORBELL.md](DOORBELL.md); the full runtime performs
the supervisor responsibilities described there. [WINDOWS.md](WINDOWS.md)
describes the Windows doorbell setup.

### Typed host requests and panel values

From a command or background worker, `ctx.view_state()` returns the current
`ViewState`; `ctx.read_volume(t, c, level, roi)` returns a `HostVolume`. `roi` is
`None` for the whole volume or `Some((min_zyx, max_zyx))` for half-open bounds at
that level. The helper checks echoed indices and the requested ROI shape. Its
`VoxelData::{U8,U16,F32}` owns aligned Rust vectors; `into_f32()` is convenient
for numerical code. The host deposit is released on copy, or on refusal. The
caller still checks `open_generation` across multiple requests.

Use `ctx.read_volume_raw(t, c, level, roi)` to request the file's own voxels even
when *Plugins See Filtered Data* is on. It sets `ReadDataVolume.raw`; the reply
carries no `filter`. `read_volume` sends `raw: false` and follows
the person's sharing setting. `ViewState.filter` still names what the views show.

`HostVolume.filter: Option<proto::AppliedFilter>` describes **that reply's**
voxels. `None` means the file's own data, including unmodified channels,
higher pyramid levels, or timepoints where the stage refused, failed or timed
out. `ViewState.filter` separately describes what the views show; never infer
read provenance from it or from a preceding reply. Compare `filter.plugin_id`
with your own id to detect reads of your own stage's output, or record the
whole filter message to describe the input to an analysis. Export factories
also receive `OpenExport.filter`, with per-volume confirmation available
through `ExportChunk::filter_for` as described above.

`ctx.get_rois()` returns the host's ROI records posed at the current frame.
`ctx.get_rois_window(Some(start), Some(end))` requests an inclusive pose window;
either bound can be omitted. Returned `RoiRecord`s preserve
`pose_domain_t_start`/`pose_domain_t_end` on **every** region with a pose domain,
including unwindowed reads. Compare that pair with `roi.t_start`/`t_end`:
equality means the same domain, while different bounds identify a slice.
Presence alone does not identify a window. Unwindowed reads still have their
dense pose rows cleared; use an explicit window to read the rest form and its
rows. Do not replace a whole contribution with a partial window and lose the
other frames. If a requested reply exceeds the message ceiling, the host returns
`OUT_OF_BOUNDS` with its size and limit, without clipping or ending the session;
retry with fewer frames. An unwindowed read supplies the domain for sizing. `ctx.extend_roi_poses(extensions)` submits
dense corrections or adjacent extensions and returns the host's region/frame
counts. The host validates ownership, matching components, no gaps and the
whole batch before writing; its errors remain ordinary `Result::Err` values.
Pose-extension components have the same 1,048,576-element limit as ROI poses.
The host rejects nonfinite ROI values and pose domains outside the dataset's
frames with `INVALID_ARGUMENT`.

`ctx.read_roi_inclusion(region_id, t)` (requires `data_access`) asks the host
for its own level-0 inclusion raster, posed through the region's parent chain
and clipped to the dataset. The returned `HostRoiInclusion` distinguishes a
hidden region (`present == false`) from a present region covering no voxels
(`present == true`, `voxel_count == 0`); both have `volume == None` and no
deposit to release. Otherwise `volume` owns U8 membership bytes (0/1), a
`shape_zyx`, and the bbox's level-0 `origin_zyx`. The helper checks region/frame
echoes and membership/count consistency, and disposes of its deposit use after
copying or refusal. Check `open_generation` when combining it with volume data.
`NOT_FOUND`, `OUT_OF_BOUNDS` and `UNSUPPORTED` are ordinary host errors.
Dropping an unread message-level reply also disposes its deposit; multiple
uses release only after the last one.

`ctx.read_roi_inclusion_clipped(region_id, t, Some((lo, hi)))` further clips
the raster to a half-open level-0 ZYX box. Each endpoint is a `[u32; 3]`.
Empty boxes are refused locally; the host checks dataset bounds and its voxel
budget. The copied bounding box must fit the requested clip. Pass `None` for
the same behavior as `read_roi_inclusion`.

`ctx.request_asset(slot, force_reprompt, timeout)` preserves clean cancellation
as `AssetResult.declined`. The caller chooses its patience; the host holds its
command deadline while its picker is visible. Only one picker may be open per
session; the host refuses a second with `OUT_OF_BOUNDS`. The SDK has no local
command budget to pause. The explicit picker timeout still bounds the plugin
worker's wait for its host reply.

A command handler may open an asset or save-path picker and wait for it; the
SDK permits both calls. The command holds the session's one request slot while
the person browses, delaying reads and other commands. A plugin that also serves
other work should return promptly and open the picker through `after_reply`,
or use `start_request` and move its waiter to an application worker. A background
task can report that worker's progress; starting a task alone does not move work
off the command handler.

`view_state_timeout` selects the caller's wait; default view/volume/ROI helpers
use the message client's 30-second limit. These calls block the calling worker
and must not run in input callbacks.

For a status query while retaining the original request, use a request handle:

```rust,ignore
let mut pick = ctx.start_request(proto::plugin_msg::Msg::RequestAsset(
    proto::RequestAsset { slot: "weights".into(), force_reprompt: false },
))?;
if let Some(reply) = pick.wait_timeout(std::time::Duration::from_secs(5))? {
    // Handle AssetResult; the request is complete.
} else {
    let status = ctx.request_status(pick.id(), std::time::Duration::from_secs(2))?;
    // PENDING is bookkeeping, not a completion guarantee. UNKNOWN does not
    // mean failure. awaiting_user says the host is waiting for a person.
    let reply = pick.wait(std::time::Duration::from_secs(30 * 60))?;
}
```

`wait_timeout` returns `None` without retiring the waiter. `wait` consumes it;
a timeout or dropping the handle abandons local interest, without cancelling
the host's work. Both use the 32-pending-request limit, so reserve
capacity for a status query. There is no automatic polling or heartbeat.

`ctx.panel("panel_id")` supplies `set_value`, `set_enabled`, `set_visible` and
`set_options` methods. Values accept `&str`, `String`, `bool`, `i64`, `f64`,
`Vec<i64>`, or `proto::Value`. Integer lists use `proto::value::Kind::Ints`,
including in command arguments and panel patches, without conversion through
floating point. For example: `ctx.panel("panel").set_value("status", "Ready")?`.
`Widget::progress("bar", 0.0, false)` declares a noninteractive progress bar,
with no command binding. `ctx.panel("panel").set_progress("bar", 0.5, false)?`
updates it; pass `true` for indeterminate work. The host clamps numeric updates
to `[0, 1]`, NaN displays as empty, indeterminate ignores the value, and progress generates no command
echo. Manifest initial values must already be within `[0, 1]`.
Use `ctx.patch` to batch several changes into one patch.

The host retains non-image patches sent before the first panel open, including
list rows, and applies them when it opens. Images require the panel's P arena,
which is granted at first open. Re-sending state in `on_panel_opened` is optional.
Closing a dock hides it without ending the session. The command example keeps
an application-side mirror and sends it on every open; that is also valid.

`Widget::list("results", [Widget::label("name", ""), Widget::image("preview")])`
declares a runtime list with the defaults: at most 256 rows, 8 visible, and no
selection command. Use `Widget::List` to set `max_rows`, `visible_rows`, or
`selectable` with a registered `CommandRef`. Both row counts must be 1–1024.
The nonempty template accepts leaf widgets, including pickers and images;
cell IDs share the panel's namespace. `Manifest::widget_count(panel_id)` returns
the structural capacity (a list counts as 1 + cells × max_rows). The 256-node
manifest limit counts template declarations once. Each list additionally permits
at most 4096 realised cells (`max_rows` × template cells), in JSON and code-side
declarations alike.

```rust,ignore
let panel = ctx.panel("panel");
panel.set_rows("results", ["result-a", "result-b"])?;
panel.row("result-a")?.set_value("name", "First result")?;
panel.row("result-b")?.set_value("name", "Second result")?;
// After the panel's image arena has been granted:
panel.row("result-a")?.push_image("preview", &png_bytes)?;
```

`set_rows` replaces the whole ordered key list, rejecting duplicate/empty keys,
keys longer than 128 Unicode characters, or more than the declared `max_rows`.
It never truncates. Removed keys lose their state; re-adding one starts from
template defaults. The host owns that state. A row handle supports all ordinary
panel operations; `Context::push_image_row` is also available. Raw `Context::patch`
callers set `PatchOp.row_key` (empty for ordinary widgets); raw `set_rows` is
also checked against the declared list's `max_rows` before queueing.
Cell commands receive `args["row"]` as `proto::value::Kind::S` alongside the
usual value. A selectable list's command receives only the row key; use
`panel.set_value("results", key)` to select a row, or `""` to clear selection.
Rust's outbound FIFO preserves separate rows and operation order, including
row removal/re-addition and dropdown options/value changes. It does not
coalesce by widget ID. Queue saturation remains subject to the limits above.

Declare host-supplied choices with `Widget::ChannelPicker`, `ColorPicker`,
`ColormapPicker` and `RoiPicker`. Each binds a `CommandRef`. A channel picker
uses `ChannelSelection::Single(index)` or, with `multiple: true`,
`ChannelSelection::Multiple(indices)` as its optional initial value. Its command
receives `Value.i` or `Value.ints` respectively, with **0-based indices**. The host
labels choices with the file's channel name or `Channel N` using a **1-based** N.
The other three take optional
string defaults: `#RRGGBB`, a colormap name, or a region ID. The ROI picker also
sends the region's name in command arguments; by default it does not change host
selection. Set `follows_selection` to use the host's linked selection control.
Only channel pickers accept `multiple`. All defaults use the shared manifest
validator, including declared-command and color-format checks.

`ctx.get_colormap(name)` returns a `proto::ColormapTable`, checking the echoed
name and exactly 1024 RGBA bytes (256 entries); unknown names return NOT_FOUND.
`ctx.request_save_path(suggested_name, extension, title, timeout)` returns a
`proto::SavePathResult`. The suggested name is a filename, not a path; the host
validates it. Cancellation is `declined: true` with an empty path, not an error.
The host creates no file. Both helpers need no permission or open dataset.
Save-path and asset requests share the host's one-picker-at-a-time lifecycle
with the plugin call's timeout as a dead-session backstop. Command handlers
have no local time budget. Generic `start_request` also supports these requests
for asynchronous polling and abandonment. A command handler may wait for either
picker, but plugins serving other work should open it off the command path
through `after_reply` or an application worker and return promptly.

### Tool engagement callbacks

`Registry::on_activated` and `on_deactivated` (also methods on `Plugin`) receive
the accepted `ToolBinding`: the binding is available at the moment the tool is
engaged. Read its assigned `triggers` and action keys to describe the honoured
controls immediately, rather than echoing the manifest's requests. The
withdrawal callback receives the binding that was removed.
Each engagement/withdrawal fires once, after its
acknowledgement is queued. Deactivation follows the open gesture's cancellation
on the same event worker. Equal replays, live binding updates, and refused
bindings do not fire an extra engagement callback. Hooks should stay short,
like input handlers; long work belongs on an application worker.

### Set-view

Set-view lets a plugin answer a person's interaction with a navigation request.
Declare `PanelSpec::view_targets` or `ToolSpec::view_targets` using `ViewTarget`
(`T`, `Z`, `SelectRegion`, `FrameRegion`, `LookAt`, `OrbitPoint`). Set
`moves_view: true` on the interactive `Widget` variant that should supply a
cause. Lists must be selectable; images must bind a command. The host draws
the move-the-view switch as the panel's first row, off every time the panel
opens. It governs the panel's widgets and the plugin's tools, and counts only
while the panel is open and visible. It has no widget ID and is never reported
on the wire; `NOT_ENABLED` is all the plugin learns. A tool with view targets
requires at least one panel with view targets (the two target lists need not
match). The host judges every request.

```rust,ignore
use vizi_doorbell::control::ViewMove;
use vizi_doorbell::declare::{CommandSpec, PanelSpec, ViewTarget, Widget};

let jump = builder.command(CommandSpec::new("jump", "Jump").hidden(), |ctx, command| {
    // InvokeCommand.cause is the host's token; ordinary commands carry zero.
    let navigation = ctx.command_view(command)?;
    let result = navigation.move_view(ViewMove { t: Some(5), ..Default::default() })?;
    // A refusal is a result, not a transport error. Report its detail to the user.
    Ok(vizi_doorbell::proto::CommandDone {
        ok: true,
        message: result.detail,
        ..Default::default()
    })
});
let mut panel = PanelSpec::new("navigation", "Navigation", Widget::Button {
    id: "jump".into(), text: "Jump to frame 5".into(), command: jump,
    moves_view: true,
});
panel.view_targets = vec![ViewTarget::T];
builder.panel(panel);
```

Answer **every cause** from a moving widget, preferably within one second;
causes become stale after two seconds. Call `navigation.decline_view()` when
there is no move to make (for example a click in an empty part of a plot).
This is the handler's responsibility; the SDK does not invent a cause or
automatically decline at drop. A clone retains the same cause. An identical
retry gets the host's original result; a different request under a spent cause
is refused as `CONSUMED`. Do not retry with a made-up fresh cause.

`ctx.input_view(&input)` captures a FIRE's tool/interaction pair and refuses
PRESS, DRAG, RELEASE and CANCEL locally. In an input callback use
`start_move(ViewMove { ... })` or `start_decline()` and transfer the returned
`ViewRequestHandle` to an application worker. Blocking there would stop the
input pump. Command workers may use `move_view` / `decline_view` directly.
`ViewRequestHandle::wait_timeout` allows finite polling; `None` keeps the
waiter, while `wait` consumes it. Dropping or timing out abandons local interest,
not the host's request, so a timeout means the outcome is unknown.
The lower-level `Context::start_view_request` / `request_view` accepts the
wire request and checks exactly one cause, and targets only on moves.

`ViewMove` also accepts `z`, `select_region`, `frame_region`, `proto::LookAt`
and `proto::OrbitPoint`. Coordinates are voxel-centred level-0 z,y,x. Requests
apply wholly or not at all. The result echoes the cause and carries
`ViewOutcome`, `ViewRefusal` and `detail`; `result.applied()` tests APPLIED.
Only that reply acknowledges the request, never a later `ViewChanged`.
The host controls declarations, the person's toggle, supersession, freshness,
dataset changes and export ownership. Vizi implements every
target, including `look_at` and `orbit_point`, and accepts tool FIRE causes for
judgment. `UNSUPPORTED` remains an ordinary refusal from a host that cannot
provide a requested capability. While a Follow owns a target, a ViewRequest
touching it is refused FOLLOW_ACTIVE.

### Follow

A Follow is timed movement started by the person. Set a panel's
`PanelSpec::follow_targets` using `ViewTarget`, then register a driver through
`PluginBuilder::follow` or `Registry::follow`. The host's panel switch also
governs Follow. Offering does not start movement; offers survive an unopened
panel, and the person presses Follow or Resume.

```rust,no_run
use std::time::Duration;
use vizi_doorbell::{control::ViewMove, declare::{PluginBuilder, PanelSpec, ViewTarget, Widget}};
let mut builder = PluginBuilder::new("demo.tour", "Tour", "1.0.0");
let mut panel = PanelSpec::new("tour", "Tour", Widget::label("status", "Ready"));
panel.follow_targets = vec![ViewTarget::FrameRegion];
builder.panel(panel);
builder.on_panel_opened(|ctx, _| ctx.panel("tour").offer_follow("example regions"));
builder.follow("tour", |ctx, follow| {
    // Replace these IDs with a snapshot of regions supplied by the host.
    for id in ["region-a", "region-b"] {
        if !follow.move_view(ViewMove { frame_region: Some(id.into()), ..Default::default() })? {
            break;
        }
        if !follow.wait(Duration::from_millis(500)) { break; }
    }
    if let Some(end) = follow.ended() {
        ctx.panel("tour").set_value("status", end.detail)?;
    }
    Ok(()) // the SDK ends a still-active Follow, including on Err or panic
});
```

Each driver has its own worker; commands, subscription callbacks and incoming
host replies keep running. `Follow::move_view` takes a **whole state**, refuses
empty or undeclared targets locally, and publishes a rising sequence number.
`Ok(true)` means queued, not applied; `Ok(false)` means the Follow has ended.
The host applies the latest state at up to 30 Hz and may end an invalid state
with `INVALID` and a detail. Pending states coalesce by follow ID; offers
coalesce by panel. `EndFollow` remains one-shot and follows the final queued
state. Panel patches keep their FIFO policy.

`Follow::wait(Duration)` returns false promptly when the host ends it.
`active()`, `ended()` (reason and detail), `id()`, `panel_id()`, `targets()` and
`resumed()` expose its lifecycle. Store your own resume cursor: a resumed
Follow has a fresh ID. `end()` is idempotent locally; returning, erroring or
panicking also ends a still-active Follow. A panel without a driver is ended
immediately. Host ends are never echoed; shutdown wakes workers without a wire
end or an invented host reason. Long application computations must check
`active()` themselves; the SDK does not forcibly terminate threads.

Use `ctx.panel(id).withdraw_follow()` to remove an offer. All ten host end
reasons are available in `proto::FollowEndReason`; unknown future numeric
reasons remain available through `ended()`. No cause is needed or invented for
Follow movement. See `examples/region_quant/rust` for a resumable tour.

Clickable `Widget::Image` takes `command: Some(command_ref)` and `moves_view`.
Its command arguments are `x` and `y` in image pixels, `button`, `modifiers`,
and `image` (the displayed ImageRef offset), plus `row` for a list cell.
`Widget::RoiPicker` additionally takes `follows_selection` and `RoiFilter::All`
or `RoiFilter::Mine`. Following selection is the host's own control and needs
no view cause; selecting there also selects the region in vizi.
Widget constructors keep navigation off. Code using enum literals
must supply `moves_view: false` unless opting in; image literals also supply
`command: None`, and ROI pickers default to `follows_selection: false` and
`filter: RoiFilter::All`. Panel/tool constructors default to no view targets.
