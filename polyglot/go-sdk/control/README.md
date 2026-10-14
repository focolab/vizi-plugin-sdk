# Reliable control plane

`control` implements the plugin side of the specified **1.0** protocol, using
bindings in `protocol` generated from `proto/vizi_plugin.proto` and `roi.proto`.

## Surface

| API | What it does |
|---|---|
| `ServeEnv(ctx, Plugin)` | Starts the doorbell before endpoint publication; serves one session; closes resources on termination |
| `NewServer(Config)` / `GRPCServer()` | Separately embeddable session engine with major-version compatibility checks and a 32 MiB gRPC limit in both directions |
| `Config.HandleRequest` | Handles all ten reader/command/export request kinds on a serial worker; replies echo the host id |
| `Config.OnMessage` | Handles panel-open and peer-violation notices on another worker |
| `Config.Shutdown` | Dedicated shutdown worker; receives reason/detail; may send final messages; completion closes the stream |
| `Client.Call` / typed host helpers | Concurrent host requests with 30 s default timeouts, bounded waiters and exact reply routing |
| `Client.Send` | Reliable push admission; copies messages; preserves order; overflow faults instead of silently dropping |
| `Memory` | Grant acknowledgements and queries, retirement, embedded installs, descriptor copies and counted releases |
| `State` | Tool, pane and time arrangements; snapshot storage; transport lifecycle changes before acknowledgements |
| `Session.PublishLoan` | One-use P/O push publication with local rollback on refused admission |
| `Session.PublishLiveOverlay` | Inline protobuf publication, subject validation, pane tail and advisory D |
| `Session.WriteLoan` | Arena allocation with coalesced, rate-bounded `SegmentShortfall` reports on refusal |
| `FaultMessage` / `ReportViolations` | Preserve optional fault facts and deliver one session-wide violation reporter |
| `AssetsFromEnv` / `AssetPath` | Parse the host's asset mapping and distinguish undeclared assets from missing files |

`Client` has typed helpers for `GetViewState`, `ReadDataVolume`, `ReadMaskVoxels`,
`GetRois`, `GetRoisWindow`, `ContributeRois`, `ExtendRoiPoses`,
`CompileLabelVolume`, `ActivateTool`, `RequestAsset`, `RequestStatus`, and
`ReadRoiInclusion`, `RequestSavePath`, and `GetColormap`.
All published message types, including panels, overlays, geometry and ROI types,
are available through `protocol`. Application-specific handle management and
payload interpretation remain with the handler.

Filter provenance is exposed directly in generated types: DataVolume.Filter
describes that read, ViewState.Filter describes the displayed filter, and
OpenExport.Filter plus ExportChunk.FilteredChannels describe export provenance
per chunk. Absence means the file's own voxels. A view's filter must not be used
to infer a read's provenance; raw fallbacks can vary with channel and timepoint.
Set `protocol.ReadDataVolume{Raw: true}` to request the file's own voxels even
with *Plugins See Filtered Data* enabled. The reply has no `DataVolume.Filter`;
`ViewState.Filter` still describes the filter shown in the views.
The payload helpers retain this metadata with owned read/export volume copies.
The session snapshots whether each accepted OpenExport had a filter and rejects
contradictory filtered_channels before every raw or typed exporter callback:
the open must have a filter and each listed channel must belong to the chunk.
Rejection returns INVALID_ARGUMENT and releases the descriptor. Further chunks
and Finish are refused; Abort remains available to clean up the partial artifact.
An abort can also follow successful chunks when the host's filter fails. The
host retries once and aborts by default; raw fallback on failure is opt-in.

