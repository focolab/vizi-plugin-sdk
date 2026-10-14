# Plugin platform design principles

Designing pieces of software that collaborate effectively involves a variety
of challenges that are uncommon in traditional sequential single-process
programming.  Because the vizi plugin architecture runs plugins as their own
independent processes--which may be independently written with reference to
and perhaps use of the plugin SDK--the principles that we recommend to
enable productive collaboration are described here.

Additional principles that guide the use of the high-speed doorbell system
are in an additional document.

| Content | Where |
|---|---|
| Protocol principles — general | §1–12 here |
| Channel-specific — control plane | `control_plane_principles.md` |
| Channel-specific — doorbell | `doorbell_principles.md` |
| Channel-specific — shared memory | `shared_memory_principles.md` |


---

## 0. The plugin API is a boundary

The central principle:

> **The plugin API is a boundary you publish.** Third parties build
> against a versioned IPC protocol and a thin open SDK; every line of vizi's
> implementation stays behind that boundary.

Any plugin feature that would require functionality to cross the boundary in
an undocumented way would violate this central principle.

The following principles for collaborative computing all assume that the
venue for collaboration is collapsed to this API boundary, with vizi acting
independently but cooperatively on one side, and the plugin doing the same
on the other.

---

## 1. There must be clear rules about who is in charge of what

All parts of the API should clearly define what each party is in charge of. 
Because they are independent, neither can count absolutely upon the other.
However, that does not mean that each should try to compensate for possible
failings in the other.  Instead, the API should make clear which party has
responsibility for which functionality.

---

## 2. Prefer state-independent and idempotent messages

These are two separable properties — a message can be state-independent
without being idempotent — and a design says which one it needs.

A state-independent message can be interpreted on its own, with no reference
to past messages. In practice, shared ground state is usually required. 
However, once ground state is established, messages that do not require
additional, cumulative state are preferred.

_Example:_ favor state over edges.  A one-shot _transition_ on a path that
can lose or reorder messages exposes a defect; carry state and let the
receiver infer the transition.

Idempotent messages can be sent one or more times without altering the
outcome.  Because communication between processes is less reliable than
direct code execution, cases can arise where messages may be lost unless
re-sent.  If the message is idempotent, the converse problem of multiple
messages is avoided.

_Example:_ name requests uniquely, don't just ask.  A request for a _memory
buffer_ that is sent twice could result in the creation of two buffers and
possibly the leak of one.  A request for _memory buffer #8_ if received
again would at most result in a second notification that the buffer is
created (or could not be).

---

## 3.  Acknowledgement upon request or specification; by default design every message able to request one unless the API explicitly declares that acknowledgement is unavailable or required

A sender can generally know in advance when it will need to be certain; the
receiver cannot guess unless the API specifies it must, must not, or may
acknowledge receipt.  For coordinated use of a shared resource,
acknowledgement is essential; for convenient notifications, acknowledgement
is overkill; but if it is not decided in advance it should be requestable.

A channel may be declared *advisory* and exempted from acknowledgement when, stated in the API up front:

- **(a)** the receiver has an independent, unconditional path to the same ground truth;
- **(b)** that path is never gated on the advisory message;
- **(c)** the content schedules work and is never used to infer state;
- **(d)** acting on it is rate-limited by the **receiver**, independently of the sender;
- **(e)** the resource it points at has a handoff discipline that makes re-checking safe.

This exemption is a property of the **channel**, declared once — never a per-message
choice, which restores the uncertainty this principle exists to resolve. The
failure mode to guard is (b): someone later observes that the advisory message
always precedes the change, concludes the independent path is redundant CPU,
removes it, and the channel becomes correctness-critical with no acknowledgement
and nobody noticing.

Note: a sender-side refusal signal can substitute for an acknowledgement,
provided that the message either is atomically passed or not (no possibility
of uncertainty whether it was or not); or if the message is idempotent (in
which case it is fine to send it again, and it should be sent again).

Atomicity should be verified per platform; behavior can vary by OS despite
superficially similar APIs.

Idempotency-with-transport-guarantee should be favored when available.  Communication is
usually reliable; this places the burden on the rare error cases rather than
the common working case.

---

## 4. Message IDs so loss can be detected where that matters

State and generation versioning is a special case: where "which version of the
state is this" matters, use the ids already present or carry a dedicated sequence.

Use state that is large enough to be inexhaustible.  Advisory messages may
use narrower counters, so long as collision after wrapping will only delay
correct behavior, not cause incorrect behavior.

