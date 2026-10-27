# Stage chain

Four processing stages, each small enough to specify to the bit: **`gain`**
rewrites one channel, **`diff`** appends a channel, **`widen`** repacks
everything as `f32` and may crop, and **`drift`** writes only the pose, from
a channel it reads. That is the whole plugin. It has no panel, no command, no
overlay and no permission.

The example is implemented in Python, Go, Rust and Java, each on its
language's SDK. This document specifies the **behavior** the four share.
Processing stages themselves are specified in
`docs/spec/processing_stages.md` (protocol `1.0`); where this document and
`docs/spec/` disagree, `docs/spec/` wins.

## What it demonstrates

- **Declaring stages and their parameters** in the manifest, and answering
  `PlanStage`: what a stage modifies, adds and reads, and the shape it puts
  out.
- **Processing a slot in place**: rewriting a plane, appending a channel,
  repacking to another dtype and allocation (a relayout), and composing a
  pose.
- **Refusing**: declining a plan, and refusing a volume.
- **State per arm**: `drift` keeps state per `(pipeline, position)`.
- **Sharing a ring.** [`python/hostsim.py`](python/hostsim.py) drives **two
  plugin processes on one ring**, and the two may be written in different
  languages.

A stage is the one place in the protocol where two plugins touch the same
bytes. A ring is granted to every session in the chain under one id; a slot's
header, written by one plugin, is validated and rewritten by the next; the
turn word is the only synchronization, and the host relays hints between
processes that never speak to each other. Three things follow:

> **The slot is the interface.** The next stage reads what this one wrote,
> possibly in another language: every offset, every reserved field left
> zero, the channel array padded to `plane_cap`, the header rounded to the
> right multiple (§5.1). A slot that is wrong is answered
> `STAGE_SLOT_INVALID`.
>
> **The arithmetic is the check.** The driver computes every voxel of every
> result itself and compares them **exactly**. So the four stages' arithmetic
> is specified here down to rounding, and a chain that runs `gain` in Go and
> `diff` in Rust has one right answer.
>
> **The turn is the synchronization.** A stage takes a turn when one
> acquire-load shows its `(pipeline, position)` with a new `iteration`, not
> when a hint arrives. The driver delays the host's relayed hint by three
> seconds and expects the next stage to have finished long before.

The host in the driver is `examples/vizi_hostsim/chain.py`.

### Notes

1. **§7's quarantine end.** A stage that misses the host's deadline does not
   know it, and hands on as usual. If the next position is armed, that stage
   takes the turn, and it processes or refuses the volume like any other. The
   host cannot intervene, so the slot leaves quarantine only when its turn
   reaches a position the host holds. The driver's deadline scenario waits
   for exactly that. The plugin has nothing to do about it.
2. **A parameter change is a new plan, then a new arm** (§2). The host
   re-plans before it re-arms. An SDK refuses an arm with no answered
   `PlanStage` behind it for exactly that input and those params (§4,
   site 32).
3. **The pose uses the dataset's pitch, and this dataset's is 1** (§5.5:
   `s = spacing ⊙ v`). `drift` computes a translation in voxels and writes it
   as physical units without asking for the pitch, which is correct only
   because the driver's dataset declares `spacing_zyx = (1, 1, 1)`. A real
   stabilizer reads the pitch from `ViewState` (§8.7) or `Geometry`; this one
   is kept free of data access so that the plugin needs no permission.

## 1. Manifest

`manifest_version` 1, id **`org.example.stage_chain`**, name `Stage Chain`,
version `0.1.0`, `engines.vizi` `1.0`, no `permissions`, and exactly these
`stages` (`manifest_schema.md` §3.7), in this order:

