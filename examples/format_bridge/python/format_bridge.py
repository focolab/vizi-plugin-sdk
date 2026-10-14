#!/usr/bin/env python3
"""Crate format bridge — the Python implementation of ``examples/format_bridge/SPEC.md``.

One reader, one exporter, four commands, a panel, on ``vizi_plugin_sdk``.  The
container itself is ``crate/`` and knows nothing about vizi; what is here is
the plugin: the reader and exporter classes the SDK dispatches to, the panel
model, the two provisioning paths for the calibration table, and the workers
behind the two commands that wait for a human.
"""

from __future__ import annotations

import functools
import json
import sys
import threading
from pathlib import Path

import numpy as np

from vizi_plugin_sdk import (
    AssetUnavailableError,
    CommandContext,
    ExportDescriptor,
    HostError,
    PanelHandle,
    PluginContext,
    ReaderInfo,
    Refusal,
    asset,
    asset_path,
    command,
    exporter,
    panel,
    panel_opened,
    pb2,
    plugin,
    reader,
    serve,
    widgets as w,
)

from crate.format import (
    FLAG_DERIVED,
    CODE_OF_DTYPE,
    Calibration,
    Crate,
    CrateError,
    CrateWriter,
    check_file,
    spacing_label,
)
from crate.ome import resolve_calibration

#: The manifest is declared in code (SPEC's Manifest section): ``--install``
#: installs it, and ``vizi-plugin.json`` beside this file is what
#: ``--manifest`` writes.
PLUGIN_ID = "org.example.format_bridge"
plugin(PLUGIN_ID, name="Crate Format Bridge", version="0.1.0", permissions=["data_access"])
PANEL_ID = "crate"

#: The manifest asset's id, and the two runtime slots.  Slot names are durable
#: per-plugin state in the host's asset store (``../../PERSISTENT-STATE.md``),
#: so they are pinned by SPEC.md and never renamed.
CALIBRATION_ASSET = "calibration"
asset(
    CALIBRATION_ASSET,
    name="Crate sensor calibration v1",
    filename="vizi-crate-cal.bin",
    size=32,
    sha256="20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3",
    source={"kind": "manual"},
)
OVERRIDE_SLOT = "calibration_override"
CHECK_FILE_SLOT = "crate_check_file"

#: A picker request waits for a person, not a host: SPEC.md §"Budgets" —
#: thirty minutes, so no user choosing a file can trip it while a request
#: orphaned by a dead host still eventually clears.
PICKER_TIMEOUT_S = 30 * 60.0

NO_CRATE_OPEN = "No crate open"
NOT_PROVISIONED = "(not provisioned)"
#: What `vox` says for a natively-read dataset: `ViewState` has no spacing
#: field, and `Geometry.spacing` is arranged only for sessions that declare a
#: tool (`control_plane_protocol.md` §8.7) — this plugin declares none.
NO_SPACING_FROM_HOST = "unknown — the host publishes no voxel size to a plugin without a tool"


# --- the panel model ----------------------------------------------------------
#
# The panel is patched from code paths that are not command handlers — a
# crate is opened by `OpenReader`, an export finishes in `FinishExport` — and
# every one of them has a handle of its own: the SDK constructs a reader or
# exporter class that takes one constructor parameter with a `PluginContext`
# (`panel(id)`, `overlay(id)`, `host`), the same handles a
# command gets.  The MODEL still lives here, because the host rebuilds the
# panel from the manifest on every open and persists nothing: `@panel_opened`
# re-pushes it, which is also how a crate opened before the panel was ever
# shown reaches it.  Every patch goes through `_push`, which applies it to the
# model and to the panel.

_LOCK = threading.Lock()
_MODEL = {"head": NO_CRATE_OPEN, "vox": "(unknown)", "cal": NOT_PROVISIONED, "status": "Ready", "verify": True}
_OPEN: "CrateReader | None" = None  # the reader serving a crate, for `crate.describe`'s direct path
_OVERRIDE: Path | None = None  # the runtime-slot calibration, once chosen

#: One describe at a time; the pickers have their own lock because a picker
#: can wait thirty minutes and should not block a describe.
_JOB_LOCK = threading.Lock()
_PICKER_LOCK = threading.Lock()


def _push(panel: PanelHandle, **fields) -> None:
    with _LOCK:
        _MODEL.update(fields)
    for widget, value in fields.items():
        panel.set_value(widget, value)


def _say(panel: PanelHandle, status: str) -> None:
    _push(panel, status=status)


