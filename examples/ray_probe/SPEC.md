# Ray probe

A tool for pointing at things in a volume. Engaged by a key, a mouse chord
or a pane button, it turns a click in the 3D view into the brightest voxel
along the line of sight, marks it, plots the intensity along that line, and
lets the user keep a numbered list of such picks — undone or cleared by a
key without leaving the gesture — and hand the list to the host as native
ROIs. In a 2D view it does the same with a voxel or a projection column.

This document defines the example's **behaviour**. Each implementation
(`python/`, `go/`, `rust/`, `java/`) is written on its language's SDK and
follows it, and [`python/hostsim.py`](python/hostsim.py) runs this
document's scenarios against **any** of them. The protocol is `1.0`; where
this document and `docs/spec/` disagree, `docs/spec/` wins.

## What this example demonstrates

- **Reading a 3D record and resolving its own anchor.** A 3D record carries
  a ray, and may arrive with or without the host's pick (`RAY` or
  `RAY_AND_PICKED`, `doorbell_E_input.md` §3). This example keeps its own
  policy — the brightest voxel along the segment the host reports, refined
  at full resolution — and reads the ray from either kind, on any phase;
  the host's `picked` is ignored, not compared.
- **The modal binding vocabulary.** Triggers a plugin *requests* and the
  host *assigns* — a toggling key, a momentary chord, a pane button with an
  icon — and actions fired *into* an open gesture with `E.action`, with the
  subset the host honoured reported back in the `ToolBinding`.
- **The pyramid.** A 3D pane shows whatever level the renderer chose, every
  coordinate on its records is in that level's index space, and
  `control_plane_protocol.md` §8.11 gives an exact affine map between
  levels. A pick found at level 2 and refined at level 0 lands on the voxel
  the user meant.
- **Two panels** (so the `P` segment lookup is by subject rather than by
  "the" panel), the `open_generation` guard on a read that a record
  predates, and `cancel_on_camera_move = false`, which a ray tool is
  entitled to because every ray record carries its own camera.

Read [`docs/spec/doorbell_E_input.md`](../../docs/spec/doorbell_E_input.md) §1
and §3 and [`control_plane_protocol.md`](../../docs/spec/control_plane_protocol.md)
§8.10 and §8.11 first — this plugin lives on those four sections.

### Protocol facts this example relies on

1. **The host's pick is optional.** A 3D record is `RAY` or
   `RAY_AND_PICKED`; which one is the host's policy and is not on the wire.
   This plugin's policy is its own (§"The pick"), so a `RAY_AND_PICKED`
   record is treated as its `RAY` half.
2. **The pane's level is the renderer's choice and not on any message you
   can poll.** `ViewState.level` is the 3D volume's *requested* level and
   may differ from what a record names; the record's `level` is the only
   truth for its coordinates (`doorbell_E_input.md` §3), and the epoch's
   `Geometry.levels` is the table it indexes. A record at a level the epoch
   does not declare reaches you with `usable = False` (the SDK has already
   reported site 27); refuse it and say so.
3. **A marker and a line have no timepoint.** `OverlayMarker` and
   `OverlayLineSet` carry no `t` (only `OverlayLabelVolume` does), so a
   pick's marker shows at every timepoint. That is right for this tool — a
   pick is a place — and the pick's `t` lives in its record, its label and
   its ROI, not in the marker.
