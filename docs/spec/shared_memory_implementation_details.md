# Shared memory implementation details

What the reference host and SDK implement, where an implementation makes its
own choices, and the gRPC surface this layer needs.

---

## 1. What ships

The Pulsed protocol in `shared_memory_protocol.md` §6 runs parametrised by a
region row: the state-word encoding, the never-claim-the-newest rule, the 8-try
coalesce, the impossible-state quarantine and the exact-CAS release. The host is
the reader of a plugin-produced region and `vizi_plugin_sdk/memory.py` is the
plugin's writer.

The host's Loaned allocator is a ring: 64-byte alignment, an `(offset, length)`
release key, a FIFO tail advance and capacity doubling.

### Where the shipping code differs from the specification

**It does not, in the areas this layer specifies.** Region discovery is
`InstallSegment`'s region table; a region is named by its dense 0-based
`Region.index`; `ReleaseDeposit` carries `{segment_id, region, offset, len}`;
and any region a segment declares may be Pulsed. Nothing in the mapping repeats
the region table (`shared_memory_principles.md` E3), so a mapping does not
self-describe beyond its preamble.

**Each purpose has one region table**: one `O` per overlay with a Pulsed lane at
region 0 beside a Loaned arena at region 1, one `P` per panel, and a single
Loaned region for `G`/`V`/`X`. Each Pulsed slot's last eight bytes are the pane
header of `shared_memory_protocol.md` §3.2.

A read claim is **not** released back to READY to make a value persist: the
claim bit is a mutex rather than a reference count, so that buys serialized
readers rather than concurrent ones, and it costs the invariant that a READY
buffer is a value nobody has consumed (`shared_memory_principles.md` E1, C4).

---

## 2. The gRPC surface

`proto/vizi_plugin.proto` carries all of this. **The host** sends
`InstallSegment`, compares the identity proof on `GrantAck`, runs §9's
missing-acknowledgement query and applies `GrantState`'s five answers. **The
plugin end** is each SDK's, and the documents are its contract: the IDL and
`shared_memory_protocol.md` §3, §8 and §9a are enough to implement it.

Each message's direction, class, delivery rules and deadline are specified in
`control_plane_protocol.md` §8.12; what follows is this layer's list of what it
depends on, plus `ContractViolation`'s shape, which is defined here because its
rules are this layer's.

| Message | Direction | Notes |
|---|---|---|
| `InstallSegment` | host→plugin | its `Region` carries `producer` (§3.1) |
| `RetireSegment` | host→plugin | |
| `SegmentReleased` | plugin→host | |
| `ReleaseRequest` | plugin→host | §9.6, a hint the host owes nothing |
| `SegmentShortfall` | plugin→host | coalescing; names the amount short |
| grant acknowledgement (success / failure) | plugin→host | |
| grant-state query | host→plugin | |
| `ContractViolation` | **both** | symmetric; see below |
| `ReleaseDeposit` | plugin→host | carries `region` |
| `ReleaseLoan` | host→plugin | the mirror of `ReleaseDeposit` (`control_plane_protocol.md` §5.6) |
| `SegmentRef` | plugin→host | |

`ReleaseDeposit`'s `region` defaults to 0, which is the one-ring-per-segment
case. `offset` and `len` are literally the allocator's key: a release looks up
the reservation recorded under `(offset, length)`.

**`ReleaseLoan` is the same message in the other direction**, keyed the same
way into the plugin-side allocator (`LoanedArena` in
`vizi_plugin_sdk/memory.py`).

### `ContractViolation`

Several rules across both layers say a peer "reports it over gRPC" and keeps the
session. This is the message they mean.

```
ContractViolation {
  where          // which rule, as a stable enum
  detail         // human-readable, for the Plugin Manager
  record         // the offending 112-byte doorbell record, when there was one
  count          // occurrences of this `where` since the last report, >= 1
}
```

**It is rate-bounded, and that is a requirement rather than an optimization.**
Its highest-rate producers are decided *per record*, ahead of any interaction
state: `doorbell_E_input.md` §2 settles the epoch and tool status before the
interaction lookup, so an ordering bug — a pane whose
epoch was never installed, a tool never registered — is re-decided on every
inbound record. At mouse rate that is ~1 kHz of control-plane messages each
carrying a 112-byte record, **on the channel the session needs in order to
recover**, which is exactly what `plugin_principles.md` 9's corollary forbids:
communication of failure must not itself become a point of failure.

> **First occurrence of a `where` carries the record and `count = 1`. After
> that, occurrences are counted per `where` and re-reported on a bounded
> interval, under a per-session report budget that a change of `where` does not
> reset.**

