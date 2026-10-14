# Manifest reference — `vizi-plugin.json`

> **This page is a guide.** The rules — every field, every check, and the
> diagnostic each produces — are [`spec/manifest_schema.md`](spec/manifest_schema.md),
> which carries the accepted and rejected fixtures the host's own suite runs.
> Where this page and that document differ, the document is right.

Every plugin directory ships a `vizi-plugin.json` at its root. The host
scans `~/.vizi/plugins/` (or `$VIZI_PLUGINS_DIR`) at startup and on
Plugin-Manager **Rescan**, reading manifests **as data — no plugin code runs
at scan time**. An invalid or incompatible manifest is logged and skipped;
it never breaks discovery of other plugins.

## Top-level fields

| Field | Type | Required | Meaning |
|---|---|---|---|
| `manifest_version` | int | yes | Manifest schema version: `0` or `1`. `1` exists for the tool vocabulary alone — `triggers`, `actions`, `pane`, `mode`, `cancel_on_camera_move`, `wants_pick` on a tool, `pane` on a command — and any of them under `0` is refused rather than dropped ([`spec/manifest_schema.md`](spec/manifest_schema.md)). |
| `id` | string | yes | Globally unique plugin id (reverse-DNS style, e.g. `org.lab.myplugin`). |
| `name` | string | yes | Human-readable name (Plugin Manager, menus). |
| `version` | string | yes | Your plugin's own version. Informational — and the natural content of the handshake's identity string, `Ready; I am <id> <version>; …`, which the host writes to your plugin's log so a session names the build that spoke (`serve(version=...)` overrides it). |
| `engines.vizi` | string | yes | Minimum host **protocol** version, `"major.minor"` — `"1.0"` for this SDK. |
| `entry.command` | list of strings | yes | The launch command. |
| `entry.startup_s` | number | no | How long the plugin expects to take to start, in seconds (advisory; see below). |
| `contributions` | object | no | What the plugin adds — see below. |
| `assets` | list of objects | no | Immutable files provisioned and verified by the host. |
| `permissions` | list of strings | no | Enforced host-capability requests. |
| `subscriptions` | list of strings | no | Host pushes the plugin asks to be sent — `"epochs"` is the one name at `1.0`. |

### `engines.vizi` — compatibility gating

The value is a **minimum**: the host runs the plugin when the protocol
majors match and the host's minor is ≥ the declared minor (the protocol is
append-only within a major). A leading `^`, `~`, or `>=` is tolerated and
immaterial. The current protocol family is **1.0**.

**The gate is the MAJOR.** A major bump breaks and a minor bump does
not, so a `1.1` host serves a plugin declaring `"1.0"` and a `2.0` host serves
neither. That is a promise the version number makes: a minor that changes
behaviour is a defect in the specification, not a licence.

**A plugin declaring another major never runs:** the host rejects its manifest
when it scans the plugins directory, like an invalid one, and Plugin Manager
lists it as *not loadable* with the version it asked for
(`spec/manifest_schema.md` §2.1). See `debugging.md`.

### `entry.command` — the launch command

Executed with the plugin directory as the working directory, so relative
paths (`"ramp_reader.py"`, `".venv/bin/python"`) resolve against it.
`command[0]` may also be a bare name resolved on the host's `PATH`
(`"python3"`) or an absolute path — the absolute path to a venv interpreter
is the most robust form. **What the command starts must be the process that
calls `serve()`**, or reach it without losing what the host handed over. On
unix the doorbell arrives as an *inherited file descriptor*, and a launcher
that spawns the real plugin as a child with the runtime's default
descriptor policy strands it — Java's `ProcessBuilder`, Go's `os/exec` and
CPython's `subprocess` all pass nothing above descriptor 2, and setup then
fails with *"the doorbell fd is not inherited"*. An `exec` that replaces the
process keeps it (`["sh", "-c", "exec .venv/bin/python plugin.py"]` works; a
Python wrapper that `subprocess.run`s does not). The host also captures the
spawned process's stdout and stderr, so a wrapper must forward its child's
streams or a setup failure shows only as *"did not bind within 60s"*. On
Windows the plugin dials in and a wrapper that propagates the environment
is enough. **The host owns the whole tree the command starts, and ends all
of it**: on unix the command runs as the leader of a new session and
teardown signals its process group (SIGTERM, the shutdown window, then
SIGKILL for what remains); on Windows the process is placed in a job object
its descendants inherit, terminated at teardown and by the kernel if the
host dies first. So a launcher in front of the plugin is fine for process
lifetime — the real plugin goes down with it — and a daemonizer that
detaches it into a session of its own is not: that process has left the
tree and nothing will come for it. A Ctrl+C in the terminal that launched
vizi reaches the host alone, which shuts its plugins down in order.
[`spec/manifest_schema.md`](spec/manifest_schema.md) §2.2 carries the rule
in both directions.

