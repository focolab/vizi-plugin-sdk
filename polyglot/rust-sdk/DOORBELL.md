# Doorbell transport API

The SDK exposes a low-level doorbell handle/listener API. This module
implements doorbell v2 framing for protocol **1.x** on Linux, macOS, and Windows.
Most plugins should use `control::Runtime`, which coordinates this transport
with gRPC, shared memory, input context, and shutdown.

## Ownership and startup

On Unix, `Doorbell::new(OwnedFd, Config)` adopts a connected socket and returns a
cloneable `Doorbell` plus one `Listener`. Setup failure closes the socket.
`unsafe Doorbell::from_env(Config)` instead adopts `VIZI_PLUGIN_DOORBELL_FD`;
the caller must ensure exclusive ownership of any open descriptor and adopt it
exactly once. Do not close, replace, or change its socket mode through an alias.
Adoption checks that the descriptor is open before constructing its Rust owner.
A closed descriptor or a number reused for a non-socket produces a launch error
with the original OS message and a launcher hint: exec the plugin or explicitly
pass the descriptor through to the child. Keeping `VIZI_PLUGIN_DOORBELL_FD` in
the environment alone does not pass the descriptor. Wrong socket types retain
their separate protocol diagnostic.

On Windows, `Doorbell::from_env(Config)` connects to the authenticated loopback
endpoint. It is safe because it creates its own socket. See [WINDOWS.md](WINDOWS.md).

Move the listener to a dedicated thread and call `Listener::run`. Start it before
publishing the gRPC endpoint. After the real Hello/Ready exchange succeeds, call
`handshake_completed(major, minor)`; Windows requires
`handshake_completed_with_token(major, minor, token)` instead. Major 1 is required;
minor differences are compatible. Sends remain gated until handshake completion.

The transport does not implement Hello/Ready itself. `control::Runtime` handles
the exchange and rejects premature records.

## Outcomes and capabilities

The listener delivers ordered `Outcome` values:

- `Frame` retains the received bytes, ordinal, diagnostics, and a disposition:
  decoded value, ignored record, semantic rejection, or framing rejection.
- `Cancellation` describes a local interaction closure and its reason; it is
  distinct from a received wire event.
- `Terminal` describes the final fault or teardown. First terminal wins.

On Unix, a decoded `Memory` record contains `Option<OwnedFd>`. Take the descriptor
inside the callback to transfer it into a memory manager. Unclaimed descriptors
close when the outcome is dropped, including during unwinding. Invalid or unknown
records close attached descriptors before callback delivery. Windows delivers a
validated section name in `NamedMemory` and no descriptor.

Decoding a grant proves only that its record is valid. The SDK's `memory::Memory`
checks object size/access, platform requirements, preamble and identity proof,
and joins `M` with `InstallSegment` before acknowledging installation. A custom
embedder must do the same. A zero-ID `M` has no grant-acknowledgement addressee;
report its contract violation with the original bytes.

## Input lifecycle

Apply lifecycle changes before acknowledging their control-plane arrangements:

1. `tool_activated(handle)` admits a new, increasing handle. Retired handles
   cannot be reactivated. A failed activation must not be acknowledged.
2. `epoch_acknowledged(view_id, epoch)` installs a pane epoch and closes its old
   gestures. Equal epochs are idempotent; regressions fail. Pane zero is valid.
3. `tool_retired`, `cancel_interaction`, and `cancel_interactions` close the
   corresponding state. Their effects are immediate; callbacks arrive later in
   the listener's ordered outcome stream.

An accepted `PRESS` carries `implicitly_closed` for a predecessor it replaced;
there is no extra cancellation for that predecessor. `FIRE` closes no gesture.
`InputEvent.action` is the little-endian u16 at E payload bytes `[42..44)`:
zero names the primary gesture, and an action FIRE carries a one-based action
index. Action FIREs can arrive during a drag or with no gesture open. Unknown
indices remain ordinary decoded inputs. Only `[92..100)` is fixed E padding;
the 48-byte position union's unused tail is still checked separately.
`RAY_AND_PICKED` uses the whole union: ray, picked voxel, then `back_frac`
at payload `[88..92)`. That fraction is in `[0, 1]`, measured from the picked front
(`back = front + back_frac * (far - front)`, `0.0` a pick of no depth), or NaN
when nothing stood out. A nonzero byte in that range on a shorter kind
is `position_tail`, not a framing fault. Both ray kinds are accepted in every
phase; `MISS` with either picked kind remains `miss_with_picked`.
If an application rejects an accepted opening, cancel that interaction. An
accepted terminal must still complete even when its coordinates are unusable.
The full runtime supplies binding, geometry, and time context separately.

Callbacks run outside transport locks and may call lifecycle/send/stop methods.
Keep them short. The low-level listener runs the callback directly; a blocked
callback delays receiving. The full runtime transfers application callbacks to
its bounded event worker.

## Notifications and terminal reports

A `Notification` names a segment and `RegionMask`, optionally with a `PaneEpoch`.
Masks use LSB-first bit indices in `0..512`. Coalesce only for the same segment
and pane association. `None` differs from pane zero.

`notify` makes one nonblocking record send. It suppresses empty masks, never
queues a refused notification, and counts would-block/backpressure drops. `Sent`
means socket acceptance, not delivery. Refused records consume an ID; interrupted
Unix sends retry with the same ID. Notification IDs wrap through zero.

Hints do not transfer Owned access. A receiver must validate region disciplines
and poll actively consumed Pulsed lanes independently of hints. The SDK memory
layer and runtime implement these checks and periodic polling.

`terminal_report()` and `wait_terminal(timeout)` expose a dedicated mailbox,
independent of callback delivery. Its one report is published before releasing
the socket. A wait timeout means no report arrived; it is not a peer-liveness
verdict. The supervisor translates terminal reports into gRPC diagnostics.

`stop()` closes admission, publishes local teardown, releases the socket, and
wakes the listener without joining. A blocked callback cannot block stop or the
terminal mailbox. Joining remains bounded by callback completion. A panicking
callback stops the transport and returns `RunError::ConsumerPanicked`.

## Platform details and limits

The default active-tool limit is 1024 and is configurable. Retired handles and
completed interactions do not accumulate. Pane epoch storage has 256 slots.
Counters saturate. Receive framing is exactly 112 bytes, with CRC and fixed
padding checks; original malformed bytes are retained within the capture limit.

Linux uses `SOCK_SEQPACKET`, `MSG_CMSG_CLOEXEC`, and `MSG_NOSIGNAL`. macOS uses
`SOCK_DGRAM`, `SO_NOSIGPIPE`, and a receive buffer of at least 64 KiB. Both validate
socket type and close all unexpected rights. macOS sets received descriptors
close-on-exec after receipt, so applications must coordinate concurrent child
creation. Its datagram peer-close signals do not replace authoritative gRPC
teardown. Windows assembles exact record windows from TCP and retains partial
EOF bytes as teardown evidence.

See [README.md](README.md) for build commands and the complete SDK API.
