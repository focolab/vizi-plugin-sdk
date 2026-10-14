# `D` — advisory notification

`D` says one thing: **look at shared memory now, don't wait for the poll.** It
carries no content. Transport rules are in `doorbell_protocol.md`; what a region
*is* belongs to the memory layer (`shared_memory_protocol.md`).

**Scope.** `D` **transmits** `(segment_id, {region indices})`. It does not
bounds-check them: the region count arrives in `InstallSegment`, which this layer
never sees, so the check and the "treat an out-of-range bit as sweep everything"
rule both belong to the memory layer (§4, `shared_memory_principles.md` A1).
Every rule that needs the mapping is the memory layer's.

**Direction: both.** Either end may hint at memory it writes (§5). **Trust:**
plugin→host is untrusted and host→plugin cannot be corrupted by the peer, but
both ends bounds-check identically — the mask is validated on its own merits, not
on who sent it (`doorbell_principles.md` B5). **Rate-limit ownership:** the
receiver's, entirely; a receiver may coalesce, defer or ignore `D` altogether.
**Ancillary descriptors: none, ever, on any platform.**

---

## 1. Payload

Offsets are from the start of the 100-byte **payload**, i.e. record byte 8.

```
[0..8)    segment_id  u64   which granted segment
[8..72)   mask        512 bits, one per region
[72..76)  epoch       u32   the pane epoch, or 0 when the segment is not
                            pane-associated
[76]      view_id     u8    which pane, when epoch != 0; else 0
[77..100)               zero padding, MUST be zero
```

**`epoch` alone says whether the pair is present.** `epoch != 0` means the
segment is pane-associated and `view_id` names the pane; `epoch == 0` means it is
not, and `view_id` carries 0 and means nothing. **`epoch == 0` is not an error
here** — it is the honest statement that no epoch applies.

**The discriminator is `epoch`, never `view_id`, because 0 is a valid pane**
(`doorbell_E_input.md` §2). A rule of the form "both non-zero or both zero" would
make every hint for pane 0 unexpressible, and — since `D` is symmetric — would do
it to the plugin's own sends as well.

A non-zero `view_id` alongside `epoch == 0` is a sender bug with no consequence:
**the FIELD is counted and ignored, and the record is delivered without a pane
association**, exactly as an out-of-range bit is ignored while the rest of the
mask is acted on. `D` still has exactly one transport-checkable content
violation (§4).

**The FIELD, never the record**, and the general rule it follows is
`shared_memory_protocol.md` §2's: **a rule about a hint must not fail the data it
annotates.** A `D` record *is* the hint, so dropping the whole record over a
malformed pane field discards a check the receiver was about to schedule in
order to punish a field that only says *which* epoch it belonged to. The analogy
is to an out-of-range bit and not to an unknown `segment_id` — where there is
genuinely nothing to look at: here the mask is still good.

**It is the field that is ignored and not the record, and the distinction is
worth stating because "ignored" reads both ways.** Everything the hint is for
survives: the `segment_id` names a real segment and the mask names real regions,
so dropping the whole record would cost a scan the receiver still needs while
losing nothing that was wrong. That is also what makes "no consequence" true — a
dropped hint has one, since the receiver falls back to its unconditional
re-check schedule and the acceleration is gone (§5). An unknown `segment_id` is
the case that *does* leave nothing to look at, and it is not the analogy for
this row.

**A hint's `epoch` is a SCHEDULING input, and never evidence about the bytes.**
That distinction has to be made because the obvious justification for ignoring a
mismatched hint is not available: `shared_memory_protocol.md` §2 says a segment's
`(view_id, epoch)` pair is declared per PULSED slot (`epoch_offset`, §3.2) and
belongs to one publication, not to the segment, so a hint -- which names no
publication -- cannot be used to conclude anything about what the mapping now
holds. An old hint does not mean old contents — a newer publication may have
landed while the hint was in flight — and a matching hint proves nothing either.
What a receiver decides here is only whether to bring a scan forward.

