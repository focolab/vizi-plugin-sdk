# Doorbell transport protocol (v2)

Framing, delivery and faults for the plugin platform's fixed-size fast path.
Record contents are specified per opcode elsewhere:

| op | byte | purpose | legal direction | document |
|---|---|---|---|---|
| `M` | `0x4D` | grant shared memory | host→plugin only | `doorbell_M_memory.md` |
| `E` | `0x45` | tool input events | host→plugin only | `doorbell_E_input.md` |
| `D` | `0x44` | advisory notification | **both** | `doorbell_D_notification.md` |
| — | `0xFF` | reserved recovery marker; never emitted | — | §5 |

The bytes are normative. They are the ASCII letters, which is why they read as
letters throughout — but an implementation should not have to infer an encoding
from a document's typography.

**Every grant is an `M`.** Reader, panel, export, overlay and data grants are
all an `M` distinguished by `InstallSegment`'s `purpose`, which keeps meaning
off the record. **`PanelOpened` has no opcode**: it carries a protobuf payload
and needs neither a descriptor nor interactive latency, so by
`doorbell_principles.md` A1 it is a gRPC message.

---

## 1. Role

The platform has three channels — the control plane, the doorbell and shared
memory. The doorbell's share of that split is two things:

- **Capabilities cross it.** `SCM_RIGHTS` cannot travel over the control plane.
- **It is the interactive path**, at ~11 µs.

Three rules follow:

1. **Arrangement may use the control plane; use may not.** A capability is
   arranged over both channels — the grant here, its meaning there — and is not
   usable until both have arrived. That is one-way, host→plugin, and paid once.
   Once arranged, nothing stands between an input event and the response to it but
   the doorbell and shared memory. Batching does not soften this; a drag event
   batched is a drag event delayed.

   A plugin-initiated request is a genuine round trip, so **nothing may wait on
   one between a sample and the response to it**. A once-per-gesture request the
   plugin chooses to make, that no host action waits on and whose result is
   applied retroactively, is permitted — `doorbell_principles.md` A2 states the
   rule and `doorbell_E_input.md` §3 is the case. A ring
   arranged once and drained per slot is the shape that belongs on a per-sample
   path.
2. **The doorbell has no acknowledgement layer** — no ack flag, no ACK op, no
   in-flight set, no transport-level retry budget. Capability grants are
   acknowledged over gRPC, where an ack can mean *installed* rather than *sent*.
   An op needing reliability obtains it from idempotent content plus the
   unambiguous refusal signal in §6.
3. **Nothing is delivered on two channels.** No record has a duplicate gRPC copy.

Same-user interception is out of scope: the Windows doorbell is loopback TCP
reachable by any local process, gated by a 128-bit token, and a host running
hostile same-user processes has larger problems.

---

## 2. Bidirectional and symmetric

One record format, one framing, one send discipline, one receive discipline, both
directions. A receiver validates identically regardless of which end it is.

Exactly two things differ by direction, and an op specification must state both:

- **Trust.** vizi controls its own sends, so host→plugin cannot be corrupted by
  the peer. Plugin→host carries whatever a bad implementation emits.
- **Rate-limit ownership.** Each receiver owns its coalescing and quota windows.
  Neither is a protocol constant.

Symmetry is about *validation*, not about who may say what. **Only vizi sends
`M` and `E`** — one grants memory and the other reports input on vizi's own
surfaces, so both are structurally one-directional forever. `D` is symmetric:
either end may hint at memory it writes.

**A defined op arriving in an illegal direction is a session fault**, not an
unknown op: the peer is misimplemented, which is exactly what §5's fail-stop
posture exists to expose. Any attached descriptor is closed first (§7.4).
Because these directions can never open, faulting costs no forward
compatibility — unlike an unknown *op*, which must not fault (§5).

---

## 3. Transports

| Platform | Doorbell | Send atomicity |
|---|---|---|
| Linux | `AF_UNIX SOCK_SEQPACKET`, `socketpair`, inherited fd | kernel contract *(measured)* |
| macOS | `AF_UNIX SOCK_DGRAM`, `socketpair`, inherited fd | kernel contract *(measured)* |
| Windows | loopback TCP, ephemeral port, token-authenticated | none guaranteed *(measured: no short returns — see below)* |

**Winsock takes a send entire or refuses it entire** *(measured)*. Against a
reader holding the buffer full, a write that lands at the buffer boundary is
accepted whole or refused, never returned short, and whole 64 KiB writes are
accepted into a socket with `SO_SNDBUF = 4096`. Winsock is not bounding
acceptance by remaining buffer space the way unix does.

Rule 2's `n == 112` assert **stays** regardless. It costs nothing, and it is the
detection for this platform assumption being wrong.

Atomicity on unix is a property of the kernel socket object. A plugin that
re-declares its inherited fd as `SOCK_STREAM` still gets message semantics —
`SO_TYPE` remains what the host chose *(measured)* — so the peer cannot forfeit
it. Windows carries no such guarantee: Winsock documents `send` as permitted to
return a short count, which §6 rule 7 bounds.

**The platform determines the socket type, and each platform has exactly one.**
The table above is the rule, not an observation: `SOCK_SEQPACKET` on Linux,
`SOCK_DGRAM` on macOS, a loopback TCP stream on Windows. Everything downstream —
framing, atomicity, the fd-count table, the 0-byte-read meaning — is keyed on
that, and an implementation may use its platform's type directly.

**`SO_TYPE` is checked once at setup, and a mismatch is a launch failure**
(§7 rule 3, §3.1). It verifies the mapping rather than replacing it: a socket
that is not its platform's type means the launch went wrong, and that is better
caught at connect than at the first record.

**This does not strand a deployed plugin if vizi ever changes a type**, which is
the objection a per-platform constant invites. A socket-type change is a change
to the doorbell format, which is a protocol version bump (§3.1a) — so the
handshake rejects the older plugin first, with a diagnostic naming both versions,
and its `SO_TYPE` check is never reached.

### 3.1 Launch environment

The host passes the doorbell to the plugin through the environment; an SDK reads
these and nothing else.

| Variable | Platform | Meaning |
|---|---|---|
| `VIZI_PLUGIN_UDS` | unix | path the plugin binds its gRPC server on |
| `VIZI_PLUGIN_DOORBELL_FD` | unix | inherited doorbell fd, already connected |
| `VIZI_PLUGIN_DOORBELL_ADDR` | Windows | `host:port` of the host's doorbell listener |
| `VIZI_PLUGIN_TOKEN` | Windows | 32 ASCII hex characters, 128 bits |
| `VIZI_PLUGIN_PORT_FILE` | Windows | where the plugin publishes its gRPC port |

On unix the inherited fd is already connected and is adopted **with its own
family and type** — `socket(fileno=fd)`, never a hardcoded `SOCK_SEQPACKET`,
which misdeclares it on macOS.

**Clear its inheritable flag immediately after adopting it** (§7.6). It arrives
inheritable by construction — it had to survive the host's `exec` — and adopting
it does not change that *(measured)*.

**A setup failure is a launch failure, not a `DoorbellFault`.** Before the
doorbell opens there is no channel to fault and no session to tear down: a
missing `VIZI_PLUGIN_DOORBELL_FD` or `VIZI_PLUGIN_DOORBELL_ADDR`, a socket whose
`SO_TYPE` is not its platform's (§7 rule 3), or a token mismatch all mean the
plugin cannot run, and it exits with a diagnostic on stderr — which the host
captures — rather than attempting to report on a channel it does not have.
**Running without a doorbell is not a legal configuration**; every plugin is
launched with one.

### 3.1a Which doorbell version is in play

The record's `ver` byte says what a frame *is*; it does not negotiate anything.
**Negotiation is `Hello`/`Ready`'s alone**: two doorbell formats are completely
framing-incompatible — a receiver of one reading a record of the other
desynchronises silently — so a mismatch cannot be discovered at parse time.

- **The doorbell format is not negotiated separately.** It is a property of the
  platform protocol version; one `protocol_major.protocol_minor` implies exactly
  one doorbell format, and **the format changes only on a MAJOR bump** — a
  plugin whose major differs is rejected at handshake
  (`control_plane_protocol.md` §3.1). It follows that a minor bump may not
  change the framing, which is the constraint this rule places on every future
  version rather than a fact about this one.
- **No record may be sent before the handshake completes**, in either direction.
  That is what makes §5's unknown-opcode rule safe: it assumes the contract is
  already settled.
- **That is a rule for senders, and a receiver must not read it as one for
  itself.** A receiver admits records from the moment its listener starts, and
  the local "the handshake settled" notification an SDK exposes gates its
  *sends* alone. Two reasons, and either is sufficient. The handshake completes
  on the host's side first, so the host may legitimately send before the plugin
  has been told the handshake finished — the plugin learns that over gRPC, after
  the fact, and a receiver holding those records back is refusing correct
  traffic. And §3.2 step 7 requires the plugin to be *draining* before it
  signals readiness, which is earlier still: session-setup grants follow the
  readiness signal immediately and macOS's stock queue is 32 records (§11), so a
  receiver that waits for its own notification loses exactly the grants that
  rule exists to protect.
- **And no `E` record for a pane before that pane's first epoch is acknowledged
  by the plugin receiving it** (`doorbell_E_input.md` §1a). The handshake settles
  the *format*; the epoch settles how a coordinate in that format is to be read,
  and there is no default for the second.
- **On Windows, no record before the token is validated** — and in particular no
  `M`, since a capability handed to an unauthenticated connection is a capability
  handed to whoever connected.

**Doorbell v2 is protocol `1.0`.** No version exists in which two formats are
legal, because one version implies one format.

**A framing change is always a MAJOR bump.** `control_plane_protocol.md` §3.2
makes a minor bump backward-compatible, and a framing change cannot be: no
amount of care makes a receiver of one format survive a record of the other.

**A transport property that differs between two formats is therefore a major
bump**, not a runtime discovery — the socket type in §3's table included.

### 3.2 The Windows connect handshake

1. **The host emits `127.0.0.1:<port>`**; parse that and connect. Splitting on
   the *last* colon rather than the first costs nothing and leaves room for an
   IPv6 literal later, at which point it will be bracketed and the brackets will
   need stripping — but that is a note about a future value, not behaviour to
   implement against input nothing produces.
2. Write the token's 32 ASCII hex characters as the **first bytes of the
   stream**, before anything else. The host reads exactly that many and
   compares. **A connection whose token does not match is closed and
   forgotten, and the host keeps accepting until its launch deadline.** The
   listener is a loopback port any local process can reach, so a rule under
   which a stranger's connection failed the launch would hand every local
   process a veto over every plugin launch; the security property is the one
   in §3.1's last bullet — nothing is sent on a connection before its token is
   validated — and a dropped stranger has been handed nothing.
   **The token may be written with a loop.** §6 rule 2's ban on `write_all`
   protects record framing, and the token is not a record — a short write here is
   ordinary and is retried, whereas a short write of a *record* is the fault that
   ban exists to expose.

   **Both ends bound the authentication phase locally.** The plugin's write loop
   and the host's 32-byte read are the one place in this protocol where a peer
   can stall the other before any session exists, and a wedged peer would
   otherwise turn a launch failure into an indefinite wait. **On the plugin's
   side a partial write, a timeout, or any I/O error fails the launch** — a
   setup failure, not a `DoorbellFault` (§3.1), since there is no session yet
   to tear down. **On the host's side the same events close that connection
   only**, for the reason above: each accepted connection's token is read on
   its own thread under a per-connection bound, so a stranger that connects
   and sends nothing costs the listener nothing, and the plugin's own
   connection failing the phase surfaces as the launch deadline expiring
   (*"did not connect its doorbell within N s"*) with the plugin's stderr as
   the diagnosis. The timeouts' values are local policy and are deliberately
   not wire constants; what is required is that they exist.
