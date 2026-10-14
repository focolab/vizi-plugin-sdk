# Interactive segmentation — plugin specification

A plugin that lets the user lasso a region in a 2D view, grows a connected
component from what is underneath it, displays the result as a dense label
volume, commits those labels to native mask annotations, and plots each mask's
intensity over time.

This document specifies **behaviour**, not implementation: the
implementations beside it ([`python/`](python/), [`go/`](go/),
[`rust/`](rust/), [`java/`](java/)) each follow it on their language's SDK,
and [`python/hostsim.py`](python/hostsim.py) runs its scenarios against any
of them. The protocol is `1.0` (vizi v0.9.0 and later). Where this document
and `docs/spec/` disagree, `docs/spec/` wins.

## What this example demonstrates

- **A tool at event rate.** Input records (`E`, `InputEvent`) arrive on the
  doorbell listener thread; the handlers collect a gesture without blocking
  ([`doorbell_E_input.md`](../../docs/spec/doorbell_E_input.md)).
- **The overlay's Pulsed lane.** The lasso trail is published live through
  the `O` segment's lock-free lane, stamped with the pane and epoch it was
  computed under
  ([`shared_memory_protocol.md`](../../docs/spec/shared_memory_protocol.md)
  §3 and §6).
- **The annotation round trip.** A label volume is pushed as segment-backed,
  loaned geometry, compiled into native masks (`CompileLabelVolume`), listed
  (`GetRois`), read back (`ReadMaskVoxels`) and measured.
- **A panel the plugin drives.** Widget values, enabled and visible state,
  dropdown options and an image are pushed from the plugin, and re-pushed on
  `PanelOpened`.
- **Tool activation.** `ActivateTool` from a command, and the arrangement's
  arrival and withdrawal (`activated` / `deactivated`).
- **Jobs off the dispatch path.** Commands that validate, reply *accepted*
  and finish on a worker, one job at a time.

The tool declares `"views": ["2d"]`, refuses a projection, and anchors every
result to the plane the user drew on: the segmentation it specifies is planar
by construction. Where the protocol does not settle what a behaviour needs,
this specification says no out loud instead of guessing.

### Protocol notes

1. **The tool is 2D by choice.** An `E` record carries a `Ray` (camera, far
   end, entry) or a `RayAndPicked` in a 3D pane and a `Column` in a
   projection (`doorbell_E_input.md` §3). A host may send a `Voxel` at the
   slider's plane from a 2D projection where the rule says `Column`
   (`control_plane_implementation_details.md` §2b); the `Column` refusal in
   §3 is written to the rule, and `seg.pick`'s `ViewState.mip` check also
   catches a projection.

2. **A `line_set` overlay is invisible in the 2D views.** The 2D
   plugin-overlay pass rasterizes `Marker` and `LabelVolume` items only.
   Live lasso feedback is therefore **markers**, which is what §"The live
   lane" specifies. Lines do render in the 3D view.

3. **`CompileLabelVolume` has requirements a displayable item need not meet.**
   The host rejects the request unless

   - the item's transform is `frame = "voxel"`, unit scale, identity rotation,
     and an **integer** `position_zyx`; and
   - **both** the labels and the colormap are **segment-backed**, not inline.

   Both constraints bite only at compile time: the very same item displays
   with a fractional position and inline geometry. `frame = "voxel"` is the
   dataset's level-0 lattice. The Python SDK's builders default to
   `frame="voxel"` and to segment-backed geometry; check what your SDK
   defaults to. The emulator checks exactly these constraints.

4. **The record is authoritative; `ViewState` is not.** `InputEvent.position`
   carries the plane under the cursor — in the montage pane the *hovered
   tile's*, which differs from `ViewState.z` for every tile but one — and
   `t`, `level` and the visible-`channels` mask ride the same record. Never
   re-derive any of them from a `ViewState` fetched later.

5. **Label volumes composite in 3D as well as 2D.**

6. **`size_px` means different things in the two views.** The 3D pass treats
   it as screen-constant pixels, converting through the view's
   `world_per_pixel`. The 2D pass treats it as a **diameter in voxels**: it
   stamps a disc of radius `max(1, round(size_px / 2))` into the slice array,
   so a marker scales with zoom instead of staying constant. Pick the value
   for the view you are drawing in.

7. **A push and a compile that name one reservation are two uses, and the
   sender must say so.** `control_plane_protocol.md` §5.6: a loan is released
   once every *declared* use is disposed of, and `CompileLabelVolume` repeating
   an `OverlayUpdate`'s descriptor is a second use (§8.8). The Python SDK's
   `label_volume_item(..., uses=2)` is the declaration; §"Committing" has it.
   Reserving twice is also conforming — a second copy, never a corruption.

8. **`InputEvent.channels` is a set, not a channel** (`doorbell_E_input.md`
   §1). A composite of several channels is what the user sees; nothing says
   which one they meant to segment. §"Refusing what the gesture does not
   settle" refuses more than one. Bit 15 stands for every visible channel at
   index ≥ 15 and is resolved through the pane epoch's `channels_high`; the
   Python SDK exposes that as `ctx.pane(ev).visible_channels(ev.channels)`,
   the exact set. An SDK that exposes only the 16-bit mask can still count
   bits 0–14 exactly and must refuse on bit 15, because it cannot say how
   many channels that bit is.

9. **A record can arrive marked unusable** (`doorbell_E_input.md` §3,
   `InputEvent.usable` in the Python SDK). When a record's `level` is not
   one the epoch's `Geometry.levels` declares, a non-`miss` voxel lies
   outside the clip box at that level, or `t` is past an arranged time
   table, the SDK reports `unusable_context` to the host and **still
   delivers the record**, because a terminal must close what it closes. A
   handler does not guess a coordinate from such a record: treat it as a
   point to drop, exactly like `miss`.

## What a gesture transmits

