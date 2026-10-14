# Shared-memory API

`github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/memory` implements the
plugin's memory layer for platform protocol **1.0**, following the
specifications in `docs/spec`. Consumer and producer operations are both
covered, and a real-process Python↔Go harness checks Pulsed publishing and
reading in both directions. Application payload schemas are separate contracts.

Native mapping is implemented on Linux, macOS and Windows. Pulsed and Staged atomics are
supported on their little-endian amd64 and arm64 targets. There is no
emulated or process-local-lock fallback for shared atomics.

This API uses the installed region table, never an in-mapping directory.
The control handshake rejects a 0.x host.

O/P grants require a nonempty subject. Nil `Config.Contributions` means the
manifest is unknown and trusts that name. To check known declarations, supply
`map[string][]string{"O": {"selection"}}`, for example; a nonnil empty map
means known no contributions. Undeclared subjects then fail malformed. G/V/X/S
require empty subjects regardless of configuration. `Registry.FindContribution` selects an installed O/P segment by
subject; `Segment.LiveLane` and `Arena` select its unique publishing regions.

## API

| API | Responsibility |
|---|---|
| `New(Config)` | Default to G/V/O/P/X/S, with optional override; configure mapping size policy |
| `Registry.Grant(frame)` | Claim the doorbell descriptor, map shared memory promptly, check size/access/preamble, close the descriptor |
| `Registry.RejectGrant(frame)` | Retain a transport-rejected malformed grant's failure for replay and state queries |
| `Registry.Install(table)` | Validate and copy the authoritative region table; join it to the mapping in either arrival order |
| `Registry.Status(id)` | Report pending halves or replay the saved terminal answer |
| `Segment.Arena()` / `Purpose()` | Discover the unique plugin Loaned arena and installed purpose |
| `Registry.Segment(id)` | Return an installed, validated segment only |
| `Registry.Wait(ctx, id)` | Wait for either installation half, including an unknown ID; stop on failure, retirement, closure or cancellation |
| `Region.ReadEpoch` | Decode an optional pane pair from bytes already copied under the correct ownership discipline |
| `Segment.TryStageTurn` / `StagedTurn.Finish` | Acquire-load an armed turn, pin its mapping, and release-store the next holder after borrowed access ends |
| `Segment.ReadConstant` | Copy an immutable range, naming a region and an offset inside it |
| `Segment.ReadPulsed` | Claim the newest unread host-published value, copy, and consume by exact CAS |
| `Segment.PublishPulsed` / `PublishingStats` | Publish plugin-produced values with bounded claims, never reuse a revision, count coalescing |
| `Segment.WriteLoan` / `ReleaseLoan` / `ApplyHostRelease` / `LoanStats` / `LoanUseStats` | Allocate copied plugin-produced loans, distinguish local reclaim from host release, inspect pressure and admission counts |
| `Segment.ConsumePulsed` | Start unconditional polling with a bounded latest-value mailbox and rate-limited hint acceleration |
| `Segment.ResolveDeposit` | Find the region a mapping-relative descriptor lies in, and its release key |
| `Segment.AcceptDeposit` / `DepositRead.Copy` | Accept a control-plane deposit, copy once, return the exact `ReleaseDeposit` fields |
| `Segment.AcceptOwned` / `OwnedRead.Copy` | Explicitly accept a control-plane ownership handoff and copy once |
| `Segment.Hint` | Check epochs and signal eligible Pulsed consumers, including a restricted sweep for stray bits |
| `Registry.RequestRelease` | Produce a once-only decision to send `ReleaseRequest`; preserve segment access |
| `Registry.Retire` | Finish active copies/claims, unmap, and return `SegmentReleased`; replay later requests |
| `Registry.Close` | Release mappings at session teardown without inventing wire messages |

Ordinary payload reads copy to caller-owned Go storage, so callers can retain
them after returning a deposit or retiring its segment. Staged turns are the
explicit exception: they borrow bounded mapping slices while holding a turn. Returned metadata and acknowledgements are snapshots too. Methods
serialize access to a segment and share its Pulsed `last_seen` across all callers.
Independent segments can be read concurrently.

Plugin-produced Pulsed and Loaned regions may coexist in a segment with host
regions. Producer and consumer calls enforce their respective directions. There
is no generic `ReadAt` or `WriteAt` that bypasses discipline or ownership.

## Publishing and loans

