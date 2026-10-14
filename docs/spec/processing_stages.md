# Processing stages

> **Status.** The Python SDK serves stages (`@stage`,
> `vizi_plugin_sdk/stages.py`). The reference host drives a chain of ONE stage:
> an applied filter is a single stage at position 1 of pipeline 1, in a ring of
> its own, run on every volume the host reads. It drives no chain of several
> stages, so the rules here about a stage's neighbours have no host-side
> implementation.

A **processing stage** is a plugin that edits a volume **in place**, in shared
memory, between vizi reading it and vizi using it. Several stages form a
**chain** and run on the same buffer one after another. Stabilization,
deconvolution, denoising and fiducial marking all have this shape.

This document is normative, and it cites `shared_memory_protocol.md` (segments,
regions, disciplines), `doorbell_D_notification.md` (the hint that signals a
turn) and `control_plane_protocol.md` (the messages that set a chain up) rather
than restating them.

---

## 1. What a stage may do

- **It is a data operation, not a display operation.** A stage always gets the
  real bytes at full resolution (level 0). Pyramid levels are display; a host
  may build a pyramid of a processed result after the fact, and nothing here
  concerns it.
- **The volume keeps its voxels.** A stage may report that only a rectangular
  subset of its output is valid (§5's extent), which is a crop. It may **add
  channels** (§3) and it may **change the data type of every channel** (§5.3).
  It may not resample, rotate the voxel grid, or grow a spatial dimension: *a
  new layer is fine, a bigger volume is not; it is the same voxels all the way
  through.* Motion is reported as a pose (§5.5), not applied as a resampling.
- **Processing is causal.** A stage processes the volume it is handed, if it
  can, without waiting for another. It may ask vizi for other volumes through
  data access as usual (`control_plane_protocol.md` §8.7; those are the
  unprocessed source unless the person shares a filter's output with plugins
  and the read does not ask for `raw`, and each reply's `DataVolume.filter`
  says which it is), but it may not hold
  this one back until another arrives.
- **A stage may refuse** when it needs something it does not have: optical flow
  that wants the previous frame's result after the user jumped ahead. **A
  refusal ends the chain** for that volume. Nothing after a missing step is
  guaranteed to make sense, so no later stage runs, and the volume is used as
  far as the chain got (§6.3).

---

## 2. The manifest

A plugin declares its stages as a contribution (`manifest_schema.md`):

```json
"contributions": {
  "stages": [
    { "id": "stabilize", "name": "Stabilize (rigid)" },
    { "id": "blur", "name": "Gaussian blur",
      "params": [
        { "key": "sigma", "name": "σ (voxels)", "type": "float", "value": 2.0, "min": 0.1, "max": 50 },
        { "key": "edge", "type": "choice", "options": ["reflect", "constant"], "value": "reflect" }
      ] }
  ]
}
```

`id` is the stage's identity in every message below; `name` is what a person
sees. Which stages form a chain, in what order, and with what parameters, is
the host's, and later the user's (not specified here).

**`params` declares the stage's settings**, as a flat list: each has a `key`,
an optional `name`, a `type` (`float`, `int`, `bool`, `choice` or `string`),
and a default `value`; `float` and `int` may bound it with `min` and `max`, a
`choice` lists its `options`, and a `string` may bound its UTF-8 length with
`max_length` (`manifest_schema.md` §3.7 has the rules). On the wire they are a
flat `map<string, Value>`, the `Value` that commands and panel widgets carry,
with the kind fixed by the type: `float` is `f`, `int` is `i`, `bool` is `b`,
`choice` and `string` are `s`.

**Parameters belong to the position, not to the stage.** A chain may run the
same stage twice with different settings (blur σ 2, erode, blur σ 5), so the
host sends a value for **every declared key and no other** in `PlanStage` (§3)
and again in `StageArm` (§4). A plugin receiving anything else (a missing or
undeclared key, a value of the wrong kind, a non-finite `float`, or one
outside its range or options, or a `string` longer than its `max_length`) answers `PlanStage` with `Error{INVALID_ARGUMENT}` and refuses the
arm (§4). **Changing a parameter is a new plan and a new arm revision**: the
host owns the values, so the host always knows when a stage's output is stale.
A plugin's own panel remains for settings that are not per position, such as a
model file.

**The plugin's `version` is part of a result's identity.** vizi keeps a
filter's output beside the file (the companion directory's `processed/`) and
reads it back instead of re-running the stage when plugin `id`, `version`,
stage and parameters all match. So a plugin must change its `version` when a
stage's output would change. A setting kept outside `params` (a model file
chosen in the plugin's panel) is invisible to that comparison: output saved
under one model is read back under another. Where that matters, make the
choice a `string` param, which the host does see.

