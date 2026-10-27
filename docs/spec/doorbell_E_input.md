# `E` — tool input events

Host→plugin input for a bound tool. Transport rules are in
`doorbell_protocol.md`.

The control-plane messages this document assumes are inventoried in
`doorbell_implementation_details.md` and specified in
`control_plane_protocol.md` §8.10.

**Direction: host→plugin only.** A plugin sending `E` is a session fault
(`doorbell_protocol.md` §2). **Trust:** vizi controls its own sends, so a
record cannot be corrupted by the peer — but it can be *wrong*, because vizi can
have bugs, which is what §2's disagreement rule is for. **Rate-limit
ownership:** the receiving plugin owns its processing budget; the host does not
pace on its behalf beyond transport backpressure, and `E` cannot coalesce (§7).
**Ancillary descriptors: none, ever, on any platform.**

---

## 1. Payload

Offsets are from the start of the 100-byte **payload**, i.e. record byte 8.

```
[0..4)    gen           u32   which dataset (open generation)
[4..8)    t             u32   time INDEX
[8..12)   interaction   u32   gesture or fire id; never 0
[12..16)  seq           u32   per-interaction; 0 on PRESS and FIRE
[16..20)  epoch         u32   the pane epoch this was captured under; never 0
[20..24)  target_id     u32   host-owned object; 0 when none
[24..28)  target_part   u32   meaning defined by target_kind
[28..32)  tool          u32   activation handle; never 0
[32..34)  channels      u16   bits 0-14 = visible mask, channels 0-14
                              bit15 = a channel at index >=15 is also
                                      visible; which, is epoch state
[34]      phase         u8    PRESS | DRAG | RELEASE | CANCEL | FIRE
[35]      view_id       u8    which pane; host-assigned
[36]      level         u8    which grid the coordinates index
[37]      mods          u8    modifier flags
[38]      click_count   u8    host-determined
[39]      target_kind   u8
[40]      payload_kind  u8
[41]      position_flags u8   MISS = 0x01; ROLL_REF_Y = 0x02;
                              bits 2-7 reserved, MUST be 0
[42..44)  action        u16   which declared action a FIRE names,
                              1-based in manifest order; 0 = the
                              primary gesture (every other phase)
[44..92)  position      48 B  union by payload_kind; unused tail MUST be zero
[92..100) padding        8 B  MUST be 0
```

**Every multi-byte field is naturally aligned, and the order is the record's
own**: the u32 identity and context fields first, then the u16, then the bytes,
then the union. `epoch` sits with `gen` and `interaction` because it is context,
not payload — the union is the only variable thing here and it goes last.

**The camera is inside the ray layouts, not a bare field** (§3). It is a 3D
thing, so it lives where the 3D kinds are and does not exist for the others.

`epoch` is the version of the pane's interpretation parameters (§1a). It is
`view_id`-scoped, never 0, and **monotonic but not consecutive from any one
plugin's view** — a plugin sees only the epochs emitted *to it*, which is a
subsequence of the pane's chain (§1a). A gap is not loss and is never counted as
one, unlike `id`, `seq` and `interaction`, all of which are consecutive by
construction.

`action` is how a key or a pane button reaches the ENGAGED tool without ending
its gesture (`control_plane_protocol.md` §8.10): the mouse is the target and the
key is the fire — a node placed on a polygon mid-drag. It is set only on a
`FIRE`, names the action's index in `ToolBinding.actions`, and the record
carries the pointer's sample at that moment. A `FIRE` with an action is
delivered whether or not a gesture is open and touches gesture state exactly
as any `FIRE` does: not at all (§2). An index the plugin's arrangement does not
carry is forward compatibility — the tool receives the record and
`ctx.action(ev)` answers nothing — never a fault.

**Padding, not reserved.** The tail range is not growth space: a field placed
there would change the layout, which bumps `ver`, which the handshake rejects for
an older peer. So a non-zero byte is a framing fault, while a reserved *bit* or
enum value inside a populated field is ignored and counted — the two are opposite
on purpose and `doorbell_protocol.md` §8 draws the line once.

Every field is naturally aligned. `tool` is u32 rather than u16 because a
handle is never reused within a session (§5) and 65,536 activations is not
obviously unreachable for a scripted plugin; the width removes the exhaustion
case instead of specifying behavior for it (`plugin_principles.md` 8).

### Constants

These are normative. Without them a record is not derivable from its fields and
`doorbell_protocol.md` §13's byte-exactness is unobtainable.

**`phase` u8**

| value | | value | |
|---|---|---|---|
| `PRESS` | 0 | `CANCEL` | 3 |
| `DRAG` | 1 | `FIRE` | 4 |
| `RELEASE` | 2 | `HOVER` | 5 *(reserved)* |
| | | `LEAVE` | 6 *(reserved)* |

5 and 6 MUST NOT be emitted by a host (§9); a plugin receiving one ignores the
record.

**`payload_kind` u8**

| value | | value | |
|---|---|---|---|
| `NONE` | 0 | `COLUMN_ZY` | 5 |
| `VOXEL` | 1 | `RAY` | 6 |
| `PICKED` | 2 | `RAY_AND_PICKED` | 7 |
| `COLUMN_YX` | 3 | | |
| `COLUMN_ZX` | 4 | | |

**`position_flags` u8**: `MISS` 0x01, `ROLL_REF_Y` 0x02 (§3), bits 2–7 reserved
and MUST be 0. `MISS`
says the coordinate is real but **does not land in the data** — see §3. It is a
flag rather than a parallel set of `*_MISS` kinds so that "did the user point at
data?" is one bit in every view, and so that a kind added later inherits the
distinction for free.

**`target_kind` u8**: `NONE` 0, `ANNOTATION` 1, `ROI` 2, `HANDLE` 3; 4 and above
reserved. An unknown value means `target_id` and `target_part` are
uninterpretable, so a plugin ignores all three — it does **not** treat the
record as invalid, because a later host may name a target class this plugin
predates.

**Unknown values.** A later host may name things this plugin predates, so the
rule is per field and never "drop the record" unless the record is unusable:

| field | unknown value |
|---|---|
| `target_kind` | ignore `target_kind`, `target_id`, `target_part`; the rest of the record is fine |
| `payload_kind` | **process the record with no position** — treat it as `NONE`, ignore the whole `position` field, and do not check its padding, whose extent is unknowable |
| `phase` | **ignore the record** — the one exception, below |
| a reserved bit set in `mods` or `position_flags` | **ignore that bit**, process the record |
| `MISS` set with `payload_kind = NONE` | ignore `MISS`; there is no coordinate for it to qualify |


**`phase` is a declared exception to `doorbell_protocol.md` §8's rule that an
unknown enum value is ignored and the record processed** — the same shape as
`M`'s reserved `perms` bit, and flagged here for the same reason. `phase` is not
a property of the record, it is a *transition*: it decides whether an interaction
opens, continues or closes. A receiver cannot process a transition it does not
understand, and guessing which of the three it is corrupts the gesture state the
rest of this document depends on. `payload_kind` and `target_kind` obey §8
normally.

`payload_kind` is the one that matters among those, because
`doorbell_protocol.md` §8 names it as the canonical op-owned field: without it
the union's extent is unknown.
Dropping the record instead would break gesture bookkeeping — a dropped `PRESS`
loses the whole stroke — whereas a phase transition nobody understands is worse
acted on than missed.

**None of these is a violation and none is reported to the host.** They are
forward compatibility working as designed — a newer host naming something this
plugin predates — so they are **local diagnostics**, aggregated, not
`ContractViolation`. Reporting them would make every protocol addition look like
a defect at the far end.

That includes the reserved bits, and the "MUST be 0" on them binds the **sender**
only. A receiver that rejected an unknown `mods` bit would turn the first new
modifier into a session-wide failure at every plugin built before it, which is
precisely the outcome reserving bits is meant to avoid.

**`mods` u8**, one bit each: `SHIFT` 0x01, `CTRL` 0x02, `ALT` 0x04, `META` 0x08
(Command on macOS). Bits 4–7 reserved, MUST be 0. The host maps its own platform
modifiers onto these; a plugin never sees a platform-specific code — a Qt modifier
flag is a 32-bit value and could not fit here even if we wanted it to.

`channels` is a set, not a channel. In multichannel display no single channel is
what the user is seeing, and a single channel index can name one that is not
visible at all. One bit set is the degenerate single-channel case, not a special encoding;
zero is "nothing visible", which is a real state.

**bit15 says only whether a channel at index ≥ 15 is visible.** Bits 0–14 always
carry the visible mask for channels 0–14, in both forms. When bit15 is set, one
or more channels at index ≥ 15 are *also* visible, and **which ones is part of
the pane's epoch state** (§1a) rather than anything the record carries.

**So the first fifteen channels are free.** Toggling one changes only the mask,
per record, mid-gesture, exactly as `t` and `level` do. Toggling a channel at
index ≥ 15 changes what bit15's "consult the epoch" resolves to, so it is an
epoch change and costs a round trip — the rare case paying for itself rather than
the common one paying for it.

There is no channel-set version, no id and no arranged handle. The epoch covers
it, so there is no exhaustion bound, no reuse question and no not-yet-synced
disposition.

---

## 1a. Epochs

**Every record is emitted under a pane epoch, and carries it.** An epoch is the
version of one pane's *interpretation parameters* — the things that decide what a
coordinate means rather than where it points: voxel spacing, extent, crop, the
level table, the index→time mapping, and the visible channels at index ≥ 15.

**The camera is NOT one of them.** A camera move is not an epoch change, because
every ray record carries the camera it was cast from (§3), so a camera move
invalidates nothing and there is nothing for an epoch to protect. `gen` is not one either: it is the dataset *open* generation, and
reopening a dataset can change the underlying data, so both fields stay.

### The first epoch is negotiated, like every other

**A pane has no implicit or default epoch, and no events precede its first one.**
Establishment is the same exchange as a change, with the same gate: vizi sends
the pane's full state, the plugin acknowledges `(pane, epoch)`, and only then can
a record for that pane exist. There is no epoch 0 to fall back to — 0 is the "no
epoch" sentinel and a record carrying it is `zero_id`.

**Subscription is what triggers it.** A plugin must subscribe to receive events
at all, and that subscription is also its subscription to the epoch chain: on
subscribing it receives the current epoch and state for each pane it will be
sent events for. A pane created later is established the same way, when it
appears.

**A plugin's first epoch for a pane is whatever that pane is currently at, not
1.** A plugin subscribing an hour into a session joins a chain already in
progress. Nothing is replayed, because an epoch is state rather than a
transition, and the intermediate states are exactly what nobody needs.