def _refusing(method):
    """`CrateError` carries the `Error.kind` name SPEC.md assigns to a refusal;
    the SDK forwards a `Refusal(kind, message)` with that kind and any other
    exception as `IO_ERROR` (`reader_writer_contract.md` §1.5; `1.0` round
    21).  The container knows nothing about vizi, so the translation is here,
    at the boundary, on every method the SDK calls."""

    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except CrateError as exc:
            raise Refusal(pb2.Error.Kind.Value(exc.kind), exc.message) from exc

    return call


def _verify_on() -> bool:
    with _LOCK:
        return bool(_MODEL["verify"])


# --- the calibration table, two provisioning paths ------------------------------


def _manifest_asset() -> tuple[Path | None, str]:
    """The published table, or `None` and why.  The SDK's `asset_path`
    reads the launch environment's `VIZI_PLUGIN_ASSETS` — a JSON mapping
    `{asset_id: absolute path}` (`control_plane_protocol.md` §2.1), not a
    directory — and raises for an id the mapping lacks and for a declared
    file that is not there, with a message saying which."""
    try:
        return asset_path(CALIBRATION_ASSET), ""
    except AssetUnavailableError as exc:
        return None, str(exc)


def _calibration() -> tuple[Calibration | None, str, str]:
    """Override slot if populated, then the manifest asset, then nothing:
    the table, the panel's `cal` label saying which is live, and the detail
    behind a `(not provisioned)`.  A malformed table is absent, with a note,
    never a crash: the host verified the manifest asset's *digest* and
    nobody verified that its contents parse."""
    with _LOCK:
        override = _OVERRIDE
    notes = []
    if override is not None:
        try:
            return Calibration.load(override), f"override: {override.name}", ""
        except CrateError as exc:
            notes.append(f"override {override.name} ignored: {exc.message}")
    path, why = _manifest_asset()
    if path is not None:
        try:
            return Calibration.load(path), "manifest asset", "; ".join(notes)
        except CrateError as exc:
            notes.append(f"manifest asset ignored: {exc.message}")
    else:
        notes.append(why)
    return None, NOT_PROVISIONED, "; ".join(notes)


# --- the reader ------------------------------------------------------------------------


@reader("crate", name="Crate volume container", extensions=[".crate"])
class CrateReader:
    """One open crate.  The SDK constructs a fresh instance per `OpenReader`
    and opens it BEFORE closing the previous one (`reader_writer_contract.md`
    §1.2's handshake: a refused open leaves the previous reader
    untouched), refuses any level but 0 and any index past the declared shape
    before asking, and answers `NOT_FOUND` for a handle nothing is open
    under -- a replaced handle included."""

    def __init__(self, ctx: PluginContext) -> None:
        self._panel = ctx.panel(PANEL_ID)
        self._crate: Crate | None = None
        self._calibration: Calibration | None = None

    @_refusing
    def open(self, uri: str) -> ReaderInfo:
        global _OPEN
        crate = Crate.open(Path(uri))
        try:
            calibration, source, detail = _calibration()  # resolved before any read, never during one
            if crate.header.calibrated and calibration is None:
                raise CrateError(
                    "IO_ERROR",
                    f"{crate.name} stores calibrated counts and no calibration table is available — "
                    f"provision vizi-crate-cal.bin for this plugin in Plugin Manager ({detail})",
                )
        except BaseException:
            crate.close()
            raise
        self._crate = crate
        self._calibration = calibration
        with _LOCK:
            _OPEN = self
        h = crate.header
        notes = ""
        if h.malformed:
            notes = f"; malformed header {', '.join(h.malformed)} treated as unknown"
        if not crate.text_utf8:
            notes += "; metadata is not valid UTF-8 and is reported as no document"
        names = crate.channel_names()
        if names is None:
            notes += "; the channel names are malformed and none are declared"
        _push(
            self._panel,
            head=f"{crate.name}: {h.t_count}×{h.c_count}×{h.z}×{h.y}×{h.x} {h.dtype_name}"
            + (" (calibrated)" if h.calibrated else ""),
            vox=crate.spacing_label(),
            cal=source,
            status=f"Opened {crate.name}{notes}",
        )
        sz, sy, sx = h.spacing_zyx
        return ReaderInfo(
            shape_tczyx=h.shape_tczyx,
            dtype=h.dtype_name,
            spacing_zyx=(sz or None, sy or None, sx or None),
            spacing_unit="um",
            time_increment=h.time_increment or None,
            time_unit="s",
            axis_inference="crate",  # SPEC.md §4: one honest word for the host's log
            channel_names=names or (),  # "" for a channel the crate does not name
        )

    @_refusing
    def read_volume(self, t: int, c: int) -> np.ndarray:
        return self._live().read_block(t, c, verify=_verify_on(), calibration=self._calibration)

    @_refusing
    def read_plane(self, t: int, z: int, c: int) -> np.ndarray:
        # One seek, one short read, and no CRC: the checksum covers the whole
        # block, and a plane read exists to be cheap (SPEC.md §3).  The panel's
        # checkbox governs volume reads only, and says so.
        return self._live().read_plane(t, z, c, calibration=self._calibration)

    @_refusing
    def metadata(self) -> tuple[str, str]:
        # A DERIVED crate's document is its source's, kept as provenance
        # (SPEC.md §4): served as this file's it would misdescribe it.
        return self._live().served_metadata()

    @_refusing
    def close(self) -> None:
        # Called by the SDK on `CloseReader`, on the next `OpenReader` and at
        # shutdown (`reader_writer_contract.md` §1.8).  The next reader is
        # opened BEFORE this one is closed (§1.2 H2), so by the time a
        # replaced reader closes, its successor has already written the
        # panel: only the reader that is still the open one says "no crate
        # open" (SPEC.md, note 8).
        global _OPEN
        crate, self._crate = self._crate, None
        if crate is None:
            return
        crate.close()
        with _LOCK:
            was_open = _OPEN is self
            if was_open:
                _OPEN = None
        if was_open:
            _push(self._panel, head=NO_CRATE_OPEN, vox="(unknown)", status=f"Closed {crate.name}")

    def _live(self) -> Crate:
        if self._crate is None:
            raise CrateError("NOT_FOUND", "this reader is closed")
        return self._crate

    @property
    def crate(self) -> Crate | None:
        return self._crate

    @property
    def calibration(self) -> Calibration | None:
        return self._calibration


