"""Drive a ray-probe plugin end to end over the real transport, with no vizi.

    .venv/bin/python hostsim.py                       # the Python implementation
    .venv/bin/python hostsim.py --plugin-dir ../rust  # any implementation

`SPEC.md` §"Acceptance", in order, against a real process over real sockets:
a synthetic dataset with a three-level pyramid (sampling factors 1, 2, 4 --
block-mean, the odd trailing sample dropped, as vizi's), an anisotropic
spacing in µm, a time mapping, and one smooth bright blob whose peak the
driver knows.  Rays are composed here from a camera and a target, clipped to
the crop box the way the host clips them (`doorbell_E_input.md` §3); the
expected pick is found by the driver's OWN dense sampling of the segment,
never by the plugin's traversal.

Words asserted are only those `SPEC.md` pins: the `count` forms, the numerals
of §5, the substring `active`, the `(<s> s)` time form.  Everything else is
synchronised on observable state -- marker positions, the reads the emulator
recorded, the PNG on the profile panel, the `ContributeRois` batch.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import struct
import sys
import time
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))  # examples/, for vizi_hostsim
sys.path.insert(0, str(HERE))  # probe/, for the ellipsoid's display compile

from vizi_hostsim import (  # noqa: E402
    KIND_COLUMN_YX,
    SHUTDOWN_WINDOW_S,
    VIEW_2D,
    VIEW_3D,
    Column,
    Dataset,
    Host,
    Ray,
    Segment,
    Voxel,
    check,
    check_encoder,
    failures,
    segment_id,
)
from vizi_plugin_sdk import pb2  # noqa: E402

from probe.ellipsoid import compile_display  # noqa: E402  -- the same builder as peak_roi's

PANEL = "probe"
PROFILE = "profile"
TOOL = "probe.point"
PICKS = "probe.picks"
AIM = "probe.aim"

SHAPE_TCZYX = (3, 2, 32, 48, 64)
LEVELS = 3
SPACING_ZYX = (2.0, 0.5, 0.5)  # µm; anisotropic on purpose
TIME_INCREMENT_S = 1.25  # t = 2 -> 2.5 s
OPEN_GENERATION = (7 << 32) | 3  # more than 32 bits: `E.gen` carries the low ones
PEAK = {0: (18, 22, 42), 1: (9, 30, 20)}  # per channel, level-0 voxel
SIGMA = (3.0, 5.0, 5.0)
BACKGROUND = 100

PICKS_SEG = segment_id(1)
AIM_SEG = segment_id(2)
PANEL_SEG = segment_id(3)
PROFILE_SEG = segment_id(4)
LANE_SLOT = 16 << 10


def volume(t: int, c: int) -> np.ndarray:
    """A smooth blob per channel, scaled by the timepoint, over a background
    with a small asymmetric ramp so no two voxels on a ray tie exactly."""
    _, _, depth, height, width = SHAPE_TCZYX
    z, y, x = np.meshgrid(np.arange(depth), np.arange(height), np.arange(width), indexing="ij")
    pz, py, px = PEAK[c]
    blob = np.exp(-(((z - pz) / SIGMA[0]) ** 2 + ((y - py) / SIGMA[1]) ** 2 + ((x - px) / SIGMA[2]) ** 2) / 2.0)
    ramp = 0.011 * x + 0.007 * y + 0.003 * z
    return np.rint(BACKGROUND + (2000 + 400 * t) * blob + ramp).astype(np.uint16)


DATASET = Dataset(
    shape_tczyx=SHAPE_TCZYX,
    np_dtype=np.dtype(np.uint16),
    wire_dtype=pb2.DTYPE_U16,
    open_generation=OPEN_GENERATION,
    volume=volume,
    t=2,
    z=18,
    c=0,
    levels=LEVELS,
    spacing_zyx=SPACING_ZYX,
    time_increment_s=TIME_INCREMENT_S,
    dataset_uri="hostsim://blob",
    dataset_name="blob",
)


# --- geometry the driver does for itself ------------------------------------


def to_level(coord, level: int) -> tuple[float, float, float]:
    """A level-0 lattice coordinate at ``level`` (§8.11's map inverted)."""
    s = 2**level
    return tuple((v + 0.5) / s - 0.5 for v in coord)


def level_shape(level: int) -> tuple[int, int, int]:
    s = 2**level
    return tuple(max(1, n // s) for n in SHAPE_TCZYX[2:])


def clip_box(level: int) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """The crop box (the whole dataset here) in edge coordinates at ``level``."""
    return (-0.5, -0.5, -0.5), tuple(n - 0.5 for n in level_shape(level))


def ray_through(level: int, camera0, target0) -> Ray:
    """A `RAY` record's layout at ``level`` for a camera and a point it looks
    at, both given at level 0: `far` is the exit of the clip box, `entry_frac`
    locates the entry, roll 0."""
    cam = np.array(to_level(camera0, level), dtype=np.float64)
    tgt = np.array(to_level(target0, level), dtype=np.float64)
    d = tgt - cam
    lo, hi = (np.array(b, dtype=np.float64) for b in clip_box(level))
    t_in, t_out = 0.0, math.inf
    for a in range(3):
        if d[a] == 0:
            assert lo[a] <= cam[a] <= hi[a], "a ray parallel to a face must start inside that slab"
            continue
        t0, t1 = sorted(((lo[a] - cam[a]) / d[a], (hi[a] - cam[a]) / d[a]))
        t_in, t_out = max(t_in, t0), min(t_out, t1)
    assert t_in < t_out, "the ray misses the box; use miss_ray"
    far = cam + t_out * d
    entry_frac = t_in / t_out
    return Ray(Voxel(*map(float, cam)), Voxel(*map(float, far)), float(entry_frac))


def miss_ray(level: int) -> Ray:
    """A ray beside the box: `far` beyond the data, `entry_frac` at the closest approach."""
    cam = np.array(to_level((18.0, 22.0, -40.0), level), dtype=np.float64)
    direction = np.array([0.0, 1.0, 0.0])  # along +y, at x = -40: never meets the box
    far = cam + 200.0 * direction
    return Ray(Voxel(*map(float, cam)), Voxel(*map(float, far)), 0.4)


def expected_pick(level: int, ray: Ray, t: int, c: int, refine: bool):
    """The driver's own answer: dense sampling of the segment, the first
    maximum along it, then the block maximum at level 0 -- independent of
    any traversal the plugin wrote."""
    cam = np.array([ray.camera.z, ray.camera.y, ray.camera.x])
    far = np.array([ray.far.z, ray.far.y, ray.far.x])
    entry = cam + ray.entry_frac * (far - cam)
    shape = level_shape(level)
    vol = DATASET.volume_at(t, c, level)
    seen, cells = set(), []
    for u in np.linspace(0.0, 1.0, 40000):
        p = entry + u * (far - entry)
        cell = tuple(int(math.floor(v + 0.5)) for v in p)
        if cell in seen or any(not 0 <= cell[a] < shape[a] for a in range(3)):
            continue
        seen.add(cell)
        cells.append(cell)
    values = [float(vol[cell]) for cell in cells]
    i = max(range(len(values)), key=lambda k: (values[k], -k))
    coarse, coarse_value = cells[i], values[i]
    s = 2**level
    if level == 0 or not refine:
        position = tuple(float((v + 0.5) * s - 0.5) for v in coarse)
        return position, coarse_value, coarse, (cells, values)
    lo = tuple(v * s for v in coarse)
    hi = tuple(min(v * s + s, n) for v, n in zip(coarse, SHAPE_TCZYX[2:]))
    block = DATASET.volume_at(t, c, 0)[lo[0] : hi[0], lo[1] : hi[1], lo[2] : hi[2]]
    k, j, i2 = np.unravel_index(int(np.argmax(block)), block.shape)
    fine = (lo[0] + int(k), lo[1] + int(j), lo[2] + int(i2))
    return tuple(float(v) for v in fine), float(block[k, j, i2]), coarse, (cells, values)


# --- helpers -------------------------------------------------------------------


_NUMBER = re.compile(r"[-+]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?")


def numbers_in(text: str) -> list[float]:
    """Every number a line contains, however the plugin printed it -- the
    driver compares VALUES, not Python's `%g` spelling."""
    return [float(m) for m in _NUMBER.findall(text)]


def names_number(text: str, value: float, rel: float = 1e-6) -> bool:
    return any(abs(x - value) <= rel * max(1.0, abs(value)) for x in numbers_in(text))


def await_until(ok, timeout: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout
    while not ok() and time.monotonic() < deadline:
        time.sleep(0.02)
    return bool(ok())


def statuses_since(host: Host, index: int, panel: str = PANEL, widget: str = "status") -> list[str]:
    return [str(v) for p, w, v in host.patches[index:] if p == panel and w == widget and not isinstance(v, (pb2.PatchOp, bytes))]


def await_status_set(host: Host, patches_before: int, timeout: float = 10.0) -> str | None:
    """The status line once it has been set again since ``patches_before``
    (the same words count), or None at the deadline -- the bar for wording
    the specification leaves free."""
    if await_until(lambda: statuses_since(host, patches_before), timeout):
        return host.status_of(PANEL)
    return None


def updates_on(host: Host, overlay_id: str):
    return [p for p in host.pushes if p.kind == "update" and p.overlay_id == overlay_id]


def cleared_since(host: Host, overlay_id: str, index: int) -> bool:
    """Whether a push since ``host.pushes[index]`` retracted the overlay:
    an `OverlayRemove`, or an `OverlayUpdate` with no items -- §8.5 makes
    the update a full replacement, so either empties it."""
    for p in host.pushes[index:]:
        if p.overlay_id != overlay_id:
            continue
        if p.kind == "remove" or (p.kind == "update" and not list(p.update.items)):
            return True
    return False


def positions_of(buf: pb2.GeomBuffer, loans=None) -> np.ndarray:
    """A geometry buffer's positions.  Inline always; a segment-backed one
    only when ``loans`` (the push's, which the host read out of the arena)
    are given -- a control-plane `set_items` may carry either (§5.6), but a
    live lane value is inline by rule (§3.3), so live callers pass none."""
    assert buf.dtype == pb2.DTYPE_F64, buf.dtype
    source = buf.WhichOneof("source")
    if source == "inline":
        data = buf.inline
    else:
        assert source == "segment" and loans is not None, source
        ref = buf.segment
        found = [l for l in loans if (l.segment, l.offset, l.length) == (ref.segment_id, ref.offset, ref.len)]
        assert found, f"no loan read for {ref}"
        data = found[0].payload
    return np.frombuffer(data, dtype="<f8").reshape(tuple(buf.shape))


def picks_pushed(host: Host, count: int, timeout: float = 10.0) -> tuple[np.ndarray, list[str]]:
    """Wait for a `probe.picks` update whose marker carries ``count``
    positions; return them and the text labels beside them.  ``count == 0``
    waits for a remove or an empty update."""
    seen = len(host.pushes)

    def decode(p):
        """One `probe.picks` push as (kind, (positions, labels)), or None."""
        if p.overlay_id != PICKS:
            return None
        if p.kind == "remove":
            return ("remove", None)
        items = list(p.update.items)
        markers = [it for it in items if it.WhichOneof("geometry") == "marker"]
        pos = positions_of(markers[0].marker.positions_zyx, p.loans) if markers else np.zeros((0, 3))
        return ("update", (pos, [it.text.text for it in items if it.WhichOneof("geometry") == "text"]))

    def latest():
        for p in reversed(host.pushes):
            got = decode(p)
            if got is not None:
                return got
        return None

    def matches(got) -> bool:
        if got is None:
            return False
        if count == 0:
            return got[0] == "remove" or len(got[1][0]) == 0
        return got[0] == "update" and len(got[1][0]) == count

    def ready():
        # The NEWEST push is not the only one that counts.  The overlay lane
        # coalesces and a plugin can get ahead of the driver, so a state the
        # scenario means to observe may already have been replaced by the time
        # we look; requiring `latest()` to match made this unsatisfiable
        # whenever that happened rather than when the plugin was wrong.
        return any(matches(decode(p)) for p in host.pushes[seen:]) or matches(latest())

    assert await_until(ready, timeout), f"no probe.picks push with {count} positions (pushes since: {[(p.kind, p.overlay_id) for p in host.pushes[seen:]]}; status {host.status_of(PANEL)!r})"
    got = next((decode(p) for p in reversed(host.pushes[seen:]) if matches(decode(p))), latest())
    if got[0] == "remove":
        return np.zeros((0, 3)), []
    return got[1]


def profile_image(host: Host, index_before: int):
    """The newest PNG pushed on the profile panel's `plot` since ``index_before``."""
    images = [(p, w, loan) for p, w, loan in host.images[index_before:] if p == PROFILE and w == "plot"]
    return images[-1][2] if images else None


def reads_since(host: Host, index: int):
    return host.read_requests[index:]


def roi_within(roi, lo, hi) -> bool:
    """``roi`` (min inclusive, max exclusive) lies inside ``[lo, hi)``."""
    if roi is None:
        return False
    (a, b) = roi
    return all(lo[k] <= a[k] and b[k] <= hi[k] and a[k] < b[k] for k in range(3))


def segment_box(level: int, ray: Ray):
    """The segment's own bounding box in level cells, min inclusive max exclusive."""
    cam = np.array([ray.camera.z, ray.camera.y, ray.camera.x])
    far = np.array([ray.far.z, ray.far.y, ray.far.x])
    entry = cam + ray.entry_frac * (far - cam)
    lo = tuple(int(math.floor(min(a, b) + 0.5)) for a, b in zip(entry, far))
    hi = tuple(int(math.floor(max(a, b) + 0.5)) + 1 for a, b in zip(entry, far))
    shape = level_shape(level)
    return tuple(max(0, v) for v in lo), tuple(min(v, n) for v, n in zip(hi, shape))


def unexpected_violations(host: Host) -> str | None:
    site, allowed = getattr(host, "expected_unusable", (None, 0))
    named = [(pb2.ViolationSite.Name(v.where), v.detail) for v in host.violations]
    elsewhere = [n for n in named if site is None or n[0] != pb2.ViolationSite.Name(site)]
    if elsewhere:
        return f"reports at a site the run does not provoke: {elsewhere}"
    if len(named) > allowed:
        return f"{len(named)} unusable_context reports where the undeclared-level records explain at most {allowed}: {named}"
    return None


# --- scenarios -----------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", type=Path, default=HERE, help="directory holding the vizi-plugin.json to launch")
    plugin_dir = parser.parse_args().plugin_dir

    check("record encoder", check_encoder)
    if failures:
        print("\nthe encoder disagrees with the corpus; nothing else was run", file=sys.stderr)
        return 1

    host = Host(plugin_dir, DATASET)
    host.add(Segment.overlay(PICKS_SEG, PICKS, 4 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.overlay(AIM_SEG, AIM, 4 << 20, host.directory, slot_bytes=LANE_SLOT))
    host.add(Segment.plugin_arena(PANEL_SEG, "P", 1 << 20, host.directory, subject=PANEL))
    host.add(Segment.plugin_arena(PROFILE_SEG, "P", 1 << 20, host.directory, subject=PROFILE))
    # No `V` here: the emulator grants it the `1.0` host's way -- at the first
    # read, sized to it, grown by supersede (`Host._v_for`).

    with host:
        state: dict = {"tool": 0, "picks": []}
        t, c = DATASET.t, 0

        def test_handshake():
            return host.check_handshake()

        def test_manifest():
            """Acceptance 1: what the manifest declares, and the icons."""
            m = json.loads((plugin_dir / "vizi-plugin.json").read_text(encoding="utf-8"))
            assert m.get("manifest_version") == 1, m.get("manifest_version")
            assert set(m.get("permissions", [])) >= {"data_access", "contribute_rois"}
            cont = m["contributions"]
            tools = cont.get("tools", [])
            assert len(tools) == 1 and tools[0]["id"] == TOOL, tools
            tool_ = tools[0]
            # manifest_schema.md §3.6: an absent `views` is both panes.
            assert set(tool_.get("views", ["2d", "3d"])) == {"2d", "3d"}, tool_.get("views")
            assert tool_.get("mode", "gesture") == "gesture" and tool_.get("cancel_on_camera_move") is False, tool_
            trig = tool_.get("triggers", [])
            assert len(trig) == 2 and any("key" in x for x in trig) and any("button" in x for x in trig), trig
            # vizi binds P (and Space) to Play/Pause, so the key is Shift+P.
            assert [x["key"] for x in trig if "key" in x] == ["Shift+P"], trig
            actions = tool_.get("actions", [])
            assert [a["id"] for a in actions] == ["undo", "clear"], actions
            assert all(a.get("key") and a.get("pane", {}).get("icon") for a in actions), actions
            assert tool_.get("pane", {}).get("icon"), "the tool has no pane button"
            for icon in [tool_["pane"]["icon"]] + [a["pane"]["icon"] for a in actions]:
                path = plugin_dir / icon
                assert path.is_file() and path.suffix == ".svg" and path.stat().st_size <= 64 << 10, icon
                assert b"<svg" in path.read_bytes()[:512], f"{icon} is not SVG"
            overlays = {o["id"] for o in cont.get("overlays", [])}
            assert overlays == {PICKS, AIM}, overlays
            panels = {p["id"] for p in cont.get("panels", [])}
            assert panels == {PANEL, PROFILE}, panels
            return "one tool (2 triggers, 2 actions with keys and pane buttons, a pane button), 2 overlays, 2 panels, both permissions, 3 SVG icons"

        def test_grants():
            for seg in (PICKS_SEG, AIM_SEG, PANEL_SEG):
                ack = host.grant(seg)
                assert ack.WhichOneof("outcome") == "installed", (seg, ack)
            return "O(probe.picks), O(probe.aim), P(probe) installed; P(profile) waits for its panel to open, as vizi does"

        def test_panels_opened():
            host.open_panel(PANEL)
            assert await_until(lambda: host.status_of(PANEL, "count") == "No picks"), host.status_of(PANEL, "count")
            assert host.status_of(PANEL) != "", "no status pushed on panel open"
            return f"probe: status {host.status_of(PANEL)!r}, count 'No picks'; profile still closed"

        def test_arrangements():
            """Acceptance 2: both triggers and both action keys honoured; the
            status says the tool is active (the hook, or the first-record
            fallback -- either, and say which)."""
            before = len(host.patches)
            state["tool"] = host.arrange(
                TOOL,
                triggers=["Shift+P", (pb2.MOUSE_BUTTON_MIDDLE, 0)],
                actions=[("undo", "U"), ("clear", "X")],
                cancel_on_camera_move=False,
            )
            assert host.tool_acks[state["tool"]] == 1 and host.epoch_acks == {VIEW_2D: 1, VIEW_3D: 1}, host.epoch_acks
            assert host.mapping_acks[OPEN_GENERATION] == 1
            status = await_status_set(host, before, timeout=3.0)
            state["activated_hook"] = status is not None and "active" in status.lower()
            if state["activated_hook"]:
                # SPEC.md §1: the honoured triggers and keys are said at activation;
                # reported, and asserted only that the hook spoke.
                keys = [k for k in ("Shift+P", "U", "X") if k in status]
                return f"tool handle {state['tool']}, panes at epoch 1; status {status!r} -- the activated hook{' names ' + ', '.join(keys) if keys else ' (no keys named)'}"
            return f"tool handle {state['tool']}, panes at epoch 1; no status yet -- the tool says so on its first record"

        def run_ray(level: int, ray: Ray, refine: bool, expect_reads: bool = True, drags: int = 0, view: int = VIEW_3D, channels: int | None = None):
            """press [drag...] release with ``ray`` at ``level``; return the
            pick's expected answer and what was observed."""
            fields = {"level": level}
            if channels is not None:
                fields["channels"] = channels
            live_before, reads_before, count_before = len(host.live), len(host.read_requests), len(state["picks"])
            patches_before = len(host.patches)
            gesture = host.press(state["tool"], view, ray, **fields)
            for _ in range(drags):
                gesture.drag(ray, **fields)
                time.sleep(0.01)
            gesture.release(ray, **fields)
            return live_before, reads_before, count_before, patches_before

        def pick_lands(count_before: int, expected_position, expected_value: float):
            positions, labels = picks_pushed(host, count_before + 1)
            assert np.allclose(positions[-1], expected_position, atol=1e-6), (positions[-1], expected_position)
            assert labels == [str(i + 1) for i in range(count_before + 1)], labels
            state["picks"].append(tuple(float(v) for v in positions[-1]))
            n = count_before + 1
            assert await_until(lambda: host.status_of(PANEL, "count") == (f"{n} pick" if n == 1 else f"{n} picks")), host.status_of(PANEL, "count")
            # §5 sets the status LAST; wait for it rather than read the previous one
            def status_names_the_pick() -> bool:
                st = host.status_of(PANEL)
                return names_number(st, n) and names_number(st, expected_value)

            assert await_until(status_names_the_pick), (host.status_of(PANEL), n, expected_value)
            return positions, labels, host.status_of(PANEL)

        def test_ray_level0():
            """Acceptance 3 and 6: an axis-aligned ray through the blob at
            level 0, with drags -- the aim on the live lane, the pick, the
            marker and its label, the count, the status, the profile."""
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, coarse, _ = expected_pick(0, ray, t, c, True)
            assert tuple(int(v) for v in expected) == PEAK[0], (expected, PEAK[0])
            images_before = len(host.images)
            pushes_before = len(host.pushes)
            live_before, reads_before, count_before, patches_before = run_ray(0, ray, True, drags=2)
            live = host.await_live(live_before + 1)
            aim = [v for v in live[live_before:] if v.segment == AIM_SEG]
            # the lane coalesces: three publishes may be one read; the newest is the one that matters
            assert aim and all(v.applied for v in aim), [(v.applied, v.reason) for v in aim]
            line = aim[-1].update.items[0]
            assert line.WhichOneof("geometry") == "line_set", line
            pts = positions_of(line.line_set.positions_zyx)
            entry = np.array([ray.camera.z, ray.camera.y, ray.camera.x]) + ray.entry_frac * (
                np.array([ray.far.z, ray.far.y, ray.far.x]) - np.array([ray.camera.z, ray.camera.y, ray.camera.x])
            )
            assert pts.shape == (2, 3) and np.allclose(pts[0], entry, atol=1e-4) and np.allclose(pts[1], [ray.far.z, ray.far.y, ray.far.x], atol=1e-4), pts
            assert aim[-1].pane == (VIEW_3D, host.epochs[VIEW_3D]), aim[-1].pane
            positions, labels, status = pick_lands(count_before, expected, value)
            # the aim is cleared after release: a remove, or an empty update
            assert await_until(lambda: cleared_since(host, AIM, pushes_before)), "the aim was not cleared after release"
            # one read at level 0, within the segment's box
            reads = reads_since(host, reads_before)
            assert len(reads) == 1 and reads[0].level == 0 and reads[0].t == t and reads[0].c == c, reads
            lo, hi = segment_box(0, ray)
            assert roi_within(reads[0].roi, lo, hi), (reads[0].roi, lo, hi)
            # the profile panel: closed during the pick.  The pick
            # reported itself, nothing was refused, and no image reached a panel
            # whose segment is not granted; opening it delivers the plot.
            assert "Failed" not in status, f"a closed profile panel failed the pick: {status!r}"
            refused = [r for r in host.refusals if PROFILE in str(r)]
            assert not refused, f"a push to the closed profile panel was refused: {refused}"
            assert profile_image(host, images_before) is None, "an image reached the profile panel before it opened"
            # vizi's order: P granted and PanelOpened sent
            # in one step, with no wait for the GrantAck -- so PanelOpened can
            # reach the plugin before P is installed, and a held image must
            # still arrive.
            host.open_panel(PROFILE, grant=PROFILE_SEG)
            assert await_until(lambda: profile_image(host, images_before) is not None), "no PNG on the profile panel once it opened (an SDK should hold it, §8.4, and send it once P is installed)"
            loan = profile_image(host, images_before)
            assert loan.payload[:8] == b"\x89PNG\r\n\x1a\n", loan.payload[:8]
            width, height = struct.unpack(">II", loan.payload[16:24])
            label = host.status_of(PROFILE, "pick")
            assert names_number(label, 1) and "c=0" in label and "t=2" in label, label
            seconds = re.search(r"\(\s*([-+0-9.eE]+)\s*s\)", label)
            assert seconds and abs(float(seconds.group(1)) - 2.5) < 1e-9, f"no '(<seconds> s)' naming 2.5 in {label!r}"
            assert all(names_number(label, v) for v in expected), (label, expected)
            return f"pick 1 at {tuple(int(v) for v in expected)} value {value:g}; aim live then cleared; 1 read at level 0 roi {reads[0].roi}; PNG {width}x{height}; {label!r}"

        def test_ray_level2_refined():
            """Acceptance 4: the same ray at level 2 -- two reads, the coarse
            one within the segment's box at level 2, the fine one within one
            block; the pick is the exact peak."""
            ray = ray_through(2, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, coarse, _ = expected_pick(2, ray, t, c, True)
            assert tuple(int(v) for v in expected) == PEAK[0], (expected, PEAK[0])
            live_before, reads_before, count_before, _ = run_ray(2, ray, True)
            pick_lands(count_before, expected, value)
            reads = reads_since(host, reads_before)
            assert [r.level for r in reads] == [2, 0], [(r.level, r.roi) for r in reads]
            lo, hi = segment_box(2, ray)
            assert roi_within(reads[0].roi, lo, hi), (reads[0].roi, lo, hi)
            block_lo = tuple(v * 4 for v in coarse)
            block_hi = tuple(min(v * 4 + 4, n) for v, n in zip(coarse, SHAPE_TCZYX[2:]))
            assert roi_within(reads[1].roi, block_lo, block_hi), (reads[1].roi, block_lo, block_hi)
            live = [v for v in host.live[live_before:] if v.segment == AIM_SEG]
            pts = positions_of(live[-1].update.items[0].line_set.positions_zyx)
            assert np.allclose(pts[1], [18.0, 22.0, 63.5], atol=1e-4), pts  # the far point in level-0 coordinates
            return f"coarse {coarse} at level 2 (roi {reads[0].roi}), refined to {tuple(int(v) for v in expected)} (roi {reads[1].roi}); aim in level-0 coordinates"

        def test_ray_level2_unrefined():
            """Acceptance 4, second half: refinement off -- one read, the pick
            is the coarse voxel's centre."""
            assert host.invoke("probe.set_refine", value=False).ok
            ray = ray_through(2, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, coarse, _ = expected_pick(2, ray, t, c, False)
            live_before, reads_before, count_before, _ = run_ray(2, ray, False)
            pick_lands(count_before, expected, value)
            reads = reads_since(host, reads_before)
            assert [r.level for r in reads] == [2], [(r.level, r.roi) for r in reads]
            assert host.invoke("probe.set_refine", value=True).ok
            return f"one read at level 2; pick = the coarse centre {expected}, value {value:g}"

        def test_oblique():
            """Acceptance 5: an oblique ray through the blob; the pick agrees
            with the driver's own dense sampling."""
            ray = ray_through(1, (2.0, 40.0, 10.0), (20.0, 20.0, 50.0))
            expected, value, coarse, _ = expected_pick(1, ray, t, c, True)
            live_before, reads_before, count_before, _ = run_ray(1, ray, True)
            pick_lands(count_before, expected, value)
            reads = reads_since(host, reads_before)
            assert [r.level for r in reads] == [1, 0], [(r.level, r.roi) for r in reads]
            return f"oblique at level 1: coarse {coarse}, pick {tuple(int(v) for v in expected)} value {value:g} -- agrees with dense sampling"

        def test_miss():
            """Acceptance 7: a MISS ray -- no pick, the status moved, no aim."""
            ray = miss_ray(0)
            live_before, reads_before, count_before = len(host.live), len(host.read_requests), len(state["picks"])
            patches_before = len(host.patches)
            gesture = host.press(state["tool"], VIEW_3D, ray, level=0, miss=True)
            gesture.drag(ray, level=0, miss=True)
            gesture.release(ray, level=0, miss=True)
            status = await_status_set(host, patches_before)
            assert status is not None, "a MISS said nothing"
            time.sleep(0.2)
            assert len(state["picks"]) == count_before and host.status_of(PANEL, "count") == f"{count_before} picks"
            assert not [v for v in host.live[live_before:] if v.segment == AIM_SEG and v.applied and v.update.items], "an aim was published for a MISS"
            assert len(reads_since(host, reads_before)) == 0, "a MISS caused a read"
            return f"no pick, no aim, no read; status {status!r}"

        def test_2d_voxel_and_column():
            """Acceptance 8: a 2D voxel is the pick; a projection column's maximum is."""
            vox = (18.0, 22.0, 40.0)
            vol = DATASET.volume_at(t, c, 0)
            live_before, reads_before, count_before, _ = run_ray(0, Voxel(*vox), True, view=VIEW_2D)
            pick_lands(count_before, vox, float(vol[18, 22, 40]))
            reads = reads_since(host, reads_before)
            assert len(reads) == 1 and reads[0].roi == ((18, 22, 40), (19, 23, 41)), reads
            col = Column(KIND_COLUMN_YX, 22.0, 40.0, 0.0, 32.0)
            column = vol[:, 22, 40]
            zbest = int(np.argmax(column))
            live_before, reads_before, count_before, _ = run_ray(0, col, True, view=VIEW_2D)
            pick_lands(count_before, (float(zbest), 22.0, 40.0), float(column[zbest]))
            reads = reads_since(host, reads_before)
            assert len(reads) == 1 and reads[0].roi == ((0, 22, 40), (32, 23, 41)), reads
            aims = [v for v in host.live[live_before:] if v.segment == AIM_SEG and v.applied]
            assert aims, "no aim for a column"
            pts = positions_of(aims[-1].update.items[0].line_set.positions_zyx)
            assert np.allclose(pts, [[0.0, 22.0, 40.0], [31.0, 22.0, 40.0]], atol=1e-6), pts
            return f"voxel {tuple(int(v) for v in vox)} picked as itself; column (y=22, x=40) -> z={zbest}; aim = the column"

        def test_actions_mid_drag():
            """Acceptance 9: `undo` fired into an open gesture removes the last
            pick and the gesture still picks on release; `clear` empties."""
            n = len(state["picks"])
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, _, _ = expected_pick(0, ray, t, c, True)
            gesture = host.press(state["tool"], VIEW_3D, ray, level=0)
            gesture.drag(ray, level=0)
            host.fire(state["tool"], VIEW_3D, ray, level=0, action=1)  # undo, inside the gesture
            positions, labels = picks_pushed(host, n - 1)
            state["picks"].pop()
            assert labels == [str(i + 1) for i in range(n - 1)], labels
            gesture.drag(ray, level=0)
            gesture.release(ray, level=0)
            pick_lands(n - 1, expected, value)
            host.fire(state["tool"], VIEW_3D, ray, level=0, action=2)  # clear
            picks_pushed(host, 0)
            state["picks"].clear()
            assert await_until(lambda: host.status_of(PANEL, "count") == "No picks"), host.status_of(PANEL, "count")
            return "undo mid-drag removed the last pick, the release picked again; clear emptied the list"

        def test_buttons():
            """Acceptance 10: the panel's undo and clear do the same."""
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, _, _ = expected_pick(0, ray, t, c, True)
            for _ in range(2):
                _, _, count_before, _ = run_ray(0, ray, True)
                pick_lands(count_before, expected, value)
            assert host.invoke("probe.undo").ok
            picks_pushed(host, 1)
            state["picks"].pop()
            assert await_until(lambda: host.status_of(PANEL, "count") == "1 pick")
            assert host.invoke("probe.clear").ok
            picks_pushed(host, 0)
            state["picks"].clear()
            assert await_until(lambda: host.status_of(PANEL, "count") == "No picks")
            return "probe.undo -> 1 pick; probe.clear -> No picks"

        def test_contribute():
            """Acceptance 11: the batch, replace_all, names, ids, centres, the radius."""
            ray0 = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            e0, v0, _, _ = expected_pick(0, ray0, t, c, True)
            ray1 = ray_through(1, (2.0, 40.0, 10.0), (20.0, 20.0, 50.0))
            e1, v1, _, _ = expected_pick(1, ray1, t, c, True)
            for level, ray, e, v in ((0, ray0, e0, v0), (1, ray1, e1, v1)):
                _, _, count_before, _ = run_ray(level, ray, True)
                pick_lands(count_before, e, v)
            assert host.invoke("probe.set_radius", value="2").ok
            patches_before = len(host.patches)
            assert host.invoke("probe.contribute").ok
            batch = host.contributions.get(timeout=10.0)
            assert batch.replace_all is True
            assert [r.contrib_id for r in batch.rois] == ["pick-1", "pick-2"], [r.contrib_id for r in batch.rois]
            assert [r.name for r in batch.rois] == ["Pick 1", "Pick 2"]
            def centre_and_tips(roi):
                v = roi.vertices[0].position
                c0 = np.array([v.z, v.y, v.x])
                return c0, np.array(compile_display(roi))[:6]

            for r, centre in zip(batch.rois, state["picks"]):
                assert r.roi.type_name == "ellipsoid"
                c0, tips = centre_and_tips(r.roi)
                assert np.allclose(c0, centre, atol=1e-6), (c0, centre)
                assert np.allclose(np.abs(tips - c0).max(axis=0), [2.0, 2.0, 2.0], atol=1e-6), tips - c0
                assert not r.roi.HasField("t_start") and not r.roi.HasField("t_end"), "a pick's ROI is timeless"
            status = await_status_set(host, patches_before)
            assert status is not None and names_number(status, 2), status
            done = host.invoke("probe.set_radius", value="not a number")
            assert not done.ok and done.message.strip(), done
            mark = len(host.patches)
            assert host.invoke("probe.contribute").ok
            again = host.contributions.get(timeout=10.0)
            # Let this contribution's own status land here, not in the next
            # scenario's window: the Python implementation sets it after the
            # contribution, and the generation scenario would otherwise read
            # it as its own answer ("Contributed 2 ROIs").
            await_status_set(host, mark, timeout=5.0)
            c0, tips = centre_and_tips(again.rois[0].roi)
            assert np.allclose(np.abs(tips - c0).max(axis=0), [2.0, 2.0, 2.0], atol=1e-6), "the refused radius changed the stored one"
            return f"2 ellipsoids named Pick 1/2, contrib_id pick-1/2, radius 2, replace_all; status {status!r}; a bad radius refused and the stored one stands"

        def test_generation_changed():
            """Acceptance 12: the dataset's generation moves between the record
            and its read -- no pick, the status says so."""
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            host.hold_reads()
            try:
                _, reads_before, count_before, patches_before = run_ray(0, ray, True)
                host.await_reads(1)  # the read is parked: the generation moves under it
                host.dataset.open_generation = OPEN_GENERATION + 1
            finally:
                host.release_reads()
            try:
                status = await_status_set(host, patches_before, timeout=5.0)
                time.sleep(0.2)
            finally:
                host.dataset.open_generation = OPEN_GENERATION
            assert status is not None, "the generation change said nothing"
            assert len(state["picks"]) == count_before and host.status_of(PANEL, "count") == "2 picks", host.status_of(PANEL, "count")
            assert len(reads_since(host, reads_before)) >= 1
            return f"no pick; status {status!r}"

        def test_refusals():
            """Acceptance 13: no visible channel; an undeclared level (the SDK
            reports unusable_context, the tool refuses)."""
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            _, reads_before, count_before, patches_before = run_ray(0, ray, True, channels=0)
            status = await_status_set(host, patches_before)
            assert status is not None, "no visible channel said nothing"
            time.sleep(0.1)
            assert len(reads_since(host, reads_before)) == 0 and len(state["picks"]) == count_before
            first = status
            before_v = len(host.violations)
            patches_before = len(host.patches)
            g = host.press(state["tool"], VIEW_3D, ray_through(0, (18.0, 22.0, -30.0), PEAK[0]), level=5)
            g.release(ray_through(0, (18.0, 22.0, -30.0), PEAK[0]), level=5)
            reported = host.await_violations("unusable_context")
            assert reported, "no unusable_context report"
            host.expected_unusable = (reported[-1].where, 2)
            status = await_status_set(host, patches_before)
            assert status is not None, "an unusable record said nothing"
            assert len(reads_since(host, reads_before)) == 0 and len(state["picks"]) == count_before
            assert not [v for v in host.violations[before_v:] if v.where != reported[-1].where]
            return f"no channel: {first!r}; undeclared level: {status!r}, unusable_context reported, no read"

        def test_busy():
            """Acceptance 14: a second release while a pick is in flight is
            refused; the first lands."""
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, _, _ = expected_pick(0, ray, t, c, True)
            # The host PARKS the first pick's read, so the pick is provably in
            # flight when the second release lands.
            host.hold_reads()
            try:
                _, _, count_before, patches_before = run_ray(0, ray, True)
                host.await_reads(1)
                g = host.press(state["tool"], VIEW_3D, ray, level=0)
                g.release(ray, level=0)
                busy = await_status_set(host, patches_before, timeout=2.0)
                assert busy is not None, "the second release said nothing"
            finally:
                host.release_reads()
            pick_lands(count_before, expected, value)
            time.sleep(0.2)
            assert len(state["picks"]) == count_before + 1, len(state["picks"])
            said = statuses_since(host, patches_before)
            assert len(said) >= 2 and said[0] != said[-1], f"the second release was not refused before the first landed: {said}"
            return f"second release refused ({said[0]!r}); the first pick landed ({said[-1]!r}); {len(state['picks'])} picks"

        def test_rebind_key_refused():
            """Acceptance 2, second half: a new arrangement with the key
            trigger refused -- the tool still picks under the new handle."""
            handle = host.bind_tool(TOOL, triggers=[(pb2.MOUSE_BUTTON_MIDDLE, 0)], actions=[("undo", "U"), ("clear", "")], cancel_on_camera_move=False)
            assert host.tool_acks[handle] == 1
            state["tool"] = handle
            ray = ray_through(0, (18.0, 22.0, -30.0), PEAK[0])
            expected, value, _, _ = expected_pick(0, ray, t, c, True)
            _, _, count_before, _ = run_ray(0, ray, True)
            pick_lands(count_before, expected, value)
            return f"handle {handle} with the key refused and clear's key withheld: a pick still lands"

        def test_profile_repush():
            """The profile panel re-pushed whole on its own PanelOpened."""
            images_before, patches_before = len(host.images), len(host.patches)
            host.open_panel(PROFILE)
            assert await_until(lambda: profile_image(host, images_before) is not None), "no PNG re-pushed on the profile panel"
            assert await_until(lambda: statuses_since(host, patches_before, PROFILE, "pick")), "the pick label was not re-pushed"
            label = host.status_of(PROFILE, "pick")
            assert label.startswith("Pick "), label
            return f"PNG and {label!r} re-pushed"

        def test_no_violations():
            assert unexpected_violations(host) is None, unexpected_violations(host)
            assert not host.unhandled, host.unhandled
            return "no ContractViolation beyond the two undeclared-level records"

        def test_teardown():
            closed = host.shutdown()
            assert closed, f"the stream did not end within {SHUTDOWN_WINDOW_S:g} s"
            assert host.stream_error is None, host.stream_error
            report = host.shutdown_report
            assert report is not None and report.outcome in (pb2.ShutdownReport.NO_HOOK, pb2.ShutdownReport.COMPLETED), report
            assert unexpected_violations(host) is None, unexpected_violations(host)
            return f"ShutdownReport {pb2.ShutdownReport.Outcome.Name(report.outcome)}, then a clean end of stream; {len(host.violations)} violation report(s) in all, none unexpected"

        check("handshake", test_handshake)
        if failures:
            print("\nthe handshake failed; nothing else was run", file=sys.stderr)
            return 1
        check("manifest: tool, triggers, actions, pane buttons, icons, overlays, panels", test_manifest)
        check("grants: two O, two P by subject, V", test_grants)
        check("panels opened: defaults", test_panels_opened)
        check("arrangements -> activated, or the first-record fallback", test_arrangements)
        check("3D ray at level 0: aim live, pick, marker + label, count, status, profile", test_ray_level0)
        check("3D ray at level 2, refined: two reads, the exact peak", test_ray_level2_refined)
        check("3D ray at level 2, unrefined: one read, the coarse centre", test_ray_level2_unrefined)
        check("oblique ray at level 1: agrees with dense sampling", test_oblique)
        check("MISS ray: no pick, no aim, no read", test_miss)
        check("2D voxel and projection column", test_2d_voxel_and_column)
        check("undo and clear as FIRE inside a gesture", test_actions_mid_drag)
        check("undo and clear from the panel", test_buttons)
        check("contribute: ellipsoids, replace_all, radius", test_contribute)
        check("generation changed under the click", test_generation_changed)
        check("refusals: no visible channel; undeclared level", test_refusals)
        check("busy: a second release while a pick is in flight", test_busy)
        check("re-arranged with the key trigger refused", test_rebind_key_refused)
        check("profile panel re-pushed on open", test_profile_repush)
        check("no violations", test_no_violations)
        check("teardown", test_teardown)

    if failures:
        print(f"\n{len(failures)} check(s) failed: {', '.join(failures)}", file=sys.stderr)
        return 1
    print("\nhost emulation passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
