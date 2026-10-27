# Shared memory protocol

> **Status.** This is the `1.0` specification. Where it and the reference host
> disagree, `shared_memory_implementation_details.md` §1 lists the differences
> and this document is what the code owes.

Normative. What a granted segment contains, how its interior is divided, and the
byte-level handoff protocol for each region discipline.

How a segment is *granted* is the doorbell's half (`doorbell_M_memory.md`).
Principles are in `shared_memory_principles.md`.

---

## 1. Origins

**Every offset in this document is from the start of the mapping**, byte 0 being
the first byte of the preamble. A region's `offset` is a mapping offset, so it is
always ≥ 32.

**A Loaned ring's INTERNAL arithmetic is region-relative**, because the region's
capacity is the modulus a ring wraps against — but that is the allocator's own
address space and it is **not what travels**. Every `offset` on the wire, in a
descriptor and in a release alike, is **mapping-relative** like every other
offset in this document (`control_plane_protocol.md` §8.12). The conversion is
`region.offset + ring_address`, it happens once at the allocator's boundary, and
the two never mix in one number.

**Every multi-byte integer in every structure this document defines is
little-endian** — the preamble's ids, the Pulsed state and length words, all of
it. An independent implementation should not have to infer this from the
doorbell record's rule.

**And so is a deposit's payload.** A voxel deposit — a `SegmentRef` with a
`shape_zyx` and a `dtype`, whichever direction it travels — is the array's
samples **contiguous in C order (Z slowest, X fastest), each multi-byte sample
little-endian, with no padding between samples, rows or planes**: exactly
`len = Z·Y·X·sizeof(dtype)` bytes, which is what §8.2's bounds check assumes.
There are no strides on the wire because there is nothing for them to say.
`ReaderOpened.little_endian` and `OpenExport.little_endian`
(`control_plane_protocol.md` §8.2, §8.3) describe the deposits that follow and
are informational: at `1.0` the only conforming value is `true`, because the
receiving end reinterprets little-endian regardless — a reader whose source is
big-endian converts before it deposits. The fields exist so that a future
big-endian host can say so rather than so that a plugin can choose.

A 32-byte error here is silent and data-dependent, which is why it is the first
section.

---

## 2. Segment anatomy

```
[0..32)     preamble          identity; see doorbell_M_memory.md §5
[32..)      regions           laid out by InstallSegment, in any order
```

**Bytes from 32 up to the lowest region offset are host-reserved.** A 1.0
receiver never reads or validates them, so a longer preamble in a later minor
grows there; any such extension carries its own integrity check.

Regions do not overlap, all lie within `size`, **all begin at or after byte 32**
— the preamble is not a region and nothing may declare over it — and the fit
check is

```
32 + Σ region.extent ≤ size
```

The `offset ≥ 32` floor is separate from the fit check and both are needed: a
single region at `offset = 0` with a small extent passes the sum and would still
have the receiver interpret the preamble as payload.

**A pane-associated segment embeds `(view_id, epoch)` in its payload**, at the
place its region's **`epoch_offset`** declares: an 8-byte header, `u32 view_id`
then `u32 epoch`, **little-endian like every other multi-byte integer this
document defines** (§1) — `view_id` is a `u8` on the doorbell's `D` and `E`
records and widens to `u32` here for alignment, so the high three bytes MUST be
zero and a receiver rejects a header where they are not.

**Rejecting the header costs the PAIR and not the payload.** This is the same
rule §2 already states one paragraph down for a deposit too short to hold a
header — *"carries no pair, and is delivered"* — and for the same reason: **a
rule about a hint must not fail the data it annotates.** A consumer that cannot
learn which epoch a payload belongs to still has the payload and can say so;
refusing the payload converts a missing annotation into lost voxels, which is the
larger failure and the one an implementation reaches for because the bound reads
like validation.

**And `epoch = 0` in a header is invalid**, as `control_plane_protocol.md`
§8.10's *"never 0"* implies. It is rejected on the
same terms: the pair is dropped, the payload is delivered.

**`epoch_offset` is relative to the UNIT OF INDEPENDENT OWNERSHIP**, which is
the rule the disciplines already differ on and the only one that stays correct
as they do:

| discipline | header sits in | why |
|---|---|---|
| PULSED | each **slot** | the pair belongs to a published revision (§6) |
| LOANED | each **deposit** | deposits are independently lived and can outlast an epoch |
| CONSTANT / OWNED | the **region** | one lifetime, so one header |

**A single region-level header would be wrong for LOANED and silently so.** A
ring holds several deposits at once and they may belong to different epochs, so
one mutable header would retroactively restate the meaning of a deposit already
in flight — the reader would find the *current* epoch attached to *older* bytes
and have no way to tell. Per-deposit placement is what makes the pair mean what
its payload means.

**And the "durable copy" claim is CONSTANT and OWNED's, not PULSED's.** §6's
release consumes a slot's state word, and whoever owns the state word owns the
payload, so a reader cannot consult a PULSED slot's header without claiming the
slot — the pair travels *with* a revision rather than sitting somewhere it can
be re-read later. That is the right behavior for a revision and it is not
durability; only a region whose contents have one lifetime offers the second.

**Bounds are part of the grant, not a runtime check**: `epoch_offset + 8` must
fit within `slot_bytes` for PULSED and within `extent` otherwise. A grant that
violates it is `malformed` — the same footing as the `offset ≥ 32` floor and the
fit check (§2), and for the same reason.

**For LOANED the grant-time bound is `extent`, and it is the only one available**,
because a deposit's length does not exist until one arrives. The
runtime half is a separate rule and a weaker one: **a deposit too short to hold
the header carries no pair, and is delivered.** It is not refused.

That direction is deliberate. **A rule about a hint must not fail the data it
annotates** — the pane pair says which epoch a payload belongs to, and a consumer
that cannot learn that still has the payload and can say so. Refusing the deposit
converts a missing annotation into lost voxels, which is the larger failure, and
it is the one an implementation reaches for because the bound reads like
validation. The field is *optional* rather
than 0-means-none, since 0 is a legal offset — **absent** is how a region says it
is not pane-associated.

**The location is declared once and the value changes per publication, so they
are separate things and stay in separate places.** `epoch_offset` is
installation-time state a receiver reads once; the pair itself is read on every
publication. Merging the two — carrying the epoch on `InstallSegment`, or
declaring a payload schema inline — would tie a per-publication value to a
message sent once. A `D` naming the segment carries the
same pair (`doorbell_D_notification.md` §1), but the hint is advisory and the
mapping is the durable copy — so a reader that finds a stale pair in memory knows
the contents predate the current interpretation without any hint having arrived.
A segment that is not pane-associated embeds nothing and its `D` carries zeroes.

**When a region declares no `epoch_offset`, a receiver rules on staleness with
the epoch it acknowledged for that pane** (`doorbell_E_input.md` §1a). That is a
weaker test — it catches a hint from before the pane's current epoch and not a
mapping whose contents lag — and it is the whole of what is available, so an SDK
that appears to check an embedded pair it was never given a location for is
checking something it invented.

**If a payload's meaning needed the epoch and did not carry it, the failure is
already upstream.** The pair is what lets a reader tell; it is not what keeps the
data fresh.

**`size` has no protocol cap of its own**, and its bound is the per-session
memory budget, which `control_plane_protocol.md` §9 settles: **the manifest's
declared segment total, the arena headroom §9 allows above it, plus twice the
largest single deposit served this
session, under a deployment ceiling defaulting to 4 GiB**. The second
term is what §9.5's doubling needs, since worst-case retention is one ring at
the high-water capacity beside the current one. A grant beyond the budget is
refused `too_large` — the one failure class that means *re-grant smaller*.

**A peer MAY also refuse a grant larger than it is willing to map**, and that is
a different refusal: the budget is the *granting* side declining to ask, this is
the *receiving* side declining to serve. It is an ordinary grant failure (§8),
reported, and the host may re-grant smaller. Both are specified outcomes rather
than gaps.

---

## 3. `InstallSegment` — the region table

Authoritative for everything about the interior. Sent host→plugin immediately
after the grant, without waiting for the acknowledgement, so the two travel in
parallel.

```
InstallSegment {
  segment_id, purpose, supersedes, subject
  repeated Region regions
}

Region {
  index            // 0-based, dense: 0 … region_count-1
  offset, extent   // mapping-relative
  discipline       // CONSTANT | OWNED | PULSED | LOANED | STAGED
  producer         // UNSPECIFIED | HOST | PLUGIN — §3.1; never UNSPECIFIED on the wire
  buffers          // PULSED: buffer count (3 today); 0 otherwise
  slot_bytes       // PULSED: bytes per buffer; LOANED: allocation alignment
  id               // optional stable name, e.g. the manifest overlay id
  epoch_offset     // OPTIONAL: where the (view_id, epoch) header sits — §2
}
```

`index` is dense and 0-based because it is what a `D` mask bit names by position
(`doorbell_D_notification.md` §1) — no translation table anywhere.

### 3.1 `producer` — which end fills the region

**A region's bytes have one author, and the record has to say which end it is.**
Without this field a `LOANED` region the host allocates and one the plugin
allocates are **byte-identical** while having opposite reserve and release roles
and *different release messages* — so a receiver cannot tell, from an authoritative
description of the interior, whether it is the allocator or the consumer. The
same is true of a `PULSED` lane: publisher and claimant are opposite roles over
identical bytes.

| `discipline` | `producer` names | the other end |
|---|---|---|
| `CONSTANT` | who wrote it before the grant — **always `HOST`**, since the host writes before it grants | reads, forever |
| `OWNED` | **not applicable**; ownership alternates and the handoff is a control-plane request/reply (§5). Set it to the end that writes the *initial* value, which is the host | — |
| `PULSED` | the **publisher** (§6.4) | claims and consumes (§6.5) |
| `LOANED` | the **allocator**, which is also the writer (§7) | reads, then **releases** |
| `STAGED` | the host, which fills each volume first (`processing_stages.md` §4) | stages take turns writing it in place; the turn word, not `producer`, says who holds it |