# --- the exporter ----------------------------------------------------------------------


@exporter("crate", name="Crate volume container", extension=".crate")
class CrateExporter:
    """The reader reversed.  `write(t, c, volume)` receives SOURCE indices
    (`reader_writer_contract.md` §2.3); the artifact index of each is its rank
    among the distinct values seen, in arrival order."""

    def __init__(self, ctx: PluginContext) -> None:
        self._panel = ctx.panel(PANEL_ID)
        self._writer: CrateWriter | None = None
        self._swap = False
        self._t_rank: dict[int, int] = {}
        self._c_rank: dict[int, int] = {}
        self._resolved = None

    @_refusing
    def begin(self, desc: ExportDescriptor) -> None:
        dtype = np.dtype(desc.dtype)
        if dtype not in CODE_OF_DTYPE:
            raise CrateError("UNSUPPORTED", f"dtype {dtype} has no crate encoding (u8, u16 and f32 do)")
        self._resolved = resolve_calibration(desc.spacing_zyx, desc.spacing_unit, desc.time_increment, desc.time_unit)
        # The source's document and the host's record are
        # what the artifact CAME FROM, kept as provenance under DERIVED; what
        # it IS is the header and the names, and a placeholder is no name.
        # `desc.provenance` is parsed by the SDK; the file keeps the record as
        # JSON (the wire's bytes are not exposed).
        record = json.dumps(desc.provenance).encode("utf-8") if desc.provenance is not None else b""
        self._writer = CrateWriter(
            Path(desc.locator), desc.shape_tczyx, dtype,
            desc.source_metadata_form, desc.source_metadata_text.encode("utf-8"),
            self._resolved.spacing_zyx, self._resolved.time_increment,
            flags=FLAG_DERIVED,
            provenance=record,
            names=[ch.name if ch.declared else "" for ch in desc.channels],
        )
        # `little_endian = false`: the SDK reinterpreted big-endian bytes in
        # native order, so the values are wrong-way-round; swapping them back
        # and writing native little-endian is the format's fixed layout.
        self._swap = not desc.little_endian
        self._writer.begin()
        if self._resolved.any_known and not self._resolved.all_from_fields:
            # SPEC.md §7: which values are unknown, when some but not all are,
            # so a healthy export is quiet.
            unknown = ", ".join(axis for axis, source in zip("zyxt", self._resolved.sources) if source == "unknown")
            print(f"{self._writer.path.name}: calibration unknown for {unknown}", file=sys.stderr)
        _say(self._panel, f"Exporting {self._writer.path.name}…")

    @_refusing
    def write(self, t: int, c: int, volume_zyx) -> None:
        writer = self._live()
        h = writer.header
        ta = _rank(self._t_rank, t, h.t_count, "timepoint")
        ca = _rank(self._c_rank, c, h.c_count, "channel")
        volume = np.asarray(volume_zyx)
        if self._swap:
            volume = volume.byteswap()
        writer.write_block(ta, ca, volume)

    @_refusing
    def finish(self) -> None:
        writer = self._live()
        try:
            writer.finish()
        except CrateError as exc:
            _say(self._panel, f"Export failed: {exc.message}")
            raise
        finally:
            self._writer = None
        resolved = self._resolved
        h = writer.header
        if resolved is not None and not resolved.any_known:
            _say(self._panel, f"Exported {writer.path.name} ({h.blocks} blocks) without voxel size — the host supplied no spacing.")
        else:
            _say(
                self._panel,
                f"Exported {writer.path.name} ({h.blocks} blocks); voxel size "
                f"{spacing_label(h.spacing_zyx, h.time_increment)}",
            )

    @_refusing
    def abort(self) -> None:
        # `AbortExport`, and at shutdown for an export the host will not
        # finish: the partial file is removed either way.
        writer, self._writer = self._writer, None
        if writer is not None:
            writer.abort()
            _say(self._panel, f"Export of {writer.path.name} cancelled; the partial file was removed.")

    def _live(self) -> CrateWriter:
        if self._writer is None:
            raise CrateError("NOT_FOUND", "no export is open on this handle")
        return self._writer


