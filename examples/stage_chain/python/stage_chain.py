#!/usr/bin/env python3
"""The stage-chain example, in Python: four processing stages (``../SPEC.md``).

- ``gain`` rewrites one channel in place, times a float factor;
- ``diff`` appends a channel, the absolute difference of two others;
- ``widen`` repacks every channel as ``f32``, optionally cropped in y and x;
- ``drift`` writes only the pose, from the centroid of a channel it reads.

The arithmetic is the SPEC's, exactly, so that a chain of stages written in
different languages produces the same bytes.
"""

from __future__ import annotations

import time

import numpy as np

from vizi_plugin_sdk import (
    REFUSE_CAUSAL,
    NewChannel,
    Refusal,
    RefuseVolume,
    StageInput,
    StagePlan,
    StageSlot,
    pb2,
    plugin,
    serve,
    stage,
)

#: The manifest is declared in code: ``--install`` installs it, and
#: ``vizi-plugin.json`` beside this file is what ``--manifest`` writes.
plugin("org.example.stage_chain", name="Stage Chain", version="0.1.0")

#: Each stage's parameters (``manifest_schema.md`` §3.7), declared with it.
GAIN_PARAMS = [{"key": "factor", "name": "Factor", "type": "float", "value": 1.0, "min": 0, "max": 16}, {"key": "channel", "name": "Channel", "type": "int", "value": 0, "min": 0, "max": 65535}, {"key": "stall_ms", "name": "Stall (ms)", "type": "int", "value": 0, "min": 0, "max": 60000}]
DIFF_PARAMS = [{"key": "a", "type": "int", "value": 0, "min": 0, "max": 65535}, {"key": "b", "type": "int", "value": 1, "min": 0, "max": 65535}]
WIDEN_PARAMS = [{"key": "crop", "name": "Crop (voxels, y and x)", "type": "int", "value": 0, "min": 0, "max": 1024}]
DRIFT_PARAMS = [{"key": "channel", "type": "int", "value": 0, "min": 0, "max": 65535}]

_INTEGER_MAX = {np.dtype(np.uint8): 255, np.dtype(np.uint16): 65535}


def _decline(why: str) -> Refusal:
    return Refusal(pb2.Error.UNSUPPORTED, why)


@stage("gain", name="Gain", params=GAIN_PARAMS)
class Gain:
    def plan(self, inp: StageInput, params: dict) -> StagePlan:
        channel = params["channel"]
        if channel not in inp.channels:
            raise _decline(f"channel {channel} is not in the input")
        return StagePlan(modifies={channel: (channel,)})

    def process(self, slot: StageSlot) -> None:
        if slot.params["stall_ms"]:
            time.sleep(slot.params["stall_ms"] / 1000)
        factor = float(slot.params["factor"])
        z0, zn, y0, yn, x0, xn = slot.extent
        plane = slot.plane(slot.params["channel"])
        window = plane[z0:zn, y0:yn, x0:xn]
        scaled = window.astype(np.float64) * factor
        top = _INTEGER_MAX.get(slot.dtype)
        if top is not None:
            # SPEC: round half to even, then clamp.
            scaled = np.clip(np.rint(scaled), 0, top)
        window[...] = scaled.astype(slot.dtype)


@stage("diff", name="Absolute difference", params=DIFF_PARAMS)
class Diff:
    def plan(self, inp: StageInput, params: dict) -> StagePlan:
        a, b = params["a"], params["b"]
        missing = [c for c in (a, b) if c not in inp.channels]
        if missing:
            raise _decline(f"channels {missing} are not in the input")
        return StagePlan(adds=[NewChannel("absdiff", "|a - b|", needs=(a, b))])

    def process(self, slot: StageSlot) -> None:
        a = slot.plane(slot.params["a"])
        b = slot.plane(slot.params["b"])
        z0, zn, y0, yn, x0, xn = slot.extent
        if slot.dtype.kind == "u":
            got = np.abs(a[z0:zn, y0:yn, x0:xn].astype(np.int64) - b[z0:zn, y0:yn, x0:xn].astype(np.int64))
        else:
            got = np.abs(a[z0:zn, y0:yn, x0:xn] - b[z0:zn, y0:yn, x0:xn])
        out = slot.add_channel("absdiff")  # zeroed, so zero outside the extent
        out[z0:zn, y0:yn, x0:xn] = got.astype(slot.dtype)


@stage("widen", name="Widen to f32", params=WIDEN_PARAMS)
class Widen:
    def plan(self, inp: StageInput, params: dict) -> StagePlan:
        crop = params["crop"]
        z, y, x = inp.alloc
        if y - 2 * crop < 1 or x - 2 * crop < 1:
            raise _decline(f"a crop of {crop} leaves nothing of {y} x {x}")
        return StagePlan(
            modifies={c: (c,) for c in inp.channels},
            alloc=(z, y - 2 * crop, x - 2 * crop),
            dtype=np.float32,
        )

    def process(self, slot: StageSlot) -> None:
        crop = slot.params["crop"]
        z, y, x = slot.alloc
        slot.relayout(np.float32, (z, y - 2 * crop, x - 2 * crop), offset=(0, crop, crop))


@stage("drift", name="Drift (pose only)", params=DRIFT_PARAMS)
class Drift:
    def __init__(self) -> None:
        #: (pipeline, position) -> (the last t processed there, the reference
        #: centroid), per SPEC's causal rule.
        self.state: dict[tuple[int, int], tuple[int, tuple[float, float, float] | None]] = {}

    def plan(self, inp: StageInput, params: dict) -> StagePlan:
        channel = params["channel"]
        if channel not in inp.channels:
            raise _decline(f"channel {channel} is not in the input")
        if inp.dtype.kind != "u":
            raise _decline(f"drift sums integers exactly; {inp.dtype} is not an integer type")
        return StagePlan(reads=(channel,))

    def process(self, slot: StageSlot) -> None:
        key = (slot.pipeline, slot.position)
        last = self.state.get(key)
        if last is not None and slot.t != last[0] + 1:
            # SPEC: a gap refuses, and the next volume starts over.
            del self.state[key]
            raise RefuseVolume(REFUSE_CAUSAL, f"t {slot.t} does not follow t {last[0]}")
        z0, zn, y0, yn, x0, xn = slot.extent
        window = slot.plane(slot.params["channel"])[z0:zn, y0:yn, x0:xn].astype(np.uint64)
        total = int(window.sum())
        if total == 0:
            centroid = None
        else:
            zz, yy, xx = np.meshgrid(
                np.arange(z0, zn, dtype=np.uint64),
                np.arange(y0, yn, dtype=np.uint64),
                np.arange(x0, xn, dtype=np.uint64),
                indexing="ij",
            )
            # Placed in vizi's voxel space: origin + (index - extent start).
            centroid = tuple(
                float(o) + (int((window * idx).sum()) / total - lo)
                for o, idx, lo in zip(slot.origin, (zz, yy, xx), (z0, y0, x0))
            )
        reference = centroid if last is None else last[1]
        if centroid is not None and reference is not None:
            # s_in = s_out + T_k: this frame's content moved from the reference.
            slot.compose_pose(np.eye(3), [r - c for r, c in zip(reference, centroid)])
        self.state[key] = (slot.t, reference)


if __name__ == "__main__":
    serve()