Warning: if communication failure is possible, a bounded recent-ids table
cannot make a wrapping id safe for deduplication.  Only a full set of active
IDs or a handshake agreeing upon bounds can ensure that no ID that one
partner believes is in use is inadvertently repurposed by the other.

---

## 5. Explicit handoff of shared data

Shared data must have one owner at a time.  Only the owner can read or write
it, as appropriate for the API.

When you give it, it is gone until it is given back.  A mutually agreed
recovery protocol is fine; silently reclaiming it because "enough time has
probably passed" is not.

Corollary: for rapid throughput, use triple-buffering: one lane for reading,
one lane for writing, one lane for transfer (empty, from reader to writer;
full, from writer to reader).

Corollary: an advisory channel cannot transmit ownership as it may be lost. 
It may advise to look at data that has within-channel proof of ownership
(e.g. atomic sentinel values in shared memory).

---

## 6. Where an error is possible and not automatically fatal, have a recovery protocol — never silent swallowing

Recoverable failures are negotiated with the peer. Failures that cannot be
recovered at the point of detection must still reach a diagnostic surface the
developer can find.

---

## 7. Do not assume order or completeness

Unless messages cannot be lost, and errors either are impossible or abort
the entire plugin session, do not assume messages arrive in order or that
they all arrive.  Have a recovery plan if needed.

_Example:_ memory grants travel on the doorbell channel, which can fill and
refuse.  A refused grant must therefore be retried — but a retry establishes
only that the record was accepted for transmission, never that it arrived.
The plugin's acknowledgement, on the reliable channel, is what establishes
that.  If retries are exhausted the plugin is deemed unresponsive and the
session ends.

---

## 8. Be precise about expectations of time and space, and about behaviour when they are violated

Where something is nominally unbounded but bounded in practice, make the bound
crystal clear and state what happens when it is exceeded. Timeouts, queue caps,
buffer sizes and message limits alike.

Corollary: A structural bound beats a validated one.  Where a limit can be
made unrepresentable — it does not fit the field — prefer that to a constant
checked at scan time.  A structural bound cannot drift from the wire, cannot
be forgotten by a second implementation, and needs no error message.  Where
it cannot be structural, 8 applies unchanged.

---

## 9. Proportionate response to failure

Before hardening a mechanism against a failure, establish what else is
*necessarily* true when that failure occurs. If the answer includes something
worse, effort should not go to fixing the lesser failure while the greater
one is outstanding.

Apply the test to the party suffering the failure, not to the system as a
whole.  A party starved by its partner is itself healthy — nothing worse is
outstanding for it, so its exposure is worth reducing even though the greater
failure lies elsewhere.  Mechanisms should permit an otherwise
successfully-operating party to reduce their exposure to a partner exhibiting
failure, but should not compromise the ability of the party exhibiting
failure to recover.  When a tradeoff must be made, favor vizi over the
plugin: vizi is the supervisor and if it cannot cope with failures, the
plugin will be terminated also.

Failure modes should also not cascade if possible.  One remedy is allocation:
every resource shared between parties needs a per-party bound, so that no
party's behavior can consume another's share.

