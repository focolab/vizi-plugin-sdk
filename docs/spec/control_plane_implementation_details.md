# Control plane implementation details

How the reference host and the reference SDK implement
`control_plane_protocol.md`, and where the shipping host differs from it.

Specification: `control_plane_protocol.md`. Principles:
`control_plane_principles.md`, `plugin_principles.md`. The layers this one
serves: `doorbell_protocol.md` + its op documents, `shared_memory_protocol.md`.

---

## 1. What ships

**Protocol `1.0`**, implemented by vizi 1.0.0 and later. The plugin end is
`vizi_plugin_sdk/`, whose `control.py` is the control-plane layer this document
describes from the other side. The Go, Rust and Java SDKs under `polyglot/` implement
the same documents independently.

The pieces, and where they live:

| Piece | Where |
|---|---|
| IDL | `proto/vizi_plugin.proto` |
| Plugin control plane: classify, arrange, acknowledge | `vizi_plugin_sdk/control.py` |
| Plugin server, dispatch loop, `ctx.host` | `vizi_plugin_sdk/server.py` |
| Host session, queues, correlation, launch, leases, request timeouts, diagnostics, the drain, panel/overlay/ROI application, answers to plugin requests, grants | the reference host, described here by behavior |

**The transport.** One bidi stream per session whose lifetime is the session;
the plugin serves and the host connects; a version check at both ends of the
handshake, on the MAJOR (`protocol` §3.2); a single-in-flight request slot with
a queue behind it; a one-way send that bypasses that slot; separate queues per
message class, drained on a divided budget. Everything
`control_plane_protocol.md` §5 describes as a class exists as a mechanism.

**The contract is enforced, not only stated.** The class of every message is a
partition each end fails to start without, a class is decided before an id is
matched, and every arrangement has a deadline (`protocol` §9).

**Read a claim about behavior as an observation only where it cites a file.**

---

## 2. Where the shipping code differs from the specification

**On the wire, nowhere it knows of.** The table below says how the reference
host and SDK realize the rules an implementer is most likely to ask about.
**Behind the wire, in what the reference host does with a conforming message,
§2b is the ledger** — the places where a plugin written to the specification is
correct and will see the promise kept by a later host.