---

## 3. Negotiation: what a stage will produce, and what it needs

Before any data moves, the host asks each stage what it will do with the input
it would be handed. **The answers chain**: each stage's answer is the next
stage's input, and the host plans the whole chain before it sizes the buffer.

```
PlanStage     host → plugin   request   { stage_id, input: StageShape, params }
StagePlanned  plugin → host   reply     { output: StageShape, modifies, adds, reads }
```

`StageShape` is `{ channels, z, y, x, dtype }`: the channel indices present, in
plane order (§5.2), the **allocation** each plane is laid out in, and the one
data type every channel has.

The reply states the **largest** output the stage will produce **with those
`params`** (§2): a threshold may turn `u16` into `u8`, and a large erosion may
crop the border.

- **`output.z`, `output.y`, `output.x`** are the allocation the stage lays its
  output out in, each **at least 1 and ≤ the input's** (an empty allocation
  holds no voxel): a stage never grows a spatial
  dimension. **Each stage chooses its own output layout**: a stage that crops to
  256³ lays its output out as 256³ rather than keeping the input's shape, and
  the next stage is handed that.
- **`output.dtype`** is the type of every channel afterwards. A stage changes all
  channels' type or none.
- **`output.channels`** is ignored on the reply; the host computes it from the
  two lists below.
- **`modifies`**: each existing channel the stage rewrites in place, with the
  input channels it **needs** to do so: "I rewrite green (1) and I need red (0)
  and green (1)".
- **`adds`**: each new channel the stage appends, with a plugin-local `key`, a
  display `name`, and the input channels it needs: "I add `flow_mag` and I need
  green". The host assigns the new channel's index (§4).
- **`reads`**: input channels the stage reads and **does not write**: "I compute
  the pose from red (0)". Loaded like any other need, and never treated as
  rewritten. Without it a rigid stabilizer, which writes only the pose (§5.5),
  could ask for its input only by claiming to rewrite it.

A stage that cannot work on the offered input answers `Error` naming why, and is
left out of the chain. The error kinds:

| Condition | `Error` kind |
|---|---|
| `stage_id` is not a stage this plugin declares | `NOT_FOUND` |
| `input` is absent, names a dtype outside `DType`, or an index above 65 535 | `PROTOCOL` |
| the stage's plan names an index above 65 535 (§4) | `PROTOCOL` |
| `params` do not match the declaration (§2) | `INVALID_ARGUMENT` |
| the stage declines this input | the stage's own choice, typically `UNSUPPORTED` |
| the stage's plan breaks a rule above (grows a dimension, names a channel the input lacks), or planning raised | `INTERNAL` |
 Empty `modifies`, `adds` and `reads` are a legal answer: a
stage that reports extent, origin or pose (§5) from nothing needs no channel;
one that computes them from a channel names it in `reads`.

### 3.1 What the host loads

**The host decides which source channels to read by working backwards through
the chain.** Starting from the channels the chain's consumer wants, and every
channel a stage in the chain `reads` (a stage reads whether or not its output
is wanted), it replaces each channel an earlier stage produced with the inputs
that stage declared for it, stage by stage back to the source; the source
channels it reaches are the ones read into the slot. So a chain that shows only
green, with a stage that needs red to rewrite green, reads both.

**In place means later stages see the modified version.** If stage 1 rewrites
green and stage 2 needs the *original* green, stage 1 must **add** its result as
a new channel instead. Nothing in the data can tell an original plane from a
rewritten one.

### 3.2 Sizing the buffer

From the plans, the host fixes, for the whole chain:

