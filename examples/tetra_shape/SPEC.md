# tetra_shape — one irregular tetrahedron, four ways

A sample plugin for regions of any geometry and for region types a plugin shapes. It demonstrates
`ContributeRois` with point-cloud geometry, handles and `LinearBinding`s, `contributions.region_types`, the SDK's
region-shaper hook and its `ShapeEditEnded` hook. One irregular tetrahedron is contributed four times, each with a different answer to "what happens when the person
drags a corner":

| Region | `type_name` | Kind | A drag of a corner |
|---|---|---|---|
| `free` | `tetra.free` | declared | moves that corner, nothing else |
| `scale` | `tetra.scale` | declared, `STAR` | scales the whole tetrahedron about its center, through 12 `LinearBinding`s |
| `volume` | `tetra.volume` | **asked** | the plugin moves the opposite face so the volume stays the same |
| `unshaped` | `tetra.unshaped` | **asked**, no shaper | the plugin answers `REJECTED`: it declared the type and registers no shaper for it |

Read `control_plane_protocol.md` §8.8 (*What a contributed region may be*, *Region types a plugin shapes*) and
`manifest_schema.md` §3.8 first. This file fixes only what the driver checks.

Region types a plugin shapes are part of protocol 1.0, so every 1.0 host implements them. The driver runs against
`examples/vizi_hostsim`, whose host half follows the spec text (`vizi_hostsim/regions.py`).

## 1. The manifest

| Field | Value |
|---|---|
| `id` | `org.example.tetra_shape` |
| `name` | `Tetra Shape` |
| `version` | `0.1.0` |
| `permissions` | `["contribute_rois"]` |
| `contributions.commands` | one: `{ "id": "tetra.contribute", "title": "Contribute the tetrahedra", "menu": null }` |
| `contributions.region_types` | `[{ "id": "tetra.volume", "name": "Tetrahedron (keeps its volume)" }, { "id": "tetra.unshaped", "name": "Tetrahedron (no shaper)" }]` |

`tetra.free` and `tetra.scale` are **not** region types: a type the plugin does not declare is a declared region,
enacted by the host from its data.

## 2. The four regions

`tetra.contribute` sends **one** `ContributeRois` with the four regions in the table's order, `replace_all = true`,
and answers `ok`. Running it again restores all four to their rest shapes.

Each region's `RoiContribution`: `contrib_id` and `name` as below; `style` left unset.

| `contrib_id` | `name` | offset (ZYX) |
|---|---|---|
| `free` | `Free corners` | (0, 0, 0) |
| `scale` | `Scale` | (0, 0, 40) |
| `volume` | `Keeps its volume` | (0, 0, 80) |
| `unshaped` | `No shaper` | (0, 0, 120) |

**Vertices** (voxel-index ZYX), each plus the region's offset:

| Index | Point | Position |
|---|---|---|
| 0 | A | (10, 10, 10) |
| 1 | B | (10, 30, 12) |
| 2 | C | (28, 18, 14) |
| 3 | D | (16, 20, 32) |
| 4 | c, the centroid | (16, 19.5, 17) |

Vertices 0–3 are **corners**: `enclosing = true`, each a handle. Vertex 4 is the **center**: not enclosing, no
handle. Every region has `root = 4`, no edges, facets or patches, `inclusion` unset (`ENCLOSED`), and no pose, frame
or parent. No other field is set.

**Handles.** Every handle's `frame` is the identity, nine values `[1,0,0, 0,1,0, 0,0,1]`.

| Region | Corner handle | `auto_surface` | `bindings` |
|---|---|---|---|
| `free` | `mode = ALL`, `dof_mask = 7` | unset (`HULL`) | none |
| `scale` | `mode = PRIMARY`, `axis_source = AXIS_RADIAL`, `extent_anchor = 4`, `min_extent = 1.0` | `AUTO_SURFACE_STAR` | 12, below |
| `volume` | `mode = PRIMARY`, `axis_source = AXIS_RADIAL`, `extent_anchor = 4` | unset (`HULL`) | none |
| `unshaped` | `mode = ALL`, `dof_mask = 7` | unset (`HULL`) | none |

`dof_mask` is `0` (unset) on the `PRIMARY` handles.

**The scale bindings.** With `v_i` corner *i*'s rest position, `c` the center, `r_i = |v_i − c|` and
`u_i = (v_i − c) / r_i`: for each target corner *i* = 0, 1, 2, 3 in turn, and for each other corner *j* in
increasing order, one `LinearBinding{ moved = j, target = i, m = M }` with

  **M = (v_j − c) · u_iᵀ / r_i**, a 3×3 matrix, written row-major (`m[3·row + col]`), in ZYX.

So 12 bindings, targets 0,0,0,1,1,1,2,2,2,3,3,3. Dragging corner *i* along its ray by `δ = t·u_i` moves corner *j*
by `M·δ = (t / r_i)(v_j − c)`: every corner scales about `c` by `1 + t/r_i`, exactly. The offset cancels in every
difference, so all four regions use the same `M`s.

