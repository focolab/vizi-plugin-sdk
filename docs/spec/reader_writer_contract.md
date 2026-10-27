# Reader and exporter application contracts

> **Status.** Normative. This document owns the two application protocols
> that ride the `G` and `X` segments — a plugin **reader** serving a dataset to
> the host, and a plugin **exporter** receiving one from it: the `G` arena's
> grant timing and buffer exchange, plane and pyramid behavior, byte order,
> and what `OWNED` means on these paths. Everything below is what the reference
> host and the reference SDK do at `1.0`; where the reference is behind this
> text it says so and `control_plane_implementation_details.md` §2b carries the
> row.
>
> `control_plane_protocol.md` §8.2 and §8.6 print the messages and cite this
> document for the exchange (`plugin_principles.md` 13); `shared_memory_protocol.md`
> §7 owns the Loaned discipline both segments use; §2 there owns the byte
> layout. Nothing here restates those — it says which of their rules applies
> at which step, and settles what they leave to the application.

---

## 0. What the two protocols have in common

**Both are Loaned exchanges, and §5.6 of the control plane is the whole
ownership model.** A reader *produces* into `G` (plugin-produced `LOANED`, one
region) and the host releases with `ReleaseLoan`; an exporter *consumes* from
`X` (host-produced `LOANED`, one region) and releases with `ReleaseDeposit`.
Every descriptor is a loan; every disposition — applied, rejected, orphaned —
counts one use; the release is owed at zero declared uses. Nothing in this
document adds a second ownership rule.

**Both segments are single-arena, sized for one payload, granted per open.**
`grants.arena_segment_size(payload_bytes)` = the 32-byte preamble plus the
payload padded to §7's slot; the region is everything after the preamble
(`grants.single_arena`; region index 0; `purpose` `G` or `X`; no `subject`).
The host writes and reads from `grants.INTERIOR_OFFSET` = 32, and every
`SegmentRef.offset` on these paths is mapping-relative, as §7 requires.

**Both payloads are the array's bytes and nothing else**: contiguous C order, X
fastest, little-endian, unpadded, `len = Z·Y·X·sizeof(dtype)` — the sentence
`shared_memory_protocol.md` §2 owns. `ReaderOpened.little_endian` and
`OpenExport.little_endian` are informational and `true`.