**This gate and the tool-activation gate are independent, and both apply.** A
binding is not installed until its activation is acknowledged
(`doorbell_implementation_details.md` §5); a pane emits nothing until its epoch
is acknowledged. A tool bound in a pane whose epoch is still pending receives
nothing, and that is neither an error nor a race — it is both gates doing their
job.

**A dataset reopen is an epoch change for every affected pane**, not a separate
mechanism. `gen` advances too, and the two are different facts: `gen` says the
data may have changed underneath, the epoch says how to interpret coordinates.

### The exchange

1. A parameter changes. **vizi stops emitting events for that pane**, to every
   plugin.
2. vizi sends the **full state** for the new epoch over gRPC — one message may
   carry several `(pane, epoch, state)` entries, because one change can affect
   several panes at once (a 2D and a 3D pane over the same data).
3. The plugin **drains its doorbell queue** and processes or discards what it
   finds, as it sees fit.
4. **Only then** does it acknowledge, as `(pane, epoch)` pairs. It may
   acknowledge some panes and not others; each pane resumes on its own.

   **A repeated `EpochChange` carrying the same `(view_id, epoch, state)` is
   acknowledged again**, and closes nothing the second time — the host re-sends
   on a lost acknowledgement, so a plugin that suppressed the repeat as a
   duplicate would have a correctly installed epoch escalated. The same
   `(view_id, epoch)` carrying *different* state is a host bug and is reported.

   **There is no epoch 0 to acknowledge.** Zero is the absent-epoch sentinel and
   is a `zero_id` `ContractViolation` on the wire, so the host never emits it as
   a pane's epoch and `epoch_acknowledged(view_id, 0)` fails without mutation
   (`doorbell_implementation_details.md` §6) — it is not "the epoch before the
   first". The first epoch a pane has is the first one the host allocates, and
   until it is acknowledged the pane has none, which is the state this section
   describes.

   **The transport is told first.** `epoch_acknowledged(view_id, epoch)` MUST be
   called before the gRPC acknowledgement is sent, exactly as `tool_activated`
   must precede the activation ack (§2) and for the identical reason: acknowledge
   first and a fast record under the new epoch reaches the epoch check before the
   value is installed, is ruled *newer*, and is dropped as `epoch_mismatch`. The
   record dropped that way is most often the `PRESS`, so the whole stroke is lost
   — and the plugin reports a host bug caused entirely by its own ordering.
5. vizi emits under the new epoch **to that plugin**, and to any other plugin
   that has acknowledged. A plugin that has not acknowledged simply receives
   nothing for that pane.

**This section is the source of truth for the epoch gate** (`plugin_principles.md`
13); `control_plane_protocol.md` §8.10 specifies the messages that carry it and
cites this rule rather than restating it.

**Events flow for a `(plugin, pane)` exactly while that plugin's acknowledged
epoch is the pane's current one.** That single condition is the whole gate.

### Why this needs no marker on the doorbell

The obvious alternative is a fence record in the stream, whose position defines
the boundary. This is better on two counts:

- **It does not depend on ordering.** Every record carries its epoch, so a stale
  one is identifiable on its own merits. A fence's correctness rests on in-order
  delivery, which is guaranteed for `SOCK_SEQPACKET` and TCP but only *measured*
  for macOS `SOCK_DGRAM` (§6).
- **The drain is an optimization, not the mechanism.** It cannot prove
  completeness anyway — the doorbell and gRPC are separate sockets with no
  cross-channel ordering, so a record may become readable after the epoch message
  arrives. Draining first means the plugin loses fewer events it could have used;
  the epoch on the record is what keeps it correct either way.

### Records from limbo

A record transmitted before vizi stopped, but not yet drained, arrives carrying
the **old** epoch. This is ordinary, not a defect.

**The record is discarded and counted. The closure is not its job.**

**That describes a record that REACHES the epoch check**, which is step 8 of the
ordered algorithm — after the semantic checks at steps 3–7. A stale record that
is *also* malformed is rejected earlier, on its own terms, and takes that
rejection's consequences: if it is an opening record it performs the implicit
close like any other (§2). The exemption is for records discarded **for
staleness**, not for every record that happens to carry an old epoch, and §2's
list of exemptions says so by naming step 8 rather than the property.

**Closing the pane's gestures happens when the new epoch is INSTALLED**, in
`epoch_acknowledged` (`doorbell_implementation_details.md` §6) — the one event
guaranteed to occur exactly once per change, on both sides. A stale record is not
that event: the host may cancel locally and emit nothing at all, in which case
none ever arrives and the gesture would stay open forever.

**What the tool does with the partial stroke is its own business** — commit it
against the old epoch's state or discard it. **A plugin that offers the commit
option retains the superseded state until the cancellations from that epoch have
been delivered**, since installing the new one is synchronous while the outcomes
are drained later; without that, two plugins commit the same stroke against
different geometry. What
no plugin may do is interpret a stale coordinate under the *current* epoch.

**A record carrying a FUTURE epoch is a host bug** — vizi may not emit under an
unacknowledged epoch — and is a `ContractViolation`, `where = epoch_mismatch`.

### A gesture that straddles a change

**vizi reuses the same `interaction`** across the boundary and does not track the
gesture — but **the plugin cannot continue it.** `epoch_acknowledged` closes
every interaction in the pane unconditionally, so no conforming transport leaves
a straddling gesture open, and a later record naming that `interaction` is an
unknown one.

What the plugin still chooses is what the *tool* does with the partial stroke:
the cancellation outcome hands it everything captured under the old epoch, and it
may commit that against the old state — which it still holds — or discard it.
Nothing on the host side depends on which.

### Intermediate epochs are unobservable, and are therefore not numbered

vizi holds, per `(plugin, pane)`, **one acknowledged epoch and at most one
pending one**. A parameter change while an epoch is pending replaces the pending
state rather than queueing behind it, so the burden is fixed rather than growing
with a stalled plugin.

**An epoch number is allocated when a state is first emitted to any plugin,
never when a parameter changes.** A state that comes and goes while every plugin
is still pending is never emitted, so no record can reference it and numbering it
would be waste.

**The number is the `view_id`'s, not the plugin's**: every plugin receiving a
given state receives the same number for it. So a fast plugin and a slow one
agree about what epoch 7 means, and the slow one simply never sees 5 and 6 —
which is why a plugin observes a subsequence and must not read a gap as loss.

> **The counter is keyed on `view_id`, advances for the life of the session, and
> NEVER restarts when a `view_id` is reassigned to a new pane.**

That is a host obligation, and it is the whole of what makes reassignment safe.
`view_id` is reusable (§2) and panes do close, so a counter restarting with the
pane would hand the new one epoch 1 while the transport still holds a higher
installed value: `epoch_acknowledged` rejects it as older, every record for that
pane is discarded at the epoch check, and **the plugin's tools in it are silently dead**
with a local counter as the only symptom.

**The obvious alternative is worse.** Clearing the installed epoch when a pane
closes would make genuinely stale limbo records classify as *newer* —
`epoch_mismatch`, a spurious host-bug report at mouse rate on the channel needed
for recovery. Never restarting keeps the transport's never-rewind rule correct
as written, and needs no lifecycle input to clear anything.

What the number must do is *advance whenever a new state is emitted*: a superseded
pending epoch that kept its number would have its stale acknowledgement mistaken
for an acknowledgement of the replacement, and vizi would emit under a state the
plugin never received.

### If the acknowledgement never comes

A plugin that does not acknowledge receives no further events for that pane —
its tools go dead, silently, which is precisely the degradation
`plugin_principles.md` 9 forbids. **The epoch acknowledgement therefore has a
deadline**, and it is the same shape as `doorbell_M_memory.md` §7's missing-grant
deadline and the same class of hazard: an absence rather than an error. On expiry
vizi re-sends the epoch idempotently; on continued silence it escalates -- gives
the arrangement up and reports it (`control_plane_protocol.md` §9's
"escalate"). The deadline is host-side, 10 s, set in that section's table.

### The trade

Every interpretation change costs a control-plane round trip before events
resume, paid at user rate. **Latency is cheaper
than acting on the wrong data**, and this is the one place in the protocol where
that trade is made deliberately rather than avoided.

`t` is an **index**, and it is on the record for the same reason `channels` is:
**so a plugin can read the voxels the user is actually looking at.** A coordinate
without the frame it was taken in names a different value on every other frame,
and a tool that samples, paints or measures needs `(t, channels, level,
position)` together or it is querying something the user never saw. Both are
per-record because both change during a gesture — the user may scrub or toggle a
channel mid-drag.

The index→time mapping is arranged once per `gen` over gRPC. **Absent an arranged
mapping a plugin may not assume uniform spacing**; silence means unknown, not
"multiply by a constant".

`gen` is u32 here while the control plane's `open_generation` is `uint64`. It is
session-scoped and advances once per successful dataset open, so it cannot wrap
in a session a human sits through; it is listed with the other session-scoped
handles in `doorbell_M_memory.md` §3. A receiver correlating the two compares the
low 32 bits.

**Coordinate conventions**, without which none of §3 is interpretable:

- **One convention, `control_plane_protocol.md` §8.11's: integer `i` is the
  CENTER of voxel `i`, which spans `[i − 0.5, i + 0.5]`.** The crop travels in
  it (`Geometry.crop`, faces on half-integers), the level map is derived from
  it, and every continuous coordinate on an `E` record is in it: a `VOXEL` or
  `PICKED` coordinate is a real position in that space, not an index that
  happens to be a float, and **`round` — not truncation — gives the containing
  voxel**. A pane's own continuous space is the host's business. The reference
  host's 2D panes count a texel or a scene pixel, where voxel `i` is
  `[i, i+1)`, and their encoder shifts by half a voxel on the way out
  (`tool_controller.centred`); its 3D frame already draws voxel `i` centered on
  `i`, so a 3D ray goes out as it is.
- **Extents are half-open**, matching `Geometry`'s, and the clip box's faces
  are at half-integers: a `RAY` through a volume of `Z = 100` exits at
  `99.5`, the high face of voxel 99, and enters at `−0.5`.
- **A column's range is half-open too**: `z_min` inclusive, `z_max` exclusive.
- **Non-finite values are forbidden.** A sender must not emit NaN or an infinity
  in any position field — coordinates come from projecting a real cursor
  position, so nothing produces them — and a receiver treats one as a **malformed
  position field**, with the disposition §2's table gives that class. "Out of
  contract, do what you like" cannot be conformance tested. This is distinct from *out of extent*, which a `MISS` coordinate
  deliberately permits (§3).

Positions are f32: ULP at 10⁶ voxels is 0.0625 and real datasets are 10³–10⁴.

---

## 2. Interactions

| phase | `interaction` | `seq` |
|---|---|---|
| PRESS | opens | 0 |
| DRAG | continues | +1 |
| RELEASE, CANCEL | closes | +1 |
| FIRE | opens and closes | 0 |

