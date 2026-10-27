# Concepts

> Protocol `1.0`. Where this page and [`spec/`](spec/) differ, the
> specification is right; the section headings below name the document to
> read for the rule.

## The process model

vizi is closed-source; plugins are open-ended third-party code. The boundary
that satisfies both: **your plugin is a separate OS process**. It never
imports vizi's code, and vizi never imports yours. Everything crosses a
versioned protocol. Plugins run with the installing user's filesystem,
network, and device access on every platform; the process boundary provides
crash isolation, not security containment.

`serve()` is the whole plugin-side runtime: it binds the endpoints the host
passed through environment variables, opens the doorbell, completes the
handshake, and dispatches host requests to whatever you registered with the
decorators. It returns when the session ends.

## The three channels

You don't touch any of these directly — the SDK does — but knowing they exist
explains the behavior you observe, and the error strings you will read.

- **Control plane** (`spec/control_plane_protocol.md`) — one gRPC
  bidirectional stream over a local transport; **your process is the server
  and the host connects to it**. Requests and replies — open-this-file,
  read-volume-(t,c), invoke-command — plus your pushes (panel patches,
  overlay updates) and your own requests to the host (`ctx.host`), all in
  order on one stream. A request and its reply pair by id, and the id of a
  request in flight belongs to its reply alone: each request takes one reply
  kind (an `OpenReader` takes `ReaderOpened` or `SeriesList`) or `Error`, and
  any other known kind under that id is a protocol error that ends the
  session. The stream's lifetime is the session: when it closes, every
  reader handle, panel, overlay and grant opened on it is gone.
- **Doorbell** (`spec/doorbell_protocol.md`) — a local socket carrying
  fixed-size 112-byte records with three opcodes: `M` grants a memory
  segment (it is how a file descriptor travels), `E` delivers tool input at
  interaction rate, `D` is a hint that a live value was published. It is
  fail-stop: a malformed record is a protocol-integrity failure, and **a
  doorbell that closes ends its session** — politely: the host sends
  `DoorbellShutdown`, waits out a 2 s window, then sends
  `Shutdown{session_fault}` naming the failure. Your plugin reports its own
  fault and keeps its stream open until that `Shutdown` arrives; it never
  ends the session itself (`spec/control_plane_protocol.md` §8.13).
- **Data plane** (`spec/shared_memory_protocol.md`) — host-granted
  shared-memory segments. Bulk bytes — volumes, PNG panel images, overlay
  geometry — cross through memory, **never** through gRPC. A reply carries a
  *descriptor* (which segment, what offset, what shape) and the other end maps
  the bytes. This is why a 100 MB volume read costs a memcpy, not a
  serialization.

### Segments, purposes and loans

A segment is granted in **two halves**: `M` on the doorbell carries the
capability, `InstallSegment` on the control plane says what the segment's
regions mean. The SDK holds anything that names a segment until both halves
have landed. Every segment has a **purpose**, and the five are the host's
vocabulary (`shared_memory_protocol.md` §3):

| purpose | who writes | serves |
|---|---|---|
| `G` | plugin | a reader's volume deposits |
| `V` | host | volumes the plugin asked for with `read_volume` |
| `O` | plugin | one overlay's geometry — its live lane and its arena |
| `P` | plugin | one panel's images |
| `X` | host | the volumes an exporter is handed |

**`O` and `P` segments are per contribution**: the grant names which overlay
or panel it serves (`subject`), so two overlays are two segments and a push
from a panel or overlay the host granted nothing for raises `NoRegion` rather
than borrowing another's arena.

Bytes a plugin pushes are **loans**. `push_image` and `set_items` reserve from
the contribution's arena, send a descriptor, and the bytes stay lent until the
host sends `ReleaseLoan` — the SDK never reuses them underneath a host that
may still be reading. An arena with no room **fails the push** (`NoRegion`,
with how much was free and wanted) and tells the host by how much it was
short; it never blocks and never overwrites.

The reverse holds for what the host lends you: a `read_volume` result is a
view into a host-produced region, which is why it is read-only and why you
`.copy()` before mutating.