| Area | What the reference implementation does |
|---|---|
| Plugin→host descriptors | a loan, released by the host — `ReleaseLoan`, and `PluginReader` releases a reply's deposit the moment it has copied it (`protocol` §5.6) |
| Correlation ids on pushes | empty, and legal (`protocol` §4's empty-id rule) |
| Message and field size caps | 32 MiB message at the host, enforced (`MAX_DECODE_BYTES`, an oversized message faults the session); 8 MiB inline at the SDK — §3.9 |
| Grant identity proof | carried on success, **compared by the host** (`protocol` §8.12) |
| `DoorbellFault` authorship | composed: transport facts + supervisor facts (`protocol` §8.13) |
| `PanelOpened` | a control-plane one-way (`protocol` §8.4) |
| `ReleaseDeposit` | carries `region` (`protocol` §8.7) |
| Arrangement | a class, with acks and deadlines: `ToolBinding`, `TimeMapping`, `EpochChange` (`protocol` §5.5) |
| Segment meaning | `InstallSegment`, one table per purpose (`protocol` §8.12) |
| Grant acknowledgement | required, meaning *installed* (`protocol` §8.12) |
| Doorbell fault reporting | `DoorbellFault` / `ContractViolation` / `DoorbellShutdown` (`protocol` §8.13, `doorbell_protocol.md` §10) |
| Deadlines | every arrangement has one (`protocol` §9) |
| Plugin-side version check | both ends check, on the MAJOR (`protocol` §3.1, §3.2) |
| Superseded loan | released at replacement time (`protocol` §5.6) |
| Plugin arena exhaustion | fail the push and count it (`protocol` §5.6) |
| Loan allocator scope | one cursor per **plugin-produced `LOANED` region** (`protocol` §5.6) |
| A plugin exiting 0 | an ordinary ending (`protocol` §3.3) |
| Handshake identity | `Hello from <version string>` / `Ready; I am <version string>`, form-checked strictly at both ends, both recorded in the plugin's log (`protocol` §3.1) |
| Segment sizing | the preamble plus the payload padded to §7's slot |
| Host writes into `V` / `X` | from the interior, past the preamble |
| Command and plugin-reader deadlines | none while the request is **supervised** — shown in the activity list, or behind the load dialog — and the fixed 10 s / 30 s values only with no window; progress and cancel on the wire (`protocol` §8.16) |

**The loan allocator's unit is the region, exactly.** `protocol` §5.6 is
explicit that the unit is one plugin-produced `LOANED` region and that *"one
word off in either direction is a defect"*: too narrow — per sender — lets two
senders allocate the same bytes, and too wide — per segment — is a receiver
allocating across a region boundary into bytes with a different discipline.

### 2b. Where the reference host is behind the specification at `1.0`

Each row is a promise the specification makes that the reference host does not
yet keep. A plugin written to the specification needs no change when it is
kept; an SDK author reading this table knows what vizi's records contain.

**No row is recorded.** A receiver handles both position kinds a 3D pane can
send, `RAY` and the pick beside it (`doorbell_E_input.md` §3).

### 2a. What the reference host does with a conforming message

Host behavior behind the wire that a plugin author can rely on.

| Area | Reference host |
|---|---|
| §9's bounds | the host walks every plugin message at the drain, checking bytes, strings and repeated counts, and refuses over a bound with the answer §9 names; the host refuses a grant past the session's 4 GiB ceiling or an arena's 4× growth before anything is created (§9) |
| What a label volume is | one definition, applied at the push, reported to the plugin on `overlay_item_refused`, and applied again by the compile; inline geometry is the one displays-but-does-not-compile case, named as the compile's transport rule (§8.8) |
| A plugin `LineSet` in 2D | drawn by the registry overlay both panes and export share: projected into every slice, solid in its own, dashed out of it, loops closed, per-vertex colors kept (§8.5) |
| `OverlayStyle.thickness` | pixels, as the IDL says |
| A 2D tool event under a projection | `COLUMN_YX`: the pixel's `(y, x)` and the Z range projected, which is the whole extent (the MIP projects every plane; the global crop's Z is ignored under a projection) — `doorbell_E_input.md` §3 |
| A plugin reader's calibration | `Metadata` asked once per open and parsed like any OME document; the declared spacing and time increment laid over it per axis; a calibrated plugin dataset measures, budgets and exports as one (`reader_writer_contract.md` §1.7) |
| Tool bindings | the modal contract: the engaged tool owns the primary and every unkept left chord; a momentary trigger's chord engages it for one gesture; a toggle key is the tool's shortcut; an action key fires into it mid-gesture as `E.action`; the host's chords are one table the resolver refuses from (§8.10) |
| Message classes | a partition over each oneof, enforced at both ends (§3.1) |
| Host→plugin dispatch | kind first, always (§3.1) |
| `AssetResult` routing | routed; an unclassified arm fails at startup (§3.1) |
| Unmatched reply id | counted and logged (§3.1) |
| Handlers on the dispatch path | none: every handler runs on a worker (§3.1a) |
| Plugin push queue | split coalescing / one-shot (§3.2) |
| Coalescing | replace per key (§3.2) |
| One-shot overflow | session fault (§3.2) |
| Drain priority | 16 / 8 / 16 capped, then an uncapped spill pass (§4) |
| What starts the drain | a session existing (§4) |
| A dropped reply's resources | released by the dispatcher on every abandoning path (§3.7) |
| `Shutdown` | sent on every voluntary teardown (§3.5) |
| The plugin's cleanup | `@on_shutdown`, on a dedicated worker, inside a bounded window (§3.5) |
| A plugin closing its stream | the completion signal, and not a failure (§3.5) |

---

## 3. The mechanisms

Each subsection names one rule of the specification and the mechanism that
implements it.

### 3.1 Unsolicited host→plugin messages are dispatched by kind

Every host→plugin message is classified before any id is matched. The SDK
holds an **exhaustiveness check over `HostMsg`'s oneof**: named sets — reply,
worker-delegated, loop-consumed — that must partition the descriptor's field
names, asserted when the servicer is constructed, so a session cannot start
against an unclassified arm. Startup rather than build time because the
generated stubs are a plugin-side dependency resolved on first use.

