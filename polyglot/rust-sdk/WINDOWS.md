# Windows transport

The Windows backend implements doorbell v2's loopback TCP transport. It shares
the Unix implementation's frame validation, record diagnostics, input sequencing,
lifecycle API, ordered outcomes, terminal mailbox, and stop behavior.

## Launch and coordination

```rust,ignore
let (doorbell, listener) = Doorbell::from_env(Config::default())?;
let worker = std::thread::spawn(move || listener.run(handle_outcome));

// The supervisor binds gRPC on 127.0.0.1:0 and atomically publishes its port
// via temporary-file + rename to VIZI_PLUGIN_PORT_FILE after starting worker.
// Complete the real Hello/Ready exchange, then:
doorbell.handshake_completed_with_token(
    hello.protocol_major, hello.protocol_minor, &hello.session_token,
)?;
```

Unlike Unix environment adoption, the Windows constructor is safe: it creates
its socket rather than claiming ownership of an environment-supplied fd. It
reads `VIZI_PLUGIN_DOORBELL_ADDR` and `VIZI_PLUGIN_TOKEN`, requires the documented
`127.0.0.1:<nonzero port>` spelling and 32 ASCII hex characters, and sends those
characters unchanged as the first plugin-to-host bytes. It doesn't perform DNS
resolution or accept a remote address.

The default **five-second total launch deadline** covers connection and all token
writes. `Doorbell::connect(address, token, config, timeout)` overrides it. Zero or
unrepresentable timeouts fail setup. Interrupted/partial token writes consume the
same deadline; a zero write, I/O error, or expiration closes the setup socket.
The token's temporary write timeout is removed before nonblocking record I/O.

Sending the token proves only that those bytes were accepted by the socket.
The host checks it; no token acknowledgement is invented on the doorbell. The
host-to-plugin stream starts with records, never a token. The later
`Hello.session_token` is checked byte-for-byte, in constant time, by
`handshake_completed_with_token`; a mismatch closes the doorbell and returns an
error without logging the token.

`control::Runtime` authenticates each control stream before it can become the
session, so a stray local connection never reaches the doorbell's check. A
`1.0` host sends the token in `Hello.session_token`, and may also send it as
gRPC call metadata (`vizi-session-token`,
`control::SESSION_TOKEN_METADATA_KEY`); the reference host sends it in `Hello`
only. A stream carrying the metadata key is checked by it before it is
accepted, and a wrong token there is refused; a stream without the key is
accepted when its `Hello.session_token` matches. Both compares are
constant-time. A
stream with a missing or wrong token is refused `UNAUTHENTICATED` and never
ends the session: the runtime keeps accepting streams until the host's arrives.
`Runtime::serve_tcp` serves an already bound loopback listener, and
`Runtime::require_session_token` sets the token for a runtime whose doorbell
has none. Omitting the token leaves outbound records
gated. The two-argument `handshake_completed` cannot open a Windows
writer. A protocol major of `1` is required; minor versions are compatible.

The supervisor owns gRPC binding, atomic port-file publication, `Hello`/`Ready`,
and failure reporting. The full SDK `control::Runtime` performs these duties; the transport alone does not.
Start its listener before publishing readiness, and acknowledge tool/epoch
arrangements only after the corresponding lifecycle call succeeds.

## Records and capabilities

The receiver retains at most 111 partial bytes and requests only the remaining
bytes of the current 112-byte window. Thus split reads and coalesced records
follow the same validation path as Unix packets. EOF with a partial window
produces teardown with those bytes in `PeerGone.capture`; it does not manufacture
a malformed frame or advance the record ordinal. A CRC failure on a complete
window is immediately terminal.

`M` carries **zero descriptors**. A valid grant emits:

```rust,ignore
DecodedRecord::NamedMemory { grant, name }
```

`name` is validated for byte length 1–80, UTF-8, no embedded NUL, the exact NUL
terminator, and zero tail. Invalid names use a record-local `malformed` grant
rejection; the socket continues. Fixed padding violations remain framing faults.
The receiver does not enforce the host's example `Local\vizi-<hex>` pattern.
On Unix the field is ignored with local diagnostics.

The memory layer must open the named section, check its actual size and access,
validate the preamble and identity proof, and join the grant with `InstallSegment`
before acknowledging installation. The transport conveys the capability. The SDK memory layer opens existing
sections, maps them with the granted access, and verifies their preamble.
Windows exposes the mapped extent at page granularity, so sub-page differences
in section size cannot be distinguished. In particular, do not report
installation merely because a Windows name decoded successfully.

## Sending, polling, and stopping

Each notification makes one nonblocking write. A short return ends the session
without appending the remainder. `WSAEWOULDBLOCK` and `WSAENOBUFS` drop and count
the advisory record. Peer-close errors cause teardown; `WSAESHUTDOWN` is separately
reported as local closure. Other send errors fault. Successful writes still do
not establish delivery. These distinctions follow the specified classifier and
the [Winsock send contract](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-send).

The backend sets `TCP_NODELAY`, nonblocking mode, and non-inheritance explicitly.
It uses a connected loopback TCP pair as the listener's wake endpoint, because
[WSAPoll](https://learn.microsoft.com/en-us/windows/win32/api/winsock2/nf-winsock2-wsapoll)
waits on sockets. `stop()` closes admission, publishes the terminal report,
releases the doorbell, and wakes that listener without waiting for a callback.
The supervisor's `wait_terminal` also wakes independently of callback delivery.

## Validation

Linux runs the shared TCP tests for every chunk size 1–224, would-block and
interrupted reads, partial EOF, token parsing, partial/interrupted token writes,
deadline expiration, actual loopback authentication, and wake-pair traffic.
Windows section-name checks are also exercised on Linux.

Windows-specific tests cover the actual Winsock listener, token/Hello gating,
fragmented/coalesced traffic, section-name outcomes and rejections, EOF capture,
framing faults, input lifecycle, concurrent sends, backpressure, idle and blocked
callback stop, and a separately executed plugin using the launch environment.
Rare send-error branches are injected into the production classifier.

Windows code can be checked from Linux with the `x86_64-pc-windows-gnu`
target.
