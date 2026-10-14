# Doorbell design principles

The framework the doorbell documents are consistent with. Details without it
drift; this is what to check a change against.

**Parent set.** The general principles — delivery, ordering, bounds, recovery,
truthfulness, and how much machinery a failure deserves — live in
`plugin_principles.md`. Everything below is specific to *this* channel and does not generalise beyond a
system with a constrained fast path alongside a rich reliable one.

Nothing here specifies the control plane. Where the control plane appears, it is
because it is the other half of a doorbell interaction — a grant's acknowledgement,
a fault report, a segment's meaning.

Specifications: `doorbell_protocol.md` (transport), `doorbell_M_memory.md`,
`doorbell_E_input.md`, `doorbell_D_notification.md`. The control-plane half these
assume is inventoried in `doorbell_implementation_details.md`, specified in
`control_plane_protocol.md`, and what of it exists today is
`control_plane_implementation_details.md`.

**The layer below.** What granted memory *contains* and how it is handed over is
not this channel's business: `shared_memory_principles.md` and
`shared_memory_protocol.md`. The doorbell transmits a capability and a set of
indices; interpreting either is the memory layer's.

---

## A. What belongs on the doorbell

**A1. Capability versus meaning.** Put on the constrained channel only what that
channel uniquely provides. The doorbell provides two things: file-descriptor
passing, and ~11 µs latency. Anything needing neither belongs on the control
plane.

Applied: `M` carries the capability — size, permissions, name, segment id — and
nothing about what the memory is *for*. Meaning is a gRPC message that can grow
fields; a fixed 100-byte payload cannot.

**A2. Arrangement may use the control plane; use may not.** Setting a capability
up costs control-plane traffic — a segment is unusable until its meaning arrives —
and that is fine, because it is one-way host→plugin content paid once. What must
never cost it is *use*: no press, release or live update may wait on a
control-plane message, and batching does not rescue that, since a drag event
batched is a drag event delayed.

A plugin-initiated request is a round trip, so it must not sit on a **per-sample**
path — nothing may wait on it between a `DRAG` and the response to it. **A
once-per-gesture round trip the plugin chooses to make is permitted**, provided no
host action waits on it and its result is applied retroactively rather than
blocking the stream; `doorbell_E_input.md` §3's optional anchor resolution is the
case. **This rule is about what *blocks*, not about how often a message is
sent** — such a round trip is not "arrangement", which is paid once and reused,
and it does not need to be in order to be legal.

Granting and releasing a segment per frame is a different mistake and stays
forbidden (D2, and `doorbell_M_memory.md` §8): it is unbounded in memory, not
merely latent.

**A3. Nothing is delivered on two channels.** No record has a reliable gRPC copy.
Dual-path delivery reintroduces ordering and duplicate ambiguity, and every
loss-recovery mechanism here is single-path — the unconditional poll for advisory
hints, bounded retry for gesture edges.

**A4. To make something faster, move its content into shared memory** — do not add
a hint alongside it. A hint can only accelerate state that is already durable
somewhere, because it carries nothing itself.

---

## B. What a record is

**B1. Fixed size, always.** **No length field determines a record's extent**, so
no peer-controlled length reaches a host read and skipping an unknown opcode is
always safe. A length *inside* a field of fixed extent — `M`'s `name` — delimits
nothing and is not what this forbids; the tail past it is zero like every other
unpopulated byte, which is what keeps a record a pure function of its fields and
byte-exact cross-language conformance vectors possible.

**B2. Size constants are measured on the tightest platform, not chosen.** 112 bytes
is where macOS's cheapest socket-buffer bucket ends; 128 would cost an eighth of its
stock queue depth (32→28) for headroom nothing uses. Any future size decision on this
channel is settled the same way.

**B3. Keep a guarantee to one rule.** The framing guarantee is `op != 0xFF` plus a
marker as long as a record. A three-part derivation invites a later version to spend
one of the parts; a one-line invariant survives. The bound this yields is
deliberately loose: `ver`, `flags` and `reserved` would tighten the longest legal
`0xFF` run from 111 to 108, and taking that would make three rules load-bearing to
save four bytes of margin nothing needs.