`seq` increments by **exactly one**, because gap detection in §6 is a comparison
against the previous value and nothing else establishes loss. **The comparison is
plain, not wrapping**: `seq` is per interaction and one gesture cannot reach 2³²
samples — at 1 kHz that is fifty days of unbroken dragging — so unlike `id` it
needs no serial arithmetic.

**One rule covers every never-reused counter here** — `seq`, `interaction` and
`tool` — because "cannot be reached" is not the same as "defined at the bound",
and a language's own answer at `u32::MAX` may be to wrap, to panic or to emit 0,
each of which breaks a different thing: a wrapped `interaction` is classified as
a duplicate and its action is dropped silently.

> **A sender terminates rather than emitting a value that wraps or repeats.**
> Exhausting `seq` ends that interaction with a `CANCEL`; exhausting
> `interaction` or `tool` ends the session with a diagnostic naming which
> counter. None is reachable in a session a human sits through, which is why the
> outcome is a clean stop rather than machinery. **It is allocated
at the same point as the record's `id`** — on admission to the writer
(`doorbell_protocol.md` §4) — so a `DRAG` the host drops on refusal has burnt its
`seq` and the gap is visible. Allocating it only for records that go out would
make `seq` continuous across exactly the loss it exists to report.

**FIRE is a complete interaction in one record.** It never opens a gesture, so a
plugin's open-interaction state is untouched by it and there is no terminal to
wait for. Modeling it as an immediate PRESS+RELEASE would force a plugin to infer
"a gesture with no drags" to recognize a click, and would deliver two records for
one action.

**CANCEL is the terminal for any gesture ending that is not a release**: Esc, a
tool switch, deactivation, a dataset change, a host-side abort. It may carry no
position.

**An interaction is scoped to one `gen`, one `view_id` and one `tool`.** The host
guarantees it — a dataset change, a pane change or a tool switch cancels the open
gesture first — and a record disagreeing with its open interaction is **not** an
implicit re-anchor.

The same applies to a **malformed `position` field** — a non-zero tail past the
kind's length, or a non-finite coordinate — which `doorbell_protocol.md` §7.5
step 7 assigns to this document. **Drop it and cancel**, per the rule below: a
bare drop would leave a gesture open forever if the dropped record was its
`RELEASE`.

### Every terminal closes its interaction exactly once

> **The question a terminal raises is never *whether* it closes, only whether it
> closes as a completion or as a cancellation.**

| the terminal | closes as | and |
|---|---|---|
| accepted | **completion** | the sample is surfaced |
| fails a rule the transport can check — a non-zero position tail, a non-finite coordinate | **cancellation** | `ContractViolation`, sample dropped |
| carries context nothing here *can* check — a `t` or `level` outside the arranged range, a non-`MISS` coordinate outside the geometry | **completion** | the transport surfaces the sample; **the supervisor** marks the part unusable and raises `unusable_context` (`doorbell_implementation_details.md` §6) |
| duplicate (`seq` at or below the last accepted) | — | the first one already closed it; ignore |

**The third row is the one that needs a rule**, because those facts need arranged
state the transport does not hold, and the supervisor may not reject a terminal
over them (`doorbell_implementation_details.md` §6). Marking the part unusable is
what lets a consumer act on the closure without guessing whether a coordinate it
was handed had been checked.

**What "cannot be reconciled" means, exactly**, since every SDK checks it
independently: `level` names an entry of the epoch's `Geometry.levels`; a
non-`MISS` voxel-naming coordinate — `VOXEL`, `PICKED`, the pick of
`RAY_AND_PICKED` — lies inside the clip box (`Geometry.crop`) converted to the
record's level per axis; and `t` indexes the arranged `TimeMapping`'s table
**when one is arranged** — `Geometry` carries no `T`, and silence about time
means unknown, so an unmapped `t` is unchecked rather than unusable. A ray's
eye and far point lie outside the box by construction and are exempt. The
reference SDK delivers the record with a local `usable = False` and reports
`unusable_context`.

### One rule for a rejected non-opening record

Rejecting a terminal is how a gesture gets stranded, so every rejection answers
the same question the same way:

> **Cancel the interaction the record identifies. If it identifies none, cancel
> the open gesture of the tool it identifies. If it identifies neither, cancel
> nothing.**

**"Identifies none" means `interaction == 0`, and nothing else.** A non-zero
`interaction` absent from the index does **not** fall through to the tool: it
identifies a particular gesture, and the gesture it identifies is simply already
over. Canceling the tool's *current* gesture there would destroy a newer,
unrelated interaction on the strength of a late record about a finished one —
which is the same harm as the wrong-`tool` case below, arriving from the other
direction. So a non-zero `interaction` the index does not hold cancels
**nothing**.

The distinction has to be stated because the sentence reads both ways and the
readings differ only in a case that is easy to miss: a malformed terminal
arriving after its own gesture closed. An implementation reading "identifies
none" as *any lookup miss* is correct on every other input and wrong on that one.

That covers the cases individually and consistently: a bad position tail names a
valid interaction, so that one is canceled; `interaction == 0` names none but
may name a good `tool`, so that tool's gesture is canceled rather than left
open — which is the stale state the tail rule exists to prevent, and it would
have been reintroduced by handling the two separately; a record naming an
interaction under the wrong `tool` cancels the **interaction**, never the tool's
own gesture, because the tool did nothing wrong; and `interaction == 0` with
`tool == 0` identifies nothing, so nothing is canceled.

**Opening records are outside this rule** — a rejected `PRESS` or `FIRE`
terminates nothing *by this rule*, and the gesture it would have opened is
simply lost. Every "cancel" in the table above is this rule applied to a
non-opening record; none of them is a separate policy.

**But a rejected `PRESS` still performs the implicit close its acceptance would
have**, and this is a separate rule because omitting it strands the *previous*
gesture rather than the rejected one. §6's implicit close means the host has
already moved on: it opened B for tool `h` and considers A over. If the plugin
rejects B and leaves A open, **A is open forever on one side and closed on the
other**, and every rule here is about avoiding exactly that.

> **A `PRESS` that is not a duplicate closes the prior gesture of the tool it
> names, whenever that tool is active — whether or not the record is then
> accepted. It does not open the rejected gesture.**

**"Duplicate" here is step 2's, which is narrower than "at or below the
watermark"**: an exact byte replay, or an id whose interaction has already
closed. A `PRESS` at or below the watermark that *differs* from the record that
opened the still-open interaction it names is not a duplicate and does close
(below).

Three edges the rule has to get right, and each is why it is worded on the
*tool* rather than on the record's disposition:

- **It is keyed on the tool being active, not on the record passing.** Most
  rejections happen *before* tool status is settled at step 9, so
  "the tool it names" is checked on its own terms: non-zero, and in the active
  set. A `PRESS` naming a never-seen or retired tool closes nothing, because
  there is nothing of that tool's to close.
- **A duplicate is exempt** (step 2), and so is a record **discarded for a
  stale epoch** (step 8) — in both the host opened nothing new, so closing would
  destroy a live gesture on a record carrying no news. Everything else that
  rejects a `PRESS` closes.

- **A duplicate means an exact replay, not merely a familiar id.** Where the
  named interaction is still open, the record is compared with the one that
  opened it: identical bytes are the retry §6 describes, and **anything else is
  a `ContractViolation` `interaction_mismatch` whose rejection performs the
  implicit close.** A reused id carrying a *different* `PRESS` would otherwise
  be dropped while its `DRAG`s were accepted as continuations of the old
  gesture — host and plugin silently disagreeing about which gesture is live,
  which is the one outcome dedup must not produce. Where the interaction is
  already closed there is nothing to compare and nothing to merge with, so the
  drop stands. **This is step 2, stated there in the same terms**; a receiver
  implementing the ordered algorithm alone must still perform the comparison.

  **All 112 bytes, header included.** A retry reuses its `id` unchanged
  (`doorbell_protocol.md` §4), so a genuine replay is byte-identical and the
  whole frame is the cheapest exact test — the same comparison
  `shared_memory_protocol.md` §8 makes on a duplicate grant. Comparing a chosen
  subset would need a rule about which fields may differ, and there are none.

  **So a receiver retains the opening record's 112 bytes while its interaction is
  open, and releases them when it closes.** That is the one piece of per-gesture
  storage this dedup needs, and it is bounded: at most one retained record per
  **active tool**, since a tool has at most one open interaction (§2). Once
  closed, an id needs no saved bytes — the watermark suppresses the replay on its
  own.
- **`FIRE` is exempt.** It never opens a gesture and never closes one (§2), so a
  malformed `FIRE` still cancels nothing.

**A malformed opening record does not advance the dedup watermark.** The record
was rejected, so nothing in it is trusted, including its `interaction`. The
gesture it would have opened is simply lost — correctly, since its `PRESS` is
where the anchor lives — and the records that follow it are ignored as naming an
unknown interaction, aggregated rather than logged per record at 1 kHz. The next
well-formed `PRESS` carries a higher id and opens normally, so nothing is stuck.

Such a record is a **semantic disagreement, not a framing fault**
(`doorbell_protocol.md` §8): it was well-formed, so the plugin reports it over
gRPC, drops it, and cancels the open interaction — a safe state — without
tearing the session down. The distinction matters because host→plugin is trusted
against *corruption* and not against *vizi bugs*, and killing a user's plugin
session over a host bug punishes the wrong party while the report is what
actually gets the bug fixed.

### Arrangement is complete before it is used

**As specified in `control_plane_protocol.md` §5.5, the host emits nothing
referencing an arrangement the plugin has not acknowledged.** What this section
adds is which of `E`'s references are arrangement: a tool activation, a `gen`'s
index→time mapping, a `gen`'s geometry and the pane epoch (§1 rule 1 of
`doorbell_protocol.md`), so they are paid once, at human rate, over gRPC. In
particular a **binding is not installed until its activation is acknowledged**,
which removes the race rather than buffering it: there is no queue to grow and
no event to drop.

**A re-sent arrangement does not interrupt a gesture, *unless it changes how the
gesture's coordinates are interpreted*.** `Geometry` is re-sent whole on any
change and can land mid-drag. For a change that does not affect interpretation —
a channel's display name, say — **any acknowledged `Geometry` for the record's
`gen` satisfies the rule**, the host neither blocks nor cancels, and a plugin
resolving a buffered stroke uses whichever it holds.

**A change to voxel spacing, extent, crop or the level table cancels every open
gesture in that `gen`**: the coordinates stay valid — they are voxel indices —
but everything computed *from* them in physical space does not, and a stroke
half-resolved under one spacing and half under another is wrong in a way nothing
on the wire can detect. **This is not the reason a camera move can cancel**, and
the two should not be read as one rule: a camera move invalidates nothing, since
every ray carries the camera it was cast from (§3), so it cancels only where a
binding asked it to and for a reason about *intent* rather than interpretation
(§5). Interpretation changes cancel because the protocol requires it; camera
moves cancel because a tool chose it. Canceling
is cheap, it happens at human rate, and the alternative is a revision field on
every record plus a plugin obliged to retain superseded geometry for the life of
a gesture.

