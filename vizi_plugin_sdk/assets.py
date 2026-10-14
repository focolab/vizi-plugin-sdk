"""Paths the vizi host provides a plugin: its immutable assets, and the folder
its settings persist in."""

from __future__ import annotations

import json
import os
from pathlib import Path

ENV_ASSETS = "VIZI_PLUGIN_ASSETS"
ENV_SETTINGS_DIR = "VIZI_PLUGIN_SETTINGS_DIR"


class AssetUnavailableError(LookupError):
    """The manifest did not declare an asset or it is not provisioned yet."""


def asset_path(asset_id: str) -> Path:
    """Return a verified, host-provisioned asset path.

    The host publishes assets atomically, verifies them against the manifest's
    declaration, and supplies the ID-to-path mapping in the launch
    environment.  Missing assets raise instead of returning a path that a
    backend will fail to open later.
    """
    try:
        mapping = json.loads(os.environ.get(ENV_ASSETS, "{}"))
    except json.JSONDecodeError as exc:
        raise AssetUnavailableError("the host supplied an invalid asset mapping") from exc
    path = mapping.get(asset_id) if isinstance(mapping, dict) else None
    if not isinstance(path, str):
        raise AssetUnavailableError(f"asset {asset_id!r} is not declared for this plugin")
    resolved = Path(path)
    if not resolved.is_file():
        raise AssetUnavailableError(
            f"asset {asset_id!r} is not provisioned; open vizi's Plugin Manager"
        )
    return resolved


class SettingsDirUnavailableError(LookupError):
    """The host did not provide a settings folder: the plugin is running
    outside vizi, or vizi could create neither the folder beside the manifest
    nor its fallback (the host's log says which)."""


def settings_dir() -> Path:
    """The folder this plugin's settings persist in, created here on
    first use: the host names it and checks it can be made, but does not make
    it, so a plugin that keeps no settings leaves nothing behind.

    ``settings/`` beside the plugin's manifest, so deleting the plugin deletes
    its settings.  When the plugin's folder is not writable, a folder under
    vizi's own data directory holding a ``plugin-id.txt`` naming the plugin.
    State about one dataset belongs in that dataset's folder instead --
    ``HostViewState.file_state_dir`` -- which moves and dies with the dataset.
    """
    raw = os.environ.get(ENV_SETTINGS_DIR, "")
    if not raw:
        raise SettingsDirUnavailableError("the host provided no settings folder")
    path = Path(raw)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SettingsDirUnavailableError(
            f"the host's settings folder {path} could not be created: {exc}"
        ) from exc
    return path