Everything a tool learns about a gesture is one `E` record per input event
(`docs/spec/doorbell_E_input.md`; `InputEvent` in the Python SDK): `phase`, a
`position` whose **shape depends on the pane**, the pane (`view_id`) and the
`epoch` it is under, the gesture (`interaction`, `seq`), the dataset `gen`,
`t`, `level`, a visible-`channels` mask, `mods`, `click_count`, a target, a
`miss` flag, and an `action` index that is 0 on every record this tool
receives, because it declares no actions. There is no screen coordinate and
no pick buffer.

What a record does **not** carry, the arrangement does. The plugin acknowledged
an `EpochChange` for the pane before any record under that epoch could arrive,
and that epoch's `DatasetState` — the `Geometry` with its extent, clip box,
spacing (with a unit and a provenance), per-level sampling factors and channel
names, and the arranged `TimeMapping` — is already installed when the handler
runs. In the Python SDK it is `ctx.pane(ev)`, a `PaneState`, with no round
trip; `visible_channels(ev.channels)` on it is the exact channel set.

**In a 2D plane view**, `position` is a `Voxel` `(z, y, x)` **in the voxel
index space of the record's `level`**: `z` is the displayed plane, `y, x` the
row and column under the cursor, already undone for any display rotation or
flip. A drag arrives as `PRESS`, then `DRAG` per mouse-move, then `RELEASE`,
all under one `interaction` with increasing `seq`. **A bare click arrives as
a `PRESS` and a `RELEASE` at the same voxel with no `DRAG` between them** —
the degenerate path §"The selection" has to handle.

**In the montage view**, the same, `z` being the plane of the tile under the
cursor. It is on the record, so nothing is re-derived from `ViewState.z`,
which is the *selected* plane and differs from the hovered one for every tile
but one.

**In a projection (MIP)**, `position` is a `Column`: the two coordinates in
the plane the user sees and the half-open range of the collapsed axis — the
clip bounds, not the lit set. The record says outright that the user was
looking at a projection; nothing anywhere says which structure in that column
they meant. **This tool refuses a `Column`** — see §"Refusing what the gesture
does not settle".

**In the 3D view**, `position` is a `Ray` — the camera, the far end of the
segment through the clip box, and where it enters — or a `RayAndPicked` when
something was under the cursor. **This tool declares `"views": ["2d"]`.** The
segmentation it specifies is planar by construction — a lasso encloses area on
the plane it was drawn on — and extending it to a picked point in 3D is a
different tool. A `Ray` arriving anyway is ignored, and `miss` on any record
means "no relevant place here": treat it as a point to drop.

**A gesture can end without its own `RELEASE`.** A camera move (for a tool
with `cancel_on_camera_move`, the default), a pane or dataset change, a tool
switch or deactivation each end the open interaction with a `CANCEL` record —
and where the host could send no record at all, the SDK hands the handler a
`Cancellation` instead. Either way the tool drops the path and retracts the
trail.

**Coordinates are in the record's `level`, not normalised to level 0**, and a
level-0 *index* has no exact level-N index at all (E §3). Everything below is
written at level 0; a gesture at another level is refused rather than
rescaled — see §"Refusing what the gesture does not settle" — and mapping the
lasso through E §3's affine instead is the obvious extension.

Everything below is built on the one thing the gesture reports exactly: **the
plane the user drew on**. Call it `z_g` — the `z` of the `PRESS`. It is exact
in the slice view and the montage; a projection does not have one.

So the design commits to it rather than working around it. The result is
required to contain a voxel on `z_g`, a projection is refused instead of
guessed at, and the one place `z` is reduced away — the threshold's column
maximum — is there to spare the user from having to find the object's
brightest plane, not to recover a depth the gesture did not carry.

## What the plugin does