**And each side cancels *before* applying the change, locally — it does not wait
for the other's `CANCEL`.** The state travels on gRPC and the cancellation on the
doorbell, and there is no ordering between those channels, so a receiver that
applied the new geometry and then kept processing the old gesture would interpret
a stroke captured under one spacing against another. That is not a peer defect to
tolerate; recalibration, crop and camera moves are ordinary operations. The rule
is symmetric and needs no new field:

> **Before applying an interpretation-changing `Geometry`, cancel every open
> interaction it affects. A `CANCEL` that arrives afterwards is an ordinary
> no-op** (§2's terminal rule already makes it one). The camera is not on this
> list: each ray carries its own (§3).

**Teardown ordering is not guaranteed and cannot be**, because deactivation
rides gRPC while its `CANCEL` rides the doorbell. Every tolerance rule therefore
lives on the teardown side:

- **A terminal for an unknown interaction is a no-op, not an error.** A gesture
  ended by deactivation produces a `CANCEL` here and a deactivation there, in
  either order. This rule makes both orders safe with no buffering and no
  cross-transport ordering contract.
- **More generally, any non-opening record — `DRAG`, `RELEASE`, `CANCEL` — for
  an unknown interaction is ignored**, with a diagnostic and no state change.
  `PRESS` and `FIRE` always open.
- **`channels` needs no arrival check at all, and bit15 is never "unknown".**
  Bits 0–14 are self-describing, and bit15's meaning — which channels at index
  ≥ 15 are visible — is epoch state that the epoch gate guarantees the plugin
  already holds before any record under that epoch can arrive (§1a). There is no
  version, nothing to be out of sync with, and no disposition for a set the
  plugin has not received. This is the simplification the epoch bought.
- **`view_id` is host-assigned and freely reusable** — 0 is a valid pane, so
there is no structural check; whether a value names a live pane is the
supervisor's, like `gen`. **Reassignment needs no quiet period**, because the
epoch counter for a `view_id` never restarts (§1a): a record from the previous
pane necessarily names a lower epoch and is discarded on its own merits.

**`gen` is never 0**, so a received 0 is structural and is `zero_id`. Whether a
  non-zero `gen` has been acknowledged is again the supervisor's; an opening
  record naming an unacknowledged one is a host bug of the same class as a
  never-seen tool, ruled on wherever the acknowledgement is tracked.

  So the three arrangement-scoped fields get **one** treatment rather than three:
  the transport checks what is structural, is *told* what it cannot source
  (`tool`), and labels the rest for whoever holds the state.
- **A record naming a `tool` handle the plugin no longer holds is ignored
  quietly.** A deactivation and an already-queued `PRESS` cross exactly as a
  deactivation and a `CANCEL` do, so this is the same race with the same answer.
  A handle the plugin has **never** seen is different — arrangement precedes use,
  so it cannot happen without a host bug — and is a `ContractViolation`.

  **The transport cannot source this distinction, but it should own the state
  machine for it.** Retired-versus-never-seen needs the activation list, which
  arrives over gRPC — yet the check sits inside the ordered algorithm, ahead of
  the index update, so deferring the ruling upstream would split decoding in two
  and make event ordering much harder. The resolution is lifecycle *inputs*
  rather than lifecycle *queries*: the supervisor calls something shaped like
  `tool_activated(handle)` and `tool_retired(handle)`, and the transport keeps
  what it is told. It then answers locally, in order, from state it was given
  rather than state it went looking for.

  **The question is three-way — active, retired, never seen — and a single set
  of live handles collapses retired into never seen**, so every ordinary
  deactivation race would emit a `ContractViolation` for a handle that was
  perfectly valid a moment ago. That is the same spurious report the ordering
  rule below exists to prevent, arriving from the other side.

  **Answer it with the active set and a high-water mark, not with a second
  set.** `tool` handles are monotonic (§5), so the largest seen is one `u32`:

  > `handle > high_water` → never seen.  Otherwise, in the active set → active;
  > not in it → retired.

  A retired *set* would grow all session at activation rate — the exhaustion §1
  widened `tool` to `u32` to avoid, reintroduced as receiver state.

  **A gap in the handles is accepted, and must be.** A handle can be allocated
  and never registered — an activation refused, lost, or rejected upstream — and
  a receiver that required the next consecutive value would then reject every
  later activation, leaving the active set empty and **all tool input dead for
  the session**. Accepting any handle above the mark fails the other way
  instead: a handle that was never issued classifies as *retired*, so a record
  naming it is ignored and counted rather than reported. That is the harmless
  direction, and it is why the three-way answer does not need consecutive
  allocation — only monotonic.

  **`tool_activated` must be called before the activation is acknowledged over
  gRPC**, and this is easy to get backwards. Ack first and a fast `PRESS` reaches
  the doorbell thread before the handle is registered, so the transport reports a
  `ContractViolation` for a perfectly valid handle — a spurious host-bug report
  caused entirely by plugin-side ordering. Same shape as
  `doorbell_protocol.md` §11a's "start the listener before signaling readiness".

  **Lifecycle calls and frame processing need one serialization**, whatever shape
  an SDK gives it. They mutate the same state, and without a defined order the
  same race gives different answers in different implementations — which is the
  one thing a published boundary cannot tolerate.

  **`tool_retired` purges that tool's entry from the interaction index**, and
  cancels its open gesture if it has one. Otherwise a retired tool's interaction
  stays in a session-scoped map forever and a later record naming it is matched
  against a tool that no longer exists.

`t`, `channels`, `level`, `mods` and the position are **per-record**: the user may
scrub, toggle a channel, cross a pyramid level or press Shift mid-drag.

**`click_count`** is the host's count for this press within its own double-click
interval: 1 for a single press, 2 for the second of a double, and so on. It is
**0 wherever the host determines none** — every `DRAG`, `RELEASE` and `CANCEL`,
and any `FIRE` not driven by a click. **There is no receiver-side rule and no
validation**: a plugin uses it or ignores it, and no value makes a record
invalid.

**It stays even if the binding registry gains double-click bindings.** The two
answer different questions — a binding decides *what fires*, `click_count`
reports *what the user did* — and a tool that fires on every press may still want
to know it was the second. The field is one byte in a fixed record; removing it
now to add it back later would cost a `ver` bump, which is a worse trade than an
unused byte.

**Every record fully identifies its context and does not carry the interaction's
history** — the anchor comes from the PRESS. Derived state is three variables,
**held per `tool`, not per session**. One tool is engaged at a time (§5), but
the tool a session's records name changes as tools are engaged and displaced —
every engagement is a new handle (`control_plane_protocol.md` §8.10) — and a
session-wide triple would let a displaced tool's late records land on the gesture
of the tool that replaced it:

- **open interaction** — the id from the last accepted **`PRESS`** for that tool,
  cleared by a terminal for that id. **`PRESS` is the only record that sets it**:
  a `FIRE` opens and closes within one record and leaves gesture state untouched
  (§2), and reading this as "the last non-terminal record" would let a `FIRE` set
  it and never clear it;
- **anchor** — the position from that interaction's PRESS;
- **current position** — from the latest record.

**Plus one session-wide index: open `interaction` → the `tool`, `gen` and
`view_id` it was opened with.** Per-tool state alone cannot detect a record whose
`tool` is wrong, because the lookup would be *by* `tool` and the disagreement
would be invisible — a `DRAG` naming a live interaction under the wrong tool
would classify as "unknown interaction, ignore" instead of the violation it is.
**An accepted `PRESS` is the only thing that puts an entry in the index**, and
`FIRE` never does — which is what keeps the index bounded, since a `FIRE` has no
terminal to remove it and would otherwise accumulate at click rate for the
session's life. **An entry leaves the index when its interaction ends**: on an accepted
`RELEASE` or `CANCEL`, on the implicit close a later `PRESS` for that tool
performs, on a cancellation from any of the rules above, and on `tool_retired`.
That is what makes the index bounded by the number of tools that can have a
gesture open at once, rather than by the number of gestures a session contains.

It is observable, not merely a leak: a late `CANCEL` arriving after a `RELEASE`
for the same interaction is "unknown interaction, ignore" precisely because the
entry is gone — which is §2's terminal-no-op rule, and would read the other way
if entries persisted.

### What to do with a bad record

One table, because the alternative is inferring it from six paragraphs. Every
row is a *well-formed* record — framing faults never reach this document
(`doorbell_protocol.md` §8).

| condition | action |
|---|---|
| `interaction == 0`, `tool == 0`, or `gen == 0` | `ContractViolation`, drop, cancel per the rule below |
| `seq != 0` on a `PRESS` | `ContractViolation`, drop, and **close the tool's prior gesture** — the implicit close happens whether or not the record is accepted; the rejected gesture is not opened |
| `seq != 0` on a `FIRE` | `ContractViolation`, drop, cancel nothing — `FIRE` neither opens nor closes a gesture |
| non-zero tail past the position kind's length | `ContractViolation`, drop, cancel per the rule below |
| a non-finite coordinate | `ContractViolation`, drop, cancel per the rule below |
| a known `target_kind` breaking §4's invariant | `ContractViolation`, drop, cancel per the rule below |
| `MISS` set with any known kind carrying `PICKED` — `PICKED` or `RAY_AND_PICKED` | `ContractViolation`, drop, cancel per the rule below |
| a non-opening record whose `gen`, `view_id` or `tool` disagrees with the interaction it names | `ContractViolation`, drop, cancel the open interaction |
| a `tool` handle never seen | `ContractViolation`, drop, cancel per the rule below |
| a `tool` handle already retired | ignore, local count |
| unknown `phase` | ignore, local count |
| a non-opening record naming an unknown interaction | ignore, local count |
| an opening record at or below the watermark, **byte-identical** to the record that opened a still-open interaction, or naming one already closed | ignore, local count (a retry that arrived twice) |
| an opening record at or below the watermark that **differs** from the record that opened the still-open interaction it names | `ContractViolation` `interaction_mismatch`, drop, and **close the tool's prior gesture** — it is not a duplicate (§2, step 2) |
| a `seq` at or below the interaction's last accepted | ignore, local count. For a terminal this is a duplicate: the interaction was already closed by the first one |
| a `seq` **gap** for a known interaction | process; local count — the samples that are gone are the missing ones, not this one |
| unknown `target_kind` | process; ignore the three target fields, local count |
| unknown `payload_kind` | process with no position, local count |
| a reserved bit set in `mods` or `position_flags` | process; ignore the bit, local count |
| an `epoch` **older** than the pane's current one | discard, local count. The pane's gestures were closed when the epoch was installed (§1a); a stale record closes nothing |
| an `epoch` **newer** than the pane's current one | `ContractViolation` `epoch_mismatch`, drop, cancel per the rule below |
| `epoch == 0` | `ContractViolation` `zero_id`, drop, cancel per the rule below; settled at step 3 with the other zero checks |

**The order matters, because a record can break several rows at once and only
some of them cancel a gesture.** Check in this order, and stop at the first hit:

1. **`phase`** — unknown, ignore the record. It goes first because every check
   below depends on whether this is an *opening* record, and that is a function
   of `phase`; testing "`seq != 0` on an opening record" before knowing the phase
   is a circular order that happens to give the same answer.
2. **Opening records only** — the open watermark: `0 < interaction ≤ watermark`
   means the id **has been seen**. That is not yet a duplicate, and what settles
   it is whether the interaction it names is still open:

   - **still open** → compare the record's **112 bytes** with the record that
     opened it. **Identical** is the retry §6 describes: ignore it, count it,
     change no state. **Anything else is not a duplicate at all** — it is a
     distinct opening record reusing a live id — and it is
     `interaction_mismatch`, whose rejection performs the implicit close
     (below).
   - **already closed** → nothing is retained to compare against and nothing to
     merge with, so the drop stands: ignore it, count it, change no state.

   It runs before the semantic checks because a record can be both a duplicate
   *and* malformed, and a malformed opening record closes the tool's prior
   gesture (below) — which a **duplicate** must never do. **The byte comparison
   is what makes that exemption exact**: an exact replay changes nothing, while a
   record that merely reuses an id is not a replay and is not exempt. The `0 <`
   guard is what lets it precede the zero checks. **The watermark itself
   advances only on full acceptance** (§6)
3. `interaction == 0`, `tool == 0`, `gen == 0`, or `epoch == 0` → `zero_id`.
   **These are the structural zero checks**, the ones a transport can make with
   no arrangement state at all, and they go together and go early: placing any
   of them after step 10 would leave a live index entry behind for a record
   that identifies nothing.
4. `seq != 0` on an opening record → `seq_on_open`
5. the position field — a non-zero tail → `position_tail`; a non-finite value
   anywhere in the kind's layout, `camera` and `entry_frac` and `roll` included,
   → `nonfinite_position`; `entry_frac` outside `[0, 1]`, or `camera == far` on
   a ray kind (which destroys the direction, §3), likewise `nonfinite_position`;
   so is a `back_frac` that is neither NaN nor in `[0, 1]` — NaN is the one
   non-finite value with a meaning, a pick where nothing stood out (§3).
   **Skipped entirely when `payload_kind` is unknown**, since the union's extent
   is then unknown
6. **`MISS` with any known kind carrying `PICKED`** — `PICKED` or
   `RAY_AND_PICKED` (§3) → `miss_with_picked`. Skipped with
   step 5 when `payload_kind` is unknown, for the same reason: the kind is what
   the contradiction is *with*.
7. **the target invariant** (§4) → `invalid_target`. Evaluated **only when
   `target_kind` is known**: an unknown kind already means all three fields are
   ignored, so there is nothing left to be consistent about.
8. **the pane epoch**, against the value `epoch_acknowledged` last supplied for
   this `view_id`: older → discard and count; newer → `epoch_mismatch`. **A pane
   with no recorded epoch counts as newer**, since a record cannot legally
   precede its pane's first acknowledgement (§1a) — under correct ordering it
   cannot happen, so it means a host that emitted early or a plugin that
   acknowledged before telling its transport. **It goes before the index update**, because the
   alternative is ruling on it upstream — and a stale *terminal* would then be a
   supervisor rejection of a record whose index entry is already gone, which has
   no repair (`doorbell_implementation_details.md` §6)
9. tool status — **never seen** → `unknown_tool`, a `ContractViolation`;
   **retired** → ignore and count locally, which is an ordinary deactivation
   race rather than a defect (the table above splits these; this step must too)
10. **Non-opening records only** — the interaction index: absent, ignore;
   present but naming a different `gen`, `view_id` or `tool` →
   `interaction_mismatch`. Openings settled their duplicate question at step 2
11. **Non-opening records only** — `seq` against the interaction's last accepted
   value (§6). A repeat is dropped and counted; a **gap** is counted and the
   record is **processed**, since a gap means samples were lost and this one is
   not among them. Neither cancels.

   **An opening record does not reach this step; it *establishes* the value at
   0.** An opening record carries `seq == 0`, and §6's rule is "not greater than
   the last accepted is dropped", so applying step 11 to a `PRESS` with a
   last-accepted defaulting to 0 drops it as a duplicate — and then every `DRAG`
   is "unknown interaction, ignore", and **all tool input is silently discarded
   with an aggregated counter as the only symptom.** The obvious spelling of the
   lookup is the one that does this, so the branch is stated rather than
   implied.

The **watermark** moves on any opening record the receiver **accepts**, `FIRE`
included (§6).

Which interaction a rejection cancels is the one rule above, not a per-row
decision.

**A violation carries the record's raw 112 bytes.** `ContractViolation` reports
them, and `shared_memory_protocol.md` §8's duplicate-grant test compares them, so
a transport that decodes and discards leaves two rules unimplementable. The bytes
travel with the disposition, whatever shape an implementation gives that.

**A cancellation is reportable, and there are four kinds** — the one a rejection
of a *non-opening* record triggers by the rule above, the one `tool_retired`
causes, **the implicit close performed by a rejected opening record**, and **the
pane-wide close performed when a new epoch is installed** (§1a).
All four are observable behavior a consumer must be able to
see; none is a wire record, and none may be silent.

**The third is the one that is easy to miss.** An *accepted* `PRESS` announces
its implicit close on the opening event it hands up (§6), so no cancellation
outcome is needed. A **rejected** `PRESS` still performs that close — it must,
or the prior gesture is open on one side and closed on the other — but there is
no opening event to carry the id. So it emits a cancellation outcome, which is
exactly what §6's definition asks for: an interaction that ended with neither its
own terminal nor a successor announcing it.

`ContractViolation.where` takes its values from the first block: `zero_id`,
`seq_on_open`, `position_tail`, `nonfinite_position`, `miss_with_picked`,
`invalid_target`, `interaction_mismatch`, `unknown_tool`, `epoch_mismatch`,
`unusable_context`. **The last is the terminal case above** — a `t`, `level` or
coordinate that cannot be reconciled with arranged state — and it is the one
neither party can raise alone: the transport cannot check it, and the supervisor
may not reject a terminal over it. **The supervisor raises it, against the record
the transport surfaced**, which is why that terminal is surfaced rather than
dropped. `position_tail` names only the padding
case — a NaN is not a tail, and one code for both would put two unrelated sender
bugs under a name that describes one of them. The
memory layer's sites extend the same enum
(`shared_memory_implementation_details.md` §2).

**"Cancel the open interaction" is a no-op where there is none.** A `FIRE` opens
and closes in one record and never touches gesture state, so a malformed `FIRE`
is dropped and nothing is canceled; the same is true of any record arriving when
that tool has no gesture open.

**Canceling an open interaction is a local event, never a synthesized record.**
§6's ban is on putting a fabricated `CANCEL` on the wire, where it would be
indistinguishable from a real one; how an SDK surfaces the abort to a tool
handler — a distinct callback carrying the tool, the interaction and the reason
is the obvious shape — is its own business.

---

## 3. Position

**A position is reported only when the host has one.** `payload_kind = NONE` means
no coordinate was determined — never origin, never a guess, never a clamp to the
nearest in-bounds voxel. A coordinate the host *does* have but which falls outside
the clip box is reported with the `MISS` flag, not suppressed — the flag says
the coordinate is not *relevant*, never that the plugin may not act on it
(`control_plane_protocol.md` §8.11).

All layouts are f32, in voxel index space of the record's `level`, at the
`position` field's origin — payload byte 44 (§1). Every byte of the field past the
kind's length MUST be zero.

| `payload_kind` | layout | bytes |
|---|---|---|
| `NONE` | — | 0 |
| `VOXEL` | `z, y, x` | 12 |
| `PICKED` | `z, y, x` | 12 |
| `COLUMN_YX` | `y, x` · `z_min, z_max` | 16 |
| `COLUMN_ZX` | `z, x` · `y_min, y_max` | 16 |
| `COLUMN_ZY` | `z, y` · `x_min, x_max` | 16 |
| `RAY` | `camera z,y,x` · `far z,y,x` · `entry_frac` · `roll` | 32 |
| `RAY_AND_PICKED` | `RAY` then `PICKED` then `back_frac` | 48 |

### The ray layout

**A ray record carries the whole camera, so a gesture is self-contained** — §2's
rule for every other field here, applied to the camera. Nothing is arranged, so
nothing can outrun the description of the camera a record was captured under.

| | |
|---|---|
| `camera` | the eye, in voxel index space of `level` |
| `far` | **exit** of the clip box on a hit; on a `MISS`, `camera + unit_direction × D` with `D` beyond the data. **Never equal to `camera`** — see below |
| `entry_frac` | in `[0, 1]`: locates the **entry** point on a hit, and the **closest approach** to the volume on a `MISS`. Either is `camera + entry_frac × (far − camera)` |
| `roll` | rotation about the ray, radians |

**Entry as a fraction rather than a point** costs 8 bytes less than a second
triple and cannot express a point off the ray. A tool that wants the segment
computes it; a tool that wants the direction takes `far − camera`.

**`far` is never the closest point itself, and that is the reason it is
synthetic on a `MISS`.** For a volume behind the camera, the closest approach on
the forward half-line *is* the camera — so a `far` set to it would give
`far − camera == 0`, destroying the ray direction and leaving `roll`
uninterpretable, on a record the specification calls exact. Putting `far` beyond
the data and locating the closest approach with `entry_frac` keeps the direction
in every case, including `entry_frac == 0`.

**`position_flags` bit 1, `ROLL_REF_Y`, says which reference axis the sender
used** — clear for world **+Z**, set for world **+Y**. It is carried rather than
recomputed because the choice is ill-conditioned near the singularity: with a
near-Z ray the projection of +Z is dominated by rounding, so an encoder deciding
from full-precision camera state and a decoder deciding from transmitted f32
can pick opposite directions and reconstruct camera-up **180° apart** — on an
ordinary front-facing view. One bit removes the question.

**`roll`'s zero is referenced to the world, never to the ray**, or two records
from one camera pose would disagree: it is the signed angle from the projection
of **the selected reference axis** — world +Z when `ROLL_REF_Y` is clear, world
+Y when it is set — onto the plane perpendicular to the ray, to the camera's up
vector, positive by the right-hand rule about `far − camera`. The sender picks +Y whenever +Z is too near the ray to project stably — in
*either* direction, since the projection is as singular for −Z as for +Z and a
front-facing camera is the common case — and says so in `ROLL_REF_Y`. **Angles are in physical space**, after applying `level` scale
and voxel spacing: anisotropic spacing makes voxel-space and physical angles
differ, and a camera's roll is a physical fact. Only rotation is unrecoverable
from the ray, which is why it is the one scalar carried.

**"Physical space" is the space the plugin can reconstruct: `Geometry.spacing`
(`control_plane_protocol.md` §8.11) applied to the record's `level` scale — and
nothing else.** A pane's *display* frame may also carry a user's Z-stretch
multiplier and axis flips, none of which is on the wire; a `roll` measured
against that frame is uninterpretable off the isotropic, unflipped path, and
when the file carries no spacing at all (`SPACING_PROVENANCE_DISPLAY_FALLBACK`)
the two spaces coincide and the question does not arise. The sender's
obligation is that the plugin, holding the ray and the published spacing,
recovers the same camera-up the sender had.

| situation | kind |
|---|---|
| 2D slice, or a montage tile | `VOXEL` |
| 2D projection | `COLUMN_YX` |
| 3D, PRESS or FIRE, ray meets the clip box | `RAY_AND_PICKED`, or `RAY` if the tool resolves its own anchor — or if the host's pick found nothing along the ray (the crop hid everything it crossed, or nothing is lit), or found only a point and the host does not send points. The `RAY` cases are indistinguishable by design: in each the plugin resolves its own anchor |
| 3D, DRAG and RELEASE, ray meets the clip box | `RAY` |
| 3D, the eye is inside the clip box | as above, with `entry_frac = 0`: the ray has no entry face, the entry *is* the eye, and `far` is the exit as usual |
| 2D, point off the image | `VOXEL` or `COLUMN_*` with `MISS` |
| 3D, ray misses the clip box | `RAY` with `MISS` |
| gutter, frame, empty cell, no volume in the pane at all | `NONE` |

**The `NONE` rule exists** because a `z, y, x` triple that is always present is,
in two of three cases, fiction a plugin cannot detect: in 3D it would be the
ray∩crop-box **entry point** reported as the voxel, and under a projection the
collapsed axis reported as whichever plane the slider sits at.

**Montage is not a distinct case.** The tile resolves `z` before the event exists,
so the coordinate is a true voxel exactly as a slice click is. `E` carries what a
coordinate *means*, not how the pane was drawn; display arrangement is human-rate
state belonging to a scene feed keyed on `view_id`.

**`PICKED` is the host's chosen voxel along the ray, and the choosing policy is
not on the wire.** It is one voxel the ray passed through, selected by whatever
the host does — a brightness search, or something else later — and that policy
is deliberately not a protocol field: it is display behavior, it will change,
and a plugin that needs its own answer takes `RAY` and resolves the anchor
itself (§3, 3D scribbling). What `PICKED` guarantees is only that the voxel is
on the ray and inside the clip box — **a real pick, never a geometric stand-in
such as the clip box's entry point**, which is a coordinate nobody asked for
and which §8's rejected-alternatives table names. A display policy will in
practice also pick a *lit* voxel, and that is deliberately **not** promised: the
policy is not on the wire precisely so it can change, and a guarantee about
lighting would pin it.

**`back_frac` is how far the picked structure runs behind its front**, as a
fraction of the ray that is left there. Let `f` be the picked front on the ray:
the center of the `PICKED` voxel projected onto `camera → far` and clamped to
that segment. The back point is

    back = f + back_frac × (far − f)

so `0.0` is a structure of **no depth** — a real pick, its back at its front —
and `1.0` one that runs to the far end. It is the one number a pick has beyond
its voxel: a seed tool wants the front, a measuring or segmenting tool wants the
extent, and the host has both from the same sweep. A finite `back_frac` lies in
`[0, 1]`.

**NaN says nothing stood out.** Nothing along the ray rose out of the noise, so
the host took a likely voxel with no structure around it; there is no structure
to have a back, and a receiver must not read a zero depth into it. `0.0` and
NaN are different answers and a receiver tells them apart.

**Measured from the front, not from the camera**, so that a sender that writes
zeros there is read as a zero-depth pick.

**A tool says whether it wants the pick, and takes either record regardless.**
A tool's manifest carries `wants_pick` (default true, `manifest_schema.md`
§3.6). A host does not compute a pick for a tool that declared false, and sends
it `RAY`. That is a request about what the host *computes* — the pick is a
trace per press, wasted on a tool that resolves its own anchor — and it is not
a filter on the wire, in either direction: a tool that wants the pick still
receives a bare `RAY` whenever the host has none to send (nothing stood out and
points are not being sent; a host with no pick at all), and a tool that
declined it still receives `RAY_AND_PICKED` from a host that always picks. **A receiver handles both kinds on a 3D pane whichever it
declared**; a host does not validate a record against the declaration, and a
supervisor must not either.

