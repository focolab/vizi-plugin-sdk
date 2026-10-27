"""``control_plane_protocol.md`` §3.1's version gating, as a table.

The feature names and the sentences are shared by every SDK, so they are
tested here as literals: a rename in one language is a plugin log that reads
differently in another.
"""

from __future__ import annotations

import unittest

import vizi_plugin_sdk
from vizi_plugin_sdk import features as f


class TheTable(unittest.TestCase):
    def test_the_names_are_the_ones_every_sdk_uses(self):
        self.assertEqual(
            f.FEATURES,
            {
                "view_subscription": 0,
                "regions_subscription": 0,
                "raw_read": 0,
                "processing_stages": 0,
                "region_shapers": 0,
                "set_view": 0,
                "follow": 0,
            },
        )

    def test_no_feature_is_newer_than_this_sdk(self):
        self.assertLessEqual(max(f.FEATURES.values()), f.PROTOCOL_MINOR)

    def test_the_package_exports_them(self):
        self.assertIs(vizi_plugin_sdk.FEATURES, f.FEATURES)
        self.assertIs(vizi_plugin_sdk.FeatureUnavailable, f.FeatureUnavailable)
        self.assertIn("FEATURES", vizi_plugin_sdk.__all__)
        self.assertIn("FeatureUnavailable", vizi_plugin_sdk.__all__)


class TheSentences(unittest.TestCase):
    def test_ready_is_the_lower_minor(self):
        for host in (0, 1, 99):
            self.assertEqual(f.ready_minor(host), min(host, f.PROTOCOL_MINOR))

    def test_the_engines_refusal(self):
        self.assertEqual(
            f.engines_refusal(2, 0),
            "this plugin's manifest requires protocol 1.2 but its code speaks 1.0",
        )
        self.assertIsNone(f.engines_refusal(0, 0))

    def test_each_refusal_names_what_it_needs(self):
        self.assertIsNone(f.unavailable("set_view", (1, 0)))
        cases = {
            (
                "follow",
                None,
            ): 'feature "follow" needs protocol 1.0; the session has no handshake yet',
            ("telepathy", (1, 0)): 'feature "telepathy" is not one this SDK knows',
            ("telepathy", None): 'feature "telepathy" is not one this SDK knows',
        }
        for (feature, session), message in cases.items():
            with self.subTest(feature=feature, session=session):
                self.assertEqual(str(f.unavailable(feature, session)), message)
        self.assertEqual(
            str(f.FeatureUnavailable("test_future", 1, 0)),
            'feature "test_future" needs protocol 1.1; this session speaks 1.0',
        )

    def test_the_error_is_a_runtime_error(self):
        error = f.FeatureUnavailable("x", None, 0)
        self.assertIsInstance(error, RuntimeError)
        self.assertEqual((error.feature, error.needs, error.session_minor), ("x", None, 0))


if __name__ == "__main__":
    unittest.main()