**Neither uses `OWNED`.** No `1.0` application protocol does: `G`, `V`, `X`,
`P` and an overlay's bulk arena are `LOANED`; an overlay's live lane is
`PULSED`. The four things `shared_memory_protocol.md` §5 says an application
protocol using `OWNED` must define — the transfer message, the return message,
the range, the replay identity — are therefore defined by nobody, and a
receiver never meets an `OWNED` region from this host. An SDK that exposes an
authorisation boundary for one (§5's "explicit boundary") has done everything
`1.0` can ask; an SDK that exposes nothing has done nothing wrong.

---

## 1. The reader

### 1.1 Order of events

| step | message / grant | who waits for what |
|---|---|---|
| 1 | `OpenReader{uri, reader_id, series?}` → `ReaderOpened` \| `SeriesList` \| `Error` | the host's open blocks on the reply, which may be given as little as 30 s by a host that does not show it, or as long as the person tolerates by one that shows it and lets them cancel (`control_plane_protocol.md` §8.16); a `SeriesList` is answered by a second `OpenReader` naming the chosen series (§1.2a) |
| 2 | **the `G` grant** — `InstallSegment` standalone on the stream, `M` on the doorbell | the host **waits for `GrantAck` (installed)** before step 3 |
| 3 | `ReadVolume` / `ReadPlane` → `SegmentRef` \| `Error`, any number, one at a time | each blocks on its reply, on the same floor as the open; the host copies out and disposes |
| 4 | `Metadata{handle}` → `MetadataResult` \| `Error`, at most once per open | — |
| 5 | `CloseReader{handle}` → `Closed` \| `Error` — **on every deliberate close; see §1.8** | best effort, 1 s |
| 6 | `RetireSegment` for `G`, then the lease | `shared_memory_protocol.md` §9 |

**The `G` grant follows `ReaderOpened`, not the handshake.** It cannot precede
it: the arena is sized from the reply — one level-0 volume, `Z·Y·X·sizeof(dtype)`
of `shape_tczyx` and `dtype` — so a session that has not opened a reader holds
no `G` at all.
It is sent standalone, and **the host waits for the installed acknowledgement
before the first read**, because the grant's two halves travel on two channels
and a read could otherwise overtake its own `M`.
So on a conforming host a read never arrives before the arena; a plugin that
receives one anyway (a host that did not wait) answers an `Error` naming the
missing purpose — the kind is not specified, because nothing depends on it;
the reference SDK's message is *"no segment with purpose 'G'"*.

**How many reads a host issues, and in what order, is not contract.** Step 3's
"any number" means it: the reference host prefetches, paced by the reader's
reply latency, so the same session can draw
a handful of reads from a slow reader and dozens from a fast one. **A reader
answers any in-range read**, in whatever order it arrives, and never assumes
the next request from the last.

### 1.2 Handle and segment — the replacement handshake

**One reader is open per session at a time**, and the session's one `G`
segment is its arena. `ReaderOpened.handle` is the plugin's to choose, and
**a handle is never reused within a session** (the reference SDK numbers them
`r0`, `r1`, …); every later request names it, and the host echoes what it was
given. A second `OpenReader` on a session REPLACES the open reader, and a
plugin must accept it: the reference host opens the next file *while the last
is still on screen*, so the two overlap on the session, and a plugin that
refused the second open would refuse every second file.

Because the opens overlap, the replacement is a **handshake with one specified
outcome per step** — never a race whose result depends on which of the old
reader's reads happened to be in flight. Three invariants hold at every
instant, and each end enforces its own:

- **I1** — at most one reader is open, and its handle is unique within the
  session. *(Plugin.)*
- **I2** — at most one `G` is installed. The host grants the next `G` with
  `supersedes` naming the current one, and the plugin retires the old on
  installing the new (`shared_memory_protocol.md` §9.5). Two installed `G`s
  is a host that granted without `supersedes`; the reference SDK refuses
  every read until one is retired (*"2 installed segments carry purpose 'G'"*)
  and that refusal is the enforcement, not a state the handshake reaches.
  *(Both.)*
- **I3** — a request naming any handle but the current one is
  `Error{NOT_FOUND}` naming it (§1.8), and the plugin diagnoses it: the host
  sends nothing for a reader once it has asked for the next (H1), so one
  arriving is the host out of step. *(Plugin.)*

The sequence, replacing reader A (handle `hA`, arena `G_A`) with B:

| step | host | plugin |
|---|---|---|
| **H1** | `OpenReader{B}`. May go while A is open. **From the moment this is sent, nothing more is sent for A** — a read the viewer would have made is dropped at the host, not failed — and a read of A already in progress finishes **copying** before this is sent: H3 supersedes A's arena, and a reply whose bytes were not yet out of it would name a segment the host has forgotten. | — |
| **H2** | — | open B; **on success** close A and answer `ReaderOpened{hB}`, `hB ≠ hA`; **on failure** answer `Error` with A untouched — *`Error` means nothing changed*. |
| **H3** | grant `G_B` with `supersedes = G_A`. | on installing `G_B`, retire `G_A` and announce `SegmentReleased(G_A)`. |
| **H4** | wait for `G_B`'s acknowledgement; B's first read follows it. | — |
| **H5** | A is done: no `CloseReader` (its handle is dead and the plugin closes the *named* handle only), no `RetireSegment` (superseded at H3), its lease released. | — |

**The one exception, at H2:** when B's URI is the URI A holds — a reload —
the plugin closes A *before* attempting B, because an exclusively locked file
cannot be opened twice. On that path a failure leaves
no reader open, and the host treats A as gone (below).

**What each failure means, and there are no others:**

- *H2 refused (different URI)* — nothing changed. The host keeps A and A is
  still readable; A's eventual close is the ordinary §1.8 one. **Only an
  explicit `Error` reply means this.**
- *H2 refused (same URI)* — A is closed at the plugin. The host closes A's
  dataset and reports the error.
- *H2's reply never came* — a timeout, a transport fault. The plugin may
  have applied the open before the reply was lost, and nothing says
  otherwise: **uncertain is "moved on", never "unchanged"**. The host treats
  A as gone (closes it, with the error) rather than keep a dataset whose
  every read the plugin would answer `NOT_FOUND`.
- *The host abandoned the open after H1* — the user canceled it, and the
  result (a dataset, or a failure) arrived anyway, because a cancel stops
  the install and not the open. If the plugin moved on, A is gone: the host
  closes A and says so — unless another open is already in flight, whose own
  result or failure reconciles instead.
- *H3 fails after H2 succeeded* — B is open at the plugin and has no arena;
  A is closed there. The host closes both and reports the error. `G_A` was
  not superseded, so A's close retires it.
- *Two `OpenReader`s in flight* (the reference host cancels an open's
  *intent* but cannot interrupt its construction) — each is a replacement;
  the plugin applies them in stream order and the later handle is current.
  The host **installs only the dataset whose reader holds the session's
  current handle** and fails the other with "replaced before it was
  installed; open it again". The retry succeeds.
- *A request naming a replaced handle* — I3: refused and diagnosed. The
  reference host never sends one; its old reader refuses locally, under the
  same lock the next open claims, so the refusal and the claim cannot cross.

The reference host runs H1–H4 as one step under a per-session lock, and each
read of the old reader — request and copy — under the same lock, so two opens
on one session run in turn and in the host's order, and no open claims the
session under a copy. The reference SDK implements every plugin row above.

### 1.2a Choosing a series

A file that holds several images is opened in two steps on one session:
`OpenReader` with `series` unset is answered `SeriesList`, the host asks the
person, and a second `OpenReader` names the chosen id. **`SeriesList` is H2's
"nothing changed"** — no handle, no `G`, the open reader untouched — with H2's
one exception: listing the file the open reader holds closed that reader first,
exactly as a same-URI reopen does, so the host treats it as gone. The reference
host avoids that case by keeping the list it was given at the first open and
showing it again rather than asking.

**The listing instance opens the choice.** Whatever the reader opened to list
the file may stay open: the reference SDK holds the instance that answered
`SeriesList` and calls `open(uri, series)` on it when the next `OpenReader` has
the same `uri` and `reader_id` and a series set. Any other open retires it
first, and so does its idle limit (10 minutes — a person may be choosing from
thousands) and the session's end. A plugin is not required to hold anything:
a second `OpenReader` naming a series is a complete request on its own, which
is what a recent-files reopen sends with no listing before it. The host
likewise may abandon a listing, in which case it simply sends nothing more.

A set series the reader does not have is `NOT_FOUND`; a set series answered with
a list is a reader defect (`INVALID_ARGUMENT` in the reference SDK). A list of
one entry is not a choice, and the reference SDK opens it rather than send it.

### 1.3 `ReadVolume{handle, t, c, level}` → `SegmentRef`

The plugin reserves `Z·Y·X·sizeof(dtype)` bytes in `G`, writes the level-0
volume at `(t, c)` in §2's layout, and replies a descriptor:

| field | value |
|---|---|
| `segment_id` | the `G` segment's |
| `offset` | mapping-relative, where the reservation begins |
| `len` | exactly `Z·Y·X·sizeof(dtype)` |
| `shape_zyx` | `(Z, Y, X)` — the whole declared volume; a reader does not sub-volume |
| `dtype` | the deposit's, which a conforming reader makes `ReaderOpened.dtype` |
| `uses` | unset (one): the reply is the one message naming this reservation |

**The host validates before it reads a byte**: `dtype`
known, `len == prod(shape_zyx) · sizeof(dtype)`, and the range inside the
segment. A descriptor that fails is *rejected* — and rejected
is a disposition, so the loan is released with the failure, never stranded.
The host copies the bytes out under its own lock, then disposes through the
broker's ledger, which posts `ReleaseLoan{segment_id, region 0, offset, len}`
when the reservation's declared uses reach zero.

**The bytes are the host's until that release**, and the reference arena holds
exactly one volume, so **a reader cannot answer a second read until the first is
released**. The reference host never needs it to: its reads are serialized —
the RPC and the copy are one unit, so a concurrent 2D + 3D pair cannot tear a
deposit written at offset 0. A host
that pipelines reads grants an arena of N volumes; a reader asked for a second
volume with the first still lent answers the arena-full `Error` and a
`SegmentShortfall` (§5.6), never overwrites.

**There is no `inline` form for a reader's reply.** The reply arm is
`PluginMsg.segment`; a volume rides shared memory or not at all.

### 1.4 `ReadPlane{handle, t, z, c, level}` → `SegmentRef`

The same exchange with `shape_zyx = (1, Y, X)` and `len = Y·X·sizeof(dtype)`.
A reader with no plane path derives it from the volume (the reference SDK
does); the host reads `(1, Y, X)` and drops the leading axis.

### 1.5 Bounds, and what an `Error` says

| condition | `Error.kind` |
|---|---|
| `reader_id` names no contribution of this plugin; `uri` cannot be opened | `NOT_FOUND` |
| `reader_id` empty and the plugin declares several | `INVALID_ARGUMENT` |
| `t`, `z`, `c` outside `shape_tczyx` | `OUT_OF_BOUNDS` |
| `level` the reader cannot serve (§1.6) | `OUT_OF_BOUNDS` |
| a request naming a handle no reader is open under, or one the session replaced (§1.2 I3, §1.8) | `NOT_FOUND`, naming the handle |
| the file failed under the reader | `IO_ERROR` |
| the host sent `CancelWork` for this request and the reader stopped (`control_plane_protocol.md` §8.16) | `CANCELLED` — not a fault |

**Any reader request may be canceled** — an open that is taking too long for
the person at the load dialog, a read they gave up on (§8.16). The reader
notices between steps, stops, and answers `CANCELLED`; the request keeps the
session's slot until it does, so a reader that never checks is a reader whose
next request waits for the work to finish. It may report progress on the same
request while it runs.

The reference host reads `message` for its log and does not branch on `kind`;
the kinds are mandated anyway because another host may branch on them.

### 1.6 Levels: there is no pyramid on this path

**`ReaderOpened` declares no pyramid, and at `1.0` the host sends `level = 0`
only** (the host refuses any other level before it sends a request, and
builds no pyramid over a plugin-served dataset). The
`level` field on `ReadVolume` and `ReadPlane` is reserved for a reader that
can declare one. **A reader answers a level it cannot serve with
`Error{OUT_OF_BOUNDS}`** — the same kind as an out-of-range `t` — and never
with level-0 data: that is an array of the wrong shape for the question asked,
and the host has no way to notice. The reference SDK does this.

### 1.7 `Metadata{handle}` → `MetadataResult{form, text}`

`form` is `"ome-xml" | "ome-ngff" | ""`; the empty form is *"this reader has no
document"* — an answer, never a failure. **Calibration precedence**: the
`ReaderOpened` spacing and time fields (§8.2) win over a document that
disagrees, because the reader chose them and the file's author wrote the
document — **per axis**: a reader that declares only Z leaves X and Y to the
document. The reference host asks for the document **once per open**, the
first time the dataset's metadata is read (which is at open, on the opening
thread), parses it through the same path as every OME file, and lays the
declared fields over it; an `Error` reply is logged and treated as the empty
form, so a reader whose document fails is uncalibrated, not unopenable. The
document's `channel_names` and `time_increment` stand where the reader
declared nothing.