**The reference host's pick** sweeps a small disk (≈1 µm) along the
record's own ray, through every visible channel as drawn — the display's
window, gamma and crop — and takes the first steep rise out of the trace's noise,
weighted gently toward the nearer of comparable rises; `PICKED` is the first voxel
on the bright side of that rise and `back_frac` from where the structure falls away. A
trace with nothing out of the noise gives a point, which the reference host sends
as a bare `RAY` unless the person has asked for points to be sent. All of that is
policy, adjustable by the person, and none of it is promised.

**Every coordinate is in voxel index space of the record's `level`.** One space,
named per record. `ViewState.level` cannot serve — it is the 3D volume's, polled,
and not the 2D plane's, and a coordinate without its grid is not a coordinate.
Coordinates are **not** normalized to level 0: rescaling would claim precision the
host does not have, since a level-3 pixel covers eight level-0 voxels.

**Ranges are the CLIP bounds, not the lit set.** A column's `z_min, z_max` is the
intersection of the projection window, the **global crop** and the dataset
extent. A `RAY` is a **segment** — entry and exit of that same box — so there is
no direction vector to be non-unit-length in anisotropic space, and no `t`
parameterisation whose space needs naming. A ray that meets no volume is still
a `RAY`, flagged `MISS` (below), rather than absent.