### `entry.startup_s` — a slow start

A launch has one deadline from spawn to `Ready`, and it is the host's policy:
vizi gives **60 s**, and the person can cancel a start at any time. A plugin
that needs longer to start — a cold JVM, a first-run dependency resolution,
a heavy import — says so with `entry.startup_s`, a positive number of
seconds (`plugin(..., startup_s=120)` in Python). It is **advisory**: vizi
honours it within 10 s to 300 s, another host may cap or ignore it, and
nothing in the plugin may depend on it being honoured. A plugin past its
budget is killed, and the next use starts it afresh
([`spec/manifest_schema.md`](spec/manifest_schema.md) §2.3,
[`spec/control_plane_protocol.md`](spec/control_plane_protocol.md) §9).

### `permissions` — declared host capabilities

The host enforces two permissions ([`spec/manifest_schema.md`](spec/manifest_schema.md) is the authority on what it accepts):

| Value | Gates |
|---|---|
| `data_access` | active-dataset reads (`view_state`, `read_volume`), ROI read-back (`get_rois`, `read_mask_voxels`, `read_roi_inclusion`), and runtime asset requests (`request_asset`, `request_asset_async`). |
| `contribute_rois` | native ROI contribution (`contribute_rois`, `extend_roi_poses`) and label-volume compilation. |

Permissions are visible in Plugin Manager and currently
**declared-equals-granted**; there is no per-plugin consent prompt yet. An
unknown string is accepted with a warning and grants nothing — append-only, so
a manifest written for a newer host still loads on this one.

The retired `gpu` permission is accepted with a warning for compatibility
but grants nothing; do not declare it in new manifests. Plugins run
unconfined under the installing user's trust and inherit the user's
filesystem, network, and device access on every platform.

### `subscriptions` — host pushes the plugin asks for

A list of names; **`"epochs"` is the only one at `1.0`**. An unknown name is
warned about and ignored, as an unknown permission is, so a manifest written
for a newer host still loads ([`spec/manifest_schema.md`](spec/manifest_schema.md) §5.1).
A subscription is not a permission: a permission says what the plugin may
*do*, a subscription what it wants *sent*.

```json
"subscriptions": ["epochs"]
```

**`"epochs"` puts a plugin that declares no tool on the epoch chain.** A
plugin with a tool is on it anyway, declared or not, because its input
events cannot be interpreted without an epoch. What the host then sends is
`EpochChange` (`spec/control_plane_protocol.md` §8.10), whenever a pane's
*interpretation* changes:

- per pane: `view_id`, `epoch` (never 0, increasing per pane), the dataset
  open generation `gen`, and `channels_high` — the visible channels at index
  ≥ 15;
- per dataset generation: the whole `Geometry` (channels, extent, the crop as
  an `EdgeBox`, voxel spacing with its unit and provenance, the per-level
  scales) and `time_mapping_revision`, which names the index→time table sent
  separately as `TimeMapping` (§8.11).

**It carries no navigation state.** No field of `EpochChange`, `DatasetState`,
`Geometry` or `PaneEpoch` holds the current timepoint, slice or level, so the
subscription tells a plugin when a pane's geometry, crop, spacing, levels,
time table or high channels change — not where the time slider is.
`ctx.host.view_state()` remains the way to read `t` (polled, and possibly a
tick stale).

**Subscribing takes on an obligation.** Each `EpochChange` is an arrangement
the plugin must acknowledge with `EpochAck`, under a 10-second deadline; a
plugin that does not is re-sent the epoch and then escalated where a person
sees it. That is why this is opt-in rather than implied by `data_access`. The
Python SDK installs and acknowledges every epoch itself, so a `serve()`-based
plugin meets the obligation without code of its own, and it can read what
it acknowledged: `ctx.pane(view_id)` answers the installed state for one pane
(geometry, spacing, levels, crop, the high channels, the time mapping) and
`ctx.panes()` the view ids it holds, on a command's, panel handler's, reader's
or exporter's context as on a [tool's](contributions/tools.md). Without a
tool or the subscription there is nothing installed, and `pane` answers
`None`. Neither carries `t` — the epoch never does — so the frame on screen
still comes from `ctx.host.view_state()`.

