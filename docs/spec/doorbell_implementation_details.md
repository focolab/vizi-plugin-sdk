# Doorbell implementation details

The half of the doorbell design that is **not** a doorbell record: the
control-plane messages the op documents assume, and the boundary between a
doorbell transport and what sits above it.

Specifications: `doorbell_protocol.md` (transport), `doorbell_M_memory.md`,
`doorbell_E_input.md`, `doorbell_D_notification.md`. Principles:
`doorbell_principles.md`, `plugin_principles.md`.

---

## 1. Status

**Protocol `1.0` has exactly one doorbell**: the fixed 112-byte record format of
`doorbell_protocol.md`, with the three ops `M`, `E` and `D`. Protocol `1.0`
hosts are vizi v0.9.0 and later.

- **The host implements both roles of the transport on all three platforms** —
  Linux `SOCK_SEQPACKET`, macOS `SOCK_DGRAM` with its `SO_RCVBUF` raise and
  `SO_NOSIGPIPE`, Windows loopback TCP with `WSAPoll` and the token handshake
  (`doorbell_protocol.md` §3.1, §3.2). On Windows a stranger's connection is
  dropped and the listener keeps accepting.
- **The host grants through it on every session**; there is no gate and no
  other path. It allocates `segment_id`, `interaction` and `seq` on admission,
  composes `E` from the pane's epoch, the binding's handle, the channel set and
  the view's ray or voxel, and routes what the transport observed through
  `doorbell_protocol.md` §10's escalation table.
- **The plugin side is `vizi_plugin_sdk/doorbell.py`** in this repository, and
  the Go, Rust and Java SDKs under `polyglot/` each implement it from these
  documents.
- **The gRPC surface of §2 is implemented at both ends.** Byte-exact vectors
  are deliberately deferred (§6).

---

## 2. The gRPC surface the doorbell assumes

`proto/vizi_plugin.proto` carries every message below. The doorbell documents
lean on it constantly — a grant is not usable until its meaning arrives, a fault
is reported over gRPC, an arrangement precedes its use — so the inventory is part
of the specification rather than a to-do list appended to it.

**Each of these is specified in `control_plane_protocol.md` §8**, which is where
its direction, message class, delivery rules and deadline live. What follows is
the doorbell's own list of what it depends on.

| Message | Direction | Named by |
|---|---|---|
| `DoorbellFault` | plugin→host | transport §12 |
| `DoorbellShutdown` | host→plugin | transport §8, §12 |
| `DoorbellFinalReport` | plugin→host | transport §8, §12 |
| `ContractViolation` | **both** | transport §8, `E` §2 |
| `Geometry` | host→plugin | §3 below |
| tool activation / binding | host→plugin | `E` §5 |
| index→time mapping | host→plugin | `E` §1 |
| `EpochChange` — `(pane, epoch, full state)`, one or more per message | host→plugin | `E` §1a |
| `EpochAck` — `(pane, epoch)` pairs | plugin→host | `E` §1a |
| `SegmentRef` | plugin→host | carries `segment_id` as u64 |

The segment lifecycle — `InstallSegment`, `RetireSegment`, `SegmentReleased`, the
grant acknowledgement and its state query, and `ReleaseDeposit` —
belongs to the memory layer and is inventoried in
`shared_memory_implementation_details.md` §2.

**`ContractViolation` is not `DoorbellFault`.** `DoorbellFault` is sent
immediately before closing the doorbell and ends the session; `ContractViolation`
reports a well-formed record whose *content* was wrong, drops it, and keeps the
session — which is `doorbell_protocol.md` §8's split, and is why the two cannot
be one message. Its shape and its sites are in
`shared_memory_implementation_details.md` §2.

---

## 3. `Geometry` — what a plugin has to know about the data

**Everything, and re-sent whole on any change.** A plugin cannot be asked to
reason about coordinates it only half understands, and edges are what a lossy or
reorderable path exposes (`plugin_principles.md` 2): carrying state and letting
the receiver infer the transition is strictly cheaper than getting a delta
protocol right for something that changes at human rate.