**It is checked as a PARTITION rather than a cover**, which is not the same
check: a union cannot see an arm in two classes.

Ids are matched only within the reply kinds (`protocol` §4 rule 3). A reply id
matching no waiter is counted and logged at powers of two (rule 5) —
**aggregated by the condition, not by the formatted message**, since a key
carrying the id would log every late reply as its own first occurrence and
retain one entry per message, on exactly the path that exists because a peer is
misbehaving. A dropped reply is still consumed for the resources it carries
(rule 6); that is §3.7.

The host end has the mirror: `classify_plugin_msg` is an exhaustive `match`
over `plugin_msg::Msg`, so a new arm in the IDL is a **compile** error there
rather than a startup one.

Without this rule a host that deactivates a session for a doorbell integrity
failure could not tell the plugin why: `DoorbellShutdown`, `EpochChange`,
`InstallSegment`, `RetireSegment`, `GrantStateQuery`, `ToolBinding`,
`TimeMapping`, `PanelOpened` and `ReleaseLoan` are all unsolicited.

### 3.1a No handler runs ON the dispatch path

`control_plane_protocol.md` §6 opens *"neither end may run work on the dispatch
path"*. Every handler arm is real work — §9 gives `export_chunk` 120 s, and
`open_reader`, `read_volume` and `read_plane` have no deadline at all — so a
handler run inline would hold a `GrantAck`, an `EpochAck` or a
`DoorbellShutdown` behind it. §9's acknowledgement deadlines are derivable only
because this rule holds.

The SDK maps every handler arm to a serialized worker: commands on one, reader
calls on another, export steps on a third (`vizi_plugin_sdk/server.py`).
Separate workers because a command handler blocking in `ctx.host` must not
stall an export and a 120 s chunk must not stall a command; serialized within
each because stream order is the contract and a reader is stateful. What stays
on the loop is routing and nothing else.

Two consequences of running handlers on workers:

- **A handler's exception.** An exception escaping a handler on a worker would
  be swallowed into a future nobody observes: session alive, request
  unanswered, and every later request stuck behind the in-flight slot until the
  host's timeout. The SDK therefore replies `Error` on any escape — the kind
  every host-side caller already handles — and counts it.
- **A reply still being computed at end-of-stream.** The SDK waits a bounded
  5 s — §9's session shutdown grace — for its workers before signaling the
  outbound generator, so a reply that already exists is not discarded.

### 3.2 Two push queues, and drop-oldest is not coalescing

Drop-oldest on a FIFO evicts the oldest *message*, not the oldest *value for a
key*: a lone pending patch for widget A is destroyed by a burst of patches for
widget B, and no later message names A. And much of what a plugin pushes is not
replaceable at all — `OverlayRemove`, `ReleaseDeposit`, `GrantAck`, `EpochAck`,
`SegmentReleased`, `DoorbellFault`, `ContractViolation`, `DoorbellFinalReport`.
The host therefore keeps two queues, per `control_plane_protocol.md` §5.4 and
§7:

- **A keyed coalescing queue.** At most one pending item per
  coalescing key — `(panel, widget)`, `(overlay id)`, `(segment id, region)` —
  replaced on arrival, keeping the key's position in the drain order. A
  multi-widget `PanelPatch` splits into one entry per widget on the way in and
  drains as one patch per widget, which is legal because ordering *between*
  keys is not contractual. `SegmentShortfall` merges by its own rule: largest
  `wanted_bytes`, summed `dropped`, and the `largest_free_bytes` that
  accompanied the retained want, since the two describe one failure.

  **A dropdown's two properties each depend on the state the other
  establishes, so no ordering of merged ops is correct.** `set_options` clears
  a dropdown and restores its selection *by text*, and `set_value` is checked
  against whatever list is there. A widget that has been sent a `set_options`
  is therefore order-sensitive and its ops are held whole; every other widget
  takes its value whole and still collapses. Being sent a `set_options` is how
  the transport knows without holding the manifest. **The test for a merge
  rule is the widget STATE it produces, never the operation order it emits.**