**B4. Structural over probabilistic.** A layout is stated, never sniffed from magic
bytes. A bound that cannot be represented beats a constant checked at scan time.
Where neither is available, say what the residual probability is — the framing
integrity bound is ~2⁻³² and is documented as such rather than implied to be zero.

**B5. Passing the frame confers no trust on the contents.** The checksum detects
accident, not intent: CRC-32 is linear and invertible, so every field is validated
independently of whether the record checksummed.

---

## C. Symmetry

**C1. One record format, one framing, one discipline, both directions.** A receiver
validates identically regardless of which end it is. Maintaining separate client and
server protocols is the cost this avoids.

**C2. Exactly two things differ by direction, and an op specification states both.**
**Trust** — vizi controls its own sends, so host→plugin cannot be corrupted by the
peer, while plugin→host carries whatever a bad implementation emits. **Rate-limit
ownership** — each receiver owns its coalescing and quota windows, and neither is a
protocol constant.

"Identical semantics" reads as "identical handling" unless these are spelled out.

---

## D. Record classes

Every **record** belongs to exactly one class, and the class decides its loss
policy. An opcode may span classes — a stream of samples and the edge that
terminates it are not the same class.

| Class | On refusal | Reliability from |
|---|---|---|
| Disposable | dropped | the receiver's independent path |
| Retried edge | retried, bounded, then escalate | an unambiguous refusal signal |
| Acknowledged capability | retried, bounded | a gRPC acknowledgement meaning *installed* |

**D1. Disposable means dropping is correct, not degraded.** A peer that can process
25 records/sec should see 25 sampled from the stream, not a backlog of stale ones.
Delivering everything would deepen a queue it cannot service.

**D0. An ordering inference is permitted only where the ordering it rests on is
an established property of every transport, and the consequence of that property
failing is named.** A watermark says "below this, therefore already seen" — a
deduction from in-order delivery, not an observation. When the deduction is wrong
the record is *dropped*, so the mechanism's failure mode is loss and the strength
of the ordering is the whole of what holds it off.

`E`'s opening watermark qualifies on exactly that basis, and **not** on a claim
about severity — it guards `FIRE`, a retried edge carrying an action, whose loss
the class does not tolerate. In-order delivery is guaranteed for TCP and
`SOCK_SEQPACKET` and measured for `SOCK_DGRAM`, and `doorbell_E_input.md` §6
names what breaks if it ever fails — a reordered opening record dropped as a
duplicate, losing an action — together with the remedy. A false positive there is
*counted locally*, so calling it "escalates rather than passing silently" would
be wrong; the justification is the established property, not a soft landing.

`seq` guards **disposable** samples on the same property. `D` infers nothing,
having no dedup at all. And `M` is an **acknowledged capability**, so it must
infer nothing: its duplicate test is an exact comparison of the record's bytes,
keyed on a `segment_id` that is never reused, and there is no watermark anywhere
in the grant path. Where loss is expensive the design relies on no deduction at
all — the severity ordering doing its work one layer above this rule.

**D1a. A retried edge is either idempotent or dedupable.** Retry is safe because
refusal is unambiguous, not because the content is harmless twice. Where the content
is a *state statement* — "interaction N ended" — a duplicate costs nothing. Where it
is an *action*, the record carries an id the receiver dedups on, because "the
transport never duplicates" is a property to defend rather than one to rely on
silently.

**D2. Queue depth is sized by the highest-rate op that cannot coalesce.** A
coalescing op collapses to one record per receiver window regardless of how much
changed; a non-coalescing one puts its full rate on the wire. That is input at
~1 kHz, which is why buffer sizing is a per-platform requirement rather than a
tuning option.

**D3. An acknowledgement means the thing the caller needs to know.** Not
*transmitted* — which is all a sender can ever establish on its own — but
*installed*, which requires the receiver to say so, and which is therefore a gRPC
message rather than a doorbell one.