**For `LOANED` this field decides which release message applies**, and that is the
whole reason it exists:

- **`producer = HOST`** — the host reserves and writes, the plugin reads and sends
  **`ReleaseDeposit`**. This is the data ring (`V`) and the export segment (`X`).
- **`producer = PLUGIN`** — the plugin reserves and writes, the host reads and
  sends **`ReleaseLoan`** (`control_plane_protocol.md` §5.6). This is the reader
  arena (`G`), the panel image and the overlay arena, bulk pushes and chunks
  alike.

**A release naming no outstanding reservation is a `ContractViolation`**, in
either direction, and it is worth saying because the alternative — ignoring it —
is what an implementation reaches for. The peer is asserting it is done with
bytes the allocator never lent it, which means one of the two has lost track of
the arena; a second, *correct* release for the same range would then be
indistinguishable from this one. Report, isolate the region, keep the session.
This is separate from a release naming the wrong **side** (§3.1): that one is
about the direction, this one about the range.

**`UNSPECIFIED` is a grant failure**, not a default. There is no honest default:
a deposit ring is host-produced and an overlay segment is plugin-produced, so
whichever way a default fell it would be silently wrong for the other, and
"silently wrong about who releases" is a leak in one direction and a
use-after-reuse in the other. **A structural bound beats a validated one**
(`plugin_principles.md` 8's corollary) and this is the cheap end of it: zero is
not a value, exactly as `zero_id` is not an identity elsewhere in these documents.

**It is `malformed`, and the rule that decides it covers every enum here.**
`unsupported` means *this peer cannot use it at all*, which is the honest answer
when a newer host names a discipline or a purpose this build does not know — a
value that exists and is not understood. **Zero is not such a value.** It is a
required field the sender did not set, so re-granting the same thing fails the
same way, which is exactly `malformed`'s definition. So:

- **an unknown NON-ZERO `discipline` or an unrecognized `purpose` is
  `unsupported`** — a peer that may be older than the grant;
- **a zero `discipline`, `producer` or `purpose` is `malformed`** — a sender
  that omitted a required field, in either direction.

Without that split an implementation has to guess, and two implementations
guessing differently disagree about a grant while agreeing about everything else
in this document.

**A segment mixes producers freely**, and the overlay segment is the case that
needs it: a plugin-published `PULSED` live lane beside a plugin-produced `LOANED`
bulk arena, in the same object as any host-produced region a future purpose wants.
`producer` is per region, never per segment.

**But AT MOST ONE region in a segment may be plugin-produced and `LOANED`**, and
a grant with two is `malformed`. That single region is the plugin's **arena**:
everything the plugin produces into that segment — a panel image, an overlay's
bulk geometry, a reader's deposit — reserves from it, and §5.6's shared cursor is
what makes several writers into it safe.

**A plugin that genuinely needs two arenas is granted two segments**, which
`purpose` already distinguishes. Segments are cheap; a second way to name a
region is not.

**`subject` names WHICH contribution the segment serves**, by its manifest
`id`, and it is what keeps the rule below from moving the ambiguity instead of
removing it. `purpose` says what kind of segment this is; `subject` says which
one. Two `O` segments are otherwise indistinguishable — same purpose, same
region shape, and `id` deliberately non-binding — so a plugin with two overlays
would be handed two grants and no way to tell which overlay each serves, which
is the same ambiguity one level up.

**Whether it is required is a property of the PURPOSE, not of how many
contributions a plugin declared**. Stated the other way it contradicts itself:
a plugin with two exporters would receive an `X` grant that one rule permits
to be empty and the next rejects for being empty.

| `purpose` | `subject` | why |
|---|---|---|
| `O`, `P` | **required, non-empty** | the segment IS the contribution's — one overlay's lane and arena, one panel's arena — and two of them are otherwise indistinguishable |
| `G`, `V`, `X`, `S` | **required to be EMPTY** | the segment serves an *exchange*, not a contribution, and the exchange already carries its own identity: a reader handle, a `ReadDataVolume` reply, an export handle, the `StageArm`s naming a stage ring (`processing_stages.md` §4) |

The division is between a **binding** and an **exchange**. An overlay's segment
outlives any one message and is the only thing that says which overlay a lane
belongs to; a reader's deposit ring is a session arena that every open reader
deposits into, and an export segment belongs to one `ExportOpened.handle` that
names it already. Adding `subject` to the second kind would be a second name for
something already named, which is the failure §8 avoids by making `id` mean
nothing.

**A receiver rejects a violation of that table as `malformed`** — an empty
`subject` on `O` or `P`, or a non-empty one on `G`, `V`, `X` or `S`. Both directions,
because a non-empty `subject` where none is defined is a sender asserting a
binding this document does not have, and a receiver that ignored it would be the
one to guess when a later version gives it a meaning.

A field on the segment rather than a binding meaning for `Region.id`: making
`id` load-bearing for this one case is a change to its specification wearing the
clothes of a convention (§8), and the same question arrives for a plugin with
two panels — so the answer belongs to the segment, not to one discipline's
region.

**AND AT MOST ONE region in a segment may be plugin-produced and `PULSED`**, for
the identical reason and with the identical remedy: a plugin publishing into a
live lane must know which lane is its own. A grant declaring two is
`malformed`, and **a plugin with two overlays is granted two `O` segments.**

**Each alternative re-introduces a key this document refuses.** Naming lanes by `id` is a change to `id`'s specification
wearing the clothes of a convention (§8: *"never load-bearing"*). Naming them by
manifest declaration order makes `index` a contract it is not — `index` is dense
so that a `D` mask bit names a region by position, which is a property of the
mask, not a promise about what the host put where. An in-mapping
directory would be a second source for what the region table already says
(`shared_memory_principles.md` E3).

**The cost is per-session and not per-frame**, which is what makes this the
right trade rather than merely the tidy one. A plugin with N overlays takes N
grants instead of one: N mappings, N 32-byte preambles, N acknowledgement round
trips — all at session setup, all bounded by the manifest, and none of it on the
publish path. The bytes a lane carries do not move, are not copied, and are not
re-laid-out; a publish still writes into its slot exactly as before. The
per-session memory budget (`control_plane_protocol.md` §9) counts the same total
either way. **Correctness here costs setup latency and no throughput at all**,
and buying an ambiguity back for that would be the wrong direction.

Two general rules are behind this, and both are worth applying to the next
question of the same shape:

- **Prefer an invariant the receiver checks once, at install, over a lookup it
  performs at every use.** A plugin producing bytes must know which region is its
  own. Answering that per push needs a key, and every available key is weaker
  than the question: `id` is *"not required to be unique and never
  load-bearing"* (§8), `purpose` is per segment rather than per region, and index
  order is not a contract. Making the ambiguity **impossible to grant** removes
  the lookup instead of strengthening it, and it fails at the moment a host bug
  is cheapest to find.
- **Do not make a field load-bearing that this document says is not.** A rule
  that needs `id` present, unique and authoritative is a change to `id`'s
  specification wearing the clothes of a convention — and an SDK that adopts it
  quietly diverges from one that reads §8 literally, which is exactly the class
  of disagreement these documents exist to prevent.

**Two cross-checks follow, and both are cheap and worth doing** (§8):

- **A region the receiver produces must be mapped writable.** `producer = PLUGIN`
  with a read-only `perms` is a contradiction the plugin can see at install time,
  and finding it then beats finding it at the first write.
- **So must a `PULSED` region it CONSUMES, and that one is easy to miss.** A
  reader claims by CAS (§6.5 step 3), which *writes* the state word, so a
  host-produced Pulsed lane needs `PLUGIN_WRITE` exactly as a plugin-produced one
  does — the discipline requires write access from both ends, and `producer` says
  who writes the payload, not who touches the region. **A `PULSED` region under a
  read-only grant is `malformed`**, whichever end produces it. The failure this
  prevents is not a wrong answer: a receiver that maps read-only and then claims
  takes a fault on the first hint, at drag rate, in a plugin that validated
  everything the document asked it to.
- **A release naming the wrong side is a `ContractViolation`.** A `ReleaseDeposit`
  for a `producer = PLUGIN` region, or a `ReleaseLoan` for a `producer = HOST`
  one, is the peer asserting a role the region table denies it. Report, isolate,
  keep the session — the write itself is undetectable, but the release is not, and
  it is the one that corrupts an allocator's bookkeeping.

This may ride the gRPC exchange a grant already has, or go standalone ahead of
it (`control_plane_protocol.md` §8.12 — a data-segment grant rides `DataVolume`;
an export grant is installed standalone ahead of its first `ExportChunk`, and
so is a data-segment grant whose first descriptor is a `MaskVoxels`, which has
no field to ride); either way the receiver holds a descriptor into the segment
until both halves are in.

**An unknown `purpose` or `discipline` fails the grant** (§8) — reported, and the
host may re-grant. That is the opposite posture from the doorbell, where an
unknown opcode is skipped and counted, and the asymmetry is deliberate: an
acknowledgement means *installed*, and a peer cannot claim to have installed
something it does not understand. There is a reporting path here and none on the
doorbell.

**`purpose` is HOST-DEFINED, and a receiver's recognized set is the set it
implements.** There is no published vocabulary, nothing to configure, and no
negotiation. A receiver that does not implement a purpose fails that grant
`unsupported` and keeps the session — by not implementing it, which is what code
already is.

**Why there is nothing to negotiate.** Nothing here is installed on demand: the
host's capabilities are all resident all the time, both ends run in userspace and
are trusted equally, and so there is no boundary across which an exchange of
vocabularies would decide anything. A plugin already asks for what it wants —
volumes through `ReadDataVolume`, an arena by declaring an overlay contribution —
and **it asks in vocabularies this specification and the manifest schema already
fix**. `purpose` is not one of them: it appears in exactly one message,
`InstallSegment`, host→plugin, as the label on what the ask produced. Publishing
that label could not change what a receiver does with a grant, because a
receiver's ability to honor one is a property of its code and not of a list it
was handed.

The only thing such a negotiation could ever have bought is a way to avoid
versioning old plugins out, and that is `control_plane_protocol.md` §3.2's job:
a major bump breaks and a minor bump does not. Solving it per capability instead
is the same problem with more moving parts.

**Host-defined is not undefined, and the difference matters to a third party.**
Having no negotiation removes a *runtime* exchange, not the obligation to say
what the strings are: an independent SDK still has to know which literal names an
overlay arena and what accepting it promises. So vizi's set is specified here,
normatively, and a host adding one adds it here:

| `purpose` | produced by | the segment is | accepting it promises |
|---|---|---|---|
| `G` | **plugin** | the reader's deposit: a reader writes the volume it decoded and the host consumes it | to reserve from the arena, and not to reuse those bytes until `ReleaseLoan` names them |
| `V` | host | host-written volume data a plugin asked for (`ReadDataVolume`) | to read the deposit and return it with `ReleaseDeposit` |
| `O` | **both** | overlay: the plugin's published Pulsed live lane beside its bulk arena — **one of each per segment** (§3.1), so a plugin with two overlays holds two `O` segments | to publish geometry into the lane, and to reserve bulk from the arena under the loan rules |
| `P` | **plugin** | panel: the plugin's arena for widget images | to reserve image bytes and hold them until `ReleaseLoan` |
| `X` | host | a sealed export segment the host writes and the plugin reads | to read the chunk and write its artifact |
| `S` | host, then each stage in turn | a processing stage's ring: `STAGED` slots the host fills and every stage in a chain edits in place, **one mapping granted to every stage** (`processing_stages.md` §4) | to touch a slot only while its turn word names this stage, and to hand it back with the turn word and a `D` |

**The producer column is the load-bearing one**, and `G` against `V` is why it
is in the table: both carry volume voxels and they run in opposite directions,
so a receiver that reads the two rows as one gets the release message backwards
— `ReleaseLoan` for a host-produced region, or `ReleaseDeposit` for a
plugin-produced one, which §3.1 makes a `ContractViolation`.

A receiver that does not implement one of these fails that grant `unsupported`
and keeps the session. **That is the whole of the compatibility story**: a host
that adds a purpose does not break an SDK that predates it, and
`control_plane_protocol.md` §3.2 governs when a change is breaking at all.

Two consequences an implementer needs, both cheap:

- **A receiver matches the whole string, exactly.** No prefixes, no case folding,
  no parsing a structure out of it. A `purpose` that looks like it encodes
  something is a coincidence a later host is free to break.
- **A host that adds a purpose needs no announcement.** An SDK that does not know
  it fails those grants and keeps the session, which is §8's ordinary path. An
  SDK is never required to refuse to start over a purpose.

### 3.2 What a `PULSED` lane's bytes are

A lane's slot is `slot_bytes` long and the region's `epoch_offset`, when
present, gives the `(view_id, epoch)` header's position **within each slot**
(§2). Where that leaves the payload has the one answer that needs no
negotiation:

- **`epoch_offset` is `slot_bytes - 8` for a pane-associated `PULSED` lane**, so
  the header occupies the LAST eight bytes of every slot and **the payload
  always begins at slot byte 0**.
- **A lane that is not pane-associated declares no `epoch_offset`** and its
  payload occupies the whole slot.
- **The published length covers the payload and is bounded by the payload's
  capacity**, which is `slot_bytes - 8` with a header and `slot_bytes` without
  one. §6.5 step 4 is where a reader checks it.

**The header goes at the END rather than the start, and the reason is §6.5.**
Putting it first would move the payload to slot byte 8, and §6.5's read
algorithm copies `payload[i][0..length)` from byte 0 — so every implementation
would have to branch its copy origin on whether the region is pane-associated,
and one that followed §6.5 literally while honoring a byte-0 header would read
eight bytes of header as payload and run eight bytes into the next slot. At the
end, the copy origin never moves and the only thing a header changes is the
maximum, which is one clause in one step.

Fixing the offset rather than leaving it free is deliberate. `epoch_offset` is
declared per region precisely so a *host* can put the header where a payload
format has room, and for a lane whose payload is the plugin's own encoding there
is no such place — so a host choosing a value in the middle would be asserting
something about a format it does not define. The end of the slot is the only
position that is correct without knowing the payload. `slot_bytes` is `u64`
-aligned for a granted lane, so the header is aligned too. A receiver still
reads the declared field rather than assuming it.

### 3.3 What a live overlay lane CARRIES

§3.2 settles where the bytes are; this settles what they mean, and without it a
correct shared-memory implementation still cannot interoperate — two peers can
agree on every offset and disagree about the message.

The rules, which are what the reference encoder does:

- **The payload is a serialized `OverlayUpdate`** (`vizi_plugin.proto`), and
  nothing else. **No envelope, no framing, no length prefix**: the slot's
  published `length` is the message's length, which is the whole reason the
  length lives in the state header rather than in the payload.
- **Every `GeomBuffer` in it MUST be `inline`.** A `segment` source is refused by
  the sender and is a `ContractViolation` at the receiver, `where` =
  **`live_geometry_not_inline`**, with the update not applied. This is not a
  convenience: a Pulsed value is consumed at a time neither end chooses, so a
  descriptor inside one would name a loan whose reservation the sender has no
  way to keep alive until the read happens. **A live value is self-contained or
  it is not a value.**
- **`OverlayUpdate.overlay_id` MUST equal the segment's `subject`.** Redundant
  on purpose, and the redundancy is the check — the same shape as `region`
  beside `offset` on a release (`control_plane_protocol.md` §8.12): two fields
  that must agree turn a mis-published lane into something *detected* rather
  than something *performed*. A disagreement is a `ContractViolation`, `where` =
  **`lane_subject_mismatch`**, and the update is not applied — neither name is
  believed over the other, because there is nothing to choose between them and
  applying either is how a mis-published lane becomes a mis-drawn one.
- **`OverlayUpdate.revision` is the OVERLAY's revision and is not the slot's.**
  Two monotonic counters meet here and they count different things: §6.3's
  revision orders *publications of a buffer* and belongs to the discipline,
  while this one orders *versions of an overlay* and is what a late-arriving
  gRPC `OverlayUpdate` is compared against. A receiver that conflated them would
  drop a valid update whenever the two ran at different rates, which they do.

**A payload larger than the slot's capacity is not published.** The sender fails
its own call rather than truncating or splitting: §6 has no continuation and a
partial `OverlayUpdate` is not a smaller one. Capacity is §3.2's — `slot_bytes`
less the pane header where one is declared.


---

## 4. Disciplines

| Discipline | Shape | Handoff | Re-checking the region is safe |
|---|---|---|---|
| Constant | written before the grant, immutable | none | trivially |
| Owned | one value, one owner at a time | gRPC request/reply | **no** — the read *is* the handoff |
| Pulsed | N ≥ 3 buffers, claim by CAS, consume once | in-band, §6 | yes |
| Loaned | a byte ring of variable-size deposits | `ReleaseDeposit` or `ReleaseLoan`, per `producer` (§3.1) | yes |
| Staged | one volume slot, edited in place by turns | an atomic turn word in the slot, hinted by `D` (`processing_stages.md` §6) | yes: reading the turn word claims nothing |

**Every row above describes a role, not an end**, and which end holds which role
is `producer` (§3.1) rather than a convention. The shipping cases happen to run
one way — the host deposits, the plugin publishes overlay geometry — and reading
the direction out of those instead of out of the record is how the same bytes
come to have two incompatible release protocols.

**A `D` bit naming a Loaned region is counted and ignored**, not an error. It
does not violate condition (e) — re-checking is safe — but a ring carries no
in-band new-deposit marker, and a peer learns of a deposit from the gRPC message
that names it. There is nothing for the hint to accelerate.

The last column is `doorbell_D_notification.md` §3's condition (e), and it is a
statement about *re-scanning* rather than about whether a value survives being
read. Pulsed values do not: see §6.

**Passing (e) is necessary and not sufficient for a `D` bit to be worth
anything**, and the two must not be collapsed. Constant and Loaned both re-check
safely and both earn nothing — Loaned because a ring carries no in-band
new-deposit marker, Constant because nothing ever changes — so "safe" and
"permitted" are different questions with different answers.
**`doorbell_D_notification.md` §3's table is the single disposition for every
row**, including the two this section explains; this one gives the reasons and
that one gives the behavior.

A segment routinely mixes disciplines. The overlay segment is the standard case:
Pulsed regions for drag-rate geometry plus a bulk region for gRPC-pushed geometry
too large for a slot.

---

## 5. Constant and Owned

**Constant** is written before the grant and never again. Nothing to claim,
nothing to release, no state word.

**Owned** holds one value with one owner at a time, handed over by a gRPC
request/reply. The read *is* the handoff, so re-reading is not safe and a `D`
bit naming an Owned region is an error at this layer — detected here, because the
doorbell cannot see disciplines. It is reported (§8) and the hint is ignored; it
is not a session fault.

**Reporting it does not quarantine the region.** The hint was wrong; the memory
was not, and the region's own handoff is unaffected. Quarantine is for proven
corruption or proven interference (§6.2, §6.5) — turning a sender's mistake about
*which channel* to use into the loss of a working region would let one bad bit
disable a handoff that never went near it.

**This document does not define Owned's handoff, and an implementation must not
invent one.** "A gRPC request/reply" names a shape and not an exchange: it does
not say which message transfers ownership to the plugin, which transfers it back,
what range within the region a transfer covers, or how a replayed transfer is
recognized. Until it does, **every application protocol that uses an `OWNED`
region defines those four things for itself**, and a generic SDK's obligation
stops at exposing an explicit authorisation boundary — a call the embedding
control plane makes when its own exchange actually hands the range over.
Ownership is not discoverable from the region's bytes, and an SDK that polled an
Owned region would be inventing the very handoff this paragraph says is missing.

---

## 6. Pulsed

A single-writer, **single-reader, consume-once** handoff at drag rate. This is
`plugin_principles.md` 5 exactly — when you give it, it is gone until it is given
back — and not a register that retains its value.

Concurrency is bounded by the claim bits, which are mutexes and not reference
counts: a second reader's claim simply fails. Single-writer/multi-reader wants a
different design (validate-after-copy, or a fixed pool of permits) and is not
reachable by changing what a release stores.

### 6.1 Layout

Interleaved per region — header immediately followed by that region's payload
buffers, so one region is one contiguous span:

```
region.offset + i*16          state[i]    u64, ATOMIC
region.offset + i*16 + 8      length[i]   u64, ordinary
region.offset + N*16 + i*S    payload[i]  S bytes        (N = buffers, S = slot_bytes)

region.extent = N*16 + N*S
```

`region.offset` MUST be `u64`-aligned, and `region.extent` MUST equal
`N*16 + N*S`.

**Those are install-time checks and they fail the GRANT, not the region.** §8
requires them before the acknowledgement, and there is nothing to quarantine
before an install: a partially installed segment is not an outcome this protocol
has, because an acknowledgement means *installed* and a peer cannot half-claim
that. So a misaligned or inconsistent `PULSED` region is `malformed` for the
whole grant, reported on the acknowledgement, and the host may re-grant.

**Quarantine is for what only running can reveal** — an impossible state word
(§6.2), an invalid published length (§6.5), a failed release CAS. Those arrive
with the segment already installed and acknowledged, where there is no grant
answer left to give and isolating one region is the proportionate response. The
two are not alternatives and an implementation should not reach for the second
where §8 asked for the first.

### 6.2 The state word

One `u64`, accessed only through atomic operations:

| bits | meaning |
|---|---|
| 0–61 | revision |
| 62 | `WRITE` claimed |
| 63 | `READ` claimed |

`0` is EMPTY. **Revision 0 is never valid**, so `state == 0` is unambiguous.

Two states are **impossible** and mean the region is corrupt or a peer is broken:

- revision 0 with either claim flag set;
- both claim flags set at once.

On either, report once and **quarantine the region** — do not clear the word,
because there is no state here the receiver provably owns (C2).

`length[i]` is an ordinary, non-atomic `u64`. **Whoever owns `state[i]` owns
`length[i]` and `payload[i]`**; that is the whole of the ordering contract, and
it is why the claim is a CAS rather than a write order.

### 6.3 Revisions

Per region, allocated by the writer, **starting at 1 and incremented by exactly
one**. Never reused. On reaching `2^62 - 1` the writer stops publishing and
reports — the space is not wrapped, because a wrapped revision would make an old
buffer look newer than a new one and the reader's `> last_seen` test is the whole
of its ordering.

**"Reports" here means a local diagnostic and there is no wire route, because the
bound is structural rather than operational.** A publish attempt burns a revision
whether or not it claims a buffer (step 1 allocates before step 4 claims), so the
ceiling is reached after 2^62 attempts — at a sustained 1 kHz, longer than the age
of the universe by ten orders of magnitude. It is therefore not a condition a
session recovers from and not one a peer needs told about: there is no
`ViolationSite` for it, and inventing one would put a value on the wire that can
never be sent. **The absence is the rule, not a gap**, and an SDK that keeps a
local counter has implemented it correctly and completely. What a peer owes is the *behavior* — stop publishing rather than
wrap — and what an SDK owes is that the ceiling is visible in its own counters,
since a region that will never publish again otherwise looks exactly like a
writer that went quiet.

### 6.4 Publishing

1. Allocate the next revision.
2. **Acquire**-load all `N` state words. A buffer is READY when its state is
   non-zero with no flags.
3. Build the candidate list, in order:
   - EMPTY buffers, in rotation starting at `revision % N`;
   - then READY buffers oldest-revision-first, **excluding the newest READY**.

   The exclusion is what keeps a durable value visible to a reader that has not
   arrived yet. It is the reason `N ≥ 3`: newest-READY held back, one buffer a
   reader may be holding, one free to write.
4. For each candidate, CAS `prior → revision | WRITE`. The first success claims
   it.
5. If nothing is claimed, rescan; **after 8 attempts, drop this update and
   return.** That is coalescing, not an error and not a stall (C3) — the value
   dropped is the one the next publish would have superseded, and the newest
   READY is untouched.
6. With the claim held: write `payload[i]` **and, where the region declares
   `epoch_offset`, the pane pair `(view_id, epoch)` at it** (§3.2), then
   `length[i]`, then **release**-store `state[i] = revision`. The store is the
   publish, and it covers the header as it covers the payload: a header
   written after the store is what a fast reader would take with a new
   payload and the previous publish's pane — the stale-frame draw the header
   exists to prevent (§6.5).
7. On any failure between 4 and 6, CAS `revision | WRITE → 0` to release. **A
   failed release CAS is fatal to the region** — it is proven interference (C2).

### 6.5 Reading

The reader keeps one `last_seen` revision per region.

1. **Acquire**-load all `N` state words, rejecting the impossible states of §6.2.
2. Choose the READY buffer with the greatest revision **strictly greater than
   `last_seen`**. If there is none, there is nothing new; stop.
3. CAS `revision → revision | READ`. On failure, rescan; **after 8 attempts,
   stop** — a writer is churning, and the next scan will find a newer value
   anyway.
4. With the claim held, read `length[i]`. Validate `0 < length ≤ capacity` and
   that `payload[i] + length` is inside the segment, where **`capacity` is
   `slot_bytes - 8` when the region declares an `epoch_offset` and `slot_bytes`
   when it does not** (§3.2: the pane header occupies the last eight bytes of
   each slot, and the published length covers the payload only). A length
   outside that range is corruption: release the claim by exact CAS, report, and
   quarantine the region.
5. Copy `payload[i][0..length)` — **from slot byte 0 either way**, which is why
   §3.2 puts the header at the end rather than the front.
6. Release by exact CAS `revision | READ → 0`, **consuming the value**. A failed
   CAS is proven interference: report and quarantine, and never clear the word.
7. Set `last_seen = revision`.

`last_seen` is belt-and-braces given step 6 empties the buffer, and it is what
keeps the reader correct if the discipline ever gains a second consumer.

### 6.6 Memory ordering

Acquire on every state load; AcqRel (with Acquire on failure) on every CAS;
Release on the publishing store. Nothing else in the region is atomic, and
nothing else needs to be: ownership of the state word is what makes `length` and
`payload` stable.

---

## 7. Loaned

A byte ring of **variable-size** deposits — not a slot array. A deposit is
reserved by length, and identified for release by the `(offset, len)` pair it was
given.

- **`offset` is MAPPING-relative on the wire** — the same number the descriptor
  carried, echoed back unchanged. The ring's own modular address is
  region-relative internally, and `region.offset + ring_address` converts at the
  allocator's boundary; a release never carries the internal form. Segment-
  relative was chosen over region-relative on which mistake each produces: a
  forgotten conversion in the region-relative direction yields a *small but
  valid* offset that lands inside another reservation and corrupts the allocator
  silently, whereas one in this direction lands outside the named region and is
  caught by the `region` cross-check (`control_plane_protocol.md` §8.12).
  Worked: a segment granted at `size = 8192` with region 2 at `offset = 4096`,
  and a deposit the host describes as `(mapping 4160, len 300)`, is read at
  mapping byte 4160 and released as
  `ReleaseDeposit{segment_id, region: 2, offset: 4160, len: 300}` — the offset
  is echoed, the region index is the consumer's, and `len` is unchanged because
  it is the requested length either way.
- **`(offset, len)` is reusable, so the DELIVERY of a deposit must be
  exactly-once.** Once released, the allocator may hand the same pair out again,
  and it carries no generation — so a consumer cannot tell a replayed old
  descriptor from a new reservation at the same address, and neither can the
  allocator tell a duplicate release from a correct one for the new deposit.
  Nothing in this document can fix that from the inside; it is a requirement on
  the channel that carries the descriptor. `control_plane_protocol.md` §5's
  classes are where that is settled, and a protocol that delivers a deposit
  descriptor at-least-once must define replay handling **before** it may release
  by this key.
- **`len` is the requested length, not the padded one.** Reservations are padded
  up to a 64-byte alignment internally; the release key is what the caller asked
  for.
- **`slot_bytes` is RESERVED on a `LOANED` region in `1.0`, and no end reads
  it.** The alignment is fixed at 64 by the rule above, not carried by the field:
  a consumer never computes with padding because the release key is the requested
  length, and an allocator pads to 64 whatever the field says. So a sender SHOULD
  send 64 or 0, **no receiver may reject a grant over its value** — refusing a
  zero turns a field nothing reads into a session-breaking one — and **an
  allocator uses 64 regardless**, rather than treating an unset field as a reason
  to fail. A later version wanting a variable alignment needs the allocation and
  release contract §7 says does not exist for other values; it can give the field
  a meaning then, because this version has none to give.
- **Release is the `producer`'s mirror** (§3.1): the *consumer* releases, so a
  host-produced ring is released with **`ReleaseDeposit{segment_id, region,
  offset, len}`** and a plugin-produced one with **`ReleaseLoan`** carrying the
  same four fields (`control_plane_protocol.md` §5.6, §8.7). Same key, same
  padding rule, opposite direction.

**`LOANED` names a discipline, not one data structure, and the two directions
do not have the same one.** The host's is a ring, described below. The
plugin-produced arena (`control_plane_protocol.md` §5.6) is **not required to
be**, and specifying one would be inventing a constraint: a ring's whole benefit
is amortised reclamation under a stream of allocations, and the plugin's traffic
is a handful of live loans against a fixed extent. What binds a plugin-produced
arena is only this:

- **it allocates from ONE cursor per arena** — one per plugin-produced `LOANED`
  region, shared by every writer into that region (§5.6) — so no two writers are
  handed the same byte. (A segment declares at most one such region, §3.1, so
  the cursor is also the segment's.);
- **it may not reuse a byte range until its `ReleaseLoan` arrives**;
- **it fails its push and counts it when it cannot allocate** — never blocks,
  never reuses.

A bump cursor that rewinds only when nothing is outstanding satisfies all three
and is the simplest thing that does. So does a ring. Neither is required.

Two properties of the **host's ring**, both observable and neither obvious:

- **Reclamation is FIFO.** A release is recorded immediately, but the ring's tail
  only advances while the *oldest* record is released. A peer sitting on one old
  deposit blocks reclamation of everything behind it — a head-of-line block on
  memory rather than on messages.
- **Exhaustion doubles rather than fails.** The replacement ring is at least twice
  the capacity, so grants are logarithmic in total bytes rather than linear in
  reads, and a peer that releases promptly may never trigger a second grant.

Sizes are chosen for actual payloads, which is why this is not a slot array:
payloads range from a plane to a volume, and fixed slots would size every one of
them for the largest.

---

## 8. Acknowledgement and failure

**As specified in `doorbell_M_memory.md` §7**, a grant is acknowledged once,
over gRPC, when *mapped ∧ preamble validated ∧ meaning applied*; the doorbell's
`M` and this document's `InstallSegment` are two halves and either may arrive
first, so the *success* ack is emitted by whichever arrives second. **What this
section owns is the per-segment state machine** — which answers are terminal,
what a duplicate is answered with, and what a failure records.

**As `doorbell_M_memory.md` §7 specifies, a failure is reported as soon as it is
known**, not held for the other half — the missing half cannot rescue a mapping
that did not work.

**Entering *failed* releases every resource the attempt acquired**, and this is
not optional bookkeeping. A mapping that succeeded before a later check failed —
the preamble not checksumming, a region table that does not fit, an unknown
`purpose` — **must be unmapped, and its descriptor closed, before the failure is
reported.** Memory cannot be withdrawn (§1 of `doorbell_M_memory.md`), so a
mapping retained past a failed install is an invisible allocation the host does
not know exists, and the host's response to a failure is to re-grant: without
this rule, each retry adds another. The one case that is *not* a leak is a
duplicate, whose descriptor is closed without ever being mapped.

### Per-segment state

"Acknowledged once" is a statement about depth, not about how many messages are
emitted — a duplicate grant is answered again (`doorbell_M_memory.md` §7). What
makes that safe is a state machine per `segment_id`, with **terminal answers
replayable**:

| state | reached by | a duplicate `M` or `InstallSegment` |
|---|---|---|
| **pending** | one half arrived | hold it; still no answer to give |
| **installed** | both halves arrived and validated | re-send the same success ack |
| **failed** | either half failed | re-send the same failure ack |

Both terminal states are **sticky and idempotent**: the answer is computed once
and replayed verbatim, so a lost ack costs a re-send and never a re-install.

**A duplicate is replayed only if it is identical.** Match on `segment_id`, then
compare the record's 112 bytes: identical means a repeat of the grant already
recorded, so replay the answer and count it. **For `InstallSegment` the
comparison is field-by-field, never byte-by-byte** — protobuf encoding is not
canonical, so two encodings of one message can differ in bytes and two different
messages can be a re-serialization apart; comparing bytes would report a
conflicting grant for a message that says the same thing.

**The unix descriptor is not in those 112 bytes**, so byte-identity alone cannot
establish that a duplicate names the same memory. It does not need to: a
`segment_id` is never reused (§3), so a record with identical bytes and a
descriptor for *different* memory is a host defect that the never-reuse rule
already excludes, and the duplicate's descriptor is closed without remapping
(`doorbell_M_memory.md` §7) — the recorded answer describes the mapping that was
actually installed, which is the one the acknowledgement's identity proof came
out of. What a receiver must not do is *remap* on a duplicate, which would make
the ack describe a mapping the host never learned about. **Anything else is a conflicting
grant** — a second, different grant of a `segment_id` that is never reused (§3) —
and it is a grant failure in its own right, reported and not allowed to replace
the recorded one. The distinction matters: a clean
refusal means nothing arrived (`doorbell_protocol.md` §6), so every duplicate that
reaches a peer is a host defect, and silently re-acking a *different* grant would
hide the one kind that cannot be recovered from.
That settles the cases the two-halves rule otherwise leaves open:

- **a duplicate `M` while pending** — close the duplicate descriptor, do not
  remap, stay pending;
- **`InstallSegment` fails before any `M`** — go straight to *failed* and report
  now, against the `segment_id` the message already carries; there is nothing
  else to name, and nothing else is needed, since the acknowledgement keys on
  `segment_id` alone (`doorbell_M_memory.md` §5). A later `M` for that
  `segment_id` closes its descriptor and replays the failure;
- **the mapping fails, then `InstallSegment` arrives** — already *failed*;
  applying meaning to a mapping that does not exist is not attempted, and the
  failure is replayed;
- **any half arriving after a terminal state** — never re-enters *pending*.

A segment id is never reused (§3), so a state entry is per segment for the life
of the session and needs no eviction rule beyond session teardown.

Cross-checks before acknowledging:

- enough room for the declared regions (§2). The `size ≥ 32` floor is
  record-local and the transport checks it (`doorbell_M_memory.md` §7); only the
  *sufficiency* half needs the region table, and that half alone is this layer's;
- the mapped object's size matches `size`;
- the access obtained matches the declared `perms`. **The peer maps with exactly
  the declared permissions and does not probe for more**; failing to obtain them
  is a grant failure. A mapping that *could* have been more permissive is not an
  error — `perms` is a declaration and enforcement lives in the fd's open mode or
  the section's `PAGE_*` protection;
- the preamble checksums and its `segment_id` matches;
- every region is in bounds, non-overlapping, correctly aligned for its
  discipline, and its `extent` agrees with `buffers` and `slot_bytes`;
- `purpose` and every `discipline` are understood (§3).

### The receiver's validation matrix

**A receiver rejects on this table and not on inference from a field's
description.** The list above says *what* to check; this says which fields a
receiver may rule on at all, because the alternative — reading a field's prose
and deciding for oneself whether a value is legal — is how two implementations
come to disagree about a grant while agreeing about every rule.

| field | the sender MUST | the receiver | class if it fails |
|---|---|---|---|
| `purpose` | send a host-defined string (§3) | match the whole string, exactly | empty: `malformed`. Unrecognized: `unsupported` |
| `discipline` | send a defined non-zero value | reject zero and reject unknown | zero: `malformed`. Unknown: `unsupported` |
| `producer` | send `HOST` or `PLUGIN` (§3.1) | reject zero and reject any other value | zero: `malformed`. Unknown: `unsupported` |
| `index` | be dense and 0-based across the table | reject a gap, a repeat or an out-of-range index | `malformed` |
| `offset` | be ≥ 32 and place the region inside `size` | check both, and check non-overlap (§2) | `malformed` |
| `extent` | be non-zero | reject zero — a region naming no bytes has no reading, and every discipline's protocol needs at least one | `malformed` |
| `buffers` | be the buffer count for `PULSED` and **zero everywhere else** | reject `< 3` on `PULSED`, and reject non-zero on any other discipline | `malformed` |
| `slot_bytes` | be the payload size for `PULSED`; the allocation alignment for `LOANED`; zero elsewhere | check the `PULSED` extent identity. **Ignore it entirely on `LOANED`, including zero** (§7), and ignore it elsewhere | `malformed` on `PULSED` only |
| `id` | be whatever names the region to the application | ignore. It is not required to be unique and it is never load-bearing | — |
| `subject` (on the grant) | be the manifest contribution id for `O` and `P`, and empty for `G`, `V`, `X` and `S` (§3) | check it against the purpose, **both ways**, and for `O`/`P` check it names a contribution this plugin declared | empty where required, non-empty where forbidden, or naming nothing declared: `malformed` |
| `epoch_offset` | be **absent** on a region that is not pane-associated. When present: exactly `slot_bytes - 8` on `PULSED` (§3.2); on every other discipline, a position with `epoch_offset + 8 <= extent` (§2) | **only where it is PRESENT, and the two disciplines differ.** On `PULSED`: reject any value other than `slot_bytes - 8`, and reject `slot_bytes <= 8`, which leaves no payload beside the header. On `CONSTANT`, `OWNED` and `LOANED`: check it against `extent` and **ignore `slot_bytes` entirely, including zero** — it is alignment there, not a size, and a `LOANED` ring legitimately declares `slot_bytes = 0` | `malformed` |
| `perms` (on the grant) | grant write where the receiver writes | require `PLUGIN_WRITE` for a region the receiver produces **and for any `PULSED` region** (§3.1) | `malformed` |

> **The `subject` check above is conditional on the receiver KNOWING its
> manifest, and a receiver that does not know it TRUSTS the name rather than
> refusing it.**

Nothing on the plugin side of the wire holds the manifest — it is the
application's, and an SDK reaches it only through a hook the application fills
in. So an SDK with no declaration set has two possible defaults, and only one of
them is safe: **trusting checks the shape and takes the name on trust; refusing
fails every `O`/`P` grant as `malformed`, because an empty declaration set names
nothing.** `malformed` is the class that tells the host *not to retry*, so
refusing-by-default kills every conforming session on a host that did nothing
wrong, and does it in the way least likely to be read as an SDK default.

The default is an interoperability matter rather than a matter of taste: with
opposite defaults the same conforming grant is installed by one SDK and
permanently refused by another. **The check is a strengthening available to a
receiver that can perform it, never an obligation on one that cannot.**

**Non-zero `buffers` outside `PULSED` is a rejection**, not a field a receiver
may ignore. The reason is the version rule: one `protocol_major.protocol_minor` implies
exactly one of everything (`doorbell_protocol.md` §3.1a), so there is no
mixed-version traffic for a lenient receiver to be compatible with — and a
sender that sets a field its discipline has no use for is a sender confused about
the discipline, which is the disagreement that later becomes a wrong layout.

**None of these is a doorbell framing fault.** The record was well-formed; the
mapping was not. Each is reported over gRPC as a grant failure and the host may
re-grant at a new index. The same applies to everything this layer detects at
runtime — an impossible state word, an invalid length, a failed release CAS:
report, isolate, keep the session. **A `D` bit naming an Owned region is the one
that reports WITHOUT isolating** (§5): the hint was wrong and the memory was not,
and quarantining there would let a stray bit disable a handoff it never touched.

---

## 9. Retirement and release

Two messages, and the whole of the disposal half:

- **host→plugin `RetireSegment{segment_id}`** — stop using this; release when
  what you already started is finished. `InstallSegment`'s `supersedes` is an
  inline form, atomic with the replacement, and behaves identically in every
  respect below.

**"Atomic with the replacement" means the replacement reaching *installed* —
`GrantInstalled`, the success arm of `GrantAck` — not the message arriving**, and §8's state machine is why the distinction is not
pedantry. A grant arrives in two halves and either may be first, so an
`InstallSegment` carrying `supersedes` routinely lands while its own `M` is still
outstanding. A receiver that retires on arrival has then given up the old segment
for a replacement that does not exist yet — and if that `M` never comes, or comes
and fails its preamble, **the plugin holds neither**, at a moment when the host's
only response to a grant failure is to grant again. So:

- **while the replacement is *pending*, the superseded segment stays live and
  fully usable.** Nothing is retired and nothing is reported;
- **when the replacement reaches *installed*, the superseded segment enters
  *retiring* at that instant**, and §9.1 onwards applies unchanged;
- **if the replacement reaches *failed*, the `supersedes` has no effect at all.**
  The old segment is untouched, and the host — which is being told the
  replacement failed — decides what to do next. A host that wants the old one
  gone regardless says so with `RetireSegment`, which is the message for it.

**`supersedes` MUST NOT name the segment being installed.** It asks a receiver
to retire the thing it is in the middle of installing, which has no meaning to
carry out and no half-measure to fall back on; it is a `malformed` grant failure
for the `segment_id` in the message, reported like any other. Saying so is worth
a line because the naive implementation of "retire, then install" reaches for the
same per-segment lock twice and hangs the receiver rather than failing it, which
turns a sender's typo into a plugin that never answers again.
- **plugin→host `SegmentReleased{segment_id}`** — released, in the exact sense
  §9.2 gives that word.

**The host retains its own mapping until the peer reports released, or the
session ends.** That is free — the memory is alive anyway while the peer holds it
— and dropping early would lose access to data the host may still need.

**Release has no timeout** (B2). A timer reports rather than acts, and an
outstanding-segments count belongs in diagnostics. **Retirement is a request,
never an enforcement** (B1): no host-side code may assume compliance, and a
segment a plugin never releases is a diagnostic line, not a recoverable state.

### 9.1 *Retiring* is a state, and it permits finishing but not starting

"Stop using this" is ambiguous exactly where it matters — a plugin holding a
Pulsed claim or halfway through copying a deposit needs to know whether it may
finish. It may. What it may not do is begin anything new:

| operation | while *retiring* |
|---|---|
| **complete** a held Pulsed claim, and release it | **yes** — required, in fact; see below |
| **finish** reading a deposit or a Constant region already begun | yes — and a retirement waits for a deposit read in progress, as for a Pulsed claim. When the read begins is the endpoint's choice: on the descriptor's arrival (the retirement then waits for a descriptor not yet copied), or when its copy starts (a descriptor retired before then answers its read with an error, never with bytes) — either is conforming, and **reading a segment already released never is**. The host never retires a ring with a deposit out (§9.5), so the two differ only against a host that does |
| **complete** a Loaned reservation already begun: its write, and sending the message that names it | **yes** — a retirement waits for it, as for a Pulsed claim |
| **complete** a `STAGED` turn in progress: write its outcome, hand the slot on and send the `D` (`processing_stages.md` §6) | **yes** — a retirement waits for it, as for a Pulsed claim |
| **send** the releases it already owes (`ReleaseDeposit`) | yes, and before `SegmentReleased` is fine but not required — §9.3 |
| **apply** a release the peer sends (`ReleaseLoan`) | **yes, always.** It is the peer *finishing* with bytes it already holds, and refusing it would strand them for no gain |
| **claim** a new Pulsed buffer, or re-read a lane | **no** |
| **start** a new Loaned reservation | **no** |
| **publish** a new Pulsed value | **no** |
| **start** a `STAGED` turn, even one whose turn word names this stage | **no** |
| **treat a `D` bit naming a region in it** as anything but counted and ignored | **no** — while retiring the whole segment's row is that one, whatever `doorbell_D_notification.md` §3's table would otherwise say |

**A normal retirement produces no error, at either end**. Work
in flight finishes and the release waits for it; work that would *start* on a
retiring segment is an ordinary outcome, never an exception or a logged error:

- **an SDK** starts it in the successor when one is installed (`supersedes`),
  and otherwise drops it, counting the drop at debug level — or, for a panel
  image, keeps it for the panel's next segment (`control_plane_protocol.md`
  §8.4);
- **the host**, receiving a message that names a segment it has retired and
  that the plugin has not yet reported released, drops it quietly and releases
  any loan it carried. It is not a violation: the plugin built it before the
  retirement reached it.

A **counted in-flight use** is what makes "the release waits" enforceable: an
SDK counts a Loaned reservation from `reserve` until every message naming it
has been sent (or it is reclaimed), and a deposit read while it copies, in the
same count as its Pulsed claims — taken **before** reading *retiring*, so either
the operation sees the retirement or the retirement sees the operation. A copy
that is merely under an allocator's lock is not enough: the message naming the
reservation leaves after the lock is dropped.

**The table is about OPERATIONS, and an SDK enforces it where the operations
are.** Every "no" row above names something a plugin does, not something it
obtains, and the two come apart in the API shape SDKs naturally reach for: a
handle to a lane or an arena is fetched once and used many times, so refusing to
*hand out* a new one after `RetireSegment` leaves every handle fetched earlier
working exactly as before. That is the common case rather than the exotic one —
a plugin fetches its handles at install and keeps them — so a receiver whose only
gate is at the handout enforces this section for nobody. The same applies to a
quarantined region (§6.2): quarantine that only refuses new handles isolates
nothing.

**"Retiring" and "released" are different states and the table is about the
first.** *Retiring* begins when `RetireSegment` arrives and the mapping is still
present, which is what makes finishing possible at all; *released* is §9.2's end
state, after the last unmap and the last handle close, and **nothing in the table
is permitted there** — a Constant re-read included. So the Constant row means
"while retiring", not "forever": the bytes are legible until they are not mapped,
and an implementation that keeps answering reads after unmap is reading something
else.

**"A held claim" means a claim THIS endpoint owns**, and that has to be said
because a claim bit is not self-identifying. A receiver scanning state words sees
the peer's claims exactly as it sees its own, so an implementation that answers
"is a claim held?" by looking at memory will block its own release on a claim the
*peer* holds — which the peer has no reason to drop and no way to know is
blocking anything, so the segment is never released and §9's whole disposal path
stops. Track what this endpoint claimed; never infer it from the region.

**Completing a held claim is an obligation, not a permission**, and it is the one
row that is not symmetric. A claim is a mutex (C4), so abandoning one on
retirement leaves that buffer permanently unavailable — and since the host keeps
its mapping until release, the lane is wedged for as long as the plugin lives.
Release the claim, then release the segment.

**A retired segment's regions are still legible; they are simply no longer
live.** Nothing about retirement changes the bytes, so a read already in flight
returns what it would have returned. **A `CONSTANT` region may be read again**
rather than only finished: §4 already says re-checking one is trivially safe,
it has no claim, no release and no handoff, and forbidding a second read would
be a rule with no property behind it. Every other discipline's read is a
handoff, which is why the row above is start-versus-finish for them and not for
this one. This is why the distinction is
start-versus-finish rather than a hard barrier: a hard barrier would require
tearing out a read that is provably safe to complete.

### 9.2 What "released" means, and it is more than `munmap`

**`SegmentReleased` asserts that this process no longer holds anything that keeps
the object alive.** That is more than one call on every platform, and a plugin
that unmaps while retaining a handle has sent a message that is not true:

| platform | what must be gone |
|---|---|
| Linux, macOS | every mapping unmapped, **and the descriptor closed** — including any duplicate made for a worker |
| Windows | every view unmapped **and the section handle closed** |

The descriptor is the half that is easy to miss, and losing it is not a
bookkeeping error: an open descriptor keeps the object alive after the last
unmap, which is `doorbell_M_memory.md` §1's **invisible allocation** arriving by
a third route — the host has been told the memory is gone, so it will not even
appear in the outstanding-segments diagnostic that B2 provides for the case where
the plugin says nothing at all. **A false release is worse than no release.**

**Send `SegmentReleased` after the last of those, never before.** The host may
close its own handle on receipt, and on Windows the section name becomes
reusable once no handle remains — so a release sent early can be followed by a
mapping that no longer names what the plugin thinks it does.

### 9.3 Retirement settles every outstanding release inside the segment

A segment being retired may hold obligations in both directions: deposits the
plugin owes `ReleaseDeposit` for, and — `control_plane_protocol.md` §5.6's
mirror — loans the *host* owes `ReleaseLoan` for. Both are settled by the
segment's own lifecycle rather than drained first, and the two directions are not
symmetric:

- **`SegmentReleased` supersedes every `ReleaseDeposit` the plugin still owes in
  that segment.** Releasing a byte range inside an object that no longer exists
  is bookkeeping with no subject. The host discards the whole ring on release and
  does not wait for a drained tail — which also means the FIFO head-of-line
  property (§7) has an exit: a peer sitting on one old deposit blocks reclamation
  *within* the ring, and retiring the segment ends it.
- **`RetireSegment` asserts that the host has already released every loan it held
  in that segment.** This one is a precondition rather than a consequence, and it
  exists to close a deadlock: a plugin whose arena is full is waiting for
  `ReleaseLoan` before it can allocate, and if it may not send `SegmentReleased`
  until it has finished work it cannot start, neither side moves. Making the
  host's releases a precondition of its own retirement means the plugin is never
  blocked on the peer that asked it to stop.
- **A loan created after `RetireSegment` is still returned, and its
  `ReleaseLoan` may cross the plugin's `SegmentReleased`**. A push
  the plugin built before the retirement reached it names the segment; the host
  drops it quietly (§9.1) and releases its loan, and the plugin, whose claim on
  that reservation ended when it sent the push, may already have released the
  segment. **A plugin ignores a `ReleaseLoan` naming a segment it has released**,
  counting it at debug level: ids are never reused, so released is told apart
  from never-installed, and only the second is `unknown_release` — as is a
  crossing that names no loan the plugin had out when it released the segment,
  or one already returned: that is the host's bug, which a crossing must not
  hide. The host keeps
  returning such loans rather than special-casing them: its loan ledger stays
  balanced by the one rule every drained message follows, and the same crossing
  happens anyway for any loan it releases just as the plugin lets the segment go.

**A plugin does not release a segment it was not asked to release.** Memory is
the host's to grant and to retire, and a unilateral unmap turns every descriptor
already in flight into one that names nothing, with no message saying so. What a
plugin may do instead is **ask** — §9.6 — which is a different act with a
different guarantee. The two exceptions to the rule are already specified and
neither is unilateral: a **failed** install unmaps and closes before reporting
(§8), and process exit ends everything.

### 9.4 Retiring what was never installed

Retirement is defined against §8's state machine, not only against *installed*:

| state when `RetireSegment` arrives | what the plugin does |
|---|---|
| **installed** | §9.1 and §9.2, then `SegmentReleased` |
| **pending** | release whatever half it holds — if a mapping was made, §9.2 applies to it — then `SegmentReleased`. The state is terminal afterwards, so a later `M` closes its descriptor without mapping and a later `InstallSegment` is answered with `SegmentReleased` again |
| **failed** | §8 already unmapped and closed, so `SegmentReleased` immediately, with nothing to do |
| **unknown `segment_id`** | `SegmentReleased`. Ids are never reused (§3), so this is a retirement for something that never arrived, and asserting the end state is both true and idempotent |

**A duplicate `RetireSegment` replays `SegmentReleased`**, on the same reasoning
as §8's replayable terminal answers: a lost message costs a re-send and never a
re-release.

**At session teardown a plugin owes nothing.** Process exit unmaps and closes
everything the kernel knows about, and the host is tearing down its own side
regardless, so `SegmentReleased` for every live segment during the shutdown
window is permitted and pointless. `control_plane_protocol.md` §3.3's window is
for messages the host cannot reconstruct; this is not one of them.

### 9.5 A ring that outgrows itself retires the one it replaced

When a non-current ring drains, retire it; the current ring stays regardless. No
hysteresis is needed, because a non-current ring is **write-closed by
construction** — reservations only come from the current ring, so a superseded
one can only shrink, and "empty" is terminal rather than a moment that might
reverse.

Worst-case retention is then one ring at the high-water capacity: the working set
the peer actually demonstrated.

**`supersedes` names only a segment with nothing outstanding in it** — no loan
the host holds inside it (`control_plane_protocol.md` §8.12's precondition on
retirement), and no deposit the host wrote there that the peer has not yet
released. The peer retires a superseded segment the moment the replacement
installs (§9), which is right for an idle one and wrong for a busy one: a
descriptor the peer has received and not yet copied either holds the
retirement until it is copied or fails its read, as the peer chooses (§9.1) —
so the ring the host meant to drop stays mapped for as long as the peer takes,
or the read the host just answered is lost — and one still on its way arrives
to a segment already *retiring*. So a ring with a deposit still out is replaced **without**
`supersedes`, stays write-closed, and is retired with `RetireSegment` when its
last deposit is released; an idle ring is superseded as below, and stays the
host's fallback until the replacement installs — a replacement that fails leaves
it current (§9). The same holds for a plugin-produced arena: the host names it
only once every loan it held there is back.

**This is the only growth idiom in these documents, and it holds in both directions.** A
segment never changes size; a larger one is granted at a **new** `segment_id`
and the old one retires behind it. That covers the plugin's own loan arena as
well as the host's deposit ring (`control_plane_protocol.md` §5.6), so an
implementation has one mechanism to build rather than two, and the retired id
makes "the old target is gone" checkable instead of remembered. **A chunked
overlay's arena is this idiom driven by the dataset rather than by exhaustion**
(`control_plane_protocol.md` §5.7): the host sizes it from the open dataset's
volumes and, when the dataset changes, grants the new size at a new id
superseding the old — a plugin-produced LOANED region whose size the host
chose, which is why the growth ceiling §9 there measures against a manifest is
not measured on it. **A reader's `G` is this idiom driven by the next open**
(`reader_writer_contract.md` §1.2, H3): the host grants the next reader's arena
superseding the last reader's, so a session carries one `G` at every instant —
the incumbent until the replacement is acknowledged, then the replacement —
and never the two that a grant without `supersedes` would leave installed side
by side, refusing every read on either.

**The replacement is installed BEFORE the retirement**, which is what "retires
the one it replaced" already assumes and is worth stating because the other
order looks equivalent and is not: between a retire and its re-grant the peer
has no arena, and a peer that may not block and may not reuse (§7) can only drop
work for that round trip. Installing first costs one interval of double
residency — the worst case above — and drops nothing.

**"Drains" is the host's observation, and §9.3 gives it a second exit.** A ring
whose tail is pinned by one unreleased deposit never drains on its own, so a host
that only ever retires drained rings retains that one forever. Retiring it
anyway is legitimate — retirement does not require the ring to be empty, and
`SegmentReleased` settles what is outstanding.

### 9.6 `ReleaseRequest` — a plugin asking to be rid of one

**plugin→host `ReleaseRequest{segment_id, reason}`** — *"I have no further use
for this."* A plugin closes a panel, retires a tool, or finishes an export, and
the segment behind it is now dead weight it may not release on its own (§9.3).

**It is a hint, and it changes nothing about the plugin's obligations.** Sending
it does not enter *retiring*: every discipline is honored exactly as before
until `RetireSegment` actually arrives, and a descriptor into that segment
arriving afterwards is read normally rather than rejected.

That is the whole design, and the alternative is why. A plugin that self-retired
on send would have to **reject a message the host sent correctly** — the host may
have a descriptor already in flight naming that segment, and it cannot have known.
The failure would be reported against the host, for a race the protocol created.
Making the request advisory removes the race rather than specifying who loses it.

**The host may retire immediately, later, or never**, and none of those is a
fault:

- **There is no deadline and no escalation.** Nothing is worse off if the request
  is ignored — the plugin is in exactly the state it was in before it asked, still
  mapped, still correct, still serving the segment. A message with no bad outcome
  needs no timer.
- **Silence here is not an unanswered question** (`control_plane_principles.md`
  D4). D4 forbids silence as the *answer* to something that asked; this asks for
  nothing, which is precisely why it is a hint and not a request.
- **`RetireSegment` is the only answer there is**, and it is the ordinary one —
  the host does not acknowledge the request itself, because an acknowledgement
  would say only that a message arrived, which nothing depends on.

**`reason` is diagnostic and the host does not act on it.** It reaches Plugin
Manager and the outstanding-segments line (B2) so a developer can see *why* a
plugin is holding memory, and no further. Acting on it would make a
plugin-supplied, unvalidatable string load-bearing for host memory policy, which
is the host trusting a plugin's account of itself
(`control_plane_protocol.md` §11). A plugin that genuinely cannot proceed reports
that as a fault; it does not encode it in a hint.

**A plugin asks only about a segment it has acknowledged.** While a grant is
*pending* the plugin owes an answer, not a request, and §8's state machine has no
room for a third one racing the first two — finish the install, or fail it, then
ask. A *failed* segment is already unmapped and closed (§8), so there is nothing
to ask about.

**Repeats are legal and idempotent, and a plugin should not schedule them.**
There is nothing to retry: the message is not a request awaiting an answer, so
re-sending it on a timer converts a free hint into a per-session flood against
§7's one-shot budget. Send it when the reason to hold the segment ends — once.

**It is one-shot** (`control_plane_protocol.md` §5.4). Losing one costs the
plugin a mapping it keeps until the session ends, which is bounded waste and
never corruption — the same cost shape as a lost `ReleaseDeposit`, and the same
reason it must not sit on a queue whose policy is to replace.

---

## 9a. What binds a plugin

Most of this document is symmetric, and an SDK author needs to know which parts
are theirs. This is the whole plugin-side lifecycle in order, with a pointer to
the rule for each step; nothing here is new, and if this section and a numbered
one ever disagree, the numbered one wins.

### Getting one

1. **A grant arrives in two halves on two channels, and either may be first** —
   the capability as `M` on the doorbell (`doorbell_M_memory.md` §4), the meaning
   as `InstallSegment` on the control plane (§3). **Key both by `segment_id` and
   hold whichever came first.** Neither waits for the other, and building a
   receiver that assumes an order is the most common way to deadlock a first
   implementation (`control_plane_protocol.md` §8.12).
2. **Take ownership of the descriptor before anything can fail.** On unix `M`
   carries one descriptor by `SCM_RIGHTS`; on Windows it carries a section name
   and no descriptor at all (`doorbell_M_memory.md` §3). Every path out of the
   receive — including a rejection — closes what it received
   (`doorbell_protocol.md` §7.6, and clear the inheritable flag).
3. **Map with exactly the declared `perms`, and do not probe for more** (§8).
4. **Validate before believing any of it**, in §8's list: the object's size
   matches, the access obtained matches `perms`, the preamble checksums and its
   `segment_id` agrees, every region is in bounds, non-overlapping, aligned for
   its discipline and consistent with `buffers` and `slot_bytes`, `purpose` and
   every `discipline` are understood, and the declared regions fit (§2).
5. **Acknowledge once, when the second half has landed and all of the above
   holds** — *mapped ∧ preamble validated ∧ meaning applied*. The success
   acknowledgement **carries the 16 identity-proof bytes read back out of the
   mapping**, never echoed from the record, and the host compares them
   (`control_plane_protocol.md` §8.12). This is the check that catches a Windows
   `CreateFileMappingW` create-or-open landing you in a fresh private zero-filled
   section, where every other check passes.
6. **Report a failure the moment it is known, without waiting for the other
   half** — and **release everything the attempt acquired first** (§8). A mapping
   retained past a failed install is an invisible allocation the host will answer
   by granting another.
7. **Answer a duplicate with the recorded answer, and never remap** (§8). A
   duplicate `M` while pending closes its descriptor and stays pending; a
   duplicate after a terminal state replays that state's ack. **A half arriving
   after the segment was released is answered with `SegmentReleased`** (§9.4),
   not with silence — *released* is a fourth answer an SDK has to be able to
   return, and one whose return type offers only success, failure and "still
   pending" cannot express it. An `M` there closes its descriptor without
   mapping, exactly as a duplicate does.
8. **Answer `GrantStateQuery` truthfully and idempotently** — it is the grant
   path's only backstop, because a grant can be lost with the send reporting
   success (`doorbell_M_memory.md` §7). **A query can cross a retirement**, since
   §9 permits retiring a pending or unknown identity, so `released` is a fifth
   answer alongside not-present, pending, installed and failed. Answering
   `not_present` there would be a lie that provokes a re-send and revives a grant
   the plugin has released; answering `installed` would be a worse one.

   **Two of the five answers carry a payload, and naming the state is not
   answering without it.** The host's action differs between them, so an answer
   that omits the payload leaves the host exactly where the missing
   acknowledgement did:

   - **`pending` names WHICH HALF arrived.** The host's response is *"if
     `InstallSegment` is missing, send that; if `M` is missing, send nothing"*
     (`doorbell_M_memory.md` §7), so `pending` alone is a state and not an
     answer — the host cannot derive the missing half, because the half it is
     missing is the one it cannot see.
   - **`installed` replays the identity proof**, exactly as the acknowledgement
     carried it. The host compares it here too
     (`control_plane_protocol.md` §8.12): this is a recovery path, not an
     exemption, and a host that installed on a bare `installed` would have
     found the one way to get an unverified mapping past the check. A host is
     entitled to treat a proofless `installed` as a failure.

   Both are stated in the IDL's field comments; they are here because §9a is
   the plugin-side checklist and an implementer working from it would otherwise
   send a conforming-looking answer that cannot be acted on.

### Using one

9. **The discipline decides everything about handoff**, and a plugin implements
   only the ones its regions declare (§4). Constant needs no protocol; Owned is
   handed over by a control-plane request/reply and **re-reading is not safe**;
   Pulsed is §6 in full — claim by CAS, consume once, bounded attempts then
   coalesce, and §6.6's ordering; Loaned is §7's byte ring.
10. **A claim is a mutex, not a reference count** (C1, C4). One reader per
    handoff, a failed CAS means someone else holds it, and **a failed *release*
    CAS is proven interference**: quarantine the region, report once, keep every
    other region working (C2).
11. **Release the ring you consume, not the one you fill**, and `producer` says
    which is which (§3.1) — `ReleaseDeposit` for a host-produced ring,
    `ReleaseLoan` from the host for a plugin-produced one. Either way the key is
    the **requested** `len`, never the padded one (§7), and reclamation is FIFO,
    so sitting on one old deposit blocks everything behind it.
12. **Keep an unconditional re-check schedule for every Pulsed region you
    consume.** `D` is a hint that can only bring a check forward; a plugin that
    polls only when told will stall on the first dropped hint
    (`doorbell_D_notification.md` §5).
13. **Report what you can see, and keep the session** — an impossible state word,
    an invalid published length, a `D` bit naming an Owned region, a failed
    release CAS. These are `ContractViolation`, under a bound the plugin enforces
    itself; none is a framing fault and none ends the session (§8,
    `shared_memory_implementation_details.md`).

### Disposing of one

14. **`RetireSegment` permits finishing, not starting** (§9.1). Complete and
    release a held claim — that one is an obligation — finish a read already
    begun, and start nothing.
15. **Release means every mapping unmapped *and* every descriptor or handle
    closed** (§9.2), and `SegmentReleased` is sent after the last of them.
16. **Do not release a segment you were not asked to release** (§9.3). The
    exceptions are a failed install and process exit, and neither is unilateral.
17. **Ask, when you are done with one** — `ReleaseRequest` (§9.6), once, for a
    segment you have acknowledged. It is a hint: keep honoring the segment
    exactly as before until `RetireSegment` arrives, and do not schedule a repeat.
18. **Owe nothing at session teardown** (§9.4). Process exit is the release.

### What a plugin does not build

**The allocator, the grant, the ring growth, and retirement itself are the
host's.** A plugin never grants a segment, never sizes or doubles a ring, never
issues `InstallSegment` or `RetireSegment`, and never revokes anything — B1 means
there is nothing to revoke with. It also does not build the missing-ack deadline
or the query-then-resend recovery (`doorbell_M_memory.md` §7): those exist
because the *host* must make progress when an acknowledgement never arrives, and
a plugin has no peer to chase.

**The one asymmetry** is the plugin→host loan
(`control_plane_protocol.md` §5.6): there the plugin allocates and the host
releases, with §7's roles swapped. Growth does not swap with them — see that
section.

---

## 9b. How the data plane grows within 1.x

Within a major, a sender gates every addition a 1.0 receiver could misread on
the receiver's session minor:

| Addition | Rule |
|---|---|
| a new `purpose`, discipline, producer or perms bit | gated on the receiver's minor |
| a new layout for an existing purpose | gated, with a 1.0 fallback the sender uses for an older receiver |
| a new `Region` field that affects correctness | gated |
| a new kind of grant | a new `purpose` on `M`, never a new opcode |
| widening an opcode's direction | gated on the host's minor |
| `D` flags or padding, the descriptor count | a new major |
| a longer preamble | `[8..12)` and the bytes from 32 to the lowest region (§2) |

**A `SegmentRef` rides only in an arm or field defined at or below the
receiver's session minor**, and a reply to an existing request keeps its 1.0
arm: a loan a receiver cannot see is a deposit it can never release.

## 10. Conformance

The cases that distinguish a correct implementation from one that works on the
developer's machine:

- a segment whose preamble `[8..12)` is non-zero (and checksummed) installs,
  and so does one with arbitrary bytes in `[32..64)` and its first region at 64;
- a publish into a lane whose every buffer is READY claims the oldest, never the
  newest;
- a reader holding a claim blocks exactly that buffer and no other;
- 8 failed claims coalesce rather than stall, and the previously published value
  is still readable afterwards;
- a revision equal to `last_seen` is not re-read;
- both impossible state words quarantine the region and leave the word untouched;
- a length of 0, of `slot_bytes + 1`, and one that runs past the segment each
  release the claim and quarantine;
- a Loaned release with a padded rather than requested `len` is rejected;
- a Loaned ring whose oldest deposit is held reclaims nothing behind it;
- a `RetireSegment` arriving while a Pulsed claim is held completes and releases
  that claim before releasing the segment, rather than abandoning it;
- a `RetireSegment` for a **pending** segment releases the half it holds, and a
  later arrival of the other half is answered without mapping;
- `SegmentReleased` is not sent while any descriptor or handle for the object
  remains open — the case a `munmap`-only implementation passes and a leak
  check does not;
- an unreleased deposit does not prevent a retired segment from being released;
- a duplicate `RetireSegment` replays `SegmentReleased` rather than re-releasing;
- a `ReleaseRequest` the host ignores leaves the plugin fully operational on that
  segment — a descriptor arriving afterwards is read, not rejected;
- an `InstallSegment` carrying `supersedes` while its own `M` is still
  outstanding leaves the superseded segment live, and retires it only when the
  replacement reaches *installed* — and not at all if the replacement fails;
- an `InstallSegment` whose `supersedes` names its own `segment_id` is a
  `malformed` grant failure and the receiver still answers afterwards;
- a lane handle or arena handle obtained **before** `RetireSegment` refuses a new
  claim, publish or reservation afterwards, rather than only refusing to hand out
  a second handle (§9.1) — and the same after its region is quarantined;
- an `M` or `InstallSegment` for a released segment is answered with
  `SegmentReleased`, not with silence (§9.4);
- a `LOANED` region with `producer = PLUGIN` is mapped writable, and a
  `ReleaseDeposit` naming it is a `ContractViolation` rather than a release;
- a `PULSED` region under a read-only grant fails the grant `malformed` **whether
  the plugin produces it or consumes it** — the case a receiver that checks only
  `producer = PLUGIN` accepts and then faults on;
- a `LOANED` region whose `slot_bytes` is not 64, **including zero**, still
  installs for a consumer;
- two writers into one plugin-produced `LOANED` region are never handed the same
  byte, and two such regions in one segment allocate independently — the two
  halves of §5.6's arena scope, and an implementation can fail either one alone;
- a mask carrying one out-of-range bit alongside valid ones acts on every valid
  bit and sweeps only the peer-produced Pulsed regions
  (`doorbell_D_notification.md` §3) — the case an implementation that discards
  the whole mask, and one that sweeps every discipline, both fail;
- a `D` bit naming an `OWNED` region reports and leaves that region usable —
  a later legitimate handoff on it still works;
- a `RetireSegment` arriving while the PEER holds a Pulsed read claim releases
  the segment, rather than waiting on a claim this endpoint does not own (§9.1);
- a `PULSED` region with a misaligned `offset` or an `extent` inconsistent with
  `buffers` and `slot_bytes` fails the whole grant `malformed` at install, rather
  than installing and quarantining that region (§6.1);
- a region with non-zero `buffers` outside `PULSED`, and one with `extent == 0`,
  each fail the grant `malformed`;
- the last publication before a writer goes quiet is delivered even though its
  `D` is dropped — the case that separates an unconditional schedule from one
  that only runs on a hint (`doorbell_D_notification.md` §5a);
- a region with `producer = UNSPECIFIED` fails the grant, and fails it as a
  classified grant failure rather than as a framing fault.

This document defines no byte-exact test vectors.

---

## 11. Open

- Whether `supersedes` stays inline on `InstallSegment` or becomes a separate
  `RetireSegment` in all cases.
- Whether Pulsed buffer counts above 3 are ever wanted.
- **Single-writer/multi-reader**, if it is wanted: a separate design, not a
  change to §6's release value. A fixed pool of permits is one shape.
- **Dynamic occupancy.** Regions are fixed at grant time, so per-object
  publishing wants over-provisioned regions whose occupancy varies rather than
  one region per object. This is what should determine how many regions a
  manifest may declare.