Four cases, against the epoch this receiver acknowledged for the pane
(`doorbell_E_input.md` §1a), which is the only pair it has:

| the hint's `epoch` | the receiver |
|---|---|
| **absent** (`0`) | schedules normally. The segment is not pane-associated and there is nothing to rule against |
| **equal** | schedules normally. The ordinary case |
| **older** | ignores and counts. Not because the scan is provably wasted — it is not, and the paragraph above says why — but because the hint is asking for the most expensive thing `D` can ask for on the weakest evidence it can offer, and (a) reaches the region anyway within its bound. The cost of ignoring it is latency |
| **newer** | ignores and counts. The receiver cannot read a region under an epoch it has not installed, and the epoch it is waiting for will arrive on the control plane with a hint of its own |

**Neither mismatch is a `ContractViolation`.** That is `doorbell_E_input.md`
§1a's answer for `E`, which carries state that must be interpreted; `D` carries
only a schedule, and a hint that arrives a moment early or a moment late is an
ordinary consequence of two channels with no ordering between them.

**What makes ignoring safe is (a), not the payload.** The unconditional re-check
schedule (§5) reaches every actively consumed region within its bound whatever
any hint said, so the cost of ignoring one is latency and never correctness —
which is the same property that makes `D` advisory at all. The scan is not free
either: §2 measures it at ~25 µs cold for 256 regions, on the Qt thread, per wake.

**SHOULD rather than MUST because no peer can observe the choice** — it is a
receiver spending or saving its own time, like coalescing (§1).

Whether a segment is pane-associated is not a judgement call at this layer: if
vizi is unsure whether data belongs to a pane, something upstream is already
wrong. And a plugin telling the host something it learned from a pane
interaction, slightly out of date, is fine — **if the answer needed the sync to
be current, the sync had already failed**, and no field here would have rescued
it.

**Bit order.** Region `r` is bit `r & 7` of payload byte `8 + (r >> 3)` —
LSB-first within each byte. Equivalently, read `[8..72)` as eight little-endian
`u64` words and test bit `r & 63` of word `r >> 6`. Both formulations are given
because this is the single easiest thing in the protocol to implement backwards,
and a wrong bit order is invisible under the lower-bound rule below: it degrades
to occasional latency rather than to an error anyone would notice.

Region indices are 0-based and dense (`doorbell_M_memory.md` §6), so bit position
*is* the region index with no translation.

**The mask is a lower bound on what changed, never the set of what changed.** A
set bit means "definitely look here"; a clear bit means "no hint", not
"unchanged".

That rule covers four cases with no special handling: more regions than the mask
spans, a wrong bit, a coalesced window that lost track, and a dropped `D`. All
degrade to "the poll catches it", which is the same property that makes `D`
advisory at all.

**An out-of-range bit is the memory layer's, and §3's table gives its
disposition** — since deciding it needs the region count, which this layer never
sees. It is named here only as one of the four absorbed cases.

**Coalescing is a bitwise OR** — drop-safe, duplicate-safe, order-safe. Fan-out
collapses: sixty-four regions updating in one window is one `D` with sixty-four
bits set.

**A sender SHOULD suppress an all-zero mask**; a receiver counts it and ignores
it (§4). Nothing depends on the sender half — the mask is a lower bound by
construction, so an all-zero one says only what the poll already covers, which is
why this is a recommendation rather than a rule with a consumer.

**Coalescing is the sender's option and its window is the sender's**; nothing
requires a sender to coalesce, and a sender that emits one `D` per change is
correct if wasteful. That is separate from condition (d) below, which is about
the **receiver** limiting how often it *acts* — the two are independent, and only
the second is a requirement.

512 bits is what the record holds comfortably (100 payload bytes less 8 for
`segment_id`), not a prediction about region counts. `segment_id` is required
because a region index means nothing without the segment it indexes.

