# Processing stages

> **What vizi drives.** vizi drives a chain of one stage: an applied filter is
> a single stage at position 1 of pipeline 1, in vizi v0.9.0 and later. It
> drives no chain of several stages yet
> ([`spec/processing_stages.md`](../spec/processing_stages.md)). The
> [host emulator](../../examples/vizi_hostsim/README.md) does drive chains of
> several (`chain.py`): several plugin processes on one ring, with the
> [`stage_chain` example](../../examples/stage_chain/SPEC.md) as its driver.
> Test a chain against that.

A **processing stage** edits a volume **in place**, in shared memory, between
vizi reading it and vizi using it. Several stages form a **chain** and run one
after another on the same buffer. Stabilization, deconvolution, denoising and
thresholding all have this shape.

## What a stage may do

([`spec/processing_stages.md`](../spec/processing_stages.md) §1.)

- **It works on data, not on display.** A stage always gets the real bytes at
  full resolution (level 0).
- **The volume keeps its voxels.** A stage may shrink the valid region (a
  crop), **add channels**, and **change the data type of every channel**
  together. It may not resample, rotate the grid, or grow a dimension. Motion
  is reported as a **pose**, not applied by resampling.
- **Processing is causal.** A stage processes the volume it is handed without
  waiting for another. It may read other volumes through `ctx.host` as any
  plugin can (those are the unprocessed source).
- **A stage may refuse** when it needs something it does not have — the
  previous frame's result after the user jumped ahead. **A refusal ends the
  chain** for that volume: no later stage runs, and the volume is used as far
  as the chain got.

## Declaring