```
Geometry {
  gen                        // the open generation this describes
  channels     [ {index, name} ]
  extent       z0, zN · y0, yN · x0, xN     // half-open, level 0 voxel indices
  crop         z0, z1 · y0, y1 · x0, x1     // EdgeBox, doubles: the CLIP box,
                                            // which is NOT the lit set
  spacing      uZ, uY, uX                   // physical size of one level-0 voxel
  spacing_unit                              // "um" | "" when unknown
  spacing_provenance                        // file | override | display-fallback
  levels       [ [lZ, lY, lX] ]             // per level, scale relative to level 0
}
```

Three things this settles that the doorbell cannot:

- **Anisotropy.** `E`'s coordinates are voxel indices, and a `RAY` is exact
  without spacing — but any *angle* a tool computes needs it. A plane
  fronto-parallel in physical space has voxel-space normal `dᵢ·sᵢ²`, not `dᵢ`
  (`E` §3), and a plugin without `spacing` cannot construct one.
- **Absence is expressible.** `spacing_unit` empty and
  `spacing_provenance = display-fallback` say "we do not know how big a voxel
  is", which vizi genuinely often does not. A plugin must not read the fallback
  1.0 as a measurement — that is `plugin_principles.md` 11 applied to the one
  field most likely to be believed.
- **It changes mid-session.** The user can recalibrate the voxel size at any
  time, so "sent once at open" would be wrong.

**The index→time mapping stays outside `Geometry`.** Whole-state resend is free
only while the state is O(1); a mapping is O(T) and would be re-sent on every
crop drag. It is arranged per `gen` on its own.

**There is no dataset world transform to carry.** vizi's volume is axis-aligned
with per-axis physical spacing; the only affines are per-object — ROI
placement frames and overlay transforms — and those belong to the objects that own them, not to the dataset frame. The field
is deliberately absent rather than present-and-identity: an identity transform
is a claim, and the field is added when there is something true to put in it.

---

## 4. Reserved

**There is no per-pane camera arrangement.** `E`'s `RAY` layout carries the
whole camera on the record, so nothing is arranged and nothing can outrun it
(`doorbell_E_input.md` §3, "The ray layout"). This section number is reserved.

---

## 5. Arrangement is acknowledged before it is used

**`control_plane_protocol.md` §5.5 states the rule and `E` §2 says which of
`E`'s references it covers; this section is what it costs**, and states neither.
Concretely: **a
binding is not installed until its activation is acked.** That is one gRPC round
trip at activation time, against a user who is about to move a mouse — and it
removes the race outright rather than buffering events behind it, which is the
only other way to be correct here.

**The pane epoch is the one arrangement with a receiver-side defence as well**,
and the two are not redundant. This rule binds the host: it stops emitting for a
pane the moment a parameter changes, and resumes only per plugin that has
acknowledged. `E` §1a's stale-epoch rule binds the plugin, and covers the case
this rule cannot — records already transmitted and not yet drained, which arrive
carrying the old epoch however well the host behaves. Every other arrangement
here fails loudly if its rule is broken; a wrong interpretation fails
*silently*, by having the tool act on the wrong data, so it gets both halves.

Teardown is the asymmetric half and cannot be arranged, because a deactivation
rides gRPC while its `CANCEL` rides the doorbell. Every tolerance rule in `E` §2
exists for that side.

---

## 6. Constants, vectors and the transport boundary

`doorbell_protocol.md` §13 defers byte-exact vectors deliberately: an SDK
implements the plugin side from these documents — receive `M`, `E` and `D`, send
`D`, frame and validate in both directions — and anything it has to guess is a
defect in the specification.

The numeric constants in the op documents are normative, because they are
what makes a byte-exact record derivable at all: `E`'s `phase`, `payload_kind`,
`target_kind` and `mods`; `M`'s `perms`; `D`'s bit order; and every layout
block's origin.

What binds the plugin, and what is host-only machinery it should not build, is
`doorbell_protocol.md` §11a.

### The transport / supervisor boundary

Several rules in these documents are written as obligations on "the plugin" and
are unimplementable by a doorbell transport alone: reporting a fault over gRPC,
telling a retired tool handle from one never seen, bounds-checking a `D` mask,
recognising a duplicate grant. Each is marked where it appears; this is the
line in one place, because a reference transport needs to know what it is *not*.

**The transport owns:**

- framing, CRC, header fields, fixed padding, ancillary descriptors, socket
  errors and their classification;