- **A one-shot queue that never drops.** `ONE_SHOT_CAP` is 256 and overflow
  ends the session (`protocol` §10) rather than evicting. The reason is
  recorded on the session rather than only handed to the request waiters,
  because a queue-policy fault often has none: an idle session flooding pushes
  has no in-flight call and nothing queued.

**The key space is structural and the transport cannot see it, so it is TOLD
the number.** §5.4 sizes the space from the manifest and §11 refuses a patch
naming an undeclared widget as validation — both at the application layer,
which holds the manifest. The transport is given a coalescing capacity the
host computes as `Σ declared widgets + declared overlays +
COALESCING_SEGMENT_KEYS`. No fixed constant is safe: the manifest caps widgets
at 256 **per panel** and does not cap the number of panels, so a manifest of
seventeen full panels declares over four thousand widgets and is accepted. A
default exists for callers with no manifest to offer (the reader-only launch
paths).

### 3.3 Arrangement is a one-way push plus an acknowledgement

A host request occupies the **single in-flight slot**. An `EpochChange` sent
that way would queue behind a running `InvokeCommand`, and
`doorbell_E_input.md` §1a stops emitting `E` for a pane until its epoch is
acknowledged — so a slow command would silently kill tool input for that pane.

Arrangement is therefore a **one-way push plus a plugin→host acknowledgement**
(`protocol` §5.5), sent on the same one-way path a data-access reply uses so
that it cannot deadlock behind the invoke that triggered it.

### 3.4 Deadlines on everything that can go absent

`doorbell_M_memory.md` §7 and `doorbell_E_input.md` §1a both depend on a
deadline for a message that never arrives, and both say the value is host
policy. `control_plane_protocol.md` §9 carries the values — 10 s for the three
acknowledgements, 30 s of no-progress, a retry budget of 3, and a 2 s + 5 s
teardown split. The host arms a timer for each, off its pump, and holds the
values in one definition. It unifies the **values, not the predicates**: *"is
this arrangement overdue?"* and *"is this grant overdue?"* have different
answers.

**The grant acknowledgement deadline is the one whose failure is otherwise
silent.** On Windows the first doorbell send after an orderly peer close
returns `Ok` and only the next write reports (`doorbell_protocol.md` §6), so a
grant can be lost with the send reporting success and nothing at the transport
layer can see it. The ack that never arrives is the sole evidence.

### 3.5 `Shutdown` is sent on every voluntary teardown

`DoorbellShutdown`'s grace window runs on this channel while the session is
still alive (`doorbell_protocol.md` §8), so a teardown announces itself before
it closes the stream:

- **`ShutdownReport{outcome, detail}`**, `PluginMsg` arm 35. §3.3 requires the
  plugin to say its outcome before it closes, because hook-completed,
  hook-raised, no-hook-registered and dispatch-died-of-something-unrelated are
  **one event at the socket**.
- **The host sends, then waits** for the session to close, without reaping:
  one removal point is what lets a whole-application shutdown open every
  window before waiting on any of them.
- **`@on_shutdown`**, bare rather than a factory: one hook per plugin, so there is
  no id to take, and `@on_shutdown()` is a `TypeError` at import. It runs on a
  dedicated thread, never the command worker, so a hook queued behind a long
  command still starts before the deadline. The hook's context has no `host`:
  §3.3 forbids starting new work in the window.
- **The dispatch loop keeps routing after `Shutdown`.** It starts the hook and
  continues, because an in-flight command may still be waiting on a
  data-access reply and the loop is the only thing that can route it. A
  teardown thread closes the stream when the hook finishes or the window
  expires; once a window is open, that thread owns the close.

Rules the window imposes on both ends:

- **A dead peer skips the window.** *Already torn down* and *nothing to
  announce* are different answers, and neither spends the grace.
- **The window closes ADMISSION.** §3.3's "the hook may not start new work" is
  unenforceable while the peer keeps handing work out, so the host stops
  promoting queued requests and the SDK refuses arriving ones — and both
  answer, rather than leaving a caller to time out.
- **An unknown outcome does not strand the process.** The enum is append-only,
  so a newer plugin sending a value the host does not know is conforming; the
  host reports it inside its own guard and reaps unconditionally.
- **The actor exiting is not the plugin having finished.** The actor also
  exits on a transport error, which is the case `ShutdownReport` exists to
  distinguish.