`PublishPulsed(index, payload)` serves lanes without a pane header and accepts
1..SlotBytes bytes. `PublishPulsedEpoch(index, payload, pair)` serves either
layout, requiring a nonzero pane epoch exactly when the lane declares a header.
Payload capacity then becomes SlotBytes−8. Both methods copy under a
WRITE claim and returns `Sample{Revision, Length}`. A zero sample with nil error
means the update coalesced after eight scans. It tries EMPTY slots in revision
rotation order, then READY slots oldest first, excluding the newest READY.
Successful publication orders payload, length, then the release store to READY.
A failed copy releases only its exact claim; failed release quarantines the region.
Local size errors allocate no revision; every admitted attempt, including one
that coalesces, advances the region's shared revision exactly once. Multiple
callers serialize on the same region state; they cannot create separate writers.

`PublishingStats` retains allocation, successful-publication and coalescing totals.
After the final representable revision, further writes return
`ErrRevisionExhausted`; already-published values remain readable by the peer.
The spec requires reporting this condition but has no matching `ViolationSite`:
surface this local error to the supervisor rather than inventing a wire category.
After a successful publication the caller may send D as a best-effort hint.
Publishing itself never depends on notification delivery.

`WriteLoan(index, payload)` copies a nonempty value and returns a `Deposit` key
with the **mapping-relative** offset and the original requested length -- the
number the descriptor carries and the number the release echoes back (§1, §7).
The ring's own modular address stays region-relative inside the allocator and is
converted at its boundary, so the caller never adds `region.Offset` itself. Local
reads (`ReadConstant`, `AcceptOwned`) name a region and an offset within it,
because no wire number is involved. All
writers into that region share a bump cursor; each reservation pads to 64 bytes,
and no bytes are reused until all outstanding reservations have been released.
Out-of-order releases are accepted, but do not rewind a partially occupied arena.
`ErrArenaFull` immediately refuses and counts the push. There is no implicit wait,
growth request, or reuse of live bytes. `LoanStats` exposes pressure locally.
An unpublished reservation pins retirement through the first descriptor
admission or explicit `ReleaseLoan` rollback. `Registry.NameLoans` releases the
pin at admission; the control adapter keeps admission and SegmentReleased in
sender queue order. Retire returns `ErrLoanActive` while such a transaction is
unfinished. Wait on `Segment.LoansDrained()` and retry Retire; the control adapter
does this automatically. `Update.PendingReleases` carries an old arena whose
superseding grant was installed while its publication was still pending. Send
the new GrantAck first, then settle those releases after draining. Already
admitted loans remain owned by the host and are settled by retirement without
waiting for individual ReleaseLoan messages. Session Close can settle unpublished
reservations too, after finishing active copies; no descriptor can then be sent.

`WriteLoanReport` additionally captures the failed request and free space under
the allocator lock; `control.Session.WriteLoan` coalesces these into bounded
`SegmentShortfall` reports.

`Region.EpochOffset` preserves protobuf presence, including offset zero. The
header offset is relative to a Pulsed slot, a Loaned deposit, or the region for
Constant/Owned. Pulsed installation requires slot_bytes > 8 and places the header
at slot_bytes−8. The published length excludes that tail. ReadPulsed copies the
header at its declared offset in the caller's slot-sized buffer; call ReadEpoch
on that buffer, not its payload-length slice. ConsumePulsed supplies Pane and
PaneError separately, retaining usable Data on an invalid annotation. Other
disciplines bound the offset against extent.
Loaned deposits too short for the header carry no pair and remain usable.
Decode a pair only from safely copied bytes with ReadEpoch, then compare it
with the acknowledged state. Pulsed payload-length checks remain in force.
Every ReadEpoch error concerns only the annotation; the copied payload remains
usable. `errors.Is(err, memory.ErrRejectedPair)` identifies an invalid view or
zero epoch, distinct from `ErrBounds` for a partial header copy. Diagnose the
missing pair separately rather than dropping data because the annotation failed.

Allocation always pads to 64 bytes; Loaned SlotBytes is reserved and ignored,
including zero and non-64 values.