### 1.8 `CloseReader{handle}` → `Closed`, and after

The plugin drops **the named handle** and whatever its file held open, and
answers `Closed` — **idempotently**: closing a handle nothing is open under,
or one the session has since replaced (§1.2), is done, not an error, because
the host's close is best effort and reads nothing from the reply. It is the
named handle and never "whatever is open": otherwise the host's close of a
replaced reader would close its successor. The host then retires `G`
(`RetireSegment`, which asserts every loan inside it is already released,
`shared_memory_protocol.md` §9.3) and releases its lease; a plugin with
nothing else open exits.

**But a plugin must not DEPEND on receiving `CloseReader`.** The reference
host sends it whenever a file is closed on purpose **and the reader is still the
session's open one**, with a 1 s wait for the reply and nothing read from it.
A reader that is merely dropped — a host crash, a finalizer, a close that
faulted before the announcement — is retired without it: a finalizer may not
block on a peer. A reader a later `OpenReader` replaced (§1.2, H5) gets
neither the announcement nor a `RetireSegment` of its own: its file was closed
at H2 and its arena superseded at H3. So what a reader **reliably** sees at
the end of its life is one of: `RetireSegment` for its `G` (a close with no
next open); a new `OpenReader` (§1.2) with a `G` grant that supersedes its
own; or the session's `Shutdown` (`control_plane_protocol.md` §3.3). A file
handle that matters is released on `CloseReader` when it comes, on
`OpenReader` of the next reader (H2), and at shutdown.