- **`plane_cap`**: the most planes any step holds (the input's planes plus every
  `adds` up to that point);
- **`capacity`**: the most **bytes** any step's data needs, the source fill or
  any stage's declared output: `planes × z × y × x × itemsize(dtype)`,
  maximised over the steps.

**Nothing is sized for a combination no step uses.** A chain whose input is
256×3840×1920 u8 and whose stage declares a 256³ f32 output needs the larger of
those two byte counts, not an f32's worth of the input shape.

---

## 4. The ring, and arming a stage

**The buffer is a ring of `K` slots in one segment, `1 ≤ K ≤ 512`** (a `D`
mask names at most 512 regions, `doorbell_D_notification.md` §1), `purpose = "S"`,
with an **empty `subject`** (`shared_memory_protocol.md` §3, §3.1's purpose
table): the ring serves an exchange that names it, since every `StageArm` names
its ring by `segment_id`, as a reader handle names its deposit ring.
**Each slot is one region**, discipline **`STAGED`** (§6), `producer = HOST` (the
host writes each volume in first). With `K = 1` one volume is in memory; a larger
`K` lets the host read the next volume while an earlier one is processed or used.

**What a receiver checks at the grant** (`shared_memory_protocol.md` §8's
matrix, extended for this purpose; each failure is `malformed`):

- the `subject` is empty (`shared_memory_protocol.md` §3's table);
- every region is `STAGED`, and there are at most 512; a `STAGED` region in a
  segment of any other purpose is refused too, since only a stage armed on the
  ring can read its turn word;
- each slot is `producer = HOST`, the grant carries `PLUGIN_WRITE`, its
  `offset` is a multiple of 64 (§6.1), and its `extent` is at least the 256
  fixed header bytes (§5.1);
- `buffers` and `slot_bytes` are zero and `epoch_offset` is absent: the slot
  names its volume in its own header (`t`, `open_generation`), and a pane
  header beside it would be a second answer to the same question.

Everything else about a slot is in its header, which changes every turn and is
checked before each one (§7). A grant cannot check it: the header is read only
by the turn's holder (§6.1).

**The same mapping is granted to every session in the chain, once per session,
under one `segment_id` that is the same in every session.** The mapping's
preamble names one id and every receiver checks it against its grant
(`doorbell_M_memory.md` §5), so the ring cannot carry a different id per
session. Nothing collides: `segment_id = (session epoch << 32) | index`, and
session epochs never alias (`doorbell_M_memory.md` §3), so every id a host mints
is unique across all its sessions. The host mints a ring's id from an epoch no
session uses. A plugin serving several stages of one chain, or one stage at
several positions, holds the ring once.

The grant is read–write for the plugin: every stage writes. On Linux the memfd
therefore carries no future-write seal (`shared_memory_protocol.md`'s read-only
grants do); on macOS and Windows nothing changes. A plugin cannot tell that
another session holds the same memory, and must not rely on anything in a slot
outside its own turn (§6).

**Arming** tells a stage where it sits:

```
StageArm     host → plugin   arrangement  { stage_id, revision, segment_id, pipeline,
                                            position, input: StageShape,
                                            output: StageShape, new_channels, params }
StageArmAck  plugin → host   ack          { stage_id, revision, pipeline, position }
```

- `pipeline` names the chain, `1–65 535`, unique among the chains a host runs at
  once. `position` is a place in it, **`1–32 766`** (§6.1 stores `position + 1`
  and `−position` in a signed 16-bit field). Together they are what a slot's
  turn word holds when the slot is this stage's, so a plugin armed at several
  places needs only the word to know which of its turns it is.
- **vizi assigns positions; a plugin does not choose where it runs.** A position
  no stage is armed at is the host's. A host that wants to look at a slot
  between two stages leaves a position unarmed between them (stages at odd
  positions, the host at every even one). A host that does not numbers the
  stages consecutively, so one hands straight to the next (§6.1).
- **An arm is identified by `(pipeline, position)`**, and its `revision` counts
  within that pair. One stage may be armed at several positions, in one chain
  or in several; each is its own arm, and an arm at a new position adds to the
  stage's arms rather than replacing one.
- `input` is what this stage will be handed there, and `output` is the plan the
  host accepted for it: the largest the stage produces from that input (§3).
  **The host plans and arms one position at a time**, so the arm, not the last
  `StagePlanned` the plugin sent, is what the stage is held to. A stage whose
  own parameters change so that the armed plan no longer holds refuses
  (reason 3, §6.3) until it is planned and armed again. **The SDK enforces it**:
  a turn that would finish with a `dtype` other than `output.dtype`, or any
  allocation larger than `output`'s, is handed on *failed*, reason `2`,
  instead of *processed*, because the stage's own code broke its plan. The
  host may check too, but a stage must not rely on that. The needs a stage
  refuses on (§7) are likewise the ones planned for **this** arm, not whatever
  a later `PlanStage` for another position answered.
- `params` are the values this position runs with: the ones the accepted plan
  was made with (§2). An arm whose `params` do not match the stage's
  declaration is refused like any other unusable arm, below.
- `new_channels` maps each `adds[].key` of this stage's plan to the channel
  index the host assigned. **Indices are assigned above the source's channel
  count**, so each channel has one identity through the whole chain. They are
  `u16` in the slot (§5.2), so a chain holds at most **65 536 channels**
  (indices 0–65 535), which is also the control plane's repeated-field bound
  (`control_plane_protocol.md` §9). A `StageShape` or `StagePlanned` naming an
  index above 65 535 is answered `Error{PROTOCOL}`, and a chain that would need
  more is not armed.

`StageArm` is an **arrangement** (`control_plane_protocol.md` §5.5): the host
hands a slot to a position only after `StageArmAck` for its current `revision`.
Re-arming (a new plan, a new ring) is a new revision.

**A host may re-arm a position while the stage holds a turn there.** The
receiver installs and acknowledges the new revision at once; the turn in
progress finishes under the revision it began with, and every turn the stage
starts afterwards is held to the new one. The receiver checks the installed
revision when it **starts** each turn, not once per pass over the ring, so a
re-arm acknowledged while one slot runs governs the next.

**The host arms only after the ring's grant is installed**: after the
`GrantAck` that names it installed. A receiver given a `StageArm` it cannot
use (its `segment_id` names no installed `S` segment, its `stage_id` names no
stage the plugin declared, its `pipeline` or `position` is out of range, its
`params` do not match the declaration, it carries no `input` or no `output`,
it names one channel index twice among `input.channels` and `new_channels`, or
no answered `PlanStage` for its `stage_id`, `input` and `params` stands behind
it)
does not acknowledge it, and reports `ContractViolation` at
`STAGE_ARM_REFUSED`; the arrangement's deadline (`control_plane_protocol.md`
§5.5 rule 5) is how the host sees it. It is never held for a grant that may
arrive later: a conforming host has already had the grant acknowledged.

