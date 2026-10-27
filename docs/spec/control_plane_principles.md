# Control plane design principles

The framework the control-plane documents are consistent with. Details without
it drift; this is what to check a change against.

**Parent set.** The general principles — delivery, ordering, bounds, recovery,
truthfulness, and how much machinery a failure deserves — live in
`plugin_principles.md`. Everything below is specific to *this*
channel and does not generalize beyond a system with a rich reliable plane
alongside a constrained fast one.

Specification: `control_plane_protocol.md`. Where the shipping host differs:
`control_plane_implementation_details.md`.

**The other two layers.** The fast path is `doorbell_principles.md` and its four
documents; what granted memory contains is `shared_memory_principles.md` and its
two. This channel is the half those assume: a grant's acknowledgement, a
segment's meaning, an epoch's state, a fault's report. Where they name a
message, this channel is where it is specified.

---

## A. What belongs on the control plane

**A1. Put here what needs either of the two things this channel uniquely
provides: reliable ordered delivery with a failure detector, and a schema that
can grow.** Anything needing neither and wanting latency belongs on the
doorbell; anything that is bulk bytes belongs in shared memory.

This is `doorbell_principles.md` A1 read from the other end, and the two
together are the whole allocation rule. The doorbell provides descriptor passing
and ~11 µs; a fixed 100-byte payload cannot grow a field, and a refused record
is simply gone. Where either of those is the problem, the message is a gRPC
message.

**A2. Three jobs, and naming them prevents a fourth from accreting.** The
control plane carries **meaning** (what a granted segment is, what a coordinate
means), establishes **arrangement** (what the fast path is then permitted to
reference), and is where the fast path **reports and reconciles**. A proposal
that is none of the three is either a doorbell record or a data-plane write, and
should be rejected here rather than accommodated.

**A3. Nothing on this channel is ever on an interactive path.** The reciprocal
of `doorbell_principles.md` A2: arrangement may use the control plane, use may
not. No press, release, drag or live update may wait on a message here. Two
properties make that non-negotiable rather than a preference:

- **Latency.** A control-plane round trip costs hundreds of microseconds at the
  median and milliseconds at the tail, against the doorbell's ~11 µs. The exact
  gap depends on the two implementations, and no measurement of it changes A3,
  because of the second property.
- **Head-of-line blocking.** One stream, and both ends dispatch it in order. One
  slow handler delays everything behind it, which no latency number improves.

Batching does not rescue this: a drag event batched is a drag event delayed.

**A4. Bytes never cross it.** A message carries a *descriptor* — which segment,
what offset, what shape — and the bytes live in shared memory the peer maps.
Serializing a volume here would force a copy out of the buffer the whole data
plane exists to avoid.

The rule has a corollary that is easy to lose: **a descriptor is only as good as
the grant it names**, so a message carrying one is meaningless until the segment
it references is installed. That is A2's arrangement, not a detail of the
message.

---

## B. What a message is

**B1. The IDL settles encoding and nothing else.** protobuf gives field numbers,
append-safety and a generator for every language, which is why the control plane
needs no conformance corpus where the doorbell does. What it does not give is
**behavior**: who may send
what and when, what a reply means, what happens when a queue fills, what a
timeout is evidence of, and what a peer owes after it has acknowledged
something. That half is prose, and prose has no compiler.

So the test for whether something belongs in `control_plane_protocol.md` rather
than in a `.proto` comment: **could two implementations both satisfy the IDL and
still fail to interoperate?** If yes, it is protocol.

**B2. Compatibility is settled at connect, and the version covers all three
channels.** `plugin_principles.md`'s Version-skew rule applies unchanged: two
gates, the manifest scan and the `Hello`/`Ready` handshake, and a mismatched peer
fails whole with a diagnostic naming both versions.

