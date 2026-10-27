# `M` — shared memory grants

How vizi hands a plugin a block of shared memory, and how it gets it back.
Transport rules are in `doorbell_protocol.md`.

**Scope.** This document covers the *grant*: the record that hands over a
capability, and what a receiver checks before acknowledging it. What the memory
contains, how its interior is divided, and every byte-level handoff protocol are
a separate layer — `shared_memory_protocol.md`. The doorbell transmits; it does
not interpret (`shared_memory_principles.md` A1).

**Direction: host→plugin only.** Only vizi grants memory, so the direction is
structurally one-way forever and a plugin sending `M` is a session fault
(`doorbell_protocol.md` §2); its descriptor is closed first. **Trust:** vizi
controls its own sends, so the record cannot be corrupted by the peer — but the
*mapping* it names can still be wrong, which is what §7's cross-checks are for.
**Rate-limit ownership:** none needed. A grant is a session-scale operation
(§8), not a paced stream. **Ancillary descriptors: exactly one on unix, none on
Windows.**

---

## 1. The governing constraint

**Memory cannot be withdrawn.** Once the plugin has mapped, both parties have
access until the plugin unmaps or exits.

| Platform | Backing | Why |
|---|---|---|
| Linux | `memfd`, sealed shrink+grow | the object lives while any fd or mapping references it; the seal makes `ftruncate` fail at the kernel |
| macOS | unlinked temp file, no sealing | shrinking would SIGBUS the plugin, which is crashing it rather than revoking; the mapping stays valid |
| Windows | named section, size fixed at creation | closing the host handle does not unmap the peer's view, and no API unmaps another process's view |

Three consequences shape everything below:

- **Retirement is a request, never an enforcement.** No host-side code may assume
  compliance.
- **The memory budget is per session, not per grant.** Reclamation happens at
  unmap or process exit.
- **A mis-mapped grant is an invisible allocation**, not merely wrong data — the
  plugin holds pages the host does not know exist.

The Linux seal is containment against the plugin shrinking the segment under the
host. It is not a lever for the host.

**macOS and Windows have no such containment, and one of them has a gap.** On
Windows a section's size is fixed at creation, so there is nothing to shrink. On
macOS the segment is an unlinked temp file with no seal, so a plugin holding the
descriptor CAN `ftruncate` it, and the host's next access past the new end is a
`SIGBUS` -- the host crashing, not the plugin. It is accepted, not fixed: a plugin runs with the
user's privileges and is installed by the user, so a plugin set on crashing vizi
has simpler means, and macOS offers no seal to close it with.

---

## 2. Acknowledgement depth

A grant is acknowledged in **one round, not by exactly one message.** The
distinction matters because a duplicate grant must be answered again (§7), and a
terminal answer is replayable: what is bounded to one is the *depth* of the
exchange, not the number of times the same answer is given. The per-segment state
machine that makes replay idempotent is `shared_memory_protocol.md` §8.

A grant is acknowledged at depth one, and no deeper. After granting, no host
action depends on the plugin's knowledge of the host's knowledge: the index is
monotonic and never reused, and the memory cannot be reclaimed (§1), so further
rounds carry no information.

The general form, for any future exchange that appears to need more: **depth is
bounded by the number of reversible state transitions.** Grant installation is
irreversible and unilateral, so depth is 1 — in both directions, including
release.

---

## 3. Names

| | Width | Lifetime | Job |
|---|---|---|---|
| doorbell `id` | u32, wraps | one transmission and its retries | match a send to a receive in a trace; nothing acks by it |
| segment id | u64, monotonic | the segment's life | name the memory |
| identity proof | 16 B random | the segment's life | prove the plugin opened *that* memory |

**Protocol-wide: every *durable* name — one whose meaning must survive the
coordination that created it — is u64 and monotonic.** A wrapping counter may
correlate but must never identify.

Session-scoped handles may be narrower, and each states its width, the rate that
consumes it, and why it cannot wrap inside one session:

