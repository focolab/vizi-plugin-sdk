# Go plugin SDK — protocol 1.0

The Go SDK for vizi plugins: a plugin-side implementation of **doorbell v2 and
platform protocol 1.0**, derived from the vizi specifications. Protocol 1.0
hosts are vizi v1.0.0 and later. Stable 1.x minor versions are compatible; 0.x
and other majors are rejected.

**The wire protocol and the Python SDK carry the 1.0 stability promise; this Go
API may still change in a 1.x minor release.** The protocol it speaks is 1.0,
which does not change: later wire additions bump
the minor, and a plugin built against 1.0 keeps working on later 1.x hosts. A
plugin that uses a newer feature checks the session first with
[`plugin.Supports`](plugin/README.md#the-hosts-protocol-version).

The Go SDK's version is the SDK release's, `control.SDKVersion` (currently
`1.0.0`). The module lives in a subdirectory, so its tag for SDK release
`1.0.0` is `polyglot/go-sdk/v1.0.0`, and likewise for each later release.

It requires Go 1.25 or newer and runs on Linux (`SOCK_SEQPACKET`), macOS
(`SOCK_DGRAM` with `SCM_RIGHTS`) and Windows (loopback TCP with named
shared-memory sections). The module is
`github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk`.

## Packages

| Package | What it does |
|---|---|
| [`plugin`](plugin/README.md) | Typed authoring. A declarative `Definition` registers contributions and handlers together, generates a validated manifest, and runs `--manifest` or `--install` from the plugin executable. Also typed command, reader/exporter and panel handlers, panel updates, overlays, chunk sources and processing stages. |
| [`control`](control/README.md) | The reliable control plane: connects the doorbell and memory layers to a generated gRPC session, with worker dispatch, host calls, arrangements, resource release and shutdown. |
| [`protocol`](protocol) | Bindings generated directly from both published `.proto` files. |
| [`memory`](memory/README.md) | Grant installation and replay, validated shared mappings, copied Constant/Pulsed/Loaned/Owned reads, unconditional Pulsed consumer polling, Pulsed publishing, Loaned allocation and segment retirement. |
| [`doorbell`](#the-doorbell-package) | The doorbell transport and record codecs, documented below for custom integrations. |
| [`violations`](violations/README.md) | Session-wide aggregation, priority for novel violation categories, and rate-limited delivery of typed reports. |
| [`payload`](payload) | Checked array codecs and owned host-data copies. |
| [`geometry`](geometry) | Crop/level conversions, per-axis measurement flags for ViewState spacing, and explicit unknown spacing/time. |
| [`manifest`](manifest) | Describes and validates generated `vizi-plugin.json` files. |
| [`stage`](stage) | Processing-stage planning and slot-layout primitives. |

Start with the [`plugin` authoring guide](plugin/README.md). The
[inspect example](examples/inspect/main.go) is a complete plugin using the
declarative API, and the [clamp example](examples/stage-clamp/main.go) is a
[processing stage](plugin/README.md#processing-stages). Stages support declared
parameters per position, planning, clean preflight refusal, and borrowed
in-place processing on shared S rings.

`control.ServeEnv` supplies the supervisory wiring between these packages: it
installs grants, aggregates doorbell and arrangement violations into one
session reporter, and delivers reports reliably. Application memory operations
can submit errors to the same reporter.

## Things to know

**Build identity.** The handshake carries build identities. Go composes its own
from embedded build metadata by default and validates Hello's identity.
`Config.BuildIdentity` is an optional label; dirty and unavailable provenance
remain explicit. See [the identity API](control/README.md#build-identity).

**Calibrated spacing.** `geometry.PhysicalViewSpacingAxes` returns ZYX spacing
and a `[3]bool` measurement mask. Check `axisKnown[i]` for every axis you use
before using `spacing[i]`: one known axis says nothing about the rest. A true
flag means a positive, finite measurement. A false flag means no measurement:
NaN in a partly recorded file, or zero when no axes are known, including on
error. NaN or zero occurs exactly where the flag is false.
`PhysicalViewSpacing` reports `known` when **any** axis is measured, not all;
it does not permit using every spacing value. Tools continue to use finite
`Geometry` spacing; a partly recorded file is display fallback there.

**Host calls.** Progress indicators, epoch subscriptions for plugins with no
tools, request-status queries, ROI pose extensions and windows, and
host-computed ROI inclusion deposits are described in the
[host-call API](control/README.md#surface) and the
[declarative API](plugin/README.md#declare-the-interface-once).

**Contribution subjects.** O/P subjects are trusted when manifest declarations
are unavailable. Supply `memory.Config.Contributions` to validate subjects
against a known manifest; a nonnil empty map declares none. `Definition`
derives this membership from its panels and overlays. Raw reader loan admission
remains explicit. The [reader/exporter contract](../../docs/spec/reader_writer_contract.md)
defines G/X buffer exchanges.

**Channel names.** Channel names include their source on both tool
`Geometry.Channels` and panel `GetViewState().Channels`. `protocol.ChannelName`
/ `ChannelInfo` exposes `Declared()`: FILE and USER names may be written to
artifacts; a PLACEHOLDER may label a trace only. Readers declare names by index
in `ReaderOpened.ChannelNames`, optionally stopping short of C. Export
callbacks receive `SourceMetadata` (the source document, for provenance only),
artifact `Channels` with source indices, and raw JSON `Provenance`. See the
[reader/exporter guide](plugin/README.md#reader-and-exporter-contract) for the
full contract.

**Tables and image sizes.** `plugin.Table` declares a `table` widget of typed
columns; `Panel.SetTable` and `Panel.PatchTable` send its rows, split under
1 MiB and 32,768 rows (or deleted keys) per message, and the sender's outbox
merges table ops by §8.4's rule. A selectable table's command reads
`args.Strings("rows")`; `plugin.Strings()` with no keys clears its selection.
`Definition.OnWidgetGeometry` hears how large each image widget is drawn, and
`Definition.OnAppearance` the host's theme and colors. See
[Tables](plugin/README.md#tables), [Image sizes](plugin/README.md#image-sizes)
and [Theme](plugin/README.md#theme).

**Long work.** Handlers report through `control.ReportProgress(ctx, fraction,
text)`, check `control.Cancelled(ctx)` between steps, and stop with
`control.StopWork(ctx, message)` or return `control.ErrCancelled`. The SDK
imposes no incoming handler deadline. Work that should release the request slot
uses `Client.StartTask` and an application-owned worker. Task handles report
progress, notice cancellation and end with Done/Finish; at most 64 are open per
session. See the
[long-running work guide](plugin/README.md#long-running-requests-and-background-tasks).

## The doorbell package

The rest of this README documents the lower-level
`github.com/focolab/vizi-plugin-sdk/polyglot/go-sdk/doorbell` package for
custom integrations. It provides:

- Fixed 112-byte records, little-endian fields, IEEE CRC32, typed `M`, `E`
  and `D` codecs, fixed-padding validation, and record-local semantic checks.
- E's position union is 48 bytes, with fixed padding at payload `[92..100)`.
  `Position.BackFraction`, `IsPoint`, `Front` and `Back` expose a `RayAndPicked`
  pick's extent, measured from the picked front; zero is a pick of no depth and
  NaN means nothing stood out, with no back. Pick presence is independent of
  event phase. See the [input API](plugin/README.md#coordinates-and-time).
- Nonblocking, serialized `D` sends; refusal drops immediately and burns its
  record ID. No retry queue, acknowledgement layer, or refusal escalation.
- Descriptor passing and ownership, oversize detection, platform-specific flags,
  socket validation, close-on-exec, and peer-close/error classification.
- Per-frame dispositions and diagnostics, aggregate counters, exact raw records,
  structured faults with explicit absence, and exactly one terminal outcome.
- Input sequencing, replay checks, tool and epoch lifecycle inputs, and ordered
  cancellation delivery. Socket code, codec, and input state are separate files.
- A wake socket, bounded lifecycle queue, prompt `Stop`, and separate `Wait`.
- Windows token startup and Hello echo validation, nonblocking Winsock sends,
  bounded TCP record assembly, partial-EOF capture, and section-name validation.

### Integration contract

`FromEnv(Config)` is the common startup entry point:

| Platform | Launch input | Explicit constructor |
|---|---|---|
| Linux / macOS | `VIZI_PLUGIN_DOORBELL_FD` | `Adopt(fd, Config)` |
| Windows | `VIZI_PLUGIN_DOORBELL_ADDR`, `VIZI_PLUGIN_TOKEN` | `Dial(ctx, address, token, Config)` |

Unix adoption **takes ownership even if setup fails**. Do not keep fd aliases,
wrap it in another reader, or alter its blocking flags. On Windows, the address
must be `127.0.0.1:<port>` and the token exactly 32 ASCII hex characters. Setup
writes the token before records, enables `TCP_NODELAY` and nonblocking mode, and
starts the listener. `Config.SetupTimeout` bounds the entire Windows startup
(default 5 seconds); `Dial`'s context can shorten it but does not own the returned
session. Setup failures close all acquired sockets and produce no transport event.
The embedding executable should print the launch diagnostic and exit.

The listener starts before the constructor returns. Start it before publishing gRPC
readiness. After validating the control-plane handshake's compatible major version,
call `HandshakeComplete(1, 0)` on Unix or
`HandshakeComplete(1, 0, helloSessionToken)` on Windows before sending `Ready`.
The Windows echo comes from gRPC Hello, not the socket; a missing/mismatched echo
terminates the transport without a DoorbellFault. Writing the launch token alone
does not authenticate the session. `Notify` rejects attempts before this gate.
The library does not negotiate a version, bind gRPC, or enforce the host's
pre-handshake sending discipline. A version mismatch leaves sends gated; the
supervisor must reject the handshake and stop the session.

On Windows the supervisor must bind gRPC to `127.0.0.1:0` and atomically publish
the selected port to `VIZI_PLUGIN_PORT_FILE` **after doorbell listener startup**,
following the platform launch specification. Those control-plane tasks are not
implemented by this socket library.

`Config.Handler` receives an `Event` with exactly one of these fields:

| Field | Consumer obligation |
|---|---|
| `Frame` | Inspect `Disposition` first. Act on `Decoded` records; translate semantic `Violation` into its indicated reporting route. All diagnostics remain available. |
| `Cancellation` | Finish/discard that interaction using the captured opening context; this is a local event, not a fabricated wire `CANCEL`. |
| `Terminal` | Ordered session completion, after all remaining gesture cancellations. |

For received frames, `HasRecord` distinguishes a complete raw record from a short
or oversized datagram; `HasHeader` distinguishes CRC-validated fields from absence.
The fault retains the actual received capture, up to 120 bytes, including the
surplus of an oversized datagram. Windows buffers at most one 112-byte record,
never interprets a partial record, and does not consume the next record before
processing the current one. A partial EOF is peer teardown, not a framing fault;
`Terminal.Capture` preserves buffered bytes on teardown/error/local stop. It is
separate from `Fault.Capture`, whose presence follows the fault-reporting rules.
CRC values are present only when at least 112
bytes were received. No header-ID gap or regression suppresses a record.

The **separate `Terminal()` mailbox must have an independent supervisor reader**.
It accepts one terminal outcome before socket release even when the event handler
is blocked. Report its `Fault` over the still-usable gRPC control plane. The later
terminal callback is the ordered view of this same event; do not send two reports.
Mailbox acceptance is a constant-size, nonblocking operation owned by the library;
there is no user fault callback whose execution needs a timeout. The transport
does not wait for actual gRPC delivery. Values delivered to callbacks and the
mailbox are read-only; the terminal's fault detail is shared between both views.

Handlers run serially without transport locks held and may call lifecycle methods,
`Notify`, or `Stop`. They **should return within 1 ms** (local SDK guidance, not a
wire deadline). A slow handler delays every following record, including grant
installation. Do not introduce an unbounded worker queue. Panics release unclaimed
descriptors and publish a `handler_panic` local teardown; a panic does not invent a
peer framing fault. Handlers must not call `runtime.Goexit`: it ends the listener;
the supervisor mailbox still receives `listener_exited`, but that goroutine can
no longer deliver ordered callbacks.

`Stop()` latches local teardown, releases the doorbell and wakes the listener;
it does not join callbacks. `Wait(ctx)` joins and is bounded by the context and
consumer cooperation. A grant descriptor already inside a handler remains
claimable until the handler returns, even if another goroutine stops the transport;
forcibly closing it would risk closing an fd already claimed and reused. A handler
that never returns can consequently retain that capability indefinitely.

### Memory and notification hooks

A decoded `M` exposes its fields, exact raw record, and header ID. On Unix it also
exposes a claimable `Descriptor`. Inside the Unix handler:

```go
file, err := frame.Descriptor.Claim()
if err != nil {
    // Local consumer error; report it to your supervisor.
    return
}
// file now belongs to the memory installer, including every failure path.
// Map, validate the underlying object and preamble, then close the file.
```

An unclaimed descriptor is closed when the handler returns, including a panic.
Calling `Claim` twice or after the handler returned fails. Rejected grants expose
no descriptor: cleanup has already happened. Unix name bytes are ignored with a
diagnostic as required by `M` §7, including nonsensical length/UTF-8/tail bytes.

On Windows `frame.Descriptor` is nil and `frame.Memory.Name` holds the validated
UTF-8 section name. Open the **existing** named mapping; never create a replacement
on lookup failure. Mapping access, size and identity/preamble checks belong to
the memory installer. Names must contain 1–80 UTF-8 bytes, no embedded NUL, and
the required terminator and zero tail. Malformed names are grant rejections, not
framing faults. The receiver does not impose the host's section-naming convention.
`EncodeMemory` writes a Windows name when `Memory.Name` is nonempty, or canonical
Unix zero name bytes when empty; a Windows receiver rejects an empty name.

The memory layer must join `M` and `InstallSegment` in either arrival order, check
the object size/access and immutable preamble, compare duplicate grants byte for
byte, enforce installation budgets, and acknowledge **mapped AND validated AND
meaning applied**. The doorbell does none of those things. It rejects record-local
invalid grants with a `Violation.Route` of `grant_ack` / class `malformed`, except
`segment_id == 0`, which uses `contract_violation` / `zero_id`.

`Notification` carries a segment, a 512-bit LSB-first mask, and optional pane epoch.
Pane 0 is valid; `Epoch == 0` means no pane association. `Mask.Set`, `Has`, and
`Merge` avoid manual bit ordering. `Notify` suppresses an empty mask before writer
admission. `Sent` means only kernel acceptance, never delivery or installation.

Every consumer acting on `D` owes all five advisory conditions: an independent
unconditional check of the same memory within a documented finite bound; that
schedule never gated on `D`; hints used only to schedule work, never infer state;
receiver-owned rate limiting; and a memory discipline safe to recheck. Region
existence, bounds, disciplines and epoch association are the memory layer's checks.
The memory package counts out-of-range bits and sweeps host-produced Pulsed
regions only, preserving the other bits' normal dispositions. Its `ConsumePulsed`
API supplies an unconditional timer per actively consumed region; `Hint` can
accelerate those checks without postponing them. The doorbell package itself
owns no mappings or region schedule.

### Input and coordination hooks

| API | When to call / result |
|---|---|
| `ToolActivated(handle)` | Before activation acknowledgement. Handles must arrive in increasing order; gaps are accepted. Repeating a live handle is a no-op. |
| `ToolRetired(handle)` | On deactivation. Cancels its gesture. Repeating retirement, including an inactive gap below the high-water mark, succeeds. |
| `EpochAcknowledged(view, epoch)` | After the consumer has arranged the state, before sending its epoch acknowledgement. A newer epoch cancels that pane's gestures; equal is a no-op; older and zero fail. |
| `CancelInteraction(id)` | Retract an opening rejected by the supervisor. Unknown/already-closed IDs are no-ops. |
| `CancelInteractions(view)` | Cancel all gestures in a closing pane. Retains its epoch; the host must never restart the counter when reusing the pane ID. |

Changes take effect synchronously under the same serialization as input processing.
Their cancellation outcomes are appended under that serialization and wake the
listener, which delivers them in order. No lifecycle outcome is dispatched on the
gRPC calling goroutine. Every method returns an error after terminal state; failed
calls must not be acknowledged to the host.

The consumer still validates dataset generation, pane existence, time/level ranges,
and geometry. If it rejects an **opening**, call `CancelInteraction`. A disposable
drag may simply be dropped. **Do not reject an accepted terminal**: preserve its
closure, mark unusable context/position at the higher layer, and report
`unusable_context`. The transport has already removed its interaction. Preserve
old geometry until queued cancellations referring to that epoch have been handled.

An accepted `PRESS` names the preceding gesture in `Frame.ImplicitlyClosed` and
does not also emit a cancellation. Rejected presses can emit a cancellation, but
never open a new gesture or advance the opening watermark. `FIRE` advances the
watermark for deduplication without entering the open-interaction index.

Local bounds are explicit: default `MaxActiveTools = 1024`,
`MaxPendingOutcomes = 1024`. A lifecycle call that needs additional cancellation
space fails transactionally with `ErrQueueFull`; no-op calls need no space. A
received frame and terminal cleanup have a bounded reserve beyond this lifecycle
limit (at most `MaxActiveTools + 3` additional outcomes), so saturation cannot
prevent shutdown. There is at most one received grant awaiting its handler.
An activation exceeding the active-tool bound returns `ErrToolLimit` without
advancing the tool high-water mark.

## Verification

From this directory:

```sh
go test -race ./...
go vet ./...
go test ./doorbell -run '^$' -fuzz FuzzFrameDecode -fuzztime 10s
go test ./doorbell -run '^$' -fuzz FuzzInputState -fuzztime 10s
go test ./doorbell -run '^$' -fuzz FuzzStreamChunks -fuzztime 10s -parallel 2
go test ./doorbell -run '^$' -fuzz FuzzWindowsNames -fuzztime 10s -parallel 2
```

Tests include hostile frames and ancillary data, fd cleanup, lifecycle ordering,
fault/send races, actual queue saturation, and an inherited-fd subprocess that
receives and maps an unlinked shared file. All socket tests select the platform's
real transport type. They require permission to use local sockets. Windows-native
tests cover token bytes and handshake gating, fragmented/coalesced TCP records,
partial EOF, named grants, nonblocking mode, lifecycle wakeups, blocked-handler
shutdown, and injected Winsock error/short-write classifications. Stream assembly
and Windows name-validation tests/fuzzers run on Linux too.

Linux tests and Darwin/Windows cross-compilation can be run on Linux;
**cross-compilation is not runtime validation**. Run the same suite on macOS to
exercise its `MSG_CTRUNC`, close notification, and buffer semantics, and on Windows
to validate Winsock behavior. The CI workflow runs on all three operating systems.

These tests are specification-derived checks and local peer probes, not host
interoperability certification.