The user checks the tool (Tools menu, or the panel's **Select** button), drags
a closed loop around something in a 2D view, and releases. A marker trail
follows the cursor while they drag. On release the plugin reads the enclosed
region, derives a threshold from it, grows a connected component, and paints
the component as a colored label volume in every view.

**Commit** turns the accumulated labels into first-class native mask
annotations, one per label. **Measure** reads one committed mask back and
plots its mean intensity against time in the panel.

## Manifest

```json
{
  "manifest_version": 1,
  "id": "org.example.interactive_seg",
  "name": "Interactive Segmentation",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["<your launcher>", "<your entry point>"] },
  "permissions": ["data_access", "contribute_rois"],
  "contributions": {
    "tools": [
      { "id": "seg.lasso", "name": "Lasso segment", "views": ["2d"] }
    ],
    "overlays": [
      { "id": "seg.trail",  "name": "Lasso trail" },
      { "id": "seg.labels", "name": "Segmentation", "segment_mib": 64 }
    ],
    "commands": [
      { "id": "seg.set_min_size",   "title": "Set minimum size",      "menu": null },
      { "id": "seg.set_threshold",  "title": "Set threshold scale",   "menu": null },
      { "id": "seg.set_3d",         "title": "Set 3D growth",         "menu": null },
      { "id": "seg.set_mode",       "title": "Set accumulate mode",   "menu": null },
      { "id": "seg.set_region",     "title": "Set measured region",   "menu": null },
      { "id": "seg.pick",           "title": "Activate lasso",        "menu": null },
      { "id": "seg.commit",         "title": "Commit to annotations", "menu": null },
      { "id": "seg.measure",        "title": "Measure over time",     "menu": null },
      { "id": "seg.reset",          "title": "Clear segmentation",    "menu": null }
    ],
    "panels": [
      {
        "id": "seg",
        "title": "Interactive Segmentation",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "button", "id": "pick", "text": "Select region…", "command": "seg.pick" },
            { "kind": "row", "id": "rsize", "children": [
              { "kind": "label",  "id": "lsize", "text": "Minimum size (voxels)" },
              { "kind": "slider", "id": "size", "command": "seg.set_min_size",
                "min": 1, "max": 5000, "step": 10, "value": 50 }
            ]},
            { "kind": "row", "id": "rthresh", "children": [
              { "kind": "label",  "id": "lthresh", "text": "Threshold \u00d7" },
              { "kind": "slider", "id": "thresh", "command": "seg.set_threshold",
                "min": 0.5, "max": 2.0, "step": 0.05, "value": 1.0, "float": true }
            ]},
            { "kind": "checkbox", "id": "grow3d", "command": "seg.set_3d",
              "text": "Grow through Z", "value": true },
            { "kind": "dropdown", "id": "mode", "command": "seg.set_mode",
              "options": ["Replace", "Add"], "value": "Replace" },
            { "kind": "button", "id": "commit", "text": "Commit to annotations",
              "command": "seg.commit" },
            { "kind": "row", "id": "rmeasure", "children": [
              { "kind": "dropdown", "id": "region", "command": "seg.set_region",
                "options": ["(none)"], "value": "(none)" },
              { "kind": "button", "id": "measure", "text": "Measure", "command": "seg.measure" }
            ]},
            { "kind": "image",  "id": "plot" },
            { "kind": "button", "id": "reset", "text": "Clear", "command": "seg.reset" },
            { "kind": "label",  "id": "status", "text": "Ready" }
          ]
        }
      }
    ]
  }
}
```

`docs/manifest.md` is authoritative over this sketch if the two disagree.

Notes on choices that are load-bearing:

- **`"menu": null` on every command**: a bare
  menu invocation sends no arguments, so a widget-bound command invoked from
  the menu can only misbehave. The tool's own checkable entry and the panel's
  "Show Interactive Segmentation" entry are the front doors.
- **`"views": ["2d"]` on the tool**, per §"What a gesture transmits".
- **Two overlay ids, two `O` segments.** Each declared overlay is
  granted its own segment — its own Pulsed lane and its own arena, keyed by
  the overlay id as the grant's `subject` — so retracting the trail cannot
  disturb the labels and the two lanes cannot collide.
- **`segment_mib: 64`** on the label overlay. The default 32 MiB is comfortable
  for geometry and tight for a dense label volume over a real crop — note that
  the array is bbox-tight around the component, not around its voxels, so a
  sparse diagonal object can need far more than its own voxel count. The 64
  applies to `seg.labels` alone. And because
  pushed geometry is a loan the host releases after use, an arena that is full
  when the next push arrives fails that push rather than overwriting.
- **No `triggers`, `actions` or `pane`.** The tool is engaged from the Tools
  menu and, once engaged, owns the primary pointer: no tool names the left
  button. A `manifest_version: 1` manifest could ask for a key that engages
  it or a mid-drag action; this example does not.
- **A float slider and an int slider.** A manifest slider with `"float": true`
  sends its value as a `double`; without it, as a signed integer. Both arms of
  `Value` reach the plugin only if both exist.

## The UI state model

Each widget's invocation carries only that widget's own value, a button sends
nothing, and there is no way to ask the host what another widget holds. The
plugin owns the model, seeded from the same defaults the manifest declares.

The plugin also **pushes** state back:

- `commit` is disabled (`set_enabled(false)`) until a segmentation exists, and
  re-disabled after a reset.
- The `rmeasure` row is hidden (`set_visible(false)`) until a commit succeeds.
- The `region` dropdown's options are replaced (`set_options`) with the
  committed region names after each commit.
- On panel open, re-push everything. The host rebuilds the panel from the
  manifest's declared initial values on every open and persists nothing, so a
  reopened panel silently disagrees with the plugin's model otherwise. The
  event that lets you fix this is `PanelOpened`, a control-plane one-way
  (`@panel_opened` in the Python SDK).

Pushing a value into a widget does **not** re-fire its command — the host
blocks widget signals around every patch, and a dropdown's invocation comes
from user selection only. Without that guarantee the re-push above would be an
infinite loop; with it, it is five lines.

**`seg.pick`** turns the tool on: `ActivateTool("seg.lasso")`, whose reply
`ToolActivationChanged` reports whether it is now active. Before activating,
fetch a `ViewState` and, if `mip` is set, warn in the status line without
activating — telling the user up front beats letting them draw a whole lasso
that §3 will refuse. Two things about tool activation to design around rather
than discover:

- The host **rejects activation during an active gesture**, with
  `INVALID_ARGUMENT`. Report it and move on.
- **User-initiated deactivation is announced on the wire.** `Esc`, unchecking
  the menu entry, or engaging another tool withdraws the arrangement with a
  `ToolBinding{retired}` (`control_plane_protocol.md` §8.10), and an open
  gesture ends with a `CANCEL`. The Python SDK surfaces the
  arrangement's arrival and withdrawal to the tool class as `activated(ctx)`
  and `deactivated(ctx)`, called after its own handling (handle installed or
  gone, open gesture cancelled). Use them: set the `status` line to *"Lasso
  active — drag a loop in a 2D view"* on `activated` and back to *"Ready"* on
  `deactivated`, so the panel reflects the tool's real state and not only the
  outcome of its own last `ActivateTool`. On an SDK without the hooks the
  plugin reports the outcome of its last request and clears the trail on
  `cancel`. Precisely: the status line moves to *"Lasso
  active…"* on an `ActivateTool` reply with `active: true` and to the
  refusal on a refusal; the trail clears on `cancel`; and **nothing moves on
  the arrangement's arrival or withdrawal, because the plugin cannot see
  them** — a plugin must not claim an engagement it did not observe, so a
  Tools-menu engagement leaves the line at *"Ready"* until the user's next
  action. The driver accepts either behaviour and says which it saw
  (Acceptance 17).

## The tool

### 1. Collecting the gesture

Tool handlers run **on the doorbell listener thread, at event rate**. Do cheap
work only: append to a buffer, push a live update, return. Never issue a host
request (`view_state`, `read_volume`, …) from a handler — that thread also
delivers segment grants, so blocking it can deadlock the very read you are
waiting for.

- `press` — if `position` is not a `Voxel`, or `miss` is set, or the record
  is marked unusable (note 9), take no point from the record (a `Ray`
  should not arrive for a `"2d"` tool, and a miss is a real coordinate in a
  place this tool does not want). **A `Column` is the one non-`Voxel` that
  is not silent**: it says the user is looking at a projection, and §3
  refuses it by name — remember the refusal, take no point, publish
  nothing, and report it at `release`. Otherwise start a new path with
  `(z, y, x)`. Record the press's `z` as **`z_g`, the drawn
  plane**, along with its `t`, `level`, `channels`, `gen`, and the record's
  `view_id`/`epoch` (the pane pair the live lane will be stamped with); **use
  those for the whole gesture** and ignore any later disagreement. `z` is a
  `double` on the wire and both 2D panes send an integral one — take
  `int(round(z))` and do not assume. Each record carries the view model as it
  was at that event, so a user who scrubs mid-drag would otherwise produce a
  path spanning two timepoints. `z_g` is the anchor everything downstream is
  built on (§7). **When the press itself contributes nothing** — a miss, an
  unusable record — the gesture is still open: its context (`t`, `level`,
  `channels`, `gen`) is the **first usable record's**, `z_g` is the **first
  usable `Voxel`'s `z`, a miss included** (a 2D miss is the true off-image
  coordinate on the displayed plane, `E` §3), and the points are the usable
  non-miss `Voxel`s from then on. A gesture no usable record reached ends in
  silence at `release` — nothing taken, nothing said, the next gesture live
  (Acceptance 16). The pane pair is the press's regardless; it is the
  arrangement's, not the record's. A miss press never leaves a drawn loop
  silently ignored.
