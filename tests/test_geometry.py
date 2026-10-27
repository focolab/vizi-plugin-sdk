"""The crop math of ``control_plane_protocol.md`` §8.11.

The document records the wrong formula it shipped and the level at which it
happens to agree with the right one, so both are tested here on purpose: the
worked example is the regression, and level 4 is the case that hid it.
"""

from __future__ import annotations

import math
import unittest

from vizi_plugin_sdk.geometry import EdgeBox, centres, edge_at_scale, level_scale


class Edges(unittest.TestCase):
    """§8.11: a boundary sits at a pixel EDGE, and the half-pixel is the point."""

    def test_the_worked_example_is_the_half_open_range_said_exactly(self):
        """Pixels 25 through 1519 -- the range [25, 1520) -- is {24.5, 1519.5}."""
        box = EdgeBox.from_index_box((25, 25, 25), (1520, 1520, 1520))
        self.assertEqual(box.z0, 24.5)
        self.assertEqual(box.z1, 1519.5)
        self.assertEqual(box.to_index_box(), ((25, 25, 25), (1520, 1520, 1520)))

    def test_a_crop_on_pixel_boundaries_never_sits_on_a_tie(self):
        box = EdgeBox.from_index_box((0, 3, 7), (10, 11, 12))
        for value in box.as_tuple():
            self.assertEqual(abs(value % 1), 0.5, "half a pixel from a rounding tie")

    def test_centres_are_inclusive_at_both_ends(self):
        """An exactly-integral bound is a hit."""
        self.assertEqual(list(centres(2.0, 4.0)), [2, 3, 4])
        self.assertEqual(list(centres(1.5, 4.5)), [2, 3, 4])
        self.assertEqual(list(centres(2.2, 2.8)), [], "no centre in the interval")


class LevelMap(unittest.TestCase):
    """§8.11: AFFINE, not proportional -- the offset is the grid's phase."""

    def test_a_level_0_edge_converts_exactly(self):
        self.assertEqual(edge_at_scale(24.5, 2), 12.0)
        self.assertEqual(edge_at_scale(1519.5, 2), 759.5)

    def test_the_case_the_wrong_formula_got_wrong(self):
        """The draft wrote ``e / scale``, which gives 13..759 at level 1 where
        the truth is 12..759 -- and it fails INWARD at the low face, silently
        omitting a plane that is inside the crop."""
        box = EdgeBox.from_index_box((25, 25, 25), (1520, 1520, 1520)).at_level(1)
        z, _, _ = box.centres()
        self.assertEqual((z.start, z.stop - 1), (12, 759))
        self.assertEqual(math.ceil(24.5 / 2), 13, "proportional scaling, for the record")

    def test_level_4_is_where_the_two_formulas_agree(self):
        """Which is how the wrong one survived verification: the raw values
        differ, and the pixel each rounds to does not."""
        edge = 24.5
        self.assertNotEqual(edge_at_scale(edge, 16), edge / 16)
        self.assertEqual(math.ceil(edge_at_scale(edge, 16)), math.ceil(edge / 16), "both 2")
        self.assertNotEqual(
            math.ceil(edge_at_scale(edge, 2)), math.ceil(edge / 2), "12 against 13 at level 1"
        )

    def test_the_map_composes_across_chained_levels(self):
        for edge in (-0.5, 0.5, 24.5, 1519.5, 7.25):
            with self.subTest(edge=edge):
                chained = edge_at_scale(edge_at_scale(edge, 2), 2)
                self.assertAlmostEqual(chained, edge_at_scale(edge, 4), places=12)

    def test_a_full_dataset_crop_is_minus_half_to_n_minus_half(self):
        """§8.11: compare against `extent` in EDGE terms -- a full-dataset crop
        is {-0.5, N-0.5}, not {0, N}."""
        box = EdgeBox.from_index_box((0, 0, 0), (64, 64, 64))
        self.assertEqual(box.z0, -0.5)
        self.assertEqual(box.z1, 63.5)
        self.assertEqual(list(box.at_level(1).centres()[0]), list(range(32)))

    def test_scales_and_levels_are_validated(self):
        with self.assertRaises(ValueError):
            edge_at_scale(1.0, 0)
        with self.assertRaises(ValueError):
            level_scale(-1)


class Clipping(unittest.TestCase):
    """§8.11: clipping is not lighting, and neither bounds what may be read."""

    def test_containment_includes_the_faces(self):
        box = EdgeBox.from_index_box((2, 2, 2), (10, 10, 10))
        self.assertTrue(box.contains(1.5, 1.5, 1.5), "the low face is inside")
        self.assertTrue(box.contains(9.5, 9.5, 9.5), "and so is the high one")
        self.assertFalse(box.contains(1.4, 5, 5), "outside is not relevant")

    def test_a_coordinate_outside_the_crop_is_still_a_place(self):
        """The rule the `MISS` flag carries: not RELEVANT is not non-existent,
        and a plugin deliberately working outside the crop is entitled to."""
        box = EdgeBox.from_index_box((2, 2, 2), (10, 10, 10))
        self.assertFalse(box.contains(0, 0, 0))
        self.assertEqual(list(box.centres()[0]), [2, 3, 4, 5, 6, 7, 8, 9])


if __name__ == "__main__":
    unittest.main()
