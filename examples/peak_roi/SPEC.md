# Peak-finder ROI — plugin specification

A plugin that blurs the active volume, finds the brightest voxel, and drops an
ellipsoidal ROI on it. Small enough to read in a sitting; wide enough to
touch the control plane, the data plane, the doorbell, and the ROI vocabulary.

This document specifies **behaviour**, not implementation. The
implementations beside it (`python/`, `go/`, `rust/`, `java/`) each build the
same plugin on their language's SDK, and
[`python/hostsim.py`](python/hostsim.py) runs this document's scenarios
against **any** of them. Where this document and `docs/spec/` disagree,
`docs/spec/` wins.

## What this example demonstrates

- **A declared panel bound to commands.** Text fields, a dropdown and a
  button, each firing a `menu: null` command, with the plugin owning the UI
  state and patching a status label.
- **A plugin-initiated read on the data plane.** `GetViewState`, then
  `ReadDataVolume` into the session's `V` segment, and `ReleaseDeposit` once
  the bytes are copied out.
- **A memory grant in two halves.** `M` on the doorbell carries the
  descriptor and the `segment_id`; `InstallSegment` on the control plane says
  what the segment's regions mean, and may ride the reply that first names
  the segment. The two arrive in either order; nothing that names a segment
  may be acted on until both have landed, and the plugin acknowledges the
  pair with a `GrantAck` carrying the preamble it read back. Read
  [`docs/spec/doorbell_protocol.md`](../../docs/spec/doorbell_protocol.md),
  [`doorbell_M_memory.md`](../../docs/spec/doorbell_M_memory.md),
  [`shared_memory_protocol.md`](../../docs/spec/shared_memory_protocol.md) §3
  and [`control_plane_protocol.md`](../../docs/spec/control_plane_protocol.md)
  §8.12 for the contract. Each SDK does this for the author.
- **A long job behind a short command.** `CommandDone` accepts the job; a
  worker runs it and reports through panel patches.
- **An ROI contribution with a per-frame pose.** `ContributeRois` with an
  ellipsoid whose structure is given in "Building the ellipsoid `Roi`" below.

Plugin-initiated requests correlate by an id the plugin chooses; the host
echoes it on a one-way reply, and such replies sit outside the host's
single-in-flight slot (see the `PluginMsg` comments in
`proto/vizi_plugin.proto`).

## What the plugin does

The user opens a panel, types a per-axis extent in voxels, picks a timepoint
mode, and presses a button. The plugin reads the volume, blurs it with a
Gaussian whose σ is half the extent, finds the brightest voxel, and contributes
an axis-aligned ellipsoidal ROI centred there with semi-axes equal to the
extent.

**Extents are in voxels, not microns.** The protocol publishes a physical voxel
spacing — `Geometry.spacing` with a unit and a provenance,
`control_plane_protocol.md` §8.11 — but only inside `EpochChange`, which is
arranged **only for sessions that declare a tool** (§8.7); `ViewState`, the
one thing a command-only plugin can ask, has no spacing field. So this plugin
cannot convert, and making the user state a per-axis extent is the honest
version: on anisotropic data they can enter `(2, 10, 10)` for a
roughly-isotropic physical blob, and that choice is visible rather than a
silent wrong guess.

## Manifest

```json
{
  "manifest_version": 1,
  "id": "org.example.peak_roi",
  "name": "Peak ROI",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["<your launcher>", "<your entry point>"] },
  "permissions": ["data_access", "contribute_rois"],
  "contributions": {
    "commands": [
      { "id": "peak.set_z",    "title": "Set Z extent",        "menu": null },
      { "id": "peak.set_y",    "title": "Set Y extent",        "menu": null },
      { "id": "peak.set_x",    "title": "Set X extent",        "menu": null },
      { "id": "peak.set_mode", "title": "Set timepoint mode",  "menu": null },
      { "id": "peak.run",      "title": "Find peak",           "menu": null }
    ],
    "panels": [
      {
        "id": "peak",
        "title": "Peak ROI",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "row", "id": "rz", "children": [
              { "kind": "label", "id": "lz", "text": "Z extent (voxels)" },
              { "kind": "line_edit", "id": "ez", "command": "peak.set_z", "value": "4" }
            ]},
            { "kind": "row", "id": "ry", "children": [
              { "kind": "label", "id": "ly", "text": "Y extent (voxels)" },
              { "kind": "line_edit", "id": "ey", "command": "peak.set_y", "value": "8" }
            ]},
            { "kind": "row", "id": "rx", "children": [
              { "kind": "label", "id": "lx", "text": "X extent (voxels)" },
              { "kind": "line_edit", "id": "ex", "command": "peak.set_x", "value": "8" }
            ]},
            { "kind": "dropdown",  "id": "mode", "command": "peak.set_mode",
              "options": ["Current timepoint", "All timepoints"],
              "value": "Current timepoint" },
            { "kind": "button",    "id": "go", "text": "Find peak", "command": "peak.run" },
            { "kind": "label",     "id": "status", "text": "Ready" }
          ]
        }
      }
    ]
  }
}
```