- `drag` — append `(z, y, x)` (a `miss` or unusable record contributes
  nothing). Do not deduplicate; consecutive identical points are normal and
  harmless. **The trail is drawn at `z_g`**, not at each record's own `z`:
  the path is a loop on the drawn plane, and a user who changes slice
  mid-drag has not moved the loop.
- `release` — append, retract the trail overlay, and hand the completed path to
  your own worker. Return immediately.
- `cancel` — the host ended the gesture (a camera move, a pane or dataset
  change, deactivation) or the SDK is telling you it can never complete:
  drop the path and retract the trail. The argument may be a `CANCEL` record
  or a `Cancellation`; treat them alike.

A record from a different `interaction` than the one open is a new gesture;
the SDK delivers the cancellation of the old one first.

### 2. The live lane

While the gesture is in progress, publish the trail through the overlay's
**Pulsed lane**, never through gRPC. In SDK terms that is
`ctx.overlay("seg.trail").set_items_live([...], pane=ev)`; on the wire it is
`shared_memory_protocol.md` §6 — the `O` segment's plugin-published `PULSED`
region, three buffers: claim a free buffer, write the serialized
`OverlayUpdate` (and nothing else) into its slot from byte 0, write the pane
pair `(view_id, epoch)` into the slot's **last eight bytes**
(`epoch_offset = slot_bytes − 8`), publish the payload length, commit the
buffer as newest — then ring the doorbell with a `D` naming the segment and
region and carrying the same pair. The host claims the newest committed
buffer on its own display-rate schedule; the `D` is a hint that brings that
scan forward and may be refused without loss (`doorbell_D_notification.md`
§5).

The handoff discipline — claim, write, commit, one buffer at a time, three
so that a reader holding one and a writer filling one still leave a committed
newest — is §6's, and it is the whole correctness argument. **Each overlay
has its own segment and its own lane**, so two overlays publishing live
cannot collide. Only `seg.trail` does here.

Requirements:

- **Markers, not lines.** Note 2. One `marker_item` containing the path
  points, color `(255, 255, 255, 220)`. Start at `size_px = 5` — a 2-voxel
  radius in the 2D pass (note 6), which is a legible dot at ordinary zoom
  and a coarse one when zoomed out.
- **Inline geometry only** (`inline=True` on the builders). A Pulsed value is
  claimed and consumed whole, so a torn read must never be able to chase a
  segment reference. It is a wire rule with a code
  (`shared_memory_protocol.md` §3.3, `live_geometry_not_inline`), the Python
  SDK's `check_live_overlay` refuses a segment-backed buffer anywhere in the
  update — label volumes included — and the same function requires the
  update's `overlay_id` to equal the lane's `subject`.
- **Pass the event you are answering as `pane=ev`.** The value then carries
  the pane and epoch the trail was computed under, and the host applies it
  only while that is still the pane's current epoch: a trail computed against
  a crop or level the user has since moved off is consumed and counted, never
  drawn in the wrong place.
- **The payload must fit the granted slot** — `slot_bytes − 8` for a lane
  with a pane header. Honour the region's `slot_bytes` from `InstallSegment`
  rather than a constant. **Fail rather than truncate.**
- **Decimate deterministically.** A long drag can exceed the slot: a slow
  careful lasso is thousands of ZYX doubles before protobuf overhead. Publish
  at most `LIVE_MAX_POINTS = 512` points, chosen by uniform index sampling —
  for a path of `n` points and `k = min(n, 512)`, index
  `round(i * (n - 1) / (k - 1))` for `i` in `[0, k)`, deduplicated, with
  `k = 1` meaning just the first point. The *full* path is what the
  segmentation uses; only the preview is decimated.

Publish on every `drag`. Do not rate-limit: a publish that finds every buffer
busy is **coalesced, not failed** — the newest committed value stays readable
and the next publish supersedes it — and dropping one only ever loses an
intermediate frame. **Except under a gesture §3 will refuse**: a gesture on a
`Column` has no points, and one at a `level` other than 0 carries level-*n*
coordinates that the marker's `voxel` frame — the level-0 lattice — would
draw at the wrong scale; publish no trail for either. A multi-channel gesture
is in the right frame and gets its trail.

On `release` or `cancel`, retract the trail (`OverlayRemove` — `clear()` in
the SDK). Do it from the handler, before handing off, so the trail cannot
outlive the gesture if the worker fails. The lane and the control plane have
no ordering guarantee relative to each other, so a host may draw one more
frame of trail after the retraction lands; that is acceptable and not worth a
second mechanism.

**A live publish that fails** — the payload does not fit the slot, the lane
is not granted, the SDK refuses the update — is reported to the user **once
per gesture** through the panel's `status` line, naming the cause and saying
the segmentation will still run on release (the preview is decoration; the
full path is what the worker uses). Clear the once-per-gesture flag on
`press`. Silence here would leave the user dragging with no trail and
nothing saying why.

## The segmentation

All of this runs on your own worker, off both the listener thread and the
command worker.