| name | width | consumed at | wraps in a session? |
|---|---|---|---|
| doorbell `id` | u32 | every record | yes — correlates only, never identifies |
| `interaction` | u32 | gesture rate | no |
| `tool` | u32 | activation rate | no |
| `gen` | u32 | one per dataset open | no — the control plane's `open_generation` is u64; the wire carries its low 32 bits |
| pane `epoch` | u32 | one per emitted interpretation change, per `view_id` | no — user rate, and only an *emitted* epoch consumes a number (`doorbell_E_input.md` §1a) |
| session epoch | u32 | one per session | no — and it is half of a u64 name |
| segment index | u32 | one per grant | no |

`segment_id = (session epoch << 32) | index`, where `index` is a monotonic,
never-reused per-session allocation. This fits `SegmentRef.segment_id`'s
`uint64` and makes a reference **self-scoping** — it names its
session as well as its segment, so it cannot outlive its meaning and land on a live
index in a later session.

**The index is allocated from a counter, never from a container length.** Taking
it as `dp.segments.len()` and pushing only on success reuses an index whenever a
grant fails permanently, so a later grant lands on a number a stale reference may
still name. A counter gives retry-recognition, stale-reference rejection and
collision-freedom on its own, none of which survive reuse. **Those three are the
whole justification**, and they hold whatever the send did: under
`doorbell_protocol.md` §6's non-blocking rule a clean refusal means nothing
arrived and anything ambiguous closes the doorbell, so there is no
delivered-but-reported-failed case for a counter to be defending against.

**A processing stage's ring is the one segment held by several sessions**
(`processing_stages.md` §4), and it carries ONE id in all of them, because its
preamble names one (§5). The host mints it from an epoch no session uses; the
non-aliasing rule below is what makes that id collide with nothing.

**Both halves start at 1**, so `segment_id == 0` is never a valid grant and is a
free "no segment" sentinel wherever one is wanted (`plugin_principles.md` 11).

**The epoch's one requirement is non-aliasing**: it must differ between any two
sessions whose segment references can coexist. That is the whole of what makes a
reference self-scoping and the only property another implementation depends on.
Where the value comes from — a counter, a random draw — and what happens if the
space is exhausted are the host's business, and are deliberately unspecified.

---

## 4. The record

`M` carries the capability and nothing else. Everything about what the memory
*means* travels over gRPC (§6).

Offsets are from the start of the 100-byte **payload**, i.e. record byte 8.

```
[0..8)    segment_id   u64   never 0
[8..16)   size         u64   total bytes, including the 32-byte preamble
[16]      perms        u8    permission bits, below
[17]      padding      u8    MUST be 0
[18]      name_len     u8    0 … 80; 0 on unix
[19..)    name         name_len bytes of UTF-8, then one 0 terminator
          padding            from the byte after the terminator to [100), MUST be 0
```

**`perms` bits**: `PLUGIN_READ` 0x01, `PLUGIN_WRITE` 0x02, `HOST_READ` 0x04,
`HOST_WRITE` 0x08. Bits 4–7 reserved, MUST be 0. **`PLUGIN_READ` is required on
every grant**: a write-only mapping maps to nothing sensible on any supported
architecture, and the plugin must read the preamble to validate the grant at all
(§5). A grant without it is a grant failure.

**`perms` is the one field where a reserved bit is *not* ignored**, against
`doorbell_protocol.md` §8's general rule. That rule holds where ignoring an
unknown value claims nothing — an unrecognized modifier key is simply not
applied. Here the acknowledgement asserts *installed, with these permissions*, so
a plugin that quietly dropped a bit it did not understand would be claiming a
capability it had not established, which is `plugin_principles.md` 11 exactly. A
reserved `perms` bit is therefore a grant failure, reported with the rest.

### The name

**Length byte, UTF-8 bytes, NUL terminator — and the two must agree.** The
terminator sits at payload `[19 + name_len]`, and no byte of the name may be 0.
A reader may therefore take the length or scan for the NUL and get the same
answer, which is the entire reason both are carried: the two encodings a caller
might reach for cannot disagree about where the name ends.