`docs/manifest.md` is authoritative over this sketch if the two disagree.

Two things here are load-bearing:

**`"menu": null` on every command.** Without it each command gets its own
Tools-menu entry, so a five-command plugin puts six entries in the menu and
four of them are broken by construction: a bare menu invocation sends no args
(`docs/contributions/commands.md`), so `peak.set_z` invoked from the menu can
only fail. These commands exist to be bound to widgets, not to be user-facing
actions. The panel's own "Show Peak ROI" entry is the plugin's front door.

`menu` accepts `null` as well as `"tools"`, for commands and tools alike
(`docs/manifest.md`, `docs/spec/manifest_schema.md`).

**Labels for the extent fields.** `line_edit` takes a `placeholder`, but that
disappears as soon as the field has a value, and these fields ship with
defaults. Three bare boxes reading `4`, `8`, `8` tell the user nothing about
which axis is which. Pair each with a `label` inside a `row`.

## The UI state model

**Each widget's invocation carries only that widget's own value.** A button
sends nothing at all. There is no way to ask the host what another widget
currently holds.

So the plugin owns the model: each `set_*` command records its value into
plugin state, and `peak.run` uses what has been recorded. Initialise state
from the same defaults the manifest declares, because a widget the user never
touches never invokes anything.

Per `docs/contributions/commands.md`, a slider sends its number as `value`, a
dropdown sends `index` and `value`, a checkbox its bool, and a `line_edit`
its text as `value`.

**An extent is a finite, strictly positive number of voxels.** A `set_*`
command given anything else — unparseable text, zero, a negative or
non-finite value — fails the invocation (`CommandDone{ok=false}`, naming the
value), leaves the stored extent unchanged, and says so on the `status`
label; the wording is yours. Zero is refused because a zero semi-axis is a
singular ellipsoid the host would misread rather than reject.

## Behaviour of `peak.run`

### 1. Inspect the view

`GetViewState` → `ViewState`. Take `t`, **the channel** (below), `shape_tczyx`
and `open_generation`. Fail cleanly if there is no dataset — treat both a
`host_error` reply and a `ViewState` whose `shape_tczyx` is absent or contains
a zero as "no dataset", since which one arrives is not specified.

**The channel is `visible_channels[0]`, or `0` when that set is empty.**
`visible_channels` is every channel enabled for display, ascending.
**Do not read `ViewState.c`**: it is deprecated and always `0`, because vizi has
no active-channel control (the proto says so). The
plugin searches one channel, the first one shown, and says which in the status
line; searching a composite would be a different tool. In all-timepoints mode
the channel is fixed at run start for every frame; it does not follow the user
mid-run.

Ignore `has_crop`: the crop is a render clip, reads return uncropped data, and
this plugin searches the whole volume.

**Read at level 0, not at `ViewState.level`.** `ReadDataVolume` takes indices
in the requested level's coordinates, but `roi.proto` describes ROI positions
as voxel indices with no pyramid level attached. The per-level
factor is published (`control_plane_protocol.md` §8.11's `Geometry.levels`,
arranged by `EpochChange`) and the Python SDK's `geometry.EdgeBox.at_level`
converts a box exactly — but a peak *index* found at level *n* still names a
block of level-0 voxels, not one, and level shapes alone do not determine the
factor for every downsampling policy. Reading at level 0 sidesteps the
question entirely and is correct regardless of what the viewer is displaying.

### 2. Read the volume

`ReadDataVolume{t, c, level: 0}` → `DataVolume`, carrying a `SegmentRef` into
the session's `V` segment — host-produced, `LOANED` (`shared_memory_protocol.md`
§3), granted in two halves, and **the `InstallSegment` half rides the first
`DataVolume`**, so the first reply's descriptor
is held unread until the `M` lands. Read those bytes, then **send
`ReleaseDeposit`** with the same `segment_id`, the **`region`** the offset falls
in (0 names the first region), `offset` and `len`, so the host
can reclaim the reservation. `SegmentRef.uses` says how many messages name
the reservation; one, here.