### 3. Refusing what the gesture does not settle

Three things the record can say that this tool answers with a refusal in the
status line and no other change. Each is the honest outcome, not a limitation
to be papered over: anything the plugin did instead would be a guess dressed
as an answer, and it would fail silently — a plausible region on a plausible
plane, just not the one the user chose.

- **A projection.** The gesture's records carried a `Column` (or `seg.pick`
  found `ViewState.mip` set before activating):

  > `"Turn off Z-MIP — the tool needs to know which plane you are pointing at."`

  The user is looking at a projection; when a column holds two structures
  nothing anywhere says which one they meant. Check it here even though
  `seg.pick` checks: MIP can be switched on between activating and drawing,
  and the record is the authority.
- **A pyramid level other than 0.** The record's `level` is not 0:

  > `"Zoom in to full resolution — the tool segments level-0 voxels."`

  (A record at a level the pane's `Geometry.levels` does not *declare*
  never gets here: the SDK marks it unusable — note 9 — and it is a
  point to drop. This refusal fires for a declared non-zero level, which
  a host with a pyramid sends when the user is zoomed out.)

  The coordinates are exact at their own level and a level-N pixel covers a
  block of level-0 voxels; mapping the lasso down through E §3's affine is the
  obvious extension and is not this example's.
- **Not exactly one visible channel.** The record's visible set — resolved
  through the pane epoch where the SDK offers it (`ctx.pane(ev)
  .visible_channels(ev.channels)` in Python), else the 16-bit mask — has
  zero members or more than one; with only the mask, bit 15 set is also a
  refusal, since it stands for an unknown number of channels (note 8):

  > `"Show a single channel — the tool cannot tell which one you mean."`

  `channels` is a set; a composite is what the user saw, and this tool
  segments intensities of one channel. The one visible channel is **`c`**
  for the run.

Take the volume shape from the pane's `Geometry.extent` if your SDK gives
you the arrangement, else from a `GetViewState` here. Take `t`, `c`, `z_g`
and `gen` from **the gesture**, not from a snapshot: the user may have
scrubbed or changed slice between releasing the mouse and the worker getting
a reply, and the segmentation belongs to the frame they drew on.

The record's `gen` is the low 32 bits of the dataset's `open_generation`, so
a reopen between the gesture and the read is detectable: compare it against
the low 32 bits of the generation stamped on every array `read_volume`
returns, and abandon the run if they disagree.

### 4. The selection

The path is a list of `(z, y, x)` in gesture order. Project it to `(y, x)` and
close it by joining the last point to the first: that polygon is the lasso.

A voxel column `(y, x)` (integers) is **selected** when the point
`(y + 0.5, x + 0.5)` is inside the polygon by the **even-odd rule**. Pin the
test so implementations agree — the standard crossing-number form, for polygon
vertices `v[0..n)` and query `(py, px)`:

```
inside = false
for each edge (a, b) = (v[j], v[(j+1) % n]):
    if (a.y > py) != (b.y > py):
        cross_x = a.x + (py - a.y) * (b.x - a.x) / (b.y - a.y)
        if px < cross_x:
            inside = not inside
```

Accumulate in `f64`. Clip the selected set to `[0, Y) × [0, X)`.

Call the selected set **S**. If **S** is empty — a bare click, a
back-and-forth path with no enclosed area, a lasso entirely outside the volume
— fail with `"Drag a closed loop around a region."` and stop. This is a normal
outcome, not an error condition; a click is how a user finds out the tool wants
a drag.

### 5. The search window

Let `(y0, x0)`–`(y1, x1)` be the exclusive bounding box of **S**. Expand it by

```
margin_y = max(8, round(0.25 * (y1 - y0)))
margin_x = max(8, round(0.25 * (x1 - x0)))
```

on each side, clip to the volume, and take **all of Z**. Call that window
**W** — a `(Z, h, w)` box. `round` here, and in §2's decimation, is **round
half to even** — Python's `round`, Rust's `f64::round_ties_even`, Go's
`math.RoundToEven`, Java's `Math.rint`; not `Math.round`, `f64::round` or
`math.Round`, which round a half up or away. It decides nothing below a side
of 34 (the floor of 8 hides it) and nothing in the decimation at 512 (no
exact halves), and implementations still have to agree on the read they send.

Read it with **one** sub-box request:
`ReadDataVolume{t, c, level: 0, roi_min_zyx: (0, wy0, wx0), roi_max_zyx: (Z, wy1, wx1)}`.
The reply descriptor's `shape_zyx` is the ROI's shape, not the level's. Copy
the bytes out and send `ReleaseDeposit` with the same
`segment_id`/`offset`/`len`.

**Read at level 0.** A label volume's position is a
voxel index with no pyramid level attached, and the level-*n*-to-level-0
conversion is unspecified.

Two consequences:

- The component cannot grow outside **W**. When the result touches a face of
  **W** that is not also a face of the volume, say so in the status line
  (`"clipped by the search window"`). Silently returning a truncated object is
  the failure mode this tool most easily hides.
- **W** spans the full Z of the dataset, which on a deep volume with a wide
  lasso is a large read. The obvious bound is a band around `z_g` of some
  physical depth, and §6 says why this document still does not specify one.

### 6. The threshold

For each `(y, x)` in **S**, let `m(y, x) = max over z of W[z, y, x]` — the
brightest voxel in that column, the whole way down.

```
T = threshold_scale * median({ m(y, x) : (y, x) in S })
```

`threshold_scale` is the float slider (default 1.0). The median of an
even-sized set is the mean of its two middle values. Compute in `f64`.

