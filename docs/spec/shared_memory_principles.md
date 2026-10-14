# Shared memory design principles

The framework the shared-memory documents are consistent with. Details without
it drift; this is what to check a change against.

**Parent set.** The general principles — ownership, handoff, bounds, recovery,
truthfulness — live in `plugin_principles.md`. This layer is where principle 5
("explicit handoff of shared data") is cashed out into byte-level protocols, so
most of what follows is that principle meeting a platform that cannot revoke.

Specifications: `shared_memory_protocol.md` (normative),
`shared_memory_implementation_details.md` (what ships, and how it differs). The
doorbell is a *separate* layer that hands this one a capability and a hint;
`doorbell_M_memory.md` and `doorbell_D_notification.md` are its half.

---

## A. The layer boundary

**A1. The doorbell transmits; this layer interprets.** The doorbell says "here
is memory" and "something changed at indices 1, 2, 7, 19". It does not know what
a region is for, what discipline it has, or what its bytes mean, and it must not
grow rules it cannot enforce. A `D` bit naming a region that should not receive
hints is this layer's error to detect, not the transport's.

The practical test: if a rule needs the mapping to check, it is not a doorbell
rule.

**A2. Meaning arrives over the control plane.** A grant is a capability; what the
memory *is* travels as `InstallSegment` (`shared_memory_protocol.md` §3). A
fixed 100-byte doorbell payload cannot grow fields and meaning always does.

---

## B. What cannot be taken back

**B1. Memory cannot be withdrawn on any supported platform.** Once the peer has
mapped, both parties have access until it unmaps or exits. Linux seals against
the *plugin* shrinking the object; that is containment, not a lever for the host.

Three consequences shape everything:

- **Retirement is a request, never an enforcement.** No host-side code may
  assume compliance.
- **The budget is per session, not per grant.** Reclamation happens at unmap or
  process exit.
- **A mis-mapped grant is an invisible allocation** — the peer holds pages the
  host does not know exist.

**B2. A timeout whose only remedy is revocation must report, not act.** "Plugin X
has held segment Y for N minutes after retirement" is a diagnostic line. There is
nothing else it could truthfully do.

---

## C. Ownership

**C1. Ownership is claimed by compare-exchange, never inferred from a write
order.** A seqlock's correctness rests on a documented ordering rule — the
generation word written strictly last — which is invisible in the code that has
to obey it, unenforceable across a language boundary, and silently wrong when
someone reorders two stores. A CAS makes the claim an object: it either
succeeded or it did not, and a failed release proves someone else touched the
word.

This is why the protocol is not a two-slot seqlock. The CAS protocol is harder
to read and impossible to get subtly wrong.

**C2. A failed release CAS is proven interference, and is quarantined rather
than repaired.** If a release does not find the exact value it published, the
other party violated ownership. Clearing the word anyway would destroy whatever
they wrote on top of a bug that is already established. Report once, isolate the
region, and let every other region keep working.

**C3. A writer that cannot claim coalesces; it never stalls the reader.** A
bounded number of attempts, then drop the update. The value being dropped is the
one that would have been superseded anyway, and the newest published value stays
durable — so dropping degrades latency, never correctness.

**C4. One reader per handoff.** A claim bit is a mutex, not a reference count: a
second reader's CAS simply fails. Single-writer/multi-reader is a *different*
protocol shape — validate-after-copy, or a fixed pool of permits — and must be
designed as one rather than reached by changing what a release stores.

---

## D. State versus edges

**D1. A region carries state; the notification carries nothing.** `D` is a hint
that a region changed, and it can be dropped, duplicated or wrong. Everything a
receiver needs is in the memory (`doorbell_principles.md` F2).

**D2. Except where the handoff *is* the transfer.** A consume-once discipline is
a queue slot, not a register, and the rule that makes it safe is
`plugin_principles.md` 5 verbatim: when you give it, it is gone until it is
given back. Pulsed is this. Saying so plainly is better than a table column that
can be read as "the value persists".

**D3. Discipline is a property of a region, not of a grant.** One segment
routinely mixes them — drag-rate geometry beside a bulk area — so lifetime,
ownership and retirement stay whole-segment while the interior varies.

---

## E. Legibility

**E1. Prefer an invariant a dump can check.** "A READY buffer holds a value
nobody has consumed" is worth more than a marginal convenience, because it is
visible in a memory dump and answers "is the reader keeping up?" without
instrumentation.

**E2. Impossible states are named and detected, not assumed away.** A state word
that cannot occur — a claim flag on revision zero, both claim flags at once —
means the region is corrupt or a peer is broken. Enumerate them; a receiver that
only handles the states it expects silently does something arbitrary with the
rest.

**E3. Structural over probabilistic.** A layout is declared by `InstallSegment`,
never sniffed from magic bytes. This is why a mapping carries no directory of
its own: it would say the same thing as the region table, and two sources of one
truth is one too many.

**E4. Absence is expressible.** `segment_id == 0` is never a valid grant, so it
is a free "no segment". A length of zero is never a valid published value, so it
is detectable corruption rather than an empty update.