3. **Record framing begins at the first byte after the token.** The token is
   not a record: no header, no `crc`, and it is not counted in any framing
   state. **The token travels plugin→host only**: it is the first thing the
   plugin writes and the first thing the host reads, and the host→plugin
   direction of the same connection carries records from its very first byte.
   Each direction is framed independently from then on.
4. **Set `TCP_NODELAY`.** A 112-byte record is far below the MSS, so Nagle
   would hold it waiting for more; this channel's whole purpose is the ~11 µs
   path. Latency only — nothing about correctness depends on it, but every
   other socket option on this channel is stated and this one should be too.
5. Set the socket non-blocking **after** the connect completes and the token has
   been written. A non-blocking connect is a harder dance for no benefit here: it
   is to loopback, and the plugin has nothing else to do until it succeeds.
6. Bind gRPC on `127.0.0.1:0`, then publish the bound port by writing a
   temporary file and renaming it over `VIZI_PLUGIN_PORT_FILE`. The file's
   appearance is the host's readiness signal, so it must never be observable
   half-written.
7. **Start the doorbell listener before publishing the port file.** The file's
   appearance is the readiness signal and session-setup grants follow it
   immediately (§11a); a plugin that publishes before it is draining can lose
   them. The same ordering applies on unix, where readiness is the gRPC bind.
8. The host echoes the same token in `Hello.session_token`. A plugin seeing a
   different one fails the session.

The token authenticates the *connection*; it is not a capability and confers
nothing on the records that follow.

---

## 4. The record

Fixed **112 bytes**, little-endian.

| off | size | field | rule |
|---|---|---|---|
| 0 | 1 | `op` | never `0xFF` |
| 1 | 1 | `ver` | 2 |
| 2 | 1 | `flags` | all bits reserved, MUST be 0 |
| 3 | 1 | `reserved` | MUST be 0 |
| 4 | 4 | `id` | +1 per record, per sender, per direction, per session |
| 8 | 100 | `payload` | op-defined; padding MUST be zero |
| 108 | 4 | `crc` | CRC-32/ISO-HDLC over bytes 0–107, all 32 bits, LE |

112 is the largest record that keeps macOS at its maximum stock queue depth of 32
— its cheapest socket-buffer bucket tops out there, and 128 drops the depth to 28,
an eighth *(measured)*.

**`crc`** is **CRC-32/ISO-HDLC exactly as `zlib.crc32` computes it** —
reflected, init `0xFFFFFFFF`, final xor `0xFFFFFFFF`. It is *unmasked* in the
sense that we reserve no sentinel and use all 32 bits; that is not a claim that
`XorOut` is 0. `0xFFFFFFFF` is an ordinary valid checksum: if the CRC matches,
the record is good, and the checksum's value is never itself a signal.
Conformance fixtures carry the check value over `"123456789"`, `0xCBF43926`, so an
implementer can self-test the primitive before touching framing.

**`id`** is diagnostic and correlational only. It matches a send to a receive in a
trace and makes a duplicate visible. **No receiver drops a record on `id` alone.**
A gap gives *aggregate* drop detection for a direction from one `u32` of receiver
state; it gives no per-op attribution, because the counter is shared and a
high-rate op burns through it. An op wanting its own drop count carries its own
counter.

`id` **starts at 1 for each direction of each session, is incremented by exactly
one per record, and wraps through the full `u32` including 0.** Nothing is
reserved. "Monotonic" is not enough: the gap argument below needs consecutive
allocation, exactly as `doorbell_E_input.md` §6 needs it for `interaction`.

**It is allocated when the record is admitted to the writer, and the requirement
is an ordering one**: ids reach the wire in allocation order, and everything
admitted burns exactly one. Allocating inside §6 rule 1's serialization is the
obvious way to get that and is what vizi does, but the observable property is the
ordering, not the lock — an implementation that achieves it otherwise is
conforming.

- **Order is preserved.** Allocating earlier, outside that lock, lets two records
  reach the wire in the opposite order to their ids, and a receiver then sees a
  phantom gap followed by a regression — the exact signal this counter exists to
  make trustworthy.
- **Everything admitted burns its `id`** — sent, refused, or dropped because a
  retry was pending (§6 rule 8). That is what makes a gap mean *a record did not
  arrive*, and the commonest cause by far is the host dropping a `DRAG` on
  refusal. Allocating on successful send instead would make a gap mean transport
  loss, which cannot happen on unix, and the detection would silently measure
  nothing.
- **A record never offered to the writer never had an `id`**, so nothing
  invisible is counted.
- A retry reuses it unchanged, which is what makes a retry recognisable rather
  than a duplicate.

**Nothing needs its `id` before it is sent.** `M`'s preamble does not carry it —
those four bytes are padding (`doorbell_M_memory.md` §5) — so no sender has to
obtain an `id` in order to finish preparing a record, and admission is therefore
one step rather than an exchange. **Writing an `id` into those bytes produces a
preamble whose CRC disagrees with vizi's**, on the one structure that must be
reproduced byte-for-byte, and it is reported as `malformed` with nothing naming
the cause.
- **It wraps at `0xFFFFFFFF` to 0**, with no skipped value. Reaching it takes
  ~50 days of unbroken 1 kHz input.
- **Ahead and behind are decided by wrapping unsigned difference, not by `<`.**
  With `delta = (id − expected) mod 2³²`: `delta == 0` is the ordinary
  in-sequence case, `0 < delta < 2³¹` is ahead by `delta` and counts a gap, and
  anything else is behind and counts a regression.
- It correlates; it never identifies (`doorbell_M_memory.md` §3).

A receiver tracks one expected-next value per direction. Ahead of it counts a
gap, strictly behind it counts a regression, equality is the ordinary in-sequence
case, and **the record is processed either way.** A regression is almost always a
defect — a retry after a clean refusal never reaches the peer twice (§6) — so it
is a diagnostic signal, never a dedup input.

**It has exactly one benign producer, and it is specified**: a grant re-sent
after a "not present" missing-ack query reuses its original `id` unchanged
(`doorbell_M_memory.md` §7), because `shared_memory_protocol.md` §8's duplicate
test compares the record's 112 bytes and a fresh `id` would make the recovery
register as a *conflicting* grant. So an ordinary recovery emits one regression
per re-sent grant. **The counter is trustworthy net of grant re-sends**, which
matters because `doorbell_E_input.md` §6 designates it as the standing evidence
that a platform's ordering guarantee still holds.

**Expected-next starts at 1 and advances modulo 2³², through 0 exactly as the
sender's counter does**; skipping 0 on either side manufactures a false gap at
the wrap. It **advances only on a record ahead of or equal to it**, and a
regression leaves it where it was: setting `expected = id + 1` unconditionally
turns one out-of-order record into an unbounded run of phantom gaps — a
diagnostic flood from a single defect, which is what §9's *degrade, never
silently* corollary warns against. **Both surfaces exist and are not
alternatives**: the gap or regression is a diagnostic attached to the frame that
revealed it (`doorbell_implementation_details.md` §6), and the counter
aggregates (§12). A consumer that must tell an ordering failure from the benign
producer above needs the per-frame fact; one watching health needs the count.

**Integrity bound.** Any framing error is accepted with probability ~2⁻³². That is
the CRC's strength and this protocol's bound; no structural rule reduces it. It
detects **accident, not intent** — CRC-32 is linear and invertible, so a passing
frame confers no trust on its contents, and every field is validated
independently.

---

## 5. Framing

> **`op` is never `0xFF`, and 112 bytes of `0xFF` is a reserved recovery marker.**

That is the entire structural guarantee. The longest `0xFF` run **the framing
rule permits** is bytes 1–111 = 111, and runs cannot join across records because
the next byte 0 is `op`. Tighter bounds are derivable — `ver == 2` and the zero
`flags`/`reserved` bytes would cap a run at 108 — and are deliberately not
taken: `reserved == 0` and the `flags` rule are cheap rejects, not premises, and
a bound that spends three rules is one a later version breaks by spending one of
them (`doorbell_principles.md` B3).

**Baseline v2 is fail-stop.** There is no resynchronization: no marker emission, no
scanning, no resync request. A framing violation is a protocol-integrity failure
for that plugin session, equivalent in consequence to losing gRPC.

`op != 0xFF` remains a hard invariant even though nothing emits a marker; it is
what keeps recovery available as a purely additive change.

**The marker is not detectable under baseline validation, and that is
deliberate.** Its `crc` field is `0xFFFFFFFF` while the checksum over its first
108 bytes is `0x4D454264`, so a marker fails §7.5 step 2 and lands in the
ordinary CRC path, which is a fault on every transport (§8). Nothing emits one,
so nothing is lost. **A receiver that negotiates
recovery adds a full 112-byte marker compare ahead of the CRC step at that
point**; the invariant that makes the scan sound — no legal record can produce a
112-byte run of `0xFF` — is a property of the byte pattern and owes nothing to
the checksum, so it is available whenever recovery is.

What *is* detectable today is a record with `op == 0xFF` that is not a marker and
whose CRC is valid: a peer emitting a reserved opcode. That is a fault, class
`reserved_op` (§12).

**When a newer sender may actually use an addition.** Dropping an unknown op is
only safe if nothing correctness-bearing was in it, so: **an addition under an
unchanged `ver` must be advisory or optional — something whose absence leaves the
receiver correct.** Anything a receiver must act on to stay correct is a `ver`
bump, which the handshake settles by failing whole. Without that rule the
skip-and-continue below would be the *partial acceptance* the version policy
forbids (`plugin_principles.md`, Version skew).

**An `op` this version does not define is dropped and counted, never a fault.**
Framing is fixed-size, so skipping is exact and cannot desynchronise; the
`Hello`/`Ready` handshake has already settled the version, so an unknown opcode
is an unknown *kind*, which is the one thing the append-safety rule covers
(`plugin_principles.md`, Version skew). The receiver:

- validates only what is op-independent — length, `ver`, `flags`, `reserved`,
  `crc`;
- **does not check padding**, whose extent is op-defined and therefore unknown;
- closes every attached descriptor (§7.4) and accepts **zero or one** —
  §7 rule 3's declared count is a property of a *defined* op, but the
  protocol-wide cap of one applies to every op, known or not. **More than one is
  terminal**, as is `MSG_CTRUNC`. The rule is on the count rather than on what
  the buffer happened to hold, so two receivers with different buffer sizes
  reach the same outcome;
- aggregates a diagnostic rather than logging per record, and continues.

`MSG_CTRUNC` remains a fault regardless of `op`: a truncated control message
means descriptors were lost, which is a transport failure, not an unknown kind.

---

## 6. Sending

1. **One serialized writer per direction.**
2. **One record per send call.** Issue a single `write`/`send` and assert
   `n == 112`. Never `write_all` — it loops on a short return and so manufactures
   the torn record it should detect.
