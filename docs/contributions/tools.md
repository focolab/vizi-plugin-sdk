# Tools

A tool is a live interaction mode. By default it appears as a **checkable**
Tools-menu entry; while active, the tool owns its gesture in its declared
views, and the host forwards press / drag / release to your handlers as
voxel-space `InputEvent`s at interaction rate, on the doorbell rather than
the control plane.

Set `"menu": null` on the manifest contribution to omit that menu entry. The
tool remains fully registered and can be activated from a panel command with
`ctx.host.activate_tool("tool_id")`; input ownership, Escape deactivation,
and the one-active-tool rule are unchanged.

## SDK surface

```python
from vizi_plugin_sdk import InputEvent, ToolContext, serve, tool

@tool("probe")                       # must match the manifest tool id
class VoxelProbe:
    def press(self, ctx: ToolContext, ev: InputEvent) -> None: ...
    def drag(self, ctx: ToolContext, ev: InputEvent) -> None: ...
    def release(self, ctx: ToolContext, ev: InputEvent) -> None: ...
    def cancel(self, ctx: ToolContext, ev) -> None: ...
    def fire(self, ctx: ToolContext, ev: InputEvent) -> None: ...

if __name__ == "__main__":
    serve(plugin_id="org.example.probe")
```

Implement any subset of the handlers — `press`, `drag`, `release`, `cancel`,
`fire`, and `action`.

- **`fire` is a whole interaction in one record**: it opens and closes at once,
  so it has no drag and no release, and a tool that implements only
  press/drag/release never sees one. A tool gets `fire` when its manifest
  `mode` is `"fire"`.
- **`action` is a `FIRE` that names one of the tool's manifest `actions`** —
  a key or a control-pane button fired *into* the engaged tool, mid-drag if
  that is where the user was, carrying the pointer's sample at that moment.
  It lands inside whatever gesture is open and touches the gesture's state
  not at all. `ctx.action(ev)` is the action's manifest id (`None` for the
  primary gesture, and for an index this manifest does not declare — forward
  compatibility, never a fault). A class without `action` receives it as
  `fire`.
- **`cancel` can arrive without a record at all.** An interaction can end with
  neither its own terminal nor a successor announcing it — the camera moved, a
  pane's epoch changed, the dataset changed, the tool was switched — and a
  tool holding gesture state has to be told or it holds it forever. Its
  argument is then a `Cancellation` (`interaction`, `tool`, `gen`, `view_id`,
  `reason`) rather than an `InputEvent`; treat either as "the gesture is
  over".

`InputEvent` carries:

| Field | Meaning |
|---|---|
| `phase` | `PHASE_PRESS` / `PHASE_DRAG` / `PHASE_RELEASE` / `PHASE_CANCEL` / `PHASE_FIRE` |
| `position` | `Voxel`, `Column`, `Ray`, `RayAndPicked`, or `None` — the shape depends on what the view could resolve |
| `view_id`, `epoch` | which pane, and the arrangement that pane is under — pass the event as `pane=ev` when publishing live geometry |
| `interaction`, `seq` | the gesture this record belongs to, and the record's order within it |
| `gen` | the dataset open generation (the low 32 bits of `HostViewState.open_generation`) |
| `t`, `level`, `channels` | timepoint, pyramid level, and per-channel visibility bits |
| `action` | 0 on every phase; on a `FIRE`, the 1-based index of the manifest action it names — `ctx.action(ev)` resolves it |
| `mods`, `click_count` | modifier bitmask and the click run |
| `target_kind`, `target_id`, `target_part` | what the gesture is on (`TARGET_NONE` / `TARGET_ANNOTATION` / `TARGET_ROI` / `TARGET_HANDLE`), when it is on something |
| `miss` | the coordinate is real but is **not a relevant place** — outside the dataset or the crop; not the same as not existing |
| `usable` | `False` when the record's context cannot be reconciled with the arranged state — a `level` the epoch's `Geometry.levels` does not declare, a non-`miss` voxel outside the clip box at that level, a `t` past an arranged time table (`spec/doorbell_E_input.md` §3). The record is still delivered — the terminal closes what it closes — and the SDK has already reported `unusable_context` to the host; a handler does not guess a coordinate from it. Local only, never on the wire |

