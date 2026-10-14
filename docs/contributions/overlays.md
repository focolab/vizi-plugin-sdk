# Overlays

An overlay draws plugin geometry — markers, lines, and dense label volumes —
into vizi's views. The manifest declaration is the authorization: it is what
makes the host grant the overlay its `O` segment, and a push from an overlay
id the host granted nothing for raises `NoRegion`.

**An overlay needs something that starts its plugin.** It is drawn only while
the plugin runs, and the host starts a plugin only for something the person
does: a panel; a command or a tool that is in the Tools menu or has a `pane`
button; a reader, an exporter or a stage. A `default_keybinding` or a tool's
`triggers` alone do not count (another binding can take them), and a manifest
whose overlays nothing starts is refused (`manifest.md`, `overlays`). The
minimal example below is started from the Tools menu; a panel is the usual
choice.

## SDK surface — `OverlayHandle`

Inside a [command](commands.md) or [tool](tools.md) handler,
`ctx.overlay(overlay_id)` returns the handle:

```python
overlay = ctx.overlay("markers")
overlay.set_items([
    overlay.marker_item("grid", positions_zyx,           # (N, 3) float64
                        color_rgba=(255, 200, 0, 255), size_px=8.0),
    overlay.line_item("path", positions_zyx,
                      color_rgba=(0, 200, 255, 255), thickness=2.0),
])
overlay.clear()                       # retract everything
```

- **Positions are ZYX float64 in the voxel frame** — the wire convention.
  Pass anything numpy can coerce to an `(N, 3)` array. The host owns the
  flip to its internal axis order; never pre-swap to XYZ.
- `marker_item(item_id, positions_zyx, *, color_rgba, size_px, shape,
  inline)` — one item drawing N markers.
- `line_item(item_id, positions_zyx, *, topology, color_rgba, thickness,
  inline)` — a polyline through the positions. **Drawn in the 2D panes
  too**: projected into every slice and dashed out of its
  own — solid where both endpoints round into the slice, dashed by arc
  length otherwise — never sectioned, so a 3D path shows its in-plane run
  solid and the rest dashed in every slice. `thickness` is pixels in the
  3D pane; **in the 2D panes a pixel is a voxel**, for `thickness` and for
  a marker's `size_px` alike — the overlay layer lives on the level-0
  lattice and follows the zoom, and a line thinner than a voxel draws one
  voxel wide.
- `set_items(items)` — a **full replacement** of the overlay's draw list,
  last write wins. There is no incremental add/remove; push the complete
  list each time. Each push carries a monotonic revision, shared with the
  live lane, so the host can order the two.
- `clear()` — retract the overlay entirely.
- Colors are 8-bit RGBA tuples.

**Geometry is a loan.** Each `set_items` reserves the geometry bytes from the
overlay's `O` arena and sends a descriptor; the bytes stay lent until the
host sends `ReleaseLoan`. The arena's size is the manifest's `segment_mib`
(default 32 MiB, comfortably 10⁵ float64 points). An arena with no room for
the next push **fails it** with `NoRegion` — naming what was free and what
was wanted — rather than overwriting what the host may still be reading, and
tells the host by how much it was short. If that happens routinely, raise
`segment_mib` or push less often.

Small geometry can travel inline instead (`inline=True` on the builders): the
bytes ride the control-plane message itself, below an 8 MiB ceiling, and no
arena is touched. The live lane requires it; for ordinary pushes it is a
choice.

## Dense label volumes

`label_volume_item` is the categorical segmentation path:

```python
labels = np.ascontiguousarray(labels, dtype=np.uint16)  # (Z, Y, X)
lut = np.zeros((256, 4), dtype=np.uint8)
lut[1:] = (0, 200, 255, 180)

item = overlay.label_volume_item(
    "objects",
    labels,
    lut,
    t=current_t,
    position_zyx=(z0, y0, x0),
    label_names={1: "nucleus", 2: "membrane"},
    uses=2,  # this push AND the compile below will both name these bytes -- the compile is certain here
)
overlay.set_items([item])
count = ctx.host.compile_label_volume("objects", item)
```

- Labels must be contiguous `(Z, Y, X)` `uint8` or `uint16` data, **on the
  dataset's level-0 lattice**. `frame="voxel"` (the default) names that
  lattice whatever pyramid level the pane is drawing or you read from — if
  you computed on a decimated level, convert the volume and its
  `position_zyx` to level 0 before pushing (`control_plane_protocol.md`
  §8.11 has the edge map). `scale_zyx` cannot say "these are level-L voxels":
  it is exactly what the compile rejects.
- The colormap is a `(256, 4)` `uint8` RGBA lookup table. **Row 0 is label
  0's alone** and should be transparent; a foreground label `id` draws with
  row `1 + (id - 1) % 255`, so ids 1..255 use rows 1..255 directly and ids
  above 255 wrap over the foreground rows — never onto row 0. A row's alpha
  is the **row's**, shared by every id that lands on it, and the compile
  persists it as the mask's `visible` — unless `label_visible={id: bool}`
  names the id, which is the only way to hide 260 and not 5. It governs the
  compiled mask; the voxels are painted from the LUT by the GPU, which has
  only the row, so above 255 the two visuals are allowed to differ. A plugin
  with no ids above 255 never needs it.