**A `ReadVolume`, `ReadPlane` or `Metadata` arriving after the close — or before
any open — is `Error{NOT_FOUND}` naming the handle.** A host log needs the
handle.

### 1.9 What a reader must not do

- **Reuse or overwrite lent bytes** before `ReleaseLoan` names them — §5.6.
- **Call `data_access` on the dataset it is serving** — the host refuses it
  (`UNSUPPORTED`) rather than deadlock, since it is blocked on this reader's
  reply.
- **Answer a level-N question with level-0 data** (§1.6).

---

## 2. The exporter

### 2.1 Order of events

| step | message / grant | who waits for what |
|---|---|---|
| 1 | **the `X` grant** — `InstallSegment` standalone, `M` on the doorbell | the host waits for **nothing** here |
| 2 | `OpenExport{...}` → `ExportOpened{handle}` \| `Error` | the host blocks (30 s) |
| 3 | for each chunk: host writes the payload into `X`, then `ExportChunk` → `ChunkWritten` \| `Error` | the host blocks (120 s; suspended while the chunk is held, §8.12) |
| 4 | `FinishExport{handle}` → `ExportDone` \| `Error`, or `AbortExport{handle}` → `ExportDone` | 60 s / 10 s |
| 5 | `RetireSegment` for `X`, then the lease | `shared_memory_protocol.md` §9 |