**Re-attaching a larger buffer.** When the chain changes so that `plane_cap` or
`capacity` must grow (a stage appended that adds channels, or whose output needs
more bytes than any step before it), the host grants a new `S` segment **superseding** the old
(`shared_memory_protocol.md` §9.5: only once every slot's turn is back with the
host) and re-arms every position with the new `segment_id`. **Disarming** is
retiring the segment (§9). An arm's identity outlives its ring: an arm naming
a retired ring is inert, and re-arming it, against a new ring or in a later
chain reusing the same `(pipeline, position)`, is its next revision.

---

## 5. The slot

Every multi-byte field is little-endian (`shared_memory_protocol.md` §1).
Offsets are from the start of the region.

### 5.1 The header

**The header is variable-length**: 256 fixed bytes, then the channel array, one
`u16` per allocated plane. At the largest legal `plane_cap`, 65 536 (§4), the
header is `256 + 131 072` bytes, 131 328 once rounded to 64; a hyperspectral
65 536 × 32³ volume therefore fits, and no legal combination of parameters
outgrows the format. Nothing else in the header scales with anything.

| offset | field | type | meaning |
|---|---|---|---|
| 0 | `turn` | u64, atomic | which volume, which pipeline, and who holds it (§6.1) |
| 8 | `outcome` | u32 | the last stage's result: `0` none, `1` processed, `2` refused, `3` failed |
| 12 | — | u32 | zero |
| 16 | `t` | u32 | the volume's timepoint |
| 20 | `done_through` | u32 | the last position whose outcome was *processed*; `0` for none |
| 24 | `reason` | u32 | with *refused* or *failed*: why (§6.3) |
| 28 | `dtype` | u32 | the `DType` of every plane, **now** (§5.3) |
| 32 | `planes` | u32 | planes present: the length of the channel array in use |
| 36 | `plane_cap` | u32 | planes allocated (§3.2) |
| 40 | `z_alloc`, `y_alloc`, `x_alloc` | u32 ×3 | each plane's layout, **now** (§5.3) |
| 52 | — | u32 | zero |
| 56 | `capacity` | u64 | bytes of data space after the header, fixed for the ring (§3.2) |
| 64 | `z0, zN, y0, yN, x0, xN` | u32 ×6 | the extent (§5.4) |
| 88 | `origin` | f64 ×3 | z, y, x (§5.4) |
| 112 | `pose_r` | f64 ×9 | rotation, row-major (§5.5) |
| 184 | `pose_t` | f64 ×3 | translation (§5.5) |
| 208 | `open_generation` | u64 | the dataset this volume came from (`ViewState.open_generation`) |
| 216 | — | 40 bytes | zero |
| 256 | `channels` | u16 × `plane_cap` | the channel index of each plane (§5.2) |