---

## 2. Why it points

A bare wake would force a full sweep on every wake, and a full sweep is expensive
in proportion to the region count — so the mechanism would cost most exactly when
wakes are most frequent.

The cost is cache and TLB misses, not instructions. With payload buffers following
each header the stride at the default 64 KiB slot is `48 + 3 × 65536 ≈ 192 KiB`,
so every region header lands on its own page: ~25 µs cold for 256 regions, on the
Qt thread, per wake.

The mask makes a wake O(changed). Whether the layout also bounds a *full* sweep
is the memory layer's problem, and a real one — the poll is mandatory (condition
(a) below) and can never be skipped. `D` does not solve it and does not depend on
it being solved.

---

## 3. The advisory exemption

`D` carries no acknowledgement. That is permitted only while all five conditions
hold **and are stated in the API**:

- **(a)** the receiver has an independent, unconditional path to the same ground truth;
- **(b)** that path is never gated on `D`;
- **(c)** the content schedules work and is never used to infer state;
- **(d)** acting on it is rate-limited by the **receiver**, independently of the sender;
- **(e)** the region it names has a discipline that makes re-checking safe.

**(e) is the one that is easy to miss:** an advisory channel is exactly as safe as
the target it points at, and `D` never carries the handoff itself. Because
`InstallSegment` puts `discipline` on the wire, (e) is *checkable*:

**This is the one disposition table**, and §1 and §4 defer to it rather than
restating it. Every row is what a *receiver* does with a bit; a sender MUST NOT
set any bit whose row is not the first.

| the bit names | the receiver |
|---|---|
| a **Pulsed** region the **peer** produces | **schedules a check**, at a rate it owns (d). This is what `D` is for |
| a **Pulsed** region the **receiver itself** produces | counts and ignores. `D` is symmetric, so each direction's hint is about the *other* end's lane; a hint about one's own names a value nobody else wrote, and claiming it would consume the receiver's own published value |
| a **Loaned** region | counts and ignores. Re-checking is safe, but **there is nothing to accelerate**: a ring carries no in-band new-deposit marker, and a peer learns of a deposit from the gRPC message that names it |
| a **Constant** region | counts and ignores, for the simpler reason that there is never anything new to find |
| an **Owned** region | reports `OWNED_REGION_HINT` and ignores the hint (`shared_memory_protocol.md` §5). Its read *is* the handoff, so re-checking is unsafe. **Reporting it never quarantines the region** — the hint was wrong, the memory was not |
| a **Staged** region | **schedules a check of its turn word**, at a rate it owns (d). Reading the turn word claims nothing, so re-checking is safe; the slot is acted on only when the turn names the receiver (`processing_stages.md` §6.2) |
| a region the receiver has **not been granted** | **sweeps** — see below. The bit itself is counted and ignored, since it names nothing here |

**"Safe to re-check" is necessary and not sufficient**, which is why the table is
about dispositions rather than about condition (e). Loaned and Constant both pass
(e) and both earn nothing; Loaned fails on having no consumer and Constant on
having no changes. Keeping those distinct is not pedantry: if a ring ever gained
an in-band marker, its row would change and Constant's would not.

**A bit past the last granted region means "sweep", and one stray bit never
discards the rest of the mask.** The valid bits are acted on exactly as their
rows say; the out-of-range bit additionally schedules **every region this table's
first row would have permitted** — the peer-produced Pulsed ones, and nothing
else. It does not sweep Constant, Loaned or Owned regions, because a bit could
not legitimately have named them either.

That is §4's rule, and it is **bounded recovery from a mask the receiver cannot
reconcile with its own table** — not an inference about why. There is no benign
cause to infer: a region table is fixed for the life of its `segment_id` and ids
are never reused (`shared_memory_protocol.md` §3, §8), so a newer table always
arrives under a different identity and a different `D`. A disagreement here means
one side is wrong and the receiver cannot tell which, so it cannot fully trust its
own mapping of the *valid* bits either.