### The live lane

Overlay geometry pushed at gesture rate does not go through the control
plane at all. An `O` segment carries a **Pulsed** lane — a few slots the
plugin publishes whole values into and the host reads at display rate — and
`set_items_live` writes there and rings the doorbell with a `D`. The hint is
advisory: the host scans lanes on a schedule regardless, so a dropped hint
costs a frame, not a value. A value carries the pane and epoch it was
computed under, and the host draws it only while that is still the pane's
current epoch. [Tools](contributions/tools.md) is where this matters.

## Which thread runs what

The SDK runs several threads and each handler kind has a home. Getting this
right is most of what separates a responsive plugin from a wedged one:

| Handler | Runs on | May block on `ctx.host`? |
|---|---|---|
| `@command`, `@panel_opened` | the command worker (one, serialized) | yes — but return within the host's **10 s** budget |
| `@tool` handlers | the doorbell listener, at event rate | **no** — hand the work to your own thread |
| reader `open` / `read_volume` / `read_plane` | the reader worker | n/a (the host is waiting on you) |
| exporter `begin` / `write` / `finish` / `abort` | the export worker, in stream order | n/a |
| `@on_shutdown` | a dedicated worker, inside the shutdown window | no new work at all |

`PanelHandle`, `OverlayHandle` and `ctx.host` are thread-safe; a background
thread you start from a command handler can push panel patches and read
volumes freely. A tool handler must not block: it runs on the thread that
receives grants and input, so a host request there stalls both.

## Coordinates and dtypes

- Dataset shape is **TCZYX** (time, channel, Z, Y, X) — the order
  `ReaderInfo.shape_tczyx` declares.
- Every position or volume on the wire is **ZYX** (numpy-native), and
  overlay/tool positions are **float64 voxel coordinates**. The host owns
  any flip to its internal axis conventions — plugins never think in XYZ.
- Integer voxel indices name corners; a voxel's center is index + 0.5.
  `vizi_plugin_sdk.geometry` has `EdgeBox`, which converts a crop exactly
  across pyramid levels where an index box cannot, and `level_scale`.
- Volume dtypes: `u8`, `u16`, `f32`. The array your reader returns must
  match the dtype it declared in `open()`.

## Asking the host for data and UI

A plugin can make requests of the host through `ctx.host` — from a command
handler directly, from a tool via a worker thread:

```python
view = ctx.host.view_state()
volume = ctx.host.read_volume(view.t, view.visible_channels[0], view.level)
```

**`view.c` is deprecated and always 0.** vizi has no active channel and no
C picker, so the field names a control that does not exist. `view.visible_channels` is
the set enabled for display (ascending; one entry under a solo), host-wide
rather than per pane — the message carries no view id, and the 3D pane
composites at most eight of them. A plugin that wants one channel takes the
first, which is at least on screen. A tool has the same set, exact to its
epoch, on its `E` records — a panel, reader or exporter reads it here,
because the host arranges `EpochChange` only for sessions that declare a
tool.

