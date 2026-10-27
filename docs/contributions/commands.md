# Commands

A command is an addressable behavior: the host shows it in the Tools menu
(and panel widgets can bind it); invoking it calls your handler.

## SDK surface

```python
from vizi_plugin_sdk import CommandContext, command, serve

@command("hello.count")              # must match the manifest command id
def count(ctx: CommandContext) -> str | None:
    n = ctx.args.get("value")        # typed args from the invoking widget
    ctx.panel("hello").set_value("status", "Hello!")
    return "Said hello"              # shown to the user; None stays silent

if __name__ == "__main__":
    serve(plugin_id="org.example.hello")
```

- `ctx.args` — a dict of typed values (bool / int / float / str). A slider
  sends its number as `value`; a dropdown sends `index` and `value`; a
  checkbox sends its bool; a line edit its text; a button and a bare menu
  invocation send nothing. **Each invocation carries only that widget's own
  value**, and there is no way to ask the host what another widget holds, so
  a panel with several inputs keeps its own model (seeded from the same
  defaults the manifest declares, because a widget the user never touches
  never invokes anything).
- `ctx.panel(panel_id)` — the [panel handle](panels.md) for pushing state.
- `ctx.overlay(overlay_id)` — the [overlay handle](overlays.md) for pushing
  draw lists.