Sweeping is the cheap answer to that: it costs one early scan the receiver was
going to make anyway, bounded by (d) and by (a)'s unconditional schedule, where
ignoring risks missing a change whenever the receiver's table is the wrong one.

**So `D` accelerates Pulsed regions and nothing else.** Loaned passes condition
(e) and fails on having no consumer, which is a different reason and worth
keeping distinct: if a ring ever gained an in-band marker, the bit would become
useful without any rule here changing.

**The check is the memory layer's, not this one's.** A `D` bit naming an Owned
region is an error at that layer, reported and ignored
(`shared_memory_protocol.md` §5); the doorbell has no way to know a discipline
and must not pretend to.

The exemption is a property of the channel, declared once — never a per-message
choice. What it obliges is per *receiver*: the channel is exempt, and each end
that acts on `D` owes (a)–(e) for the regions it consumes. §5 is where that bites.

**`D` is what permits the poll to be infrequent**, not the other way round. The
sweep is cheap enough that a fast poll would work, but a 125 Hz `QTimer` competes
with rendering on the Qt thread and burns CPU while nothing happens. `D` buys
microsecond latency on change and near-zero cost at idle; the 40 ms interval is a
consequence of that.

**How the reference host acts on a plugin's `D`.** The transport folds
hints per segment on the socket thread, as §4 describes, and writes one byte to
a host-wide wake endpoint when a session's pending set goes from empty to
non-empty — a loopback socket pair, so the Qt thread waits on it with a
`QSocketNotifier` on every platform. The notifier drains the wake, takes the
hints (**hints only**: the tick's fold of violations and diagnostics, whose
report rate the 40 ms interval bounds, is not advanced by acting on a hint) and
scans the named session's live lanes — at most one wake-driven scan per session
per frame (16 ms); a wake inside that window is deferred to its end, never
dropped, and later wakes fold into the deferral. **While every live session is
inside that window the host leaves the hints PENDING rather than taking them**,
which is what bounds the wake rate: taking them empties the transport's set, so
the next `D` finds it empty again and writes another byte. The deferred
scan takes the held hints when it runs, so the path re-arms for the next value
rather than waiting for the 40 ms tick. The scan is scheduled by
*segment*; the folded hint's pane is not consulted, because each slot's own
`(view_id, epoch)` pair is what the scan validates (§2). A hint naming a segment
the session was never granted is refused before it takes a pending slot, so a
stranger's ids cannot crowd out a real lane. The 40 ms sweep stays as the
independent bound (§5).

---

## 4. Bounds, trust and rate

The region count is fixed at grant time by the fit check in
`shared_memory_protocol.md` §2, `32 + Σ region.extent ≤ size`, where the 32 is
the preamble and nothing else — there is no in-mapping directory to account for
(`shared_memory_principles.md` E3). In practice a segment carries far fewer
regions than the mask can name: `shared_memory_protocol.md` §3 allows at most
one plugin-produced PULSED region and one plugin-produced LOANED region per
segment, and an overlay's segment is a lane and an arena. The mask's width is
a bound on the wire, not a constraint anyone meets.

**Bounds-check the mask in both directions.** Plugin→host is untrusted by
construction; host→plugin is trusted against corruption but not against vizi
bugs, and the check is a comparison against a number the receiver already holds.
A bit past the granted region count is **never an error**; §3's table says what
it does instead, and the bounds check is a comparison against a number the
receiver already holds. A payloadless flood is bounded by (d) and by the
supervisor's pending caps — `PENDING_HINT_SEGMENTS`, `PENDING_REPORTS` and
`OUTCOMES_PER_POLL`, each 256, in `control_plane_protocol.md` §9's table with
what happens past them: a hint for a 257th distinct segment between two drains
is dropped and counted, and the count reaches the host as one diagnostic naming
the cap.