**All three terms are CONVEX, so the interval has no gap** — and that, not
axis-alignment, is why this is a definition rather than a lossy narrowing. A ray
meets the intersection of convex sets in one segment; a projection column meets
it in one interval. (Axis-alignment is incidental: a *rotated* region box is
convex too and would also give one segment. What cannot be reported as an
interval is their **union**, which is what a set of region crops is.)

**And it is exact in representation too**, because the crop travels as **edge
coordinates in floats** (`control_plane_protocol.md` §8.11): a boundary sits at
a pixel edge, so pixels 25 through 1519 are `{24.5, 1519.5}` rather than a
rounded `[25, 1520)`. Two consequences land here:

- **Convert the crop into the record's `level` FIRST, then to pixel centers.**
  A level-0 edge `e` is exactly **`(e + 0.5) / scale − 0.5`** at level N — an
  *affine* map, because the pyramid block-downsamples anchored at index 0, so
  level-N pixel `j` is centered at level-0 `j·s + (s − 1)/2` rather than at `j·s`.
  A level-0 *index* has no exact level-N index at all, which is why this section
  refuses to normalize coordinates between levels. Centers then come from
  `ceil(lo) ..= floor(hi)`, inclusive at both ends. Doing it the other way
  rounds twice.
- **A column's `z_min, z_max` is therefore an integer pair with half a pixel of
  slack behind it**, not a value sitting on a rounding tie. That slack is what
  makes an independently-written implementation agree with vizi's rather than
  differ by a plane.

**Named-region crops and per-voxel mask crops are deliberately NOT terms here.**
They are *lighting*, not *clipping* — `control_plane_protocol.md` §8.11 draws
the distinction and this is its consequence on the record. A region crop leaving
`[2,4)` and `[8,10)` lit along one column still reports `[2,10)`, because the
voxels between are **there**: they are addressable, and a tool that reads them
gets data rather than an error. Reporting the comb would describe what the user
can *see*, which is display state, changes at human rate, and belongs to a scene
feed keyed on `view_id` — not to a coordinate on a 1 kHz record whose consumer
is deciding which voxels to operate on.

### Pointing outside the data

**A lasso is drawn *around* what it selects**, so the samples a selection most
needs are exactly the ones that land outside. Reporting them as `NONE` makes a
lasso inexpressible — in 2D and 3D alike — and silently: the plugin sees a run of
absent coordinates and cannot tell them from a gutter click.

So the host reports the coordinate it has, and sets **`MISS`**:

- **2D**: the true off-image coordinate. It is what the pixel maps to under the
  same transform every in-image click uses; it simply falls outside the extent.
  A column keeps its `z_min, z_max`, which describes the projection window and is
  still true.
- **3D**: `RAY` whose `entry_frac` locates **the point on the ray closest to the
  clip box**. The segment is degenerate — entry and the closest approach
  coincide — but the ray itself is not: `far` still lies beyond the data, so the
  direction survives (above).

`NONE` therefore narrows to *the host has no coordinate at all*: a gutter, a
frame, an empty montage cell, or no volume in the pane at all.

**Two rules hold for every `MISS`, in any view:**

- **The coordinate may lie outside the dataset extent**, which a non-`MISS`
  coordinate never does. A plugin must not clamp it, reject it or treat it as
  corrupt.
- **It is not a RELEVANT place, which is not the same as not existing.** A
  `MISS` coordinate may be outside the dataset entirely, or merely outside the
  crop — and in the second case the voxels are there and a plugin that reads
  them gets data (`control_plane_protocol.md` §8.11). What the flag says is that
  the host does not count it as part of what the user is working in. A tool that
  paints or samples the current volume skips a `MISS` exactly as it skips
  `NONE`; a tool building a selection uses it; a tool deliberately working
  outside the crop is entitled to, and that is its call rather than the host's.
  This is the entire reason the distinction is on the wire.

The 3D closest-approach point takes three conventions, all guessable wrong and
none consequential in themselves:

- **The ray is the forward half-line from the eye**, not the full line, so a box
  beside or behind the camera cannot return a point behind the viewer.
- **Distance is measured in physical space.** Tangency is affine-invariant so the
  continuity below holds either way, but for a non-tangent near-miss the two
  spaces put the point in different places, and physical is what "closest" means
  to someone looking at the screen.
- **The box is the same crop box a hit is tested against**, so hit and miss are
  answers about one solid.
- **Where the closest point is not unique** — a ray parallel to a face, or
  degenerate — **any minimizer is conforming.** The continuity argument below
  needs a point at the minimum distance, not a canonical one, and pinning a
  tie-break would be a rule with no consumer. It is the axis-aligned box `Geometry` reports; vizi
  has no dataset world transform.