**The budget is what makes it a bound.** Re-reporting "whenever `where` changes"
alone is bypassed by two violations alternating — each record changes the
category, so each record reports, and the 1 kHz cascade returns intact.

**It is a RATE and not a lifetime total.** The harm being bounded is a rate — communication of failure must
not become a point of failure — and a lifetime cap buys nothing against it while
costing something real: a session that spent its cap in the first minute goes
permanently silent about a category discovered in the fiftieth, which is a
developer-findable surface deleting itself (`plugin_principles.md` 6). So the
budget replenishes with time, a change of `where` never resets it, and there is
no total after which a session stops reporting.

**A deferred report goes out on its own.** The rate delays a report; it does
not make one wait for further traffic. A report the interval or the budget held
back is sent within about one interval **without further input** — a sender
that only drains its pending reports when a message arrives loses the last one
on a session that then goes quiet, and the receiver cannot tell that loss from
a delay.

**An occurrence is one `(received record, where)` pair.** `count` is "the number
of records affected", and one record can break the same rule several times — a
single `D` naming three `OWNED` regions is one `owned_region_hint` occurrence,
not three, because one record was affected. The rules that follow from that, and
each of them settles a case an implementation otherwise guesses:

- **two different `where` values on one record count once for each** — they are
  different rules and the host acts on them separately;
- **two receptions of identical bytes count as two.** Do not deduplicate by the
  record's content or by its `id`: a retry that genuinely arrived twice affected
  two records, and §6's dedup is a different mechanism at a different layer;
- **a violation with no record counts once per detection.** Runtime memory
  violations — an impossible state word, an invalid published length, a failed
  release CAS — have no doorbell record to be an occurrence of, so the detection
  is the occurrence;
- **a detector that finds several regions on one record must report them as one
  batch**, or it cannot count correctly. That is a shape requirement on the
  detector/reporter boundary, not just on the counter.

Region detail is worth keeping and does not belong in `count`: put it in
`detail`, which is for the developer, and leave the number meaning what the host
is told it means.

**`M` with `zero_id` is exempt from aggregation**, and reports once per affected
grant. The host needs the raw record to identify *which* outstanding grant it
must close out (`doorbell_M_memory.md` §7), and a count cannot name two of them —
the second would stay pending, enter missing-ack recovery, and escalate the
session, which is the cascade the special handling exists to prevent. `M` is
session-rate, so the exemption cannot recreate the flood.

