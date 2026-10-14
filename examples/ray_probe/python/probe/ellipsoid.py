"""The axis-aligned ellipsoid ``Roi`` builder -- `examples/peak_roi/SPEC.md` "Building the ellipsoid Roi", copied verbatim from `examples/peak_roi/python/peak/ellipsoid.py` as `SPEC.md` §"Contributing" asks.

The arrangement here is load-bearing and cannot be derived from anything the
SDK repository publishes: the host reconstructs the semi-axis matrix from the
first four vertices **by position**, so a structurally different but
geometrically identical `Roi` is misread rather than rejected.

Four control vertices, eighteen display vertices, twelve quarter-arc edges, and
the linear compile operator relating them.  Imports only the generated ROI
stub, so ``selftest.py`` can check the structure with no host present.
"""

from __future__ import annotations

import math

from vizi_plugin_sdk import roi_pb2

#: Quarter arc of a conic as a rational quadratic Bezier.  An ellipse arc is
#: the affine image of a circular one, so this weight is exact under any
#: semi-axes.
ARC_WEIGHT = math.sqrt(2.0) / 2.0

# The six axis tips, as signatures (α, β, γ) of `c + α·a0 + β·a1 + γ·a2`.
_TIPS = (
    (1, 0, 0),
    (-1, 0, 0),
    (0, 1, 0),
    (0, -1, 0),
    (0, 0, 1),
    (0, 0, -1),
)

# The three principal rings, named by the pair of semi-axes spanning them:
# ZY, ZX, YX.  Semi-axis 0 is Z, 1 is Y, 2 is X.
_RINGS = ((0, 1), (0, 2), (1, 2))

# The four corners of a ring, in the cyclic order its four quarter arcs visit.
_RING_CORNERS = ((1, 1), (-1, 1), (-1, -1), (1, -1))


def _signature(axis_i: int, sign_i: int, axis_j: int = -1, sign_j: int = 0) -> tuple:
    signature = [0, 0, 0]
    signature[axis_i] = sign_i
    if axis_j >= 0:
        signature[axis_j] = sign_j
    return tuple(signature)


def _display_signatures() -> tuple:
    """The 18 display-vertex signatures: 6 axis tips, then 4 corners per ring.

    SPEC.md leaves this ordering to the implementor — nothing outside `edges`
    and the compile rows refers to it — and recommends this one so independent
    implementations stay comparable.  (vizi's own native builder happens to use
    a different order, which is the same claim seen from the host side.)
    """
    signatures = list(_TIPS)
    for axis_i, axis_j in _RINGS:
        for sign_i, sign_j in _RING_CORNERS:
            signatures.append(_signature(axis_i, sign_i, axis_j, sign_j))
    return tuple(signatures)


DISPLAY = _display_signatures()
_INDEX = {signature: index for index, signature in enumerate(DISPLAY)}


def _arcs() -> tuple:
    """The 12 quarter arcs as ``(source, target, control)`` display indices.

    Each ring is four tips in cyclic order; the control of the arc joining two
    tips is the ring corner between them.
    """
    arcs = []
    for axis_i, axis_j in _RINGS:
        tips = [
            _INDEX[_signature(axis_i, 1)],
            _INDEX[_signature(axis_j, 1)],
            _INDEX[_signature(axis_i, -1)],
            _INDEX[_signature(axis_j, -1)],
        ]
        corners = [
            _INDEX[_signature(axis_i, sign_i, axis_j, sign_j)]
            for sign_i, sign_j in _RING_CORNERS
        ]
        for step in range(4):
            arcs.append((tips[step], tips[(step + 1) % 4], corners[step]))
    return tuple(arcs)


ARCS = _arcs()


def ellipsoid(center_zyx, extents_zyx, pose_translation=None):
    """An axis-aligned ellipsoid centred on ``center_zyx`` with the given semi-axes.

    ``pose_translation``, when given, is one ZYX offset per frame of a dense
    whole-region pose starting at frame 0; the region then declares the frame
    domain it covers and HOLDs its end pose outside it.  Omit it for a timeless
    region.
    """
    center = tuple(float(value) for value in center_zyx)
    extents = tuple(float(value) for value in extents_zyx)
    if len(center) != 3 or len(extents) != 3:
        raise ValueError("centre and extents are ZYX triples")

    roi = roi_pb2.Roi(type_name="ellipsoid", root=0)

    # The controls, in the ONE order the host accepts: centre, then centre plus
    # each semi-axis vector.  Count and order are what the host reads; four
    # entries and nothing else.
    semi_axes = (
        (extents[0], 0.0, 0.0),
        (0.0, extents[1], 0.0),
        (0.0, 0.0, extents[2]),
    )
    for offset in ((0.0, 0.0, 0.0),) + semi_axes:
        vertex = roi.vertices.add()
        vertex.position.z = center[0] + offset[0]
        vertex.position.y = center[1] + offset[1]
        vertex.position.x = center[2] + offset[2]

    # The display vertices are NOT stored: they are the output of this
    # operator.  `c + α·a0 + β·a1 + γ·a2` is the affine combination with row
    # [1-α-β-γ, α, β, γ] over the four controls, so the whole ellipsoid is
    # linear in them and a dragged axis tip recompiles every ring.
    roi.compile.num_display = len(DISPLAY)
    roi.compile.controls.extend((0, 1, 2, 3))
    for row, (alpha, beta, gamma) in enumerate(DISPLAY):
        coefficients = (1 - alpha - beta - gamma, alpha, beta, gamma)
        for column, coefficient in enumerate(coefficients):
            if coefficient:
                roi.compile.triplets.add(
                    display_vertex=row,
                    # An index into `controls` — an operator COLUMN, not a
                    # vertex index.  They coincide here because the controls
                    # are a leading prefix; they need not in general.
                    control=column,
                    coeff=float(coefficient),
                )

    for source, target, control in ARCS:
        roi.edges.add(
            source=source,
            target=target,
            control=control,
            weight=ARC_WEIGHT,
            # An ellipsoid's inclusion is analytic, not a triangulated hull, so
            # its rings are drawn but are not part of the enclosing surface.
            # The proto3 default (ELEMENT_BOTH) would claim otherwise.
            role=roi_pb2.ElementRole.ELEMENT_DRAWN,
        )

    if pose_translation is not None:
        offsets = [tuple(float(value) for value in row) for row in pose_translation]
        if not offsets or any(len(row) != 3 for row in offsets):
            raise ValueError("pose_translation is one ZYX row per frame")
        roi.t_start = 0
        roi.t_end = len(offsets) - 1
        roi.out_of_span = roi_pb2.OutOfSpan.HOLD
        for row in offsets:
            roi.pose_translation.extend(row)

    return roi


def compile_display(roi) -> list:
    """Evaluate a linear compile operator against its controls.

    Not used by the plugin — the host does this itself — but it is the only way
    to check offline that the stored controls and the operator agree, which is
    the failure the host cannot report.
    """
    positions = [[0.0, 0.0, 0.0] for _ in range(roi.compile.num_display)]
    controls = list(roi.compile.controls)
    for triplet in roi.compile.triplets:
        vertex = roi.vertices[controls[triplet.control]].position
        row = positions[triplet.display_vertex]
        row[0] += triplet.coeff * vertex.z
        row[1] += triplet.coeff * vertex.y
        row[2] += triplet.coeff * vertex.x
    return [tuple(row) for row in positions]