**Closest approach rather than a fixed depth, because it does not jump.** As the
cursor crosses the silhouette, entry and exit converge on the tangency point and
bifurcate from it, so a tool sees one continuous curve that widens into a
segment. A plane at the box's nearest depth would teleport the point the length
of the box for a ray grazing the *far* edge. And because the point stays on the
ray rather than on the box, a wide lasso and a tight one give different points —
nothing collapses onto the silhouette.

Its depth is fabricated — a property of the box, not of anything the user pointed
at — which is why `MISS` is declared rather than inferred: detecting it by testing
two f32 triples for equality is a sniff (`doorbell_principles.md` B4), and a ray
grazing a box corner produces the same bytes for a genuine hit.

**A 3D `MISS` never carries `PICKED`** — a ray meeting no volume picks nothing —
so the kind is `RAY`, never `RAY_AND_PICKED`. That is a sender rule with a
receiver-checkable contradiction, so it is **checked**, exactly like §4's target
invariant: **`MISS` with any kind that includes `PICKED` — `PICKED` itself or
`RAY_AND_PICKED` — is `ContractViolation`, `where = miss_with_picked`** (§2).
`PICKED` promises a voxel inside the clip box and `MISS` says the
coordinate is not inside it, so the two cannot both hold whichever kind carries
them. A rule a receiver can test and does not is a rule two
implementations will disagree about.

### 3D scribbling

**A scribble over a 3D scene is a family of rays, one per sample, and that is
exact.** Each ray was captured at the moment it was drawn, so it carries no
dependence on where the camera later goes. What the gesture does *not* contain is
depth.

| what the tool wants | recoverable from the rays alone |
|---|---|
| a cone or lasso selection | **yes, exactly** — the rays are its generators |
| the screen curve, for feedback | **yes** — see below |
| a 3D path (paint, trace) | **no** — depth comes from a rule the tool chooses |

**Turning a segment into a point is tool policy, not protocol.** The plugin has
the dataset geometry, arranged over gRPC, and may work in whatever frame it
likes. The common choice is a plane through the `PRESS` anchor, fronto-parallel
in *physical* space; a plugin constructing that plane in voxel space uses normal
`dᵢ·sᵢ²`, not `dᵢ`, where `s` is the voxel spacing — `dᵢ` is perpendicular only
when voxels are isotropic. A tool wanting its own picking policy takes `RAY` at
PRESS and resolves the anchor over gRPC, buffering segments in `seq` order and
resolving retroactively. Nothing is lost; the preview appears a few milliseconds
late.

**That is a plugin-initiated round trip on a per-gesture path, and
`doorbell_principles.md` A2 permits it on its own terms** — not as arrangement,
which is paid once and reused, where this is paid per `PRESS`. Three things make
it acceptable: the plugin chooses to pay it, no host action waits on it, and the
result is applied retroactively instead of blocking the stream. A tool unwilling
to pay it takes `RAY_AND_PICKED` and never touches the control plane.

**Screen coordinates are not carried, and are not needed for feedback.** Take
any fixed parameter along each ray — the entry point is the obvious one — and the
resulting world-space point projects *exactly* onto the pixel that generated the
ray, because the ray was built by unprojecting that pixel. A `LineSet` through
those points draws the curve the user drew, with no viewport geometry on the wire
and no renderer projection frozen into the protocol. It also degrades the right
way: if the camera moves, that polyline stays where it was drawn in the world,
whereas screen coordinates plus one camera pose would be actively wrong.

**The apex is on the record.** `camera` is in every ray layout, so a selection
cone needs no reconstruction: two rays *would* meet at the eye approximately, and
a loop clearing the volume entirely would recover nothing at all.

---

## 4. Targets

**The host reports intersections with host-owned objects only** — annotations,
ROIs, handles. A plugin intersects its own geometry itself: it knows where it put
its markers and lines, and it has the segment. A click on a plugin's label volume
needs no field at all, since the plugin looks the label up from the coordinate.

That bounds the namespace to UI-scale objects rather than data-scale ones, so a
segmentation label is never a `target_id`.

**Nearest only, and honest absence.** A 3D ray crosses many objects; the host
reports the nearest, and a tool wanting all of them has the segment and its own
geometry. Nothing intersected means `target_kind = none` and `target_id = 0`.

`target_part` names a sub-element whose meaning depends on `target_kind` — which
handle of an ROI, which vertex of a mesh, which marker of a set.

**`target_part` reserves 0 for "no particular sub-element", so parts are numbered
from 1.** Otherwise a click on an ROI's body rather than one of its handles is
indistinguishable from a click on handle zero — principle 11 on a field this
document otherwise handles carefully.

**The three fields have one invariant, and it is checkable without any host
state**: `target_kind == NONE` if and only if `target_id == 0`, and when
`target_kind` is `NONE` then `target_part` is 0 as well. Anything else is a
`ContractViolation` — a named target with no id, or a part of nothing, is a
record that cannot be acted on and should not be guessed at.

---

## 5. Triggers and the primary pointer

A tool is **engaged** — from the Tools menu, the control pane, or a trigger it
requested in its manifest (a key that toggles or holds it, a mouse chord that
holds it) — and once engaged it owns the **primary pointer** in its views: the
bare left button and every left chord the host does not keep. **The primary
button means whatever the engaged tool means, so no tool ever names it**
(`control_plane_protocol.md` §8.10, the modal rule). `tool` is the handle
assigned at activation over gRPC, never reused within a session.

Two consequences:

- **One tool is engaged at a time, host-wide.** A momentary trigger engages its
  tool for as long as it is held and hands the pointer back after; the record
  stream a momentary chord produces is the tool's own PRESS / DRAG / RELEASE,
  with the chord's modifiers in `mods`.
- **The record carries no `buttons` field.** There is exactly one pointer
  stream — the primary, or the chord that engaged the tool — so an event is
  *use* and the arrangement says what engaged it. A key or a pane button that
  fires *into* the engaged tool is an `action` (§1), not a second button.

`mods` stays per-record because holding Shift **mid-drag** to constrain an axis is
a state change during use, not part of the arrangement.

**The tool declares its mode**: `fire` emits FIRE only; `gesture` emits
PRESS/DRAG/RELEASE/CANCEL. Fire-on-press is the sensible default for anything that
does not need a path — tap-to-click produces a very short press-release and
press-hold-drag is awkward on a touchpad. An action FIRE (§1) is delivered
under either mode.

**The tool also declares whether a camera move cancels its gesture.** In 3D
that is the one failure a representation cannot absorb: the rays stay exact, but
the user's intent is itself ill-defined once the view slides under the stroke.
`cancel_on_camera_move` is the default for anything screen-defined — a lasso that
survives an orbit is not a lasso — and it is what makes one arranged camera per
`view_id` sufficient for the whole gesture (§3).

**A tool that turns it off forfeits nothing**, because each ray record carries
the camera it was cast from (§3): a stroke that spans an orbit describes itself
correctly throughout, which one arranged camera per pane could not do.

**A camera move is not an epoch change and needs no transport machinery.** It
invalidates nothing — every record remains exactly interpretable — so there is
nothing to cancel for interpretation's sake. A binding with
`cancel_on_camera_move` set is canceled by the host as an ordinary host-side
abort, which reaches the plugin as an ordinary `CANCEL` record like Esc or a tool
switch (§2).

`tool` handles **start at 1, increase, and are never reused** — **per plugin
session, from a counter that plugin alone consumes**, so **0 never appears** and
means "no activation" wherever a handle is optional. The scope is load-bearing:
a host-global counter satisfies "never reused" while handing one plugin 5, 9, 14,
and §2's three-way answer is keyed on the largest handle *that plugin* has seen.

**Monotonic, not consecutive.** Monotonicity is what lets a receiver answer
"never seen" from a high-water mark instead of a retired set that grows all
session (§2); gaps are ordinary, since a handle can be allocated and never
registered. This is where `tool` differs from `id`, `seq` and `interaction`,
whose consecutiveness *is* load-bearing, because for those a gap is the loss
signal.

`tool` is a handle rather than the manifest string because an event identifies an
*activation*, not a name: a plugin already knows the names of its own tools, and
activation is a message addressed to it.

**A keyboard action with no pane under the cursor is a command, not a tool event.**
FIRE is for spatially-situated actions; without spatial context the plugin wanted a
command handler.

---

## 6. Loss

The transport does not lose `E` silently: a send is accepted or refused with the
sender knowing, and receiver-side backpressure propagates to the host. All loss is
a host decision.

| phase | policy |
|---|---|
| DRAG | dropped on refusal |
| PRESS, RELEASE, CANCEL, FIRE | retried on refusal, bounded, paced on the next writability edge |

Refusal is unambiguous on all three platforms, so a retry cannot duplicate
(`doorbell_protocol.md` §6). Dedup is therefore **defence, not repair**: it exists
because "the transport never duplicates" is a property to defend rather than one
to rely on silently (`doorbell_principles.md` D1a). It matters most for FIRE,
which unlike a terminal is an **action** rather than an idempotent state
statement — delivered twice it acts twice.

**One session-wide counter: the open watermark** — the highest `interaction`
**successfully opened**, starting at 0 so `interaction` 1 is accepted. At or
below it, an opening record is a **candidate** duplicate: it is dropped if it is
a byte-exact replay of the record that opened a still-open interaction, or if
that interaction has already closed, and it is `interaction_mismatch` otherwise
(§2, step 2). "At or below the watermark" is the test for having *seen* the id,
never on its own the test for being a duplicate.

**There is no second, diagnostic counter.** An expected-interaction counter
beside it would earn nothing a receiver acts on:
`doorbell_protocol.md` §4's header `id` already gives per-direction loss and is
§6's standing evidence for the ordering property, `seq` gives per-stroke loss
more precisely, and a lost opening record is separately visible as a run of
unknown-interaction `DRAG`s.

**It moves on any opening record the receiver accepts, `FIRE` included.** Not "on any record that opened a gesture", which
excludes `FIRE`, since §2 says `FIRE` never opens one: a duplicate `FIRE` would
then sit permanently above the watermark and be acted on twice, which is the
single harm §6 says this dedup exists to prevent.

**And that is why there is no second counter beside this one.** No single
counter can serve both readings: the diagnostic reading wants a backward
`interaction` counted and opened normally, while the dedup reading drops it.
Keeping two would make that contradiction a permanent obligation on every
implementation; loss diagnosis stays with `id` and `seq`, where the rules are
already written and where no second reading competes.