def _rank(ranks: dict[int, int], source: int, declared: int, axis: str) -> int:
    """Artifact index = rank among the distinct source indices seen.  One more
    distinct value than `OpenExport` declared contradicts the geometry the
    host itself stated — a host bug worth surfacing, not absorbing."""
    if source in ranks:
        return ranks[source]
    if len(ranks) >= declared:
        n = len(ranks) + 1
        suffix = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
        raise CrateError(
            "OUT_OF_BOUNDS",
            f"source {axis} {source} would be the {n}{suffix} distinct {axis} of an export declared with {declared}",
        )
    ranks[source] = len(ranks)
    return ranks[source]


# --- commands -----------------------------------------------------------------------------


@command("crate.set_verify", title="Verify CRC-32 on volume reads", menu=None)
def set_verify(ctx: CommandContext) -> str:
    panel = ctx.panel(PANEL_ID)
    value = ctx.args.get("value")
    if not isinstance(value, bool):
        raise ValueError(f"expected the checkbox's bool, got {ctx.args!r}")
    _push(panel, verify=value)
    return f"CRC-32 verification {'on' if value else 'off'} for volume reads (plane reads are never verified)"


@command("crate.describe", title="Describe active dataset", menu=None)
def describe(ctx: CommandContext) -> str:
    """Basic statistics of the active dataset — and which path had to read
    them: host data access, or this plugin's own reader when the host refuses
    (`UNSUPPORTED`) because the dataset is one this same process serves."""
    panel = ctx.panel(PANEL_ID)
    if not _JOB_LOCK.acquire(blocking=False):
        _say(panel, "Busy — a describe is already running.")
        raise RuntimeError("a describe is already running")
    host = ctx.host
    threading.Thread(target=_describe_work, args=(panel, host), daemon=True, name="crate-describe").start()
    return "Describing the active dataset; the result is on the Crate panel."


def _describe_work(panel: PanelHandle, host) -> None:
    try:
        view = host.view_state()
        shape = tuple(view.shape_tczyx)
        if len(shape) != 5 or any(extent == 0 for extent in shape):
            _say(panel, "No dataset is open.")
            return
        # The first channel shown, not the deprecated, always-0 `c`.
        channel = int(view.visible_channels[0]) if view.visible_channels else 0
        try:
            volume = host.read_volume(view.t, channel, level=0)
            path = "read via host data access"
            vox = NO_SPACING_FROM_HOST
        except HostError as exc:
            if exc.kind != pb2.Error.UNSUPPORTED:
                raise
            with _LOCK:
                serving = _OPEN
            crate = serving.crate if serving is not None else None
            if crate is None:
                _say(panel, f"Host data access refused ({exc}) and this plugin is not serving a crate.")
                return
            volume = crate.read_block(view.t, channel, verify=_verify_on(), calibration=serving.calibration)
            path = "read directly — host data access is unavailable for a dataset this plugin serves"
            vox = crate.spacing_label()
        stats = np.asarray(volume, dtype=np.float64)
        _push(
            panel,
            status=f"t={view.t} c={channel}: min {stats.min():g}, max {stats.max():g}, mean {stats.mean():.3f} ({path})",
            vox=vox,
        )
    except HostError as exc:
        _say(panel, f"Describe failed: {exc}")
    except Exception as exc:  # noqa: BLE001 — the panel is the only report channel
        _say(panel, f"Describe failed: {type(exc).__name__}: {exc}")
    finally:
        _JOB_LOCK.release()