`ev.position` is the part that varies most: a 2D pane resolves a `Voxel`, a
projection resolves a `Column` (the collapsed axis and its half-open range),
and a 3D pane resolves a `Ray` (`camera`, `far`, `entry`), or a
`RayAndPicked` when something was under the cursor. **`ev.ray` is the ray under
either kind and `ev.pick` the pick or `None`**, so a handler need not branch on
which arrived. A pick's `picked` is the front voxel of the structure the host
found; `front` is that voxel on the ray, and `back` is `front + back_frac ×
(far − front)`. A `back_frac` of `0.0` is a real pick of no depth (`back ==
front`); NaN (`is_point`) says nothing stood out, and `back` is `None`.
Declaring `wants_pick=False` on the tool asks the host not to compute the pick.
That is a request about what the host computes, not a guarantee about what
arrives: a host may send `RayAndPicked` regardless, and a host that picks
sends a bare `Ray` when it found nothing. `spec/doorbell_E_input.md`
§3 is the whole rule. **The dataset's geometry for that event is
`ctx.pane(ev)`** — the `PaneState` this plugin acknowledged for the event's
pane and epoch, already installed, so no round trip and nothing to block on:
`geometry` whole, `spacing_zyx` / `spacing_unit` / `any_axis_known`, `extent`,
`crop` (an `EdgeBox`; `crop_at(n)` converts it to level `n` with the
declared per-axis factor — the number §8.11's map applies — where the bare
`EdgeBox.at_level(n)` assumes vizi's `2^n`), `level_scale(n)`,
`visible_channels(ev.channels)` (bit 15 resolved through the epoch's
`channels_high`), `channel_names()`, and `time_table`. It is `None` only for
an event under an epoch the plugin has already moved past, which the gate
makes rare and a cancel makes harmless. `ctx.host.view_state()` gives a
snapshot, but a tool reads the epoch. **`any_axis_known` is true
when at least one axis's pitch is a measurement, not all three**: read each
axis's own flag (`axis_known_zyx` on a view state) before using that axis's
pitch, and keep the others in voxels.

A tool class may also define `activated(self, ctx, binding)` and
`deactivated(self, ctx, binding)`, optional like the phase handlers, called
after the SDK's own handling (handle installed or gone, open gesture
canceled); a re-statement of a live arrangement is not a second
activation. **The binding is in the tool's hands at the moment it is
engaged** (§8.10): `binding` is the `ToolBinding` just installed
— its assigned `triggers` and every action with the key the host kept —
so a tool can say *P toggles, U undoes* from the hook. A hook written
`(self, ctx)` is called without the binding: the SDK goes by the signature. Later,
`ctx.arrangement("tool_id")` finds the installed binding by manifest id,
as `ctx.arrangement(ev)` does by an event.

**`ctx.panel(id)`** is the patch handle for a manifest-declared panel, the
same one a command gets, so a tool can say *Lasso active* from `activated`,
key a refusal on the record it refused, or report a failed live publish —
without a handle captured from whichever command happened to run first. A
patch is a queued push, so it is safe from the listener thread, unlike a
`ctx.host` request (see the end of *The two feedback lanes*).

**Where the current host differs from that rule** is listed in
`spec/control_plane_implementation_details.md` §2b. Write to the rule: a tool
written to it needs no change when the host catches up. On a 3D pane a record
arrives as a `Ray` or as a `RayAndPicked`, whichever the host had to send, so
a handler takes both.

## Engaging the tool — `triggers`, `actions`, `pane`

The interface is **modal**: one tool is engaged at a time, host-wide, and
the engaged tool owns the primary pointer in its views — the bare left
button and every left chord the host does not keep. **The primary button
means whatever the engaged tool means, so a tool never asks for it.** With
`manifest_version: 1` a tool asks instead for what *engages* it and what it
can be *told* (the [manifest reference](../manifest.md#the-modal-rule--no-tool-names-the-primary-button)
has the fields):

- `triggers` — a key that toggles it (`{"key": "P"}`), a key or a chord that
  holds it while pressed (`{"key": "Space", "hold": "momentary"}`,
  `{"button": "middle", "modifiers": ["ctrl"]}`); on a momentary release the
  previously engaged tool returns, told `deactivated` then `activated`. The
  host assigns what it can and tells the user what it refused; a refused
  trigger is not a refused tool.
- `actions` — `{"id": "add_node", "key": "N"}`: live only while the tool is
  engaged, delivered as an `action` `FIRE` (above).
- `pane` — an icon and tooltip for a control-pane button that engages the
  tool (or fires an action, or runs a command).

`ctx.arrangement(ev)` is the installed `ToolBinding` — the assigned
`triggers` and every `action` with the key it kept — so a tool can tell the
user what engages it and what fires what. `mode` and `cancel_on_camera_move`
are the tool's own and are not part of the binding. (vizi does not draw a
control pane yet; a `pane` declaration is checked at scan and has no effect
until it does.)