- encoding and decoding typed `M`, `E` and `D` values;
- record-`id` gap and regression diagnostics;
- op-local validation — everything checkable from the record alone;
- `E` interaction sequencing and dedup, which needs only its own history;
- **handing up each frame's raw 112 bytes alongside its decode.** Two rules
  elsewhere need them and neither can get them otherwise:
  `shared_memory_protocol.md` §8's duplicate-grant test compares the bytes of an
  `M`, and `ContractViolation` reports the offending record. A transport that
  decodes and discards leaves both unimplementable, and an implementer building
  from `doorbell_M_memory.md` §7's hand-up list alone would get the wrong type;
- **retaining the 112 bytes of each open interaction's opening record**, which is
  a third use of the same bytes and the only one that is the transport's *own*
  rather than a hand-up. `doorbell_E_input.md` §2 step 2 settles a watermark hit
  by comparing the frame against the record that opened the interaction, so a
  transport that hands the bytes up and keeps none cannot perform its own dedup.
  The retention is bounded — at most one record per active tool, released when
  the interaction closes — and it is worth stating separately because sizing it
  from the two hand-up cases alone gives zero;
- emitting the **observable outcomes** below. This is a list of what a consumer
  must be able to see, not a prescription of types or callbacks — a monolithic
  handler and three thin modules should both be able to satisfy it.

  | outcome | when |
  |---|---|
  | a **frame disposition** — decoded record, unknown opcode, semantic rejection, ignored condition, or **framing rejection** | once per frame |
  | zero or more **diagnostics** attached to it | an `id` gap or regression, a reserved bit, a local-only violation. **Every "counted, not reported" condition in these documents surfaces here** — `plugin_principles.md` 6 requires a developer-findable surface, and a counter nobody can read is not one |
  | zero or more **interaction state changes** | a cancellation, whether a frame caused it or `tool_retired` did. **A frame-caused cancellation FOLLOWS the frame that caused it** — see below |
  | a **terminal fault** | once, ever; ends the session. **Preceded by one cancellation per interaction still open**, so no consumer is left holding a gesture that will never end — the same for teardown |
  | **teardown** | not a frame and not a fault (`doorbell_protocol.md` §8) |

  **The two ordering rules point opposite ways and both are deliberate.** A
  cancellation a frame caused comes *after* that frame; the cancellations a
  terminal implies come *before* it. The terminal ends the stream, so anything it
  would otherwise strand has to precede it. A frame ends nothing, and its
  cancellation is a consequence of the disposition the consumer is about to be
  handed — so the cause arrives first, and a consumer whose cancellation handler
  wants to know *why* has already seen the record and the `where`.

  **The accepted case settles it, because there the closure has nowhere else to
  go.** An accepted `PRESS` names the interaction it implicitly closed on the
  opening event itself and no cancellation row accompanies it
  (`doorbell_E_input.md` §6) — the closure rides the frame. Emitting the
  *rejected* case's cancellation ahead of its frame would invert the
  relationship between two neighbouring rules about the same event, and an
  implementation that reads one from the other would get it wrong.

  Three things this settles. **The first three rows are not alternatives**: one
  valid frame can carry an `id` gap *and* a decoded record *and* an op-level
  violation, and a model that forces a choice loses two of the three.
  **Cancellations are their own row because they need not come from a frame** —
  `tool_retired` produces one with nothing on the wire. And **teardown is
  neither a disposition nor a fault**; the transport is the only thing that can
  observe an orderly close, so without a row for it an implementer either invents
  one or reports a normal shutdown as a fault, which is what §8 exists to prevent.

  A terminal fault is a *row*, not a disposition, because it is not a property of
  a frame: it is the session ending, handed up synchronously before the socket is
  released (`doorbell_protocol.md` §8). A frame may be what revealed it, and that
  frame still gets its own disposition — the rejection that caused the fault.

  **On the send side, only escalation reaches the consumer.** A successful send
  cannot prove delivery (`doorbell_protocol.md` §9), so a per-send result is not
  something correctness can rest on, and a dropped `D` or `DRAG` is its class
  working as designed. What must surface is what needs action outside the
  transport: **retry exhaustion, a non-disposable edge abandoned because the
  bounded queue filled, a terminal send fault, and teardown.** Everything else is
  a counter a consumer can query. Whether an individual send returns a boolean,
  an enum, a future or nothing is an implementation choice.