Deliver each host `ReleaseLoan` exactly once to `Segment.ApplyHostRelease(key)` using
the mapping-relative offset and the requested length, never the padded length.
An offset that falls outside the region the release names is a mistargeting and
reports `release_region_mismatch`; one inside it that matches no reservation
reports `unknown_release`. Those two and wrong
producers quarantine the named region and return reportable violations. They do
not free bytes. Keys can be reused after release and have no generation, so the
SDK cannot recognize a delayed duplicate that aliases a new loan. The reliable
dispatcher's exactly-once obligation remains essential. Once a key is returned
by `WriteLoan`, it is reserved immediately but has not been sent. If descriptor
construction or admission fails before ANY use is admitted, the caller must
retry admission or reclaim it with a single local `Segment.ReleaseLoan(key)`.
No host release will arrive for a never-admitted reservation. This method sends
no wire message; ErrRetired means retirement already settled cleanup. Once ANY
use is admitted, retain until host release or retirement: a later send refusal
or request timeout does not authorize local rollback. See the
[raw-loan example and reader/multi-use limits](../control/README.md#raw-loans-reservation-is-not-admission).

`Registry.NameLoans` atomically checks and counts all plugin-produced reservations
in an admitted message. The control engine calls it after validating the message
and reserving queue capacity. Uses 0/1 both mean one; later declarations must
match, and excess namings fail with `ErrUsesExceeded`. A failed batch consumes
no uses. `ReleaseLoan` serves only local reclaim and returns `ErrLoanAdmitted`
once any use was admitted. `ApplyHostRelease` is exclusively for the receiver's
reliable host-release path. Low-level embeddings must wire admission and release
to these distinct operations.

`LoanUseStats` reports outstanding admitted namings, still-unadmitted declared
uses, and `Stranded`: reservations whose local reclaim was refused after admission.
Such loans remain outstanding until the host releases them or the segment retires.
Later successful namings may complete them; they are never silently reclaimed.

Retirement rejects new publishing and reservations, waits for an in-flight copy
and claim to finish, then unmaps and settles remaining loans. Cached segment
handles cannot restart operations afterward.

## Windows mappings

Windows opens an existing section by its validated name using `OpenFileMappingW`,
then maps only the declared extent with the requested read/write access. It
never calls `CreateFileMappingW` on the receiving path. Common preamble validation
still checks ID, zero padding and CRC and returns the host's proof from the view.
The section handle closes immediately after mapping; the view retains the object
until retirement unmaps it, matching the lifetime rules in
[Microsoft's mapping documentation](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-createfilemappingw).
The slice exposes only the declared byte count; Windows section allocation may
be page-rounded, so no Unix-style exact file-length claim is made for that object.

## Integration

Create one registry per session. Nil Purposes selects the normative G/V/O/P/X/S
set; an optional override specifies the purposes your embedding implements. `MaxSegmentBytes` defaults to 1 GiB as a **local policy**, not a
wire limit or a total session budget.

```go
mem, err := memory.New(memory.Config{
    MaxSegmentBytes: 256 << 20,
})
```

In the doorbell callback, pass a **Decoded M** to `mem.Grant(frame)` before the
callback returns. It consumes the descriptor on every outcome, including a
duplicate. Do not claim it separately. Pass each control-plane `InstallSegment`
to `mem.Install(...)`, preserving every field and repeated-field order. Either
may arrive first; neither waits for the other. Rejected doorbell records retain
their existing transport reporting route and must not go through `Grant`.

Both methods return `(Update, error)`. An empty update is pending. Send a returned
`Ack` reliably on the control plane; send every returned `Released` identity as
`SegmentReleased`. A grant failure is in `Ack.Failure`, not the Go error. The
separate Go error is an integration or cleanup failure and must be surfaced. Process
an update even if an error also accompanies it: a replacement can install while
cleanup of the superseded segment fails. Never claim a release that was not
returned. The registry performs no callbacks and waits for no network delivery.
Each explicitly started Pulsed consumer owns a timer goroutine and a one-entry
mailbox of copied values; no application callback runs on that goroutine.

Identical M repeats compare all 112 bytes and close their duplicate descriptor
without mapping it. Identical Install repeats compare fields, treating nil and
empty repeated fields equally. Conflicts return a malformed grant acknowledgement
without replacing the original state. Pending/failed/installed/released identities
are retained for the session, as the protocol requires. `supersedes` takes effect
only when the replacement is fully installed.

`Status` is a local snapshot whose five states map to `GrantStateQuery` answers.
The control adapter sends `GRANT_STATE_RELEASED` for a query crossing retirement,
as specified by shared-memory §9a step 8. Late M/InstallSegment handling returns
`SegmentReleased`.

The descriptor is closed promptly after `mmap`; retirement only has the retained
mapping to release. No descriptor or view aliases are handed to application code.
An active synchronous copy finishes before unmap, including its exact Pulsed
release CAS. Loaned reservations additionally count a claim from before the
retirement check through every message's successful transport Send or release.
`Registry.AdmitLoans` atomically admits a message and returns its idempotent send
completion; queue admission alone does not release the claim. `NameLoans` keeps
its admission-only API, but senders should use AdmitLoans to track completion.
After a failed transport, Close abandons pending publications.

An accepted deposit pins the mapping even before Copy begins. It may finish on a
retiring segment; successful Copy ends its claim, and Close abandons an unread or
failed read. The control adapter uses `Registry.ClaimDeposit` on arrival and keeps
that claim until message disposition, including across grant preparation. New
reads are refused after retirement; cached handles fail after unmap. Retire
returns ErrLoanActive while these claims remain; retry after LoansDrained closes.
SettleRetirement retries without replaying another settlement's completed release;
explicit Retire still replays terminal identities.

Start a Pulsed consumer for every region the application actively consumes:

```go
seg, err := mem.Segment(segmentID)
// Check err before the next call.
reader, err := seg.ConsumePulsed(regionIndex, memory.ConsumerConfig{
    PollInterval: 40 * time.Millisecond,
    HintInterval: time.Millisecond,
})
// Check err before using reader.
defer reader.Stop()
for update := range reader.Updates() {
    if update.Error != nil {
        reportMemoryError(update.Error)
        continue
    }
    consume(update.Data) // independent Go-owned bytes; safe to retain
}
```

The SDK checks immediately and then every `PollInterval` (default 40 ms), even in
a completely quiet session after a lost final D. Valid `Hint` calls signal the
consumer automatically and can add a check no more often than `HintInterval`
(default 1 ms). They never postpone the unconditional deadline. Those are
receiver-owned scheduling intervals; actual execution is also subject to Go/OS
scheduling and completion of an in-flight segment operation.

The mailbox retains the newest copied value, replacing an unread older one.
`Stats().Coalesced` counts replacements. Slow application processing cannot block
polling or retirement. A read violation ends that consumer and remains available
as the final mailbox entry. `Stop` ends active consumption; `Done` closes after
any in-flight claim is released. Retirement and session close also stop consumers.
Installing a region alone does not consume values the application never requested.
Only one consumer can own a region at a time.

`ReadPulsed` remains the low-level synchronous primitive for integrations that
already implement an unconditional schedule; do not use it only from D callbacks.
It returns `ErrConsumerActive` while `ConsumePulsed` owns the region, preventing
competing readers from stealing each other's updates.

For that primitive, the destination's **length**, not just capacity, must cover `SlotBytes`. This is
checked before claiming anything. No value or eight lost CAS attempts returns a
zero sample with nil error. Corruption returns a `*Violation` and quarantines just
that region; subsequent calls return `ErrQuarantined`. Exact-release interference
never clears the foreign word. If invalid length and release interference occur
together, the returned joined error contains both violations. Do not use the
destination bytes on error.

Read a host deposit only in response to its reliable descriptor:

```go
key, err := seg.ResolveDeposit(ref.Offset, ref.Len) // SegmentRef is mapping-relative
// Check err before each next call.
handoff, err := seg.AcceptDeposit(key)
// Check err before deferring Close.
defer handoff.Close()
buf := make([]byte, key.Length)
release, err := handoff.Copy(buf)
// On success, enqueue ReleaseDeposit using exactly release's four fields.
// buf is now independent of the host's allocation and can be decoded later.
```

`Copy` can succeed only once for a handoff, even if the handle value is copied.
An undersized destination can be retried. It returns the requested length, never
the internally padded allocation size. Neither reading bytes nor returning the
release value sends the message: the supervisor must deliver the release reliably,
and close the read handle on an error or when abandoning it. Never construct a
second handoff from a stale descriptor. The memory layer cannot distinguish a legitimate reuse of `(offset,
length)` from a replay after release; reliable-message dispatch must call
`AcceptDeposit` exactly once per actual handoff.

`AcceptOwned` is similarly an explicit boundary for an actual request/reply
ownership transfer. It does not prove ownership from the mapping or invent a wire
handshake. Keep the control-plane ownership until its one-shot `Copy` completes,
then perform the exchange's required reply. No 1.0 application protocol uses
OWNED.

`ConsumePulsed` implements the five advisory-channel conditions: unconditional
bounded scheduling, independence from D, hints used only for scheduling, receiver
rate limiting, and reading only host-produced Pulsed regions. `Hint` signals
registered consumers and returns the corresponding plan; it does not read inline.
Pass the acknowledged epoch for the notification's pane, including pane zero.
Old epochs produce `stale_epoch`; newer/unacknowledged epochs produce `future_epoch`.
Both are counted and ignored. Constant and Loaned bits are diagnostic no-ops, and
Owned bits produce `owned_region_hint` violations. A bit naming a plugin-produced
Pulsed lane is counted as `hint_on_produced_region` and ignored. Out-of-range bits
are counted individually and additionally schedule all host-produced Pulsed
regions. This sweep excludes Constant, Loaned, Owned and plugin-produced lanes;
explicit bits still receive their usual diagnostics and violations. Scheduled
regions are unique and use the same rate-limited consumer path. Clearing a bit
does not prove a region unchanged.

gRPC conversion/delivery, wiring to the session-wide violation reporter, local
diagnostic and duplicate counters, application decoding, and host
retry/deadline policy remain outside this package. No report should block the
doorbell callback. `Close` is for session teardown; use `RequestRelease` followed
by the host's `RetireSegment` for ordinary release.

The sibling [`violations` package](../violations/README.md) supplies the required
session-wide reporter before wiring these values to gRPC:
`HintPlan.Violations` and read errors are detection results, not permission to send
one control-plane message per result. Use `ObserveHint` once for the whole D's
plan and `ObserveError` once per failed read, sharing the same reporter as
doorbell `ObserveFrame`. The sender calls `Next` to obtain reports with novel
categories prioritized before repeats, under a shared rate limit.

## Verification

From `polyglot/go-sdk`:

```sh
go test -race ./...
go vet ./...
```

For required real Python↔Go checks on Linux/macOS, install `atomicshm==0.1.0` in a
Python environment, then run from `polyglot/go-sdk`:

```sh
VIZI_MEMORY_PYTHON=/absolute/path/to/python go test -race ./memory -run Python -v -count=1
```

The Go test process grants the same real file-backed mapping to a Python child
through inherited fd 3. Both endpoints use their actual public publisher/reader
APIs, with producer roles declared relative to the endpoint. Python uses native
`atomicshm.AtomicView` through `_shared_atomic`; missing dependencies fail when
the harness is requested. No Python unit-test stand-in is imported. CI requires
the harness on Linux/macOS; ordinary local Go tests explicitly skip it unless
the interpreter is configured. Tests compare rotation, READY ordering, exact
consume-once behavior and 5,000 concurrent variable-length publications in each
direction. They validate received contents and monotonic progress through the
final value, allowing intermediate coalescing. This is memory-ordering evidence
across two real atomic backends, not exhaustive proof of every interleaving.

The memory tests use real shared mappings, exact preamble bytes, a real doorbell
descriptor transfer, and a separate-process Pulsed reader. They cover both grant
orders and replay; classified validation failures and cleanup; permission mismatch;
overflow and overlap; copy lifetime and release keys; consume-once handoffs;
impossible state words, invalid lengths, eight-attempt contention, failed release
CAS, shared consumer state, retirement with a held claim, late halves, supersession,
and advisory hints. Virtual-time tests cover quiet-session polling, hint rate
limits and independent deadlines, slow receivers, consumer exclusivity, quarantine,
and shutdown. These tests are not host interoperability certification.

## Processing-stage rings

Purpose S has an empty subject and 1..512 HOST-produced STAGED regions. Grants
require PLUGIN_WRITE, 64-byte-aligned offsets, at least 256 bytes per region,
zero buffers/slot_bytes and no epoch_offset. The normal preamble identity/CRC
check applies: the same ring ID is used in every receiving session. Grant
validation reads only the region table and preamble, never a slot header.

Most authors use plugin.StageHandlers; control owns the worker and arming.
Low-level integrations may use StageWord (atomic word only) and TryStageTurn
(one acquire-load, matching holder and nonzero/new iteration). A successful
borrow pins the mapping until Finish or Abandon; no segment mutex is held while
application code works. Validate the header with stage.DecodeSlot and the armed
input before exposing it to authors. Only the holder may inspect ordinary bytes.
Finish writes outcome/reason/done_through and release-stores position + 1 on
success, or negative position otherwise. The caller sends D afterwards.

If retirement or Close encounters a live borrow, it latches retirement and
returns ErrStageActive without unmapping or acknowledging release. The last
borrow frees the mapping after it returns. A held turn may finish and publish
while retiring; HandedOn records whether its D is owed even after unmap. Session
Close instead suppresses publication. Direct Registry callers retry to settle
retirement/Close; the control adapter automatically retries a pending
RetireSegment after stage work returns and sends SegmentReleased. The host
drains a ring before superseding it. No local timeout can forcibly reclaim an author's memory. Every borrow must
finish, even on error, and no derived slice may survive it.