**`name_len ≤ 80`**, which is what puts the terminator at or before the payload's
last byte. The bound is derived rather than chosen — it is simply how much fits —
so **a longer or differently-shaped name needs no wire change**, only a different
value in §5. That is the point of sizing it this way rather than to the current 43-byte
`Local\vizi-…`.

Everything after the terminator is padding and MUST be zero, so a record stays a
pure function of its fields and byte-exact vectors remain possible
(`doorbell_principles.md` B1). `name_len` is a length *within* a field of bounded
extent; it delimits nothing about the record, which is what B1 forbids.

**This is the classic buffer-overrun shape, and it is defused rather than
avoided.** Three things bound it, and an implementation must do them in this
order:

1. **Validate `name_len ≤ 80` and the terminator's position before reading any
   name byte.** Size every destination from the *validated* length, never from
   the raw byte.
2. The source is a fixed 112-byte record, so even an unvalidated read cannot
   leave it — the damage a wrong length can do is bounded by construction, not
   by care.
3. The CRC has already passed, so a corrupt `name_len` is a ~2⁻³² event rather
   than something a peer can steer (`doorbell_protocol.md` §4).

**Padding here is the op's, not framing's.** Its extent depends on `name_len`, a
payload field, which is exactly the criterion `doorbell_protocol.md` §8 uses: byte
17 is fixed and framing owns it; the post-terminator padding is variable and
travels with the rest of `M`'s checks on the grant acknowledgement.

The opcode declares the rest: `op == 'M'` means this grants memory, requires a
gRPC acknowledgement, and carries exactly one fd on unix and none on Windows.

**`M`'s rules are keyed on platform where the transport's are keyed on class**
(`doorbell_protocol.md` §7 rule 3) — `name_len == 0` on unix, the descriptor
count, the section name mattering only on Windows. The two are equivalent only
because setup refuses a socket whose `SO_TYPE` is not its platform's, so unix is
always a message transport and Windows always a stream. Platform is the right key
here: what differs is `SCM_RIGHTS` and named sections, not framing.

Each field is present because mapping needs it: `size` for `mmap`, `perms` to
choose `ACCESS_READ` versus `ACCESS_WRITE` (a read-only fd will not map writable),
`name` because on Windows it *is* the capability, `segment_id` to name what is
being acked and to cross-check the preamble.

**`perms` is a declaration the plugin cross-checks, not an instruction.**
Enforcement lives in the fd's open mode on unix and the section's `PAGE_*`
protection on Windows. Its host half matters independently: knowing the host may
also write tells the plugin its reads can race and must use the region's
discipline.

**The grant layer is whole-block.** Lifetime, ownership and retirement are per
segment; the number of regions inside makes no difference.

---

## 5. The preamble

The first 32 bytes of every granted mapping. Offsets here are from the start of
the **mapping**, not the record:

```
[0..8)   segment id          u64
[8..12)  reserved            u32   senders write 0; receivers MUST NOT check it
[12..28) identity proof      16 B  host-generated, random
[28..32) crc over [0..28)    u32
[32..)   segment content
```

Little-endian, and **the same checksum as the doorbell record**: CRC-32/ISO-HDLC
exactly as `zlib.crc32` computes it — reflected, init `0xFFFFFFFF`, final xor
`0xFFFFFFFF` (`doorbell_protocol.md` §4). "crc32" names a family, not an
algorithm, and this preamble is the one structure an independent memory-layer
implementation must reproduce byte-for-byte.

The host writes it before the first send attempt; it is immutable for the
segment's life.

**`[8..12)` is reserved, the preamble's one growth slot.** A sender writes
zero there and a receiver does not check it, so a later minor can give it a
meaning without a 1.0 receiver refusing the grant; the CRC still covers it. A
grant record id could not go there anyway. The preamble
is written *before* the record's `id` is allocated, it is immutable for the
segment's life, and a retry reuses the same `id` unchanged — so such a field
would be dead by construction while every independent implementation still had to
reproduce it byte-exactly.