- receiving **lifecycle inputs** it cannot source itself, and owning the state
  machines they feed (`doorbell_E_input.md` §2). There are five:
  `tool_activated(handle)`, `tool_retired(handle)`,
  `epoch_acknowledged(view_id, epoch)`, `cancel_interaction(n)` and
  `cancel_interactions(view_id = …)`.

  **A lifecycle input fails after the terminal latch, and after the listener has
  exited**, rather than enqueueing an outcome nothing will drain —
  `tool_retired`'s cancellation is the reachable case.

  **Every one is synchronous and serialized, and returns success or failure to
  its caller** — a supervisor that acknowledges an activation whose
  `tool_activated` silently failed leaves host and transport disagreeing about
  whether the tool exists. **They are not all idempotent**, and saying so
  uniformly would be false: `epoch_acknowledged` is only idempotent for an
  epoch it has already installed.

  | | does |
  |---|---|
  | `cancel_interaction(n)` | removes exactly interaction `n` from the index; clears the per-tool open interaction **only if** that tool's current one is `n`; emits exactly one cancellation outcome if an entry was removed; **no-op** for an unknown or already-closed `n` |
  | `cancel_interactions(view_id=)` | the same, for every entry the index holds under that pane; one cancellation outcome per entry removed; **no-op** when none match. Its producer is a pane going away; an epoch change closes through `epoch_acknowledged` |
  | `tool_retired(handle)` | an **active** handle retires and succeeds. `handle == 0` or `handle > high_water` fails without mutation. At or below the mark but inactive — already retired, or a gap never registered — is an idempotent **success**, since the state it asks for already holds. Retirement never advances the mark. Purges the index entry and cancels the open gesture (§2) |
  | `tool_activated(handle)` | accepted for **any `handle > high_water`**: adds it to the active set and moves the mark to it. A repeat of a live handle is a no-op; a retired handle and `handle == 0` fail without changing state. **A gap is accepted deliberately** — see below. **Calls MUST arrive in increasing-handle order**: 12 before 11 moves the mark past 11, which is then rejected forever as retired. Serialization does not supply this on its own — allocating each handle at engagement, and replacing a displaced activation's statement with its retirement, does (`control_plane_protocol.md` §8.10) |
  | `epoch_acknowledged(view_id, epoch)` | **`epoch == 0`**: fails without mutation. Zero is the absent-epoch sentinel (`doorbell_D_notification.md` §5) and is a `zero_id` `ContractViolation` on the wire, so it is never a value to install — same shape as `tool_activated`'s `handle == 0`. **Newer** than the installed value: install it and close every interaction open in that pane, one cancellation outcome each. **Equal**: no-op, and in particular closes nothing — a gesture opened since must survive. **Older**: rejected, never rewinds. Every later record for the pane is ruled against the installed value (`doorbell_E_input.md` §1a) |

  **None of them ever rewinds the open watermark.** That would let a duplicate
  `FIRE` act twice, which is the single harm the dedup exists to prevent.

  **What the supervisor owes when it rejects a record the transport accepted.**
  The transport commits interaction state before the supervisor can rule on
  `gen`, `view_id` or the pane epoch, so an upstream rejection always
  arrives after the fact. The obligation depends on what was rejected, and the
  two halves are not symmetric:

  | rejected record | the supervisor MUST |
  |---|---|
  | an **opening** record | call `cancel_interaction(n)`. Without it the transport delivers every later `DRAG` and the `RELEASE` for a gesture the supervisor believes never opened |
  | a **`DRAG`** | nothing; dropping one sample is that class's policy |
  | a **terminal** | **not reject it.** `cancel_interaction` cannot repair this — the transport has already closed the interaction and the call is a no-op on an id that is gone, so the consumer would receive neither the terminal nor a cancellation, and its gesture would never end |

  **A supervisor rejection of a terminal has no expressible outcome**, and that
  is exactly why **the pane epoch is pushed DOWN into the transport rather than
  ruled upstream.** A stale-epoch `RELEASE` would otherwise be a supervisor
  rejection of a terminal — the unrepairable row above — and no rule written
  here could fix it after the fact. `epoch_acknowledged` gives the transport the
  one fact it needs, so the epoch is checked inside the ordered algorithm, ahead
  of the index update, and the terminal never reaches a state that cannot be
  undone. Same argument as `tool`, and the same shape of answer: a lifecycle
  *input* rather than a lifecycle *query*.

  **There is no `gen`-scoped form.** `E` §2's `gen`-scoped cancel list — voxel
  spacing, extent, crop, the level table — is exactly `§1a`'s epoch parameter
  set, so every event that would call it is already an epoch change for the
  panes it affects and closes through `epoch_acknowledged`. Keeping it would
  also oblige every supervisor to diff a whole-state `Geometry` to decide
  whether a change was interpretation-changing, for a field set nothing
  specifies.

  **The scoped form exists because `E` §2 makes a cancellation mandatory that
  the supervisor cannot otherwise perform.** "Before applying an
  interpretation-changing `Geometry`, cancel every open
  interaction it affects" is scoped by `gen` or by `view_id` — but the index that
  maps interaction → `(tool, gen, view_id)` is the transport's, and
  `cancel_interaction(n)` is keyed on an `n` the supervisor does not hold. The
  alternative is every supervisor shadowing the index from emitted outcomes,
  which is the split-decoding cost this section rejects for `tool`, and which
  diverges silently when a shadow misses an implicit close.

  `tool` and the pane `epoch` are pushed *down* into the transport because their
  checks sit inside the ordered algorithm, ahead of the watermark and the index
  update, so ruling on either upstream would split decoding in two. **`gen` and
  `view_id` are ruled
  *up*stream**, because nothing about them is checkable in the ordered path — and
  that is exactly why the retraction inputs are needed. Without them the
  transport accepts a `PRESS` naming an unacknowledged `gen`, advances the
  watermark, inserts the interaction into its index and hands it up; the
  supervisor rejects it; and the transport then delivers every `DRAG` and the
  `RELEASE` for a gesture the supervisor believes never opened;