```json
"stages": [
  { "id": "gain", "name": "Gain",
    "params": [
      { "key": "factor", "name": "Factor", "type": "float", "value": 1.0, "min": 0, "max": 16 },
      { "key": "channel", "name": "Channel", "type": "int", "value": 0, "min": 0, "max": 65535 },
      { "key": "stall_ms", "name": "Stall (ms)", "type": "int", "value": 0, "min": 0, "max": 60000 }
    ] },
  { "id": "diff", "name": "Absolute difference",
    "params": [
      { "key": "a", "type": "int", "value": 0, "min": 0, "max": 65535 },
      { "key": "b", "type": "int", "value": 1, "min": 0, "max": 65535 }
    ] },
  { "id": "widen", "name": "Widen to f32",
    "params": [
      { "key": "crop", "name": "Crop (voxels, y and x)", "type": "int", "value": 0, "min": 0, "max": 1024 }
    ] },
  { "id": "drift", "name": "Drift (pose only)",
    "params": [
      { "key": "channel", "type": "int", "value": 0, "min": 0, "max": 65535 }
    ] }
]
```

The host reads the manifest for the params' types and defaults, and sends a
value for every key (§2). A params map that does not match (a key missing
or undeclared, the wrong `Value` kind, a non-finite float, a value out of
range) is the SDK's to refuse, `Error{INVALID_ARGUMENT}` on `PlanStage` and
site 32 on `StageArm`, before any code of yours runs. A `Value` kind your
SDK does not know (a later minor's) is not a mismatch: the SDK refuses that
arm unacknowledged, with a local diagnostic, and reports nothing.

## 2. Planning

Each stage answers `PlanStage` from its input `StageShape` and its params
(§3). Where a stage **declines** below, it answers `Error{UNSUPPORTED}`
naming why; the wording is yours.

| stage | declines when | `modifies` | `adds` | `reads` | output allocation | output dtype |
|---|---|---|---|---|---|---|
| `gain` | `channel` is not in the input | `{channel: [channel]}` | — | — | the input's | the input's |
| `diff` | `a` or `b` is not in the input | — | one, key **`absdiff`**, needs `[a, b]` | — | the input's | the input's |
| `widen` | `y − 2·crop < 1` or `x − 2·crop < 1` | every input channel, each needing itself | — | — | `(z, y − 2·crop, x − 2·crop)` | `f32` |
| `drift` | `channel` is not in the input, or the input dtype is not `u8` or `u16` | — | — | `[channel]` | the input's | the input's |

`diff`'s new channel's `name` is yours. `a` equal to `b` is legal and adds a
zero channel.

## 3. Processing

A stage is handed a slot at a position it is armed at (§6). Everything below
happens inside the **extent** `[z0, zN) × [y0, yN) × [x0, xN)`. Voxels outside
it are not touched, except where a stage lays out a new plane or repacks
(§5.2, §5.3). "The plane of channel *c*" is the plane `p` with `channels[p] =
c`; plane `p` is not channel `p` in general (§5.2), and the driver's chains
make sure it is not always.

Arithmetic is IEEE 754 binary64 ("f64") unless it says binary32 ("f32"),
with round-to-nearest-even throughout. Every integer sum is exact (64-bit
unsigned is enough for every volume the driver sends).

### 3.1 `gain`

If `stall_ms > 0`, first wait that many milliseconds. The stage holds the
turn throughout (this is how the driver makes a stage miss the host's
deadline, §7).

Then, for each voxel `v` of the plane of `channel` inside the extent:

- `p = f64(v) × factor`, one f64 multiplication;
- **`u8` / `u16`:** the result is `p` rounded to the nearest integer, **ties
  to even** (`rint`, not `round`: 2.5 → 2, 3.5 → 4), then clamped to
  `[0, 255]` or `[0, 65535]`;
- **`f32`:** the result is `p` converted to f32 (round to nearest even);
- **`f64`:** the result is `p`.

The driver uses factors such as 1.5, 0.5 and 0.25, whose ties occur, so
rounding half away from zero fails it.

### 3.2 `diff`

Append the plane for `absdiff` at the channel index the arm assigned (§4,
`new_channels["absdiff"]`), **zero everywhere**, then, inside the extent:

- **`u8` / `u16`:** `|A − B|`, computed exactly as integers (as signed
  64-bit, say), which always fits the type;
- **`f32`:** `|A − B|` with the subtraction in f32;
- **`f64`:** the same in f64;

where `A` and `B` are the voxels of the planes of `a` and `b`. The existing
planes are unchanged.

### 3.3 `widen`

Repack every plane (§5.3) as `f32` with the allocation
`(z, y − 2·crop, x − 2·crop)`: new plane `p`, voxel `(i, j, k)`, is
`f32(old plane p at (i, j + crop, k + crop))` — each value converted exactly
(every `u8` and `u16` value is exact in f32) or, from `f64`, rounded to
nearest even. The channel array is unchanged. Then restate the placement
(§5.4) so that every voxel stays where it was placed:

- **extent**, per axis, with window offset `o` (`0` for z, `crop` for y and
  x) and new size `n`: `lo' = clamp(lo − o, 0, n)`, `hi' = clamp(hi − o, 0,
  n)`;
- **origin**, per axis: `origin' = origin + (lo' + o − lo)`, in f64.

With a full extent and the host's origin that is the extent
`(0, z, 0, y − 2·crop, 0, x − 2·crop)` and the origin
`(o_z, o_y + crop, o_x + crop)`. `crop = 0` changes only the dtype (and on an
`f32` input changes nothing).

### 3.4 `drift`

`drift` keeps, **per arm** — per `(pipeline, position)` — the `t` it last
processed there and a **reference** centroid. Arms do not share this state,
even when one plugin is armed at several positions.

1. **The causal rule.** If this arm has processed a volume before, and the
   slot's `t` is not that volume's `t + 1`, the stage **refuses**
   (§6.3, *refused*, reason **1**, "needs earlier state") before writing
   anything, and **forgets** this arm's state, so the next volume starts
   over.
2. **The centroid** of the plane of `channel` inside the extent: with `S` the
   exact sum of its voxels, and `S_z`, `S_y`, `S_x` the exact sums of each
   voxel times its slot index on that axis (`z`, not `z − z0`), the
   centroid on each axis is

   `c = origin + (f64(S_a) / f64(S) − lo)`

   evaluated in that order in f64: one division, one subtraction of the
   extent's start `lo` on that axis, one addition of the origin. If `S = 0`
   there is no centroid.
3. **The reference** is the centroid of the first volume this arm processes
   (after a start or a forgetting). If that volume had no centroid, there is
   none, and the stage never writes a pose until it starts over.
4. **The pose.** If this volume has a centroid and there is a reference,
   compose `R_k = I`, `T_k = reference − centroid` (each axis one f64
   subtraction) into the slot's pose as §5.5 says:
   `(R, T) ← (R·R_k, R·T_k + T)`. Otherwise leave the pose alone.
5. Record this `t`. Nothing else in the slot changes.

The first volume after a start therefore gets `T_k = 0`.

## 4. Nothing else

No panel, no command, no overlay, no reader. A stage plugin in a session with
no chain does nothing at all.

## Acceptance

The driver runs these in order. Each launches **two** sessions, A
(`--plugin-dir`) and B (`--other-dir`, default the same directory) on one
dataset: `(6, 2, 4, 16, 16)` u16, pitch `(1, 1, 1)`, whose channel 0 is a
low textured background with a bright 3 × 3 blob that moves with `t`, and
channel 1 a different texture that also varies with `t`. Every expected voxel
is computed by the driver from §3; every comparison is exact. A pose is
compared as numbers (so `−0.0` equals `0.0`).

1. **Planning**, in both sessions: the four plans exactly as §2's table
   (`reads = [0]` for `drift`, nothing else); then nine refusals and their
   kinds: `factor` sent as a string, out of range (100), missing, an
   undeclared key, non-finite → `INVALID_ARGUMENT`; an undeclared stage id →
   `NOT_FOUND`; `gain` on channel 5, `widen` with crop 8 on 16 × 16, `drift`
   on an `f32` input → `UNSUPPORTED`.
2. **Two processes, one ring:** `gain ×1.5` in A at 1, `drift` in B at 2,
   `diff` in A at 3. The `S` ring is granted to both sessions **under one
   id**, whose epoch is neither session's, and both install it; each position
   armed and acknowledged; three volumes (`t = 0, 1, 2`), each *processed*,
   `done_through = 3`, planes `(0, 1, 2)`, the data and the pose exact; the
   host relayed hints A → B and B → A.
3. **Before the relay:** `gain ×2` in A at 1, `gain ×0.5` on channel 1 in B
   at 2, with the host holding every relayed hint back **3 s**. The volume
   comes back processed in under 2 s: B took position 2 on its own schedule
   (§6.2).
4. **Odd positions:** `gain ×2` (A, 1), `diff` (B, 3), `widen crop 3`
   (A, 5). The host holds 2 and 4, looks at the slot there (`done_through` 1
   then 3, the planes as expected) and hands it on; the turn goes
   1 → 2 → … → 6. The result is `f32`, `4 × 10 × 10`, extent
   `(0, 4, 0, 10, 0, 10)`, origin `(0, 3, 3)`.
5. **A refusal ends the chain:** chain 2's shape; `t = 0` and `1` processed;
   `t = 3` comes back at **`−2`**, outcome *refused*, reason 1,
   `done_through = 1`, two planes (`diff` did not run), channel 0 as `gain`
   left it. Then `t = 4` is processed with a zero pose (`drift` started
   over).
6. **An added channel, then a widened type:** `diff` (A, 1), `widen crop 2`
   (B, 2). The host sizes the ring for 3 planes and
   `max(2·4·16·16·2, 3·4·12·12·4)` bytes; the result is three `f32` planes
   `4 × 12 × 12`, channels `(0, 1, 2)`, origin `(0, 2, 2)`, every voxel exact.
7. **Params per position:** `gain ×2` (A, 1), `gain ×0.25` (A, 2), `gain ×3`
   on channel 1 (B, 3): one stage at two positions in one session with
   different values, and the result exact (`×2` then `×0.25`, each rounded).
   Then an arm of position 2 with `factor = 99` and one of position 1 with
   `factor` as an `int`: **each unacknowledged**, and A reports
   `VIOLATION_SITE_STAGE_ARM_REFUSED` for each and nothing else. Then an arm
   of position 1 whose `factor` carries a `Value` kind a later minor adds
   (field 7): **unacknowledged and not reported**, as an unknown dtype is,
   and its plan stays planned. Re-armed properly, the chain runs again.
8. **A missed deadline:** host deadline 0.4 s; `gain ×2 stall_ms 1500` in A at
   1, `gain` on channel 1 in B at 2, a ring of two slots. The volume ends
   `deadline` at position 1; that slot is **quarantined** and the other is
   the free one; the quarantine ends only once the turn reaches a position the
   host holds (note 1). Re-planned and re-armed with `stall_ms 0`, the
   chain runs again.
9. **A re-arm while the stage holds a turn** (§4): `gain ×2 stall_ms 800`
   (A, 1), `gain` on channel 1 (B, 2). While A is inside its stall, the host
   re-plans and re-arms position 1 as `×3`, `stall_ms 0`. A acknowledges **at
   once, with the turn still at 1**; that volume finishes under the old
   revision (`×2`), and the next is `×3`.
10. **A session that ends while it holds a turn** (§7): `gain ×2` (A, 1),
    `gain` on channel 1 with `stall_ms 5000` (B, 2), host deadline 10 s. While
    B holds position 2 its process is killed. The host takes the turn back
    (the word reads `−2`) and discards the volume. With position 2 removed,
    re-planned and re-armed, A alone carries on (`done_through = 1`).
11. **The chain outgrows its ring** (§4, re-attaching a larger buffer):
    `gain ×1.5` (A, 1), `drift` (B, 2), one volume; then `diff` (A, 3) is
    appended, which needs a third plane. The host grants a larger ring to
    both sessions **superseding** the old one, and each sends `SegmentReleased`
    for the old one. Every position is re-armed on the new id. The next
    volume has three planes, `plane_cap` 3, and a pose that shows `drift`'s
    per-arm state survived the re-arm (`t = 1` follows `t = 0`).

In every scenario, also: nothing unhandled, no host diagnostic, no
`ContractViolation` from either plugin but those acceptance 7 provokes, and
every slot that comes back has a valid header (§7's minimum, the host's own
codec).

The mixed-language runs are the same eleven scenarios with A and B in
different languages: any implementation can be session A and any other
session B.