@command("crate.pick_calibration", title="Use a different calibration", menu=None)
def pick_calibration(ctx: CommandContext) -> str:
    """Acknowledge, THEN prompt: the ten-second command budget is measuring
    the user's trip through a file dialog, not the 32-byte table."""
    panel = ctx.panel(PANEL_ID)
    if not _PICKER_LOCK.acquire(blocking=False):
        _say(panel, "A file picker is already open.")
        raise RuntimeError("a file picker is already open")
    host = ctx.host
    threading.Thread(target=_pick_calibration_work, args=(panel, host), daemon=True, name="crate-pick").start()
    return "Choose a calibration table; the outcome is on the Crate panel."


def _pick_calibration_work(panel: PanelHandle, host) -> None:
    global _OVERRIDE
    try:
        try:
            path = host.request_asset(OVERRIDE_SLOT, force_reprompt=True, timeout=PICKER_TIMEOUT_S)
        except HostError as exc:
            _say(panel, f"Calibration selection failed: {exc}")
            return
        if path is None:
            _say(panel, "Calibration unchanged — no file was chosen.")
            return
        try:
            table = Calibration.load(path)
        except CrateError as exc:
            _say(panel, f"{path.name} is not a calibration table ({exc.message}); the previous calibration stands.")
            return
        with _LOCK:
            _OVERRIDE = path
        _push(
            panel,
            cal=f"override: {path.name}",
            status=f"Calibration override set to {path.name} ({len(table.offsets)} channels); it applies to the next crate opened.",
        )
    finally:
        _PICKER_LOCK.release()


@command("crate.check_file", title="Check a crate file…", menu="tools", default_keybinding="Ctrl+Shift+K")
def check_file_command(ctx: CommandContext) -> str:
    """The menu path: no arguments, a host picker, whole-file validation —
    every outcome reaching the user through the panel alone."""
    panel = ctx.panel(PANEL_ID)
    if not _PICKER_LOCK.acquire(blocking=False):
        _say(panel, "A file picker is already open.")
        raise RuntimeError("a file picker is already open")
    host = ctx.host
    threading.Thread(target=_check_file_work, args=(panel, host), daemon=True, name="crate-check").start()
    return "Choose a crate to check; the result is on the Crate panel."


def _check_file_work(panel: PanelHandle, host) -> None:
    try:
        try:
            path = host.request_asset(CHECK_FILE_SLOT, force_reprompt=True, timeout=PICKER_TIMEOUT_S)
        except HostError as exc:
            _say(panel, f"Check failed: {exc}")
            return
        if path is None:
            return  # a clean cancellation does nothing and says nothing
        try:
            result = check_file(path)
        except CrateError as exc:
            _say(panel, f"{path.name}: {exc.message}")
            return
        _say(panel, result.summary(path.name))
    finally:
        _PICKER_LOCK.release()


@panel_opened(PANEL_ID)
def on_panel_opened(ctx: CommandContext) -> None:
    """The host rebuilds the panel from the manifest on every open and
    persists nothing; re-push the model — which is also how a crate opened
    before the panel was ever shown finally reaches it."""
    panel = ctx.panel(PANEL_ID)
    _, live, _ = _calibration()  # what the next open would use, which may have changed
    with _LOCK:
        _MODEL["cal"] = live
        snapshot = dict(_MODEL)
    for widget, value in snapshot.items():
        panel.set_value(widget, value)


# No `@on_shutdown` hook: the session's end drops every handle opened on it
# (SPEC.md §5), and the SDK does that itself -- the open reader's `close()`
# after the in-flight drain and before the report, and `abort()` on every
# export still open (`reader_writer_contract.md` §1.8).  The host sees
# `ShutdownReport NO_HOOK`.


panel(
    PANEL_ID,
    title="Crate",
    menu_label="Crate bridge…",
    root=w.column(
        "root",
        w.label("head", "No crate open"),
        w.label("status", "Ready"),
        w.row("rvox", w.label("lvox", "Voxel size"), w.label("vox", "(unknown)")),
        w.row("rcal", w.label("lcal", "Calibration"), w.label("cal", "(not provisioned)")),
        w.checkbox("verify", "Verify CRC-32 on volume reads", command=set_verify, value=True),
        w.button("describe", "Describe active dataset", command=describe),
        w.button("pickcal", "Use a different calibration…", command=pick_calibration),
    ),
)


if __name__ == "__main__":
    serve()