Copy the bytes out before releasing. Bounds-check the `SegmentRef` against the
region you actually mapped rather than trusting it. Volumes are `(Z, Y, X)` in
the `dtype` the ref names — **`u8`, `u16` or `f32`**. `DTYPE_F64` exists in the
enum but the `.proto` reserves it for overlay geometry buffers, and the Python
SDK's dtype table omits it, so an `f64` volume deposit would fail
there; do not expect one. Handling it defensively costs nothing.

**A deposit is contiguous C-order (Z slowest, X fastest), little-endian
multibyte values, unpadded** (`shared_memory_protocol.md` §2). `SegmentRef`
carries no strides or byte order, and every SDK's array codec applies the
rule.

Requires the `data_access` permission. Note the re-entrancy rule in
`docs/contributions/commands.md`: reading a dataset served by *this same
plugin's* reader raises `UNSUPPORTED` rather than deadlocking. It does not
apply here — this plugin contributes no reader.

### 3. Blur

Separable Gaussian, per axis, with `σ_a = extent_a / 2`.

Specified precisely so the implementations can be compared:

- Kernel radius `r = ceil(3σ)`; taps `k[i] = exp(-i²/(2σ²))` for `i ∈ [-r, r]`,
  normalised so `Σk = 1`.
- `σ = 0` on an axis means no blur along that axis (the kernel's definition;
  the UI never produces it — see the extents rule above).
- Edges **clamp**: sampling outside the volume replicates the nearest edge
  voxel.
- Convert to `f64` first; accumulate in `f64`; apply Z, then Y, then X.
- **Non-finite voxels** (an `f32` volume can carry them): before blurring,
  replace every NaN and `−Inf` with the volume's **minimum finite value**, so
  a bad voxel can neither be the peak nor lift or sink its neighbourhood;
  `+Inf` stands, and is a legitimate maximum. A volume with no finite voxel
  fails the run with a message. **The blur spreads a `+Inf`**:
  every voxel within the kernel's reach of it blurs to `+Inf`, so §4's
  tie-break puts the peak at the lowest raster index of that neighbourhood,
  up to the kernel radius from the `+Inf` voxel itself, and not on it. That
  is intended: the ROI marks the saturated region.

Use no image-processing library. A separable Gaussian is a few dozen lines
anywhere, and depending on one would make this trivial in a language that has
SciPy and a research project in one that does not.

### 4. Find the peak

Index of the maximum blurred value. Ties resolve to the **lowest index in
raster order** — smallest `z`, then `y`, then `x`.

### 5. Contribute the ROI

An axis-aligned ellipsoid centred on the **voxel centre** of the peak: index
`(z, y, x)` maps to position `(z + 0.5, y + 0.5, x + 0.5)`. Integers are voxel
corners and half-integers are centres — see `QuantizeLattice` in
`proto/roi.proto`.

Semi-axes are the user's extents: `a₀ = (ez, 0, 0)`, `a₁ = (0, ey, 0)`,
`a₂ = (0, 0, ex)` in ZYX.

Send `ContributeRois` with one `RoiContribution`:

- `contrib_id` — stable, e.g. `"peak"`. Re-running replaces that row rather
  than accumulating duplicates.
- `roi.type_name` — `"ellipsoid"`. The host accepts only `box`, `ellipsoid`,
  `cylinder`, `surfq6` from plugins.
- `name` — something legible, e.g. `"Peak"`.
- `style.color` — a CSS hex string.

Requires `contribute_rois`. The reply is `RoisContributed`; every rejection
arrives as an `Error`.

The required *structure* of an ellipsoid `Roi` is given in full below, because
it cannot be derived from anything this repository publishes — see the next
section.

### 6. Timepoint modes

**Current timepoint** — use `ViewState.t`, contribute one ROI with no time
fields set (timeless).

**All timepoints** — for each `t` in `[0, T)` where `T = shape_tczyx[0]`, read
and find `peak_t`. Build the ellipsoid at `peak_0`'s voxel centre, then set:

