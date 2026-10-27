#!/usr/bin/env python3
"""The tetra_shape example, in Python (``../SPEC.md``): one irregular
tetrahedron contributed four ways -- free corners, a declared scale, an
asked shape that keeps its volume, and an asked type with no shaper.

The manifest is declared in code: ``plugin(...)``, the command, the shaper
(``@region_shaper`` declares its type) and ``region_type`` for the type with
no shaper.  ``vizi-plugin.json`` beside this file is what ``--manifest``
writes, with the relative launch line the driver uses.
"""

from __future__ import annotations

import sys

import numpy as np

from vizi_plugin_sdk import CommandContext, Rejected, ShapeEdit, command, on_shape_edit_ended, pb2, plugin, region_shaper, region_type, roi_pb2, serve

#: SPEC §1.
plugin("org.example.tetra_shape", name="Tetra Shape", version="0.1.0", permissions=["contribute_rois"])

#: SPEC §2: the corners A, B, C, D and the center c (their centroid), ZYX.
CORNERS = np.array([[10, 10, 10], [10, 30, 12], [28, 18, 14], [16, 20, 32]], dtype=np.float64)
CENTRE = CORNERS.mean(axis=0)
IDENTITY = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
REGIONS = (  # contrib_id, name, type_name, offset
    ("free", "Free corners", "tetra.free", (0, 0, 0)),
    ("scale", "Scale", "tetra.scale", (0, 0, 40)),
    ("volume", "Keeps its volume", "tetra.volume", (0, 0, 80)),
    ("unshaped", "No shaper", "tetra.unshaped", (0, 0, 120)),
)


def _vec(p) -> roi_pb2.Vec3:
    return roi_pb2.Vec3(z=float(p[0]), y=float(p[1]), x=float(p[2]))


def _handle(type_name: str) -> roi_pb2.HandleSpec:
    if type_name in ("tetra.free", "tetra.unshaped"):
        return roi_pb2.HandleSpec(frame=IDENTITY, mode=roi_pb2.ALL, dof_mask=7)
    spec = roi_pb2.HandleSpec(frame=IDENTITY, mode=roi_pb2.PRIMARY, axis_source=roi_pb2.AXIS_RADIAL, extent_anchor=4)
    if type_name == "tetra.scale":
        spec.min_extent = 1.0
    return spec


def scale_bindings() -> list[roi_pb2.LinearBinding]:
    """SPEC §2: M = (v_j - c) u_i^T / r_i, target-major, moved ascending."""
    out = []
    for i in range(4):
        r = float(np.linalg.norm(CORNERS[i] - CENTRE))
        u = (CORNERS[i] - CENTRE) / r
        for j in range(4):
            if j != i:
                m = np.outer(CORNERS[j] - CENTRE, u) / r
                out.append(roi_pb2.LinearBinding(moved=j, target=i, m=[float(v) for v in m.ravel()]))
    return out


def region(type_name: str, offset) -> roi_pb2.Roi:
    off = np.asarray(offset, dtype=np.float64)
    vertices = [roi_pb2.Vertex(position=_vec(p + off), enclosing=True, handle=_handle(type_name)) for p in CORNERS]
    vertices.append(roi_pb2.Vertex(position=_vec(CENTRE + off)))
    roi = roi_pb2.Roi(type_name=type_name, vertices=vertices, root=4)
    if type_name == "tetra.scale":
        roi.auto_surface = roi_pb2.AUTO_SURFACE_STAR
        roi.bindings.extend(scale_bindings())
    return roi


@command("tetra.contribute", title="Contribute the tetrahedra", menu=None)
def contribute(ctx: CommandContext) -> str:
    rois = [pb2.RoiContribution(contrib_id=cid, name=name, roi=region(type_name, offset)) for cid, name, type_name, offset in REGIONS]
    ctx.host.contribute_rois(rois, replace_all=True)
    return f"contributed {len(rois)} tetrahedra"


@region_shaper("tetra.volume", name="Tetrahedron (keeps its volume)")
def keep_volume(ctx, edit: ShapeEdit):
    """SPEC §3, from the edit alone: the drag's and the commit's answer."""
    h = int(edit.handle)
    if not 0 <= h < 4:
        return Rejected(f"vertex {h} is not a corner")
    p = np.asarray(edit.positions, dtype=np.float64)
    p1 = np.asarray(edit.p1, dtype=np.float64)
    a, b, d = [j for j in range(4) if j != h]
    g = (p[a] + p[b] + p[d]) / 3
    n = np.cross(p[b] - p[a], p[d] - p[a])
    n /= np.linalg.norm(n)
    s0, s1 = float(n @ (p[h] - g)), float(n @ (p1 - g))
    if s0 * s1 <= 0 or abs(s1) < abs(s0) / 16:
        return Rejected("the corner would come too close to the opposite face")
    k = np.sqrt(abs(s0) / abs(s1))
    out = p.copy()
    out[h] = p1
    for j in (a, b, d):
        out[j] = g + k * (p[j] - g)
    return out


#: SPEC §4: a declared type with no shaper; the SDK answers its commits REJECTED.
region_type("tetra.unshaped", name="Tetrahedron (no shaper)")


@on_shape_edit_ended
def ended(ctx, drag_id: int, outcome: str, detail: str) -> None:
    """SPEC §4: hear every outcome; a log line is enough."""
    print(f"tetra_shape: drag {drag_id} {outcome}{': ' + detail if detail else ''}", file=sys.stderr, flush=True)


if __name__ == "__main__":
    serve()
