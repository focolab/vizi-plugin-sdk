"""Crop geometry: edge coordinates, pyramid levels, and included centers.

``control_plane_protocol.md`` §8.11 and ``doorbell_E_input.md`` §3 are what this
implements, and both are emphatic about *why* it is a module rather than three
lines at a call site:

    The proportional form ``e / scale`` is wrong on roughly 63% of crops at
    every level >= 1 [...] and agrees with the affine map at level 4 of that
    example, so a check at one level does not distinguish them.

Two rules carry everything here:

- **A boundary sits at a pixel EDGE.** Integer pixel ``i`` occupies
  ``[i - 0.5, i + 0.5]``, so a crop of pixels 25 through 1519 is
  ``{24.5, 1519.5}`` -- the half-open integer range ``[25, 1520)`` said exactly
  rather than approximately.  The half-pixel is the point: it is the margin
  that absorbs rounding and float error, so four call sites converting slightly
  differently stops mattering.
- **The level map is AFFINE, not proportional.**  vizi's pyramid
  block-downsamples anchored at index 0, so level-N pixel ``j`` covers level-0
  ``[j*s, j*s + s - 1]`` and is *centered* at ``j*s + (s - 1)/2``.  Inverting
  that is where the ``+- 0.5`` comes from; dropping it shifts every bound by
  ``0.5*(s - 1)/s`` level-N pixels and fails **inward** at the low face, which
  silently omits a plane that is inside the crop.
- **``scale`` is the SAMPLING FACTOR, never the ratio of two array shapes.**
  The two part on an odd dimension: vizi block-downsamples by 2 and discards
  an odd trailing sample, so a 31-voxel axis becomes 15 and the factor is
  ``2`` while the shape ratio is ``2.0667``.  The map above is applied to the
  number directly, so a ratio puts every converted coordinate and every crop
  face slightly wrong at every level >= 1 of any odd dimension -- the same
  inward failure by a different route.  ``Geometry.levels`` carries the
  factor (§8.11); a
  ``ViewState.levels`` entry carries a SHAPE, and dividing two of those is
  the mistake.

**Clipping is not lighting, and neither bounds what a plugin may read.**  The
crop says which content is *relevant* to an interaction.  Crop-active named
regions and per-voxel mask crops change what is *lit*, are display state, and
are carried on neither channel -- so a column whose region crop leaves ``[2,4)``
and ``[8,10)`` lit still reports ``[2,10)``, correctly, because every voxel
between them is addressable and a plugin reading one gets data.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = ["EdgeBox", "edge_at_scale", "centres", "level_scale"]


def level_scale(level: int) -> int:
    """The level-0 pixels per level-``level`` pixel in vizi's pyramid, on an
    axis that is still folding.

    Each level halves, so this is ``2 ** level`` -- until the axis has shrunk
    to one sample, where the factor stops at its last fold (§8.11) and only
    the host's declaration, ``PaneState.level_scale(level)``, knows it.  A
    dataset whose pyramid is not 2x per level has its own factor too.  Every
    function here takes a ``scale`` so the declared one can be passed instead
    of this assumed.
    """
    if level < 0:
        raise ValueError("a pyramid level is not negative")
    return 1 << level


def edge_at_scale(edge: float, scale: float) -> float:
    """Convert one level-0 **edge** coordinate to a level with this scale.

    ``(edge + 0.5) / scale - 0.5`` -- exact for any scale, and it composes:
    ``((e + 0.5)/2 - 0.5 + 0.5)/2 - 0.5 == (e + 0.5)/4 - 0.5``.  A level-0
    *index* has no exact level-N index at all, which is the whole reason
    ``doorbell_E_input.md`` §3 refuses to normalize coordinates between levels.
    """
    if scale <= 0:
        raise ValueError("a pyramid scale is positive")
    return (edge + 0.5) / scale - 0.5


def centres(lo: float, hi: float) -> range:
    """The pixel centers an edge interval includes: ``ceil(lo) ..= floor(hi)``.

    **Inclusive at both ends**, so an exactly-integral bound is a hit.  Empty
    when the interval contains no center, which a crop set on pixel boundaries
    never produces -- those land on half-integers, half a pixel from a tie.
    """
    first, last = math.ceil(lo), math.floor(hi)
    return range(first, last + 1) if last >= first else range(0)


@dataclass(frozen=True)
class EdgeBox:
    """A box in edge coordinates, level-0 voxel units -- ``Geometry.crop``.

    The field order is the IDL's, ZYX, and each pair is ``(low face, high
    face)``.  This is the *clip* box: what an interaction means.
    """

    z0: float
    z1: float
    y0: float
    y1: float
    x0: float
    x1: float

    # -- conversions ------------------------------------------------------

    @classmethod
    def from_index_box(
        cls, min_zyx: "tuple[int, int, int]", max_zyx: "tuple[int, int, int]"
    ) -> "EdgeBox":
        """From ``ViewState``'s half-open index form: ``edge = index - 0.5``.

        ``ViewState.crop_min_zyx`` / ``crop_max_zyx`` are min-inclusive,
        max-**exclusive** level-0 indices.  They are not
        interchangeable numbers with this box, and they cannot express a bound
        that is not on a pixel boundary -- prefer ``Geometry.crop`` where both
        are available.
        """
        (z0, y0, x0), (z1, y1, x1) = min_zyx, max_zyx
        return cls(z0 - 0.5, z1 - 0.5, y0 - 0.5, y1 - 0.5, x0 - 0.5, x1 - 0.5)

    def to_index_box(self) -> "tuple[tuple[int, int, int], tuple[int, int, int]]":
        """Back to the half-open index form: ``min = ceil(lo)``,
        ``max = floor(hi) + 1``."""
        lows = tuple(math.ceil(v) for v in (self.z0, self.y0, self.x0))
        highs = tuple(math.floor(v) + 1 for v in (self.z1, self.y1, self.x1))
        return lows, highs  # type: ignore[return-value]

    def at_scale(self, scale) -> "EdgeBox":
        """This box in a level whose pixels are ``scale`` level-0 pixels wide
        -- one number for every axis, or a ZYX triple when the host declared
        the factor per axis (``PaneState.level_scale(level)``, which is the
        number §8.11's map applies)."""
        if isinstance(scale, (int, float)):
            sz = sy = sx = float(scale)
        else:
            sz, sy, sx = (float(v) for v in scale)
        return EdgeBox(
            edge_at_scale(self.z0, sz),
            edge_at_scale(self.z1, sz),
            edge_at_scale(self.y0, sy),
            edge_at_scale(self.y1, sy),
            edge_at_scale(self.x0, sx),
            edge_at_scale(self.x1, sx),
        )

    def at_level(self, level: int) -> "EdgeBox":
        """This box at pyramid ``level`` **of vizi's own pyramid** (``scale =
        2 ** level`` on every axis).  NOT the pane-aware conversion: a host
        declares its sampling factor per level and per axis in
        ``Geometry.levels``, and §8.11 says never to assume ``2^L`` -- so a
        plugin converting the pane's crop uses ``PaneState.crop_at(level)``,
        which applies the declared factor, and this is for a box a plugin
        knows to be on vizi's pyramid.

        **Convert to the level FIRST, then to centers.**  Doing it the other way
        rounds twice, and the second rounding has no half-pixel of margin left
        to absorb it.
        """
        return self.at_scale(level_scale(level))

    # -- what a plugin actually asks --------------------------------------

    def centres(self) -> "tuple[range, range, range]":
        """The included pixel centers per axis, in this box's own level."""
        return (
            centres(self.z0, self.z1),
            centres(self.y0, self.y1),
            centres(self.x0, self.x1),
        )

    def contains(self, z: float, y: float, x: float) -> bool:
        """Whether a coordinate is inside the clip box, bounds included.

        A ``False`` here is what the host reports as ``MISS`` on an ``E``
        record: **not relevant, which is not the same as not existing.**  The
        voxel may be perfectly readable, and a plugin that wants it is entitled
        to it (§8.11).
        """
        return self.z0 <= z <= self.z1 and self.y0 <= y <= self.y1 and self.x0 <= x <= self.x1

    def as_tuple(self) -> "tuple[float, float, float, float, float, float]":
        return (self.z0, self.z1, self.y0, self.y1, self.x0, self.x1)