An unknown `segment_id` is likewise ignored, not faulted: a `D` racing a
`RetireSegment` is an ordinary occurrence, and the hint for a segment that no
longer exists is exactly the kind of loss the lower-bound rule already absorbs.

**`segment_id == 0` is the exception the transport can catch on its own.** It is
the protocol-wide "no segment" sentinel (`doorbell_M_memory.md` §3), so rejecting
it needs no mapping and no region table — unlike a bit index, which does.

`D` has exactly **one** transport-checkable content violation: `segment_id == 0`,
reported as `ContractViolation` with `where = zero_id`, the record dropped and
nothing cancelled — there is no `D` state to cancel.

**An all-zero mask is not one — it is counted locally and ignored**, as an
out-of-range bit and an unknown `segment_id` are. **It differs from the
out-of-range bit in what follows**: §3's fallback sweeps because that bit names a
region this receiver has not been granted, and an empty mask names no region at
all, so there is nothing to fall back to and it schedules nothing.

All three are sender bugs, and
the "it is a sender bug" argument does not distinguish them; what does is that
this is the one op whose entire design says a wrong, absent, stale or dropped
hint is absorbed by the mandatory poll.

**Both directions report the same way, because `ContractViolation` is
symmetric** (`control_plane_protocol.md` §8.13; `HostMsg.contract_violation`
is the host's arm). A plugin receiving a bad host→plugin `D` reports it there,
like any other semantic disagreement; the host receiving a bad plugin→host `D`
reports it there too — `zero_id` for a claim bit with no id, the one
transport-checkable content violation §4 names — through the same rate-bounded
reporter as every other site. What the host does NOT report is what has no
site: a hint past the pending cap or naming an ungranted segment is counted and
surfaces as a Plugin Manager diagnostic (`control_plane_protocol.md` §9's
table, §12).

---

## 5. Direction

**Both.** `D` is the one symmetric opcode: it hints at shared memory, and both
ends write shared memory. Host→plugin `D` is the pump for a Pulsed region the
plugin reads — triple-buffered geometry, a frame/dataset/ROI change — and
plugin→host `D` is the pump for one the plugin writes.

**A plugin is free to ignore `D` entirely.** That is conditions (b) and (d)
exercised, not an exemption from them: the receiver owns the rate, and the
independent path is what the hint accelerates rather than replaces.

**But "free to ignore" is not "free to depend on".** The five conditions of §3
bind the **receiver**, not the direction, and (a) is the one a plugin can fail by
omission: a plugin that reads a Pulsed region *only* when a `D` arrives has made
`D` load-bearing, and will sit on a stale last value forever the moment a writer
stops — because there is no further `D` coming, and nothing is wrong. So the API
says it plainly: **keep your own read schedule; `D` only makes it earlier.** The
SDK provides one for every region it consumes on the plugin's behalf.

> **Every actively consumed Pulsed region is re-checked within a documented
> finite bound, independently of `D` and of any other traffic.**

The schedule's mechanism and duration are the SDK's — a timer, the gRPC dispatch
loop, a per-region lazy check — but **a bound has to exist**. A receiver that
re-checks only when unrelated activity happens to wake it satisfies a loose
reading of "keep your own schedule" and then stays stale forever after a dropped
`D` in a quiet session, which is exactly what makes the advisory channel
correctness-critical. vizi's bound is the 40 ms pump interval (§3).

### 5a. What that obliges an SDK

The sentence above is a requirement on the **SDK**, not advice to the plugin
author: an API that exposes a read and leaves the schedule to the caller has
moved a correctness obligation onto the one party with no way to know it exists.
So:

- **"Actively consumed" is a region the application has asked this SDK to
  deliver values from**, and nothing wider. It begins at whatever call registers
  that interest and ends at the matching one, at quarantine (§6.2), or at
  retirement — an SDK that starts polling every Pulsed region in every grant is
  doing work no one asked for, and one that polls none until a hint arrives has
  built the stall this section names;
- **the SDK provides the scheduling path itself.** A documented interval and a
  documented way to drive it — its own thread, or a `sweep()` the application's
  loop calls — and if the application never drives it, that is a fact the SDK
  states rather than a silent stall;
- **a hint may only bring a check FORWARD.** Receiving one, ignoring one, or
  receiving none can never postpone the next scheduled check. This is (b) stated
  as an API property: the moment a hint can delay a poll, the poll is gated on
  `D` and (a) is gone;
- **delivery may not block the schedule indefinitely.** A slow or wedged
  application callback must not become an unbounded pause on every other region,
  and an SDK says what it does instead — drop, queue with a bound, or stop and
  report — because all three are defensible and silence is not;
- **quarantine and retirement remove a region from the schedule** rather than
  making it raise forever, and the SDK says which regions it dropped and why;
- **each SDK publishes its interval, its delivery policy and its coalescing
  policy.** They are implementation choices and they are not private ones: a
  plugin author sizing a buffer or a latency budget is choosing against them.

None of that fixes an interval here. What it fixes is that two SDKs can differ
in every one of these and a plugin author can still find out which they got.

That is the same failure §3 names as the one to guard, seen from the other end.
The host's version is someone deleting the poll as redundant CPU; the plugin's
version is never writing one.

Both directions share the record and differ in exactly two ways, per
`doorbell_protocol.md` §2: **trust** and **rate-limit ownership**.

---

## 6. Scope: what `D` can and cannot accelerate

**`D` accelerates only state that is already durable in shared memory**, because it
carries nothing. Once content is in memory the reliable follow-up is **the poll**;
once it is not, `D` cannot carry it. So "fast lossy hint plus reliable gRPC copy" is
either already the design or cannot work.

**To make a message faster, move its content into shared memory** — do not send a
`D` alongside it. `OverlayRemove` is the live candidate: it is a one-shot
*transition*, and a plugin retracting an overlay because it has *finished* will
never send another. It rides an undroppable queue
(`control_plane_protocol.md` §7), which removes the loss but not the reason —
as region state it would be loss-proof, `D`-accelerated and poll-backstopped,
and that is a stronger property than a queue that never drops.

---

## 7. Rejected alternatives

| Alternative | Why not |
|---|---|
| A bare wake with no payload | Forces an O(regions) sweep per wake, most expensive exactly when wakes are frequent |
| A single region index instead of a mask | A window touching five regions would need five records, losing the fan-out collapse |
| A `D`-specific drop counter | The header `id` already gives aggregate drop detection; `D` is disposable, so per-op attribution has no consumer |
| Treating the mask as an exact set | A drop, a wrong bit or an overflow would then be a correctness failure instead of a latency one |
| A flow-control hint | Backpressure is already the mechanism |
| A heartbeat | Proves the sender works, not the listener — and the listener is what dies |
| A priority bit | A plugin telling the host how to schedule; condition (d) owns that |
| A duplicate gRPC copy of the same fact | Redundant with the poll, and reintroduces dual-path ambiguity |
| Making `D` plugin→host only | The plugin needs the same pump for a Pulsed region it reads; the conditions bind the receiver, not the direction |
| Faulting on an unknown `segment_id` or an out-of-range bit | A `D` racing a retirement is ordinary, and the lower-bound rule already absorbs it |

---

## 8. Open

- **Dynamic occupancy.** Regions are fixed at grant time, so per-object publishing
  wants over-provisioned regions whose occupancy varies. That design should
  determine how many regions a manifest may declare; 512 is a ceiling that fits the
  record, not a prediction.
- Whether a region's current occupant must be discoverable from its header once
  occupancy is dynamic, so a reader finding an unexpected occupant treats the region
  as changed rather than as an error.
- Whether the SDK's unconditional scan loop is a timer, a wait on the gRPC
  dispatch loop, or per-region and lazy. §5 requires that one exist; it does not
  say which.