`control.FileStateDir(view) *string` exposes the current dataset's optional
state folder; it also accepts `protocol.OpenReader` and `protocol.OpenExport`
(the export's source, not its destination). Empty/absent values become nil.
Check for nil before joining a filename; protobuf fields themselves retain raw
wire strings. The accessor never creates the folder or requires it to exist.
The plugin creates it only on first write. `control.SettingsDir() (string,
error)` creates the host-named `VIZI_PLUGIN_SETTINGS_DIR` on first use without
needing a Client or command context. It returns `*SettingsDirUnavailableError`
for an unset/empty variable, creation failure, or a non-directory path. Neither
accessor creates a fallback. See the authoring guide's
[settings and per-file state](../plugin/README.md#settings-and-per-file-state).

Both routing tables are checked against the generated oneof descriptors when
`NewServer` starts. A new IDL arm must have a reviewed route before a server can
start; tests also verify that every request expects a classified reply.

Processing-stage negotiation is classified as `PlanStage` → `StagePlanned`
(or `Error`). `StageArm` is an arrangement and `StageArmAck` a push. Built-in
execution uses a dedicated worker with S mappings, periodic checks and D hints.
Unusable arms report STAGE_ARM_REFUSED without an ack or waiting for a grant.
The authoring layer supplies Plan, optional clean Check, and borrowed Process
callbacks; see the [stage API](../plugin/README.md#processing-stages). Custom
servers supply Config.StageMemory and StageNotify and call Client.HintStages;
ServeEnv wires them automatically.

A newer host's unknown message kind with a pending call's ID completes that
call with `ErrUnsupportedReply`, without ending the session. Any other nonempty
ID receives `Error{UNSUPPORTED}` with the same ID. An empty ID produces only a
local diagnostic. These unknown kinds do not increment `UnmatchedReplies()`.

`CallTracked(ctx, message, onAdmitted)` supplies the SDK-assigned correlation ID
once the request enters the outbound queue. The callback runs synchronously:
hand the ID to a monitoring goroutine and return promptly. That goroutine can
call `RequestStatus(ctx, id)`. `PENDING` describes host bookkeeping, not a promise
of completion; `UNKNOWN` is not evidence of failure. `AwaitingUser` says the
request is waiting for user interaction. Admission can precede host receipt, so
an immediate query may find no pending request. Caller-supplied message IDs are
still replaced by the SDK.

`RequestAsset` and `RequestSavePath` may be called and awaited inside a command;
the SDK allows both. Waiting holds the session's one request slot while
the person browses, so reads and other commands queue behind the dialog. Plugins
serving other work should use `control.AfterReply` (or an application-owned
background worker/task) for pickers and return from the command promptly.

The host holds its unsupervised command deadline while showing a picker. The SDK has
a separate `Config.PickerTimeout` for `RequestAsset` and `RequestSavePath`,
defaulting to 30 minutes. This is a dead-session backstop for a request answered
by a person, not a work budget. Ordinary calls retain `Config.RequestTimeout`
(default 30 seconds); an earlier caller context deadline wins for either.
Late replies with retired IDs are silently dropped, including releasing any
descriptors; they cannot fill the diagnostic queue.
`Client.UnmatchedReplies()` reports the cumulative per-session count of replies
received without a waiting caller. Reading it is concurrency-safe and does not
reset it. The count includes late replies, duplicates and unknown IDs without
distinguishing their causes; it emits no diagnostics and implies no fault.

`GetRois(ctx)` keeps the current-frame, pose-cleared read. `GetRoisWindow(ctx,
tStart, tEnd)` requests an inclusive dense pose window in the rest frame. Its
raw `RoiRecord` results retain the optional `PoseDomainTStart`/`PoseDomainTEnd`
pair on every posed region, even without a window; regions with no pose domain
carry neither. Compare that pair with `Roi.TStart`/`TEnd` to identify a partial
span. Presence alone does not mean a slice.
An unwindowed read supplies the domain for sizing a subsequent pose-window
request, but still contains no pose rows. Replacing an ROI with a partial window
would discard its other frames. If a requested reply exceeds the message ceiling,
the host returns `*HostError` with `OUT_OF_BOUNDS` and its size/limit message;
the session remains usable and the caller can request fewer frames.
`ExtendRoiPoses` lets the host apply dense corrections or adjoining extensions
without resending the whole pose; ownership, component matching, gap refusal
and batch atomicity are host validations returned as `*HostError`.

`ReadRoiInclusion(ctx, request)` returns a `RoiInclusionReply` owner, following
`ReadDataVolume` and `ReadMaskVoxels`: defer `Close`, then read/copy its optional
segment before closing. `reply.Inclusion` retains region ID, signed frame index,
open generation, presence, voxel count and level-0 origin. Its deposit contains
bbox-local U8 membership computed by the host using the posed shape and parent
chain. `Present=false` means hidden at that frame; `Present=true` with zero voxels
means empty coverage. Both have no segment and owe no release. A reply with a
segment uses the usual installation hold and counted release path, including
late replies and failed copies. `NOT_FOUND`, `OUT_OF_BOUNDS` and `UNSUPPORTED`
are ordinary host refusals. This request requires `data_access`.

## Build identity

A normal `go build` needs no identity configuration:

```go
cfg := control.Config{PluginID: "org.example.inspect"}
```

The SDK composes `Ready.id` from the plugin ID, compiled SDK source version
(`1.0a85`), and **metadata embedded by the Go toolchain**:
main module/package, full VCS revision and modified flag when available,
SDK module version/replacement, toolchain and build target. No timestamp,
random token, runtime git lookup or adjacent manifest is used to identify
the compiled code. A manifest may change independently of an
already-built executable, so it is not silently used as that executable's version.

The revision is labelled `main-vcs`, with `modified=true`, `false`, or `unknown`.
For true/unknown the identity explicitly says `not an exact source snapshot`.
Missing VCS data (including `-buildvcs=false`) is `unavailable`; missing SDK
module version data is `unknown`. The compiled source version remains `1.0a85`;
it does not replace the actual Go module version or imply an unmodified build.
A replacement's actual path/version is shown after
`=>`; a local replacement or unversioned workspace dependency requires a source
snapshot. A clean main checkout says nothing about those dependencies' state.
The SDK never labels the whole build as reproducible merely because its main
checkout was clean.

`Config.BuildIdentity` can supply an optional human label, such as a release
name or a reference to a retained source archive. It replaces only the plugin-ID
label; the embedded provenance is still appended. Invalid labels or a composed
identity exceeding 2048 UTF-8 bytes fail construction, before ServeEnv opens
resources. Provenance is never truncated to make a misleading identity fit.

**Reproducing and experimenting.** Retain the binary and inspect
`go version -m ./plugin`, or record `Client.BuildInfo()` for its complete embedded
module versions, checksums, replacements and build settings. Start with the
reported module version or main repository revision, the recorded toolchain,
and the dependencies in that metadata. If `modified=true`, the commit is only
the base: retain the source tree including untracked/ignored build inputs and
local dependency edits to recover that build. A dirty flag does not archive them; two different dirty builds can share the
same identity description. Equal descriptions are not proof of equal code.
Compare behavior first, then change one thing and rebuild; the next binary
records its own provenance automatically. `vcs.time`, if present in the full
metadata, is the revision's timestamp, not the compilation time.

This is build provenance, not a promise of identical behavior on every system.
Native libraries, datasets, runtime configuration and hardware still matter.
Even `modified=false` does not certify ignored/generated/external inputs. If
build metadata is unavailable, the identity says so rather than guessing.

The first `Hello.id` must likewise be `Hello from ` plus a nonempty version
string with the same bounds. Only its form is checked; arbitrary version syntax,
Unicode and manifest differences are accepted. Refusal names both identities,
quotes controls and truncates oversized received strings. Identity validation
runs before authentication/doorbell activation and Ready.

The Session handler sends gRPC response headers immediately, before reading
Hello. Headers indicate that the call is running; Ready still follows Hello's
identity/version and authentication checks. After Ready, the SDK reads and
dispatches immediately; Connected runs separately and cannot gate incoming
requests. This follows §3.1's eager-header recommendation and works with hosts
that wait for headers, although conforming hosts send Hello without waiting.

`Client.HostIdentity()` exposes the full validated host string, including its
prefix, from Attach onward; `Client.PluginIdentity()` returns this session's
Ready identity. Neither string participates in request/reply correlation.

## An application handler

The handler receives generated types. For example:

```go
func handle(ctx context.Context, host *control.Client, in *protocol.HostMsg) (*protocol.PluginMsg, error) {
    switch request := in.Msg.(type) {
    case *protocol.HostMsg_InvokeCommand:
        if request.InvokeCommand.CommandId != "inspect" {
            return nil, &control.HostError{Kind: protocol.Error_NOT_FOUND, Message: "unknown command"}
        }
        state, err := host.GetViewState(ctx)
        if err != nil {
            return nil, err
        }
        return &protocol.PluginMsg{Msg: &protocol.PluginMsg_CommandDone{
            CommandDone: &protocol.CommandDone{Ok: true, Message: fmt.Sprint(state)},
        }}, nil
    default:
        return nil, &control.HostError{Kind: protocol.Error_UNSUPPORTED, Message: "unsupported request"}
    }
}
```

Pass it to `ServeEnv` using `Plugin.Control = Config{PluginID: ..., BuildIdentity: ..., HandleRequest:
handle}`. Nil `Plugin.Memory.Purposes` uses the specified G/V/O/P/X/S set.
`Plugin.Started` receives the connected `Session` for
access to its memory, arrangement state, doorbell and reporting helpers.

`ServeEnv` serves one session and returns after Shutdown completes or its window
expires, a session fault, or control-stream EOF/error without Shutdown (including
a crashed or killed host). Let `main` return or exit at that point; do not wait
for another session. Cleanup stops the doorbell and closes mapped segments.
A doorbell-only fault keeps the control stream available for fault reporting
and the host's teardown decision.

`ServeEnv` owns `Attach`, `End`, `Handshake`, `Admit`, `Prepare`, `Release`,
`Arrange`, and `Connected` integration. It chains user `Attach`, `End`,
`Handshake`, and `Connected` callbacks; provide `Admit`/`Prepare`/`Release`/
`Arrange` only when using `NewServer` directly. `Handshake` is required by that
lower-level constructor: complete doorbell authentication there before Ready.

## Ownership and concurrency

**Defer `Close` on descriptor replies.** `ReadDataVolume` and `ReadMaskVoxels`
return a reply owner alongside their typed message. Copy their descriptor with
`session.Memory.CopyDescriptor(ref, dst)` before closing the reply. Closing is
idempotent and releases on failed copies too. The dispatcher handles replies
that arrive after cancellation, including cancellation racing delivery. The
receiving adapter decrements `SegmentRef.uses` on every disposition and emits
one `ReleaseDeposit` at zero; 0 and 1 both mean one use.

Export requests own their descriptor until the request handler returns. The
server then releases that use. Copy it before returning; never retain a borrowed
descriptor for background work. An inline export chunk with an invalid attached
`install` reports `install_without_segment`, discards the install, and delivers
the inline chunk to the handler without waiting for shared memory.
The built-in adapter supports **host-produced
Loaned** descriptors. An Owned application handoff needs a specified binding and
a custom adapter; it is not silently treated as a loan.

The dispatch loop handles routing and bounded SDK arrangement operations.
Application requests, notices, startup, shutdown and diagnostic callbacks run
on separate workers. Every descriptor into an uninstalled segment waits on bounded
workers, whether M, InstallSegment, or both are missing. This includes unknown
segment IDs and standalone export grants; embedded meaning is installed in stream
order. Descriptors dispatch in arrival order within each segment while unrelated
segments and arrangements continue. Failed or retired grants drop their descriptors
without an application read; `Memory.DroppedDescriptors()` counts these
dispositions. Host requests still receive a NOT_FOUND error naming the segment;
a waiting plugin caller receives a local HostError. Session cancellation stops waits; the host owns grant escalation.
Application callbacks must cooperate with cancellation. A callback that ignores
its context can outlive the RPC; Go cannot forcibly stop it.

Local defaults are 32 outstanding plugin requests/descriptor preparations and
256 entries per reliable output/notification/diagnostic queue. Outgoing Call
admission at the request limit returns ErrQueueFull locally. Exceeding the
incoming descriptor-preparation backlog faults the session explicitly: those
messages already own deposits, and without the installation there is no known
region for an honest ReleaseDeposit. Arrangements continue to be processed
while descriptors wait. Host requests run
serially; the host's protocol permits only one in flight. Oversized messages or
one-shot queue overflow terminate the session. A separate sender coalescer merges
**descriptor-free, order-independent** panel patches by (panel, widget, row),
replacing each property in place; overlay updates replace the whole value by overlay id; shortfalls retain
max(wanted) and sum(dropped). `Config.CoalescingCapacity` defaults to 4096 pending
keys, including updates frozen ahead of ordered messages. This local bound is
independent of the host's required manifest/grant-ledger bound. No key is evicted
for another, and a multi-widget Send is accepted atomically. Ordered messages
freeze their dependencies; the shutdown barrier flushes both queues. An image
freezes its own keys and the panel's pending rows, leaving unrelated cells
mergeable. With `Config.Panels`, a subset `set_rows` supersedes pending rows and
drops removed cells, a returning key retains its removal/recreation, and a new
list selection follows its rows. Mixed row patches, `set_options`, and raw row
patches without declarations keep ordered boundaries.
The row-drop rule is scoped by `Config.Panels` cell ownership: identical
row keys in different lists are independent. Descriptor-bearing cells retain
their accepted delivery order and every loan use.

Descriptor-bearing coalescing pushes retain every accepted descriptor use and
its order. The host owns receiver-side coalescing and returns those loans; the
sender cannot safely dispose of one use of a reservation shared with another
message. If admission is full, `Send` returns `ErrQueueFull` **without faulting the
session** for these pushes or for a new coalescing key. A failed Send accepts
nothing: the caller retains responsibility for its bytes and update, and must
handle the refusal. `Client.QueueStats()` reports cumulative merges and refusals
without flooding the diagnostic worker. Unknown protobuf fields also force
ordered delivery because their ownership and merge rules are unknown.

Allocation shortfalls additionally have a rate limiter: at most 1024 pending
arena keys, one report per 100 ms, maximum wanted size and summed drop count.
It retains refused reports for a later send. Full-arena allocation still fails
immediately.

`State.Pane` returns an independent interpretation snapshot. Capture it when an
interaction opens and keep it until the terminal/cancellation is processed;
the current pane state may have advanced by then. Time mappings retain previously
acknowledged revisions. Call `ForgetDataset` after its final pane switches away;
there is no invented dataset-close message or reset of a pane's epoch counter.
Retention is locally bounded to 1024 mappings and 64 MiB of time values; an
arrangement exceeding this is diagnosed and not acknowledged. Internal panes
share immutable mapping storage; public snapshots belong to their caller.
Application code still validates input generation, geometry and its own context,
and still follows the doorbell's accepted-terminal rules.

`ServeEnv` installs M, routes D into existing Pulsed consumers, observes transport
violations, and reads the terminal mailbox independently of application handlers.
Only that mailbox emits `DoorbellFault`; ordered terminal callbacks do not emit
a second report. The supervisor includes the SDK version and local health
snapshot: the plugin process is running, and stream health records whether its
session context is still live. These observations do not establish the health of
every component. Manifest source is absent because the SDK does not read it.
The transport accepts the terminal into its mailbox before
socket release; actual gRPC delivery is asynchronous and not awaited. A doorbell
fault ends that channel while control remains alive for the host's authoritative
shutdown. `DoorbellShutdown` also stops the channel before emitting one final
report containing the host reason, optional failure class and listener counters.
Repeat notices are diagnosed locally; the reliable session remains usable.
Open gestures receive the usual ordered `local_stop` cancellations without
blocking the control worker on application callbacks.
For actively consumed Pulsed regions use `ConsumePulsed`; starting a
session does not automatically subscribe to every region.

## Verification and regeneration

```sh
go test -race ./...
go vet ./...
bash regenerate-proto.sh
```

Tests use the generated host client over gRPC, real Unix endpoints and SCM_RIGHTS
grants, hostile ordering, deadline/cancellation races and explicit fake lifecycle
failures. They are not host interoperability certification.
Windows/macOS compilation is separate from native runtime validation.

`regenerate-proto.sh` pins both Go generators and uses the repository's pinned
protoc wrapper. Generated files are committed; plugin authors do not need protoc
to build. Keep the generated bindings current whenever either published proto
changes. Check them with `bash regenerate-proto.sh --check` from the SDK root;
CI runs it too.

## Arrangement, bound and shutdown rules

The sender must start each identity's revisions at 1 (§5.5). Go installs an
otherwise valid first ToolBinding or TimeMapping above 1, reports site 26
(arrangement_first_revision), and acknowledges the received revision. An exact
replay is acknowledged again without another first-revision report. Zero remains
refused; content conflicts report site 19 and regressions site 20.
Non-finite or descending TimeMapping values install and acknowledge with a local
diagnostic. Stored snapshots retain the exact index-wise values and replay content
checks still apply.
Repeated-field overflow is refused before application and reports site 21.
The three Roi and RoiPoseExtension pose arrays and TimeMapping.t permit
1,048,576 entries each; the
general repeated-field bound remains 65,536 and the whole-message cap 32 MiB.
Every bytes or string field, including map values, has an 8 MiB UTF-8 byte bound;
bound refusals return PROTOCOL before invoking application handlers.

Shutdown emits its typed outcome after accepted final output. An absent deadline
uses 5 s and reports site 25 (missing_deadline); an explicit zero gives no grace
and no violation. Generated `Shutdown.DeadlineMs` is `*uint32`: use
`proto.Uint32(ms)` for a present deadline, including zero. Pending violation
reports enter the output queue before successful shutdown completion, bounded
by the deadline. New requests receive PROTOCOL before ride-along installation;
repeated Shutdown does not extend the first window. Conflicting deposit uses
report site 24 (uses_disagreement) and retain the first received count, including
when completion order differs from receipt order.

A set_options patch preserves its complete operation order and freezes earlier
pending values. Later values can merge until the next options boundary; no
session-lifetime sensitivity is retained. A held-whole patch permits at most
256 ops per sensitive widget. Sender refusals return ErrQueueFull and increment
QueueStats.Refused; late requests increment RefusedInTeardown.
Reusing one reservation twice returns ErrDuplicateReservation before admission.
The caller retains its bytes on refusal; PublishLoan performs rollback for its
supported single-use publication.

To validate a known manifest, set `Plugin.Memory.Contributions` to its O/P IDs,
for example `map[string][]string{"O": {"selection"}, "P": {"settings"}}`. Nil
means unknown and trusts nonempty subjects; a nonnil empty map declares none. `Registry.FindContribution` selects by
purpose and subject, refusing ambiguity. `Session.PublishLiveOverlay` publishes
an inline OverlayUpdate into the selected O lane with a separate slot revision.
Use `plugin.Overlay.ReplaceLive` to share the overlay revision with reliable
Replace/Clear calls. A nonzero returned Sample means committed even if the
subsequent advisory notification fails.

`NewServer` owns violation delivery. Supply `Config.Reporter` to share a reporter
with custom arrangements/memory; do not start a second ReportViolations worker.
ServeEnv sets this up automatically. Deferred ContractViolation reports have
their own deadline: `violations.Reporter.Next` waits on a timer and the
session's report worker continuously drains it. A quiet connection does not
hold a repeat indefinitely.

## Retirement during publication

Loaned reservations count an in-flight use before checking retirement. The use
continues through **every declared message's successful gRPC Send**, or until
rollback/host release. Queue admission alone does not end it. Send completions
capture the original reservation identity, so a racing host release and reuse of
the same bytes cannot end a newer reservation. STAGED callbacks continue to pin
mappings until Finish/Abandon; synchronous Pulsed copies finish before unmap.

Descriptor arrival also claims the segment, including arrival before its two
grant halves have joined. An arrived deposit remains readable while retiring;
`Reply.Close` (or export-handler disposition) ends its claim after the last read.
The low-level `AcceptDeposit` handle ends its claim on successful Copy, or Close
when the caller abandons the read. A new arrival after retirement is quietly
counted and disposed. A pending, never-installed grant can still be retired.

A normal RetireSegment that meets a claim returns to dispatch immediately. The
adapter waits for the drain signal and settles the release without another host
message or a polling timer. Concurrent settlement passes cannot both release it.
Supersession acknowledges the new arena before the deferred old release.
Explicit terminal retirement/grant replays retain their protocol responses.

Typed overlay/chunk/live pushes select installed successors and quietly drop
work if none is available. `Memory.DroppedPublications()` counts these drops
without error diagnostics. Panel images instead retain the latest owned bytes
for their panel's next segment and flush when P installs (retrying on
PanelOpened). Unknown identities,
malformed payloads and arena pressure still return errors. Raw memory methods
retain ErrRetired for manual integration. At retirement the registry records
outstanding loans by region, mapping-relative offset and length. A late
ReleaseLoan consumes a matching key once and increments the debug counter
`Memory.CrossedReleases()` without a diagnostic. Unknown keys, duplicates and
never-installed identities report unknown_release and do not increment it.
`Registry.ApplyHostRelease` serializes settlement with retirement.

Deposit claims are acquired on control-stream arrival in `Memory.Admit`, before
reply/export worker dispatch, via `Registry.ClaimDeposit`. They hold the mapping
through copy/disposition, even if retirement starts meanwhile. Copy checks the
claim and holds the segment lifetime lock; a segment already released cannot
be read.

## Raw loans: reservation is not admission

Raw request handlers may register `AfterReply(ctx, work)` using their handler
context. The bounded continuation runs off the request worker after a successful
reply's gRPC Send completes. It is suppressed on failed replies, failed
admission/send, or prior session cancellation. The typed command spelling is
`plugin.Result.AfterReply`; see the plugin authoring README for lifecycle rules.

ServeEnv installs `Memory.AdmitOutbound` and `Memory.SentOutbound` as the paired
Config hooks; custom control servers using the SDK registry must supply both.
SentOutbound runs once, after successful transport Send, using the same admitted
message pointer. A failed transport abandons remaining claims at session Close. At the final queue
admission point it checks all SegmentRef/ImageRef reservations together and counts
their uses only if the whole message can be accepted. Pushes, calls and replies
share the same ledger. Uses 0/1 both mean one; mismatched declarations and excess
uses are local errors. A refusal consumes no new uses. A timed-out call that was
admitted still counts.

Local `Segment.ReleaseLoan` refuses an admitted reservation with
`memory.ErrLoanAdmitted`; the received host ReleaseLoan instead goes through
`Segment.ApplyHostRelease`. `Segment.LoanUseStats` exposes admitted namings,
pending declared uses and reservations stranded by an attempted local reclaim.
A stranded reservation keeps its host-owned bytes, but after its already-admitted
sends finish, the unadmitted uses no longer pin retirement. Retrying admission
while the segment is active acquires a new claim.

**WriteLoan reserves immediately. The caller owns cleanup until descriptor
admission.** PublishLoan performs that transaction for its supported one-use
pushes; a raw handler still owns rollback before admission. A partially admitted
multi-use reservation pins retirement until its remaining uses are sent, the
host releases it, or its unadmitted remainder is explicitly abandoned by calling
`Segment.ReleaseLoan`. That last call returns `ErrLoanAdmitted` and retains the
bytes for the host, but lets retirement proceed after admitted sends complete.

If building a descriptor fails, or Send returns an error such as ErrQueueFull
or ErrDuplicateReservation **and no other use of this reservation was admitted**,
retry admission or call `seg.ReleaseLoan(deposit)` locally, exactly once. No host
release will arrive for bytes it was never told about. That method sends nothing
on the wire. ErrRetired from this cleanup means retirement already settled it;
preserve other cleanup errors. For example, within a function returning error:

```go
seg, err := session.Memory.Registry.Segment(segmentID)
if err != nil { return err }
d, err := session.WriteLoan(segmentID, region, pngBytes)
if err != nil { return err }
// This reservation has never appeared in any other admitted message.
msg := &protocol.PluginMsg{Msg: &protocol.PluginMsg_PanelPatch{
    PanelPatch: &protocol.PanelPatch{PanelId: "results", Ops: []*protocol.PatchOp{{
        WidgetId: "preview", Op: &protocol.PatchOp_SetImage{SetImage: &protocol.ImageRef{
            SegmentId: d.SegmentID, Offset: d.Offset, Len: d.Length,
        }},
    }}},
}}
if err := session.Client.Send(msg); err != nil {
    cleanupErr := seg.ReleaseLoan(d) // local rollback, not a wire release
    if errors.Is(cleanupErr, memory.ErrRetired) { cleanupErr = nil }
    return errors.Join(err, cleanupErr)
}
return nil // admitted: retain bytes until host ReleaseLoan or retirement
```

**Once ANY use is admitted, never roll the reservation back locally.** If a
later send of a shared loan is refused, retry the unadmitted use with the same
reservation and declared uses count, or retain the reservation until retirement
or teardown. Decreasing uses after sending an earlier descriptor is unsafe.
One eventual host ReleaseLoan settles the entire reservation. A Client.Call
error or timeout is not proof of non-admission: the request may have reached the
host, so do not apply the Send rollback example to Call errors generally.

Reader callbacks return before the engine validates and enqueues their reply.
They must reclaim loans abandoned on their own error/build paths, but must not
`defer ReleaseLoan` on a successful descriptor return. The raw callback API has
no reply-admission result; after handing a descriptor to it, keep the loan until
host release or retirement/teardown. Prevalidate the reply before handing it off.
If the engine replaces an invalid reply with Error after return, that raw loan
can remain pinned until session teardown: automatic reply rollback is not
provided. Unpublished reservations must be admitted or explicitly rolled back;
ordinary retirement waits for that transaction to finish.

## Request admission and fresh-loan rollback

`Client.CallWithAdmission(ctx, message)` returns `(reply, admitted, err)`.
It has the same correlation, timeout and reply-ownership behavior as `Call`.
`admitted` says whether this request entered the outbound queue; it remains
true for a later timeout, host refusal or session failure, even if the host has
already released the named bytes. Only false proves that fresh reservations
which have never appeared in another message may be rolled back locally.
Never infer non-admission from an error or by inspecting the current loan ledger:
a released offset can already have been reused. The typed `plugin.LabelVolume`
operations handle this distinction and rollback automatically.

## Subscribed state and host calls

Config.ViewChanged and Config.RegionsChanged receive owned snapshots on separate
workers that may call the host. Pending views are replaced; pending region
changes merge by ID with the newer arrival winning regardless of revision,
preserving deletions and reset boundaries. They
fall back to OnMessage on those same independent workers. Shutdown cancels their
contexts. Declare subscriptions `view`/`regions` with `data_access`; Definition's
OnViewChanged/OnRegionsChanged register both automatically.

GetRois and GetRoisWindow accept optional region IDs. Generated replies preserve
region revisions/content hashes, selected region ID and time interval metadata.
RequestSavePath shares RequestAsset's lifecycle/timeout, works inside command
handlers with the same request-slot cost, and creates no file;
GetColormap checks the host's 1024-byte RGBA table. Neither needs permission.
Value.ints is exposed through protocol.IntList and plugin.Ints/Args.Ints.
For an owned inclusion mask with voxel-count validation and automatic reply
cleanup, use payload.ReadInclusion; raw ReadRoiInclusion retains explicit ownership.
The host keeps pre-open non-image panel state and applies it at open, options
before values. Re-sending on PanelOpened is optional. `PublishPanelImage` (used
by `plugin.Panel.SetPNG`) holds the latest owned image bytes until the P grant is
available, with a 16 MiB budget per panel. Installation flushes these even
without PanelOpened; PanelOpened also retries before the application handler.
`Config.Panels` supplies list ownership and max_rows for raw updates, and
`Definition.Configure` supplies this metadata automatically. Successful set_rows
admissions remove only that list's deleted held cells.
The SDK keeps only these pending image bytes, not the panel's application model.

## User-caused view requests

`Client.RequestView(ctx, *protocol.ViewRequest)` sends the request and
returns `*protocol.ViewRequestResult`. It rejects missing/dual causes, invalid
tool identities, a decline with targets, or a move with no targets before sending.
`ViewTargets.Request(cause)` constructs a move; pointer fields retain explicit
zero frames and empty selections. The reply must echo the supplied cause.

This is an ordinary correlated request, not a coalescing push. REFUSED outcomes,
including UNSUPPORTED, are returned without a Go error; inspect Outcome, Reason
and Detail. Errors retain the normal Call ownership/timeout semantics. The host
owns freshness, idempotence, permissions and all-or-nothing application. A timeout
means unknown, not permission to change the request under the same cause.

Raw handlers read InvokeCommand.Cause. The plugin adapter exposes CommandCause,
MoveView/DeclineView, and FIRE-only MoveToolView/DeclineToolView. Answer every
cause, preferably within 1 s (expiry 2 s); decline when there is no target. The
reply is the acknowledgement, never a later ViewChanged notification. Do not
block the doorbell listener waiting for it. Vizi applies all
targets for both command and FIRE tool causes. The switch is the first row of a
plugin panel: host-drawn, off every open, and effective only while open and
showing. It governs panel widgets and the plugin's tools. Its state is never on
the wire; NOT_ENABLED is the only indication. A tool declaring view_targets
requires a panel declaring view_targets.

## Follow

`OfferFollow` and `FollowState` are coalescing pushes, keyed by panel ID and
Follow ID respectively. A state replaces the entire previous state; omitted
fields do not inherit earlier targets. Empty offer labels withdraw the offer.
`EndFollow` is a one-shot push: earlier accepted states remain ordered before
it. All three use `Client.Send` with an empty envelope ID.

`FollowStarted` and `FollowEnded` are one-way transitions, regardless of envelope
ID. `Config.FollowTransition` receives them synchronously in stream order; keep
this SDK hook bounded and launch application work on a separate worker. Without
it, they go to `OnMessage`. The `plugin` adapter installs the hook and supplies
cancellable Follow drivers, target validation and sequence numbers. Its end
handling cannot be delayed by ordinary panel callbacks. Session loss cancels
workers without sending an end request or inventing a host reason.

The host applies states at up to 30 Hz, rejects invalid states by ending the
Follow with `INVALID` and a detail, and refuses conflicting view requests with
`FOLLOW_ACTIVE`. The host owns the person's master switch and camera easing;
there is no duration field or plugin-start operation.

## Long-running work (§8.16)

`ReportProgress(ctx, fraction, text)` and `Cancelled(ctx)` apply to every incoming
request handler. A nil fraction reports indeterminate progress. `StopWork(ctx,
message)` unwinds that handler into Error{CANCELLED}, preserving deferred cleanup;
returning ErrCancelled, including a `%w` wrapper, is also supported. Cancellation
is not diagnosed and does not free the request slot until the handler returns.
CancelWork runs on the receive loop. Requests are registered before queueing or
waiting for descriptor installation, so cancellation before a handler starts is
retained and prevents the callback from running.

`Client.StartTask(title[, cancellable])` returns a concurrency-safe TaskHandle
with Progress, Cancelled, Done and Finish. Default cancellable is true, the
session permits 64 open tasks, and ending one frees its place. A false cancellable
flag only advises the host's UI: CancelWork still sets the task's cancelled flag
without refusal, diagnostic or violation, and the task may ignore it. The caller owns
the background goroutine. Shutdown retires the task handles; before sealing a
graceful stream it sends their final outcomes when admission succeeds.

WorkProgress coalesces separately by request/task and id, with 68 reserved sender
keys in addition to Config.CoalescingCapacity. Titles and text are clipped to
256 UTF-8 bytes, including raw WorkProgress pushes. Late reports from a finished
request's context and progress after TaskHandle.Done are ignored.

Incoming handlers have no SDK deadline or slow-handler warning. RequestTimeout
is only the backstop on ordinary outgoing host calls; PickerTimeout separately
bounds the two picker calls. Shutdown's host-supplied deadline still applies;
`ShutdownReasonName(USER_STOPPED)` is `user_stopped`, handled like
`not_needed`. A deadline supplied on the entire gRPC stream remains authoritative.

## Region types shaped by the plugin

Declare `contributions.region_types` and the `contribute_rois` permission, then
register `Config.Shapers[typeID]`. A `ShapeHandler(ctx, client, edit, commit)`
returns `*protocol.ShapeResult` and an error. The same callback handles previews
(`commit=false`) and final shapes (`commit=true`). Each edit contains the whole
rest-form controls in voxel-index ZYX, including for copied regions whose
`contrib_id` is empty. Read `RoiRecord.TypeOwner` to identify the plugin shaping
its type; it need not match the region's origin.

The callback owns its input. Return one finite control `Vec3` per input vertex
in order for OK or CLAMPED, or no positions and a message for REJECTED. The SDK assigns
DragId and Seq, checks positions and message size, and converts errors, panics,
missing handlers and invalid results to REJECTED with an explanation. These two
output arms belong to the shaper dispatcher; `Client.Send` refuses manual shape
results so callers cannot bypass validation or send another answer to a commit.

Shape traffic takes no ordinary request slot. Each live preview has one running
callback and one replaceable pending edit, keyed by drag_id. A newer edit cancels
the running preview's context and replaces pending work; an obsolete result is
never admitted. Callbacks must honor cancellation for prompt updates. At most
four preview callbacks run; one that ignores cancellation cannot create unbounded
workers. A commit cancels preview work and runs independently, even if a preview
or command is stalled. Up to `MaxPendingRequests` commit callbacks can run;
further commits receive an immediate REJECTED capacity answer. Commits have no
SDK work deadline: the host waits indefinitely while supervised, or applies its
10-second unsupervised deadline. Shutdown cancels all shaper contexts.

On commit or `ShapeEditEnded`, the dispatcher stops proposing and removes any
still-coalescing proposal for that drag. The writer also drops obsolete proposals
from frozen ordered batches. Messages already in Send may be in transit and are silently dropped by the host when obsolete. Proposal keys have
four reserved sender slots, independent of ordinary keys and WorkProgress's 68.
A late drag after commit and an end for an unknown drag produce no diagnostic.
`Config.ShapeEditEnded` runs on the notification worker with the complete
COMMITTED, REVERTED or CANCELLED outcome and detail; it also receives unknown
ends. Coordinate shared application state across this hook and shapers.

Generic `protocol.Roi` geometry passes through unchanged, including
`AutoSurface_AUTO_SURFACE_STAR` and `InclusionMode_INCLUDE_INTERSECTED`. The SDK
does not triangulate or decide voxel membership. Build regions to §8.8's rules,
including rule 0: geometric rules use display indices and positions.
With a CompileOperator, an enclosing control must be exposed by a row whose only
nonzero is exactly 1 in that control's column; the first such row is its display
index. Controls and display positions coincide when no operator is present.
Closure ignores `(a, a)` sides. STAR uses the published direction tolerances and
fans a coplanar face from its lowest display index. INTERSECTED uses cells shifted
by epsilon = 1e-9, without deriving a surface. The host compiles shaper result
controls before applying these rules.
SHELL and POINTS contributions are refused by the host. Built-in type names and
`vizi.` names cannot be declared as plugin region types.