### 3.6 Head-of-line blocking is avoided by rule

A handler blocking in `ctx.host` must not stall the routing that would deliver
its reply. That is a **rule** (`control_plane_protocol.md` §6) and it is
enforced by construction: an arm is classified to a worker or it does not reach
a handler at all, and the startup partition refuses a session where one is
missing. The paths that must not queue behind a command have their own
workers — the grant install and its acknowledgement is the case
`doorbell_M_memory.md` §7 requires be prompt and bounded.

### 3.7 A dropped reply's deposit is released

A plugin request that times out leaves the host still serving it. The reply
arrives afterwards with no waiter, **carrying a `DataVolume` or `MaskVoxels`
descriptor that names a live ring reservation.** The host cannot reclaim it:
memory it loaned is not reclaimable on a timer (`plugin_principles.md` 5), and
it has no way to know whether the plugin received the reply.

**The cost of leaking one is the process's address space.** The host's deposit
ring reclaims FIFO, so one abandoned deposit at the head pins every later
released deposit behind it; and when the ring cannot reserve, the host grants a
new segment at twice the capacity rather than failing. One unreleased
reservation per doubling doubles the plugin's shared-memory footprint while
every operation continues to succeed.

`control_plane_protocol.md` §4's resource rule is therefore implemented as one
helper (`_release_abandoned` in `vizi_plugin_sdk/control.py`) called from every
abandoning path — the receiver owns the resource from arrival:

- a reply whose waiter has gone;
- a reply that arrives between the caller's timeout and its waiter being
  removed — the waiter lookup and the delivery are one critical section against
  the removal, so a reply is either delivered to a live caller or released;
- a failed copy out of the deposit;
- every validation between the reply arriving and the copy consuming it — a
  wrong reply kind, an unsupported dtype, a `len` disagreeing with the shape, a
  mask naming the wrong region. These are covered by a guard around the
  validation rather than per call site, so a validation added later inherits
  the property.

Only two reply kinds carry a releasable reservation, `DataVolume` and
`MaskVoxels`; a `ViewState` nobody waits for owns nothing.

### 3.8 Plugin→host descriptors are loans

A plugin writes into its own granted segment and sends a descriptor on this
channel. The bytes are a loan until the host releases them
(`plugin_principles.md` 5); "the host has probably read it by now" is not a
release. Two pieces implement this, and neither works without the other:

- **The allocator is arena-scoped**, one cursor per plugin-produced `LOANED`
  region (`control_plane_protocol.md` §5.6). Two declared overlays, or two
  image widgets patched in one handler, allocate from one cursor and never
  from the same byte. An arena-scoped allocator with no release is monotonic
  and exhausts in about forty pushes, which is why the release is not
  optional.
