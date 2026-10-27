"""Plugin SDK — what a (Python) plugin author imports to speak the protocol.

A plugin runs in its own process + venv (never importing vizi's code). The SDK
gives it: the pre-generated protocol stubs (``_proto``), a ``@reader`` decorator
+ a server runner that hides the gRPC ``Session`` bidi-stream and the
doorbell and shared-memory mechanics, and manifest helpers. The ``.proto`` is the
contract; this is the convenience layer over it.

Every export resolves LAZILY (PEP 562): grpcio/protobuf are plugin-environment
deps, and the HOST imports this package (for ``pb2`` on its control-plane
paths) from a wheel install that does not carry them — a module-scope stub
import would crash the host at startup.  ``from <package> import serve``
etc. keep working unchanged for plugin authors.
"""

from __future__ import annotations

#: The SDK's own version, IN THE SOURCE so it travels with the code: this is
#: what the handshake's identity string names (`control_plane_protocol.md`
#: §3.1), and a version looked up from the environment names whichever
#: distribution happens to be installed there -- not necessarily the copy
#: that is running.  vizi's in-tree copy of this package, a checkout on
#: `PYTHONPATH`, and an older install beside them all answer differently to
#: `importlib.metadata`, and only this constant answers for the file that was
#: loaded.  `pyproject.toml` reads it from here; bump it here.
#:
#: The last component moves with ANY change to this package -- a moved
#: accessor as much as a wire change -- so two builds that differ never share a
#: version: ``1.0.0`` is the first public release, then ``1.0.1``, ``1.0.2``.
#: A change to the specification alone leaves it alone, so it reads "the last
#: change to this wheel".  The PROTOCOL is `1.0` and is not this
#: number.  `tests/test_version.py` fails when a file here changes
#: and this line does not.
__version__ = "1.0.0"

_PROTO_EXPORTS = ("pb2", "pb2_grpc", "roi_pb2")
_ASSET_EXPORTS = (
    "AssetUnavailableError",
    "SettingsDirUnavailableError",
    "asset_path",
    "settings_dir",
)
#: `control_plane_protocol.md` §8.11's crop math -- stdlib only, so it costs a
#: caller nothing and is usable without a session.
#: Declaring the interface in code; ``widgets`` is a submodule.
_DECLARE_EXPORTS = (
    "DeclarationError",
    "action",
    "asset",
    "overlay",
    "pane",
    "panel",
    "plugin",
    "region_type",
    "trigger",
)
_GEOMETRY_EXPORTS = ("EdgeBox", "centres", "edge_at_scale", "level_scale")
#: `control_plane_protocol.md` §3.1's version gating: the feature table and the
#: error `HostClient.require` raises -- stdlib only, like the geometry.
_FEATURE_EXPORTS = ("FEATURES", "FeatureUnavailable")
_SERVER_EXPORTS = (
    "Cancelled",
    "ChannelName",
    "TaskHandle",
    "cancelled",
    "report_progress",
    "Chunk",
    "ChunkWant",
    "CommandContext",
    "PaneState",
    "AppliedFilter",
    "ExportDescriptor",
    "RegionChange",
    "RegionsChange",
    "ViewChange",
    "ViewResult",
    "Follow",
    "follow",
    "on_regions_changed",
    "on_view_changed",
    "Rejected",
    "Shape",
    "ShapeEdit",
    "on_shape_edit_ended",
    "region_shaper",
    "HostClient",
    "HostDataArray",
    "HostError",
    "SessionEnded",
    "HostViewState",
    "InclusionMask",
    "AmbiguousSegment",
    "NoRegion",
    "OverlayHandle",
    "PanelHandle",
    "PendingRequest",
    "PluginContext",
    "ReaderInfo",
    "SeriesChoice",
    "SeriesEntry",
    "Refusal",
    "ShutdownNotice",
    "ToolContext",
    "chunk_source",
    "command",
    "exporter",
    "on_shutdown",
    "panel_opened",
    "WidgetSize",
    "widget_geometry",
    "Appearance",
    "appearance",
    "reader",
    "serve",
    "stage",
    "tool",
)
_STAGE_EXPORTS = (
    "NewChannel",
    "REFUSE_CAUSAL",
    "REFUSE_MISSING_INPUT",
    "REFUSE_OTHER",
    "RefuseVolume",
    "StageInput",
    "StagePlan",
    "StageSlot",
)
#: The doorbell's vocabulary, as a `@tool` handler sees it
#: (`doorbell_E_input.md`).  Re-exported through :mod:`.server` so a plugin has
#: one import: an `InputEvent` carries a phase, an interaction, a pane epoch, a
#: target and one of four position kinds.  `VIEW_2D`/`VIEW_3D` are `E`'s
#: `view_id` values.
_DOORBELL_EXPORTS = (
    "Column",
    "KIND_COLUMN_YX",
    "KIND_COLUMN_ZX",
    "KIND_COLUMN_ZY",
    "InputEvent",
    "Notification",
    "PHASE_CANCEL",
    "PHASE_DRAG",
    "PHASE_FIRE",
    "PHASE_PRESS",
    "PHASE_RELEASE",
    "VIEW_2D",
    "VIEW_3D",
    "Ray",
    "RayAndPicked",
    "TARGET_ANNOTATION",
    "TARGET_HANDLE",
    "TARGET_NONE",
    "TARGET_ROI",
    "Voxel",
)

__all__ = sorted(
    _PROTO_EXPORTS
    + _ASSET_EXPORTS
    + _DECLARE_EXPORTS
    + _GEOMETRY_EXPORTS
    + _FEATURE_EXPORTS
    + _SERVER_EXPORTS
    + _STAGE_EXPORTS
    + _DOORBELL_EXPORTS
)


def __getattr__(name: str):
    if name in _PROTO_EXPORTS:
        from . import _proto

        return getattr(_proto, name)
    if name in _ASSET_EXPORTS:
        from . import assets

        return getattr(assets, name)
    if name in _DECLARE_EXPORTS:
        from . import declare

        return getattr(declare, name)
    if name in _GEOMETRY_EXPORTS:
        from . import geometry

        return getattr(geometry, name)
    if name in _FEATURE_EXPORTS:
        from . import features

        return getattr(features, name)
    if name in _STAGE_EXPORTS:
        from . import stages

        return getattr(stages, name)
    if name in _SERVER_EXPORTS or name in _DOORBELL_EXPORTS:
        from . import server

        return getattr(server, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