## The two feedback lanes — this is the part that matters

Handlers run **at event rate** on the doorbell listener thread — the thread
that also receives segment grants. Do cheap work only, and choose the lane by
phase:

- **During the gesture** (`press`/`drag`): push with
  `ctx.overlay(id).set_items_live(items, pane=ev)` — the overlay's Pulsed
  lane, which the host reads at display rate, plus an advisory `D` on the
  doorbell. Two requirements: geometry must be **inline** (`inline=True` on
  the item builders), because a Pulsed value is claimed and consumed whole,
  and the encoded update must fit the lane's slot (a `ValueError` naming the
  sizes otherwise). Never do per-event gRPC work in these handlers. A publish
  that finds every buffer busy is **coalesced, not failed**: the newest value
  stays readable and a later push supersedes it.
  **Pass the event you are answering as `pane=ev`.** That stamps the pane and
  epoch the geometry was computed under onto the value, and the host draws it
  only while that is still the pane's current epoch — so a marker computed
  against a crop or level the user has since moved off is dropped rather than
  drawn in the wrong place.
- **At gesture end** (`release`): push the final state with the ordinary
  `set_items(...)` — it persists as a regular overlay update (and may use
  segment-backed geometry of any size the arena holds).

`ToolContext.host` exposes the same host client as commands, but a host
request blocks. Never call `ctx.host.view_state()` or
`ctx.host.read_volume()` directly from a tool handler: that would stall input
events and segment grants on the listener thread. Capture the event
coordinates, hand the work to your own worker, and push the result from there.

## Minimal example — a marker that follows the cursor

Manifest:

```json
{
  "manifest_version": 1,
  "id": "org.vizi.voxel_probe",
  "name": "Voxel Probe (minimal example)",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "voxel_probe.py"] },
  "contributions": {
    "tools": [
      { "id": "probe", "name": "Voxel probe", "views": ["2d", "3d"] }
    ],
    "overlays": [
      { "id": "probe_marker", "name": "Probe marker" }
    ]
  },
  "permissions": []
}
```

`voxel_probe.py`:

```python
import numpy as np
from vizi_plugin_sdk import InputEvent, ToolContext, Voxel, serve, tool

@tool("probe")
class VoxelProbe:
    def _marker(self, overlay, at: Voxel, inline: bool):
        pos_zyx = np.array([[at.z, at.y, at.x]], dtype=np.float64)
        return [overlay.marker_item("probe", pos_zyx,
                                    color_rgba=(0, 255, 128, 255),
                                    size_px=10.0, inline=inline)]

    def _at(self, ev: InputEvent) -> "Voxel | None":
        # A 3D pane resolves a ray rather than a voxel, and a MISS is a real
        # coordinate in a place this tool does not want; both mean "no marker".
        return ev.position if isinstance(ev.position, Voxel) and not ev.miss else None

    def press(self, ctx: ToolContext, ev: InputEvent) -> None:
        at = self._at(ev)
        if at is None:
            return
        overlay = ctx.overlay("probe_marker")
        overlay.set_items_live(self._marker(overlay, at, inline=True), pane=ev)

    def drag(self, ctx: ToolContext, ev: InputEvent) -> None:
        self.press(ctx, ev)

    def release(self, ctx: ToolContext, ev: InputEvent) -> None:
        at = self._at(ev)
        if at is None:
            return
        overlay = ctx.overlay("probe_marker")
        overlay.set_items(self._marker(overlay, at, inline=False))

    def cancel(self, ctx: ToolContext, ev) -> None:
        ctx.overlay("probe_marker").clear()

if __name__ == "__main__":
    serve(plugin_id="org.vizi.voxel_probe")
```

Check "Voxel probe" in the Tools menu, then drag across a view: a marker
tracks the cursor live and stays where you release. This sample, with a trail overlay and a
panel image added, runs against the host emulator in
[`examples/voxel_probe/python/`](../../examples/voxel_probe/python/) — the
place to see a gesture, the live lane and the release push over a real
process without a vizi.