- `ctx.host` — the [host client](#reading-the-active-dataset) for reads,
  [ROIs](#contributing-and-reading-native-rois),
  [assets](#asking-the-host-to-import-a-user-selected-file) and tool
  activation.
- **The return value is the outcome.** Return a string to report success
  with a message the host shows; return `None` (or `""`) to stay silent;
  raise to report failure (`ok=False` with `str(exc)`). `True` is a silent
  success and `False` a failure, so a handler whose last expression is a
  boolean host call reads correctly; **any `int`, zero included, is a
  silent success too** (`compile_label_volume` and
  `contribute_rois` return counts, so `return ctx.host.compile_label_volume(...)`
  reads correctly); any other type is reported as a
  failure naming the type rather than dropped.
- Handlers run serially on one command worker, not on the session-dispatch
  loop, so a blocking `ctx.host` request can receive its reply. A long
  handler still delays later command invocations for this plugin.

## Long work belongs on a background thread

The host allows **10 seconds for the entire command handler**, and expiry
marks the session suspect, unless the command is **supervised**: shown to the
person, who can see how long it has run and cancel it. A supervised command
has no deadline. vizi shows every command in its activity list, so in the
application a command is supervised; with no window to show it in (a script,
the emulator) the 10 seconds stand (`spec/control_plane_protocol.md` §8.16,
§9). Report progress with `report_progress(...)` so the person can tell
working from stuck, and stop when canceled. The SDK's host requests default to 30-second
timeouts, but those are dead-session backstops, not a command-time budget.

For a read or computation that might run long, return from the command
handler immediately and continue on a plugin-owned thread. Use one
non-blocking lock so repeat clicks report "busy" instead of starting
overlapping jobs, publish progress through a panel, and catch every worker
exception so the thread cannot die silently:

```python
import threading

from vizi_plugin_sdk import CommandContext, command


_RUN_LOCK = threading.Lock()


@command("analysis.run")
def run(ctx: CommandContext) -> None:
    panel = ctx.panel("analysis")
    if not _RUN_LOCK.acquire(blocking=False):
        panel.set_value("status", "Busy — analysis is already running.")
        raise RuntimeError("analysis is already running")
    host = ctx.host

    def work() -> None:
        try:
            panel.set_value("status", "Reading active volume…")
            view = host.view_state()
            c = view.visible_channels[0]  # the lowest channel on screen; `view.c` is deprecated (always 0)
            volume = host.read_volume(view.t, c, view.level)
            result = analyze(volume)
            panel.set_value("status", f"Done: {result}")
        except Exception as exc:
            panel.set_value("status", f"Analysis failed: {exc}")
        finally:
            _RUN_LOCK.release()

    threading.Thread(target=work, daemon=True, name="analysis-run").start()
```

Panel patches and `ctx.host` are thread-safe. Once the handler has returned,
the panel is the only channel left for reporting, which is why the worker
writes its outcome there. Keep the lock plugin-wide when multiple commands
share the same long-running resource or host-read path.

## Reading the active dataset

**A request that names no dataset addresses the primary dataset.** That holds
for `ctx.host.view_state()`, the volume reads and the `"view"` subscription:
none of them carries a dataset selector, and each means the primary dataset,
the one the person is working in. A selector, should vizi ever show several
datasets at once, is a later addition that leaves this meaning in place.

Declare `"data_access"` in the manifest permissions, then use `ctx.host` from
a command handler:

```python
from vizi_plugin_sdk import CommandContext, HostError, command, pb2


@command("analysis.run")
def run(ctx: CommandContext) -> None:
    try:
        view = ctx.host.view_state()
        volume = ctx.host.read_volume(view.t, view.visible_channels[0], view.level)
    except HostError as exc:
        if exc.kind == pb2.Error.UNSUPPORTED:
            ...  # for example, read a source already owned by this plugin
        raise
    analyze(volume)
```

`view_state(timeout=30)` returns a frozen `HostViewState`:

- `t`, `z`, `c`, `level` are the **requested** navigation state at the time
  of the call — what the user last set, not what happens to be decoded — so
  it is deterministic under scrubbing. The user may navigate again
  immediately afterward.
- `shape_tczyx` and `dtype` describe the open dataset.
- `has_crop`, `crop_min_zyx`, and `crop_max_zyx` describe the render crop in
  level-0 voxel indices, min-inclusive and max-exclusive. The crop is a
  render clip: reads return uncropped data and you apply the box yourself.
  `view.crop_box()` returns it as an `EdgeBox`, which converts exactly to
  another pyramid level where the index form cannot — with the declared
  per-axis factor (`edge_box.at_scale((sz, sy, sx))` from
  `view_state().levels`, or a tool's `pane.crop_at(n)`); the bare
  `.at_level(n)` assumes vizi's `2^n`.
- `mip` is the current 3D projection mode flag.
- `levels` contains `(level, (Z, Y, X))` entries currently readable. **Those
  are shapes, not scales** — the ratio of two shapes is not the factor on any
  odd dimension. The table may grow while a background pyramid build runs;
  the read reply is authoritative.
- `dataset_uri` is the source path/URI, or an empty string when none is
  available. A plugin-served dataset reports the exact URI from its
  `OpenReader` request. `dataset_name` is the host display name.
- `open_generation` is a process-monotonic identity stamp. It changes on every
  successful dataset open, including reopening the same URI.

`read_volume(t, c, level=0, roi=None, timeout=30)` returns a read-only numpy
array shaped `(Z, Y, X)` — a `HostDataArray`, an ndarray subclass carrying
`.open_generation`; compare that stamp with the `HostViewState` generation
that scheduled the read to detect a dataset swap. Call `.copy()` before
mutating. `roi`, when given, is `(min_zyx, max_zyx)` in the requested level's
coordinates, min-inclusive and max-exclusive; the host slices before
depositing. Reads may be issued concurrently from several threads; each reply
occupies its own deposit in the host's `V` segment until the SDK has copied
it out.

Failures always raise `HostError(kind, message)`: no dataset, missing
permission, invalid indices/ROI, unavailable level, malformed reply, or a
timeout caused by a dead session; `kind` is a `pb2.Error.Kind` (0 for a
timeout or malformed reply). One rule to know: when the active dataset is
served by **this same plugin process's reader**, a read is answered
`UNSUPPORTED` immediately, because serving it would call back into your
blocked reader. Read data your plugin already owns directly instead.

Tool contexts also expose `ctx.host`, but tool handlers run on the doorbell
listener. Spawn a worker for any host request from a tool; see [tools](tools.md).

## Contributing and reading native ROIs

Declare `"contribute_rois"`. `ctx.host.contribute_rois(rois, replace_all=False,
timeout=30)` accepts `pb2.RoiContribution` messages and waits for the host's
reply. Success returns `None`; every rejection raises `HostError`: missing
permission is `UNSUPPORTED`, no open dataset is `NOT_FOUND`, and malformed
geometry, namespace/parent violations, or attribute-limit violations are
`INVALID_ARGUMENT`.

`contrib_id` is stable only inside your plugin's namespace. Re-emitting the
same id replaces that native row. `replace_all=True` also removes this
plugin's rows omitted from the batch; it never touches user regions or
another plugin's regions.

**A contribution is one message, and a message is capped at 32 MiB.** That is
roomy for ordinary annotation and not roomy for tracking, because a region's
pose is dense — one row per frame, so 24 bytes per frame with only
`pose_translation` set and 96 with rotation and stretch as well. A few hundred
regions over a few thousand frames exceeds the cap, and the refusal comes
before anything is sent. Batch by region count when you are near it.

If you do batch, **`replace_all=True` belongs on the first batch alone.** It
removes every one of your rows absent from *that* batch, so setting it on each
one leaves you with only the last batch's regions. Send the first with it and
the rest without.

`Roi.attrs` is an invisible string-to-string metadata map for stable identity,
confidence, track ids, and similar plugin data:

```python
contribution.roi.attrs.update(
    {
        "identity": "AVA",
        "confidence": "0.93",
        "track_id": "7",
    }
)
ctx.host.contribute_rois([contribution])
```

An upsert replaces the whole attrs map; omitting attrs clears the previous
map. The host persists attrs and returns them from `ctx.host.get_rois()`
(a tuple of `pb2.RoiRecord`; needs `data_access`). Limits per ROI are 128
entries, 128 UTF-8 bytes per key, 4 KiB per value, and 64 KiB total.
`direction_specs` is not supported.

Two more, both around masks: `ctx.host.read_mask_voxels(region_id)` returns
one mask annotation's bbox-local boolean membership and its ZYX origin
(membership is static across time — fetch each mask once, then intensity
volumes per timepoint), and `ctx.host.compile_label_volume(overlay_id, item)`
turns a segment-backed [label-volume item](overlays.md#dense-label-volumes)
into one editable native mask per nonzero label.

### Which voxels a region covers — `read_roi_inclusion`

`ctx.host.read_roi_inclusion(region_id, t)` asks the host which voxels any
region `get_rois()` lists covers at frame `t`, computed the way the host
measures and draws it: the shape posed at `t` through its parent chain,
rasterized by its inclusion mode, clipped to the dataset
(`spec/control_plane_protocol.md` §8.7). It saves a plugin re-implementing
the host's rasterizer for boxes, ellipsoids, polygons and meshes. Needs
`data_access`.

```python
got = ctx.host.read_roi_inclusion(record.region_id, t)
if got is not None:
    mask, (z0, y0, x0) = got                     # bbox-local bool, level-0 corner
    if mask.any():
        dz, dy, dx = mask.shape
        volume = ctx.host.read_volume(t, c, 0, roi=((z0, y0, x0), (z0 + dz, y0 + dy, x0 + dx)))
        mean = float(volume[mask].mean())
```

It returns `(mask, origin_zyx)` — a read-only boolean `HostDataArray` and its
level-0 corner — or `None` when the region does not exist at `t` (a segment
of its time domain hides it; a state, not an error). A region that exists and
covers no voxel of the dataset is a `(0, 0, 0)` array. A mask answers its
stored membership, the same at every `t`. One frame per request. Failures
raise `HostError`: `NOT_FOUND` for no such region, `OUT_OF_BOUNDS` for a `t`
outside the dataset's frames, `UNSUPPORTED` for an inclusion mode the host
does not rasterize (`SHELL`, `POINTS`).

**Ask for only the part you watch with `roi=`.** `read_roi_inclusion(region_id,
t, roi=((z0, y0, x0), (z1, y1, x1)))` clips the answer to a level-0 box, min
inclusive and max exclusive, with the same semantics as `read_volume`'s
`roi`, and the host builds only that part — a tracker watching a corner of a
large region need not rasterize all of it. The mask and origin describe the
region's voxels inside the box; a region that misses the box is the
`(0, 0, 0)` array. An empty box, or one that leaves the dataset, is
`OUT_OF_BOUNDS`; a box without three values at each end is
`INVALID_ARGUMENT`. The host also has a voxel budget for one answer: a region
whose clipped box exceeds it is `OUT_OF_BOUNDS`, and a smaller box is the way
through.

### Reading a pose back — `get_rois` with a window

`get_rois()` with no arguments returns one `RoiRecord` per region **posed at
the frame on screen, with its pose cleared** — small, whatever the regions'
time domains. To read a pose back, pass a window of frames, both ends
inclusive: `get_rois(pose_t_start=a, pose_t_end=b)` returns each region in
its rest form with its dense pose rows **clipped to the window**, and
`record.roi.t_start` / `t_end` then describe the rows returned
(`spec/control_plane_protocol.md` §8.8).

**Every record of a region that has a pose domain carries the whole domain**
in `pose_domain_t_start` / `pose_domain_t_end`, windowed or not; a region
with no domain carries neither. So the test for "this record holds the whole
pose" is a comparison, not the fields' presence:

```python
def holds_whole_pose(record) -> bool:
    return record.HasField("pose_domain_t_start") and (
        (record.pose_domain_t_start, record.pose_domain_t_end)
        == (record.roi.t_start, record.roi.t_end)
    )
```

A record for which that is false holds a **slice**, and contributing its `Roi`
back truncates the region to the window with nothing to say so. Size a window
before you ask for it: the unwindowed read names every region's domain, and
a windowed reply is where the 32 MiB message ceiling is reachable (96 bytes
per fully posed region-frame). A reply that would exceed it is refused, never
clipped — `HostError` `OUT_OF_BOUNDS` naming the size — and you ask again
with a narrower window.

### Growing a pose — `extend_roi_poses`

`contribute_rois` takes a region's whole pose table, so a tracker that
extends its regions frame by frame and re-contributes them sends a table
that grows every time. `ctx.host.extend_roi_poses(extensions)` adds or
corrects rows on regions **this plugin** contributed instead. Needs
`contribute_rois`; returns `(region_count, frame_count)`.

```python
ext = pb2.RoiPoseExtension(
    contrib_id="cell-7",
    t_start=120,                     # first frame covered
    span=10,                         # frames covered
    pose_translation=rows,           # 3 * span doubles, ZYX per frame
)
regions, frames = ctx.host.extend_roi_poses([ext])
```

The rows have `Roi`'s layout: `pose_translation` and `pose_rotation` are
`3 * span` doubles, `pose_stretch` `6 * span`. **Overlap replaces**, so
re-tracking a stretch is an ordinary correction. The batch is checked whole
before anything is written, and refused (`INVALID_ARGUMENT`) for **a gap**
between the extension and the region's domain — a pose is dense, and a
filled gap would read back as a measurement — for a set of components that
differs from the set the region already carries, for wrong lengths, for a
region with no pose domain yet (start it with `contribute_rois`), or for two
extensions of one region in one batch. An unknown `contrib_id` is
`NOT_FOUND`.

## Activating a tool from a command

`ctx.host.activate_tool(tool_id, deactivate=False)` toggles one of **this
plugin's** declared [tools](tools.md), which is how a panel button starts an
interaction mode for a tool declared with `"menu": null`. No permission is
needed; the host refuses the request during an active gesture.

## Asking the host to import a user-selected file

For user-specific inputs such as fine-tuned model weights, whose size and
digest cannot be declared in the manifest.

**The picker waits on a person, so it does not belong in a command handler**
— see [above](#long-work-belongs-on-a-background-thread). The host HOLDS the
command's 10-second deadline while its own file dialog is up
(`spec/control_plane_protocol.md` §8.9), so a person browsing does not make
the session suspect. But the command worker is serialized, so every later
command is queued behind a handler that is waiting on a person. Ask from the
background thread and return from the command now:

```python
@command("analysis.weights")
def pick_weights(ctx: CommandContext) -> None:
    panel = ctx.panel("analysis")
    host = ctx.host

    def work() -> None:
        try:
            weights = host.request_asset("weights", timeout=600)
            if weights is None:
                panel.set_value("status", "No weights chosen.")
                return
            load_model(weights)
            panel.set_value("status", f"Loaded {weights.name}")
        except Exception as exc:
            panel.set_value("status", f"Could not load weights: {exc}")

    threading.Thread(target=work, daemon=True, name="weights-pick").start()
```

Declare `"data_access"`. The first call opens a host-owned native picker,
imports the selected file, and returns a `pathlib.Path`. Later calls reuse
the same slot, so only the first waits on anyone. Pass `force_reprompt=True`
only from an explicit replacement action. Cancellation returns `None`;
host/import failures raise `HostError`. Directory selection is not supported.

**One picker per session at a time.** A `request_asset` made while this
session's picker is still up is refused `HostError` `OUT_OF_BOUNDS` and opens
nothing (§8.9): a person answers one dialog at a time. That includes a pick
this plugin has stopped waiting for — the dialog stays up until the person
answers it — so ask again only once the first is answered.

How long a pick may take before the SDK gives up is the SDK's own choice —
nothing in the protocol sets it, and the 30-second default on ordinary host
requests is a dead-session backstop, not a limit on a person. This SDK's
`request_asset` takes a `timeout`; raise it for a picker rather than
expecting the default to accommodate browsing.

### Waiting on a person — `request_asset_async` and `request_status`

`request_asset` blocks for its whole `timeout` and gives up after it.
`ctx.host.request_asset_async(slot, force_reprompt=False)` sends the same
request and returns at once with a `PendingRequest`, shaped like a
`concurrent.futures.Future`:

| Member | Does |
|---|---|
| `request_id` | the id the request went out under |
| `done()` | whether the host's reply has arrived |
| `result(timeout=None)` | the imported `Path`, or `None` if the person canceled; waits up to `timeout` seconds (`None` waits indefinitely). A wait that times out raises `TimeoutError` and **leaves the request pending**, so it can be waited on again; a host refusal raises `HostError` |
| `status(timeout=2.0)` | asks the host about this request — below |
| `abandon()` | stops waiting for good; a reply that arrives later is dropped |

A command handler can start the pick and return, and a worker thread does
the waiting. Waiting in slices lets it ask the host what is going on:

```python
def wait_for_pick(host, slot: str):
    pending = host.request_asset_async(slot)
    while True:
        try:
            return pending.result(timeout=30)   # a Path, or None if canceled
        except TimeoutError:
            pass
        status = pending.status()
        if status.pending or pending.done():
            continue                            # awaiting_user: a person is browsing
        pending.abandon()
        raise RuntimeError(f"the host has no record of request {pending.request_id}")
```

`ctx.host.request_status(request_id, timeout=2.0)` is the same query for any
request id, from any thread (`spec/control_plane_protocol.md` §8.9). The
answer has three fields: `subject_id`, `pending` — the host took the request
in and has not answered it — and `awaiting_user`, set while the host's picker
is up for it, which is the signal to keep waiting rather than give up.
**It reports the host's bookkeeping and nothing more.** `pending` does not
promise the request will finish, or when; `pending=False` means the host has
no record — answered, abandoned, or never arrived — and is not evidence the
work failed (the reply may be arriving as you ask, which is why the loop
above checks `done()`). The short default timeout is deliberate: a status
query that itself times out raises `HostError`, and says the host is not
answering, not that it is gone.

## Minimal example — one button, one label

Manifest:

```json
{
  "manifest_version": 1,
  "id": "org.vizi.hello_command",
  "name": "Hello Command (minimal example)",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "hello_command.py"] },
  "contributions": {
    "commands": [
      { "id": "hello.count", "title": "Say hello" }
    ],
    "panels": [
      {
        "id": "hello",
        "title": "Hello",
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            { "kind": "label", "id": "status", "text": "Not clicked yet" },
            { "kind": "button", "id": "go", "text": "Say hello", "command": "hello.count" }
          ]
        }
      }
    ]
  },
  "permissions": []
}
```

`hello_command.py`:

```python
from vizi_plugin_sdk import CommandContext, command, serve

_COUNT = {"n": 0}

@command("hello.count")
def count(ctx: CommandContext) -> None:
    _COUNT["n"] += 1
    ctx.panel("hello").set_value("status", f"Hello x{_COUNT['n']}")

if __name__ == "__main__":
    serve(plugin_id="org.vizi.hello_command")
```

Open the panel from the Tools menu, click the button, watch the label.