3. **Non-blocking on every platform, set once at setup, on every platform.** The
   socket's blocking mode is a property of the socket and is established before
   the first use — not latched by whichever call site happens to run first, which
   is the failure `plugin_principles.md` 10 exists to forbid (`O_NONBLOCK`
   inherited from a reactor's `dup` and `SO_SNDTIMEO` latched by an unrelated
   caller are two forms of it). **The inherited unix fd
   is set non-blocking immediately after adoption**, exactly as its inheritable
   flag is cleared (§3.1).

   That makes the listener readiness-polled **everywhere**, which §7's classifier
   and §8's macOS requirement both already assume. There is no blocking-listener
   variant: once `O_NONBLOCK` is on the file description, `recv` cannot block, so
   a wake endpoint is needed on Linux for the same reason it is needed on macOS.
   `MSG_DONTWAIT` stays at every unix send site — including `send_with_fd` — as
   the redundant assertion principle 10 asks for, not as the mechanism. Windows:
   `set_nonblocking` at setup; never `SO_SNDTIMEO`, and never a per-call timeout
   on a shared socket.

   **On Windows the same mode is socket-wide** *(measured)*, so it covers
   receives too; a listener there needs `select` / `WSAPoll` around its receives
   exactly as every other platform does.

   **A readiness-polled listener with a dedicated wake endpoint is required on
   all three platforms**, at the same requirement level on each. The argument is
   this rule's own and it is the same everywhere: the socket is non-blocking, so
   `recv` cannot block, so `shutdown()` is not an exit path and something must
   wake the listener. Not a "self-pipe": `select`
   and `WSAPoll` wait on sockets, and an ordinary Windows pipe is not one, so the
   portable form is a loopback socket pair — an `AF_UNIX` `socketpair` on unix, a
   connected loopback TCP pair on Windows — polled beside the doorbell.

   **What is required is the property, not the mechanism**: a thread-safe stop
   that is bounded, cannot deadlock, and cannot spin, woken by something the
   same readiness primitive can wait on alongside the doorbell.

   **`stop()` is a cancellation request, and returns as soon as the request is
   recorded and the socket released.** It is *not* a join. A listener blocked
   inside a consumer callback cannot be released by a wake endpoint — nothing
   can, short of killing the thread — so a `stop()` that promised the listener
   had finished would be unimplementable against the inline dispatch
   `doorbell_implementation_details.md` §6 describes. Joining is a separate
   operation and is bounded only by the consumer: **transport callbacks SHOULD
   return within a documented bound**, and an SDK that dispatches a slow handler
   inline owes that bound to its own users. A runtime
   whose selector or reactor already offers cancellation satisfies it directly.
   The socket pair is named because it is what satisfies the property on every
   platform at once — on Windows in particular, where `select`/`WSAPoll` wait on
   sockets and an ordinary pipe is not one, so a self-pipe does *not* satisfy
   it. An `eventfd` on Linux does.

   Be precise about what it buys: **polling prevents an unkillable blocked call;
   it does not manufacture a close signal.** The thing that actually wakes the
   listener is **a write to that endpoint**, so a transport needs a thread-safe
   stop in its published surface — not merely a socket someone else can close.

   **`shutdown()` is not the listener's exit path**, and the reason is this
   rule's own rather than any platform's: the stop has to be an operation the
   transport *publishes*, not something a consumer achieves by reaching around it
   into the socket. A wake endpoint gives that; `shutdown()` requires whoever
   stops the listener to hold the socket.

   It also does not behave uniformly, which is a second reason not to build on
   it. `shutdown(SHUT_RDWR)` gives `POLLIN|POLLHUP` on every type measured, but
   the paired `recv` differs: **0 bytes on all three shipped doorbells** — Linux
   `SOCK_SEQPACKET`, macOS `SOCK_DGRAM`, Windows TCP — and **`EAGAIN`,
   indefinitely, on Linux `SOCK_DGRAM`** *(measured on both platforms)*, which §7
   rule 5 correctly calls "not an error, resume polling" and which therefore busy
   spins. That configuration is nobody's doorbell, so no shipped transport spins;
   it is recorded because `SOCK_DGRAM` is a doorbell type *somewhere*, and an SDK
   that develops against Linux with the macOS socket type would meet it.