**Non-finite intensities** (an `f32` volume can carry NaN or ±Inf from
deconvolution or flat-field correction): a NaN voxel and a `-Inf` voxel never
contribute to a column maximum, and a column with no contributing voxel —
every voxel NaN or `-Inf` — is **dropped from the median** rather than
admitted as `-Inf` or allowed to refuse the run. If every selected column is
dropped, fail with `"Nothing measurable under the loop."` and stop. A `+Inf`
voxel does contribute, and is its column's maximum. So the set the median is
taken over holds finite values and `+Inf` only; its median is finite or
`+Inf`; and, `threshold_scale` being positive and finite by the slider's
range, **`T` is finite or `+Inf` — never `-Inf`, never NaN.** In §8, then,
by ordinary comparison and with no exception to `M = (W ≥ T)`: a NaN voxel
and a `-Inf` voxel are never `≥ T`, so both are background; a `+Inf` voxel
is always `≥ T`, so it is foreground; and when `T = +Inf` the `+Inf` voxels
are the whole foreground. This policy survives a stray bad voxel without
either poisoning the threshold or refusing the gesture; treating `-Inf` as
NaN for the maximum is what keeps `T` from being `-Inf` (an all-`-Inf` loop)
or NaN (the even median `(-Inf + Inf) / 2`).

Two things this buys, and one it costs.

It spares the user from having to find the object's brightest plane: a column
maximum is the same whether they scrolled to the middle of the object or one
plane off it, so the threshold does not lurch as they scrub. And the median over
a loop that is part object, part background sits between the two, which is what
a threshold should be — **when the loop is roughly half object.** A loop with
more background than object under it has a *background* median, `T` is the
background, and the whole search window is foreground: the component floods
**W**, `MAX_COMPONENT` does not catch a window-sized flood, and what the user
sees is a box with §5's *clipped by the search window* on it. That is the rule
behaving as written, and the `Threshold ×` slider is the escape
hatch; the driver provokes it on purpose so the clip report is
checked. A user draws close to the object, and this is why.

What it costs is that a *crowded* column raises `T`. If something bright sits
above or below the object in the same footprint, its column maximum enters the
median and makes the tool stricter than "what is bright on this plane". A
`z`-band around `z_g` of a fixed *physical* depth would be the obvious
refinement, and it can be specified: the pane's `Geometry.spacing` carries
the pitch with a unit and a
provenance (`control_plane_protocol.md` §8.11), and a tool reads it from the
arrangement (`ctx.pane(ev).spacing_zyx` / `spacing_known` in Python). **This
specification still takes the whole column**, for two reasons: the provenance
is often `display-fallback`, meaning *unknown*, and a band that silently
degrades to "all of Z" on such a dataset is the kind of behaviour this
document refuses elsewhere; and keeping the algorithm fixed is what keeps
implementations comparable. The `Threshold ×` slider is the escape hatch.

### 7. The anchor

**The result must contain a voxel on the plane the user drew on.** State that as
an invariant before anything else, because the alternative is the failure this
design exists to avoid: a user focuses to `z = 9`, lassoes a cell, and gets back
a mask sitting entirely at `z = 3` because something brighter happened to be
under the same footprint. That is not a near-miss, it is the tool answering a
question nobody asked.

So define the candidate set as

> the connected components of `{W ≥ T}` — §8 fixes which connectivity — that
> contain at least one voxel `(z_g, y, x)` with `(y, x) ∈ S`

where `z_g` is the `press` event's `z`. Membership in **S** alone is not enough:
that is the version that produces the `z = 3` mask, because a component sitting
entirely at `z = 3` still has voxels whose `(y, x)` the user circled.

Separately, compute the **diagnostic**: `(z*, y*, x*)`, the position of the
maximum of `W` over all `z` and over `(y, x) ∈ S`, ties resolving to the lowest
raster-order index. It anchors nothing. Its job is §8's failure message.

### 8. Growing

Let `M = (W ≥ T)`.

- With **Grow through Z** on: 6-connected components of `M` in 3D — the six
  face neighbours, no diagonals.
- With it off: 4-connected components of `M` within the plane `z = z_g` alone,
  and the result is one voxel deep.

Take the **largest by voxel count** among the components meeting §7's anchor
condition. Ties resolve to the component containing the lowest raster-order
voxel. Both modes then satisfy the invariant by construction; in single-plane
mode it is trivial.

Taking the largest rather than the one containing some particular voxel is what
makes a lasso around two touching lobes behave the way the user drew it.

**The lasso indicates; it does not clip.** A component may extend well outside
the loop in `y`/`x` — that is what region growing is, and circling roughly to
get an exact boundary is the point. The only clip is **W**. An implementation
that intersected the result with **S** would be a different (and defensible)
tool.

Then:

- **No component meets the anchor condition.** Report where the structure
  actually is, using the diagnostic: `"Nothing above the threshold on plane 9.
  The brightest voxel you enclosed is at z=3 — scroll there, or lower
  Threshold ×."` The user's next action is now obvious, which is the whole
  reason for computing `z*`. This is the expected outcome when a tight loop is
  drawn several planes off the object.
- Fewer than `min_size` voxels (the integer slider) — report
  `"No component of at least N voxels."` and change nothing.
- More than `MAX_COMPONENT = 8_000_000` voxels — report
  `"Selection too permissive — try a tighter loop or a higher Threshold ×."`
  and change nothing. Without this bound a low threshold floods the window and
  the next step tries to push it through a shared-memory segment.

### 9. Publishing the result

Accumulate into a label map. In **Replace** mode each run starts from empty
with label id 1. In **Add** mode each run takes the next id, 1 through
**255**, and refuses beyond that; where a new component overlaps voxels an
earlier label already holds, **the newer label wins** those voxels.

The 255 cap is not arbitrary: a label draws with LUT row `1 + (id − 1) mod
255` (`control_plane_protocol.md` §8.8 — row 0 is label 0's alone, and ids
above 255 wrap over the foreground rows), so ids past 255 share a colour with
an earlier label. They keep their own *names*, since `label_names` is keyed
by label id, but two regions in one colour is still a display
the user cannot read.

The accumulation is scoped to `(t, c, open_generation)` from the gesture. A
gesture at a different timepoint or channel, or after the dataset is reopened
(`ViewState.open_generation` changes on every successful open, including
reopening the same file), starts a fresh accumulation. Say so in the status
line when it happens.

On success, say so in the status line, so a refusal from the last gesture is
not left standing over a result. The information is fixed — the end of this
section lists it: label id, voxel count, threshold, clipped or not — and the
words are yours; the driver looks for the voxel count as a numeral and for
§5's *clipped by the search window*, nothing else.