_Example:_ a doorbell listener shared by every plugin session would let a
plugin that floods it delay the rest — each of which is in perfect health.
vizi gives each session its own listener thread, so a flood starves only the
session that sent it (`doorbell_protocol.md` §10's fairness rule); a host built
with one shared reactor would owe a per-session read quota instead.  Sharing
can also be indirect: on Linux, file descriptors in
flight are charged against the user rather than the socket, so several
well-behaved sessions can together exhaust a budget none of them approached
alone.

Corollary: cope proportionately but never silently.  Surfacing detected
failures allows pinpointing of the root cause as well as proximate errors,
but also assists in allowing the two to be linked.  This is far more
actionable than silently running with degraded behavior.  However, be aware
that high-frequency surfacing of low-level failures, like a frequently full
advisory channel, is likely to exacerbate problems.  Communication of
failure should not be a point of failure (an instance itself of
proportionate response).

---

## 10. Assert every safety property; inherit none from unrelated sites

A property that holds because of an unrelated call site is not a contract
and does not survive a refactor.  State it at every site that depends on it,
even where that is redundant today.  This ensures that successful
collaboration is explicit not accidental.

_Example:_ a grant path that is non-blocking on Linux *only* because
a reactor registered a `dup` with `O_NONBLOCK` on the shared open file
description — clear that flag and the same code blocks ≥5 s.  Or a Windows
grant bounded at 200 ms *only* because an unrelated call site latched
`SO_SNDTIMEO` on a shared socket.  Neither is stated as a requirement, yet
the behavior is relied upon.

---

## 11. Never claim what you do not have

Absence must be expressible, or a receiver cannot distinguish a real value
from a fabricated one.  **A field that is always populated is always
believed.**

If a sentinel value is used, document it clearly.  If an encoding is used
that can express optional values, use it.

Corollary: when giving information about user actions, report information
that aligns with what they see.  A point picked on a YX MIP should not return
a Z value at all: the projection collapsed that axis, so no single plane is
what the user pointed at.  Returning the current z-plane setting is worse than
returning nothing, because the receiver cannot tell it apart from a real
coordinate.

Corollary: report what produced the answer, not what exists.  The MIP pick
above should carry the range of Z that was projected over — and if the volume
is cropped, the range that was visible, not the range that is present in the
data.

---

## 12. Coordinate when necessary but bound the depth of handshaking

An acknowledgement chain needs another round only when a party's next action
depends on what the other knows. Where the transition is irreversible and
unilateral, depth is one — in both directions.

_Example:_ a shared-memory grant cannot be revoked on any supported
platform, so the host needs to know that the plugin received it and
therefore the plugin must reply; but the plugin does not need to know that
the host heard the reply.  Either the host will use it, or the host will
request acknowledgement again; or the plugin will use it and will notify the
host who can then infer that the memory has been received.

---

## 13. One obligation, one document — and every other mention cites it at the site

**No two documents may specify the same obligation.** Exactly one is the source
of truth for it; every other mention states the obligation only by reference,
naming the owning document and section in the sentence that mentions it:

> *"As specified in `shared_memory_protocol.md` §9.1, a retiring segment permits
> finishing what was started and forbids starting anything new."*

**A rule about which document wins would be a rule someone has to remember.** The
alternative is a precedence order — the owning layer defines the
obligation, the carrying layer defines the message. That is a fine principle and
it is invisible at the point of use: a reader looking at one of two conflicting
paragraphs has no way to tell, from the paragraph, that it is the subordinate
one. Citing at the site makes authority a property of the text a reader is
already looking at, and makes a *second* normative statement visibly wrong rather
than merely unlucky.

**The defect this prevents is drift:** a decision written into one document
while the others keep asserting the state it replaced. The documents restate
each other deliberately, so that a reader arriving at any one of them can follow
it — and that redundancy is exactly what rots. A reference cannot drift from what it points at; a
paraphrase always can.

**Two things it does not forbid.** *Explaining* an obligation in a second
document is fine, and often necessary — what must not appear twice is the
normative statement of it, so an explanation cites and then elaborates.
And a document may own the **message** that carries an obligation while another
owns the obligation itself; those are different things and each gets its own
owner.

---

## 14. Divergence is a defect only where something depends on the exact behaviour

**Two conforming implementations may answer the same paragraph differently,
and that is fine as long as each answer is reasonable.**  Perfect behavioural
match across implementations is not a goal of this corpus; plugins and hosts
that behave *reasonably* is.  Exact agreement is required only where a peer,
a message or a persisted artifact **depends** on the exact behaviour — a
refusal that must be acknowledged so the sender can stop waiting, a count the
other end aggregates, a layout bytes are read back by.

**So a sentence that mandates one behaviour has to say what depends on it.**
Where nothing does, the honest sentence is *"a receiver may do X or Y; either
conforms"* — or no sentence at all, with the divergence recorded where it was
found.  A mandate written only to make four columns of a table identical costs
every implementation a change and buys nobody anything; and it is how a corpus
grows rules no one can remember the reason for (principle 13's defect, one
level up).

**The test for a reviewer holding two implementations that differ:** name the
thing that breaks if they keep differing.  If there is one, it is a finding and
the spec owes a rule.  If there is not, it is a note.

---

## Version skew

A separate axis: 1–14 govern peers that already agree on the contract; this
governs peers that do not.

**The compatibility policy is explicit and settled at connect, not
discovered at message-parse time.  When peers cannot agree, fail whole and
say so — never partially.**

vizi's model is worth recording as the shape: two gates, manifest scan and
then the `Hello`/`Ready` handshake, with a mismatched plugin failing launch
with a diagnostic naming both versions, and unknown panel widget kinds
rejected at parse time rather than rendering a partial panel.

A consequence worth stating: a "drop and count the unknown version" rule at
the record level is *partial* acceptance, and belongs only to append-safety
for unknown message **kinds** — never to versions, which the handshake has
already settled.