4. **`MSG_NOSIGNAL` on Linux; `SO_NOSIGPIPE` at socket setup on macOS.**
   `SO_NOSIGPIPE` is **not exposed by CPython's `socket` module** — pass the raw
   value (`0x1022`) to `setsockopt`.

   **Neither doorbell type raises `SIGPIPE` on either platform** *(measured, both,
   with a counting handler deliberately installed)*. Only `SOCK_STREAM` signals;
   `SOCK_SEQPACKET` and `SOCK_DGRAM` return their errno silently. So this is
   **defence in depth, not the mechanism** — and **`EPIPE` must not be read as
   evidence of a signal**, since neither doorbell type raises one and
   `SOCK_DGRAM` does not report `EPIPE` at all (rule 7's table).

   It stays because principle 10 asks a safety property to be asserted where it
   is depended on rather than inherited from a socket type someone may change —
   and because Windows' doorbell *is* a stream. **It is SDK robustness, not wire
   conformance**: nothing a peer sees depends on it, so a conformance run cannot
   and should not test for it. Note `SO_NOSIGPIPE` is **refused
   outright on Linux** (`ENOPROTOOPT`), so the platform split here is a
   requirement, not a preference.

   **Every runtime a plugin is likely to be written in disarms the signal
   before any plugin code runs**: CPython installs `SIG_IGN`, Rust's runtime
   does the same before `main`, and HotSpot does it during JVM startup. The
   property is only observable with a handler installed on purpose.

   **That is an argument for the flag, not against it.** The more runtimes that
   disarm it for their own reasons, the less any one of them is a property this
   protocol may depend on — none of them does it *for us*, none of them promises
   to keep doing it, and a plugin author can re-arm the signal in their own
   process without knowing they have broken a transport invariant. It is also
   why relying on CPython's `SIG_IGN` is not sufficient in particular: Rust's
   handling runs in `lang_start`, which never executes for a cdylib.
5. **Assert every safety property; inherit none.** A property that holds because
   of an unrelated call site does not survive a refactor.
6. **Refusal is backpressure.** **This table is the protocol's one send
   classifier**; where another document names a subset it is citing it.

   | class | errnos | action |
   |---|---|---|
   | backpressure | `EAGAIN`, `EWOULDBLOCK`, `ENOBUFS`, `WSAEWOULDBLOCK`, `WSAENOBUFS`, `ETOOMANYREFS` | **the sending op's policy decides** (§10) — a retried edge waits for the next writability edge, a disposable record is dropped; `ETOOMANYREFS`, and `ENOBUFS` on a datagram socket, are the exceptions below |
   | interrupted | `EINTR` | retry immediately |
   | peer gone | rule 7's list | teardown, no fault |
   | **locally closed** | `WSAESHUTDOWN` | teardown, no fault, **not attributed to the peer** |
   | anything else | — | fault |

   POSIX permits `EAGAIN` and `EWOULDBLOCK` to be distinct values, and Winsock
   documents both `WSAENOBUFS` and `WSAEWOULDBLOCK` for `send`; a classifier that
   names one of each pair works until it doesn't. Never a session fault, never a
   tool deactivation.

   **`EINTR` is retryable too, and immediately** — it is not backpressure, so it
   waits for no writability edge. An interrupted datagram send transfers nothing,
   so re-issuing it is exact. CPython retries automatically since PEP 475, which
   is why this is easy to leave out and why the omission surfaces on the first Go
   or Rust plugin issuing a raw `sendmsg`; without it, a signal closes the
   doorbell.

   `ETOOMANYREFS` is the one that is easy to miss: unreceived `SCM_RIGHTS`
   descriptors are charged against the **sending user's** `RLIMIT_NOFILE`, not
   per socket, so it appears only once several sessions each hold a full buffer —
   the multi-session case, where killing a session is least acceptable. It clears
   when a peer drains and queues nothing, so it is backpressure by every property
   that matters.

   **It is the one refusal that cannot be retried on a writability edge.** The
   limit is user-wide, so *this* socket may stay writable throughout and no edge
   ever arrives; a retry armed on readiness waits forever. `ETOOMANYREFS` retries
   on a scheduled timer instead.

   **`ENOBUFS` on a datagram socket is timed for the same reason.** On macOS's
   `SOCK_DGRAM` it reports the *receiver's* buffer full (§11), so the sender's
   socket can stay writable throughout; an edge-armed retry then spins, spends
   the whole retry budget in microseconds, and a plugin whose handler stalled
   for half a second loses its doorbell and its session. It is re-offered after
   a pause that starts at 10 ms and doubles to a 160 ms ceiling, so the budget
   spans about 0.8 s. `ENOBUFS` on a stream socket is the sender's own buffer
   and keeps the writability edge. These two are the only timed retries; every
   other refusal in this rule is a property of the sending socket, and its edge
   is the correct trigger.
7. **`n < 112`, or any ambiguous send error, closes the doorbell.** No further byte
   is appended and the session tears down through the gRPC-transport-error path.
   **`EPIPE` / `ECONNREFUSED` / `ECONNRESET` / `EDESTADDRREQ` / `ENOTCONN` /
   `WSAECONNRESET` / `WSAECONNABORTED` are not in that bucket.** They mean the
   peer is *gone*, which is teardown, not a fault: nothing arrived, nothing is ambiguous, and the session
   is over for reasons this send did not cause. Treating them as faults makes
   every shutdown race emit a `DoorbellFault` — the send-side twin of the
   orderly-close mistake §8 warns about on receive.

   **The errno depends on the socket type *and* the platform, and is not
   guessable from either alone.**

   **Every cell is the first send and the ones after it**, because on a datagram
   socket they differ:

   | plain `send` after the peer closed | Linux | macOS |
   |---|---|---|
   | `SOCK_STREAM` | `EPIPE`, then `EPIPE`, 1 signal | `EPIPE`, 1 signal |
   | `SOCK_SEQPACKET` | `ECONNRESET` **if the peer had unread data queued**, else `EPIPE`; `EPIPE` thereafter; no signal | type does not exist |
   | `SOCK_DGRAM` | `ECONNREFUSED`, then **`ENOTCONN`**, no signal | **`ECONNRESET`**, then **`EDESTADDRREQ`**, no signal |

   `ECONNREFUSED` is **Linux's** datagram errno and never occurs on macOS; the
   two datagram columns share no errno at all, so a classifier written from one
   platform's column is wrong on the other.

   **Non-stickiness is a property of the datagram types, not of macOS.** Both
   datagram columns change errno at the second send while both stream/seqpacket
   columns repeat theirs. A classifier that lists only the first errno faults on
   the *second* record of an ordinary teardown, which is this rule's own failure
   mode arriving one call later — and it is why the list above is a set of
   errnos rather than a per-platform first-value table.

   **Each column settles after its first send, but which value comes first is
   not fixed** *(measured)*. Linux `SOCK_SEQPACKET` gives `ECONNRESET` rather than `EPIPE` when
   the closing peer still had **unread data queued to it** — an ordinary
   teardown, since a plugin's undrained `D` records are exactly that. **So the
   classifier is a set and must stay one**: trimming it to whatever value a
   table shows first faults on a normal shutdown.

   **Windows distinguishes three cases where unix collapses them** *(measured)*:

   | how the peer went | 1st send | every send after |
   |---|---|---|
   | orderly (exit, FIN) | **succeeds** | `WSAECONNABORTED` |
   | abortive (killed, RST) | `WSAECONNRESET` | same |
   | our own `shutdown(Write)` | `WSAESHUTDOWN` | same |

   **`WSAESHUTDOWN` is not a peer-gone signal and must not be listed as one.** It
   arises in neither close scenario — only ever from *our own*
   `shutdown(Write)` with the peer alive, which is §10's escape hatch doing its
   job. A classifier keyed on it reports a dead host for a state we caused. It is
   its own class in rule 6: teardown, no fault, and never attributed to the peer.
   On Linux the same event is indistinguishable — all three collapse to `EPIPE`
   after the first call — so there the cause cannot be recovered from the errno,
   and a diagnostic should say so rather than guess.

   **The first send after an orderly Windows close succeeds.** A record is
   accepted into a socket whose peer has already gone, and only the *next* write
   reports it *(measured)*. Two consequences:

   - **A successful send is never evidence of delivery.** §9 argues this from
     first principles; this is the measured instance, and its window is exactly
     one record.
   - **Anything that poisons the channel on a write error lets that one record
     through believing it landed.** The record classes already absorb it — a
     disposable record was droppable anyway, and a retried edge's reliability
     comes from the acknowledgement rather than the send (`doorbell_principles.md`
     D1a) — but an implementation that treats "send returned Ok" as arrival is
     wrong by exactly one record, at teardown, every time.
8. **The outbound path is strictly ordered per direction.** A record awaiting
   retry sits at the head of its direction's queue and nothing behind it is sent
   until it goes or its budget is exhausted. **Disposable records generated
   during a pending retry are dropped, not queued** — which is their policy
   anyway (§9), and is what stops a queue growing behind a stalled edge. A
   retried `PRESS` therefore cannot be overtaken by a later `DRAG`, which is the
   ordering hazard §14 cites against splitting phases across transports and
   which a retry queue would otherwise reintroduce inside one.

Non-blocking on Windows is what makes rule 6 meaningful there. A `SO_SNDTIMEO`
expiry is ambiguous — a timed-out send has typically already delivered the record
*(measured)* — and under fail-stop an ambiguous send is terminal, so a timeout
would end a session for 200 ms of ordinary backpressure. Non-blocking refuses
cleanly, giving all three platforms one contract: **the whole record went, or
nothing went, cleanly reported.**

**So a clean refusal means nothing arrived, on every platform including Windows,
and a retry after one cannot duplicate.** There is no surviving
delivered-but-reported-failed case: every other send error is ambiguous, and rule
7 closes the doorbell on those rather than retrying. **The hazard is a property
of blocking sends with `SO_SNDTIMEO`, not of Windows** — which is what the
non-blocking rule removes, and why nothing in v2 may be justified by "a refused
Windows send may already have arrived."

---

## 7. Receiving

Rules 1–4 are cited elsewhere as §7.1–§7.4.

> **`MSG_CTRUNC` is the only `msg_flags` bit this channel reads, and it MUST be
> read through the platform's own constant.** Never hardcode the value, and never
> carry one across platforms: these bits differ between Linux and Darwin *and*
> collide with each other *(measured on both)*.
>
> | | Linux | Darwin |
> |---|---|---|
> | `MSG_CTRUNC` | `0x08` | `0x20` |
> | `MSG_EOR` | `0x80` | `0x08` |
> | `MSG_TRUNC` | `0x20` | `0x10` |
>
> A Linux-derived `MSG_CTRUNC` of `0x08` tests `MSG_EOR` on Darwin, which is
> never set on this transport — so **the unconditionally-fatal
> descriptor-truncation check silently never fires**. It fails *open* and does
> not error: a wrong bit reports a different flag rather than failing. The other
> two rows are there because they are what `0x08` and `0x20` mean on the wrong
> platform, not because anything reads them — rule 1 uses the datagram's length,
> and nothing reads `MSG_EOR` at all (§8).
>
> **Darwin also echoes input flags back into `msg_flags`**, so a receive that
> passes `MSG_DONTWAIT` reads that bit back out. Rule 1's "pass no input flags"
> is what removes the question; `MSG_CMSG_CLOEXEC` is the one exception and does
> not exist on Darwin, so nothing is echoed there.

1. **Message transports:** one datagram is one record. **Receive into a buffer
   strictly larger than 112 bytes** — at least 113; vizi uses 120 to keep every
   buffer on this channel on an 8-byte boundary, which no peer can observe.
   **The length alone then classifies the frame**, with no flag involved:

   | return | meaning |
   |---|---|
   | `= 112` | a record; validate it |
   | `0` | **not a length at all — see §8**, which decides it by transport class: teardown on `SOCK_SEQPACKET`, `bad_length` on `SOCK_DGRAM` |
   | `0 < n < 112` | undersized — `bad_length`, fault |
   | `> 112` | oversized — `bad_length`, fault |

   **The zero row is separate because folding it into `< 112` faults every
   clean shutdown**, which §8 opens by naming as the first mistake a new
   implementation makes. Only a *short but non-empty* frame is malformed.

   Into a *record-sized* buffer an oversized datagram returns exactly 112
   *(measured)* and is indistinguishable from an exact one, which is the whole
   reason for the extra byte. The surplus is never part of a record and is
   retained in the diagnostic.

   **This rests on a read never spanning a message boundary, which is measured
   rather than assumed** *(both datagram types)*. Two queued 112-byte records read into a 120- or
   224-byte buffer return **112**, never 120 or 224. Stream transports do
   coalesce, which is why rule 2 is a different rule.

   **`MSG_TRUNC` is not used, in either direction.** As an *output* flag it says
   only what the length already said; as an *input* flag it is a portability trap
   with no upside — Darwin ignores it for the return value and then echoes it
   back into `msg_flags` (see the box above), so a receiver that passes it
   uniformly reads truncation on **every** record and faults the session on the
   first one. **Pass no input flags at all**, except `MSG_CMSG_CLOEXEC` where it
   exists (§7.6); §6 rule 3's setup-time non-blocking mode is what makes that
   possible, since no `MSG_DONTWAIT` is needed either.
2. **Stream transports:** accumulate to exactly 112 bytes, then validate.
   Arbitrary chunking is expected.
3. **`MSG_CTRUNC`, or an ancillary-descriptor count other than the op's declared
   one, is a fault.** Surplus descriptors are closed before the record is discarded.
   **The rule is on the COUNT, so the buffer size is an implementation
   choice.** An unknown op may carry zero or one descriptor, which is closed
   before the record is skipped; **more than one, or `MSG_CTRUNC`, is
   terminal.** Every receiver then behaves identically on identical traffic
   whether it sized for one descriptor or ten, and legal traffic — capped at one
   protocol-wide — is always accepted.

   Mandating a buffer capacity would also make the behaviour uniform, but it
   fixes the wrong variable: with the count rule, a one-slot receiver that takes
   `MSG_CTRUNC` and a two-slot receiver that counts two descriptors reach the
   same terminal outcome by different routes, which is all conformance needs.
   Sizing for two remains worthwhile for *diagnostics* — it reports
   `wrong_fd_count` where a one-slot receiver reports `ctrunc` — and is a
   SHOULD on that basis alone.
   The declared counts are constant per op **on a given transport class**:

   | op | message transport | stream transport |
   |---|---|---|
   | `M` | exactly 1 | 0 |
   | `E` | 0 | 0 |
   | `D` | 0 | 0 |
   | unknown | **0 or 1**; closed before skipping; **>1 is terminal** | 0 |

   **No v2 record carries more than one descriptor.** That protocol-wide cap is
   what makes the unknown-op rule in §5 safe: legal traffic never exceeds one, so
   an unknown op can always be skipped without hitting `MSG_CTRUNC`, which is
   always fatal. **Append-safety is bounded accordingly** — an op needing more
   than one descriptor is a `ver` bump, not an addition, and the handshake keeps
   it away from a receiver that predates it.

   The two-descriptor buffer does *not* provide that safety and is not why it
   holds; it only turns one specific violation — a peer sending two — from
   `ctrunc` into the more precise `wrong_fd_count`. A peer sending three still
   faults, correctly.

   The table is keyed on transport class because that is what the descriptor
   count actually follows; §3's platform→type mapping is what says which class a
   given platform has. **A socket whose `SO_TYPE` is not its platform's is
   refused at setup** — `SOCK_SEQPACKET` on Linux, `SOCK_DGRAM` on macOS, a
   stream on Windows. Settling it at connect is the version-skew rule applied to
   the transport: fail whole and say so, rather than discovering at the first
   record that the class was never what the table assumed.

4. **A discarded record still releases its resources.** An attached fd is closed
   before discard.
5. **Classify a receive error, exactly as §6 rule 6 classifies a send error.**
   Without this, §8's "an OS error that is not peer-gone is a fault" makes the
   two most ordinary events on this channel fatal.

   | outcome | errnos | action |
   |---|---|---|
   | interrupted | `EINTR` | retry immediately; nothing was consumed |
   | would-block | `EAGAIN` / `EWOULDBLOCK` / `WSAEWOULDBLOCK` | not an error — resume polling |
   | locally closed | `WSAESHUTDOWN` | teardown, no fault, not the peer's doing |
   | peer gone | the §6 rule 7 list | orderly teardown, no fault |
   | anything else | — | fault, class `recv_error` |

   **Would-block is the ordinary case, not an edge.** A readiness-polled listener
   is required on every platform (rule 3), and `EAGAIN` is what §8's own
   table reports for a closed Linux datagram peer. **Windows' non-blocking mode is
   socket-wide, measured**: one `set_nonblocking(true)` and both a receive with
   nothing to read and a send into a full buffer return `WSAEWOULDBLOCK`, while
   blocking mode blocks on both. `EINTR` is the same event rule 6 already
   argues about, hidden by CPython since PEP 475 and not by Go or Rust.

---

### 7.5 Validation order

**Four parts of this order are normative; the rest is a recommendation.** What
binds is the order that protects safety or state: acquire the frame and handle
its length and ancillary data first; **CRC before any field is trusted**;
descriptor cleanup on every exit path; and the `id`-tracking boundary below. The
`E` semantic order in `doorbell_E_input.md` §2 is separately normative, because
there the order changes which gesture is cancelled and which record is processed.

**Precedence among simultaneously-terminal header defects is not normative** —
`bad_ver` versus `bad_flags` versus `bad_reserved` only decides which diagnostic
wins when several coexist, and every one of them tears the session down
identically. Two conforming receivers may disagree there. It is stated as an
order because a suggested one is cheaper for an implementer than none, not
because a peer can tell.

CRC comes first because a misaligned 112-byte window is arbitrary bytes:
`bad_crc` is the honest classification of a frame we cannot vouch for, where
`bad_ver` would claim we had read a field.

1. **Acquire a frame.** Message: one datagram, length exactly 112. Stream:
   accumulate to exactly 112.
2. **`crc`.** Nothing else is inspected first. On failure: **immediate fault, on
   every transport** (§8).
3. **`ver`, `flags`, `reserved`.** Fault on any.
4. **`op`.** `0xFF`: fault (`reserved_op`) — a whole-record marker never reaches
   this step, having failed step 2 (§5). A defined op in an illegal direction:
   fault. An op this version does not define: drop and count (§5), and stop here
   — nothing below is knowable.
5. **The op's declared ancillary count**, for a now-known defined op. Wrong:
   fault. **The protocol-wide cap of one is checked with step 1, not here** —
   otherwise an unknown op stops at step 4 and never reaches a count check, and
   §5's "more than one is terminal" would be unreachable for exactly the traffic
   it is written about.
6. **The op's fixed padding** — the ranges at fixed offsets with fixed extents
   that §8 assigns to framing. Non-zero: fault.
7. **The op's own payload rules**, including any range whose extent depends on a
   payload field. Violations here are the op's to report, and never a fault.

**Where `id` tracking sits, and where it must not.** Update expected-next
**after step 3 and before step 4** — that is, for every frame that has passed
CRC and the op-independent header checks, *including* an unknown opcode and
including a record later rejected on the op's own rules. Two failure modes make
the placement normative rather than incidental:

- **A window that failed CRC must never feed it.** Its `id` is arbitrary bytes.
  A bad CRC is terminal (§8), so nothing follows it to be affected — but the
  rule costs nothing, holds for the recovery mode `doorbell_principles.md` E2
  reserves syntax for, and stops a receiver reaching for a value it has no reason
  to trust.
- **A record rejected for op-level reasons must feed it**, or the counter
  under-reports exactly the drops it is there to see.

`MSG_CTRUNC` **and the protocol-wide one-descriptor cap** are checked with step
   1 and fault regardless of everything below, so they apply to unknown ops too.
**Every exit path closes any attached descriptor before discarding** (§7.4) —
step 1's included, since a wrong-length datagram or a truncated control message
can carry descriptors too.

### 7.6 Descriptors are never inheritable

**Every descriptor this channel touches is closed on `exec`**: the inherited
doorbell fd, and every descriptor arriving by `SCM_RIGHTS`. Neither is by
default, and each leak has a consequence this document already names.

- **The doorbell fd.** It is inheritable because it had to survive the host's
  `exec`, and adopting it leaves it that way *(measured)*. Any child the plugin
  spawns then holds the doorbell open after the plugin dies, so the host sees
  neither EOF nor `EPIPE` — **§8's entire teardown-detection path on
  `SOCK_SEQPACKET`, defeated silently.** The plugin clears the flag right after
  adopting it.
- **Granted descriptors.** A descriptor received through `SCM_RIGHTS` comes back
  inheritable *(measured)*. One leaked into a child outlives the plugin's unmap
  and keeps the segment alive — `doorbell_M_memory.md` §1's *invisible
  allocation*, arriving by a second route that no memory accounting sees.

**Linux closes the race and macOS does not** *(measured on both)*. `recvmsg` with
`MSG_CMSG_CLOEXEC` delivers the descriptor already non-inheritable on Linux,
atomically. **macOS does not have the flag and the descriptor arrives
inheritable**, so a receiver there clears it immediately after `recvmsg` returns
and wears a real window — not a theoretical one — in which a concurrent `fork` +
`exec` leaks it. Use the atomic form where it exists; do not treat the fallback
as equivalent.

This is the `SO_NOSIGPIPE` shape again: **a runtime's spawn defaults decide
whether the defect is visible, and they differ.**

| runtime | what a spawned child inherits | so |
|---|---|---|
| CPython | `subprocess` defaults to `close_fds=True` | accidentally safe |
| JVM | `ProcessBuilder` passes **nothing above descriptor 2** *(measured)* | accidentally safe; only an FFM-spawned child could leak |
| Go | `os/exec` passes an inherited descriptor that is not close-on-exec | **leaks** |

**So the first Go plugin that spawns anything is where this surfaces, and a JVM
plugin is not.** Most runtimes are safe by accident, which is precisely principle 10's case for asserting the property rather than
inheriting it. A plugin that reaches for FFM, or a runtime that changes its spawn
default, loses a safety property nobody wrote down as depending on it.

---

## 8. Faults

Only the affected plugin session shuts down. Nothing a peer writes may crash or
block the host.

**An orderly close is not a fault.** A receive returning end-of-stream — 0 bytes
on a stream, or a closed socket on a message transport — means the peer closed
the doorbell, which is what teardown *is*. The listener stops and the session
ends through the control plane. **"Reports nothing" means no `DoorbellFault`, not
no outcome**: every peer-gone close and every local stop emits **exactly one
teardown outcome** (`doorbell_implementation_details.md` §6), because the
transport is the only thing that can observe the close and a consumer waiting on
one would otherwise wait forever. **A terminal fault replaces it** — a faulting
session emits the fault and no teardown, and a `stop()` afterwards emits
nothing, so a consumer sees exactly one terminal event either way. Only a **short but non-empty**
frame is malformed. Getting this wrong makes every normal shutdown emit a
`DoorbellFault`, which is the first thing a new implementation does.

**End-of-stream with a partial window buffered is teardown, not a torn record.**
A peer that closes mid-record is a peer that is going away; faulting achieves
nothing it does not already get. The buffered bytes go in the diagnostic.

**A receive error meaning the peer is gone is teardown too**, by the same errno
list. Only end-of-stream and an OS error that is *not* peer-gone are separable
states: the first is teardown and the second is a fault.

**Neither end ever sends a zero-length datagram**, and **what a 0-byte return
means is decided by transport class, not by a universal rule**:

| class | a 0-byte return means |
|---|---|
| stream (Windows) | end-of-stream — teardown. **Orderly close only**: after an *abortive* close the receive returns `WSAECONNRESET` in both modes rather than 0 *(measured)*, which rule 7's list already routes to teardown |
| `SOCK_SEQPACKET` (Linux) | teardown — the guarantee removes the only other case |
| `SOCK_DGRAM` (macOS) | **an empty datagram from a live peer** — the guarantee broken, so `bad_length` |

A closed datagram peer never returns 0 on either platform — Linux reports
`EAGAIN`, macOS `ECONNRESET` (below) — so on `SOCK_DGRAM` a 0-byte read can only
be a peer emitting an empty message, and reading it as teardown would end a
session silently on a peer bug.

**The risk this leaves is on Linux, not macOS** — which is the reverse of the
intuition that macOS's weaker socket type must be the exposed one. On
`SOCK_SEQPACKET` an empty datagram and a peer close are the *identical* 0-byte
read: same length, same flags. **`MSG_EOR` cannot separate them, and is not a
candidate to** — it is never set on this transport, for anything, including a
well-formed 112-byte record *(measured, all three reads)*. So the
no-empty-datagram guarantee is what separates them and nothing verifies it.
A peer that breaks it ends the session silently there. On `SOCK_DGRAM` the same
guarantee is checkable, because a real close does not look like this at all.
That asymmetry is the price of one rule per class, and it is worth naming rather
than smoothing over.

**One empty message IS separable on every class: one that carries ancillary
data.** A real close carries none, and `SOCK_SEQPACKET` does deliver a
zero-length message with an `SCM_RIGHTS` descriptor attached *(measured on
Linux)*. So a 0-byte return with a descriptor count above zero, or
with `MSG_CTRUNC` set, is a **frame**, and §7's frame rules classify it in their
usual order — `ctrunc`, then `wrong_fd_count`, then `bad_length` — closing every
descriptor that arrived. Reading it as teardown would end the session under the
wrong reason and leak the descriptor.

### What a dead peer looks like

*(measured on Linux and macOS.)*

| after the peer closes | Linux `SOCK_SEQPACKET` | Linux `SOCK_DGRAM` | macOS `SOCK_DGRAM` |
|---|---|---|---|
| `send` | `ECONNRESET` if data was left unread, else `EPIPE`; `EPIPE` after | `ECONNREFUSED`, then `ENOTCONN` | `ECONNRESET`, then `EDESTADDRREQ` |
| `recv` | 0 bytes (EOF) | `EAGAIN`, indefinitely | `ECONNRESET` **once**, then nothing |

**On macOS the doorbell can announce peer death exactly once, and only to
whoever is reading at that moment.** Queued datagrams all arrive first; then one
receive reports `ECONNRESET`; then the socket goes quiet — a subsequent blocking
receive waits indefinitely and a poll reports no readiness. **It is not
invisible — Linux's datagram result does not generalise here — it is
unrepeatable**, which needs the same machinery for a different reason.

Three consequences, none optional:

- **The readiness-polled listener with a wake endpoint earns its keep twice over
  on macOS.** §6 rule 3 already requires it everywhere; here it is also the only
  way to catch the one signal, because a listener that is not reading when it
  arrives never gets another, and a blocking receive afterwards never returns.
- **`ECONNRESET` on a receive is peer-gone, not `recv_error`.** §7 rule 5 already
  routes it there through rule 7's list; getting this wrong turns every macOS
  teardown into a fault.
- **Teardown is still announced by the control plane**, as specified in
  `control_plane_protocol.md` §3.3. A one-shot signal is not something to build a
  shutdown path on; it is useful corroboration, and the authoritative notice
  arrives over gRPC.

**Immediate session faults:** on a message transport, a length that is neither
112 nor a transport-class teardown — `0 < n ≠ 112` everywhere, plus `n == 0` on
`SOCK_DGRAM` only, since zero is teardown on `SOCK_SEQPACKET` and on a stream
(the table above). That includes a
record split across two send calls (which arrives as two malformed messages, and is
the error a new SDK author hits first, which a datagram longer than 112 is counted
with); `MSG_CTRUNC`; wrong `ver`;
`flags != 0`; `reserved != 0`; **a non-zero byte in an op's fixed padding** —
**payload-relative offsets throughout**: `E`'s `[92..100)` (its `[42..44)` is `action`, `doorbell_E_input.md` §1), `D`'s
`[77..100)`, `M`'s byte 17, but *not*
`M`'s variable padding after the name terminator, which is the op's (see the
table below); `op == 0xFF` outside a negotiated recovery; a **defined** op in an
illegal direction (§2); a wrong ancillary-descriptor count for a **defined** op;
a sender-side short write or ambiguous send error.

**Not a fault:** an `op` this version does not define (§5). It is the one thing a
receiver skips, and the distinction is the whole of the additive-version story.

**A bad CRC is an immediate session fault on every transport.** No thresholds, no
counters, no staying in phase.

**A tolerance threshold for streams looks right and has no producer.** A bad CRC
on a byte stream is genuinely ambiguous between corruption and lost alignment, so
dropping the window and terminating only at a threshold reads as the careful
answer — but **the only stream transport is loopback TCP**, which does not
corrupt. A bad CRC there means the peer computes checksums wrong, writes
wrong-sized records, or has corrupt memory, and all three fail *every subsequent
window*. The tolerated case, an isolated bad CRC followed by a valid record, has
no producer but a peer deliberately pacing bad frames.

`doorbell_principles.md` E2's marker syntax stays reserved regardless, so a
recovery mode remains a purely additive change for the day field evidence asks
for one.

**A framing fault tears the session down; a semantic disagreement is reported
over gRPC and the record is dropped.** That line decides every "is this fatal?"
question in the op documents, so it is drawn once here:

- **Framing owns what is checkable at a FIXED offset with a FIXED extent**:
  length, `ver`, `flags`, `reserved`, `crc`, ancillary count, legal direction,
  and every byte range an op declares as fixed padding. All of it is fail-stop.

  **The criterion is the record's layout, not op-independence** — locating `E`'s
  `[92..100)` requires knowing the op, and §5's unknown-opcode rule explicitly
  does not check padding. What these ranges share is that their position is a
  property of the *format*: a non-zero byte in one means the peer's record
  generator disagrees with ours about the layout, which every subsequent record
  inherits. That is what fail-stop is for (`doorbell_principles.md` E1). A
  variable range is different in kind — its position comes from a payload field,
  so a violation is one record's content being wrong, not the format.

  The consequence is deliberate and worth seeing plainly: in `E`, a stray byte
  at `[92..100)` ends the session while a stray byte at `[44..92)` is reported
  and dropped, 24 bytes apart. The first says the sender is building a different
  record; the second says this record was filled in wrong.
- **The op owns anything whose extent or legality depends on a payload field** —
  `E`'s position union tail, which cannot be located without decoding
  `payload_kind`; `M`'s `name` tail, which needs byte 18 read as a length. A
  violation there means a well-formed record whose *content* is wrong, so it is
  reported over gRPC and the record is dropped, never faulted.

  **Which message carries the report depends on whether the op already has an
  answering channel.** `M` does: every one of its violations rides the **grant
  acknowledgement**, because the host needs the `segment_id` to re-grant and the
  ack is the message whose entire purpose is to say whether the install worked.
  `E` does not, so its violations go on **`ContractViolation`**. Neither is
  `DoorbellFault`, which is sent immediately before closing and ends the session
  — precisely what a semantic disagreement must not do. Shapes and sites:
  `shared_memory_implementation_details.md` §2.

**Zero padding is required always.** Every byte not covered by a populated field
is zero, including the unused tail *inside* a variable field — otherwise a
record is not a pure function of its fields and byte-exact vectors are
impossible (`doorbell_principles.md` B1). Which layer checks a given range
follows from the split above.

**Padding is not growth space, and reserved values are.** These get opposite
treatment on purpose, so the line is drawn once here:

| | on a non-zero or unknown value |
|---|---|
| a **fixed** padding range — `reserved` at byte 3, and every zero-filled span at a fixed offset and extent | **fault**, framing's |
| the `flags` byte | **fault** — a flag changes what a record means |
| a **variable** padding range — one whose position depends on a payload field, such as `M`'s bytes after the name terminator | **the op's**: reported and dropped, never a fault |
| a reserved **enum value**, or a **bit inside a populated field** | **ignore it, count locally, process the record** |

**Why padding is fatal and a reserved enum value is not**, since the same "it
might be an undeclared field" reasoning would seem to cover both: it is what each
range is *declared to be*. A reserved enum value or bit is **declared growth
space** — tolerating one is what keeps a future addition from failing at every
plugin built before it. A padding range is declared **not** growth space: a field
placed there changes the layout, which bumps `ver`, which the handshake rejects.
So a matched peer cannot legitimately produce a non-zero fixed padding byte, and
one that does disagrees with us about the record's shape rather than about one
field's meaning.

**`flags` and `reserved` stay two bytes** despite identical dispositions: the
name records *where a future flag goes*, which a reader of the layout should not
have to infer, and `bad_flags` versus `bad_reserved` distinguishes a peer that
invented a flag from one that wrote garbage into padding — different bugs with
different first suspects.

A padding range is not where a future field goes: anything needing those bytes
changes the record's layout, which bumps `ver`, which the `Hello`/`Ready`
handshake rejects for a peer that predates it — so a matched peer never sends a
non-zero one, and a peer that does is broken and worth catching. **Where a
payload document says "reserved" for a byte range, read "padding".**

An unknown enum value or flag bit is different: it changes only what a value
*means*, a receiver can carry on without knowing, and tolerating costs nothing.
That is defence in depth rather than a promise of cross-version compatibility,
which the handshake owns — but the alternative turns the first added modifier
into a session failure at every plugin built before it, which is not a trade
worth making for a check that catches nothing a matched peer can do.

**The required serialization covers the transport's own state, and is never held
across a consumer callback.** Lifecycle inputs and frame processing mutate the
same state and must be ordered against each other (`doorbell_E_input.md` §2) —
but holding that serialization across an emit is a deadlock waiting for an
ordinary sequence. A `PRESS` handler taking the once-per-gesture round trip `E` §3
explicitly permits would hold it across a gRPC RTT, while `tool_activated` — which
must run *before* the activation ack — blocks behind it on the gRPC thread the
host is synchronously waiting on. The grant path and `cancel_interaction` queue
behind the same lock, and `doorbell_M_memory.md` §7's missing-ack deadline fires
next.

**There is one total emission order, and it is the listener's.** A lifecycle
input mutates state *synchronously*, under the serialization, so it is in effect
before it returns — `tool_activated` must be (`doorbell_E_input.md` §2). But its
**outcomes are enqueued onto the listener's outbound sequence and drained
there**, never emitted on the calling thread. Both halves are required and
neither is optional:

- **Emitting a lifecycle outcome inline strands gestures.** `tool_retired` and
  `cancel_interaction` arrive on the gRPC thread, and their cancellations are a
  first-class outcome (`doorbell_implementation_details.md` §6). So the listener
  can process a `PRESS`, insert interaction 9, release the lock, and be
  *preempted before its emit runs* — at which point `tool_retired` purges 9 and
  emits "9 cancelled" first. The consumer drops a cancellation for a gesture it
  has not been told about, then opens 9, and the transport has already forgotten
  it: every later `DRAG` and the `RELEASE` are "unknown interaction, ignore" and
  **the consumer's gesture never terminates.** That is the stranded state §2's
  whole cancel discipline exists to prevent, reached through an ordinary
  deactivation. The `Geometry` cancellations of `E` §2 arrive the same
  way.
- **Holding the serialization across the emit deadlocks**, which is why the
  answer is a queue rather than a wider lock.

**The sequence is four steps, and three of them are load-bearing:**

1. **Mutate the state and append the resulting outcomes to the sequence, under
   the same serialization, as one step.** Appending *after* releasing the lock
   reproduces the failure above exactly — the listener mutates, releases,
   is preempted, `tool_retired` mutates and appends "9 cancelled", and the
   listener then appends "9 opened" behind it. The queue does not fix an
   ordering the enqueue itself got wrong.
2. **Release the serialization.**
3. **Wake the listener.** Any lifecycle input that produced an outcome MUST do
   this, and it is a liveness requirement rather than a latency one: a
   `tool_retired` cancellation is enqueued for a tool that will never send
   another record, so in an idle single-tool plugin **nothing else will ever
   drive the drain** and the consumer's handler sits mid-gesture forever. The
   wake endpoint §6 rule 3 already requires is the mechanism — this is its second
   consumer, alongside the stop.
4. **Drain, and run consumer callbacks — outside the serialization.**

**Outcome delivery is bounded**: an enqueued outcome is delivered on the next
drain, and step 3 guarantees a drain follows. Nothing waits on doorbell traffic
that may never come.

**And the drain is per FRAME, not per readiness edge.** A listener reads until
the socket would block, so draining only once per wake puts a cancellation
queued mid-burst behind every frame still in the queue. That is a delay rather
than the inversion above, and it is not what step 1 is defending — but the
consumer meanwhile sees a *non-opening record for an unknown interaction*, for a
gesture it has not been told was cancelled, which reads exactly like the loss
§6 makes visible and is not. Draining before each frame costs one uncontended
lock acquisition on the ~1 kHz path and makes emission order equal production
order.

"Emission order is preserved by the listener being one thread" is therefore a
statement about where outcomes are *drained*, not about where they are produced.

**A send-side fault joins the same ordered sequence, and "handed up" is defined
so that this is satisfiable.** It can be raised on any thread, or reentrantly
inside a listener callback, so:

> **Handing a fault up means atomically publishing one terminal outcome into the
> transport's own state — not completing a consumer callback.** The socket is
> released once the **fault sink** has accepted the report: a dedicated,
> non-blocking hand-off the supervisor drains to put `DoorbellFault` on the
> control plane. Release waits for that acceptance, never for a consumer
> callback and never for gRPC delivery, and the local deadline above is the
> escape hatch if even the sink is wedged.

Without that definition the two requirements cannot both hold: a fault must reach
the supervisor before the socket is released, and a stalled callback can delay
delivery indefinitely. Publication is the part the transport controls.

**The first terminal event latches.** Whether it is a fault or an escalation
(retry exhaustion, an abandoned edge), the transport closes to new traffic in
both directions at that moment and produces **exactly one** terminal outcome;
everything after it is refused, including a later fault.

**A fault stops both directions, before anyone is told.** Once any fault is
raised the doorbell sends and receives nothing further — not a diagnostic, not a
pending retry, not an in-flight `D`. §6 rule 7 already says this for a send-side
fault; it holds identically for a receive-side one, because the session is ending
either way and a record written into a channel whose framing is in doubt is a
record nobody can interpret. The report goes on the control plane (§12), never
here.

**The order is: close the channel to further traffic, then report, then release
the socket** — and the middle step must not be able to undo the first.

**"Synchronous" and "bounded" are only jointly satisfiable with a separate
timer.** A hand-up that blocks cannot bound itself, so the deadline below is
enforced from outside the call: it releases the socket while the consumer
callback may still be running. Saying only "hand it up synchronously, bounded by
a local deadline" leaves an implementer to drop the bound or make the hand-up
asynchronous, and neither is what is wanted. Three
consumer behaviours have to be survivable, because a specification that does not
name them gets a deadlock or a leak from each:

- **the consumer blocks** — the socket is already closed to traffic, so nothing
  is corrupted, but **the bound has to be local.** **A descriptor still inside
  the emit when that deadline fires is abandoned, not closed**: the consumer may
  already have claimed it, and closing a claimed descriptor is the double-close
  this section's ownership rule exists to prevent — in a threaded process that fd
  number is live again by then, so the second close can take out the doorbell
  socket or a gRPC connection. A leak is the lesser failure, and is reported as
  one. The grace window below cannot
  serve: it arrives on `DoorbellShutdown`, which the host cannot send until it
  receives the `DoorbellFault`, which cannot be sent until this hand-up returns.
  A peer bounds its own hand-up with its own deadline and releases the socket
  when that expires. **That deadline is SDK policy, not protocol** — nothing on
  the wire depends on its value, and no peer can observe it — but it must exist
  and be documented, because "no bound" is how a fault path becomes a hang;
- **the consumer raises** — the fault is still terminal; the socket is released
  anyway, on the way out;
- **the consumer tries to send** — refused, not queued. There is no state in
  which a doorbell send succeeds after its fault.

**Descriptor ownership across that boundary follows one rule: exactly one close,
and never by two parties.** A descriptor attached to a record that is discarded
for any reason — unknown op, fault, or a violation found before the record is
handed up — is closed by the transport and never surfaces. A descriptor on a record that *is* handed up is **claimable, and the transport
closes it if and only if it is unclaimed when the emit returns** — including when
the emit returns by raising. Both other readings are unsafe, so the choice cannot
be left open: closing unconditionally after the emit double-closes a descriptor
the consumer took, and in a threaded process that fd number is live again by then,
so the second close can take out the doorbell socket or a gRPC connection; handing
over and forgetting leaks it on every raise, and a leaked granted descriptor keeps
its segment alive — `doorbell_M_memory.md` §1's invisible allocation, by the route
no accounting sees. **A record-locally
rejected `M` is not handed up in a claimable state**: its descriptor is closed
before the rejection is returned, so error-path ownership never depends on a
caller remembering a convention. What must not exist is a path where
the transport closes a descriptor it has already handed over, or hands one over
and then forgets it on an error path.

**Teardown gets a bounded grace window.** The doorbell is already closed, so the
window runs over gRPC: the host sends a shutdown notice **with the reason**, the
peer may emit diagnostics and a final state report, and teardown happens at the
deadline regardless of cooperation. No capabilities are granted during it and
nothing host-side blocks on it.

---

## 9. Proportionate response to failure

> **Before hardening a mechanism against a failure, establish what else is
> necessarily true when that failure occurs. If the answer includes something
> worse, the hardening only runs in a state already handled.**

A send is refused only when the peer is not draining, which means one of:

| cause | what else is true |
|---|---|
| the peer's listener thread has died | deaf to everything on the doorbell, permanently |
| the peer's handler is slow (the SDK dispatches inline) | already behind on processing |
| the peer process is wedged | everything is broken |
| a burst above capacity | as above |

**For a disposable record, dropping is the correct outcome, not degradation.** A
peer that can process 25 records/sec should see 25 sampled from the stream, not a
backlog of stale ones.

Two consequences for op specifications:

- **Cheap detection, no expensive guarantees.** A bounded retry for a rare
  once-per-interaction record handles the transient stall. Building further buys
  only a stronger *transmission* guarantee — bytes accepted into the socket buffer
  — which is not delivery and cannot become it, since the only evidence of delivery
  is an acknowledgement and the doorbell has none (§1). Escalation is the better
  answer.
- **Degrade, never silently.** The drop is the visible symptom of the worse thing.
  A diagnostic names the likely cause — "peer not draining" points at the peer's
  handler, not at the socket.

**Exception: cross-session interference.** On a host whose sessions share a
reader, a flooding peer can starve a well-behaved one; there, nothing worse is
happening in the victim's session, it is simply a victim. That is why §11's
fairness rules are requirements rather than niceties. The reference host gives
each session its own listener thread, which is how it meets them.

---

## 10. Timing and backpressure

**A gRPC timeout means *unknown*, never *dead*.** Control-plane timeouts are sized
against a busy control plane — seconds, not RTT — and expiry triggers an idempotent
state query, not a teardown. The two liveness checks that exist (`Popen.poll()` and
`session_health`) both stay green for a peer whose doorbell listener thread has died
while gRPC keeps answering, so neither is evidence of doorbell health.

**Backpressure is queue depth on a bounded outbound queue**, not an errno inspected
at a call site:

| state | action |
|---|---|
| below the mark | normal |
| transiently full | the sending op's policy decides what is dropped and what is retried |
| sustained past a named threshold | gRPC fault, diagnostic, then `shutdown()` |

**Closed to traffic and released are different moments.** The host closes the
doorbell to further traffic when it decides to tear down, sends
`DoorbellShutdown` over the control plane — §8's "the doorbell is already
closed" — and calls `shutdown()` at the deadline, or on the plugin's
`DoorbellFinalReport` if that comes first, to release any sender still blocked
in it. A host implementer reading either sentence alone picks a different
point.

**What the release means to the plugin is settled by the host's next message.**
A plugin that consumed the `DoorbellShutdown` before the release has already
stopped its listener and does not observe the release; one that had not sees a
peer close. Either way the channel is gone and the session is ending: the host
follows the window with `Shutdown{session_fault}` (`control_plane_protocol.md`
§8.13), so a plugin **keeps its stream open and waits for it** rather than
ending the session on the close. Ending it first loses the report the window
exists to carry.

**The host's escalation path, stated in full**, since `DoorbellFault` is
plugin→host and the host has no counterpart. The host sends
**`DoorbellShutdown{reason, failure_class?, deadline_ms, detail}`**, opens the
grace window of §8, and calls `shutdown()` at the deadline:

| `reason` | when |
|---|---|
| `retry_exhausted` | a retried edge exhausted its budget |
| `peer_not_draining` | sustained fullness past the threshold |
| **`framing_fault`** | the host detected a framing fault in the peer's traffic (§8); `failure_class` carries which, from the same enum `DoorbellFault` uses |
| **`peer_fault`** | the *peer* reported a `DoorbellFault` and the host is answering it; `failure_class` echoes what the peer said |
| **`local_transport_fault`** | the host's **own** doorbell transport failed — `short_write`, `ambiguous_send`, or a receive error that is not peer-gone; `failure_class` carries which |
| `host_shutdown` | the session is ending for a reason outside this channel |
| **`peer_closed`** | the peer closed its doorbell and the host has heard no `DoorbellFault` for it yet; no `failure_class`. A faulting peer closes its socket and reports on the control plane, and the two need not arrive together, so the window opens for the report as for any closure; a `DoorbellFault` inside it is answered `peer_fault` and names the ending |

**`framing_fault` and `peer_fault` are what keep the two ends' accounts of one
event the same** — otherwise a plugin records a throttle where the host saw a bad
CRC, or an unrelated `host_shutdown` where the host was answering the plugin's
own `DoorbellFault`. They are the two an SDK actually meets. The host still never
sends a `DoorbellFault`: that op reports a peer's framing failure *to* the
supervisor, and the host is the supervisor — but its shutdown notice has to be
able to say what it saw.

**The threshold needs hysteresis.** A peer that resumes reading does not release the
sender promptly — it must drain a whole receive buffer first, measured at 1,159,680
bytes on Linux. Fire on sustained fullness over a window; clear on sustained drain.

**The numbers are host-side and named; the ones that exist are in
`control_plane_protocol.md` §9's table.** The writer's retry budget is
`RETRY_BUDGET` (8) -- not §9's
"retry budget before escalation", which is the ARRANGEMENT budget of 3; the
receiver's pending caps are `PENDING_HINT_SEGMENTS`,
`PENDING_REPORTS` and `OUTCOMES_PER_POLL` (256 each, §9, with what happens past
them). A fullness window and a drain window are still required to exist as
documented constants and have no defensible value yet — inventing one would be
worse than saying so (`plugin_principles.md` 8 asks for a clear bound, not a
confident guess). What *is* settled is the behaviour at the edges, which is what
an implementation needs first:

- **retry exhaustion** closes the doorbell (`DoorbellShutdown{retry_exhausted}`)
  and the session ends after the window (`control_plane_protocol.md` §8.13) —
  it is never silent and never unbounded;
- **sustained fullness past the threshold** goes to gRPC fault, diagnostic, then
  `shutdown()` (the table above);
- **a bounded queue that is full** drops disposable records and blocks nothing;
- **a second non-disposable edge arriving while one awaits retry** joins the
  queue behind it — order is what rule 8 protects, so it cannot overtake — and if
  the queue is full of edges, **the oldest pending edge is abandoned and its
  abandonment escalates**, exactly as retry exhaustion does. Dropping the *new*
  edge silently would break its reliability class; queuing without a bound would
  break §9's resource rule; escalating is the only answer that breaks neither.

Settling the values needs a measurement of how long a
real peer stalls under load, against the 278-record Linux and 32-record macOS
depths in §11. **None of it binds a plugin**, which sends only `D` and retries
nothing (§11a).

**The escape hatch must be built.** `shutdown()` on the socket releases a blocked
sender *(measured)* — with `EPIPE` on unix and `WSAESHUTDOWN` on Windows, which is
why §6 rule 6 gives the latter its own class: the released sender must not report
its own rescue as the peer dying. The rescuer needs a socket handle cached
**outside** anything a wedged sender may hold, and must never take a lock that
sender holds; §6 rule 1 is what makes that possible. The reference host's
listener hands out such a handle (`Listener::rescue_handle`), which reaches the
socket without touching the writer's lock.

---

## 11. Buffers and fairness

**Queue depth is sized by the highest-rate op that cannot coalesce** — today input
events at up to ~1 kHz.

| Platform | Depth at 112 B *(measured)* | Tolerance @1 kHz | What bounds it |
|---|---|---|---|
| Linux, default | 278 | 278 ms | sender's `SO_SNDBUF` (212992 B), at ~766 B per record |
| macOS, default | 32 | 32 ms | receiver's `SO_RCVBUF`, by bytes |
| macOS, `SO_RCVBUF` 64 KiB | 512 | 512 ms | as above |
| Windows, default | 1,171 | 1.17 s | sender's `SO_SNDBUF` (131072 B) |

**The last column is why these are not four instances of one number.** Linux's
278 does not move with record size — 12, 71 and 112 bytes all give 278 — but it
is not a count: it is the sender's byte budget divided by a per-record kernel
charge (~766 B, the `skb` overhead), which swamps a 112-byte payload. It moves
1:1 with the sender's `SO_SNDBUF` *(measured: 131072 → 171,
262144 → 342, capped by `wmem_max`)* and not at all with the receiver's.
macOS's is a byte budget against the *receiver's* buffer. Windows' is a byte
budget against the *sender's*. Extrapolating any of them to a new record size
using another platform's rule gives the wrong answer.