### `assets` — immutable model and data files

Assets are top-level declarations, not contributions. They let an author
publish reproducible model/data requirements whose exact size and digest the
host verifies before exposing them to the plugin.

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Stable id: alphanumeric first, then letters/digits/`.`/`_`/`-`. |
| `name` | no | Plugin Manager label; defaults to `id`. |
| `filename` | yes | One plain filename, with no path separators. |
| `size` | yes | Exact positive byte count. |
| `sha256` | yes | Exact 64-digit hexadecimal SHA-256. |
| `source.kind` | yes | `"manual"` or `"https"`. |
| `source.url` | for HTTPS | Credential-free HTTPS URL. |

Example:

```json
"assets": [
  {
    "id": "cell-model",
    "name": "Cell segmentation weights",
    "filename": "weights.bin",
    "size": 184320000,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "source": {
      "kind": "https",
      "url": "https://models.example.org/weights.bin"
    }
  }
]
```

The user explicitly chooses **Download** or **Import…** in Plugin Manager.
The host accepts a file only when size and SHA-256 match, publishes it
atomically as a read-only copy in a per-plugin store, and passes its path to
the plugin. Resolve it at runtime with:

```python
from vizi_plugin_sdk import AssetUnavailableError, asset_path

try:
    weights = asset_path("cell-model")
except AssetUnavailableError:
    ...  # tell the user to provision it in Plugin Manager
```

`asset_path` raises if the id was not declared or the verified file is absent.
Asset declarations cover immutable payloads, not mutable plugin state.

For a user-specific file whose size and digest cannot be known when the
manifest is authored, use `ctx.host.request_asset(slot, force_reprompt=False)`
instead. It requires `data_access`, imports one user-selected file into a
persistent named slot, and returns its copied path. Runtime slots are not
listed under `assets`.

## Contributions

All contribution kinds live under `"contributions"`: `readers`, `commands`,
`panels`, `exporters`, `overlays`, `tools` and `stages`. Ids must be unique
within their kind, not across kinds. A kind the host does not know is
ignored, as every unknown field is, so declaring a newer kind (such as
`stages`) needs no `manifest_version` change
([`spec/manifest_schema.md`](spec/manifest_schema.md) §1, §3.7).

### `readers`

"I open files matching these extensions." Lazily launched when the user
opens a matching path.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Addresses the reader within the plugin. |
| `name` | string | no | Defaults to `id`. |
| `extensions` | list of strings | yes | Non-empty. Normalized to lowercase with a leading dot; matched case-insensitively against the file suffix. |

### `commands`

An addressable behavior the plugin runs — fired from the Tools menu or from
panel widgets. See [commands](contributions/commands.md).

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique. Your `@command(id)` handler must match. |
| `title` | string | yes | Menu text. |
| `description` | string | no | |
| `default_keybinding` | string | no | A **request**: the plugin **loses** any conflict with a core binding. |
| `menu` | `"tools"` or `null` | no | Default `"tools"`. `null` registers the command without a menu entry — for commands that only panel widgets fire. |

### `panels`

A declarative side panel the **host** builds as real widgets — "plugins
propose, host disposes". See [panels](contributions/panels.md).

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique. |
| `title` | string | yes | Dock title; also the Tools-menu open entry. |
| `menu_label` | string | no | The Tools-menu entry verbatim (non-blank, ≤ 128 chars); default `Show <title>`. |
| `root` | widget object | yes | The widget tree (below). |

Each widget object: `kind` (one of `label`, `button`, `slider`, `dropdown`,
`checkbox`, `line_edit`, `image`, `progress`, `row`, `column`), a unique
`id`, and per-kind fields:

| Kind | Fields |
|---|---|
| `label` | `text` |
| `button` | `text`, `command` (required) |
| `slider` | `command` (required), `min` < `max`, `step` > 0, `value`, `float` |
| `dropdown` | `command` (required), `options` (non-empty), `value` (must be in options) |
| `checkbox` | `command` (required), `text`, `value` |
| `line_edit` | `command` (required), `placeholder`, `value` |
| `image` | (target for `push_image`) |
| `progress` | `value` (default 0, within `[0, 1]`), `indeterminate` (default `false`) — **not** interactive; binds no command (target for `set_progress`) |
| `row` / `column` | `children` (non-empty list of widgets) |