- exposing a **thread-safe stop**. Ending the listener is an operation the
  transport must publish, not something a consumer can achieve by closing a
  socket behind its back — which is why `shutdown()` is not the exit path
  (`doorbell_protocol.md` §6 rule 3), independently of how it happens to behave
  on any one socket type.

**The transport does not:** send gRPC messages, map or interpret shared memory,
validate arrangements, poll regions, or supervise sessions. A fatal fault is
handed to the supervisor **synchronously, before the socket closes**
(`doorbell_protocol.md` §12) — the transport raises it and the supervisor is what
puts a `DoorbellFault` on the control plane.

**"Deduplicate" means four different things and only one of them is the
transport's:**

| | |
|---|---|
| header `id` | **never** a dedup input — diagnostic only (`doorbell_protocol.md` §4) |
| `E` | the open watermark, the opening record's byte comparison, and `seq`; the transport's, given the tool lifecycle inputs above — it sources no outside state, but it is *told* two facts and it *retains* one record per open interaction |
| `D` | duplicates are harmless by construction — the mask is an OR and the poll is the backstop; no dedup at all |
| `M` | duplicate *installation* is the memory layer's; the transport delivers the record, its `id` and the descriptor safely and stops there |

**"Inline dispatch" is the SDK's posture, not the transport's.** The transport
emits typed outcomes synchronously and does nothing else — it never calls a tool
handler, sends a gRPC message, or supervises a session. What follows is about the
consumer that sits on top of it, and it is a choice that consumer makes.

**That consumer dispatches inline on the listener thread**, which is the reference
SDK's posture and is deliberate: a slow `E` handler means the socket stops draining,
the host's sends are refused, and `DRAG` records are dropped at the source rather
than piling into a queue the plugin cannot service — `doorbell_principles.md` D1
working as intended. The cost is real and should be documented rather than
discovered: **an `M` grant queued behind a slow `E` handler has its
acknowledgement delayed by that handler.** A handler budget belongs in the SDK's
documentation; handing work to a worker thread is the alternative, and it trades
that delay for a queue whose depth nobody bounds.

---

## 7. Open

- A resolvable namespace for host-owned targets — annotations, ROIs and handles
  need stable session-scoped ids a plugin can hold across events (`E` §9).
- The binding registry: conflict resolution over (button × modifier), and
  whether double-click is a binding rather than a per-record `click_count`.
- Whether the 2D pane's `level` tracks zoom. If it does, `E`'s `level` already
  carries click precision; if not, a click zoomed out is coarser than a voxel and
  nothing on the wire says so.
- Whether `Geometry` should carry per-channel display state (contrast, LUT) or
  whether that belongs to a separate scene feed. `E` carries channel
  *visibility* per record; nothing carries appearance.