- Labels are categorical and nearest-sampled, never interpolated.
- `t=<index>` shows the item only at one timepoint; omit it to show at all
  timepoints.
- `position_zyx` places the tight label-volume box at a level-0 voxel origin;
  omit it for `(0, 0, 0)`.
- `label_names` is optional — a `{label id: name}` mapping (or a sequence,
  index = id) — and is what `compile_label_volume` names the resulting masks
  with. It is keyed by **label id, not LUT row**, so labels that share a row
  (1 and 256) keep their own names; an unnamed label compiles to
  `"Label <id>"`, and a name for 0 is ignored.
- `uses` is how many messages will name the item's bytes: each `set_items`
  push that includes it, plus one per `compile_label_volume` of it. The
  labels and LUT are loans from the overlay's arena, released by the host
  once that many messages are done — and only you know the number, because
  pushes and requests travel on channels the host drains in different orders.
  Leave it at 1 for a push alone. The SDK keeps the mirror of the count and
  refuses your own excess before it leaves: the message that would name a
  reservation for the `uses + 1`-th time raises `UsesExceeded`, in whichever
  order the push and the compile come, because the alternative is the host
  releasing the bytes under a message still in flight. Over-declare and they
  stay pinned until the segment retires (waste, never corruption) — and **a
  declared use cannot be withdrawn**, so `uses=2` is for a push whose compile
  is certain. A push whose compile may never come (a tool that pushes per
  gesture and commits rarely) declares one use, and the compile, when it
  comes, reserves the item again: one copy at commit, nothing pinned on the
  gestures that were superseded (`spec/control_plane_protocol.md` §8.8).
  Refused with `inline=True`, which has no reservation.
- **A label volume is a voxel selection, and the host checks it at the
  push**: `frame = "voxel"`, unit scale in `"world"` mode,
  identity rotation, an integer `position_zyx`, and the whole box —
  position plus the labels' shape, zeros included — inside the dataset. A
  push whose label volume is not one is **not applied at all** (an update
  is a full replacement, and half of one is a state you never sent), and
  the host says which rule through `ContractViolation` site 28,
  `overlay_item_refused` — the same site that reports every push the host
  refuses at the wire. The compile refuses the same item by kind. The one
  thing that displays and does not compile is inline geometry: a label
  volume that will be committed is sent segment-backed.
- Label volumes composite in both the 2D and native 3D views. The native pass
  follows the renderer-selected pyramid level with nearest-neighbor
  decimation, so labels smaller than the decimation factor may disappear
  while zoomed out and return as the selected level changes.

Label-volume items are plugin-owned display overlays by default.
`ctx.host.compile_label_volume(overlay_id, item)` compiles the same
segment-backed labels into one editable native mask annotation per nonzero
label — it needs `contribute_rois`, and the item must be a voxel selection:
`frame="voxel"` (the default), unit scale, identity rotation, an integer
`position_zyx`, segment-backed geometry (`inline=False`, the default), and a
volume wholly inside the dataset. Those are checked at compile, not at push —
an item violating them displays as its transform says. **A compile is one
more use of the item's loans**: build the item with `uses=2` when the
push-then-compile is certain, as above. Built with the default and already
pushed, the compile is the second use of a one-use reservation, and the SDK
refuses it with `UsesExceeded` rather than let the host release bytes under
it — build the item again for the compile (a second reservation) when the
push could not know.

For event-rate updates from tools there is a second lane —
`set_items_live` — covered on the [tools page](tools.md).

## Chunked overlays — a movie's segmentation, one volume at a time

A label volume is one item on a full-replacement draw list, so the unit of
overlay state is the whole overlay — and a dense segmentation of every frame
of a movie fits no arena. A **chunked overlay**
(`spec/control_plane_protocol.md` §5.7) changes the unit to **one label
volume at `(level, t)`**, and reverses who decides what is sent: **the host
names the volumes it wants** and the plugin answers each, one at a time.

Declare it in the manifest — `"chunked": true`, and `"time": "per_t"` (the
default: one volume per level per timepoint) or `"static"` (one volume per
level for every timepoint, an atlas). No `segment_mib`: the host sizes the
overlay's arena from the open dataset (one level-0 u16 volume plus one
level-1 volume) and re-grants it when the dataset changes.

```python
from vizi_plugin_sdk import Chunk, ChunkWant, chunk_source

@chunk_source("cells")
def cells(ctx, want: ChunkWant) -> Chunk | np.ndarray | None:
    volume = ctx.host.read_volume(want.t, 0, level=want.level)   # ON the wanted level
    labels = segment(volume)                                     # (Z, Y, X) uint8 or uint16
    box = tight_box(labels)                                      # or None: nothing here
    return None if box is None else Chunk(box.labels, position_zyx=box.origin)
```