- **The loan has a lifetime** (§5.6's disposition table). The host releases what
  it is done with — applied, superseded, or rejected — via `ReleaseLoan`, and
  the SDK's arena keeps a reservation until that release names it.

The ledger lives in the host, where the release is sent, and counts a
reservation's declared uses down across all three of §5.6's dispositions:

- **Supersession happens inside the host's queue**, so the queue keeps what a
  displaced message NAMED — descriptors, never the message, which would retain
  `inline` payloads that are not loans.
- **Rejection is covered structurally.** The drain notes a message's
  descriptors before dispatch and disposes of them afterwards whatever the
  outcome. The ledger is never told which disposition happened, because §5.6
  is total over them.
- **One accounting.** Every path that consumes a descriptor — the UI pump, the
  headless drain, a reader's own reply, a reply nobody waited for — reaches the
  same ledger, and `uses` counts messages across all of them, so one
  reservation is released by whichever disposition is last.

The one obligation that is the SDK's is the sender's `uses` declaration (§5.6):
a reservation named by more than one message says so, or the release comes back
early.

---

### 3.9 The bounds, and where each is enforced

`control_plane_protocol.md` §9's size and queue bounds:

| Bound | Value | Reference host |
|---|---|---|
| maximum decoded message | 32 MiB | enforced (`MAX_DECODE_BYTES`; an oversized message faults the session with its size) |
| maximum inline byte field | 8 MiB | enforced at the drain (§2a); the SDK refuses on send |
| maximum repeated-field count | 65 536, three exceptions | enforced at the drain (§2a); the SDK refuses on send |
| host request queue capacity | 32 | the inbound side is bounded (`REQUEST_CAP`); the outbound `call` queue is not |
| one-shot queue capacity | 256 | enforced (§3.2) |
| per-pass drain shares | 16 / 8 / 16 of 32, then an uncapped pass | as specified (§4) |
| per-session shared-memory budget | declared + 2× largest deposit, 4 GiB ceiling | enforced at the grant (§2a) |

A plugin sending more than 32 MiB in one message faults its session with the
size. The number is set against `GetRois`, the one reply that can approach it:
the ROI-pose ceiling it accepts is in §9's "Why these values".

Nothing legitimate approaches the repeated-field and inline-field caps, so
enforcing them refuses only traffic the specification already calls a defect.

---

## 4. The drain, and why its interval is contractual

The host drains every live session's inbound queues on one timer: 40 ms, at
most 32 messages per session per tick — about 800 messages per second per
session. **Draining is keyed on a session existing, not on the surface that
created it**: a reader-only session with no panel, command or tool is drained
like any other, because a grant acknowledgement is exactly what such a session
needs to send.

**It is a published bound, not an implementation detail.**
`doorbell_D_notification.md` §5 requires that every actively consumed Pulsed
region be re-checked within a documented finite bound, independently of `D`, and
names this interval as vizi's. Changing it changes an advisory-channel
guarantee.

**As specified in `control_plane_protocol.md` §7, a per-pass cap must not be
able to stall a queue.** A pass that ends at 32 with messages still queued is
fine here because the next tick follows on a timer. It would not be fine on an
edge-triggered drain, where unread data produces no further readiness edge —
the same trap `doorbell_protocol.md` §11 names for the unix doorbell reactor,
which is where the analogous fairness mechanism has to be built differently.

**~800 messages/s is the ceiling for everything on this channel**, in aggregate,
per session. That is generous for session-rate traffic and it is why every
reporting message must be rate-bounded at the source
(`shared_memory_implementation_details.md` §2): a violation reported per doorbell
record would arrive at ~1 kHz against an 800/s drain, on the channel the session
needs in order to recover.

**The budget is divided, never spent in strict priority.** Serving requests
strictly first lets a plugin holding enough concurrent reads refill the request
queue before every tick, so the push queues are never reached: releases stop
being drained and the deposit ring never reclaims; grant and epoch
acknowledgements stop being drained and arrangements hit their deadlines; and
the one-shot queue fills, which §7 makes a session fault — on a session that
did nothing wrong.

A pass therefore runs the three queues twice in §7's priority order — capped at
16 / 8 / 16 in the first, uncapped in the second — so no pass ends under budget
with work still queued and no queue holding work is served zero. A single pass
with a reserve does not have those properties: it leaves budget unspent while
acknowledgements wait, and lets two saturated push queues serve zero requests.

## 5. The host's implementation, by mechanism

protobuf supplies this channel's encoding, so it needs no conformance corpus
(`doorbell_protocol.md` §13 is the doorbell's). What follows is how the
reference host implements each mechanism, and the choices in it that are
decisions rather than transcription. Everything here is part of `1.0`: one
`major.minor` implies exactly one control plane and one doorbell format
(`control_plane_protocol.md` §3.2), and a plugin either speaks `1.0` or is
rejected at the handshake.

1. **Dispatch, queues and the drain** — §3.1, §3.1a, §3.2 and §4. These are
   what make every other message *deliverable*.
2. **`Shutdown`** — §3.5.
3. **The arrangement class (§3.3).** `ToolBinding` is its simplest tenant: one
   message, one ack, one high-water mark.
4. **`InstallSegment` and the grant acknowledgement.** The host's segment
   ledger builds and sends `InstallSegment`, compares the identity proof on
   `GrantAck`, runs §9's 10 s / 3-send / `GrantStateQuery` backstop and applies
   `GrantState`'s five answers; `RetireSegment`, `SegmentReleased`,
   `ReleaseRequest` and `SegmentShortfall` are wired into the drain.

   **The grant does not go through the arrangement class.** §9 gives the grant
   `GrantStateQuery` where the other tenants get an idempotent re-send, because
   only the peer knows which of the two halves is missing.

   **The ride-along is used for `DataVolume`.** The ledger's descriptor call is
   the gate and its exception in one: an installed segment sends the descriptor
   alone, a *declared* one sends it with the `InstallSegment` embedded, and
   anything else refuses.

   **`X` is installed standalone at the grant, and the host waits for no
   acknowledgement before the first chunk.** The receiver's rule in §8.12 makes
   that correct: a descriptor into a segment the receiver has not yet installed
   is held until it is, whichever half is missing. `X` is granted once per
   export job, so a ride-along would save nothing, and the standalone form
   keeps `ExportChunk` a bare descriptor. The host meets §8.12's deadline
   suspension by ORDER: the `M` is dispatched at the grant and the installation
   precedes any descriptor, so both halves are in flight before a request is
   issued, and a capability that never lands is the grant's own 30 s
   escalation, inside the request's 120 s. The host has no inline export path
   and never sets `ExportChunk.install`.

   **A `not_present` answer re-sends the doorbell capability.** Re-sending the
   installation alone against a peer holding neither half would leave it
   pending forever while the host believes it retried.

   `shared_memory_protocol.md` §3.3 settles what a live overlay lane carries: a
   serialized `OverlayUpdate`, no envelope, inline geometry only, `overlay_id`
   equal to the segment's `subject`.
5. **`EpochChange` / `EpochAck`, with `TimeMapping` / `TimeMappingAck`.** The
   host holds the per-`view_id` counter, the per-`gen` mapping and the state
   snapshot, arranges both per tick and routes the two acknowledgements.
   **This one IS an arrangement-class tenant**, with an externally owned
   revision: `EpochChange` carries no `revision` field because
   `PaneEpoch.epoch` *is* one, and `doorbell_E_input.md` §1a fixes it as the
   `view_id`'s, not the plugin's.

   - **The gate compares against the state the host is in NOW, not the last one
     emitted.** §1a stops emission when a parameter *changes*, and that is a
     different moment from when the replacement epoch is allocated — allocation
     waits for somebody to be eligible to receive it. A session that cannot
     advance (a mapping it has not acknowledged, a peer gone quiet) would
     otherwise keep an open gate and go on emitting under geometry the user has
     already moved off. The snapshot refreshes on the pump tick, so the bound
     is one pump interval; the window is the straddling gesture §1a already
     covers.
   - **One pane per message**, though §8.10 permits several. The acknowledgement
     is per-pane regardless, so keying the message the way the ack is keyed
     makes the retry per-pane too; batched, a pane whose ack was lost would drag
     an acknowledged one along on every re-send.
   - **The epoch chain is established for tool-declaring sessions only.** Those
     are the sessions an input event can address. Sending an epoch elsewhere is
     a round trip for nothing and a deadline against a peer with no reason to
     answer.
6. **The deadlines (§3.4).** Each arrangement tenant and the segment ledger run
   a timer off the host's periodic drain, and the values have one definition. The host's
   tests read §9's table out of these documents and compare both ways, so a row
   added to §9 with nothing behind it fails.
7. **`DoorbellFault`, `DoorbellShutdown`, `ContractViolation`.** Every
   `PluginMsg` arm is classified and handled, and the host checks that
   exhaustively.

   **The two reports fail in opposite directions.** `ContractViolation` is a
   semantic disagreement — report, isolate, **keep the session** — and costs
   the record it names and nothing else. `DoorbellFault` is a framing fault and
   tears down, because there is no way to keep using a channel whose framing is
   in doubt.

   **It tears down the DOORBELL first and the session after.** A panel, a
   reader or an exporter needs no doorbell for its own traffic, but every grant
   is an `M` on the doorbell, so a session without one can be given no new
   memory. `DoorbellShutdown` opens the window for the report, and `Shutdown
   {session_fault}` follows it naming the doorbell's failure
   (`control_plane_protocol.md` §8.13).

   - **Emission stops at the FAULT, not at the window's expiry.** The grace
     window is for the peer's *report*, not a license to keep writing into a
     channel both ends have agreed is broken.
   - **A repeat fault does not restart the window.** A peer raising a second
     has a doorbell already closed at this end.
   - **An exhausted retry budget escalates** (§9); it is never only a returned
     value.
8. **The host's doorbell.** `1.0` is "control plane `1.0` **and** doorbell v2"
   (`control_plane_protocol.md` §3.2, `doorbell_protocol.md` §3.1a).

   - **The doorbell is attached AFTER the gRPC handshake, to the session id it
     returns.** The socket buffers anything sent in between, and
     `doorbell_protocol.md` §3.1a forbids the plugin sending before `Ready`.
   - **`segment_id`, `interaction` and `seq` are allocated inside the writer's
     admission.** `doorbell_E_input.md` §2 and §6 require the two counters to
     burn *on admission* — a `DRAG` dropped on refusal has burnt its `seq` and
     the gap is what the receiver counts — and only the writer knows admission.
     Every `E` is also self-checked through the receiver's record-local rules
     before admission, so a host bug is refused at the host rather than
     reported back as a `ContractViolation`.
   - **Every grant goes through one place in the host, and its two halves are one call.**
     A reader opened headlessly, an export job, a data-access reply, a panel
     and an overlay all take that path. The `subject` check (§8) runs
     **before** the doorbell creates the segment and sends its `M`: a refused
     grant sends nothing on either plane.
   - **One `O` segment per overlay and one `P` per panel**, `subject` = the
     contribution id, because that is what the plugin end keys its arenas and
     lanes on. `V` defers its installation to ride the first `DataVolume`
     (§5.5) and a ring that outgrows itself names the one it replaced (§9.5);
     `X` is installed standalone (item 4).
   - **Closed to traffic and released are different moments**
     (`doorbell_protocol.md` §10). A fault stops the emitters at once; the
     socket is released when the grace window closes, by expiry or by the
     peer's final report, never at the fault.
   - **A 3D pane sends the ray it cast** (`doorbell_E_input.md` §3), with the
     `MISS` flag and a synthetic `far` on a miss; `roll` is measured against
     the dataset's +Z, or +Y with `ROLL_REF_Y` near the singularity. A 2D pane
     sends a voxel, and an event over a gutter is still an event — `KIND_NONE`.
   - **The host consumes a Pulsed lane's `(view_id, epoch)` pair.** It reads
     the pair from the slot's last eight bytes (`shared_memory_protocol.md`
     §3.2), applies a value only while the pair names the pane's current
     epoch, counts a stale one, and applies a pair-less value unconditionally.

---

## 6. Rules an independent implementation depends on

Four rules that an implementation written from these documents alone needs
stated, and where each is:

- **Identity proof validation.** The grant acknowledgement carries the proof
  (`doorbell_M_memory.md` §5's 16 bytes at `[12..28)`) on success and the host
  compares it (§8.12). It exists for the Windows `CreateFileMappingW`
  create-or-open failure.
- **Who fills a `DoorbellFault`.** `doorbell_protocol.md` §12 asks for plugin
  id, manifest source, SDK version and stream health; the transport's own
  boundary (`doorbell_implementation_details.md` §6) excludes all of them. The
  message is **composed** by the supervisor rather than forwarded (§8.13).
- **A token mismatch refuses the stream, not the session or the launch**
  (§2.2) — the plugin checks the token before accepting the stream, in call
  metadata or in a first `Hello`, refuses a stream that cannot prove it
  `UNAUTHENTICATED`, and keeps listening for the host's.
- **One terminal doorbell event yields one `DoorbellFault`** (§8.13), even where
  a transport surfaces it through both a terminal mailbox and the ordered
  outcome stream.

---

## 7. Open

- **Whether the request timeouts should be a documented table or per-call
  policy.** They are per-call in the host; `control_plane_protocol.md` §9
  collects them.
- **Whether the one-shot queue's capacity is per session or per class.** A
  per-class bound is more honest about what overflow means but multiplies the
  constants; a per-session bound is one number and one fault.
- **Host event pushes** — a host→plugin push that is neither a reply nor an
  arrangement is a class this document does not have.
- **Whether the drain should be adaptive.** 32 per tick is a fairness cap; a
  session doing bulk ROI contribution and one doing nothing get the same share.