One consequence is load-bearing elsewhere and is stated here because this is the
channel that owns it: **one `protocol_major.protocol_minor` implies exactly one
doorbell record format** (`doorbell_protocol.md` §3.1a). The doorbell's `ver`
byte describes what a frame *is*; it negotiates nothing, and it could not — a v1
receiver reading a v2 record sees a nonsense length. The handshake here is the
only version gate the platform has, and a transport property that differs
between two doorbell formats is therefore a bump of *this* version.

**B3. Absence must be expressible, and proto3 makes that a decision rather than
a default.** An unset scalar and a zero are the same bytes, so a field whose
absence carries meaning MUST be `optional`, inside a sub-message, or paired with
an explicit presence flag. `ViewState.has_crop` and `RoiStyle.visible` are the
shipping instances of each. This is `plugin_principles.md` 11 meeting a wire
format that will silently fabricate a value on your behalf.

**B4. A reply names its request; a push names what it replaces.** Correlation is
not optional bookkeeping — it is what makes a late reply distinguishable from an
answer, and what makes a coalescing push safe to drop. A push with no
replacement key is a one-shot record and belongs to class D2, whatever it looks
like.

---

## C. Roles and asymmetry

**C1. The plugin is the server, the host is the client, and the stream's
lifetime is the session.** One bidirectional `Session` stream carries
everything; when it ends, every handle opened on it is gone. This is the
deliberate opposite of `doorbell_principles.md` C1's symmetry, and the asymmetry
is what makes lazy launch and crash isolation cheap: the host connects to a
process it started, and a dead plugin is a closed stream rather than a state to
diagnose.

**C2. Trust is asymmetric, and the direction decides how much validation a
message gets.** The host validates every plugin-supplied id, index, offset,
length and shape before acting on it — a plugin is untrusted input, and a
descriptor is a request to read host memory. A plugin may take the host's values
as well-formed, because the host is the supervisor and a host that lies to a
plugin has already failed at something larger (`plugin_principles.md` 9).

This mirrors `doorbell_principles.md` C2's trust rule and differs from it in one
way worth stating: on the doorbell both directions share one validating reader,
because the framing is symmetric. Here the two directions carry different
messages entirely, so the asymmetry is in what exists, not only in how hard it
is checked.

**C3. Both ends dispatch in stream order, and neither may block that dispatch.**
Ordering within the stream is free — it is TCP or a UDS underneath — and it is
the property most of this channel's guarantees rest on. It is also the property
a long-running handler destroys, so **work runs off the dispatch path or it is
short**. Where an implementation cannot promise short, it owes a worker.

---

## D. Message classes

Every message belongs to exactly one class, and the class decides what happens
to it under overload. This is the analogue of `doorbell_principles.md` D, and it
is the section the shipping implementation most needs.

| Class | Under overload | Reliability from |
|---|---|---|
| Coalescing push | **replaced per key**; never evicted by another key | the next push for the **same key** |
| One-shot push | never dropped; overflow is a session fault | the class itself |
| Request | queued, bounded; overflow answers an **error** | its reply |
| Arrangement | queued, bounded; never dropped | its acknowledgement, plus a deadline |

**D1. Loss is safe only where something restates what was lost, and that means
per KEY — never per queue.** A coalescing queue holds at most one pending item
per replacement key and a new item replaces the pending one for its key. It is
not a queue with an eviction policy, and drop-oldest on a FIFO is not a weaker
version of it but a different thing: drop-oldest evicts the oldest **message**,
so a lone pending update for one widget is destroyed by a burst of updates for
another, and nothing ever restates it. Making a message state-shaped does not
make arbitrary loss safe; it makes *superseded* loss safe, which is a much
narrower claim.

Two corollaries that are easy to skip and both bite:

- **The key is finer than a message and coarser than a property.** A
  `PanelPatch` batches ops across widgets, so the message cannot be the key; but
  two properties of one widget can be *ordered state* — `set_options` then
  `set_value` — so the property cannot be it either. The key is the object whose
  state is being replaced, and merging within it must preserve the DEPENDENCY
  between properties.
  **Where two properties each depend on the state the other establishes,
  nothing can be merged at all** — no ordering saves it. A dropdown is the
  case: its option list carries the selection across a change by text, and its
  value is checked against whatever list is there now
  (`control_plane_protocol.md` §5.4).
  **The test for a merge is the STATE it produces, never the order it emits.**