- `t_start = 0`, `t_end = T - 1`
- `out_of_span = HOLD`
- `pose_translation` — `3T` doubles, ZYX per frame, where frame `i` holds
  `peak_i - peak_0`. Frame 0 is therefore `(0, 0, 0)`.

The pose is **dense by contract**: one row per frame in the domain, no
sparse keyframing. Report progress to the panel's `status` label as you go.

### 7. Report

**The host allows 10 seconds for the entire command handler**
(`docs/contributions/commands.md`), which is less than an all-timepoints run
over a real volume will take. So `CommandDone` cannot be the failure channel
for the analysis itself.

Return `CommandDone{ok=true}` to mean *the job was accepted* as soon as inputs
validate, run the analysis on your own worker, and report progress and the
final outcome — success or failure — by patching the `status` label. Reserve
`CommandDone{ok=false}` for what you can decide immediately: unparseable
extents, no open dataset, a run already in progress.

Current-timepoint runs on a small volume may well finish inside the budget, but
do not rely on it; use one path for both modes.

**The panel is the feedback channel for the run.** The host shows
a successful `CommandDone.message` too (`control_plane_protocol.md` §8.3), so
`peak.run` can tell the status bar the job was accepted — return a short
string. What the run *found* still arrives after that reply has gone and can
reach only a user who has the panel open, which is the other reason every
command here sets `"menu": null`: a menu-invoked run would say "started" and
then appear to do nothing.

## Acceptance

1. The plugin launches, and Plugin Manager shows it running after first use.
2. The panel opens from the Tools menu with its declared widgets.
3. On a volume with an obvious bright blob, the ROI lands on it, and its
   drawn size tracks the entered extents.
4. Changing an extent and re-running moves and resizes the *same* ROI row.
5. In all-timepoints mode, scrubbing T moves the ROI to each frame's peak.
6. A synthetic volume with a known Gaussian blob places the ROI centre at the
   blob's peak voxel.

## Building the ellipsoid `Roi`

`roi.proto` documents `CompileOperator`'s semantics, states that a quarter
arc is weight `√2/2`, and says the ellipsoid's root is its centre. This
section gives the part the host depends on: how many control vertices an
ellipsoid has, and in what order.
The host reconstructs its semi-axis matrix from those controls **by position**,
so a structurally different but geometrically identical `Roi` is misread rather
than rejected.

An ellipsoid with centre `c` and semi-axis vectors `a₀, a₁, a₂` (ZYX):

- **`Roi.vertices` holds exactly 4 entries — the controls, and nothing else**,
  in order: `c`, `c + a₀`, `c + a₁`, `c + a₂`. The host derives its semi-axis
  matrix from these four by position, so order and count are load-bearing.
- **`root`** — 0, the centre.
- **Display vertices** — 18, which are **not stored**. They exist only as the
  output of the compile operator below: the 6 axis tips `c ± a_k`, and the 12
  ring corners `c ± a_i ± a_j` for each of the 3 axis pairs. Storing them in
  `vertices` produces a silently wrong ROI, because the host reads the first
  four entries as the semi-axis frame regardless of what follows.
- **Compile operator** — the point `c + αa₀ + βa₁ + γa₂` is the affine
  combination with row `[1-α-β-γ, α, β, γ]` over the four controls, so every
  display vertex is linear in them. Emit these as `CompileTriplet`s, with
  `controls` listing the four control indices in column order.
  `CompileTriplet.control` is an index **into `controls`**, i.e. an operator
  column, not a vertex index directly. For an ellipsoid the two coincide
  (`controls = [0,1,2,3]`), which hides the distinction here but not for a
  type whose controls are not a leading prefix.
- **Display-vertex ordering** is yours to choose — nothing outside your own
  `edges` and compile rows refers to it. The implementations here use tips
  `(+Z,−Z,+Y,−Y,+X,−X)` followed by four corners
  each for the ZY, ZX and YX rings.
- **Edges** — 3 principal rings, each 4 quarter-arc edges: rational quadratic
  Béziers of weight `√2/2`, whose `control` is the ring corner between the two
  axis tips it joins. An ellipse arc is the affine image of a circular one, so
  the same weight is exact under any semi-axes. Their `role`: the Python
  the same weight is exact under any semi-axes. Their `role` is
  `ELEMENT_DRAWN`: the rings are scaffold for the display, the inclusion
  being analytic.

`direction_specs` and `views` are rejected from plugins. Leave `inclusion`
unset — the default `ENCLOSED` is correct for a filled ellipsoid.