The data begins at `header_bytes = 256 + 2·plane_cap`, rounded **up** to a
multiple of 64. `header_bytes + capacity` must **fit** the region's extent,
and a slot whose header does not is invalid (§7); a region may be larger,
since the host rounds regions to its own alignment and the space past the data
is never read. `plane_cap` above 65 536 is invalid, whatever the region holds. **Every field marked `—` is
reserved**: written zero, and a non-zero one is an invalid header, as non-zero
preamble padding is an invalid grant.

### 5.2 Planes and channels

The planes are **packed**: with `plane_bytes = z_alloc · y_alloc · x_alloc ·
itemsize(dtype)`, plane `p` (`0 ≤ p < planes`) starts at
`header_bytes + p · plane_bytes` and holds one channel as a C-order
`(z_alloc, y_alloc, x_alloc)` array of `dtype`. In numpy the data is one
`(planes, z_alloc, y_alloc, x_alloc)` array.
**`channels[p]` is its channel index.** The slot holds only the channels that
were loaded (§3.1), so plane `p` is not channel `p` in general. **The indices
in use are distinct**: a header naming one channel at two planes is invalid
(§7), as is a `StageShape` that does. A stage that adds
a channel appends a plane: it writes the plane, sets `channels[planes]` to the
assigned index (§4), then increments `planes`.

### 5.3 Changing the layout

`dtype` is one type for every plane, and `z_alloc × y_alloc × x_alloc` is every
plane's layout. **A stage that changes either repacks all the data**, since a
change of width or of shape moves every element, and then writes the new
`dtype` and allocation into the header. How it repacks in place (in order, via
scratch memory of its own, or otherwise) is the stage's business; the result
must be the packed layout of §5.2 and must fit `capacity`. A stage that changes
neither leaves every plane where it is.

### 5.4 Extent and origin

- **The extent** `[z0, zN) × [y0, yN) × [x0, xN)` is the valid rectangle,
  the same for every plane: in numpy, plane `p` is valid at
  `slot[p, z0:zN, y0:yN, x0:xN]`. It lies within the allocation. The host fills
  it as the whole allocation; a stage never makes valid what it was not given
  (it may shrink the valid set, and it re-states the extent in its own layout).
- **The origin** is where voxel `(z0, y0, x0)` sits in vizi's level-0 voxel
  space, as floats. Layout and placement are independent: the data may sit
  anywhere in the allocation and be placed anywhere by the origin, even though
  it is often convenient for the two to agree. The host fills the origin as
  `(z0, y0, x0)` of the source.
- **A consumer that does not use fractional voxels rounds each component** with
  exactly `round(x) = ceil(x) if ceil(x) − x < x − floor(x) else floor(x)`: an
  exact half rounds down. Everyone who rounds then agrees on the voxel.