## 3. The `tetra.volume` shaper

A `ShapeEdit` carries `move.roi` (the region at the drag's start), `move.handle` and `move.p1`, the point the host
projected onto the handle's ray. The shaper answers **from that message alone**, the same way for a `shape_drag`
(as a `shape_proposal` naming its `seq`) and for a `shape_commit` (as the `shape_answer`, `seq = 0`).

With `P` the five positions of `move.roi`, `h = move.handle`:

1. If `h` is not 0–3: `REJECTED`, message `"vertex h is not a corner"`.
2. The opposite face is the other three corners, `j ≠ h`, `j < 4`, in increasing order `a < b < d`. Its centroid
   `g = (P_a + P_b + P_d) / 3`; its unit normal `n = (P_b − P_a) × (P_d − P_a)`, normalized.
3. `s0 = n · (P_h − g)` and `s1 = n · (p1 − g)`, the corner's signed height over the face before and after.
4. If `s0 · s1 ≤ 0` or `|s1| < |s0| / 16`: `REJECTED`, with a non-empty message for the person (for example
   `"the corner would come too close to the opposite face"`).
5. Otherwise `k = √(|s0| / |s1|)`, and the answer is, for every vertex in order:
   - corner `h`: `p1`;
   - each face corner `j`: `g + k · (P_j − g)`;
   - the center, vertex 4: unchanged, `P_4`.

   Resolution `OK`.

The volume is a third of the face's area times the corner's height; the face scales in its own plane by `k`, its
area by `k²`, and the height by `|s1| / |s0|`, so the volume is unchanged. (Pulling a corner out draws the opposite
face in.)

## 4. What the plugin owes, beyond the shapes

- **Answer every `shape_commit`.** For `tetra.unshaped`, which has no shaper, the answer is `REJECTED` with a
  non-empty message saying no shaper is registered. Silence would leave the region held while a person waits.
- **Stop proposing for a drag** once its `shape_commit` or `ShapeEditEnded` has arrived.
- **Drop silently**, logging nothing as an error: a `shape_drag` arriving after its drag's `shape_commit`, and a
  `ShapeEditEnded` for a drag the plugin never answered.
- **Hear every `ShapeEditEnded`** through the SDK's hook, and keep the outcome (a log line is enough). The driver
  does not observe it.

## 5. Acceptance (the driver, `python/hostsim.py`)

Each check runs against the emulator; `ok` lines count. Tolerances are `1e-9` absolute on positions and `1e-9`
relative on volumes.

1. **Launch:** `Ready`; the host read `region_types` = {`tetra.volume`, `tetra.unshaped`}.
2. **Contribute:** `tetra.contribute` answers `ok`; four regions, nothing refused; `type_owner` is the plugin for
   `volume` and `unshaped` and empty for `free` and `scale`.
3. **The geometry** of all four regions is §2's: positions, handles, `root`, `auto_surface`, and the scale bindings.
4. **Free:** the host drags `free`'s corner 2 by (+3, −2, +5): that corner moves there and nothing else does.
5. **Scale:** the host drags `scale`'s corner 1 to 1.5× its distance from the center, with the pointer 3 voxels
   off the ray at right angles to it: the projection lands at exactly 1.5×, every corner ends at that scale about
   the center, and the center stays.
6. **Scale clamp:** the host drags `scale`'s corner 0 through the center: `CLAMPED`, the corner 1.0 from the
   center.
7. **Volume, asked:** five moves of `volume`'s corner 3 out along its ray (1.1× to 1.5×). Each proposal the host
   draws keeps the volume, puts corner 3 at the projected point, and moves the face corners toward their centroid by
   one factor. On release the host gets a `shape_answer`, sends `COMMITTED`, and the region's shape is the answer.
8. **Volume, rejected:** a drag of `volume`'s corner 0 to within 1/32 of its height over the opposite face: the
   proposal is `REJECTED`, the shape on screen stays, and the release is `REVERTED` with the plugin's message; the
   region is unchanged.
9. **Spinbox:** a spinbox edit of `volume`'s corner 2 to 1.2×, a commit with no drag before it: `COMMITTED`, volume
   kept.
10. **Cancel:** a drag of `volume`'s corner 1, then the person cancels: `CANCELLED`, the region unchanged.
11. **Silent drops:** a `shape_drag` sent after its drag ended, and a `ShapeEditEnded` for a drag the plugin never
    saw: nothing reported, and the plugin then answers a command and a new drag as before.
12. **No shaper:** a drag and release of `unshaped`'s corner 0: `REVERTED`, with the plugin's non-empty message.
13. **Again:** `tetra.contribute` restores every region's rest shape.
14. **Nothing refused**, no violation either way, nothing unhandled.