Push one `label_volume_item` on the `seg.labels` overlay:

- `labels` — a **bbox-tight** `(Z, Y, X)` `uint8` array covering exactly the
  union of the accumulated components. Not the search window: the tight box is
  smaller, and the transform is what places it.
- `colormap` — a `(256, 4)` `uint8` RGBA LUT. Row 0 is `(0, 0, 0, 0)`;
  row `k` for `k ≥ 1` is entry `(k - 1) % 8` of

  ```
  (230, 25, 75)  (60, 180, 75)  (67, 99, 216)  (245, 130, 49)
  (145, 30, 180) (66, 212, 244) (240, 50, 230) (191, 239, 69)
  ```

  at alpha **180**. Alpha 0 in the LUT makes the compiled region invisible, so
  do not use it for a live label.
- `position_zyx` — the tight box's origin in volume coordinates. **Integers.**
- `frame` — `"voxel"`.
- `t` — the gesture's timepoint, so the labels show only where they were
  measured. Omitting it shows them at every `t`, which is wrong for a
  single-timepoint segmentation.
- `label_names` — optional, **keyed by label id** (`map<uint32, string>`;
  a name for 0 is ignored). Supply `"Region k"` so the committed annotations
  are legible.

This is an ordinary (non-live) push: `OverlayUpdate` over gRPC with
segment-backed geometry, a **full replacement** of the overlay's draw list.
Geometry is reserved from the overlay's arena and is a **loan**
(`control_plane_protocol.md` §5.6): the bytes stay lent until the host has
disposed of every declared use and sent `ReleaseLoan`. A label volume the
arena cannot hold fails the push cleanly (`NoRegion` in the Python SDK,
naming what was free and wanted) rather than writing past the end or over
bytes still lent.

Then enable `commit` and report the result: label id, voxel count, threshold,
and whether it was clipped.

## Committing to native annotations

`seg.commit` sends `CompileLabelVolume{overlay_id: "seg.labels", item,
replace_all: true}` with **the same item descriptor** that was pushed. Only
the descriptor travels; the label bytes stay in the overlay segment where the
host can already read them. The reply is `LabelVolumeCompiled{region_count}`.
`replace_all` is `true` so that an Add commit of `{1, 2}` followed by a
Replace commit of `{1}` does not strand native label 2 — the plugin's masks
are always exactly the labels it currently holds.