- **Data that the origin places outside vizi's bounds may be ignored.**

### 5.5 The pose: how the frame moved in real space

The origin places data in vizi's voxel space. **The pose relates that space to
real space**, so a region meant to be *fixed in real space* can be adjusted
while one *fixed in the data stream* is not, and a stage can move to an
object-centred frame without storing the whole blank field.

Positions are physical: a voxel at slot index `(z, y, x)` is at vizi position
`v = origin + (z − z0, y − y0, x − x0)`, and at physical position
`s = spacing ⊙ v`, where `spacing` is the dataset's voxel pitch in
`control_plane_protocol.md` §8.11's shape (its display fallback included, so the
arithmetic is defined even when the pitch is not measured). Its real-space
position is

```
r = R · s + T          R = pose_r (3×3, row-major, acting on column (z, y, x)),  T = pose_t
```

**The pose in the header is always absolute**: it maps this slot's data to real
space, whatever stages came before. The host fills the identity (`R = I`,
`T = 0`). A stage that moves the frame writes the new absolute pose. When it
knows its own motion relative to its input, `s_in = R_k · s_out + T_k`, the new
pose is the composition with the pose it found in the header, as **one
simultaneous update**:

```
(R, T) ← (R · R_k,  R · T_k + T)       R and T on the right: the values before this stage
```

A stage that computes an absolute pose directly (registration to a fixed
reference) writes it as is. `R` is a rotation
(orthonormal, determinant +1); a stage that only drifts leaves it alone.

---

## 6. A turn

### 6.1 Who may touch the slot

`turn` is one `u64`, the slot's only atomic, accessed only through atomic
operations (as Pulsed's state word is, `shared_memory_protocol.md` §6.2):

| bits | field | meaning |
|---|---|---|
| 0–31 | `iteration` | which volume: the host increments it on every fill of the slot, skipping 0 when it wraps; `0` means never filled |
| 32–47 | `pipeline` | the chain (§4), unsigned |
| 48–63 | `position` | who holds the slot, **signed** 16-bit: `0` the host; `k > 0` position `k`; `−k` the host, after the stage at `k` refused or failed |

**A slot whose `iteration` is `0` has never been filled**, and is ignored
whatever its other bits say.

**Only the holder of the turn writes anything in a slot, header or data.**
Position `k > 0` is held by the stage armed there, or by the host if no stage
is (§4); `0` and every negative position are the host's. The turn moves with
`iteration` and `pipeline` fixed for the whole pass:

```
host fills ──► 1 ──► stage at 1 ──► 2 ──► stage at 2 ──► … ──► n + 1: the host
                          │
                          └── refused or failed ──► −1: the host
```

- **A stage that processed stores `position + 1`.** If a stage is armed there,
  it may take the turn the moment it loads it; if none is, the host has it,
  which is how a host looks between two stages (§4), and how the host gets the
  slot back after the last one.
- **A stage that refused or failed stores `−position`.** The chain ends, the
  host has the slot, and the word alone says which stage stopped it.

The host never needs to relay a turn between consecutive stages, but it still
relays the hint: plugins never signal each other (§6.2).

**Handing over.** The holder writes everything it changes, then stores `turn`
with **release** ordering, then sends a `D` naming the slot's region to the
host. The next holder loads `turn` with **acquire** ordering before reading
anything else. **Each slot region begins at a multiple of 64 bytes**, so `turn` is
8-aligned for every implementation's 64-bit atomics.

### 6.2 The doorbell

`D` runs in both directions (`doorbell_D_notification.md` §5):

- **plugin → host**: "I have handed slot *s* on", naming the ring's
  `segment_id` and bit *s*;
- **host → plugin**: "slot *s* is at a position you are armed at", sent when
  the host fills a slot or hands it on, **and relayed** when one stage hands to
  the next.

`D` is a hint and carries no handoff; the turn word does. A `STAGED` region
passes §3's condition (e): reading `turn` claims nothing, so re-checking is
safe. **A receiver re-checks the turn words of its `STAGED` regions on its own
schedule** as well (conditions (a) and (b)), so a lost `D` delays a turn and
never loses one, and a stage that polls sooner than the relay arrives starts
sooner.

A stage acts on a slot when **one acquire-load of `turn`** shows a
`(pipeline, position)` it is armed at **and** an `iteration` different from the
last it processed in that slot at that position. The iteration is compared only
for equality, so its wrap is harmless, and it rules out acting twice on one
volume when the turn comes back to the same position with the next. A `D` for a
slot whose turn is not the receiver's is counted and ignored.

### 6.3 The outcome

Before handing on, a stage writes:

| `outcome` | when | `reason` | `done_through` | stores `position` |
|---|---|---|---|---|
| `1` processed | it did its work | `0` | its `position` | `position + 1` |
| `2` refused | it needs something it does not have | `1` needs earlier state (causal), `2` a declared input channel is absent, `3` other | unchanged | `−position` |
| `3` failed | it tried and could not | `1` the header is invalid (§7), `2` internal error, `3` other | unchanged | `−position` |

`outcome` is the last stage's; nothing resets it between stages, and the next
stage overwrites it.

- **On *refused*, the chain ends** for this volume, and the volume is used as
  far as `done_through` says it got. **A stage refuses before it writes
  anything**: everything a refusal can name (earlier state, an input channel,
  a plan that no longer holds) is knowable before work starts, and the data
  must still be the previous stage's output.
- **On *failed*, the slot's data is undefined** (the stage may have rewritten
  or repacked some of it) **and the host discards the volume.** `done_through`
  stays as a diagnostic.

