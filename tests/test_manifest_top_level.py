"""The manifest's top level (``manifest_schema.md`` §2): ``manifest_version`` is
1, the plugin id is lower-case reverse-DNS, ``min_vizi`` and ``sdk_version``
are optional facts about the release, and a permission or subscription is
honored from the protocol minor that introduced it."""

from __future__ import annotations

import unittest
from unittest import mock

from vizi_plugin_sdk import manifest
from vizi_plugin_sdk.manifest import ManifestError, parse_manifest


def _data(**fields) -> dict:
    data = {
        "manifest_version": 1,
        "id": "org.example.x",
        "name": "X",
        "version": "1",
        "engines": {"vizi": "1.0"},
        "entry": {"command": ["python3"]},
    }
    data.update(fields)
    return data


class ManifestVersion(unittest.TestCase):
    def test_only_one_is_read(self):
        self.assertEqual(parse_manifest(_data()).id, "org.example.x")
        for version in (0, 2):
            with self.subTest(version=version):
                with self.assertRaisesRegex(
                    ManifestError,
                    rf"manifest_version {version} unsupported \(this host reads \[1\]; a 1\.x "
                    r"addition raises engines\.vizi, which this one declares as '1\.0', never "
                    r"manifest_version\)",
                ):
                    parse_manifest(_data(manifest_version=version))

    def test_zero_is_refused_for_itself_not_for_a_field_it_lacked(self):
        tool = {"id": "t", "views": ["3d"], "triggers": [{"button": "left"}]}
        with self.assertRaisesRegex(ManifestError, "manifest_version 0 unsupported"):
            parse_manifest(_data(manifest_version=0, contributions={"tools": [tool]}))


class PluginId(unittest.TestCase):
    def test_lower_case_reverse_dns_of_3_to_64_characters(self):
        for good in ("a.b", "org.example2.my_tool", "org." + "a" * 60):
            with self.subTest(id=good):
                self.assertEqual(parse_manifest(_data(id=good)).id, good)
        for bad in (
            "ab",
            "exampletool",
            "org.Example.tool",
            "1org.example",
            "org..example",
            "org.example.",
            ".org.example",
            "my_org.example",
            "org.1example",
            "org.my-tool",
            "org." + "a" * 61,
        ):
            with self.subTest(id=bad):
                with self.assertRaises(ManifestError) as caught:
                    parse_manifest(_data(id=bad))
                self.assertIn(
                    f"id {bad!r} is not a plugin id: lower-case reverse-DNS, 3 to 64 characters, "
                    r"matching ^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$",
                    str(caught.exception),
                )

    def test_a_windows_device_name_as_the_first_part_is_refused(self):
        # Installers use the id as a folder name, and Windows refuses these in any case.
        for bad, part in (
            ("con.example", "con"),
            ("lpt1.tool", "lpt1"),
            ("LPT1.tool", "LPT1"),
            ("com9.plugin", "com9"),
            ("nul.a.b", "nul"),
            ("Aux", "Aux"),
            ("prn.x", "prn"),
            ("com0.plugin", "com0"),
            ("lpt0.x", "lpt0"),
        ):
            with self.subTest(id=bad):
                with self.assertRaises(ManifestError) as caught:
                    parse_manifest(_data(id=bad))
                self.assertIn(
                    f"id {bad!r} is not a plugin id: "
                    f"its first part {part!r} is a Windows device name",
                    str(caught.exception),
                )
        # A device name inside a longer first part, or as a later part, is an ordinary id.
        for good in ("console.example", "com10.x", "example.con", "org.lpt1.aux", "conx.nul"):
            with self.subTest(id=good):
                self.assertEqual(parse_manifest(_data(id=good)).id, good)

    def test_a_blank_id_is_still_named_as_blank(self):
        with self.assertRaisesRegex(ManifestError, "id must be non-empty"):
            parse_manifest(_data(id="   "))


class ReleaseFacts(unittest.TestCase):
    def test_min_vizi_is_a_pep_440_version(self):
        for good in ("0", "0.9.5", "1.0rc2", "1.0a1.post2.dev3", "2.dev1"):
            with self.subTest(min_vizi=good):
                self.assertEqual(parse_manifest(_data(min_vizi=good)).min_vizi, good)
        for bad in ("", "v0.9", "latest", "1.", "1.0-beta", "1.0+local", "1!2.0", "1.0rc"):
            with self.subTest(min_vizi=bad):
                with self.assertRaisesRegex(ManifestError, "is not a PEP 440 version"):
                    parse_manifest(_data(min_vizi=bad))
        for bad in (0.9, None, ["0.9"]):
            with self.subTest(min_vizi=bad):
                with self.assertRaisesRegex(ManifestError, "min_vizi must be a string"):
                    parse_manifest(_data(min_vizi=bad))

    def test_sdk_version_is_a_non_empty_string(self):
        self.assertEqual(parse_manifest(_data(sdk_version="1.0a87")).sdk_version, "1.0a87")
        for bad in ("", 87, None):
            with self.subTest(sdk_version=bad):
                with self.assertRaisesRegex(
                    ManifestError, "sdk_version must be a non-empty string"
                ):
                    parse_manifest(_data(sdk_version=bad))

    def test_both_are_optional(self):
        parsed = parse_manifest(_data())
        self.assertIsNone(parsed.min_vizi)
        self.assertIsNone(parsed.sdk_version)


class IntroducedMinors(unittest.TestCase):
    def test_every_name_today_arrived_with_1_0(self):
        self.assertEqual(manifest.PERMISSION_INTRODUCED, {"contribute_rois": 0, "data_access": 0})
        self.assertEqual(manifest.SUBSCRIPTION_INTRODUCED, {"epochs": 0, "view": 0, "regions": 0})
        self.assertEqual(manifest.KNOWN_PERMISSIONS, frozenset(manifest.PERMISSION_INTRODUCED))
        self.assertEqual(manifest.KNOWN_SUBSCRIPTIONS, frozenset(manifest.SUBSCRIPTION_INTRODUCED))

    def test_a_name_from_a_later_minor_is_ignored_like_an_unknown_one(self):
        later_p = {**manifest.PERMISSION_INTRODUCED, "teleport": 1}
        later_s = {**manifest.SUBSCRIPTION_INTRODUCED, "weather": 1}
        with (
            mock.patch.object(manifest, "PERMISSION_INTRODUCED", later_p),
            mock.patch.object(manifest, "SUBSCRIPTION_INTRODUCED", later_s),
        ):
            with self.assertLogs(manifest.__name__, "WARNING") as logs:
                parsed = parse_manifest(
                    _data(
                        permissions=["data_access", "teleport"],
                        subscriptions=["view", "weather"],
                    )
                )
        self.assertEqual(parsed.permissions, ("data_access",))
        self.assertEqual(parsed.subscriptions, ("view",))
        self.assertTrue(any("'teleport'" in line for line in logs.output), logs.output)
        self.assertTrue(any("'weather'" in line for line in logs.output), logs.output)


if __name__ == "__main__":
    unittest.main()