- **The host sends `ChunksWanted`** — the whole set of `(level, t)` it wants
  for the overlay, in its priority order, restated whenever it changes,
  empty when nothing is wanted (no dataset open). The SDK keeps the latest
  per overlay and runs your source **on a thread of its own**, once per
  want while that want stays in the set, in the host's order. A want that
  leaves the set is forgotten; one that returns is served again; the same
  set restated serves nothing; on a dataset change the host restates the
  set empty, re-grants the arena, and names the new dataset's volumes once
  it is installed. `want.t` is `None` on a `static` overlay.
- **Answer on the wanted lattice.** `want.level` is the pyramid level the
  host will draw the chunk at; read there and label there. `position_zyx`
  is in that level's voxel indices and the box must lie inside that level's
  shape. Nothing converts to level 0 — that is the coarser lattice a label
  volume cannot express, legal here because the host chose it.
- **Bbox-tight, `uint8` or `uint16`.** Return a `Chunk`, a bare array (a
  volume at the origin), or `None` for an **empty** chunk — a state
  ("nothing here"), not a removal. A source that raises is diagnosed and the
  next want served.
- **A full arena defers, it does not fail.** Every other push fails and
  counts when the arena is full; a chunk was asked for, and the host
  releases the one before it in the tick that applies it, so the SDK keeps
  your answer and **waits for the host's next `ReleaseLoan`**, then retries.
  No `NoRegion`, no `SegmentShortfall`. You never see it.
- **The label table is yours to send, once:** `ctx.overlay(id).set_label_table(
  colormap, label_names=, label_visible=, opacity=, hidden=, draw_priority=)`
  — what a label volume carries per item, once per overlay, inline. Until
  it arrives the host draws chunks with a default LUT. Send it again when
  the names or the visibility change; a threshold change needs no new table.
- **When your results change**, `ctx.overlay(id).invalidate_chunks()`
  serves every current want again. `ctx.overlay(id).wants` is the current
  set, and `publish_chunk(want, labels, position_zyx=)` publishes by hand
  for a plugin that owns the waiting itself (it raises `NoRegion` on a full
  arena; then *you* wait for the release).
- **A chunked overlay takes nothing else**: an `OverlayUpdate` to one is
  refused by the host (`ContractViolation` site 30, `overlay_chunk_refused`
  — the same site reports a chunk the host named but cannot apply: a level
  the dataset lacks, a box outside it, a dtype that is not u8/u16). A chunk
  the host did **not** name is dropped and counted, not refused: a want
  that left while the chunk was on the wire is ordinary. And a chunked
  overlay **never compiles** — `compile_label_volume` on one is
  `INVALID_ARGUMENT`.

`examples/movie_seg/python/` is a whole plugin on this surface: a band
segmentation of every frame, a `static` footprint, the tables, and a
threshold command that re-serves what the host is looking at. Its
`hostsim.py` shows what the host does on the other side, including the
re-grant on a dataset change and a deferred publish observed by holding the
host's releases.

## Minimal example — push and clear from the Tools menu

Manifest:

```json
{
  "manifest_version": 1,
  "id": "org.vizi.marker_overlay",
  "name": "Marker Overlay (minimal example)",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["python3", "marker_overlay.py"] },
  "contributions": {
    "commands": [
      { "id": "markers.show", "title": "Show demo markers" },
      { "id": "markers.clear", "title": "Clear demo markers" }
    ],
    "overlays": [
      { "id": "markers", "name": "Demo markers" }
    ]
  },
  "permissions": []
}
```

`marker_overlay.py`:

```python
import numpy as np
from vizi_plugin_sdk import CommandContext, command, serve

@command("markers.show")
def show(ctx: CommandContext) -> None:
    overlay = ctx.overlay("markers")
    grid_zyx = np.array(
        [[2.0, y, x] for y in (4.0, 12.0, 20.0) for x in (4.0, 12.0, 20.0)],
        dtype=np.float64,
    )
    diagonal_zyx = np.array([[0.0, 0.0, 0.0], [2.0, 20.0, 20.0]], dtype=np.float64)
    overlay.set_items([
        overlay.marker_item("grid", grid_zyx, color_rgba=(255, 200, 0, 255), size_px=8.0),
        overlay.line_item("diag", diagonal_zyx, color_rgba=(0, 200, 255, 255), thickness=2.0),
    ])

@command("markers.clear")
def clear(ctx: CommandContext) -> None:
    ctx.overlay("markers").clear()

if __name__ == "__main__":
    serve(plugin_id="org.vizi.marker_overlay")
```

Open a dataset, then Tools → "Show demo markers". Overlays pushed from a
tool's gesture run against the emulator in
[`examples/voxel_probe/python/`](../../examples/voxel_probe/python/), and a
chunked label overlay in [`examples/movie_seg/python/`](../../examples/movie_seg/python/).