**The acknowledgement keys on `segment_id`**, which is u64, monotonic and never
reused, so it already names the grant uniquely and durably; the 16-byte proof is
what establishes that the *right* mapping was opened. An `InstallSegment` that
arrives first and fails before any `M` reports against the `segment_id` it
already has.

**An acknowledgement must prove shared identity, not merely installation.** On
Windows the SDK opens with `mmap.mmap(-1, size, tagname=name)`, whose CPython
implementation calls `CreateFileMappingW` — **create-or-open, not open**. If the
name no longer exists the plugin silently receives a fresh, private, zero-filled
section and every other check passes. Only a host-written value read **out of the
mapping** distinguishes that case.

Rules that follow:

- **The 16-byte proof never appears in the `M` record.** Echoing what you were told
  proves nothing. `segment_id` may appear in both — it is a name, not evidence.
- **The proof is an identity check against accident, not a capability.** On Windows
  the unguessable *name* is the capability; nothing may treat the proof as a
  security token.
- **The name has requirements, not a spelling.** It MUST live in the per-logon
  `Local\` namespace — `Global\` needs `SeCreateGlobalPrivilege` and would widen
  the reachable set past the session that owns it — MUST carry at least 128 bits
  from a CSPRNG, MUST be unique for the session's life, MUST NOT be derived from
  `segment_id` or anything else the plugin has already seen, and MUST fit the
  bound in §4. vizi spells it `Local\vizi-<32 lowercase hex>`, 43 bytes against
  a bound of 80; that is an example, not a contract, and it cannot be otherwise
  since receivers are forbidden to validate the pattern (§7).
- **A fresh section is self-detecting.** `crc32(28 zero bytes)` is `0x807077e9`
  *(measured)*, so a zero-filled preamble fails its own checksum and the plugin can
  report a precise error before any round trip.

32 bytes rather than 24: it is 8- and 16-aligned and carries both names, so a
memory dump identifies itself without a doorbell record that may be long gone.

---

## 6. Meaning travels separately

`M` carries the capability. What the memory *is* — its regions, their offsets and
extents, and the handoff discipline of each — arrives as `InstallSegment` over
gRPC and is specified in `shared_memory_protocol.md` §3.

Two things about that split matter to the doorbell and nothing else does:

- **A segment is not usable until both halves have arrived**, which is why the
  acknowledgement in §7 covers meaning as well as mapping.
- **Regions are named by a dense, 0-based index**, because that is what a `D`
  mask bit names by position (`doorbell_D_notification.md` §1). The doorbell
  needs the index to exist and to be bounded; it does not need to know what one
  contains.

`InstallSegment` may ride the gRPC exchange a grant already has, or go
standalone ahead of it (`control_plane_protocol.md` §8.12 — a data-segment
grant rides `DataVolume`; an export grant is installed standalone ahead of its
first `ExportChunk`); either way the receiver holds a descriptor into the
segment until both halves are in.

---

## 7. Acknowledgement

**This section is the source of truth for when a grant is acknowledged and for
the failure classes** (`plugin_principles.md` 13). `shared_memory_protocol.md` §8
owns the per-segment state machine and cites this; `control_plane_protocol.md`
§8.12 owns the delivery rules and cites both.

The plugin acks once, over gRPC, when **mapped ∧ preamble validated ∧ meaning
applied**. Whatever an SDK exposes as "this segment is ready to use" gates on the
same three conditions.

**The two halves may arrive in either order**, because the grant does not wait
for `InstallSegment` and `InstallSegment` does not wait for the ack (§6). The
plugin keys each by `segment_id` and holds whichever arrives first; **a success
ack is emitted by whichever arrives second**, since only then are all three
conditions established. **A failure is reported as soon as it is known** — the
missing half cannot rescue a mapping that did not work, and waiting would delay a
report the host can already act on. Holding is bounded by the number of outstanding grants, which is
session-scale, so there is no queue to grow. "Install on the listener thread"
below governs the `mmap`, which is what must not queue; applying the meaning
happens wherever the second half lands. This is the same both-orders-safe shape
`doorbell_E_input.md` §2 uses for terminals, and for the same reason: two
transports have no ordering contract between them.

- **Success:** `{segment id, proof read from [12..28)}`
- **Failure:** `{segment id, failure class, error message to surface}`

**The failure carries a class, not only a string**, and **every validation
failure maps to exactly one** — otherwise two SDKs classify the same fault
differently and the host recovers differently:

| class | what it means for the host | which failures |
|---|---|---|
| `retryable` | a transient resource failure; re-granting may work | **a named set**: `mmap`/`MapViewOfFile` out of address space, out of handles, or out of memory. Nothing else is retryable |
| `too_large` | re-grant smaller | **only** an explicit size-policy refusal — the peer declined `size` on its own budget (`shared_memory_protocol.md` §2). Not a mapping failure that happened to involve a large size |
| `malformed` | a vizi bug; re-granting the same thing fails again | every record-level violation in the list above; a preamble that does not checksum or whose `segment_id` disagrees; a region table that does not parse or does not fit; the mapped object's size disagreeing with `size`; the access obtained not matching `perms` |
| `unsupported` | this peer cannot use it at all | an unknown `purpose` or `discipline` |
| `failed` | **the catch-all: permanent, do not retry** | anything else — a mapping or open error that is not in `retryable`'s named set, or an OS error the peer cannot classify. `error message to surface` carries what it was |

**The catch-all is what makes "every validation failure maps to exactly one"
true.** Without it an implementation meeting an unlisted `mmap` errno must
either invent a class or pick the nearest, and the choice decides whether the
host retries forever, grants smaller, or stops — so it is a divergence with
user-visible cost. **`retryable` is a closed list precisely so that the default
is permanent**: a class that means "try again" must never be reached by
accident.

A retry reuses the same doorbell id, index and preamble, which is what makes it
idempotent — the `id` is allocated once when the record is created and reused
unchanged, never re-allocated (`doorbell_protocol.md` §4). The ack covers meaning as well as mapping, because applying the meaning
can also fail — an unknown `purpose`, a region table that does not parse — and
those deserve the same reported-error path rather than a silent half-install.

### Plugin-side rules

- **Install promptly and boundedly.** `mmap` is fast, and what matters is that
  the acknowledgement is not queued behind unrelated work — not which thread does
  it. Doing it on the listener thread is the simplest way to get that and is what
  the shipping SDK does; an implementation that guarantees the bound another way
  is conforming. Naming a thread here would also contradict the transport
  boundary, which says the transport does not map memory at all.
- **Contain errors.** A failed install reports and does not kill the listener
  thread.
- **Publish the segment as usable only after validation**, so whatever an SDK
  offers as "wait until this segment is ready" cannot succeed on one that failed.
- **A duplicate unix fd grant closes the duplicate descriptor** and re-reports the
  installed state. It never remaps and never leaks.

### Cross-checks before acking

The full list is `shared_memory_protocol.md` §8, since most of it is about the
mapping rather than the record. Two belong to `M` itself:

- the preamble checksums, and its `segment_id` equals the record's;
- **on Windows only, the name is well formed** — `name_len ≤ 80`, a 0 byte at
  `[19 + name_len]`, no 0 byte inside the name, valid UTF-8, and zero from the
  terminator to the end of the payload. All of it is the op's rather than
  framing's, because locating any of it requires knowing that byte 18 is a
  length. **On unix none of it is checked** (below): the field is ignored whole,
  and listing a terminator rule here while calling the field ignored is how one
  SDK would fail a mappable grant that another installs.

**Almost every `M` violation rides the grant acknowledgement**, never a fault.
`M` is the one op with an answering message already, and the host needs the
`segment_id` in it to re-grant (`doorbell_protocol.md` §8).

**`segment_id == 0` is the exception, and it reports on `ContractViolation`
instead.** The acknowledgement keys on `segment_id`, and this record names no
segment — 0 is the "no segment" sentinel (§3) — so an ack has no addressee and
the host could not correlate one if it arrived. A record that identifies no grant
cannot report through a per-grant message. `where` is `zero_id`, matching
`doorbell_D_notification.md` §4's answer to the identical case.

**And the host MUST consume that violation to close the grant out.** Otherwise
the grant is pending forever: no acknowledgement can ever name it, the
missing-ack deadline fires, and the query-then-resend path re-sends the same
malformed record indefinitely. `ContractViolation` carries the offending
record's raw 112 bytes (`doorbell_E_input.md` §2), which is what lets the host
identify which of its outstanding grants emitted it. **On receipt the host marks
that grant permanently failed and does not retry** — the defect is in vizi's own
send and re-sending reproduces it.

This is deliberately *not* a session fault, though it is the one `M` case with
no answering message. `segment_id == 0` can only come from a vizi bug, and
`doorbell_E_input.md` §2's argument applies unchanged: killing a user's plugin
session over a host bug punishes the wrong party, while the report is what gets
the bug fixed.

**The transport does not send that ack**, so it does not decide the outcome
either. What it owes depends on whether the record survived: **a record it hands
up carries the decoded grant, the record's `id` and the descriptor**; **a record
it rejects carries no descriptor at all**, that one having been closed before the
rejection was returned (`doorbell_protocol.md` §8), so there is nothing a caller
could claim or leak. Either way it reports the record-level violations it found — **`segment_id == 0`**, which the
record forbids (§4) and which is the "no segment" sentinel (§3); **`size < 32`**,
which is as record-local as `segment_id` and which the transport MUST check, not
may (`shared_memory_protocol.md` §8 has the half that needs the region table, and
that half alone is the memory layer's); `PLUGIN_READ` absent; a reserved `perms` bit; and — **on Windows only** — `name_len > 80`, a
missing or misplaced terminator, a 0 byte inside the name, invalid UTF-8, a
non-zero byte after the terminator, and `name_len == 0`, where the name *is* the
capability and an empty one grants nothing.

**On unix none of the name items apply.** The diagnostic there records the raw
`name_len` byte and nothing else — it does not read the name, so there is no
length to validate before sizing a read, which is the question an unconditional
list would have raised for `name_len > 80`.

**On unix the name field is not validated at all.** The descriptor is the
capability there; the name carries nothing. So `name_len`, the name bytes, the
terminator and the tail are read for the diagnostic and for nothing else — any
content is a **local** diagnostic and the grant proceeds. A success
acknowledgement has nowhere to carry it and `ContractViolation` is not `M`'s
channel (§7), so local is what it can be.

**Ignoring the field means ignoring all of it** — `name_len`, the bytes, the
terminator and the tail alike. Splitting it, so that a non-zero `name_len` is a
diagnostic while the UTF-8, terminator and tail checks stay grant failures, makes
a field the platform ignores capable of failing a grant unrecoverably. The
sender's obligation is unchanged: it still zeroes everything
(`doorbell_principles.md` B1), and the conformance vectors are generated from the
sender, so byte-exactness is unaffected.

**Descriptor ownership follows `doorbell_protocol.md` §8 and is not restated
here**: a record-locally *rejected* `M` has its descriptor closed before the
rejection is returned and never becomes claimable, while a record that is handed
up transfers its descriptor with it. In particular the transport does **not**
close it "only if nobody upstream takes ownership" — that is the opposite rule
for the rejection case, and it would have one SDK failing a grant another
installs.

Note which violations are *not* rejections: `name_len != 0` on unix is a
diagnostic (below), so such a record **is** handed up, descriptor and all.

**The Windows name's format is not validated by the receiver.** Length and a zero
tail are checked; the shape is not. A receiver enforcing `Local\vizi-<hex>` would
reject a future naming scheme for no benefit, and a wrong name is caught by the
preamble checksum anyway (§5), which is a stronger check than a pattern match.

The record's *fixed* padding — byte 17 — is **not** on this list. It sits at a
fixed offset with a fixed extent, so the framing layer owns it and a violation is
a session fault (`doorbell_protocol.md` §8); listing it here as well is what would
give the same byte two different remedies. The post-terminator padding is
variable, so it is the op's, and it is in the list above.

**A failure here is a *grant* failure, not a doorbell framing fault.** The record
was well-formed; the mapping was not. It reports over gRPC and the host may re-grant
at a new index; it must not trip the fail-stop session teardown.

### Retry

Clean `EAGAIN` / `ENOBUFS` / `WSAEWOULDBLOCK` / `ETOOMANYREFS` may retry the same
grant unchanged — nothing arrived, on every platform. An ambiguous send error may
not; it closes the doorbell. **The host records and retains the segment before
the first send attempt**, so a retry always has something to retry with, and the
segment table never depends on a send having succeeded.

On a missing ack the host issues an **idempotent gRPC query** for the plugin's
installed-grant state, keyed on `segment_id`; a gRPC failure during that query is
an ordinary session failure. **The answers and what each obliges are part of the
protocol**, because "query the state" without them leaves every host to invent a
recovery:

| answer | what the host does |
|---|---|
| **installed** | the ack was lost, not the grant. Take the answer as the ack; nothing is re-sent |
| **failed** | the recorded failure is replayed; treat it as the ack it was, and re-grant **at a new `segment_id`** if the class allows (§7) |
| **pending**, naming which half arrived | **never re-send `M`.** If the missing half is `InstallSegment`, send *that*; if `M` is the missing half, send nothing. Either way the no-progress deadline keeps running |
| **not present** | **re-send the SAME grant** — same `segment_id`, same index, same preamble, same descriptor, same doorbell `id`. Never a new segment id |
| **released** | stop waiting, drop the host's own mapping, and **never re-send either half** — a re-send would revive a grant the peer has released, and the peer answers `SegmentReleased` again forever |

**`released` is a legal answer, and its obligation is the row above**:
`shared_memory_protocol.md` §9a step 8 says a query can cross a retirement, and
`GrantStateKind.GRANT_STATE_RELEASED` is its value in the IDL.

**`pending` is not an edge case — it is what "the two halves may arrive in either
order" (§7) routinely produces**, and every other answer is actively harmful
there:

- **`not present`** would re-send, and a duplicate `M` while pending is answered
  by "close the duplicate descriptor, do not remap, stay pending"
  (`shared_memory_protocol.md` §8). So the loop re-arms every deadline, and each
  pass puts **another `SCM_RIGHTS` descriptor in flight against the host's
  user-wide `RLIMIT_NOFILE`** — the `ETOOMANYREFS` multi-session case
  `doorbell_protocol.md` §6 rule 6 calls the one where killing a session is
  least acceptable. A recovery path that degrades the host is worse than failing
  cleanly.
- **`installed`** claims something the plugin has not established
  (`plugin_principles.md` 11), and the host takes it as the acknowledgement.
- **The mirror sub-case — `InstallSegment` arrived and `M` did not — sends
  nothing on purpose.** The descriptor argument does not apply there, since the
  peer recorded that no `M` arrived and would consume a re-sent one; but the only
  non-transient cause is a listener that is no longer draining, and re-sending
  into it accomplishes nothing the no-progress deadline will not settle more
  honestly.
- **`failed`** re-grants at a new `segment_id`, producing two mappings for one
  intended grant — the deliberate invisible allocation the `not present` row
  exists to avoid (§1).

**The host bounds the whole install, not only the re-sends.** A named number of
`not present` answers for one `segment_id` escalates rather than looping — and so
does a `segment_id` that has made **no progress** for a named total, `pending`
included. Otherwise a plugin whose listener died while gRPC stays healthy answers
`pending` forever, the host neither retries nor escalates, and the segment is
outstanding for the life of the session. Escalation gives the grant up and
reports it, and keeps the session (`control_plane_protocol.md` §9's "escalate");
it never starts descriptor-producing retries.

**The `not present` row is the one that matters, and the obvious alternative is wrong.**
"Not present" does not prove the original `M` is gone: it may still be sitting in
the peer's socket queue, undrained. Allocating a fresh `segment_id` and granting
again would then land *both* — two mappings of two objects for one intended
grant, the second of which the host believes is the only one, which is
`doorbell_M_memory.md` §1's invisible allocation created deliberately. Re-sending
the same identity is idempotent by construction: if the original arrives too, the
peer sees a duplicate and replays one answer (`shared_memory_protocol.md` §8).

**A new `segment_id` is only ever allocated after a terminal answer**, which is
what makes *failed* and *not present* different rows rather than one.

**This query is the grant path's only backstop, and it is worth naming what it
catches.** A grant can be lost with the send reporting success: on Windows the
first send after an orderly peer close returns `Ok` and only the next write
reports (`doorbell_protocol.md` §6 rule 7). Nothing at the transport layer can
see that. The ack that never arrives is the sole evidence, so **the deadline that
decides "missing" is load-bearing** — it is the difference between a lost grant
being noticed and a plugin waiting forever on memory it will never receive. It is
10 s (`control_plane_protocol.md` §9), and of the host-side deadlines it is the
one whose absence would be silent rather than merely untuned. gRPC's contribution is a **failure detector** — delivery or a connection
error, never silence — so ambiguity is resolved by asking, never inferred from a
missing ack.

---

## 8. Grants are session-scale

Retirement, release, and the lifecycle of a Loaned ring are
`shared_memory_protocol.md` §9. What belongs here is the consequence for the
doorbell: **a grant is not a paced stream**, which is why `M` declares no
rate-limit rule and why the transport's queue sizing (`doorbell_protocol.md`
§11) is driven by input events rather than by grants.

Per-frame reclamation happens *inside* a segment, through a Loaned ring, with
`ReleaseDeposit{segment_id, region, offset, len}` over gRPC — never by granting
and retiring segments at frame rate. That would put a round trip on a
per-interaction path, which is `doorbell_principles.md` A2 wearing a different
hat.

This is guidance for new work rather than a description of anything shipping: no
current path grants at frame rate, and the busiest amortises by doubling its
ring.

---

## 9. Rejected alternatives

| Alternative | Why not |
|---|---|
| A size-zero `M` as a release request | Carries no capability, so it belongs on gRPC; needs reliability the doorbell lacks (a lost release is a permanent session leak); makes every validation rule conditional on `size == 0`; breaks the fd-count check |
| `(index, generation)` instead of a monotonic index | A never-reused index gives retry-recognition, stale-reference rejection and collision-freedom on its own |
| Carrying the identity proof in the `M` record | Echoing a value you were told proves nothing about the mapping |
| `purpose`, layout or `supersedes` in the `M` record | None is needed to map, validate or acknowledge; all are meaning, and meaning grows |
| A revoke primitive | Does not exist on any platform (§1) |
| A release timeout that acts | Its only remedy is revocation |

Alternatives about the memory's *interior* — the in-mapping directory, magic-byte
sniffing, reusing a drained ring — are in `shared_memory_protocol.md` and
`shared_memory_principles.md`, with the layer they belong to.

---

## 10. Open

Nothing. **A Constant-only grant is acknowledged like every other**: "always" is
a simpler rule than "always except", and the exception has a cost. A plugin that
skipped the ack for a Constant grant would leave the host's missing-ack deadline
to fire, query, and re-send, forever, for a grant that installed correctly the
first time.

`supersedes`, Pulsed buffer counts and dynamic occupancy belong to the memory
layer: `shared_memory_protocol.md` §11.