- **Prefer a key space that cannot be exhausted.** Where the set of live keys is
  fixed by something already known — a manifest, a declared contribution list —
  the queue is sized from it and overflow is unreachable, which is
  `plugin_principles.md` 8's structural-bound corollary and removes the need for
  a recovery protocol nobody would otherwise specify well.

`OverlayRemove` shows what the one-shot class is for: a plugin retracting an
overlay because it has finished will never send another, so a queue that
assumes everything on it is replaceable would lose the retraction for good. It
is on the one-shot queue.

**D1a. Priority between queues is an allocation problem, not a preference.**
Serving one class strictly ahead of another starves the other whenever the first
can be kept non-empty, and `plugin_principles.md` 9's allocation rule applies
between queues exactly as it does between sessions: every shared resource needs
a per-party bound. The specific trap is serving *ordinary work* ahead of *the
messages that finish that work* — a request stream that outruns the releases and
acknowledgements it generates starves its own completions and then faults a
healthy session (`control_plane_protocol.md` §7).

**D2. A one-shot message is either idempotent or reliable, and preferring state
to edges is how it becomes the first.** `plugin_principles.md` 2 is the general
form; here it decides which class a message lands in. Where a transition can be
restated as state — "this overlay's content is now empty" rather than "remove
it" — it becomes coalescing and the problem disappears. Where it genuinely
cannot — a fault, a release, an acknowledgement — the transport owes it delivery,
and losing it is a session fault rather than a degradation.

**D2a. A message carrying a descriptor is a handoff, and the class does not
change that.** Whatever a push's loss policy is, the *bytes* it names have one
owner at a time (`plugin_principles.md` 5): the sender may not reuse them until
the receiver has taken them, and "the receiver is probably done by now" is the
reasoning that rule exists to forbid. A coalescing push is free to be dropped;
the segment it pointed at is not free to be overwritten. The two are independent
and conflating them is how a timing assumption gets written into a protocol.

**D3. An acknowledgement means the thing the caller needs to know.** Not
*received* — which a reliable transport already established and which nobody
needed a message for — but **applied**: mapped, installed, registered,
interpreted. `doorbell_principles.md` D3 states this from the doorbell's side;
this is the channel that has to carry the answer, and an ack that means anything
weaker is worse than none, because it will be believed.

**D4. Silence is never an answer.** Every request receives a reply, including
every failure — no permission, no dataset, out of bounds, queue full, or an
internal error. A blocked peer cannot distinguish silence from slowness, so a
request the host declines to serve MUST be refused explicitly. The data-access
messages state this rule for themselves in `proto/vizi_plugin.proto`; it is the
channel's.

**D5. An arrangement is not installed until it is acknowledged, and the host
emits nothing that references an unacknowledged one.** This is the gate the fast
path depends on: `doorbell_E_input.md` §1a's epoch, §5's tool activation, and
`doorbell_M_memory.md` §6's meaning are all arrangement. The rule removes the
race rather than buffering it — there is no queue to grow and no event to drop —
and it is the reason a plugin that never acknowledges goes quiet rather than
wrong.

Which is exactly why **every arrangement has a deadline** (E5).

---

## E. Failure posture

**E1. This channel is the failure detector for the other two.** Its contribution
is that it produces either delivery or a connection error, never silence. That
is what lets the doorbell be fail-stop and lets a grant be reconciled by asking.
Every recovery mechanism in the other two documents terminates here, so the
posture that makes them work is: **this channel reports; it does not go quiet,
and it does not fail-stop on a peer's mistake.**