**`X` is granted once per export job, before `OpenExport`, and the host waits
for no acknowledgement before the first chunk**. This is `control_plane_protocol.md` §8.12's standalone
form: the receiver **holds** a chunk into a segment it has not yet installed
until both halves land, and the chunk's deadline is suspended while it does.
The arena is sized for one chunk — the artifact's per-volume
`Z·Y·X·sizeof(dtype)` after crop and projection. **`X` is the job's, and the job
retires it** (step 5) whether it finished, aborted or failed: a plugin may not
release a segment on its own (`shared_memory_protocol.md` §9.3), so an `X` the
host did not retire would stay installed, and count against the session ceiling,
for as long as another lease kept the session alive.

### 2.2 `OpenExport` — the artifact, declared once

`shape_tczyx` is the **artifact's** whole geometry: `T` and `C` are the
selection's counts (not the source's), `Z, Y, X` the per-volume shape after
crop and projection. `dtype` and `little_endian` describe every chunk.
`source_metadata` is the SOURCE's tagged document or the empty form — what
the artifact was derived from, never its description; `channels` names the
artifact's channels and `provenance` carries the host's record
(`control_plane_protocol.md` §8.6). The calibration fields are what the host
knows — and **an exporter writes no measurement it was not given**, and no
channel name the host marked a placeholder. The reply's
`handle` is the plugin's to choose; every later request names it.

### 2.3 `ExportChunk{handle, t, channels, source}` → `ChunkWritten{handle, total_bytes}`