---

## E. Failure posture

**E1. Fail-stop, not self-repair.** The doorbell is a small bespoke channel beside a
mature one. If it fails while gRPC is healthy, the cause is a bad implementation, a
bug in ours, or a platform assumption that needs revisiting — so expose it and end
the session rather than teaching the ad-hoc channel to repair itself. gRPC is where
the failure is reported.

**E1a. An unknown opcode is dropped and counted, never a fault.** Fail-stop is
for a peer that got the *contract* wrong, and the handshake has already settled
the contract — so an unknown opcode is an unknown message *kind*, which is the one
thing append-safety covers (`plugin_principles.md`, Version skew). Fixed framing
is what makes skipping exact. This is the single rule three other places assume:
B1's "skipping an unknown opcode is always safe", `doorbell_D_notification.md`
§5's additive direction, and every future op.

**E2. Reserve syntax for recovery without implementing it.** `op != 0xFF` and the
all-`0xFF` marker stay reserved and unemitted, so recovery remains a purely additive
change if field evidence ever shows the *assumption* is wrong rather than a peer.
Reserved syntax with a **stated precondition** is cheap; reserved syntax with no
stated consumer is dead code nobody dares remove.

Reserved is not the same as recognised: a baseline receiver cannot identify a
marker, because its checksum field does not match its own bytes and it fails CRC
first (`doorbell_protocol.md` §5). What is reserved is the *byte pattern* and the
invariant that no legal record produces it — which is what a scan needs, and which
a receiver gains the moment it negotiates recovery and adds the compare.

**E3. The escape hatch is part of the design, not a consequence of it.** A wedged
sender is released by `shutdown()`, which means a socket handle cached outside the
lock a wedged sender holds, and a rescuer that never takes that lock. A call to
`shutdown()` existing somewhere does not mean that machinery exists.

**E4. Every safety property is asserted at every site that depends on it.** On this
channel that means `MSG_DONTWAIT` at every send site rather than an `O_NONBLOCK`
inherited from a reactor's `dup`, and a socket's blocking mode set once at setup
rather than latched by whichever call site ran first.

---

## F. Layering

**F1. The grant layer is whole-block.** Lifetime, ownership and retirement are per
segment. How the interior is carved up makes no difference to any of them, so
region discipline is not a grant property — one segment routinely carries several
disciplines at once.

**F2. An advisory hint points; it never carries.** The hint names what to look at
and the receiver reads state from shared memory. What it names is a lower bound —
"definitely look here" — never an exact set, so a drop, a wrong bit or an overflow
degrades latency rather than correctness.

**F3. A hint lets the poll be slow; the poll is not the hint's fallback.** The
relationship reads backwards otherwise, and someone eventually removes the poll as
redundant CPU — at which point the hint is correctness-critical with no
acknowledgement and nothing says so.

---

## Checklist for a new opcode

1. Does it need the doorbell at all — a capability to pass, or interactive latency?
   If not, it is a gRPC message. (A1)
2. Which **class** is it, and therefore what happens on refusal? (D)
3. Which **directions** are legal, and what are its trust and rate-limit rules in
   each? (C2)  An op document states all three in its header, and an illegal
   direction is a session fault (transport §2) — which is safe to make fatal only
   because a direction that can never open costs no forward compatibility, unlike
   an unknown opcode.
4. What is the **fd count**, and is it constant for the op **on a given
   transport**? (transport §7 — `M` is 1 on unix and 0 on Windows, which is
   constant per transport, not per op)
5. Does the payload fit in 100 bytes with **zero padding and no length field**, and
   is every bound structural where it can be? (B1, B4)
6. If it claims a value, can it also express **not having one**? (parent principle
   11)
7. If it is advisory, do all five exemption conditions hold and are they stated in
   the API? (parent principle 3, advisory exemption)
8. What does its **acknowledgement mean**, if it has one, and is that what a caller
   needs? (D3)
9. What is the **worst thing true** when it is dropped — and is the dropped record
   the worst of them? (parent principle 9)
