"""What the host provides a plugin by path: ``settings_dir()``
(``control_plane_protocol.md`` §2.3)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vizi_plugin_sdk import SettingsDirUnavailableError, settings_dir
from vizi_plugin_sdk.assets import ENV_SETTINGS_DIR


class SettingsDir(unittest.TestCase):
    """§2.3: the host names the folder in the environment and the SDK
    creates it on first use; with no variable an SDK reports the absence
    rather than inventing a folder."""

    def test_the_hosts_folder_is_returned(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.dict(os.environ, {ENV_SETTINGS_DIR: tmp}):
                self.assertEqual(settings_dir(), Path(tmp))

    def test_no_variable_is_no_folder_and_says_so(self):
        env = {k: v for k, v in os.environ.items() if k != ENV_SETTINGS_DIR}
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(SettingsDirUnavailableError, "no settings folder"):
                settings_dir()

    def test_a_named_folder_not_there_yet_is_created_on_first_use(self):
        """The host created ``settings/`` for every
        plugin at launch, leaving an empty folder in each source tree."""
        with tempfile.TemporaryDirectory() as tmp:
            named = Path(tmp) / "settings"
            with mock.patch.dict(os.environ, {ENV_SETTINGS_DIR: str(named)}):
                self.assertEqual(settings_dir(), named)
            self.assertTrue(named.is_dir())

    def test_a_folder_that_cannot_be_made_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_bytes(b"")
            with mock.patch.dict(os.environ, {ENV_SETTINGS_DIR: str(blocker / "settings")}):
                with self.assertRaisesRegex(SettingsDirUnavailableError, "could not be created"):
                    settings_dir()


if __name__ == "__main__":
    unittest.main()