The host makes one native mask annotation per nonzero label, named from
`label_names[id]` (or `"Label <id>"`), colored from the LUT row §8.8's rule
selects (its alpha becomes the region's `visible`), and
addressed within your plugin's namespace by `contrib_id = str(label_id)`. So
committing twice with the same ids updates those rows rather than duplicating
them, exactly like `ContributeRois`. Requires `contribute_rois`.

The requirements from note 3 apply here and only here — the same item is
perfectly displayable with a fractional position and inline geometry, and only
becomes invalid when you ask for it to be compiled.

**The descriptor you send must name bytes that are still lent, and that is
the sender's accounting to get right.** The host does not
consult its retained draw list; it reads the offsets in the descriptor out of
the overlay segment, right now. A loan is released once every use the sender
*declared* has been disposed of, and a compile that repeats a push's
descriptor is a **second use of the same reservation** (§5.6, which works this
exact case in both arrival orders). So either:

- declare **`uses = 2`** on the item's `SegmentRef`s when you push, and the
  host releases only after both the update and the compile are disposed of.
  The Python SDK: `overlay.label_volume_item(..., uses=2)`, then
  `set_items([item])` and `compile_label_volume(overlay_id, item)` with the
  *same* item (`examples/vizi_hostsim/fixture/plugin.py` is the working
  pattern); or
- **reserve twice** — build the item once for the push and again, fresh, for
  the compile, each a one-use loan. This costs a second copy of the labels in
  the arena; it is conforming, and it is the route for an SDK whose builders
  cannot declare `uses` (note 7).

**The two routes cost different things, and for this plugin the second is
cheaper.** A `uses` declaration cannot be lowered (§5.6: it is a property
of the reservation, and nothing withdraws it), so a `uses = 2` push whose
compile never comes — the user drew again, or cleared — stays lent **until
the segment retires**. A tool pushes on every gesture and commits rarely, so
route 1 pins a label volume per uncommitted gesture in a 64 MiB arena.
Route 2 costs one extra copy per commit and pins nothing. The Python
implementation takes route 2, and `control_plane_protocol.md`
§8.8 says the same in one sentence — *`uses = 2` is for a push whose
compile is certain*; a push whose compile may never come declares one use
and the compile reserves again.

Declare one use and push, and the host releases after applying the update;
the compile then names bytes the SDK is free to reuse, and the host compiles
whatever it finds there, with no error anywhere — in *either* order, because
requests drain ahead of pushes.

Other limits worth knowing before you hit them:

- At most **500** nonzero labels per compile. The 255 cap above keeps you well
  inside it.
- The volume must lie entirely inside the dataset at its stated position.
- The `overlay_id` must be one the manifest declares, and the session must
  already hold an overlay-segment grant.
- The host refuses if there is no open dataset, or if `contribute_rois` is not
  declared.

**Retract the overlay after a successful commit.** Compiled masks are drawn by
the host as native annotations; leaving the plugin's label volume up draws the
same voxels twice, in two slightly different colors.

**Then find out what you just made.** `LabelVolumeCompiled` reports a count and
nothing else — not the `region_id`s of the regions it created — so the only way
to address them afterwards is to ask. Send `GetRois`, keep the records whose
`origin` is `"plugin:<your plugin id>"`, and remember the resulting
`(name, region_id)` list **in dropdown order**. Then `set_options` on the
`region` dropdown with those names, `set_visible(true)` on the measure row, and
a status line naming the count — as a numeral, which is what the driver looks
for; the sentence around it is yours.

Addressing by remembered index rather than by name matters: nothing makes
region names unique, and `seg.set_region` receives the dropdown's `index`
alongside its `value`.

**A commit keeps the accumulation.** The label map is not cleared by a
commit: a re-commit repeats the same ids and updates the same rows
(Acceptance 9), and an **Add** gesture after a commit takes label `n + 1` —
re-pushing labels `1..n` over the native masks until the next commit, which
is the double drawing the retraction above avoids only between commits.
Clearing on commit would make the next `replace_all` commit drop the earlier
masks; keeping is the lesser surprise.

## Measuring

`seg.measure` measures one committed region across time.

1. Resolve the selected dropdown index to a `region_id` through the list
   remembered at commit time. If the list is empty or the index is stale — the
   user deleted the region, or reopened the dataset — re-run `GetRois` and say
   so rather than guessing.
2. `ReadMaskVoxels{region_id}` → `MaskVoxels`, a descriptor into the session's
   `V` segment (host-produced; the same one `read_volume` replies land in)
   plus the mask's ZYX `origin`. The payload is a bbox-local `u8` membership
   array of `shape_zyx`; treat it as boolean. Release the deposit as always,
   with `ReleaseDeposit` naming the region. Reading a region that is not a
   mask fails with `INVALID_ARGUMENT`, and an unknown region with `NOT_FOUND`.
3. Take `T` and the channel from a fresh `ViewState`, not from the gesture
   that made the region. **The channel is `visible_channels[0]`, or `0` when
   that set is empty; do not read `ViewState.c`**, which is deprecated and
   always `0` on vizi's host. Measuring a
   segmentation drawn on one channel against another channel is the normal
   reason to want this at all, and it costs nothing to allow.
4. For each `t` in `[0, T)`: `ReadDataVolume{t, c, level: 0}` with a sub-box of
   exactly the mask's bounding box, then the mean of the voxels the mask
   selects. Report progress as you go.

   Fetch the mask **once** and the intensities **per timepoint**, in that
   order. Mask membership is static across time, so this is
   `O(regions + timepoints)`; asking for the mask per timepoint would be
   `O(regions × timepoints)` for identical answers.
5. Render mean-versus-`t` as a PNG and push it into the `plot` image widget.
   Any encoder; the bytes travel through the panel segment, which is separate
   from the overlay segment and is not the one an overlay reference may name.

A plot of a handful of points is enough — this demonstrates `push_image`, not
building a charting library. Label the axes.

## Budgets, threads and failure

- **The host allows 10 seconds for a command handler.** `seg.commit` is
  usually fast, `seg.measure` reads `T` volumes and is not. Both must validate,
  return `CommandDone{ok=true}` meaning *accepted*, and continue on a worker.
- **The panel is the channel for a job's outcome.** The host
  shows a successful `CommandDone.message` too, so `seg.commit` and
  `seg.measure` may say "started"; what they found lands after that reply and
  reaches the user only through the panel.
- **One job at a time.** A non-blocking lock shared by the gesture worker,
  commit and measure; a second request reports busy rather than overlapping —
  as `CommandDone{ok: false}` with the message, plus the same message in the
  `status` line, so it is visible with the panel open or closed. Two workers
  pushing different label volumes to the same overlay id is a last-write-wins
  race with no error anywhere.
- **Tool handlers never block.** Stated twice on purpose.
- A `host_error` reply is a normal outcome — no dataset, missing permission,
  out-of-bounds ROI, backpressure. Report it in the status line and keep the
  session alive.
- **`seg.reset` is not a job.** It is not under the lock: it clears the
  accumulation and retracts the label overlay at once, re-disables `commit`,
  and says so; a job in flight finishes on its own (a segmentation lands as
  a fresh label 1, a measurement is untouched) and its outcome may follow
  *Cleared* on the status line. **The committed regions are the host's**:
  a reset does not forget them, empty the dropdown or hide the measure row.
- **A refusal from a command** — `seg.pick` under `mip`, a commit with
  nothing to commit, a measure with nothing committed, busy — is
  `CommandDone{ok: false}` with the sentence, **and** the same sentence on
  the status line, as the busy rule says. **Progress lines are welcome
  anywhere** — the driver reads every value the status line takes, not the
  first change after a request.

## Acceptance

1. The plugin launches; the tool appears as a checkable Tools-menu entry, and
   the panel's **Select region…** button activates it (`ActivateTool`).
2. Dragging in a 2D view paints a marker trail that follows the cursor and
   disappears on release.
3. Releasing around a bright object paints a colored region over it, visible in
   both the 2D slice view and the 3D view.
4. Raising **Minimum size** past the object's voxel count makes the next run
   report no component instead of painting one.
5. Turning **Grow through Z** off produces a single-plane result on the plane
   that was displayed when the drag started.
6. **The result always includes voxels on the plane it was drawn on.** Scroll a
   few planes away from an object with something brighter above or below it,
   lasso it, and the tool either segments what is on *this* plane or reports
   that nothing here is above the threshold and names the plane that is. It
   never returns a mask that does not touch the drawn plane.
7. With **Z-MIP** on, the tool refuses and says so, and nothing changes.
8. **Add** accumulates a second region in a different color; **Replace** starts
   over.
9. **Commit** produces one native mask annotation per label, correctly named
   and colored, and the plugin's own overlay disappears. Re-committing updates
   the same rows rather than adding more.
10. **Measure** plots a curve with one point per timepoint.
11. A bare click reports "drag a closed loop" and changes nothing.
12. Escape deactivates the tool; the trail does not persist.
13. Reopening the panel shows the plugin's current state, not the manifest
    defaults.
14. A `cancel` mid-gesture — the host sends one on a camera move, a pane or
    dataset change, or deactivation — clears the trail and segments nothing.
15. With more than one channel visible, or zoomed to a coarser pyramid level,
    the tool refuses and says which.
16. A record the SDK marks unusable (note 9) contributes no point and
    reports nothing; the next gesture is live.
17. The panel's status line follows the tool's engagement (`activated` /
    `deactivated`), including when the user engages it from the Tools menu
    rather than the panel — on an SDK that surfaces the arrangement's
    arrival and withdrawal to the tool. On one that does not, the line
    follows the plugin's own `ActivateTool` reply and stays at *Ready* on a
    Tools-menu engagement; that is not a failure (*The UI state model*).