4. **A `text` item is a label at a point, drawn by the 3D pane only.**
   `control_plane_protocol.md` §8.5 lists the five geometry arms and where
   each is drawn: `anchor_zyx` is a point in `transform.frame` (a level-0
   voxel under `"voxel"`, as a marker's positions are), the rest of the
   transform does not apply, and `size_px` is screen pixels whatever
   `scale_mode` says (0: the host's 12). The Python SDK builds one with
   `text_item(item_id, text, anchor_zyx, *, size_px, color_rgba, frame)`.
   So a pick is labelled with its number (§5).
5. **A line is drawn in the 2D panes too**: projected into every slice and
   dashed out of its own, never sectioned; a pixel is a voxel there, for
   `thickness` and `size_px` alike; the 3D pane's `thickness` is pixels. A
   `text` or a `mesh` is 3D-only. So the column aim draws in the slice it
   was drawn in and a ray's aim shows dashed in every 2D slice it crosses.
6. **The activation hook is handed its binding**: `activated(self, ctx,
   binding)` receives the `ToolBinding` just installed — the honoured
   subset of the triggers, every action with the key the host kept — and
   `ctx.arrangement(id)` finds it by manifest id later. §8.10 states the
   rule: *the binding is in the tool's hands at the moment it is engaged.*
   So the keys are said at activation (§1).
7. **Spacing may be unknown.** `Geometry.spacing_unit == ""` with
   `SPACING_PROVENANCE_DISPLAY_FALLBACK` means the host does not know how
   big a voxel is; the 1.0 it carries is not a measurement (§8.11). Every
   physical quantity this plugin shows — distance along the ray, a position
   in µm — is shown only when the unit is known, and is otherwise in voxels
   and says so.
8. **An axis that has shrunk to one sample keeps the factor of its last
   fold**: `Geometry.levels` is `2^min(L, ⌊log₂n₀⌋)` per axis, so the maps
   apply unchanged everywhere and there is no special case; §"Levels" says
   so.

## What a click transmits

A record's `position` has a shape per pane (`doorbell_E_input.md` §3), and
this tool accepts every shape it can turn into a set of voxels:

| pane | kind | what the probe searches |
|---|---|---|
| 3D | `RAY` (or the ray half of `RAY_AND_PICKED`) | the voxels of the record's level that the **segment** from `entry` to `far` passes through, where `entry = camera + entry_frac × (far − camera)` and `far` is the exit of the clip box |
| 2D slice, montage tile | `VOXEL` | that one voxel |
| 2D projection | `COLUMN_*` | the column: the two fixed coordinates rounded, the third over the reported range |
| 3D, a host that picks | `PICKED` alone | that one voxel — the kind guarantees a real voxel on the ray inside the clip box, so it is a `VOXEL` for this tool |
| any | `MISS` set | **nothing** — the coordinate is real but not a relevant place; a tool that samples the volume skips it (§3, "Pointing outside the data") |
| any | `NONE` | nothing — the host had no coordinate |

Every coordinate is in voxel index space of the record's `level`, and this
document never normalises between levels except by the one exact map §8.11
gives (§"Levels").

## Manifest

```json
{
  "manifest_version": 1,
  "id": "org.example.ray_probe",
  "name": "Ray probe",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["<your launcher>"] },
  "permissions": ["data_access", "contribute_rois"],
  "contributions": {
    "tools": [
      {
        "id": "probe.point",
        "name": "Probe",
        "views": ["2d", "3d"],
        "mode": "gesture",
        "cancel_on_camera_move": false,
        "triggers": [
          { "key": "Shift+P", "hold": "toggle" },
          { "button": "middle" }
        ],
        "actions": [
          { "id": "undo",  "name": "Undo pick",   "key": "U", "pane": { "icon": "icons/undo.svg",  "tooltip": "Undo the last pick" } },
          { "id": "clear", "name": "Clear picks", "key": "X", "pane": { "icon": "icons/clear.svg", "tooltip": "Clear every pick" } }
        ],
        "pane": { "icon": "icons/probe.svg", "tooltip": "Probe: the brightest voxel under the pointer", "hold": "toggle" }
      }
    ],
    "overlays": [
      { "id": "probe.picks", "name": "Picks", "segment_mib": 4 },
      { "id": "probe.aim",   "name": "Aim",   "segment_mib": 4 }
    ],
    "commands": [
      { "id": "probe.set_radius", "title": "Set ROI radius", "menu": null },
      { "id": "probe.set_refine", "title": "Refine at level 0", "menu": null },
      { "id": "probe.undo",       "title": "Undo pick", "menu": null },
      { "id": "probe.clear",      "title": "Clear picks", "menu": null },
      { "id": "probe.contribute", "title": "Contribute picks as ROIs", "menu": "tools" }
    ],
    "panels": [
      {
        "id": "probe",
        "title": "Ray probe",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "label", "id": "status", "text": "Ready" },
            { "kind": "row", "id": "rradius", "children": [
              { "kind": "label", "id": "lradius", "text": "ROI radius (voxels)" },
              { "kind": "line_edit", "id": "radius", "command": "probe.set_radius", "value": "3" }
            ] },
            { "kind": "checkbox", "id": "refine", "command": "probe.set_refine", "text": "Refine at level 0", "value": true },
            { "kind": "row", "id": "rbuttons", "children": [
              { "kind": "button", "id": "undo", "command": "probe.undo", "text": "Undo" },
              { "kind": "button", "id": "clear", "command": "probe.clear", "text": "Clear" },
              { "kind": "button", "id": "contribute", "command": "probe.contribute", "text": "Contribute" }
            ] },
            { "kind": "label", "id": "count", "text": "No picks" }
          ]
        }
      },
      {
        "id": "profile",
        "title": "Ray profile",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "image", "id": "plot" },
            { "kind": "label", "id": "pick", "text": "No pick yet" }
          ]
        }
      }
    ]
  }
}
```

Field names, widget kinds and the tool block are
[`docs/spec/manifest_schema.md`](../../docs/spec/manifest_schema.md) §3.6's
(`manifest_version: 1` is what admits `triggers`, `actions`, `pane`, `mode`
and `cancel_on_camera_move`; under version 0 they are rejected). What is
and `cancel_on_camera_move`; under version 0 they are rejected). What is
pinned, and why:

- **Two triggers of the two kinds.** A key that **toggles** (the sticky pane
  button by keyboard) and a mouse chord, which is **momentary** by rule — a
  chord is down or up and nothing else, so `hold` may not say otherwise on
  it (§3.6.1). `middle` is free in both views by construction (§8.10's
  reserved table); `Shift+P` is a request, and the host may refuse it if a
  global key already fires there. (vizi binds `P`, with Space, to
  Play/Pause, and binds no `Shift+P`.) **A refused trigger is not a refused tool**: the
  `ToolBinding` lists the assigned subset, and the tool still engages from
  the menu, the pane button and whatever was honoured.
- **Two actions, each with a key and a pane button.** `U` and `X` are live
  only while the tool is engaged, so another plugin may use them too; the
  binding reports `""` for a key the host declined. An action reaches you as
  `phase = FIRE` with `action` = its 1-based index here — `undo` is 1,
  `clear` is 2 — carrying the pointer's current sample, and **it lands
  inside whatever gesture is open without disturbing it** (§8.10).
- **A pane button for the tool and one per action.** The icons are
  checked when the manifest is read — present, `.svg`, small, really SVG —
  and a tool installs whole or not at all (§3.6.3). Ship three small SVGs
  under `icons/`; the driver launches your manifest and the host
  would refuse the plugin without them.
- **`mode: "gesture"`** — the probe aims during the drag and picks on
  release; a `fire` tool would have no aim. **`cancel_on_camera_move:
  false`** — a ray record carries its own camera, so a camera move mid-drag
  invalidates nothing this tool holds (§8.10: "a binding that turns it off
  forfeits nothing"). Both are the non-default and are pinned for that
  reason.
- **`views: ["2d", "3d"]`.** The 3D pane is the point; the 2D shapes cost
  nothing extra and are the same search over a smaller set.
- **Two overlays, not one.** An `OverlayUpdate` is a **full replacement**
  for its overlay id, on the live lane as on the reliable one. The aim line
  is pushed at event rate and cleared at release; the picks are pushed once
  per change and must survive every aim. One overlay would make each aim
  re-send the picks or erase them; two keep the lanes apart.
- **Two panels.** The profile is a plot the user wants beside the view; the
  controls are a strip they dock elsewhere. The plugin therefore holds two
  `P` segments, so a panel image goes to the arena whose
  `subject` is that panel's id and not "the" arena
  (`shared_memory_protocol.md` §3).
- **`probe.contribute` is also in the Tools menu** (`menu: "tools"`), so
  the list can be handed over with the panel closed; the other four are
  panel-only (`menu: null`).
- **Widgets:** the `probe` panel's `status` is the tool's voice, `count` is
  the pick tally, `radius` is the ellipsoid radius in level-0 voxels for
  §"Contributing" and `refine` is §"Refinement"'s switch; the `profile`
  panel's `plot` is the target of `push_image` and `pick` describes the
  last pick in words. Every value the panel shows is the plugin's model,
  re-pushed whole on `PanelOpened` (the host rebuilds from the manifest on
  every open and persists nothing), for either panel independently.

## The UI state model

State the plugin owns:

| state | initial | changed by |
|---|---|---|
| `picks` | empty list | a pick (append), undo (pop), clear (empty) |
| `radius` | 3 | `probe.set_radius` (a positive finite number of level-0 voxels; anything else is refused with `CommandDone{ok=false}` and the stored value stands) |
| `refine` | true | `probe.set_refine` |
| `status`, `count`, `pick` label, `plot` image | `"Ready"`, `"No picks"`, `"No pick yet"`, none | as below |
| the job lock | free | one pick at a time (§"Budgets") |

**A pick** is: its number, its position as a level-0 lattice coordinate
(three floats — integral after refinement, a block centre without it, see
§"Levels"), its value, the `t` it was taken at, the physical time of that
`t` when a time mapping is arranged, the channel it was read from, the
record's level, and whether it was refined.

**Numbers are positions in the list.** The first pick is 1; undo pops the
last and the next pick takes its number again; clear empties the list. A
`contrib_id` is derived from the number (§"Contributing"), so re-contributing
after an undo replaces rather than accumulates.

The `count` label says the tally as a numeral: `"No picks"`, then `"1
pick"`, `"2 picks"` — the numeral and the noun are pinned, the driver reads
them.

## The tool

### 1. Engagement

`activated`: set `status` to say the probe is active, what to do, and
**which triggers and action keys the host honoured**, built from the
`ToolBinding` the hook is handed rather than from the manifest — the
honoured set may be smaller, and `""` names an action with no key
(*"Probe active — click in a view — Shift+P toggles, middle button holds; U
undoes, X clears"* is the Python implementation's; the driver asserts only
that the status changed to something containing `active`, and reports which
keys it named; it also accepts the keys being said on the first record).
`deactivated`: `status` back to `"Ready"`; an open gesture has already been
cancelled by the SDK.

A momentary trigger displacing this tool mid-session tells it `deactivated`
then `activated` again on release (§8.10) — nothing to do beyond the two
status lines, and no state is lost: the picks are the plugin's.

### 2. The aim

On every `PRESS` and `DRAG` with a `RAY` position that is not a `MISS`,
publish the segment as the **aim**: a `line_item` on `probe.aim` with two
points, `entry` and `far`, converted to level-0 lattice coordinates
(§"Levels"), topology segments, via the **live lane**
(`set_items_live`, passing the event as the pane so the host draws it only
under the epoch it was computed for). On a `COLUMN_*` the aim is the
column's segment; on a `VOXEL` or a `MISS` there is no aim and nothing is
published. Every live publish supersedes the last; the lane coalesces.

A column's aim runs from its first included centre to its last (`lo` and
`hi − 1` on the free axis, the two fixed coordinates as reported), in
level-0 coordinates like everything published.

On `RELEASE` and on `cancel` — record or `Cancellation` alike — **clear**
`probe.aim` with a reliable push: an `OverlayRemove`, or an `OverlayUpdate`
with no items — §8.5 makes the update a full replacement, so either
retracts it, and the driver accepts either. The aim is feedback for a
gesture in progress and must not outlive it.

A live publish that fails (the lane refused, the arena full) is said once
per gesture on `status` and does not stop the pick: the aim is a courtesy,
the pick is the job.

### 3. The pick, on release

A `RELEASE` ends the gesture and starts **one** pick from that record's
position. The order of refusals, each a `status` sentence in your words and
no pick (the driver asserts the tally did not move and the status changed):

1. `usable == False` — the record's context could not be reconciled with
   the arranged state; do not act on its coordinate.
2. no position (`NONE`), or `MISS` set — *"Nothing under the pointer"* is
   the Python implementation's sentence.
3. no visible channel: `pane.visible_channels(ev.channels)` is empty — or
   there is no pane state to ask (`ctx.pane(ev)` is `None`: an epoch this
   plugin has moved past), which is the same refusal.
4. busy: a pick is still in flight (§"Budgets").

Otherwise hand the record to a worker and let the handler return: the
search reads the volume, and **a tool handler never blocks on the host**
(`docs/contributions/tools.md`). Everything below runs on the worker.

**The channel** is the lowest index in the visible set. A composite shows
several; the probe reports one, says which (the `pick` label carries `c=`),
and the lowest is the one every implementation will agree on.

**The set `S`** is the table in §"What a click transmits", at the record's
level `L`, ordered:

- `VOXEL`: `S = {(⌊z+0.5⌋, ⌊y+0.5⌋, ⌊x+0.5⌋)}`.
- `COLUMN_YX` (and the other two by symmetry): the two fixed coordinates
  rounded the same way; the free axis over `[lo, hi)` as the SDK delivers
  the range (`doorbell_E_input.md` §3: an integer pair with half a pixel of
  slack behind it), ascending.
- `RAY`: every voxel of level `L` whose **closed** cube the **closed**
  segment from `entry` to `far` meets — the membership rule — **in order
  along the ray from the camera** by the parameter at which each cube is
  first met; cells first met at the same parameter (the cells around an
  edge or a corner the segment passes through) may stand in any order
  among themselves. A voxel `(k, j, i)` occupies `[k − 0.5, k + 0.5] × …` —
  coordinates are voxel *centres*, as every position on the wire is. So a
  segment that merely touches a face, an edge or a corner counts every
  cell touching it: an endpoint on a boundary belongs to both cells there,
  and a simultaneous crossing of two or three boundaries yields every cell
  reached by stepping any subset of the tied axes (four cells at an edge,
  eight at a corner). A voxel traversal (Amanatides–Woo) produces this
  when its tie step is written for it; one that steps one axis at a time
  at a tie omits cells the rule includes. Two more things a traversal must
  get right: **recompute the crossing
  parameter from the cell at each step and compare within a small
  tolerance** (the Python implementation uses 10⁻⁹ of the segment) — accumulating
  `t += Δt` drifts by an ulp and loses ties that are exact in real
  arithmetic — and **an endpoint on several boundaries at once belongs to
  every cell around it** (eight at a corner), not to one per axis. The
  driver's rays avoid ties by construction; the rule is still the rule,
  and `python/selftest.py` checks it against a slab test.

Then clip `S` to **the clip box at level `L`** — the epoch's `crop`,
converted to level `L` first and to included centres second
(`ceil(lo) ..= floor(hi)`, §8.11; `crop.at_level(L)` in the Python SDK) —
and to the level's shape. The host already clipped the ray to the same box,
so this changes nothing on an exact record; it is the plugin refusing to
read a voxel the record did not name. An empty `S` after clipping is
refusal 2.

**The read** is one `read_volume(t, c, L, roi=AABB(S))`, the axis-aligned
bounding box of `S` at level `L` (min inclusive, max exclusive), never the
whole level. If the box holds more than **2²⁶ voxels** refuse instead (*"Too
large"*): a diagonal ray through a large volume at level 0 is the pane's
resolution, not the tool's problem, and the cap is what keeps one click
from a gigabyte.

**The generation guard.** The reply carries the host's `open_generation`;
its low 32 bits must equal the record's `gen`. If they differ, the dataset
changed between the click and the read: discard the data, refuse (*"The
dataset changed"*), and make no pick. This is the check
`doorbell_E_input.md` §1's `gen` exists for, and the driver provokes it.

**The coarse pick** is the voxel of `S` with the greatest value, ties to
the **first in the order above** (nearest the camera, or lowest along a
column). `NaN` is skipped; `±inf` compare as numbers. If every value is
`NaN`, refusal 2's sentence.

### 4. Refinement, and the level of a pick

If `L > 0` and `refine` is on, the coarse voxel `j` (per axis) covers the
level-0 block `[j·s, j·s + s)` where `s` is that axis's **sampling factor**
from `Geometry.levels[L]` — the factor, not a shape ratio (§8.11). Read the
block (one `read_volume(t, c, 0, roi=block ∩ extent)`) and take its maximum,
ties to the smallest `z`, then `y`, then `x` (peak-ROI's rule). **The pick
is that level-0 voxel and that value.**

If `L == 0`, the coarse pick is the pick. If `L > 0` and `refine` is off,
the pick is the coarse voxel's **centre** in level-0 coordinates —
`(j + 0.5)·s − 0.5` per axis, generally fractional — and its coarse value.

A feature narrower than a block can be missed at a coarse level: the coarse
maximum lands on the block whose *average* is highest, and refinement
searches only that block. That is the resolution the pane is showing, not
a defect, and the driver's blob is wide enough that it does not arise.

### 5. Publishing a pick

In this order:

1. Append the pick; the `count` label.
2. `probe.picks`: **one** `marker_item` whose positions are every pick's
   level-0 coordinate in list order, `shape = "disc"`, `size_px = 8`,
   colour `(255, 204, 0, 255)`, `frame = "voxel"`; **and one `text` item
   per pick**, `item_id = "label-<N>"`, text the numeral `N`, anchored at
   the pick's level-0 coordinate, `size_px = 0` (the host's default), the
   same colour (fact 4 above). A reliable `set_items`, replacing the item
   set: `1 + N` items for `N` picks, none for none.
3. The `profile` panel: `push_image("plot", png)` where the PNG plots the
   values of `S` in traversal order against distance from `entry` — in
   the spacing unit when known (`Σ √((Δz·uZ)² + (Δy·uY)² + (Δx·uX)²)`
   between consecutive voxel centres), else in level-`L` voxels — with the
   coarse pick marked; then the `pick` label:
   *"Pick N: (z, y, x) = …, value …, c=…, t=T"* with `N` and `T` as
   numerals and, **when a time mapping is arranged**, the physical time
   appended as `(<seconds:g> s)` — `"t=2 (2.5 s)"` — read from the pane
   state's time table. Position in level-0 voxels, and in the spacing unit
   after it when known.

   **The profile panel may not be open.** Its `P` segment is granted only
   when the person opens it, and §8.4 says an SDK SHOULD hold
   an image pushed before then and send it on `PanelOpened`. **A failure to
   update the profile panel is not the pick failing**: report it on its own (a log line), never as the
   pick's outcome on `status`, and go on to step 4.
4. `status`: a sentence naming the pick's number as a numeral and its
   value; the rest is yours.

Undo and clear re-push `probe.picks` (an empty item list clears it),
`count`, and say what happened on `status`.

### 6. Actions

`undo` (index 1) and `clear` (index 2) arrive as `FIRE` records naming
them; the same two arrive as `probe.undo` and `probe.clear` from the panel
buttons. Their effect is identical, and **an action inside an open gesture
touches the gesture not at all**: the aim continues, the release still
picks. An action while a pick is in flight applies after it completes (the
job lock serialises them) — never to a pick that has not been appended yet.

A `FIRE` naming an index this manifest does not declare (`ctx.action(ev)`
is `None` for a `FIRE`) is ignored; it is a newer host naming something this
plugin does not declare, not a fault.

## Levels

Two conversions, both exact, both from `control_plane_protocol.md` §8.11:

- **Level-`L` coordinate `c` → level-0 lattice coordinate:**
  `(c + 0.5)·s − 0.5`, where `s` is the sampling factor of level `L` on that
  axis. The aim line, a coarse pick's centre, and every overlay position
  this plugin publishes go through it — `frame = "voxel"` is the level-0
  lattice.
- **Level-0 edge `e` → level-`L` edge:** `(e + 0.5)/s − 0.5`, then included
  centres `ceil(lo) ..= floor(hi)`. The crop goes through it, in that order.

`s` comes from the epoch's table, never from a shape ratio and never assumed
to be `2^L` — vizi's is, and a host's may not be. A level the table does not
to be `2^L` — vizi's is, and a host's may not be. A level the table does not
declare never reaches the search (fact 2 above).

**An axis that has shrunk to one sample keeps the factor of its last
fold** (fact 8 above). `Geometry.levels` carries `2^min(L, ⌊log₂n₀⌋)` per
axis, the lone sample covers exactly what the factor says, and **the two
maps and the crop rule apply unchanged with no special case** — an
implementation that reads the declared factor is right everywhere, and one
that assumes `2^L` is wrong on a thin axis. The driver's dataset
(`32 × 48 × 64` at factors 1, 2, 4) has no such axis.

## Contributing

`probe.contribute` sends every pick as a native ROI: one `ContributeRois`
with `replace_all = true` and, per pick in list order, a `RoiContribution`
with `contrib_id = "pick-<N>"`, `name = "Pick N"`, `style.color =
"#ffcc00"`, and an **ellipsoid** of radius `radius` level-0 voxels on every
axis, centred on the pick's level-0 coordinate, **timeless** (no time
fields). The ellipsoid's control structure is
[`../peak_roi/SPEC.md`](../peak_roi/SPEC.md) §"Building the ellipsoid
`Roi`", verbatim; equal voxel radii are a marker of a place, not a
measurement, which is why they are not scaled by spacing.

`replace_all` is what makes the host's set mirror the list: a pick undone
here, and so absent from the batch, is removed there. With no picks, contribute sends
the empty batch with `replace_all = true` — which clears this plugin's
prior contributions — and says so. `status` afterwards names the count as
a numeral (*"Contributed 3 ROIs"*, *"Cleared the contributed ROIs"*); a
host `Error` is reported on `status` in full.

Requires `contribute_rois`; the command fails cleanly without it.

## Budgets, threads and failure

- **One pick at a time.** A release arriving while a pick is in flight is
  refusal 4; the in-flight pick completes. Actions and commands that touch
  the list wait for the lock rather than refuse.
- **Nothing blocks the listener.** `press`/`drag`/`release` do arithmetic
  and enqueue; reads, PNG encoding and every host request run on a worker.
  The live publish is the one host-bound thing a handler does directly, and
  it is a queued push.
- **A read that fails** (`HostError`) is the pick's failure: `status` says
  so with the host's message; nothing is appended.
- **A record the SDK marked unusable** is refused, never acted on, and the
  SDK has already told the host (site 27).
- **Panel patches to a closed panel go nowhere** and are not errors; the
  model is re-pushed on `PanelOpened`, per panel.
- The reads are bounded by the cap above; the profile PNG is small (one
  line, a few hundred points at most — decimate a long `S` to 1024 samples
  for the plot, never for the search).

## Acceptance

The driver, [`python/hostsim.py`](python/hostsim.py), runs these against any
implementation (`--plugin-dir`). Its dataset has a pyramid of three levels
(factors 1, 2, 4), an anisotropic spacing in µm, a time mapping, and one
smooth bright blob whose peak it knows.

1. Handshake; the manifest declares one tool with two triggers, two actions
   with keys and pane buttons, a tool pane button, two overlays, two panels
   and both permissions; the icons exist and are SVG.
2. Arrangement with both triggers and both action keys honoured: `status`
   says the tool is active (at activation, or on the first record — the
   driver accepts either and says which). Arrangement with the key trigger
   refused: the tool still picks.
3. A 3D ray at level 0 through the blob along an axis: the pick is the
   blob's peak voxel; `probe.picks` carries exactly that position as a
   marker and a `text` item `"1"` anchored there;
   `count` is `1 pick`; `status` names `1` and the value. **The `profile`
   panel is still closed during this pick** (its `P` segment is granted when
   it opens, as vizi does). Nothing is refused, and `status` reports the
   pick. The driver then opens it: the panel receives a PNG on `plot`, and
   its `pick` label names `1`, the
   coordinate, `c=`, `t=` and the physical time `(<s> s)`.
4. The same ray at level 2 with `refine` on: exactly two reads — one at
   level 2 with an ROI no larger than the segment's box, one at level 0
   with an ROI no larger than one block — and the pick is the exact peak.
   With `refine` off: one read; the pick is the coarse voxel's centre.
5. An oblique 3D ray through the blob: the pick agrees with the driver's
   own traversal (dense sampling, independent of the plugin's).
6. A drag: `probe.aim` carries a two-point line on the live lane after
   each record, in level-0 coordinates matching the record's segment; it
   is cleared after release.
7. A `MISS` ray: no pick, `status` changed; no aim.
8. A 2D `VOXEL`: the pick is that voxel. A 2D `COLUMN_YX`: the pick is the
   column's maximum.
9. `undo` as a `FIRE` mid-drag: the last pick is gone (`probe.picks`
   positions shrink by one, its label with them), the gesture continues
   and its release picks.
   `clear` as a `FIRE`: no picks, `count` is `No picks`.
10. `probe.undo` and `probe.clear` from the panel: the same.
11. `probe.set_radius` to `2`, then `probe.contribute`: one
    `ContributeRois`, `replace_all = true`, one ellipsoid per pick named
    `Pick N` with `contrib_id = pick-N`, centred on the picks, radius 2;
    `status` names the count. A non-numeric radius is refused and the
    stored value stands.
12. The dataset's generation changes between a record and its read: no
    pick; `status` says the dataset changed.
13. A record with no visible channel: refused. A record at an undeclared
    level: refused, and the SDK reported `unusable_context`.
14. A second release while a pick is in flight (the driver delays its
    read): refused with a status; the first pick lands.
15. Teardown: a clean `ShutdownReport` and end of stream.

Wording asserted by the driver is only what this document quotes in
backticks or as *pinned*: the `count` forms, the numerals named in §5, the
substring `active`, and `(<s> s)` after `t=`. **A numeral is compared as a
number**: the driver parses every number on the line and looks for the
value within one part in 10⁶, so `2901`, `2901.0` and `2.901e3` all name
2901, and the seconds inside `(… s)` likewise — an implementation prints
numbers its own way. Everything else is the implementation's.