A stage is a `stages` contribution — see the
[manifest reference](../manifest.md#stages). Its `params` are the settings
the host sends it, declared as a flat list of `float`, `int`, `bool` or
`choice` entries with defaults. `@stage` takes the same declaration, checks
it when the decorator runs, and records it for a
[declared plugin](../manifest.md#declaring-the-manifest-in-code).

## SDK surface

```python
from vizi_plugin_sdk import StageInput, StagePlan, StageSlot, serve, stage


@stage("blur", name="Gaussian blur", params=[
    {"key": "sigma", "name": "σ (voxels)", "type": "float", "value": 2.0, "min": 0.1, "max": 50},
])
class Blur:
    def plan(self, inp: StageInput, params: dict) -> StagePlan:
        # Rewrite every channel in place; each needs only itself.
        return StagePlan(modifies={c: (c,) for c in inp.channels})

    def process(self, slot: StageSlot) -> None:
        sigma = slot.params["sigma"]
        z0, zn, y0, yn, x0, xn = slot.extent
        for plane in slot.planes:
            plane[z0:zn, y0:yn, x0:xn] = smooth(plane[z0:zn, y0:yn, x0:xn], sigma)


if __name__ == "__main__":
    serve()
```

The class defines two methods. One instance serves every position the stage
is armed at, constructed with a `PluginContext` when `__init__` takes a
parameter (as a reader's is). `plan` runs on the command worker and
`process` on the stage worker, so the two may overlap.

### `plan(self, inp, params)` — what the stage will produce

Before any data moves, the host asks each stage what it will do with the
input it would be handed, and each answer is the next stage's input
(§3). `inp` is a `StageInput`: `channels` (the channel indices present, in
plane order), `z`, `y`, `x` (each plane's allocation, also as `inp.alloc`)
and `dtype` (a numpy dtype, one for every channel). `params` holds this
position's values as plain Python values, already checked against the
declaration.

Return a `StagePlan` stating the **largest** output the stage produces from
that input with those parameters:

| Field | Meaning |
|---|---|
| `modifies` | `{channel: needs}` — each existing channel the stage rewrites, with the input channels it needs to do so |
| `adds` | a list of `NewChannel(key, name="", needs=())` — each channel the stage appends; `key` is yours, the host assigns the index |
| `reads` | input channels the stage reads and does **not** write — a stabilizer computing its pose from channel 0 says `reads=(0,)`; the host loads them like any need and never treats them as rewritten |
| `alloc` | the output allocation `(z, y, x)`, each **at least 1** and ≤ the input's; default the input's |
| `dtype` | the output dtype of every channel (`uint8`, `uint16`, `float32` or `float64`); default the input's |

`StagePlan.needs()` is every input channel the plan cannot run without: the
channels in `modifies`, what each of them needs, what each of `adds` needs,
and `reads`. A channel in `reads` is checked like one in `modifies`: it must
be present in the input and at most 65 535.

An empty `modifies`, `adds` and `reads` is legal: a stage that only reports
extent, origin or pose from nothing. One that computes them from a channel
names it in `reads` rather than claiming to rewrite it. **In place means
later stages see the rewritten channel**; if a later stage needs the
original, add the result as a new channel instead.

Raising `Refusal(kind, message)` from `plan` answers the host with an error
of that kind and leaves the stage out of the chain. What the host hears in
each case (§3's table):

| Condition | `Error` kind |
|---|---|
| the `stage_id` is not a stage this plugin declares | `NOT_FOUND` (the SDK answers it) |
| the input is absent, names a dtype outside `DType`, or a channel index above 65 535 | `PROTOCOL` (the SDK answers it) |
| your plan names a channel index above 65 535 | `PROTOCOL` |
| the `params` do not match the declaration | `INVALID_ARGUMENT` (the SDK answers it; `plan` never runs) |
| you decline this input | your `Refusal`'s kind, typically `UNSUPPORTED` |
| your plan breaks a rule above (a grown or zero dimension, a channel modified, read or needed that the input does not hold), or `plan` raised anything else | `INTERNAL` |

### `process(self, slot)` — one volume, in place

`slot` is a `StageSlot`, the slot during this stage's turn:

| Member | What it is |
|---|---|
| `params` | this position's parameter values (a dict) |
| `planes` | a `(planes, z, y, x)` numpy array **over the shared memory** — writing it is the edit |
| `plane(channel)` | the plane holding channel index `channel` (`KeyError` if absent) |
| `channels` | the channel index of each plane, in plane order — plane `p` is not channel `p` in general |
| `add_channel(key)` | appends the plane for the plan's `adds` entry `key`, zeroed, at the index the host assigned, and returns it |
| `relayout(dtype=None, alloc=None, offset=(0, 0, 0))` | repacks every plane into a new dtype and allocation: each new plane is the old one's window at `offset`, cast with a plain `astype` (any scaling is yours); moves the extent and origin so every voxel stays placed; returns the new `planes` |
| `extent` | `(z0, zN, y0, yN, x0, xN)`, the valid rectangle of every plane; settable, **shrink only** |
| `origin` | where voxel `(z0, y0, x0)` sits in vizi's level-0 voxel space, as floats; settable |
| `pose` | `(R, T)`, this slot's data to real space, **absolute**; settable |
| `compose_pose(R_k, T_k)` | composes this stage's own motion (`s_in = R_k · s_out + T_k`) into the absolute pose |
| `t`, `open_generation`, `iteration`, `pipeline`, `position`, `stage_id` | which volume, from which dataset, at which place in which chain |
| `dtype`, `alloc`, `capacity` | the current layout and the slot's data capacity in bytes |

A stage that changes the dtype repacks every plane, so it uses `relayout`
and then the array `relayout` returns — an array taken before describes the
old layout:

```python
import numpy as np

from vizi_plugin_sdk import StagePlan, stage


@stage("threshold", params=[{"key": "level", "type": "int", "value": 100, "min": 0}])
class Threshold:
    def plan(self, inp, params):
        return StagePlan(modifies={c: (c,) for c in inp.channels}, dtype=np.uint8)

    def process(self, slot):
        mask = slot.planes >= slot.params["level"]   # a copy, taken before the repack
        slot.relayout(np.uint8)[...] = mask
```

A stage that adds a channel appends it with the key its plan named:

```python
@stage("flow")
class FlowMagnitude:
    def plan(self, inp, params):
        if 1 not in inp.channels:
            raise Refusal(pb2.Error.UNSUPPORTED, "needs channel 1")
        return StagePlan(adds=[NewChannel("flow_mag", "Flow magnitude", needs=(1,))])

    def process(self, slot):
        slot.add_channel("flow_mag")[...] = magnitude(slot.plane(1))
```

(`Refusal`, `pb2` and `NewChannel` are imported from `vizi_plugin_sdk`.)

**Keep no array past `process`.** They are views of memory the next stage
may be writing as soon as you return; the SDK reports one that outlives the
call.

### The outcome — processed, refused, failed

| `process` … | Outcome | What follows |
|---|---|---|
| returns | *processed* | the header changes are written and the slot goes to the next position |
| raises `RefuseVolume(reason, detail="")` | *refused* | the chain ends; the volume is used as far as the previous stage got |
| raises anything else | *failed* | the slot's data is undefined and the host discards the volume |
| returns, but left a dtype other than the armed output's, or any allocation larger than it | *failed*, reason `2` | as above, and the SDK reports it: the stage broke its own plan |

**The armed output is enforced.** What the stage is held to is the plan the
host armed at this position, not the last `plan` answer it gave (§4): the
SDK checks each turn's result against the arm's `output` and hands a turn
that outgrew it on as *failed*. The host may check too; do not rely on it.

`reason` is `REFUSE_CAUSAL` (needs earlier state), `REFUSE_MISSING_INPUT`
(a declared input channel is absent) or `REFUSE_OTHER` (the default).
**Refuse before writing anything** (§6.3). A refused volume is used as the
previous stage left it, so its data must still be that stage's output.
Everything a refusal can name — earlier state, an input channel, a plan that
no longer holds — is knowable before work starts. The SDK writes only the
outcome and reason on a refusal and discards your extent, origin and pose
changes, but it cannot undo writes to `planes`.

The SDK does the rest of §6–§7 for you: it validates the slot's header before
calling `process` (an invalid one is *failed* and reported, and your code
never runs), refuses with `REFUSE_MISSING_INPUT` when a channel your plan
needs is absent, stores the turn and signals the host.

## Parameters belong to the position

A chain may run the same stage twice with different settings — blur σ 2,
erode, blur σ 5 — so **parameters belong to where a stage sits, not to the
stage** (§2). The host sends a value for every declared key, and no other,
each time it plans the stage (`plan`'s `params`) and again when it arms it at
a position (`slot.params` during that position's turns). The SDK answers a
plan whose values do not match the declaration — a missing or undeclared key,
the wrong type, a value outside its range or options — with
`INVALID_ARGUMENT`, and refuses such an arm without acknowledging it. It
refuses the same way an arm that carries no input or no output, one naming
a channel index twice among its input channels and new channels, and one
that no answered `plan` for its stage, input and parameters stands behind
(§4) — so the host learns of each at the arrangement's deadline, and your
`process` never runs under it.

**Changing a parameter is a new plan and a new arm**, made by the host, which
owns the values and so always knows when a stage's output is stale. **A host
may re-arm a position while your stage holds a turn there**: the SDK
acknowledges the new revision at once, the turn in progress finishes under
the revision it began with, and every turn after it is held to the new one
(the SDK reads the arm as each turn starts). So
`plan` may be called several times with different `params`, and each
answer stands for its own. A plugin's own [panel](panels.md) remains for
settings that are not per position, such as a model file; a stage whose own
settings change so that the plan it was armed with no longer holds refuses
with `REFUSE_OTHER` until the host plans and arms it again (§4).

## Where a stage runs

**vizi assigns positions; a plugin does not choose where it runs** (§4). One
stage may be armed at several positions in one chain or several chains, and
each is its own arm with its own parameters. Which stages form a chain, in
what order, and how a person composes one are the host's, and not yet
specified (§8).