The asymmetry with `doorbell_principles.md` E1 is deliberate. The doorbell is a
small bespoke channel beside a mature one, so a violation there means someone got
the contract wrong and the right answer is to expose it and stop. Here a
malformed message is a decoder error in a generated stub, and the session is over
anyway if the stream is broken — so there is no equivalent ladder to build.

**E2. A timeout means *unknown*, never *dead*.** Control-plane timeouts are sized
against a busy control plane — seconds, not RTT — and expiry triggers a query,
not a teardown. Inferring death from slowness is the trap that makes a refused
doorbell write look like a dead plugin.

Where a timeout *is* unrecoverable, that must be a property of the concurrency
model stated as one, not a belief about the peer. A single-in-flight session
cannot resume after a lost reply — so the handle is marked suspect and the
session is restartable, and the diagnostic says which of the two happened.

**E3. Recovery is by asking, not by inferring.** No finite acknowledgement
exchange gives common knowledge and gRPC does not change that — it is TCP
underneath. What it supplies is a failure detector, so an ambiguous state is
resolved with an **idempotent query** for the peer's own account of it, and the
answers are part of the protocol rather than each host's invention
(`doorbell_M_memory.md` §7's four-row table is the worked instance).

**E4. Communication of failure must not itself be a point of failure.**
`plugin_principles.md` 9's corollary, and this channel is where it bites, because
this is where failures are reported. A per-record violation reported per record
puts the fault rate on the channel the session needs in order to recover.
Every reporting message on this channel is therefore **bounded**, and the bound
is a requirement rather than an optimization
(`shared_memory_implementation_details.md` §2 specifies `ContractViolation`'s).

**E5. An absence needs a deadline, or it is invisible.** An error arrives; a
message that never comes does not. Every acknowledgement this channel defines has
a bound after which the host acts — re-send idempotently, query, or escalate —
because the alternative is a session that is neither working nor failing.
`doorbell_M_memory.md` §7's missing-grant deadline and `doorbell_E_input.md`
§1a's epoch deadline are the two the fast path already depends on; the values are
host policy and are named-and-unmeasured (`control_plane_protocol.md` §9).

---

## F. Layering

**F1. This channel carries meaning; it never carries the capability.** A
descriptor cannot travel here — `SCM_RIGHTS` is a doorbell property — so a
message naming a segment is a statement *about* memory the peer was granted
elsewhere. The mirror of `shared_memory_principles.md` A2.

**F2. Arrangement is one-way, paid once, and reused.** A round trip per use is
what A3 forbids; a round trip per *arrangement* is what makes A3 affordable,
because everything after it is local. When a design finds itself wanting a
per-sample message here, the fix is to move the content into shared memory and
arrange the region once (`doorbell_principles.md` A4).

**F3. What the fast path may reference, this channel defines.** Segment ids,
region indices, tool handles, pane epochs and open generations are all allocated
and given meaning here, and the doorbell carries only the identifier. That split
is what keeps a 112-byte record from needing to grow, and it means a change to
any of those namespaces is a change to *this* document.

---

## Checklist for a new message

1. Does it need reliable delivery or a growable schema? If neither, and it wants
   latency, it is a doorbell record. (A1)
2. Is it meaning, arrangement, or reconciliation? If none of the three, say what
   it is before adding it. (A2)
3. Could anything interactive end up waiting on it? (A3)
4. Which **class** is it, and therefore what happens to it under overload? (D)
5. If it is a push, what does it replace — and if the answer is "nothing", it is
   one-shot and must not share a coalescing queue. (B4, D1, D2)
6. If it is a request, what does it answer on **every** failure path? (D4)
7. If it is an acknowledgement, does it mean *applied*? (D3)
8. If it establishes something the doorbell may reference, what is its deadline
   and what happens at expiry? (D5, E5)
9. If it carries bulk, is it a descriptor plus a segment? (A4)
10. Does any field's absence carry meaning — and is that absence representable?
    (B3, `plugin_principles.md` 11)
11. Is it append-safe, and does it need a version bump? Remember that bumping
    this version also declares a doorbell format. (B2)