The manifest must declare the `data_access` permission for reads, and
`contribute_rois` for contributing native ROIs or compiling label volumes.
Reads are explicit in time, channel, level, and optional ZYX ROI; the
[commands page](contributions/commands.md#reading-the-active-dataset) has the
complete contract and error behavior. Every request is answered — failures
raise `HostError`, never hang — and the 30 s timeouts are dead-session
backstops, not a budget.

A plugin can also ask the host to show a native file picker for a
**user-specific** file whose digest cannot be known when the manifest is
written (`ctx.host.request_asset(slot)`), and can activate one of its own
declared tools (`ctx.host.activate_tool(tool_id)`).

## Where a plugin keeps what it remembers

Two folders, both conventions rather than confinement — a plugin can still
write anywhere (`spec/control_plane_protocol.md` §2.3):

- **Settings** — anything not about one file — go in `settings_dir()`, the
  plugin's own `settings/` folder beside its manifest. The host **names** it
  and checks that it could be made there; it does not create it.
  `settings_dir()` creates it on first use, so a plugin that keeps no
  settings leaves no empty folder in its source tree. It lives as long as
  the plugin is installed, and deleting the plugin's folder removes it with
  everything else. Plugin Manager's *Settings folder* button opens it. When
  the plugin's folder is not writable, the host uses a fallback folder under
  its own data directory instead — that one it does create, with a
  `plugin-id.txt` naming the plugin so a leftover can be traced — and
  deleting the plugin does NOT remove it. It is a module-level function, so
  a reader or an exporter can call it too. It raises
  `SettingsDirUnavailableError` when the host named no folder, or when the
  one it named cannot be created; do not invent one.
- **State about one dataset** — a cache, a result, a per-file choice — goes
  in that file's folder: `ctx.host.view_state().file_state_dir` for the open
  dataset, a reader's `PluginContext.file_state_dir` for the file it is
  opening, and `ExportDescriptor.file_state_dir` for an export's source. It
  sits in the file's companion directory, beside the host's region saves, so
  it follows the file where those do. Where the host keeps companions pooled
  or under a custom root instead, the folder is keyed by the file's path, and
  moving or deleting the file does not move or delete it. Plugin Manager does
  not point at it. **The host names it and never
  creates it**: create it when you first write, so that opening a file leaves
  nothing behind for a plugin that stores nothing. It is `None` when the
  dataset has no local path (a remote or plugin URI, a demo dataset). When
  it is set it is an absolute path, even for a dataset opened by a relative
  one.

## Assets, environments, and accelerators

Author-known immutable model/data files are manifest `assets`: declare their
exact size and SHA-256, let the user import or download them in Plugin
Manager, then resolve them with `asset_path(id)`. Use
`ctx.host.request_asset()` instead when the file is user-specific.

Plugins inherit the user's environment except for loader/interpreter-path
variables that could make the plugin accidentally load vizi's bundled native
libraries. They also inherit ordinary OS accelerator visibility. There is
no `gpu` permission: the string is accepted with a warning and grants
nothing. Framework, runtime and driver compatibility is the plugin's
responsibility.

`data_access` and `contribute_rois` are declared-equals-granted and visible to
the user. There is not yet a per-plugin consent prompt. Unknown permission
strings are accepted with a warning and grant nothing.

## Lifecycle

- **Scan** — at startup and on Plugin-Manager Rescan, the host reads
  manifests as data. Nothing launches. Invalid/incompatible manifests are
  skipped with a log line.
- **Lazy launch** — the process starts on first use (a matching file is
  opened, a command is invoked, a panel is opened, an export is started). One
  process serves all of a plugin's contributions, and concurrent uses share it
  (leases); it stops when the last lease is released or the plugin is
  disabled.
- **Handshake** — the host's first message is `Hello`, the plugin's is
  `Ready`; each carries an identity string the host logs so a session names
  the two builds that spoke. The version match is on the major.
  **A launch has one deadline from spawn to `Ready`: 60 seconds in vizi**, or
  the manifest's `entry.startup_s` held to 10–300 s, and the person can
  cancel it. Expiry fails the launch. A plugin whose module imports a deep
  numerical or GPU stack at the top spends that budget before it ever speaks:
  do that work on first use, not at import, or declare `entry.startup_s`. A launch that
  dies here shows as `stage: startup` in the diagnostics
  ([debugging](debugging.md)).
- **Health** — the Plugin Manager shows per-plugin status: *stopped*,
  *running*, *not responding*, or a retained crash/failure summary. A hung
  plugin is detached after a timeout rather than freezing the viewer. The
  Manager offers restart and retained diagnostics; disabling a running plugin
  stops it immediately.
- **Shutdown** — on every voluntary teardown the host sends `Shutdown` with
  a `reason` and a deadline, then waits for the plugin to close the stream.
  An `@on_shutdown` hook runs inside that window (`ShutdownNotice.reason`,
  `.grace_seconds`, `.starting_again`); it may flush and persist, it may not
  start new work, and the host tears down at its deadline regardless. A
  command in flight runs to its own deadline or the window's, whichever is
  sooner. A plugin registering no hook closes immediately.
  When `Shutdown` arrives, or the session ends any other way, every host
  request the plugin still has pending fails at once with `HostError`, before
  the hook runs, and a request made afterwards fails without being sent; a
  handler running in the shutdown window must expect that error and stop
  quietly rather than report it.
  **The window is short — 5 seconds when the host names no deadline of its
  own** — and nothing in it interrupts a thread. A background thread that is
  computing rather than talking to the host will not notice the window at all:
  a host request inside it fails at once, which is how a thread that reads or
  writes finds out, but a pure computation runs until the process goes. Give
  any long-running thread a flag it checks at its own boundaries, set that
  flag from the shutdown hook, and it can stop at a point of its choosing
  instead of being cut off mid-write.
- **Enable/disable** — persisted per plugin in the host's settings; a
  disabled plugin's contributions don't register at the next scan.

## Version compatibility

**`engines` is the floor, `Ready` the negotiated ceiling.** Two checks run
automatically:

- **Manifest gate**: `engines.vizi` (`major.minor`) is the oldest host the
  plugin runs on. The scanner rejects a different major, and also a NEWER
  minor than the host speaks. A rejected plugin never runs, and Plugin
  Manager lists it as *not loadable* with the reason.
- **Launch handshake**: the host's `Hello` says which minor it speaks, and the
  SDK answers with a `Ready` at the lower of that minor and its own. The
  session runs at the `Ready`'s minor. A `1.1` host serves a `1.0` plugin; a
  `2.0` host serves neither. A `Ready` above the host's minor is refused (*this
  plugin speaks protocol 1.3; this vizi speaks 1.1*), and so is a `Ready` below
  the plugin's own `engines.vizi`, which is a plugin whose manifest asks for
  more than its SDK speaks (*this plugin's manifest requires protocol 1.2 but
  its code speaks 1.0*).