**Raise `SO_RCVBUF` on the plugin endpoint before inheritance on macOS**, whose
stock tolerance is below the 40 ms pump interval it must survive. On Linux the
host leaves `SO_SNDBUF` alone: the stock 212992 already buys 278 ms, and the
kernel doubles any explicit ask, so "asking for 64 KiB" *lowers* it to 131072
and depth to 171 *(measured)*.

The host guarantees this, so an SDK need not act. A plugin **may** raise it on
its own inherited fd anyway — the call is idempotent and harmless — and an
independent SDK wanting to be robust against an older host should.

**Anything a peer can observe is a MUST.** This document holds exactly two
`may`s, and neither is observable: raising `SO_RCVBUF` above what the host
already set, and coalescing outbound `D` (`doorbell_D_notification.md` §1)
change only how much slack or how many records one end uses for itself.

**Fairness.** A valid-record flood passes CRC, so checksum policy is not a resource
bound. The host bounds every peer-driven queue per session in both directions
(§10's constants), and isolates sessions from each other by construction: one
listener thread per session on level-triggered readiness (`poll` on unix,
`WSAPoll` on Windows), so a flooding session starves only itself. A host built
the other way — one reactor thread serving every session — has to add what
that shape gets for free: a per-session processing budget, and a way to
resume a session it stopped reading mid-flood, because on an edge-triggered
reactor **a quota that simply returns early is a stall** (unread data may
produce no further readiness edge). Level-triggered readiness is the simplest
answer, which is why the reference host chose it.

**No peer-controlled length exists anywhere in the host**, so every receive
buffer is the fixed 120 bytes of §7 rule 1.

---

## 11a. What binds a plugin

Most of this document is host machinery. An SDK author needs to know which parts
are theirs, because building the rest is wasted work against a channel that will
never ask for it.

**A plugin must:**

- frame, validate and fault identically in both directions (§2, §4, §7, §8) —
  One exception: **a plugin has no illegal inbound direction.** `M`, `E` and `D`
  are all legal host→plugin, so §7.5 step 4's direction check and §13's
  illegal-direction case are host-side work; a plugin writing them writes dead
  code;
- send one record per call, non-blocking, asserting `n == 112` (§6 rules 2–5);
- drop and count unknown ops (§5), and never desynchronise on one;
- receive `M` and `E`, and act on them per their documents;
- **receive `D`** as well as send it — the op is symmetric and the host may hint
  at a Pulsed region the plugin reads (`doorbell_D_notification.md` §5);
- send `D`, which is disposable: **never retried, never queued, dropped on
  refusal**;
- **start its listener before signalling readiness.** Session-setup grants ride
  setup and macOS's stock queue is 32 records (§11), so a plugin that publishes
  its port or answers `Hello` before it is draining can lose the grants that
  arrive next;
- **acknowledge each pane's epoch before expecting any `E` for it**, including
  the first (`doorbell_E_input.md` §1a) — a plugin that subscribes and then waits
  for input without acknowledging waits forever, and the host's deadline is what
  eventually reports it;
- report a `DoorbellFault` over gRPC before closing, while the control plane is
  still usable (§12).

**A plugin never escalates on its own send refusals.** `D` is the only thing it
sends and `D` is disposable: refused means dropped and counted, however long it
persists. Escalation against a peer that is not draining is §10's ladder, and it
belongs to the host — the end that serves every session, one process for all
of them, and therefore the end with something to protect.

**A plugin must not build**: the retry budget (§6 rule 8 — nothing it sends is
retried), backpressure hysteresis (§10), the **cross-session rescue** described
in `doorbell_principles.md` E3, or per-session fairness scheduling (§11). Those
exist because the host serves every session — many peers in one process, each
on its own listener thread — and a plugin serves one session and has nothing to
be fair between.

That is not a ban on `shutdown()` — but it is **not the listener's exit path
either**, for §6 rule 3's reason: stopping the listener is an operation the
transport publishes, not one a consumer performs by closing the socket behind it.
A plugin ends its listener through the wake endpoint and its thread-safe stop;
`shutdown()` releases a *blocked sender*, which is a different thing on a
different call.

What a plugin does not build is the machinery E3 is about: a socket handle cached
outside a sessions map so a *rescuer* can unwedge a **different** session's
blocked sender.

---

## 12. Diagnostics

The first integrity failure creates a durable, structured Plugin Manager
diagnostic, not an stderr line. It must retain enough to decide whether the **peer**
was wrong or the **transport assumption** failed:

plugin id, declared version, SDK version, manifest source; host and protocol
versions; OS, transport class, direction, session id; **record ordinal** — how many frames
this receiver has examined on this session in this direction, counting every
frame including the failing one, so two diagnostics from one session can be
ordered against each other; failure class and raw OS error; expected and
observed CRC; datagram length and `MSG_CTRUNC` state; parsed header
fields; the complete 112-byte offending record; socket state (`SO_TYPE`, buffer
sizes, blocking configuration); whether the process and gRPC stream were healthy at
detection; the termination action taken.

Every fault is terminal, so there are no repeated failures to aggregate: one
diagnostic, carrying the record that caused it.

**The doorbell closes after the report has been handed to the control plane, not
before** — that ordering is the whole point of reporting on the other channel, and
a transport layer that closes first has nothing to report with. The sequence a
transport owes: stop reading, close any attached descriptors, hand the fault up
synchronously, then close the socket. Whether the report has been *delivered* is
not waited on; gRPC is a failure detector, not an acknowledgement (§10).

The plugin reports a **`DoorbellFault`** over gRPC before closing, while the
control plane remains usable. It carries the subset of the list above that the
plugin can supply:

**The class set is CLOSED, and that is what makes the field safe to send.** It
is one value per fault in §8, so the mapping from a raised fault to a wire value
is **total by construction**: an implementation never has a class it cannot
name, and there is therefore no catch-all and no reason to want one. Two rules
depend on this and are jointly satisfiable only while it holds — enum zero is a
protocol error by the IDL's convention, and `control_plane_protocol.md` §7
forbids dropping a fault report. A peer holding a class outside §8's list has
**invented a fault this protocol does not define**; the defect is there and not
in the enum, and a catch-all would only make it unfindable while destroying the
per-class aggregation §12 keys on. So an implementation checks the spelling
where the fault is *raised*, not where it is encoded — by then the only choices
left are sending something invalid or violating §7.

**Being total does not mean carrying all thirteen.** `illegal_direction` is
host-only: a plugin has no illegal inbound direction, so a plugin-side
implementation is total over twelve, and the missing one is a direction that
cannot arise rather than a case unhandled.

```
DoorbellFault {
  failure_class          // stable enum, one per fault in §8:
                         //   bad_crc,
                         //   bad_length      (any datagram that is not exactly
                         //                    112 bytes, over or under)
                         //   bad_ver, bad_flags, bad_reserved, bad_padding,
                         //   ctrunc, wrong_fd_count,
                         //   reserved_op, illegal_direction,
                         //   short_write, ambiguous_send, recv_error
                         // (recv_error: an OS receive error that is not
                         //  peer-gone; peer-gone is teardown, never a fault)
  capture                // VARIABLE length: whatever bytes were held when the
                         // fault was raised — a whole 112-byte record, the
                         // truncated head of an oversized datagram, or a partial
                         // stream window. A fixed 112 cannot carry the last two,
                         // which §8 requires be retained.
  observed_len           // bytes the transport actually read.  A value > 112 is
                         // itself the oversize signal (§7 rule 1) -- there is no
                         // separate flag, because "the datagram exceeded the
                         // buffer" is not provable from a full read and
                         // "it exceeded 112" already is.
  msg_ctrunc             // as reported, not inferred from lengths
  ordinal                // the record ordinal above, so two diagnostics from one
                         // session can be ordered.  ABSENT for short_write and
                         // ambiguous_send: it counts frames EXAMINED, which a
                         // send fault has not done
  os_error               // raw errno / WSA code, 0 if none
  expected_crc, observed_crc
  transport_class, direction
  detail                 // free text for the Plugin Manager
}
```

**`observed_len` is the count the receive returned, and nothing else is needed to
classify it.** Rule 1's buffer is larger than a record, so a return above 112 is
itself the oversize signal. A separate flag would claim the
datagram exceeded the *buffer*, which a full read cannot establish — a 120-byte
datagram read into a 120-byte buffer returns 120 and is not longer than that. The
provable statement is `observed_len > 112`, which the field already carries.

The exact length of a datagram we are about to fault on is not worth a
portability hazard to recover. It is available on Linux only, and buying it costs
the `MSG_TRUNC` input flag — which Darwin echoes back into `msg_flags`, so a
receiver that passes it uniformly reports truncation on every record.

**Which fields are present depends on the failure class, and every absence is
representable rather than zero-filled** (`plugin_principles.md` 11). Two
implementations filling plausible zeroes differently is the failure this set
would otherwise invite, and "absent for send-side faults" is not enough of a
rule — a receive error has no frame either.

| class | `capture` | `observed_len` | `ordinal` | CRCs | `msg_ctrunc` | `os_error` |
|---|---|---|---|---|---|---|
| `bad_crc` | yes | yes | yes | **yes** | yes | — |
| `bad_length` | yes, what was read | yes | yes | only if ≥ 112 bytes were read | yes | — |
| `bad_ver`/`bad_flags`/`bad_reserved`/`bad_padding` | yes | yes | yes | yes | yes | — |
| `ctrunc`/`wrong_fd_count` | yes | yes | yes | **only if ≥ 112 bytes were read** | yes | — |
| `reserved_op`/`illegal_direction` | yes | yes | yes | yes | yes | — |
| `recv_error` | **—** | **—** | yes | **—** | **—** | **yes** |
| `short_write` | — | bytes the send accepted | **—** | — | — | yes |
| `ambiguous_send` | — | — | **—** | — | — | yes |

`ordinal` is absent on the two send-side rows for the same reason the rest is:
it counts frames *examined*, which a send fault has not done. The CRC pair is absent wherever fewer than
112 bytes exist to checksum — a two-byte datagram has no `observed_crc` to
report, and reporting 0 would claim one.

**Reach for it through the right call.** This channel carries `SCM_RIGHTS` on the
same socket, so **every receive on a unix message transport** must be from the
`recvmsg` family — plain `recv` takes no ancillary data. **This does not extend
to the Windows stream**, which carries no descriptors (§7 rule 3's fd-count table
is 0 for every op there) and where `recvmsg` does not exist in the reference
environment at all — Python documents it as unix-only. An ordinary `recv` /
`recv_into` is correct there, and requiring otherwise would specify a call the
platform does not have.

**Never treat bytes past the buffer as present.** Only `capture`'s first
`observed_len` bytes are real.

**Teardown has its own two messages**, and neither is a fault: host→plugin
`DoorbellShutdown{reason, failure_class?, deadline_ms, detail}` — §10 defines the
shape and its reasons; this is the same message — opens the bounded grace window
of §8. `deadline_ms` is a **duration in milliseconds, measured from the host's send**,
never an absolute time — two processes agreeing on a clock is a problem this
exchange has no reason to acquire. **At the receiver it is an upper bound, not a
budget**: the host starts its timer when it sends, and tears down at the deadline
whether or not the message ever arrived, so a message delayed in transit promises
time the plugin does not have. Measuring from *receipt* would require the host to
know when receipt happened, which is precisely what it has no acknowledgement
for. And
plugin→host `DoorbellFinalReport{detail}` is the diagnostic the peer may emit
inside it. **The deadline is on the message** rather than being a constant each
side guesses at: the peer does not enforce it, but a bound nobody states is a
bound nobody can respect (`plugin_principles.md` 8). Teardown happens at the
deadline whether or not either message arrives. The proto's
existing empty `Shutdown` is the *session* shutdown and is a different thing. Direction is diagnostic: malformed plugin→host traffic
from an independent SDK points at that SDK; malformed host→reference-SDK traffic
points at vizi or at the platform assumption, and is a release-level concern.

---

## 13. Conformance

The audience is an SDK implementer; ordinary plugin authors get typed SDK
operations that cannot construct an invalid frame. Zero padding makes a record a
pure function of its fields, which is what makes byte-exact cross-language vectors
possible.

- the CRC check value, before any framing;
- arbitrary stream chunking reconstructs 112-byte frames;
- one send per record on message transports; a split send rejected as two malformed
  records;
- a single bad CRC is an immediate fault on every transport;
- a short or failed stream send closes the doorbell with no later write;
- undersized and oversized datagrams, both `bad_length` from the read's own
  length; two queued records are never returned by one read; ancillary-fd count
  mismatches;
- an unknown `op` is skipped without desynchronising, and a valid record after it
  is still framed; its descriptors are closed;
- a defined op in an illegal direction faults and closes any descriptor first
  **(host only)**;
- fairness under one flooding session while another stays responsive **(host
  only)**.

**Only two of these are host-side: fairness, and the illegal-direction case** —
the first because a plugin serves one session and has nothing to be fair between,
the second because every op is legal inbound to a plugin (§11a). Everything else
is a plugin's too. In particular a Windows plugin **receives on a stream**, so
a plugin **sends `D` on that same stream**, where §3's table guarantees no send
atomicity — so the short-write case is a plugin's, on exactly the platform that
makes it most likely.

**Byte-exact vectors are deferred.** The numeric constants in the op
documents — `phase`, `payload_kind`, `target_kind`, `mods`, `perms`, `D`'s bit
order and every field origin — are what make a byte-exact vector *derivable*,
and they are normative.

---

## 14. Rejected alternatives

| Alternative | Why not |
|---|---|
| Length-prefixed records | Puts a peer-chosen length in a host read; forces ~64 KiB receive buffers to dodge truncation |
| 128-byte records | Costs macOS an eighth of its stock queue depth (32→28) for headroom nothing uses |
| CRC-32C | Not in Python's stdlib, and the reference SDK is Python; hardware acceleration is worth nanoseconds against a syscall |
| Clearing CRC bit 31 as a `0xFFFFFFFF` sentinel | A record whose checksum lands there becomes permanently unsendable — the CRC is a pure function of the bytes and a retry regenerates them |
| Marker scanning and resync in baseline | Only a misimplemented peer produces desync; fail-stop plus a gRPC fault is simpler and reports better. Syntax stays reserved |
| A recent-ids dedup filter on `id` | Cannot be made sound: eviction tracks records *received* while the id space advances with records *sent* |
| A doorbell acknowledgement layer | Grants ack over gRPC, where an ack can mean *installed*; nothing else on the doorbell needs one |
| Duplicating a record on gRPC as a reliable copy | Dual-path delivery reintroduces ordering and duplicate ambiguity |
| `SO_SNDTIMEO` on Windows | A timed-out send may already have delivered, which under fail-stop is terminal |
| `write_all` | Loops on a short return, converting a detectable fault into a torn record |
| Named pipes on Windows | Message mode would unify framing, but forces Win32 handles and `SetNamedPipeHandleState` into every SDK |
| `AF_UNIX` on Windows | Stream-only there, so it changes the security model and not the framing |