One chunk is **at most one timepoint slab**: `len(channels)` volumes of the
artifact's `(Z, Y, X)`, concatenated in `channels` order, §2's layout, so a
`segment` source's `len == len(channels) · Z·Y·X · sizeof(dtype)` — and the
plugin bounds-checks that before reinterpreting a byte (the host is trusted;
cheap honesty keeps the check symmetric). `source` is a `SegmentRef` into `X`
or `inline` bytes; the reference host sends **one channel per chunk, one chunk
at a time, always at the arena's start** (`INTERIOR_OFFSET`), and never
`inline`. `channels` are **source** channel indices, in the order the artifact's
`C` axis should carry them; the `i`-th volume of the payload is channel
`channels[i]`. **A source channel's artifact index is its rank among the
distinct source channels the export names**, which the host sends in artifact
order — the same rule as `t`'s below, and the one that works when a chunk
carries one channel (the reference host's shape), where a payload index of `0`
would put every channel of a `t` at artifact index 0. The reference SDK hands
`write(t, c, …)` the source channel and leaves the ranking to the exporter.

**`t` is the source timepoint too**, never an artifact index: an export of
timepoints 40–49 sends `t = 40 … 49`, and `t < T` is not a check. The artifact's
time index of a chunk is **the rank of its `t` among the export's distinct
`t`s** — the reference host sends distinct `t`s in ascending source order, one
after another, so the `k`-th distinct `t` to arrive lands at artifact index
`k`, and an exporter that must name an indexed time axis before `FinishExport`
counts distinct `t`s as they arrive. The two fields are shaped the same way on
purpose: an exporter that also reads the source (`ctx.host`) can address it
with what a chunk carries, without a selection it was never told.

**The reply is the handoff.** The host reuses the arena the moment
`ChunkWritten` (or the `Error`) arrives — it does not wait for the plugin's
`ReleaseDeposit` — so a plugin has consumed or copied the bytes *before* it
replies. The `ReleaseDeposit{segment_id, region 0, offset, len}` is still owed
by §5.6 and the reference SDK sends it at the request's disposition; the
reference host accepts it and applies it to nothing. A plugin that never sends it
strands nothing on this host and a ring's capacity on a host that counts.

`total_bytes` is **committed** bytes and is authoritative: ingest is a hint.

### 2.4 `FinishExport` and `AbortExport`

`FinishExport` → `ExportDone` once the artifact is complete on disk; an
`Error` here means the artifact is *not* usable, and an unknown handle is an
`Error`. `AbortExport` → `ExportDone` always (`control_plane_protocol.md` §8.6
— "no reply" is not an option), and the partial artifact is abandoned and
cleaned up; for an unknown handle the reference SDK still answers
`ExportDone`, and §8.6 permits an `Error` — either conforms, since the host is
already abandoning the job and reads nothing from the reply but its arrival.
After either, the handle is dead and a chunk naming it is `Error{NOT_FOUND}`.

### 2.5 What an exporter must not do

- **Reply before it has consumed the bytes** — the reply is the handoff (§2.3).
- **Write a measurement it was not given** (§2.2).
- **Finalize on abort** (§2.4).

---

## 3. Where the reference is behind this text

Kept here as well as in `control_plane_implementation_details.md` §2b so a
reader of this document alone knows. **Nothing.** The reference host and the
reference SDK implement every rule above. A row here never changes what a
conforming plugin sends.

---

## 4. Conformance, in one list

A reader conforms when: it declares shape, dtype and what calibration it knows
in `ReaderOpened`; it answers every read from the session's `G` arena with a
descriptor whose `len` matches `shape_zyx × dtype`, level 0, C-order
little-endian; it refuses any other level `OUT_OF_BOUNDS`; it does not reuse
lent bytes before `ReleaseLoan`; it answers `Metadata` with a form or the empty
form; it closes idempotently; and it answers `NOT_FOUND` for a handle nothing
is open under.

An exporter conforms when: it holds a chunk whose segment it has not installed
rather than failing it; it checks `len` against `channels × shape × dtype`; it
consumes the bytes before `ChunkWritten`; it sends `ReleaseDeposit` for what it
took; it writes no measurement it was not given; it finalizes on `FinishExport`
only and abandons on `AbortExport`; and it answers `ExportDone` to an abort
regardless.