Within a major the wire is append-only: new fields take new numbers and an
older peer skips what it does not know. A minor bump therefore never breaks a
plugin — a minor that changed behavior would be a defect in the
specification.

**The pattern.** Minors only add, so compatibility is per feature:

1. Set `engines.vizi` to the oldest host you tested on, not to the newest
   minor your SDK speaks.
2. Let the SDK set `Ready`; it never claims more than the host speaks.
3. For anything newer than your `engines.vizi`, ask the session with
   `supports("feature")` and fall back when it says no. The answer comes from
   the negotiated minor, so it is the same on every host that runs the plugin.
   Calling a guard (`require`) instead fails at the call site, naming the
   feature, the minor it needs and the session's.

```python
@command("show-best")
def show_best(ctx):
    best = find_best_frame(ctx)
    if ctx.host.supports("set_view"):
        return ctx.move_view(t=best).outcome
    return f"best frame: t={best}"   # an older host: say it instead
```

The feature names are the same in every SDK. At protocol `1.0` they are
`view_subscription`, `regions_subscription`, `raw_read`, `processing_stages`,
`region_shapers`, `set_view` and `follow`, and every `1.0` session has them
all; a later minor adds names to the table, each with the minor that
introduced it.

**What 1.0 is, and how it moves.** Protocol `1.0` is the contract published
with this SDK's 1.0 release. Every addition to the wire after it is a new
minor (`1.1`, `1.2`, …), and the vizi release that first speaks a minor says
so. Compatibility runs forward: a plugin built against `1.0` keeps working on
every later `1.x` host. Nothing is removed within `1.x`. Something deprecated
in a later minor is marked as such in the specification and the IDL, and every
`1.x` host still honors it; removal waits for `2.0`. No promise is made
across majors yet. The wire protocol and the Python SDK carry this 1.0
stability promise; the Go, Rust and Java language APIs may still change in a
1.x minor release. The IDL's 1.0 form is
kept in [`compat/`](../compat/README.md), and a change that would break a
`1.0` plugin fails this repository's CI.

The SDK's own version (`vizi_plugin_sdk.__version__`)
is a different number from the protocol's and is not checked by anyone; it
rides in the identity string so a log can name the build.