Interactive kinds **must** bind a declared command id; `label`, `image` and
`progress` bind none. Limits: tree depth ≤ 8, ≤ 256 widgets per panel.
**Unknown widget kinds are rejected** at parse time — one of the few places
the host does not ignore what it does not know, because a panel with holes
in it would have no diagnostic — so a too-new panel fails loudly on an older
host.

### `exporters`

"I write artifact format Y" — an entry in the Export Data dialog's format
selector. See [exporters](contributions/exporters.md).

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique. Your `@exporter(id)` class must match. |
| `name` | string | no | Defaults to `id`. Shown in the format selector. |
| `extension` | string | yes | Drives the save-dialog filename filter. |

### `overlays`

"I draw into the views." Declaring the id is what authorizes pushes — an
update naming an undeclared overlay id is rejected. See
[overlays](contributions/overlays.md).

**A plugin that declares an overlay must declare something that starts it**
(`spec/manifest_schema.md` §3.5). An overlay is drawn only while its plugin
runs, and the host starts a plugin only for something the person does. What
counts is what the host always offers: a panel; a command or a tool that is in
the Tools menu or has a `pane` button; a reader, an exporter or a stage. A
`default_keybinding` or a tool's `triggers` do not count on their own, since
another binding can take them. A manifest with overlays and none of these is
refused. A panel is the usual answer: it is where the person finds the
overlay's controls.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique. |
| `name` | string | no | Defaults to `id` (Plugin Manager attribution). |
| `segment_mib` | int | no | Overlay shared-segment size, 1–512, default 32. The default suits ~10⁵ float64 points. **Refused on a chunked overlay**, whose arena the host sizes from the dataset. |
| `chunked` | bool | no | Default `false`. `true` makes this a [chunked overlay](contributions/overlays.md#chunked-overlays--a-movies-segmentation-one-volume-at-a-time): one label volume per `(level, t)` the host asks for, served through `@chunk_source`; the host sizes and re-grants its arena; it takes `OverlayChunk` and `OverlayLabelTable` only and never compiles (`spec/control_plane_protocol.md` §5.7). |
| `time` | string | no | Only with `chunked`: `"per_t"` (default) — one volume per level per timepoint — or `"static"` — one volume per level for every timepoint, wanted without a `t`. |

### `tools`

A live interaction mode: a **checkable** Tools-menu entry; while active the
tool owns left-mouse in its declared views and receives press/drag/release
events. See [tools](contributions/tools.md).

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique. Your `@tool(id)` class must match. |
| `name` | string | no | Defaults to `id`. Menu text. |
| `views` | list | no | Subset of `["2d", "3d"]`; default both. |
| `menu` | `"tools"` or `null` | no | As for commands. `null` omits the checkable entry; activate from a panel command with `ctx.host.activate_tool(id)`. |
| `mode` | `"gesture"` (default) or `"fire"` | no | press / drag / release, or one `FIRE` per click. **v1.** |
| `cancel_on_camera_move` | boolean | no | default `true`: a camera move ends the open gesture with a `cancel`. **v1.** |
| `wants_pick` | boolean | no | default `true`: the host computes its surface pick for this tool's 3D presses. `false` asks it not to, for a tool that resolves its own anchor from the ray. A request about what the host computes, not a guarantee about what arrives: handle a `Ray` and a `RayAndPicked` whichever you declared. **v1.** |
| `triggers` | list | no | What **engages** the tool (below). **v1.** |
| `actions` | list | no | What the engaged tool can be **told**, mid-gesture (below). **v1.** |
| `pane` | object | no | A control-pane button that engages the tool (below). **v1.** |

#### The modal rule — no tool names the primary button

One tool is engaged at a time, host-wide. Once engaged — from the Tools
menu, the control pane, or a trigger it asked for — it owns the **primary
pointer** in its views: the bare left button and every left chord the host
does not keep. **The primary button means whatever the engaged tool means,
so a tool never asks for it.** What a tool asks for is what *engages* it
(`triggers`) and what it can be *told* while engaged (`actions`)
([`spec/manifest_schema.md`](spec/manifest_schema.md) §3.6 is the authority;
`spec/control_plane_protocol.md` §8.10 the rule).

#### `tools[].triggers` — what engages the tool

Each entry names a key **or** a mouse chord, never both:

| Field | Meaning |
|---|---|
| `key` | a portable key name (`"P"`, `"Ctrl+J"`, `"Space"`); it carries its own modifiers |
| `button` | `"right"` or `"middle"`, or `"left"` **with modifiers** — never bare left |
| `modifiers` | list from `"shift"`, `"ctrl"`, `"meta"` (a single string is accepted); `"alt"` is refused on every mouse trigger |
| `hold` | `"toggle"` (keys' default: press once to engage, again to disengage) or `"momentary"` (engaged while held; the previously engaged tool returns on release — told `deactivated`, then `activated`). A mouse trigger is always momentary |

A trigger is a request: the host assigns what it can (`ToolBinding.triggers`
is the assigned subset, and the user is told what was refused) and keeps its
own chords — in 3D `left`/`right` bare, with Shift and with Ctrl; in 2D both
bare and with Ctrl. A refused trigger is not a refused tool: it still
engages from the menu. A momentary engage arriving mid-gesture is dropped.

#### `tools[].actions` — what the engaged tool can be told

| Field | Meaning |
|---|---|
| `id` | non-blank, unique among the tool's actions; what `ctx.action(ev)` returns |
| `name` | defaults to `id` |
| `key` | a portable key name, live **only while the tool is engaged** — so two plugins may share `N`; refused only where a global key would fire at the same press |
| `pane` | a momentary control-pane button that fires the action once |

An action is how a key or a button reaches the engaged tool *without ending
its gesture*: the mouse is the target and the key is the fire — a node placed
on a polygon mid-drag. The host delivers a `FIRE` with `ev.action` naming
the action (1-based, manifest order) and the pointer's current sample; the
tool's `action(self, ctx, ev)` handler receives it (or `fire`, when the
class has no `action`), and `ctx.action(ev)` resolves the id.

#### `pane` — a control-pane button (on a tool, an action, or a command)

| Field | Meaning |
|---|---|
| `icon` | path relative to the manifest directory, `.svg` or `.png`, at most 64 KiB; no `..`, never absolute. One that will not load refuses the manifest |
| `tooltip` | at most 128 characters |
| `hold` | a **tool's** button: `"toggle"` (default, sticky) or `"momentary"` (one gesture). An action's or a command's button is always momentary |

The host builds the pane from these; the contract is what ships now.

### `stages`

"I edit a volume in place, in a chain." A processing stage — see
[stages](contributions/stages.md). vizi drives a chain of one stage (an
applied filter); a host that does not drive stages ignores the kind.

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique among stages. Your `@stage(id)` class must match. |
| `name` | string | no | Defaults to `id`. |
| `params` | list | no | The stage's settings, a flat list (below). |

Each `params` entry ([`spec/manifest_schema.md`](spec/manifest_schema.md) §3.7):

| Field | Required | Rule |
|---|---|---|
| `key` | yes | non-blank, unique within the stage |
| `name` | no | defaults to `key` |
| `type` | yes | `"float"`, `"int"`, `"bool"` or `"choice"` |
| `value` | yes | the default: a finite number for `float`, an integer for `int`, a boolean for `bool`, one of `options` for `choice` |
| `min`, `max` | no | `float` and `int` only, of the same kind; `min ≤ max`, and `value` within them |
| `options` | `choice` only | non-empty list of distinct strings |

A field the entry's `type` does not take is rejected. **Parameters belong to
the position, not the stage**: the host sends a value for every declared key
(and no other) each time it plans or arms the stage at a place in a chain,
so one stage can run twice in a chain with different settings.

```json
"stages": [
  { "id": "stabilize", "name": "Stabilize (rigid)" },
  { "id": "blur", "name": "Gaussian blur",
    "params": [
      { "key": "sigma", "name": "σ (voxels)", "type": "float", "value": 2.0, "min": 0.1, "max": 50 },
      { "key": "edge", "type": "choice", "options": ["reflect", "constant"], "value": "reflect" }
    ] }
]
```

## A maximal skeleton

```json
{
  "manifest_version": 1,
  "id": "org.example.everything",
  "name": "Kitchen Sink",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": [".venv/bin/python", "plugin.py"] },
  "assets": [{
    "id": "model", "name": "Analysis model", "filename": "model.bin",
    "size": 123456,
    "sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
    "source": { "kind": "manual" }
  }],
  "contributions": {
    "readers":   [{ "id": "r", "name": "My format", "extensions": [".myf"] }],
    "commands":  [{ "id": "go", "title": "Run analysis" }],
    "panels":    [{ "id": "p", "title": "Analysis", "root": {
                    "kind": "column", "id": "root", "children": [
                      { "kind": "button", "id": "b", "text": "Run", "command": "go" },
                      { "kind": "progress", "id": "bar" }
                    ] } }],
    "exporters": [{ "id": "x", "name": "My export", "extension": ".out" }],
    "overlays":  [{ "id": "viz", "name": "Results" }],
    "tools":     [{ "id": "pick", "name": "Pick points", "views": ["3d"] }],
    "stages":    [{ "id": "blur", "params": [
                    { "key": "sigma", "type": "float", "value": 2.0, "min": 0.1 } ] }]
  },
  "permissions": ["data_access"]
}
```

A `manifest_version: 1` polygon tool: toggled by `P`, held by `Space`, with
one mid-drag action on `N`:

```json
"tools": [{
  "id": "polygon", "name": "Draw polygon", "views": ["2d"],
  "mode": "gesture", "cancel_on_camera_move": false,
  "triggers": [ { "key": "P" }, { "key": "Space", "hold": "momentary" } ],
  "actions": [ { "id": "add_node", "name": "Add node", "key": "N" } ],
  "pane": { "icon": "icons/polygon.svg", "tooltip": "Draw a polygon" }
}]
```

## Declaring the manifest in code

A Python plugin may skip writing `vizi-plugin.json` by hand and declare its
interface beside its handlers instead. Calling `plugin(...)` makes it a
**declared** plugin: the decorators take the manifest's own field names as
keyword arguments, and `panel()`, `overlay()` and `asset()` declare the
contributions that have no handler.

```python
from vizi_plugin_sdk import CommandContext, command, panel, plugin, serve, widgets

plugin(id="org.lab.demo", name="Demo", version="0.1.0",
       permissions=["data_access"])

@command("demo.recompute", title="Recompute", menu=None)
def recompute(ctx: CommandContext) -> None:
    ...

panel("demo", title="Demo", root=widgets.column(
    "root",
    widgets.slider("threshold", command=recompute, min=0, max=255),
    widgets.progress("bar"),
    widgets.image("plot"),
))

if __name__ == "__main__":
    serve()
```

A widget may bind the `@command` function itself or its id. **Only what you
write is emitted**: a field you leave out stays the host's default, except a
command's `title`, which defaults to its id. `plugin()` also takes
`subscriptions=["epochs"]` and `startup_s=` (`entry.startup_s`, above). `widgets` has one constructor per widget kind;
`pane()`, `trigger()` and `action()` build a tool's or command's buttons,
triggers and actions; `@stage(id, name=..., params=[...])` and
`@chunk_source(overlay_id, time=...)` declare their contributions too.

A declared plugin answers its own command line from `serve()`, then exits
instead of serving:

| Invocation | Does |
|---|---|
| `python plugin.py --manifest[=DIR]` | writes `vizi-plugin.json` (and any pane icons it names) into `DIR`, default the working directory |
| `python plugin.py --install[=DIR]` | writes the package into `DIR/<id>/`, default `$VIZI_PLUGINS_DIR` or `~/.vizi/plugins` |
| `--force` (with `--install`) | allows an interpreter in a cache or temporary environment (`uvx`, `pipx run`), and writing into a directory the installer did not create |

The manifest is built with `manifest_version: 1` and `engines.vizi` set to
this SDK's protocol, and is checked by the same parser the host scans with
before anything is written. **Installing is in place**: `entry.command` names
the interpreter you ran (`sys.executable`, unresolved so a venv keeps its
packages) and your script's absolute path — or `-m <module>` when the plugin
was run as an importable module. Move or delete either and the plugin stops
launching; run `--install` again. `PYTHONPATH` is not something the host will
have, and `--install` warns when it is set. For any other layout (a private
venv, a relocated copy), start from `--manifest` and arrange the directory by
hand.

With no arguments `serve()` serves, as the host launches it, and warns on
stderr if the `vizi-plugin.json` it was launched with no longer matches the
code's declaration. A plugin that never calls `plugin()` keeps its
arguments to itself, except that `--manifest` and `--install` exit with an
error saying the interface must be declared first.
