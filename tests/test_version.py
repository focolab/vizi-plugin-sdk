"""``__version__`` is ``1.0.<N>`` and moves with the package."""

from __future__ import annotations

import unittest

import _version_guard as guard
import _version_manifest as recorded
import vizi_plugin_sdk


class TestTheVersionMovesWhenThePackageDoes(unittest.TestCase):
    def test_the_version_has_its_form(self):
        self.assertRegex(vizi_plugin_sdk.__version__, guard.VERSION_FORM)
        self.assertEqual(vizi_plugin_sdk.__version__, guard.current_version())

    def test_a_changed_package_carries_a_new_version(self):
        version = guard.current_version()
        differ = guard.changed(recorded.FILES, guard.current_hashes())
        if differ and version == recorded.VERSION:
            self.fail(
                f"vizi-plugin-sdk is still {version} but these files changed since that "
                f"version was recorded: {', '.join(differ)}.  Any change under the package "
                f"is a new SDK: bump __version__ and regenerate the manifest "
                f"({guard.REGENERATE})."
            )
        if differ or version != recorded.VERSION:
            self.fail(
                f"the version manifest records {recorded.VERSION} and the package is "
                f"{version}"
                + (f" with {', '.join(differ)} changed" if differ else " with no file changed")
                + f": regenerate the manifest ({guard.REGENERATE})."
            )

    def test_the_form_is_a_release_number(self):
        """The public SDK is ``1.0.0``, then ``1.0.1``, ``1.0.2``: the round
        is the last component, and nothing else is a version."""
        for good in ("1.0.0", "1.0.1", "1.0.42"):
            self.assertRegex(good, guard.VERSION_FORM)
        for bad in ("1.0a104", "1.0", "1.0.01", "1.1.0", "1.0.0rc1", "v1.0.0"):
            self.assertNotRegex(bad, guard.VERSION_FORM)

    def test_an_edit_an_addition_and_a_removal_are_each_a_change(self):
        base = {"a.py": "1", "b.py": "2"}
        self.assertEqual(guard.changed(base, dict(base)), [])
        self.assertEqual(guard.changed(base, {"a.py": "1", "b.py": "3"}), ["b.py"])
        self.assertEqual(guard.changed(base, {"a.py": "1", "b.py": "2", "c.py": "4"}), ["c.py"])
        self.assertEqual(guard.changed(base, {"a.py": "1"}), ["b.py"])

    def test_the_stubs_are_counted(self):
        names = set(guard.current_hashes())
        self.assertIn("_proto/vizi_plugin_pb2.py", names)
        self.assertIn("__init__.py", names)

    def test_the_manifest_is_what_the_generator_writes(self):
        self.assertEqual(
            guard.MANIFEST.read_text(encoding="utf-8").replace("\r\n", "\n"),
            guard.render(recorded.VERSION, recorded.FILES),
        )


if __name__ == "__main__":
    unittest.main()