**The host MUST NOT read a repeat as a new defect** — `count` is the number of
records affected, not the number of faults. The local-diagnostic cases are
already aggregated for this reason (`doorbell_E_input.md` §2, "aggregated rather
than logged per record at 1 kHz"); this is the same rule for the reported ones.

**Both directions carry it.** Three of the sites below — a `D` bit naming
an Owned region, an impossible Pulsed state word, a failed release CAS — are
equally detectable in a *plugin's* traffic as in a host's, and the rate bound,
the aggregation rule and the "a repeat is not a new defect" rule apply
identically whichever end is reporting. `ViolationSite`'s `E`-record members are
plugin→host in practice, because `E` flows one way.

It is **not** `DoorbellFault`. `DoorbellFault` is sent immediately before closing
the doorbell and ends the session; `ContractViolation` explicitly does not, and
the distinction is `doorbell_protocol.md` §8's — framing faults tear down,
semantic disagreements report and drop. Its sites:

- an `E` record disagreeing with its open interaction
  (`doorbell_E_input.md` §2);
- a `D` bit naming an Owned region (`shared_memory_protocol.md` §5);
- an impossible Pulsed state word, or an invalid published length (§6);
- a failed release CAS — proven interference;
- a release naming no outstanding reservation, or naming the wrong side
  (`shared_memory_protocol.md` §3.1).

### The `where` values

**`where` is "which rule", the host switches on it, and it is a wire enum** --
`ViolationSite` in `proto/vizi_plugin.proto`. The values are therefore
not an SDK's to spell: **the enum name is normative and the prose name is
prose.**

| rule | `ViolationSite` | layer |
|---|---|---|
| a record under an uninstalled epoch (`doorbell_E_input.md` §2 `epoch_mismatch`) | `EPOCH_MISMATCH` | doorbell `E` |
| an opening record reusing a live id with different bytes (§2 `interaction_mismatch`) | `INTERACTION_MISMATCH` | doorbell `E` |
| a record referencing something unacknowledged | `MISSING_ARRANGEMENT` | control plane |
| an identity field that must not be zero (§2 `zero_id`), **and `M` with `segment_id == 0`** (`doorbell_M_memory.md` §7) | `ZERO_ID` | doorbell `E`, `M` |
| `MISS` with a kind that carries `PICKED` (§2 `miss_with_picked`) | `MISS_WITH_PICKED` | doorbell `E` |
| a record naming a tool that was never activated (§2 `unknown_tool`) | `UNKNOWN_TOOL` | doorbell `E` |
| a `D` bit naming an `OWNED` region, whose read *is* the handoff (`shared_memory_protocol.md` §5) | `OWNED_REGION_HINT` | memory |
| revision 0 with a claim flag, or both claim flags at once (§6.2) | `IMPOSSIBLE_STATE_WORD` | memory |
| a claimed buffer's `length` outside `0 < length <= slot_bytes`, or running past the segment (§6.5 step 4) | `INVALID_PUBLISHED_LENGTH` | memory |
| an exact-CAS release that found a foreign value -- proven interference (§6.4 step 7, §6.5 step 6) | `RELEASE_CAS_FAILED` | memory |
| a release naming the wrong **side** (§3.1) | `WRONG_PRODUCER_RELEASE` | memory |
| a release naming no outstanding reservation, in either direction (§3.1) | `UNKNOWN_RELEASE` | memory |
| `seq != 0` on an opening record (`doorbell_E_input.md` §2 `seq_on_open`) | `SEQ_ON_OPEN` | doorbell `E` |
| a non-zero tail on a position payload (§2 `position_tail`) | `POSITION_TAIL` | doorbell `E` |
| a non-finite coordinate (§2 `nonfinite_position`) | `NONFINITE_POSITION` | doorbell `E` |
| a target combination §2 forbids (§2 `invalid_target`) | `INVALID_TARGET` | doorbell `E` |

**The enum is authoritative and the prose name is not.** A `where` in these
documents is written lowercase for readability — `owned_region_hint` — and an SDK
may spell its own local diagnostic however it likes, but what goes on the wire is
`ViolationSite` and nothing else. Two SDKs whose log lines differ are fine; two
whose enum values differ are not, and a `where` no `ViolationSite` value names is
a rule that needs adding here and in the IDL together.

Grant failures are **not** `ContractViolation`; they ride the grant
acknowledgement, which is the message whose whole purpose is to say whether the
install worked. **The one exception is `M` with `segment_id == 0`**, which names
no grant and so has no acknowledgement to ride (`doorbell_M_memory.md` §7).

---

## 2a. What an implementation decides for itself

`vizi_plugin_sdk/memory.py` implements `shared_memory_protocol.md`'s plugin
side — the two-halves grant, the per-segment state machine, all four
disciplines, and §9's retirement. These are the points the specification leaves
to an implementation, and what the reference implementation does at each:

- **A held WRITE claim is outside §9.1's reach.** The rule says complete and
  release a held Pulsed claim before releasing the segment, and the reference
  implementation can only see a **READ** claim — because in it a writer's claim
  never outlives one `publish()` call, so there is nothing for a retirement to
  observe. An SDK that hands the application a claim *object* has a longer
  window and a real case here. Whether a claim can be "held" here depends on
  the SDK's API shape, not on the protocol.
- **The claim check needs atomics, and they are optional.** Reading a state word
  to answer "is a claim held?" requires the same process-shared atomics as
  Pulsed itself. An SDK without that backend — for a plugin using only
  `CONSTANT` and `LOANED` regions, which is the common case — cannot answer, and
  the reference implementation answers *no* rather than blocking a release it
  cannot reason about. An SDK that blocks instead also conforms.
- **The `retryable` errno set is named by category, not by value.** §7's closed
  list is *"out of address space, out of handles, or out of memory"*, which is
  the right level for a cross-platform document; turning it into a set costs a
  judgement per platform, and the reference implementation's is `ENOMEM`,
  `EMFILE`, `ENFILE`, `EAGAIN` plus Python's `MemoryError`. Two SDKs will
  differ at the edges, and the consequence is bounded — the catch-all is
  permanent, so a disagreement costs a retry that would have worked, never a
  retry loop that should not have started.
- **The recognized `purpose` set is the receiver's own.** `purpose` is a
  `string` in the IDL and is **host-defined** (`shared_memory_protocol.md` §3):
  a receiver's recognized set is the set it implements, and nothing is published
  or configured. The reference implementation defaults to the letters it
  implements (`G V P O X`).
- **Single-writer can be enforced by construction.** §6 says single-writer;
  the reference implementation returns the *same* writer object for a region
  rather than a new one, so a second writer cannot exist. An SDK that hands out
  two writers for one region breaks §6's discipline.