---

## 7. Validation, deadlines and failure

**Every stage validates the header before using the slot**, because it was
written by the host or by another plugin, and a zero-copy handoff means nobody
checked it in between. The minimum:

- `header_bytes + capacity` fits the region's extent (§5.1), and
  `planes · plane_bytes ≤ capacity` (§5.2);
- `planes ≤ plane_cap ≤ 65 536`, `dtype` is a known `DType`, and the first
  `planes` entries of `channels` are distinct;
- the extent lies within the allocation, with `z0 ≤ zN` and so on;
- `origin`, `pose_r` and `pose_t` are finite;
- every reserved field is zero (§5.1);
- **the slot matches the arm**: `dtype` is the armed `input.dtype`, and
  `z_alloc`, `y_alloc` and `x_alloc` are each at most the armed input's (a
  plan states the *largest* output, so a slot may be smaller than planned).

A slot that fails is handed on *failed*, reason `1`, and reported with a
`ContractViolation` (`shared_memory_implementation_details.md` §2) at site
`STAGE_SLOT_INVALID`. An input channel the stage declared that is absent from
`channels` is *refused*, reason `2`, and not a violation.

**Deadlines are the host's.** A turn not handed on within the host's deadline
ends the chain for that volume. **The slot is then quarantined, not reused**:
memory cannot be taken back from a process that may still be writing it, and
only the holder writes the turn word (§6.1), so the host cannot take the turn
back either. It returns to use when its turn reaches a position the **host**
holds — past the last stage, at a negative position, or at a position no stage
is armed at — or when the holding stage's session ends. The host discards its
contents then.

**Meanwhile the abandoned volume travels like any other.** A late stage does
not know it is late: it stores `position + 1` as usual, and if a stage is armed
there the turn is that stage's, which may process or refuse the volume as it
would any other. Only when the turn comes back to the host does the host see
the volume again. The deadline's value is the host's policy and is not fixed
here.

**A stage whose session ends while it holds a turn** loses it: the host takes the
turn back, treats it as *failed*, and discards the volume, which may be
half-written.

---

## 8. Open

- **Order and UI.** Who composes a chain and where it is shown. This document
  gives the host everything it needs to run one, and says nothing about how a
  person builds it.
- **Where results go.** Showing a processed volume live, and writing it as a
  compressed OME-Zarr, one record per timepoint, are host features that consume
  a finished slot; neither is specified here.
- **Recording the pose in a result**: in metadata, or as a Zarr whose chunks
  outside the moving frame are simply missing.
- **The deadline's value** (§7).
- **A stage asking to be re-planned.** A stage whose parameters change refuses
  until the host plans and arms it again (§4); a message by which it asks for
  that is not specified.