**`interaction` is allocated when its opening record is admitted to the writer**
— the same point as that record's `id` and `seq` (`doorbell_protocol.md` §4) —
per session, starting at 1, incremented by exactly one, and never reused. The
allocation point is what gives a gap its meaning: allocating at gesture *start*
instead would let a gesture end before its `PRESS` was ever admitted, and then a
gap would be routine noise rather than the loss signal §6 relies on. **0 is
reserved for "no interaction" and never appears.** Both properties are load-bearing and neither is decoration:
consecutive allocation is what makes a gap *between* interactions visible below,
and monotonicity is what makes the dedup sound.

**The dedup applies to opening records only, and its watermark is session-wide.**
A `PRESS` or `FIRE` whose `interaction ≤` the **open watermark** has been seen
before, and §2's byte comparison decides whether it is a replay to drop or a
distinct record reusing a live id. **Not per tool** — `interaction` is allocated session-wide, so a
per-tool watermark lets a duplicate of an id opened under tool A arrive labeled
tool B, clear B's lower watermark, and then collide with A's live entry in the
interaction index below. One counter for the session is both simpler and the only
one that is exact. Non-opening records are matched to the open interaction and deduped by
`seq`: a `seq` already seen for that interaction is dropped. Stated unqualified
the rule would drop every `DRAG`, since a `DRAG` carries the interaction that is
already open.

One `u32` for the session, O(1), no table. That is the whole soundness argument and it
is worth stating rather than implying: `doorbell_protocol.md` §14 rejects a
recent-ids filter as unsound, and `plugin_principles.md` 4 warns that a bounded
table cannot make a wrapping id safe. Monotonicity is the escape, and it holds
because `interaction` advances at *gesture* rate — a u32 does not wrap in any
session a human sits through.

**The watermark and gap detection are both session-wide, and per-tool state is
only the active gesture.** Across a session's successive activations the
interaction ids interleave, so a single tool's sequence has holes with nothing
lost; only the session-wide sequence is dense, and only it can be compared
against.

**Events with no coordinate are still sent.** Dropping them is what manufactures
gestures with no PRESS (drag inward from outside the data) and gestures that never
terminate (release outside it), and it puts holes in a stroke with no `seq`
discontinuity to declare them.

`seq` is authoritative for loss and nothing else is: gaps come from `seq`
discontinuity, and **a record whose `seq` is not greater than the last accepted
`seq` for its interaction is dropped**. The rule is that comparison and not set
membership — one integer per open interaction, no table — and the two agree
because every transport delivers in order. **In-order delivery is a stated
property of every supported doorbell transport**, not an assumption:
`SOCK_SEQPACKET`, `AF_UNIX SOCK_DGRAM` and loopback TCP all deliver in order, and
it is measured on the two where POSIX declines to promise it — a saturated
socketpair on Linux `SOCK_SEQPACKET` and on macOS `SOCK_DGRAM`, zero inversions
on either.

**Take that measurement for what it is.** It runs the production configuration —
§6 rule 1's single serialized writer into one receive queue — which is the
configuration the dependency is on, but it is *consistent with* the property
rather than an attempt to falsify it: a single sender into a single AF_UNIX queue
is FIFO by construction, so no arm of it could have produced an inversion. It
also cannot see drops, only order. The measurement's value is that it covers the
deployed shape; it is not a stress test, and nothing here should be read as one.

**What actually monitors the property in production is the header `id`'s
regression counter** (`doorbell_protocol.md` §4). Ids are consecutive per
direction, so an inversion on the wire shows up as a regression against
expected-next — the one existing mechanism that would make a violation of this
assumption visible rather than silent, and the reason that counter is worth
having even though it is never a dedup input.

**And what breaks if a platform ever violates it**, since
`plugin_principles.md` 7 asks us not to assume order without saying so: a
reordered *opening* record is the damaging case — `FIRE` 12 arriving before
`FIRE` 11 advances the watermark past 11, which is then dropped as a duplicate,
losing an action rather than a disposable sample. Reordered non-opening records
are harmless, absorbed by `seq`. The remedy if it is ever observed is a bounded
set of recently-accepted opening ids rather than a watermark — sound here, unlike
the `id` filter `doorbell_protocol.md` §14 rejects, because `interaction` does not
wrap within a session. It is deliberately not derived
from what the sender believes about delivery — most loss here is the host
*choosing* to drop a `DRAG` on refusal, which is a sender decision the receiver
must still be able to see. `seq` is per-interaction, so a gap means "you missed 3
samples of *this* stroke"; a gap between interactions is visible in the
session-wide `interaction` sequence.

**A terminal is an idempotent state statement** — "interaction N ended, RELEASE" —
so a duplicate is harmless. Nothing here establishes that a terminal *arrived*, only
that it was transmitted; on retry exhaustion the host raises a gRPC fault and the
session escalates. By `doorbell_protocol.md` §9 that is the right amount: exhaustion
means the peer has not drained for the whole retry window, so the missing terminal is
not the worst thing happening in that session.

**A PRESS implies the previous interaction *of the same tool* is over**, so a
plugin never sits in a stale active state across interactions. It is scoped to
the tool: tool B pressing says nothing about tool A's gesture, which a
displaced tool's records arriving late would otherwise be read as. The implicit close puts **no synthesized `CANCEL` on
the wire** — a fabricated terminal is indistinguishable from a real one — but it
should not be invisible either. The shape that satisfies both: the opening event
an SDK hands its handler **names the interaction it implicitly closed**, so
nothing is fabricated and nothing is hidden.

**That naming is the whole of it: an implicit close is NOT a separate
cancellation outcome.** `doorbell_implementation_details.md` §6 gives
cancellations their own outcome row, and an implicit close is frame-caused, so
both readings are available and a consumer written against the wrong one strands
the gesture that was closed. One answer, stated here: the opening event carries
the id it closed, and no cancellation row accompanies it. A cancellation row
means *an interaction ended with neither its own terminal nor a successor
announcing it* — `tool_retired`, a rejection, an interpretation change — which is
precisely the case where nothing else would tell the consumer.

---

## 7. Rate

Input is **mouse rate, up to ~1 kHz** — above frame rate, because a stroke's path
matters and not just its latest point. A latest-value register is not sufficient for
a tool whose full stroke is meaningful.

`E` therefore sizes the transport's queues: it cannot coalesce, so mouse rate reaches
the wire directly. See `doorbell_protocol.md` §11.

---

## 8. Rejected alternatives

| Alternative | Why not |
|---|---|
| Always sending a `z, y, x` triple | Two of the three view cases have no true voxel and the plugin cannot tell |
| Reporting the 3D crop-box entry point as a voxel | A coordinate nobody asked for, indistinguishable from a real pick |
| The pick's back as a length or a second triple | A length along a ray in anisotropic voxel space has no single unit, and a triple can name a point off the ray; a fraction of `camera → far` is the ray's own parameter, as `entry_frac` is |
| A separate kind for a pick with no extent | A kind per outcome of a policy that is deliberately off the wire; NaN in the one field that does not apply says it |
| Dropping events with no coordinate | Manufactures gestures with no PRESS and gestures that never terminate |
| Clamping to the nearest in-bounds voxel | A lie where `NONE` is available |
| A `view_mode` field | `payload_kind` distinguishes everything a plugin can act on; a mode enum would grow with every display feature while carrying nothing usable |
| An axis byte on a column | Names the coordinates ambiguously; distinct kinds name the layout, and enum values are free where reserved fields are dead weight |
| A column spanning the whole volume | Reports what exists rather than what produced the pixel |
| `RAY` as origin + direction | Unbounded, and a direction in anisotropic voxel space is neither unit-length nor angle-preserving |
| Screen coordinates | Puts viewport geometry on the wire; a point on the ray back-projects to the pixel anyway |
| Reporting a point outside the data as `NONE` | Makes a lasso inexpressible in either view, and indistinguishable from a gutter click |
| A `*_MISS` kind per payload kind | The kind space doubles and every future kind needs a twin; one flag answers "did this land in data?" in every view |
| A wall-clock timestamp on the record | The frame a sample belongs to is `t`, which is what a plugin queries with; arrival time is what a live tool has, and a buffered one is resolving against `seq` order anyway |
| Clipping a missed ray to the view frustum | Puts coordinates out where f32 stops being free, and a near→far segment is a different kind of thing wearing the same layout |
| A missed ray at the box's nearest depth | Jumps by the length of the box as the cursor crosses a far edge; closest approach is continuous through the silhouette |
| Inferring a miss from `entry == exit` | An f32 equality test is a sniff, and a grazing hit produces the same bytes |
| Prescribing the depth rule for a 3D stroke | The plugin has the geometry and the tool owns the policy; the recipe was also wrong under anisotropy |
| f64 coordinates | 24 bytes for range no dataset has |
| Normalizing to level 0 | Claims precision the host does not have |
| Carrying the anchor in every record | PRESS is retried; a gesture without one means the session is already escalating |
| A `dropped` counter beside `seq` | `seq` gives dedup and gap detection from one field, and is correct where a sender's belief about delivery is not |
| Per-session `seq` | A gap between interactions is visible in the `interaction` id |
| `tool_id` as a manifest string | The plugin knows its own names; an event identifies an activation |
| A `buttons` field | The binding is arrangement |
| FIRE as an immediate PRESS+RELEASE | Two records for one action, and forces a plugin to infer a click |
| Routing the terminal over gRPC | Head-of-line blocked behind any long handler, and splitting phases across transports lets a DRAG arrive before its PRESS |
| An `E` acknowledgement | Would run only when the peer is not draining, where escalation is the answer |
| Segmentation labels as `target_id` | The plugin owns the volume and looks the label up from the coordinate |

---

## 9. Open

- **A resolvable namespace for host-owned targets.** Annotations, ROIs and handles
  need stable session-scoped ids a plugin can hold across events.
- **Whether double-click is a binding** rather than a per-record `click_count`.
  vizi's per-window `CommandRegistry` is the precedent. (Conflict resolution over
  the (button × modifier) space is settled in `control_plane_protocol.md` §8.10:
  a contested binding goes to nobody and the user is told which plugins
  collided.)
- **Whether the 2D pane's level tracks zoom.** If it does, `level` already carries
  click precision; if not, a click zoomed out is coarser than a voxel and nothing
  says so.
- **HOVER and LEAVE**, deferred with their phase values reserved. Hover is currently
  suppressed for an active tool and never forwarded, so pre-click feedback is
  impossible.
- **`COLUMN_ZX` and `COLUMN_ZY`** are unproducible today — the only kernel is
  `mip_project_z` and `slice.rs`'s `Orientation` is display rotation, not an ortho
  plane.
- **Gesture semantics above this layer**: what a tool commits on RELEASE, what
  CANCEL discards, stale-drag filtering.

**Output-side note, not this document's to fix.** A stroke's natural representation
is a `LineSet`, and 2D does not rasterize one — `render_2d` handles voxel-frame
`Marker`s and composited `LabelVolume` slices only. So a 2D scribble reaches the
screen as a dense marker trail or a painted label volume.
