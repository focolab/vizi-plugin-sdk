# Control plane protocol

> **Status: this is protocol `1.0`.** Protocol `1.0` hosts are vizi v0.9.0 and
> later. `proto/vizi_plugin.proto` is the IDL and `vizi_plugin_sdk/` is the
> reference plugin end; the Go, Rust and Java SDKs under `polyglot/` implement it
> independently. Where the reference host departs from a rule here,
> `control_plane_implementation_details.md` §2b lists it rather than this
> document asserting it. Read a claim about "the host" as a requirement, not an
> observation, unless it cites a file.

The plugin platform's reliable channel: session lifecycle, versioning, the
message classes and what each one costs, arrangement and acknowledgement, and
the messages the other two layers assume.

**This document specifies behaviour, not encoding.** The `.proto` is the wire
contract and every language's protobuf toolchain implements it from that file;
what no generator supplies is who may send what and when, what a reply means,
what happens when a queue fills, and what a timeout is evidence of. Two
implementations can both satisfy the IDL and still fail to interoperate, and
everything in this document is in that gap.

| Content | Where |
|---|---|
| Encoding, field numbers, append-safety | `proto/vizi_plugin.proto` |
| Why the channel is shaped this way | `control_plane_principles.md` |
| What ships, and how it differs | `control_plane_implementation_details.md` |
| The fast path | `doorbell_protocol.md` + its three op documents |
| What granted memory contains | `shared_memory_protocol.md` |

---

## 1. Role

The control plane is the **authoritative session**: the reliable, ordered,
schema-evolving channel that every other mechanism in the platform reports to
and recovers through. Its share of the three-channel split is four things:

- **Meaning.** A granted segment is bytes until `InstallSegment` says what its
  regions are; a coordinate is a number until an epoch says how to read it.
- **Arrangement.** Everything the doorbell is permitted to *reference* — segment
  ids, region indices, tool handles, pane epochs, open generations — is
  allocated and given meaning here (`control_plane_principles.md` F3).
- **Reconciliation.** Acknowledgements, idempotent state queries, and the answers
  to them. gRPC's contribution is that it produces delivery or a connection
  error, **never silence**, which is what lets the doorbell be fail-stop.
- **Diagnosis.** Faults, contract violations and the structured records behind
  what the Plugin Manager shows.

Three rules follow, and they are the reciprocals of `doorbell_protocol.md` §1's:

1. **Nothing interactive waits on this channel.** No press, release, drag or
   live update may depend on a message here, and batching does not soften it —
   a drag event batched is a drag event delayed. The reason is not only latency
   (§9) but head-of-line blocking: one stream, dispatched in order at both ends.
2. **This channel never carries a capability.** `SCM_RIGHTS` cannot travel here.
   A message naming a segment is a statement *about* memory granted on the
   doorbell, and is meaningless until that grant lands (§8.12).
3. **Nothing is delivered on two channels.** No message here duplicates a
   doorbell record, and no doorbell record duplicates a message here. Dual-path
   delivery reintroduces the ordering and duplicate ambiguity both channels are
   built to avoid.

**And bulk never crosses it.** A reply carries a *descriptor* — which segment,
what offset, what shape, what dtype — and the peer maps the bytes. Serialising a
volume here would copy it out of the buffer the data plane exists to avoid, and
the cost of that copy at volume scale is the whole reason for the split.

**The rule is a ceiling, not an absolute.** Two arms carry bytes inline —
`ExportChunk.inline` and `GeomBuffer`'s inline form — and their comments call
them small-payload and test paths, which is a *convention*, not a wire rule a
second implementation can obey. The rule that can be obeyed is a number:

> **An inline byte field is legal only below a stated ceiling, and a message
> exceeding it is refused rather than truncated or accepted.** "Small payloads
> only" is unenforceable, and a peer that reads it as permission will send a
> volume.

**That ceiling is 8 MiB, inside a 32 MiB message** (§9). Both clear the one
inline path that is genuinely production — `GeomBuffer.inline`, which
`set_items_live` requires — several times over. The message ceiling is the
host's `MAX_DECODE_BYTES`, enforced: a
message over it is refused at its length prefix and the session faults with the
size (§10). A bound is required here: §10's "nothing a plugin sends may crash
or block the host" cannot hold on a host that serves every session from shared
machinery and decodes messages of any size.

---

## 2. Transport and roles

**The plugin is the gRPC server; the host connects as the client.** One
bidirectional stream — `rpc Session(stream HostMsg) returns (stream PluginMsg)`
— carries the entire session, and **the stream's lifetime is the session**: when
it ends, every reader handle, export handle, panel, overlay and grant opened on
it is gone.

| Platform | Endpoint | Authentication |
|---|---|---|
| Linux, macOS | `AF_UNIX` stream at `VIZI_PLUGIN_UDS` | filesystem permissions on the socket path |
| Windows | loopback TCP, ephemeral port published to `VIZI_PLUGIN_PORT_FILE` | 128-bit token in `Hello.session_token` |

The asymmetry with the doorbell is deliberate and is
`control_plane_principles.md` C1: the host connects to a process it started, so
a dead plugin is a closed stream rather than a state to diagnose.

### 2.1 Launch environment

The host passes both channels through the environment. `doorbell_protocol.md`
§3.1 owns the doorbell variables; the control plane's are:

| Variable | Platform | Meaning |
|---|---|---|
| `VIZI_PLUGIN_UDS` | unix | path the plugin binds its gRPC server on |
| `VIZI_PLUGIN_PORT_FILE` | Windows | where the plugin publishes its bound port |
| `VIZI_PLUGIN_TOKEN` | Windows | 32 ASCII hex characters, 128 bits |
| `VIZI_PLUGIN_ASSETS` | all | JSON object, `{asset_id: absolute path}` — see below |
| `VIZI_PLUGIN_SETTINGS_DIR` | all | absolute path of the plugin's settings folder, which may not exist yet — §2.3 |

**`VIZI_PLUGIN_ASSETS` is a mapping, not a directory**, and the distinction is
the kind that fails silently: it holds one entry per manifest-declared asset the
host verified and published, keyed by asset id, valued with an absolute path.
There is no store root in the environment and no path a plugin may derive one
from — `AssetStore.plugin_root` exists but is never exported. An SDK that reads
this as a directory and joins the asset id onto it finds nothing, for every
asset, with no error to report, because the value parses perfectly well as a
path. An asset absent from the mapping is **not declared for this plugin**,
which is a different condition from declared-but-missing and gets a different
diagnostic.

This variable carries *manifest-declared* assets only. §8.9's `RequestAsset`
slots are a separate runtime mechanism and do not appear here.

**Readiness is the endpoint's appearance**, and it MUST NOT be observable
half-ready: on unix that is the `bind` + `listen`, on Windows the port file,
which is written to a temporary path and renamed over the target so the host
never reads a partial one.

**The host's channel sends `:authority = localhost`**, on both transports — a
fixed value, since the connector dials the socket path or the port and reads
nothing from the URI — and a plugin's server MUST accept it and SHOULD read
nothing from it. It is stated because HTTP/2 stacks disagree on what a legal
authority is: a client that derives one from a Unix socket path sends the
path percent-encoded, which the `http` crate rejects and grpc-go, grpc-java
and grpcio accept, and an SDK tested only against such a client would find
the real host easier than its test harness.

**The doorbell listener starts before readiness is signalled.** Session-setup
grants ride the handshake — §3.1 forbids any record before it completes, and the
host issues them as soon as it may — and between the readiness signal and that
moment the plugin does nothing the host waits on. So readiness is the last
instant the plugin controls, macOS's stock doorbell queue is 32 records, and a
plugin that signals before it is draining can lose the grants that arrive next
(`doorbell_protocol.md` §11a). This is a control-plane ordering rule because the
signal is a control-plane artifact.

Starting the listener at readiness rather than at `Ready` is deliberately
earlier than the rule requires: it costs a thread that briefly has nothing to
read, and it removes the window entirely rather than making the plugin reason
about how wide it is.

**A setup failure is a launch failure**, not a session fault: there is no
session to tear down and no channel to report on, so the plugin exits with a
diagnostic on stderr, which the host captures and retains.

### 2.2 The Windows token

The host generates a per-session 128-bit token, passes it in the environment,
and repeats it in `Hello.session_token`. **A plugin whose `Hello` carries a
different token drops the session** — the endpoint is loopback TCP and reachable
by any local process, so the token is what makes "the host connected" mean
something. On unix the field is empty and the socket path's permissions carry
the same weight.

**A token mismatch here ends the SESSION; it is not a launch failure**, and the
distinction is §2.1's. By the time `Hello` arrives the plugin has connected its
doorbell, written its own copy of the token, bound gRPC and published its port —
the doorbell is open and this stream is open, so there is something to tear down
and a channel to say so on. The launch-failure path covers what happens *before*
that: a missing environment variable, a socket of the wrong type, a token the
host rejects on the doorbell's own connect. Getting this backwards means either
a plugin exiting to stderr with a live session behind it, or a host waiting on a
readiness signal that already arrived.

The token authenticates the *connection*. It is not a capability and confers
nothing on the messages that follow. Same-user interception is out of scope: a
host running hostile same-user processes has larger problems than this channel.
Constant-time comparison is therefore not required — the reference SDK does it
anyway, which costs nothing and is the right habit, but a specification that
demanded it while declaring the threat out of scope would be incoherent.

---

### 2.3 Where a plugin keeps what it remembers

The host provides two folders, told apart by how long what is in them should
live. Both are **conventions, not confinement**: a plugin runs with the
user's own access and can write anywhere. They are what
vizi provides, and what Plugin Manager can point a person at.

| Folder | Lives as long as | Where | Reaches the plugin as |
|---|---|---|---|
| settings | the plugin is installed | `settings/` beside the manifest | `VIZI_PLUGIN_SETTINGS_DIR` |
| state about one file | the file | `plugins/<id>/` in the file's companion directory | `file_state_dir` |

**Settings go beside the manifest because that is what deleting a plugin
deletes.** There is no uninstall step to hang a cleanup on — a plugin is removed
by deleting its folder — so a settings folder anywhere else outlives it. When
the plugin's folder is not writable, the host uses a folder under its own data
directory instead and writes a `plugin-id.txt` in it naming the plugin, so a
leftover can be traced to its owner. **Beside the manifest, the host names the
folder and does not create it**; it checks that a file can be created there
(by creating one — a permission-bits check misses an access-control list),
and an SDK creates the folder on first use, so a plugin that keeps no
settings leaves no empty folder in its source tree. The fallback folder is
created, with its `plugin-id.txt`. When neither is usable the variable is
absent and the plugin has no settings folder, which an SDK reports rather
than inventing one.

**State about one file goes where the host keeps the file's region saves**: the
companion directory, which follows the user's output-location preference
(beside the file, pooled under vizi's data directory, or a chosen root) and
falls back when beside-the-file is not writable. A cache, a computed result, a
per-file choice: it moves with the dataset and is deleted with it. The host
**names this folder and does not create it** — a plugin creates it when it
first writes — so opening a file leaves nothing behind for plugins that store
nothing. It is empty when the dataset has no local path: a remote or
plugin-specific URI, a demo dataset.

`file_state_dir` travels with whatever names the file, since the file changes
within a session and the environment cannot follow it:

- `ViewState.file_state_dir` — the open dataset (§8.7);
- `OpenReader.file_state_dir` — the file being opened (§8.2);
- `OpenExport.file_state_dir` — the export's SOURCE, not its `locator` (§8.6).

`<id>` is a folder name derived from the manifest id, **the same on every OS**,
so one id names one folder everywhere. **The id is used as it is** when all of
these hold:

- it is a letter or digit, then letters, digits, `.`, `_`, `-`;
- it does not end in `.` (Windows drops a trailing dot, so `abc.` and `abc`
  would share a folder);
- it has no upper-case letter (Windows and macOS compare names without case,
  so `Org.X` and `org.x` would share a folder; a derived name's hash is of the
  id as written, which keeps them apart);
- the part before its first `.` is not a Windows device name: `con`, `prn`,
  `aux`, `nul`, `com0`–`com9` or `lpt0`–`lpt9`, in any case. Windows reserves
  a device name with any extension, so `aux.plugin` is as unusable as `aux`.

**Otherwise it is derived, in exactly these steps**, since an independent
implementation must name the same folder:

1. replace each maximal run of characters outside `A–Z a–z 0–9 . _ -` with
   one `_`;
2. trim `.` and `_` from both ends;
3. keep the first 64 characters;
4. if nothing is left, use `plugin`;
5. if the part before the first `.` is a device name (as above), prefix `_`;
6. append `-` and the first 8 lowercase hex digits of the SHA-256 of the id's
   UTF-8 bytes.

No derived name leaves `plugins/`, ends in `.`, or begins with a device name.
Test vectors:

| id | folder |
|---|---|
| `org.example.plug` | `org.example.plug` |
| `Org.Example` | `Org.Example-353f0972` |
| `con` | `_con-1143da2b` |
| `NUL` | `_NUL-ae6de182` |
| `aux.plugin` | `_aux.plugin-6e31b3b9` |
| `Lpt9.cfg` | `_Lpt9.cfg-50f37a5c` |
| `abc.` | `abc-5ac9481b` |
| `a/b` | `a_b-c14cddc0` |
| `../../etc` | `etc-74ccf3c5` |
| `..` | `plugin-5ec1f7e7` |
| ` spaced id ` (leading and trailing space) | `spaced_id-6d01a897` |
| `é` | `plugin-4a99557e` |

## 3. Session lifecycle

### 3.1 The handshake

1. The plugin binds its endpoint and starts its doorbell listener (§2.1).
2. The plugin signals readiness (the bind, or the port file).
3. The host connects and opens the `Session` stream.
4. **The host sends `Hello{protocol_major, protocol_minor, session_token}`
   first**, before anything else, with `id` = `Hello from <version string>`.
   **It does not wait for the plugin's response headers to do so.**
5. **The plugin's first message is `Ready{protocol_major, protocol_minor,
   plugin_id}`**, unsolicited, with `id` = `Ready; I am <version string>`.
6. Either end rejects a mismatch — of version, or of the identity string's form
   — and fails the session, with a diagnostic naming **both** versions, or
   both strings.

**`Hello` and `Ready` may cross.** "Unsolicited" means the plugin need not wait
for `Hello` before sending `Ready`: it may send `Ready` as soon as the call
opens, or read `Hello` first and check it. The host accepts either order. What
the plugin may not do is **stop reading once it has sent `Ready`.** The host
treats a `Ready` as a live session and may send its first request at once, and
it cannot know whether the plugin has read `Hello` yet, so **from `Ready` on the
plugin reads the stream and handles what arrives** — `Hello` included, whose
checks still fail the session on a mismatch whenever it is read.

**Why the host must not wait for headers.** A gRPC server orders a response
(headers, messages, trailers) but is free to time it, and grpc-java, grpc-go and
tonic all send their headers with the first message. A plugin that reads
`Hello` before sending `Ready` sends nothing until `Hello` arrives, so a host
that waits for headers before sending `Hello` waits forever, and so does the
plugin. **A plugin SHOULD still send its response headers when the call
opens**, so that a host which does wait is not stranded, and so that any host
can tell a plugin whose handler is running from one that never answered the
call. vizi shows that difference while a plugin starts: *connecting*
until the headers arrive, *waiting for Ready* after.

**The identity string.** The handshake's two `id`s are not correlation ids:
each is the sending end's canonical build identity, so that a session log
names exactly which two builds spoke — the reason this exists is
reproducibility, and it costs one string per session.

- The `id` is the fixed prefix — exactly `Hello from ` on the host's `Hello`,
  exactly `Ready; I am ` on the plugin's `Ready` — followed by the **version
  string**: everything after the prefix, **non-empty**, free-form, chosen by
  that end and stable for a build. A title, a version and the largest
  reasonable hash in hex all fit: the whole `id` is at most **2048 bytes** of
  UTF-8. It contains **no control character** (Unicode category Cc:
  U+0000–U+001F, U+007F–U+009F), because both strings are written to logs and
  a newline in one would forge a log line.
- **The receiver checks the FORM and nothing else.** It never matches the
  string against anything, never parses the version string, and never
  compares it to the manifest or to its own. A first message whose `id` fails
  any of the above fails the session, with a diagnostic naming the receiver's
  own string and what arrived (truncated — an oversized one is refused
  without being echoed whole). This is strict on purpose: composing one fixed
  string correctly is the least a peer is asked to do, and one that cannot is
  not one to trust with the rest.
- **What each end records.** The host writes both strings into the plugin's
  diagnostics log at launch, as a `handshake` record beside the plugin's own
  output. The SDK exposes the host's string to the plugin author.
- **Shipping.** The host's string is `Hello from vizi <version>`. The plugin's is the SDK's to compose;
  its manifest id and version are the natural content.

**The version match is on the MAJOR, and both ends check.** A
differing major fails the session; a differing minor does not, because §3.2
makes a minor bump backward-compatible and a major bump the only breaking one.

An older peer meeting
a newer one skips field numbers it does not know (§3.2's append-only rule) and
refuses message kinds it does not implement, which is §8's ordinary path rather
than a session failure.
**Both ends check.** The host checks the plugin's `Ready`, and the reference
SDK checks the host's `Hello` (`vizi_plugin_sdk/server.py`); each fails the
session with a diagnostic naming both versions. A plugin needs its own check
because it cannot rely on every host to reject it.

The two gates do different jobs: the manifest's `engines.vizi` decides whether
to *launch*, the handshake decides whether to *proceed*. Both turn on the
major — see §3.2.

**No message of any kind may precede the handshake, on either channel**
(`doorbell_protocol.md` §3.1a states the doorbell's half). That rule is what
makes the doorbell's unknown-opcode skip safe: skipping assumes the contract is
already settled, and before the handshake it is not.

**Nor may a doorbell record precede the token check on Windows**, and in
particular no `M` — a capability handed to an unauthenticated connection is a
capability handed to whoever connected.

### 3.2 What the version covers

`protocol_major.protocol_minor` versions **the whole platform boundary**: this
channel's messages, the doorbell's record format, and the shared-memory
protocols. One pair implies exactly one of each.

That the doorbell's format is included is load-bearing and is stated here
because this is the only gate that exists. The doorbell's `ver` byte says what a
frame *is*; it negotiates nothing and could not, because a v1 receiver reading a
v2 record sees a nonsense length and desynchronises silently
(`doorbell_protocol.md` §3.1a). So:

> **There is no version in which two doorbell formats are legal**, and a
> transport property that differs between them is a bump of this version rather
> than a runtime discovery.

**Doorbell v2 is protocol `1.0`.**

**Within a major the wire is append-only**, protobuf-enforced: field numbers are
permanent, new fields take new numbers, retired numbers are reserved, and old
peers ignore what they do not know. That is the mechanical half.

**The judgement half is the same shape: a MAJOR bump breaks, a MINOR bump
does not.** A `1.1` host serves a `1.0` plugin; a `2.0` host serves neither.
A plugin author cannot build against a moving target, and an ecosystem of
third-party plugins is the point.

**So a breaking change is a major bump, and the version number says which kind
of change it was.** That obligation is what makes the promise checkable: a minor
that changes behaviour is a defect in this document, not a licence.

**This is why a doorbell format is a major version.** The doorbell format
cannot be made compatible by any amount of care — a v1 receiver reading a v2
record sees a nonsense length and desynchronises silently (see above, and
`doorbell_protocol.md` §3.1a) — so a change of format is a breaking change by
construction.

`plugin_principles.md`'s Version-skew rule governs what happens when peers
disagree — fail whole, at connect, never partially — and they disagree on a
differing MAJOR.

**A "drop and count the unknown version" rule has no place on this channel.**
Unknown *field numbers* are skipped, which is append-safety; unknown *versions*
are settled by the handshake, and accepting one at parse time is the partial
acceptance the parent rule forbids.

### 3.3 Teardown

Four ways a session ends, and they are not interchangeable:

| Ending | Initiated by | What the peer sees |
|---|---|---|
| **Graceful shutdown** | host, `Shutdown` | the message, then the stream closes |
| **Stream close** | either | end-of-stream, no message |
| **Session fault** | either | a fault message, then close (§10) |
| **Process death** | the plugin, involuntarily | stream error; the host reaps and retains a diagnostic |
| **Host gone** | the host, involuntarily | end-of-stream or a stream error with no `Shutdown` before it, and the doorbell's EOF; nothing else, ever |

**A plugin process serves one session and exits when it ends, however it
ended.** After `Shutdown`'s window, after a fault, and after the stream
simply went away — the last is what a host crash, a kill or a hang that was
killed looks like from the plugin's side, and it is the one ending nobody
announces. The plugin SHOULD exit then: on Windows the host's job object ends
it regardless (`manifest_schema.md` §2.2), but on unix the plugin runs in a
session of its own by design — that is what contains its tree at a
*voluntary* teardown — so a host that died reaches nothing, and a plugin
that waits for a second session waits forever, holding its mapped segments.
The reference SDK's `serve()` returns when the session stream closes, after
letting it carry the `ShutdownReport`, and the process ends with it; each SDK
is asked to do the same in its own idiom. This is a SHOULD and not a MUST
because the failure is the plugin's own — an orphan costs the user a process,
not the host a session — and because the host does not go looking for its
plugins after its own death, on any platform, so a MUST here would be one the
host could not check. A plugin that exits promptly on every ending is, on all
three platforms, the whole story.

**The host MUST send `Shutdown` before closing the stream on a voluntary
teardown**, and MUST allow a bounded window for the plugin to finish. Without
it no plugin cleanup handler can run.

**`Shutdown` is the session's, and `DoorbellShutdown` is one channel's.** They
are different messages with different scopes and the naming makes that easy to
get wrong: `DoorbellShutdown` closes the fast path and opens its grace window
(§8.13) while the session continues long enough to hear the report;
`Shutdown` ends everything. A doorbell that closes always ends its session, so a
teardown that begins with a doorbell fault sends both, in that order.

**This section is the source of truth for teardown authority**
(`plugin_principles.md` 13); `doorbell_protocol.md` §8 cites it rather than
restating it.

**Teardown is announced here even when it was detected elsewhere.** macOS's
doorbell can report peer death exactly once and to whoever happens to be reading
(`doorbell_protocol.md` §8), which is corroboration, not a shutdown path. The
authoritative notice is this channel's.

#### What `Shutdown` obliges

A message with no lifecycle is a message nobody can implement against, so the
plugin side of it is specified rather than left to each SDK:

| | |
|---|---|
| **Registration** | the SDK exposes a shutdown hook, alongside `@command` and `@tool`; a plugin that registers none has nothing to run and closes immediately |
| **Where it runs** | **not on the dispatch path** (§6), and on a **dedicated** worker — never the command pool, where a hook queued behind a long-running command would not start before the host's deadline expires, silently, in exactly the scenario the hook exists for |
| **What it may do** | send final messages: a `DoorbellFinalReport`, a last `ReleaseDeposit`, a diagnostic. The stream stays open for the window |
| **What it may not do** | start new work — no grants are issued during the window, and a request made in it may not be answered |
| **Completion** | the plugin **closes the stream**. That is the signal, and it is the only one that needs no new message |
| **Work already running** | a command in flight when `Shutdown` arrives **runs to its deadline or the window's, whichever is sooner**, and its reply is sent if it makes it. A command that fails because teardown began failed by the HOST's doing, not the plugin's, and §12 forbids reporting it as a plugin fault |
| **New work** | **refused at both ends.** The host sends no request it has not already sent, including one queued behind the in-flight slot, and the plugin answers any that arrives with an `Error` rather than running it. Without this the "may not start new work" row above is unenforceable: cleanup would run beside a command the plugin never asked for |
| **On raise** | the plugin sends a diagnostic **before** closing the stream; teardown continues either way, and a hook that throws does not become a hang |
| **On overrun** | the host tears down at the deadline regardless, exactly as `DoorbellShutdown`'s window does (§8.13) |

**Four edges of the window**, each settled here rather than left to reading:

- **The `Error` that refuses a request arriving inside the window carries
  `Kind = PROTOCOL`.** The host broke this section's own *"sends no request it
  has not already sent"*, and `PROTOCOL` is what names that; `INTERNAL` would
  report the plugin's own machinery for the peer's mistake.
- **A ride-along `InstallSegment` on such a request is NOT applied.** *"No
  grants are issued during the window"* covers the ride-along, which is a grant
  travelling inside another message rather than an exception to the rule. The
  refusing `Error` is a §5.6 disposition, so any descriptor the message carried
  into an already-installed segment is released with it — refusing a message
  must not strand the bytes it named.
- **A standalone `InstallSegment` arriving inside the window is applied and
  acknowledged.** The window's rule is about *work*; an arrangement is not
  work, an installed-and-acknowledged segment in a closing session costs
  nothing, and the ride-along rule above is about the request it rides on, not
  about the grant. A host that sent one has already broken *"no grants are
  issued during the window"*, so either answer would be defensible, but only one of them leaves the host's grant ledger
  consistent with the plugin's, which is the thing that depends on it
  (`shared_memory_protocol.md` §8's ack deadline would otherwise fire on a
  closing session). Diagnosed locally, since the host did break the rule.
- **A second `Shutdown` inside the window changes nothing.** The first window
  and its deadline stand, the hook does not run again, and the repeat is
  diagnosed (§12). The deadline is measured from the host's *first* send, so
  honouring a second one would let a peer extend a window this section
  deliberately makes the host's to bound.

**The deadline is the host's and is measured from its send**, for the same
reason `DoorbellShutdown`'s is: the host cannot know when the message arrived, so
a duration measured at the receiver would promise time the host does not intend
to give. It rides as `Shutdown.deadline_ms`, alongside `reason` and `detail`.
Its value is §9's **5 s**. An empty `Shutdown` is wire-valid and reads as
`UNSPECIFIED` with no deadline.

#### What `reason` is for

**Behaviour does vary, so the reasons are specified — and every reaction below
is PERMITTED, never required.** A plugin that ignores `reason` and just runs its
hook is conforming; the field exists so that one which *can* do better is able
to. The axis is **will I be started again**, because that is the only thing a
plugin can usefully act on inside a 5 s window.

| `reason` | Started again? | What that permits |
|---|---|---|
| `host_exit` | no, vizi is closing | discard warm caches rather than spend the window persisting them |
| `user_disabled` | not until a person acts | as above |
| `not_needed` | maybe, without a person | a dataset closed or a contribution went idle; cheap state may be worth keeping |
| `restart` | **yes, imminently** | the one reason to persist expensive derived state, and the only one where a plugin may assume the same work is coming back |
| `session_fault` | host's call | something was wrong; a final diagnostic is worth more here than anywhere else |
| `user_stopped` | **yes, when next used** | the person stopped it from work that did not answer a cancel (§8.16); as `not_needed`, but the work it was doing was abandoned |

**The distinction earns its place only because `restart` exists.** Without it the
six values would be flavour on an event whose response is identical, and a field
nobody branches on is a field two SDKs will disagree about for no gain —
`plugin_principles.md` **11**, *never claim what you do not have*, applied to a
distinction that would not carry one. `restart`
is a real fork: persisting a cache is worth doing exactly once and wasteful
every other time.

**The plugin closing the stream is what "done" means**, and choosing it over an
acknowledgement is deliberate — an ack would need a message, a matching id and a
rule for a plugin that sends it and then keeps running. Stream close is
unambiguous, needs nothing new, and is what the host already watches for.

**But close alone cannot say WHY, so the outcome rides ahead of it** as
`ShutdownReport{outcome, detail}` — `no_hook`, `completed`, `raised`,
`overran`. Hook
completed, hook raised, no hook registered, and dispatch loop died of something
unrelated are one event at the socket. So *the plugin says the outcome before
it closes*, and the close remains the completion signal. This does not contradict §14's rejection of a heartbeat: a heartbeat is asked to prove a
handler ran, which a stream-level event cannot do; here the handler reports its
own outcome and the stream event only bounds it.

**And the host does not record this as a failure.** A plugin closing its
stream after `Shutdown` is the completion signal, and §12 says that expected
host shutdown is not reported as a crash. A plugin exiting 0 is an ordinary
ending, not a crash.

---

## 4. Correlation

Every message carries a string `id`.

| Direction | Who allocates | Shipping form |
|---|---|---|
| Host request | the host session | `r<n>`, monotonic per session |
| Plugin reply | echoes the request | — |
| Plugin request | the plugin | `q<n>` in the reference SDK |
| Host reply to a plugin request | **echoes the plugin's id, untouched** | — |
| Unsolicited push, either direction | — | **empty** (see rule 4) |
| `Hello` / `Ready` | the sending end | its identity string, `Hello from …` / `Ready; I am …` (§3.1) — not a correlation id |

Six rules:

1. **An id is unique among the messages in flight in its direction on its
   session.** Reuse after a request has completed is legal and is not relied on.
   A **one-way** has no completion event, so it has no reuse point either: an id
   on a one-way is free immediately, because nothing will ever match it. The one
   place that bites is `GrantStateQuery` (§8.12), a one-way whose answer
   `GrantState` is a push echoing its id — so a host-allocated id travels
   plugin→host into the same matcher that holds host request ids. Rule 3 is what
   keeps that safe.
2. **A reply's id is byte-identical to its request's.** No canonicalisation, no
   normalisation, no case folding.
3. **A message's CLASS is decided by its kind, never by its id** — and the
   classification MUST happen *before* any id matching. A push arriving while a
   request is pending has an id that matches nothing, and an implementation that
   matches first drops it as a stray. Both ends do this: the host's actor
   classifies with an exhaustive match ahead of the pending slot, and the
   reference SDK's dispatch loop with a partition over `HostMsg`'s oneof that
   fails at startup if an arm is left out of it
   (`control_plane_implementation_details.md` §3.1).
4. **An empty `id` is legal exactly where nothing correlates.** Requests carry
   a non-empty id, unique in their direction while in flight; replies echo it
   byte-for-byte; a push the protocol correlates — `GrantState` answering a
   `GrantStateQuery` — carries the id it answers. **An ordinary unsolicited
   push carries an empty id**, and a receiver MUST NOT reject it for that:
   `PanelPatch`, `OverlayUpdate`, `OverlayRemove` and `ReleaseDeposit` all
   carry one. **The two handshake messages are
   the exception in the other direction**: `Hello.id` and `Ready.id` are the
   identity strings of §3.1 — non-empty by rule, checked for form, and never
   matched against anything.
5. **An unmatched id is never silently discarded.** A reply whose id matches no
   waiter means one of: the waiter timed out, the peer echoed the wrong id, or
   the message was misclassified as a reply. All three are diagnosable and none
   is routine, so the message is surfaced as a diagnostic even where it is
   dropped.
6. **A dropped reply is still CONSUMED for the resources it carries.** This is
   the rule a timeout makes reachable and it is separate from the unmatched-id
   rule above, because logging a message is not the same as releasing what it
   holds.

### An abandoned reply still owns something

A `ReadDataVolume` or `ReadMaskVoxels` that times out leaves a request the host
is still serving. The reply then arrives carrying a descriptor into a **live
ring reservation** — real bytes the host is holding on the plugin's behalf — and
dropping it as an unmatched id leaks that reservation for the life of the
session.

**The damage is out of proportion to the event, because Loaned reclamation is
FIFO.** The host's deposit ring marks a record released and advances the tail
only while the *front* record is released, so one abandoned deposit at the head pins every
later released deposit behind it, and the ring grows instead of being reused.
One timed-out read costs the ring, not one slot.

> **The receiving dispatcher releases the resource, without delivering the
> payload to anyone.** A late `DataVolume` is answered with `ReleaseDeposit` for
> exactly the reservation it names, and the message is then dropped and
> diagnosed per the unmatched-id rule above. The expired caller is never handed
> a value it has already given up on.

**The host may not reclaim it instead**, and that is the whole reason this
obligation sits with the receiver. Reclaiming a reservation because a timer
expired is silently taking back memory the peer may be reading —
`plugin_principles.md` 5 in as many words: *"silently reclaiming it because
'enough time has probably passed' is not"* a recovery protocol. The host cannot
know whether the reply was received; only the receiver knows, and only the
receiver can say so.

**Generally: a reply that names a resource has an owner from the moment it
arrives, regardless of whether anyone was waiting for it.** Any later
message carrying a reservation, a descriptor or a handle inherits this rule
rather than needing its own.

---

## 5. Message classes

Five, and the class decides delivery, ordering and what overload does to it.
`control_plane_principles.md` D is the reasoning; this is the contract.

| Class | Direction | Reply | Under overload |
|---|---|---|---|
| Host request | host→plugin | required | queued behind the in-flight slot |
| Host one-way | host→plugin | none | sent immediately, never queued |
| Plugin request | plugin→host | required | bounded queue; overflow **answers an error** |
| Push | either | none | see below — depends on **coalescing or one-shot** |
| Arrangement | host→plugin + ack | ack required | bounded queue; **never dropped** |

**A host→plugin push may be coalescing too (§5.7): `ChunksWanted` is state keyed per overlay.** It takes no queue at
either end — the host merges on its own side, sending once per change, and
the plugin applies what arrives — so it is a push by class and a one-way by
delivery; it is named apart because a receiver that treated a stale one as a
transition would be wrong, and §5.4's replace-per-key is what the *sender*
does with it.

### 5.1 Host request

An id-matched call. The host blocks a caller thread on the reply, bounded by a
timeout (§9).

**At most one host request is in flight per session.** The next one is queued
until the current reply arrives. It has two consequences that must be stated
rather than discovered:

- **A lost reply is unrecoverable for the session.** The pending slot never
  clears, so every later request queues behind a reply that will never come. The
  host therefore marks the session **suspect** on a request timeout and offers
  restart — that is a property of the concurrency model, not evidence about the
  plugin (`control_plane_principles.md` E2).
- **Nothing that must not wait may be a host request.** An arrangement sent as a
  request queues behind a long-running command, and `doorbell_E_input.md` §1a
  stops emitting input for a pane until its epoch is acknowledged — so a slow
  command would silently kill tool input. **Arrangement is therefore class 5.5,
  never class 5.1.**

### 5.2 Host one-way

A message the host sends with no reply expected. **It bypasses the in-flight
slot entirely and leaves the moment it is submitted, even mid-request.** This is
what keeps the answer to a plugin request from deadlocking behind the
`InvokeCommand` that triggered it, and it is the mechanism
every host→plugin message that must not wait uses.

Two sub-kinds share it, and they differ only in the id:

- **A reply to a plugin request** echoes the plugin's id (§4).
- **An unsolicited host push** — an arrangement, a shutdown notice, a release —
  carries an **empty** id, because nothing correlates it (§4 rule 4). The one
  exception is a push the protocol correlates by construction: `GrantState`
  answering a `GrantStateQuery` echoes the query's id.

`Hello` is the one host one-way that is neither: its `id` is the host's
identity string (§3.1), which correlates nothing and is checked for form by
the plugin rather than matched. It is not a push *about* anything, so the rule
below does not reach it.

**A push never names its subject in the `id` field.** Every acknowledgement and
report in §8 correlates by *content* — `GrantAck` on `segment_id`,
`TimeMappingAck` on `{gen, revision}`, a fault on the ids in its body — and the
`id` field is for reply matching alone. A fault that named the message it was
about in `id` would land in the receiver's reply matcher, which is §4's
classify-by-kind rule failing in the one place it is most expensive.

### 5.3 Plugin request

The plugin initiates and blocks a plugin thread on the reply; the host answers
with a one-way `HostMsg` echoing the plugin's id.

- **Bounded queue, and overflow answers an error rather than dropping** — a
  plugin thread is blocked on every one, so silence is the one unacceptable
  outcome (`control_plane_principles.md` D4). Shipping capacity is 32
  (`REQUEST_CAP`), overflow answers `host_error{INTERNAL}`.
- **Requests are served ahead of coalescing pushes but NOT ahead of one-shot
  ones** (§7). Ordering *between* queues is not contractual; ordering within
  each is.
- **Every failure path answers**: no permission, no dataset, out of bounds, no
  such level, queue full, internal error. The reply kind is `host_error` with a
  structured `Error.Kind`.
- **Re-entrancy is refused, not deadlocked.** A data read whose active dataset is
  served by the requesting plugin's *own* reader is answered `UNSUPPORTED`
  immediately, because serving it would call back into the blocked reader.
- **A data read the reader cannot decode is `UNSUPPORTED`, not `IO_ERROR`**
  The file is readable but holds an encoding the reader refuses (a
  TIFF plane whose compression or predictor it does not implement, validated
  again at decode because IFDs may differ; a reader plugin's own
  `UNSUPPORTED` reply for a dataset it serves). `IO_ERROR` is for a read that
  failed. `UNSUPPORTED` therefore has two meanings on a data read — refused
  before any read (above), and undecodable — and the message says which. A
  read the host cancelled inside a reader plugin is answered `CANCELLED`.

### 5.4 Push

Unsolicited, no reply. **Every push is either coalescing or one-shot, the
sender's document says which, and the receiver's queue policy follows from
that** — this is `control_plane_principles.md` D1 and it is the single most
consequential rule in this section.

| | Coalescing | One-shot |
|---|---|---|
| Has a replacement key | yes | no |
| A later message with the same key supersedes it | yes | n/a |
| Correct overload policy | **replace per key** | **never drop** |
| Overload behaviour | count, report | session fault |

A one-shot push carries a transition that will never be restated: an
acknowledgement, a release, a fault, a violation report.

**A coalescing push may still carry a descriptor, and then its replacement has
an obligation** — the superseded message's bytes are a loan the receiver now
owns and must return (§5.6). Coalescing is a policy about the *value*; it never
disposes of the bytes that carried it.

**"Coalescing" is a property of the RECEIVER'S QUEUE, not of the message, and
drop-oldest does not supply it.** This is the rule that is easiest to state
loosely and get wrong, so it is stated exactly:

> **A coalescing queue holds at most one pending item per replacement key, and
> a new item REPLACES the pending one for its key.** It never evicts a key to
> make room for a different key.

Drop-oldest on a FIFO does something else entirely: it evicts the **oldest
message**, whatever key that message carried. A single pending update for widget
A is evicted by a burst of updates for widget B, and A is then wrong for the
rest of the session with nothing to correct it — no later message names A, and
nothing knows one is missing. The message being state-shaped does not help,
because the state that was lost is the state nobody restates.

**And a replacement key is finer than a message but NOT as fine as a
property.** `PanelPatch` carries a batch of ops across widgets, and `set_value`
for a widget does not supersede an earlier `set_enabled` for the same widget —
so the key cannot be the message. It cannot be (widget, property) either,
because **two properties of one widget can be ordered state**: `set_options`
followed by `set_value` on a dropdown is one update, and applying the value
against the old option list loses it — the host clears the combo on
`set_options` and restores the prior selection *by text*, so a value applied
first is silently dropped. The host preserves op order within a patch, and
a coalescer that treats properties as independent throws that away.

> **The keys are (panel, widget) — (panel, widget, row) for a `list` row's cell
> — (overlay id), (segment id, region), and —
> for a chunked overlay (§5.7) — (overlay id, level, t) for a chunk and its
> own (overlay id) for the label table, and (drag id) for a shape proposal
> (§8.8). A coalescing queue holds one pending
> item per key; HOW two items for one key combine is the message's own rule.
> For the op-carrying keys an op replaces the pending op for its property —
> EXCEPT on a widget whose ops are state-dependent, where nothing may be
> merged at all.**

A chunk's key is last-write-wins like an overlay's, and `t` absent — a
`static` overlay's one volume per level — is a key of its own that never
aliases `t = 0`. The table's key is not the `OverlayUpdate` key for the same
id: the two are different state and must not supersede each other.

**The test for a merge is the state it produces, never the order it emits.**
Two plausible ordering rules both fail it:

- *"replace at that property's existing position"* sounds order-preserving and
  is not: a replacement inherits a position chosen before the dependency
  existed, so `value(a)`, `options([a,b])`, `value(b)` emits the value ahead of
  the options it needs and the widget finishes on `a` where the unmerged
  sequence finishes on `b`.
- *"a canonical order, `set_options` before `set_value`"* fails the mirror:
  `options([a,b])`, `value(1)`, `options([b,a])` finishes on `b` unmerged and
  `a` merged. It also fails on options **alone** — starting on `b`,
  `options([a])` then `options([a,b])` finishes on `a` unmerged and `b` merged,
  because the intermediate list drops a selection the later list would have
  kept.

**The reason no ordering can work is that a dropdown's two properties each
depend on the state the other establishes.** `set_options` carries the
selection across a list change *by text*, and `set_value` is bounds-checked —
or text-matched — against whatever list is there now. So an intermediate
`set_options` is not a superseded value; it is a step the widget's state went
through, and a `set_value` is not a value at all until you know the list.

> **A widget that has been sent a `set_options` is order-sensitive, and its
> pending ops are held whole: nothing is superseded, nothing is reordered.**
> Every other widget in the vocabulary takes its value whole — a slider's range
> comes from the manifest, a checkbox, a label and an image each take what they
> are given — so those still collapse to one pending op per property, which is
> what coalescing is for.

**A list's rows are the other state a value depends on.** A list's
`set_value` names one of its rows, so it goes after a `set_rows` pending with
it, never in an older place ahead of it; and a `set_rows` that brings back a
key a pending one dropped is kept beside it rather than merged, because
merging would erase the removal and the row would not start fresh. A
`set_rows` naming only keys the pending one has still supersedes it.

Being sent a `set_options` is how a receiver knows, without holding the
manifest. Nothing can be superseded on such a widget, so the only bound
available is a ceiling, and reaching it is a session fault (§7) rather than a
drop — a dropdown is a human-rate surface and a plugin queuing hundreds of list
changes between two drains is not one the host can help.

**Order-sensitivity lasts as long as a `set_options` is PENDING, not for the
widget's lifetime**, and the difference is a drain. Once the queue has been
drained the receiver has applied the options; two `set_value`s arriving
afterwards may merge, because the condition a merge has to satisfy is *"no
`set_options` sits between the ops being merged"* — and after a drain, none
does. A sticky-for-the-session reading is conservative rather than wrong; the
host is per-batch, and per-batch is the reading that is correct and cheaper.
**The sticky reading costs patch size and buys nothing**, since a value applied against an options list the receiver has
already installed is exactly the case coalescing is for.

The ceiling is therefore a bound on **one pending batch**, which is what makes a
finite number sufficient: see §9's table, where it is 256 ops per widget — the
one-shot queue's figure, for the one-shot queue's reason, since these ops can no
more be dropped than an acknowledgement can.

**A merge rule for this section must be checked against the code that applies
it**: replay the merged and unmerged sequences through the real widget builder
and compare the resulting state. A test that asserts operation order passes
both failing rules above.

**`SegmentShortfall` is the third key and it merges differently, which is why
the rule above separates "one item per key" from "how they combine".** Its merge
is not last-write-wins:
it keeps the **largest** `wanted_bytes` and **sums** `dropped` (§5.6). Both
follow from what the message is for — a re-grant that clears the biggest
shortfall clears the smaller ones, and the drop count is a total §12 needs, so
replacing it would discard exactly the number the message exists to carry.

**`largest_free_bytes` travels WITH the `wanted_bytes` it was measured beside**
— when a merge keeps a new, larger want, it takes that message's free figure
too; when it keeps the held one, it keeps the held figure. Neither a maximum nor
the newest value is right, because the pair is one observation: the host reads
them together to tell exhaustion from fragmentation, and a want from one moment
beside a free run from another describes an arena that never existed. This is
what the host implements. So
"coalescing" constrains the queue to one pending item per key; **what a merge
means is the message's own**, and each coalescing kind says.

For the two op-carrying keys that is last-write-wins per property *and*
dependency-ordered per widget. Ordering *between* keys is not preserved and does not need to be: widgets
and overlay ids are independent of each other.

**A SENDER may merge its own outbound pushes by the same keys and the same
rules.** Coalescing is defined above as a
property of the receiver's queue because that is where the guarantee lives; it
is not a prohibition on the sending side, and on a fixed arena (§5.6) it cannot
be, since every pending descriptor pins a reservation and an unmerged backlog
makes a plugin drop pushes it need not drop.

Two rules come with it:

- **§12's carve-out applies to whichever end cannot release at replacement
  time.** A queue that supersedes a message naming a loan must release it *at
  replacement time or not coalesce at all* — written for the receiver, and true
  of the sender for the same reason, except that there the stranded arena is the
  sender's own. A sender with no way to return the superseded reservation sends
  in order instead of merging.
- **An unmergeable push that cannot be admitted is REFUSED TO ITS CALLER, not a
  session fault.** §5.4's overload answer for a coalescing kind is *count and
  report*, and the caller still holds bytes it can reclaim — so the honest
  outcome is to tell it the push did not go, which is also what a full arena
  already does (§5.6).

  > **This is the SENDING end's rule, and §9's held-whole ceiling is the
  > RECEIVING end's. They are not in conflict and are not about one event.**

  Held-whole ops travel one way — `PanelPatch` is plugin→host — so the SDK is
  always the sender here and the host always the receiver. This paragraph is
  what an SDK owes its own caller when its outbound queue is full; §9's
  *"session fault"* is what the host does about a peer that ignored this
  paragraph and put 257 on the wire anyway. **A conforming SDK makes §9's row
  unreachable**, which is what a sender rule and a receiver backstop are
  supposed to look like.

  An SDK that faults its own session for its own caller's overflow is applying
  the host's rule to itself.

**And the queue cannot run out of keys, which is why there is no resend
protocol.** The bound has **three sources**, and they are different in kind:

- **Static, from the manifest.** A panel's widgets are declared statically and
  `PatchOp` has a closed set of op kinds, so the op-carrying keys are bounded by
  `Σ widgets × op kinds + declared overlays`, plus, when any panel declares
  `follow_targets`, one `OfferFollow` key per such panel and two `FollowState`
  keys (the running Follow's and one just ended that may still be pending,
  §8.15) — all readable at scan time, before the plugin starts.
- **Dynamic, from the host's own ledger.** `(segment id, region)` is not in the
  manifest and is not known at scan time: segments are granted *during* the
  session. It is still structural rather than validated, because the host grants
  every segment and every `InstallSegment` declares its regions — so the live
  key count is something the host already knows exactly, rather than something
  it must trust a plugin about. **A retired segment's key leaves the space when
  `SegmentReleased` settles it**; a pending shortfall for a segment being
  retired is dropped rather than kept, since a re-grant is what the plugin is
  getting anyway.

- **Per gesture, from a sender rule: `shape_proposal`'s `drag_id`** (§8.8).
  The host begins drags at a person's pace, one at a time, and a conforming SDK
  stops proposing for a drag once its commit or its end reaches it — so the live
  keys are the drag in progress plus the one or two whose end is still in
  flight. That is a bound by rule rather than by structure, so §9 gives it a
  fixed slack, and a plugin past it faults the session as an undeclared widget
  does.

Neither of the first two can be exhausted by a peer, which is the property that
matters; the third is the one place a key space rests on the sender keeping a
rule, and it is a person's gesture that it counts.

**A structural bound beats a validated one** (`plugin_principles.md` 8's
corollary): the queue is sized from the manifest and the grant ledger, eviction
is unreachable, so there is nothing to recover from and no resend request to
specify.

A key that is *not* in that space — a patch naming an undeclared widget — is a
validation failure (§11), not an overflow.

**Coalescing and one-shot may not share a queue.** A mixed queue with any
eviction policy is unsound for the mixture even where it is sound for one
member. One FIFO with drop-oldest carrying both is the shape this rule exists
to name (`control_plane_implementation_details.md` §3.2).

**Prefer state to edges and the problem disappears.** Where a transition can be
restated as state — "this overlay's content is now empty" instead of "remove it"
— it becomes coalescing and needs none of the above
(`plugin_principles.md` 2, `doorbell_D_notification.md` §6).

### 5.5 Arrangement

A host→plugin one-way message that establishes something the fast path may then
reference, plus a plugin→host acknowledgement.

Five rules, and none is optional:

**This subsection is the source of truth for the arrangement gate**
(`plugin_principles.md` 13). `doorbell_E_input.md` §2 and
`doorbell_implementation_details.md` §5 both apply it to `E` records and cite it
rather than restating it.

1. **The gate rule: the host emits nothing referencing an unacknowledged
   arrangement.** No `E`
   record for a pane whose epoch is unacknowledged, none naming a tool handle
   whose activation is unacknowledged, no descriptor into a segment that is not
   installed.

   **One exception, and without it every first use of a segment costs a round
   trip:** an arrangement MAY be carried by the same message as the first use
   that depends on it, and the receiver applies the arrangement before
   interpreting the rest of that message. `InstallSegment` rides `DataVolume`
   (§8.12), so the first transfer into a data segment carries both the meaning
   and a descriptor into it. Read as a bare prohibition, this rule would force
   the host to send `InstallSegment` standalone, wait for `GrantAck`, and only
   then send the descriptor — which *works*, and is the thing the exception is
   trading away. Ring growth pays the same round trip. **It is a latency cost,
   not a deadlock**,
   because `InstallSegment` is its own `HostMsg` arm and can always be sent
   alone. The exception earns its place on a drag-rate path, not by necessity.

   **And for a descriptor into a SEGMENT the exception is one form of a more
   general rule, which §8.12 owns: the receiver holds a descriptor into a
   segment it has not yet installed, whichever half is missing, and reads it
   when the installation completes.** That is what lets an export grant be
   installed standalone ahead of its first `ExportChunk` without the host
   waiting for the acknowledgement — the unlock is the handoff, not the ack.

   **This is not a weakening, because there is no race inside one message.** The
   gate exists to stop a reference *outrunning* its arrangement across two
   deliveries with no ordering between them; within a single message the order
   is the receiver's own, and it is fixed here: **apply, then interpret.** The
   acknowledgement is still owed and still means *installed*, and everything
   after that message is gated normally.
2. **The ack-means-applied rule.** An acknowledgement means *applied*, not
   received: mapped, installed,
   registered, interpreted (`control_plane_principles.md` D3).
3. **The install-before-ack rule: the peer's own local state is updated BEFORE
   the acknowledgement is sent.**
   This is the ordering everyone gets backwards. Acknowledge first and a fast
   record under the new arrangement reaches the fast path's checks before the
   value is installed there, is ruled invalid, and is dropped — and the record
   dropped that way is most often the `PRESS`, so the whole stroke is lost and
   the plugin reports a host bug it caused itself. `doorbell_E_input.md` §1a
   states it for `epoch_acknowledged` and §2 for `tool_activated`; it is a
   general rule about arrangement, not two special cases.
4. **The re-ack rule: a re-sent arrangement is acknowledged again**, and
   applying it a second time
   changes nothing.

   **Where an arrangement may be RE-STATED with new content, the
   acknowledgement carries a revision and the identity alone is not enough.**
   `ToolBinding` is that case (§8.10 updates a known handle in place), so it
   and `ToolBindingAck` carry `revision`: monotonic per identity, unchanged by a
   re-send — which is what keeps a retry idempotent — and incremented when the
   content changes. A receiver echoes what it installed; a sender ignores an
   acknowledgement naming anything else.

   Without it a retry of the old content is still legitimately in flight when
   new content goes out, its acknowledgement lands afterwards, and the sender
   reads it as confirmation of the new arrangement — opening the gate on
   something the peer never installed. **A revision of `0` names no statement**
   and confirms nothing; they begin at `1`.

   **That rule binds the ARRANGEMENT as well as the acknowledgement.** A `ToolBinding` carrying
   `revision = 0` names no statement either, so a receiver **refuses it and does
   not acknowledge**, exactly as it refuses `tool = 0`: acknowledging would
   confirm nothing while looking precisely like confirmation, which is the
   failure the field exists to prevent. There is no "unversioned" reading in
   which 0 is installed.

   **Each IDENTITY'S sequence begins at 1**, not merely "1 is the lowest legal
   value". A sender allocating revisions from a per-session counter — so that
   handle 7's first statement is revision 12 — is non-conforming. vizi allocates
   per identity and its first statement for any handle is revision 1.

   > **That binds the SENDER. A receiver INSTALLS such an arrangement and
   > reports it — `where` = **`arrangement_first_revision`** — and does not
   > refuse it.**

   **The rule is hygiene, not something the receiver depends on.** A receiver's only uses of the revision are echoing it in the
   acknowledgement and checking that the identity has not regressed. Handle 7's
   first statement arriving as 12 echoes as 12, matches the sender's ledger, and
   opens the gate on exactly the content the peer installed — nothing downstream
   is unsound. The rule the receiver genuinely needs is the `0` refusal above,
   and it already has it.

   **Refusing costs a session over arithmetic.** The host re-sends the same
   revision, the same refusal follows, and §9's retry budget escalates against a
   peer whose only fault is where it started counting — which is
   `plugin_principles.md` 9's forbidden silent degradation, arrived at
   deliberately. It is also the shape used twice elsewhere: §5.6's
   `uses` disagreement and §9's missing deadline both apply the documented
   fallback and report the defect, and a third rule answering differently would
   be a receiver policy nobody could hold in their head.

   **The rule does not exist to detect a missed predecessor.** An arrangement
   carries its whole meaning, so a missed predecessor is not a loss and there
   is nothing to reconstruct — which is the same property §8.10 states as
   *"monotonic, not consecutive"* for handles and `doorbell_E_input.md` §1a as
   *"a plugin observes a subsequence and must not read a gap as loss"* for
   epochs. What the rule buys is that a
   receiver's diagnostic means something: a first revision of 1 everywhere makes
   `arrangement_first_revision` a defect rather than a style note.

   The host re-sends on a lost acknowledgement, so a peer that
   suppressed the repeat as a duplicate would have a correctly installed
   arrangement escalated. The same identity carrying *different content* is a
   host bug and is reported (§11), as `arrangement_content_mismatch` — **its
   own site, not `interaction_mismatch`**, which is an `E`-record replay whose
   bytes changed and merely has the same shape. §12 aggregates per `where`, so
   a shared code would merge two unrelated defects into one counter and one
   retained exemplar.

   **The general form of that rule — what a receiver refuses, and what it
   installs and diagnoses.** A receiver refuses an arrangement only where the
   *sender* depends on the refusal: a statement that names nothing (`tool =
   0`, `revision = 0`), a content mismatch under a known identity, and
   whatever the owning message section names. Everything else that is merely
   *odd* is **installed, acknowledged, and diagnosed locally** — a `view_id`
   above 255 (the doorbell's `u8` can never name it, so it simply receives no
   `E`), two entries naming one `view_id` or one `gen` inside a message, a
   `ToolBinding` whose `tool_id` this plugin never declared (the tool has no
   handler, which the plugin discovers on the first `E`), a `TimeMapping`
   whose `t` is not finite or not ascending (the receiver's only use of it is
   a lookup that works one index at a time), a `DatasetState` with no
   `geometry` or a `gen` that disagrees. *Which* of two colliding entries a
   receiver keeps is its own affair; the reference host never sends two.

   The reasoning is rule 4's, generalised: refusing without acknowledging
   costs a session over a sender's arithmetic — the host re-sends into the
   same refusal until §9 escalates against a peer whose fault was cosmetic —
   while installing is unsound nowhere, because an arrangement carries its
   whole meaning and nothing downstream reads the odd value in a way that
   breaks. Only refusing and staying silent costs anything, which is why this is a rule and
   the local diagnostic's wording is not (`plugin_principles.md` §14).
5. **The deadline rule: every arrangement has a deadline** (§9). An unacknowledged arrangement is
   an absence, and an absence with no deadline is invisible: the pane goes
   quiet, the tool goes dead, the segment stays pending, and nothing is either
   working or failing.

### 5.6 A push that carries a descriptor is a loan

**A descriptor names bytes the sender does not own again until the receiver is
done with them.** The sender writes into its own granted segment, sends the
descriptor, and **must not reuse or overwrite those bytes** until the receiver
says it has copied them out. That is `plugin_principles.md` 5 with nothing added:
when you give it, it is gone until it is given back.

The same rule holds in both directions. Host→plugin, a `DataVolume` descriptor
names a reservation in a Loaned ring and the plugin returns it with
`ReleaseDeposit` (§8.7) — the host does not reuse those bytes until it arrives,
and `shared_memory_protocol.md` §7 specifies the allocator. Plugin→host, the
release is `ReleaseLoan`.

**Reuse on a timing assumption is a defect, and it does not need speed to
fail.** Three shapes of it, each of which the rules below forbid:

- **Writing every payload at one fixed offset.** A panel with two image
  widgets patched in one handler then sends two `ImageRef`s naming the same
  bytes, and only the second image is present — deterministically, with no
  contention.
- **Resetting a bump cursor when the update is enqueued**, so the next push
  re-packs over bytes still in flight. "The host copies out during apply, so
  reuse is safe at human rate" is exactly the reasoning `plugin_principles.md`
  5 rules out.
- **A cursor per sender over an arena shared by several.** Two declared
  overlays then allocate from the same byte on their first push, needing no
  timing at all.

**What is required.** These regions are **Loaned in the plugin→host direction**,
with the roles of `shared_memory_protocol.md` §7 swapped: the plugin allocates,
the host consumes, and the host releases. The release is **`ReleaseLoan`** (§8.7), host→plugin,
naming `{segment_id, region, offset, len}` — and the same one-shot delivery
class as its mirror (§5.4), since losing a release leaks the plugin's
own capacity until teardown.

**The allocator is ARENA-scoped, not sender-scoped**, and an arena is **one
plugin-produced `LOANED` region**. Outstanding loans are a property of the arena,
so every writer into one region allocates from that region's single cursor and no
two of them can be handed the same byte. It is stated as a rule rather than left to an
implementer, because "each sender tracks its own outstanding loans" is the
natural reading of a loan protocol and it reproduces the collision exactly.

**"Per segment" would be the wrong unit, and one word off in either direction is
a defect.** Too narrow — a cursor per *sender* — is the collision above.
Too wide — one cursor for a whole segment — is a receiver allocating across a
region boundary into bytes with a different `extent` and possibly a different
discipline, which `shared_memory_protocol.md` §2's non-overlap rule exists to
prevent. The property being protected is that no byte is handed out twice, not
that a segment has one allocator.

**A segment carries at most one plugin-produced `LOANED` region**, which
`shared_memory_protocol.md` §3 owns and states. With one, the cursor's unit and the segment's
arena coincide without either rule bending, and a plugin needing two arenas is
granted two segments.

**A reservation is released ONCE, when every declared use has finished — and
the SENDER declares how many there will be**, in `SegmentRef.uses`. Release is a
property of the reservation, not of the message that named it: the receiver
decrements on every disposition and emits one `ReleaseLoan` at zero.

**The receiver cannot derive the count.** §8.8 lets `CompileLabelVolume` name an overlay's descriptor
rather than re-transfer voxels, so one reservation has two consumers inside the
same peer — but they arrive on paths with *different drain priority*: §5.3
serves requests ahead of coalescing pushes, and §8.8 says outright that the item
*"cannot be assumed to have arrived"* by an earlier `OverlayUpdate`. So a
receiver counting only the uses it has **started** hits zero when the first
finishes, releases, and the sender is then entitled to overwrite bytes the
second message names. It fails in both orders and needs no unusual timing:

| | |
|---|---|
| compile first | request drains ahead of the push, completes, count 1 → 0, release; the overlay then reads freed bytes |
| overlay first | push applied, count 1 → 0, release; the compile then names freed bytes |

**Only the sender knows the set, because only the sender has not sent the second
message yet.** `uses` is that number, `0` and `1` both mean one, and decrementing on *every* disposition makes arrival order
irrelevant — which is the property a two-channel protocol needs, since the
channels are deliberately not ordered against each other (§6).

**Within ONE message, no two operations may name the same reservation**, and a
message that does is refused whole and counted. The receiver may split a message
for queueing — a `PanelPatch` coalesces per widget (§5.4) — and a split carries
each part to its own disposition, so a reservation named twice is disposed twice
while `uses` counted the message once. The first release then frees bytes the
second part still needs.

Refused rather than tracked, because the alternative is preserving ownership
across a split and a partial supersession for a case nothing needs: a second
widget takes a second reservation, or the same bytes written twice. **The
refusal is a disposition** — the message is dropped and its loans go back
exactly once — and it is counted where §12 can see it, because an author
following this document should not have a patch disappear without a reason.

**A sender that over-declares pins the reservation until retirement or
teardown.** That is waste and never corruption, which is the right direction for
the failure to fall: an over-declared count costs a plugin its own capacity,
while an under-declared one would hand it freed bytes.

**And `uses` is a property of the RESERVATION, so every message naming one
carries the SAME count.** A sender that declares 2 in one message and 3 in
another is non-conforming. A receiver has two tempting answers and neither is
safe — erroring discards a
descriptor over a sender's arithmetic, and keeping the larger silently accepts
that the sender does not know its own set, which is the one thing `uses` exists
to assert. A receiver that sees a disagreement reports it — `where` =
**`uses_disagreement`** — and applies the count it was first given; the
reservation is the sender's to describe consistently.

**The sender keeps the mirror of that count, and refuses its own excess before
it leaves.** A message that would name a reservation for the `uses + 1`-th time
is an error at the sender — the reference SDK raises `UsesExceeded` from the
push or the request, in whichever order the messages come — because the
alternative is the receiver releasing the bytes under the message still in
flight, on every timing, with nothing to tell the author which push overwrote
which. And a reservation an earlier message already carried is the receiver's
to release: a later message that fails admission does not take it back,
however the sender recovers what nothing ever sent (the reference SDK counts
it as stranded until the session ends).

The site is not `unknown_release` or `release_region_mismatch` — those are about
*which* bytes a release names, and this is about how many uses were declared for
them, which §12's per-`where` aggregation has to be able to tell apart.

**If two operations that want the same bytes ever need two transfers, that is an
API defect rather than a protocol question** — a second way to ask for data
already in shared memory should be removed rather than specified, at least
wherever the data is large enough for the copy to matter.

**Every disposition releases, not only "copied out".** A loan comes back when the
receiver is done with the bytes, and it is done with them whatever it did with
them. Naming only the successful path is how a loan protocol strands its bytes
in precisely the cases that matter, so the obligation is a total function over
dispositions:

| Disposition | What the receiver does |
|---|---|
| **applied** | copies out, then **decrements** |
| **superseded** — replaced on the coalescing queue before it drained | **decrements at replacement time**, for the replaced message's reservation |
| **rejected** — validation failure, undeclared overlay, stale revision, a descriptor naming the wrong segment | **decrements**, *then* the diagnostic |

**Each row decrements; `ReleaseLoan` is sent when the count reaches zero.** For
the ordinary `uses = 1` case those are the same event. The distinction only appears when a reservation is named
twice, and then it is the whole point: a superseded message must decrement
*promptly* — the reason the row exists at all — without releasing bytes another
declared use still names.
| **the segment retires** | nothing further — `RetireSegment` asserts the host has *already* released every loan it held there (`shared_memory_protocol.md` §9.3), so a plugin is never blocked on the peer that asked it to stop |
| **the session ends** | nothing — teardown releases everything |

**The superseded row is the one that is easy to miss, and it is the one that
stalls a plugin rather than leaking.** A `PanelPatch` and an `OverlayUpdate` are
both coalescing (§8.4, §8.5) and both can carry a descriptor, so a supersession
is the *ordinary* outcome under exactly the load that makes the segment tight.
A panel image has one slot's worth of arena: if the superseded loan is never returned,
a conforming plugin's next push has nowhere to go and never will. So the rule
cannot be "release what you consumed" — replacement is a disposition, and it is
the receiver's, not the sender's, because only the receiver knows a replacement
happened.

The rejected row matters for the same reason and is easier to enumerate: a
host drops an overlay update without reading it when there is no overlay
store, the overlay is undeclared, the revision is stale or non-positive, the
wire form is invalid (including the live-region overlap check), or an
`ImageRef` names a segment other than the panel's. None is hypothetical.

**Growth does NOT swap with the roles.** `shared_memory_protocol.md` §7's
*"exhaustion doubles rather than fails"* is the **granting** side's answer, and
the host grants in both directions — it is not one of the roles that reverses.
The plugin's overlay segment is sized once from its manifest, so a plugin whose
arena fills has nowhere to go until the host re-grants. **The one arena not sized from the manifest is a chunked
overlay's (§5.7): the host sizes it from the open dataset and re-grants it when
the dataset changes**, which is the host's ring idiom applied to a
plugin-produced region.

**An arena changes size by being RETIRED AND RE-GRANTED, never in place.** The
host installs a larger segment at a **new** `segment_id` naming the old one in
`supersedes`; the plugin maps it, acknowledges, and allocates from it — and the
old one retires **on that successful acknowledgement**, not at receipt. The
ordering matters, because a retire-first sequence leaves the plugin with no
arena for a round trip and §5.6's own rule then makes it drop every push in that
window. Retiring second costs one interval of double residency, which is exactly
`shared_memory_protocol.md` §9.5's host-side arithmetic and its worst case.

**Retirement takes effect at each side's OWN knowledge of the acknowledgement**,
and saying so removes a window that would otherwise fault an innocent peer. The
plugin stops *starting* new work in the old segment when it sends
`GrantInstalled`; the host stops when it receives it. In between, a descriptor
the host had already sent into the old segment is legitimate and is **not** a
`ContractViolation` — the host could not have known. This matters for the
host-produced ring rather than the plugin's arena: there the party that must stop
sending is the one that does not generate the trigger. `RetireSegment` has no
such asymmetry, because its sender is the side that stops.

**`supersedes` is what carries that**, and **as specified in
`shared_memory_protocol.md` §9 it fires when the replacement reaches *installed*,
never on arrival and never on failure** — that document owns the rule and states
the three cases (`plugin_principles.md` 13). What belongs here is why the
control plane wants it that way: tying it to the acknowledgement makes the safe
order the *only* order, which is strictly better than a two-message sequence the
host must remember to complete, since a forgotten `RetireSegment` leaks a mapping
until session end.

**The plugin asks, and it says HOW MUCH it is short** — `SegmentShortfall`
(§8.12), naming the allocation that failed, the largest free run, and how many
pushes have been dropped for want of space. Naming the amount is the point: a
host watching release pressure learns *that* a plugin is short and never by how
much, so it could only grow by doubling — which is what the manifest's
`segment_mib` was supposed to replace. The plugin is the only end that knows the
number, because the sizes that outrun an arena are a widget's pixel dimensions,
an overlay item count that scales with the *data*, and the depth of loans the
host has not yet released; none of the three is derivable from the manifest and
the last is not derivable by the plugin either until it happens.

**It is coalescing, and that is a requirement rather than a tidiness.** A full
arena on a drag-rate path fails many pushes in a row, and one message per
failure is a flood on the channel the session needs in order to recover — the
same reason `ContractViolation` is rate-bounded. Keyed on
`(segment_id, region)` and merged in place: the merge keeps the **largest**
`wanted_bytes`, because a re-grant clearing the biggest shortfall clears the
others, and **sums** `dropped`, because §12 wants the count of lost transitions
and a replace would throw it away.

**The host owes it nothing binding**, exactly as with `ReleaseRequest` (§8.12):
memory policy is the host's (`shared_memory_principles.md` B1) and any
re-grant is clamped by §9's
per-session budget, so a plugin naming more than the budget allows gets what the
budget allows. What the host may not do is ignore it *silently* — a plugin
dropping user-visible frames for want of an arena is a Plugin Manager
diagnostic, which is what §12's count is for.

**Retirement rather than resizing is the point, not a consequence.** A new
`segment_id` makes "the old target is gone" checkable — a descriptor naming the
retired segment is refusable, and the retiring state
(`shared_memory_protocol.md` §9.1) permits finishing
what was started while forbidding anything new. A `RequestSegmentGrowth` message
was considered and **rejected**: naming it growth implies the mapping survives
and old offsets stay meaningful, which is the confusion retirement exists to
prevent. What a host should *do* about a shortfall is §15's.

Two obligations hold regardless of the trigger, and they are the settled part:

- **A plugin that cannot allocate fails its own push and counts it. It does not
  block, and it does not reuse.** §10 forbids a plugin blocking the host;
  nothing forbade the reverse, and an unbounded plugin-side stall on a drag-rate
  path is not better than the defect it replaces.
- **A dropped push of a coalescing kind is still a lost transition unless a
  successor is guaranteed**, which §12 requires be counted and reported —
  see §12's supersession carve-out.

The mechanism and the trigger are specified above; the host's policy for
*deciding* on a shortfall is memory policy rather than protocol (§15).

**The direction is on the record, and it is `shared_memory_protocol.md` §3's.**
A LOANED region the host allocates and one the plugin allocates have opposite
reserve/release roles and *different release messages*, so the message that
installs the region says which it is: **`Region.producer`**
(§3.1 there), required, with `UNSPECIFIED` a grant failure rather than a default.
`producer = HOST` means the consumer releases with `ReleaseDeposit`;
`producer = PLUGIN` means it releases with `ReleaseLoan`. A release naming the
wrong side is a `ContractViolation` — the errant *write* is undetectable, but the
release is not, and it is the one that corrupts an allocator's bookkeeping.

**No other shape makes a plugin→host descriptor path sound.** A Pulsed register (`shared_memory_protocol.md` §6) fits drag-rate
overlays and already serves them, but a panel PNG is neither fixed-size nor
drag-rate; and "the host copies synchronously on receipt" cannot hold when the
receipt is a queue drain the sender cannot observe.

### 5.7 Chunked state: the host names the part, the plugin speaks of it

**Some state is too big to send whole, and §5.4's answer — restate it — is then
no answer.** A dense segmentation of a whole movie is the case:
a label volume is one item on a full-replacement draw list (§8.5), so the unit
of overlay state is the overlay, and a movie's worth of volumes fits no arena.
Volumes fit in memory — a standing commitment of the host — and the time series
of them does not, so the time axis is what gets partitioned, and a plugin speaks
of **one volume at a time**, as state.

Four rules, and together they are the discipline:

1. **The unit is a chunk, and a chunk is coalescing state under its own key.**
   For a chunked overlay (manifest `chunked: true`) the unit is one label
   volume at `(level, t)`, never smaller than a volume; its key is
   `(overlay id, level, t)` (§5.4), and restating a chunk replaces it. An empty
   chunk — no labels — is the empty state, not a removal.
2. **The HOST names the chunks it wants to HOLD** — `ChunksWanted`, the whole
   set per overlay in the host's priority order, the chunks it already holds
   included, restated whenever it changes and empty when nothing is wanted.
   **A plugin serves each want once per continuous membership of the set**,
   applies every restatement as it arrives, and serves a want again when its
   own results change — which the host accepts, because the key is still in
   the set. A key does not leave the set by being held. It is a host→plugin coalescing push and needs no
   queue: the host merges on its own side (one send per change) and the plugin
   keeps the latest. What the host wants is **policy**, not protocol; the
   reference host wants the 2D panes' level-0 volume at the current t, the 3D
   pane's volume at its own level, and one level-0 prefetch in the direction
   the user last scrubbed, capped at §9's eight. A `static` overlay is asked
   once per level, with no `t`.
3. **The key space is therefore the host's own** — §5.4's second, dynamic source
   — and no plugin can exhaust it: a chunk the host did not name in its current
   set is **dropped at receipt and counted**, its loan released (§5.6), because
   a want that left the set while the chunk was on the wire is ordinary, not a
   defect. A chunk the host did name but cannot apply — an overlay not declared
   chunked, a box outside its level's shape, labels that are not u8/u16, a
   descriptor into the wrong segment — is refused on
   `ContractViolation.where = overlay_chunk_refused`, as is an `OverlayUpdate`
   sent to a chunked overlay. The transport's slack for the keys is a constant
   per chunked overlay (`manifest_schema.md` §6), not a data-scaled term.
4. **The bytes ride the overlay's LOANED arena exactly as a label volume's do,
   and the arena is HOST-SIZED FROM THE DATASET.** One level-0 volume of u16
   plus one at the first coarser level, granted when a dataset is open and
   re-granted — superseding the last — when the dataset changes (§8.12); a
   dataset the session ceiling cannot fund leaves the overlay unavailable for
   it, reported in the Plugin Manager and to the plugin as `GrantFailed`, never
   a fault. §9's growth ceiling does not apply to a host-sized arena; the
   session ceiling does. A `static` overlay's arena is the same size.
   **A dataset change restates the set EMPTY before the arena is re-granted,
   and nothing is named until the new arena is INSTALLED at the plugin.** The
   first is what keeps a key served for the old dataset from counting as served
   for the new — a plugin's served-set is keyed `(level, t)` and nothing on a
   `ChunkWant` names the dataset; the second is what keeps a want from being
   served into the arena it replaces while a grant's two halves are still
   landing. Closing the dataset restates the set empty too.

**And one rule that differs from every other push: a chunk the arena cannot
hold is DEFERRED, not failed.** §5.6's *"a plugin that cannot allocate fails its
push and counts it"* is written for an unsolicited push on a fixed arena. A chunk
is solicited — the host asked for this volume and will release the one before it
in the tick that applies it — so the reference SDK keeps the computed answer,
waits for the next `ReleaseLoan`, and retries; it records no shortfall, because
the arena is not short. The plugin never blocks the dispatch path for it: the
serving runs on a thread of its own (`@chunk_source`), one want at a time in the
host's order, and a want that leaves the set is forgotten, one that returns is
served again.

**The host holds exactly what it wants.** A chunk whose want leaves the set is
discarded and asked for again if it returns; a chunk is copied out of the arena
when applied, as an ordinary label volume is, and the loan comes back in the
same tick — so the arena funds one level-0 volume in flight, not one per want,
and a prefetch is served after the release rather than beside the chunk it
follows. Whether a host retains more than it wants is memory policy
(`shared_memory_principles.md` B1) and is not on the wire.

**What is not here, and is not this feature.** A chunked overlay never compiles
into native masks (§8.8): "ROI masks too big to fit in memory" is a scheme of
its own, and the wire that would carry a compile of chunks does not exist. Nor
does a chunk carry a crop — a plugin answers bbox-tight and the host zero-fills
— nor a lattice coarser than the dataset's own levels. Each is a later, additive
change if it is ever needed (§15).

---

## 6. Ordering and concurrency

**Stream order is total per direction and both ends dispatch in it.** That is
free — TCP or a UDS underneath — and most of this document's guarantees rest on
it.

**There is no ordering between this channel and the doorbell**, and none can be
manufactured: they are separate sockets. Every cross-channel rule in the corpus
exists because of that — the epoch is on the record rather than a fence in the
stream, a terminal for an unknown interaction is a no-op, each side cancels
before applying a change rather than waiting for the other's `CANCEL`
(`doorbell_E_input.md` §1a, §2).

**Neither end may run work on the dispatch path.** One slow handler delays
everything behind it, including acknowledgements, faults and the answers other
threads are blocked on. Concretely:

- **The plugin's dispatch loop routes and delegates.** Every handler runs on a
  worker, not inline. A handler blocking in `ctx.host` would otherwise be
  waiting for a reply that the loop it is blocking is the only thing that can
  route, and a reader or export arm run inline holds everything behind it for
  up to its 120 s budget, or with no deadline at all
  (`control_plane_implementation_details.md` §3.1a).
- **Arrangements, one-ways and acknowledgements are routed without waiting on
  any application handler**, and this is a structural requirement rather than
  advice. It is the receiver-side half of §5.1's rule that the *sender* keeps
  them out of the in-flight slot, and alone the sender's half buys nothing: a
  dispatch loop that runs a handler inline re-serialises precisely what the
  sender took care to keep parallel. A grant's install and its acknowledgement
  is the case the memory layer already requires be prompt and bounded
  (`doorbell_M_memory.md` §7).
- **This rule is what makes §9's acknowledgement deadlines derivable**, and the
  dependency runs one way. A deadline on a peer that may run a 120 s export
  chunk ahead of an acknowledgement cannot be shorter than 120 s, which is not a
  deadline; with the rule, the bound is a mapping plus a queue drain and the
  values in §9 follow. An implementation that dispatches inline must therefore
  be read as failing *this* rule, not as evidence that §9's numbers are wrong.
- **The host's drain is bounded and periodic** (§7), and its interval is part of
  the contract because `doorbell_D_notification.md` §5 requires a documented
  finite re-check bound and names this one.

**Concurrency is per class, not per session.** Host requests are one at a time
(§5.1); host one-ways are unordered against them by construction; plugin
requests may be issued concurrently and each reply occupies an independent
deposit, so overlapping reads cannot overwrite one another;
pushes are unbounded in number and bounded in queue.

---

## 7. Queues, bounds and fairness

`plugin_principles.md` 8: where something is nominally unbounded but bounded in
practice, state the bound and what happens when it is exceeded.

Five queues exist per session, and a sixth direction has none by design.

| Queue | Direction | Class | Overflow |
|---|---|---|---|
| host request queue | host→plugin | 5.1 | bounded by the callers waiting on it; an abandoned request is skipped, never sent — see below |
| plugin request queue | plugin→host | 5.3 | answer `host_error`, never drop |
| **host→plugin one-shot push queue** | host→plugin | 5.2 + 5.4 one-shot | never drop; overflow is a session fault |
| **coalescing push queue** | plugin→host | 5.4 coalescing | replace per key; the key space is manifest-bounded, so out-of-keys is unreachable (§5.4); a chunked overlay's chunk keys are the host's own (§5.7) |
| **one-shot push queue** | plugin→host | 5.4 one-shot + 5.5 acks | **never drop; overflow is a session fault** |
| host→plugin coalescing push | host→plugin | 5.7 (`ChunksWanted`) | **no queue at either end**: the host sends once per change, the plugin keeps the latest |

**A REPLY is not on any of them, and that is worth stating because the list
reads as exhaustive.** A reply to a request is matched by correlation id and
delivered to the waiting caller (§4); it never queues behind a push and is never
dropped, because §5.1 makes a lost reply unrecoverable for the session — the
in-flight slot stays occupied and the caller waits out its deadline for an
answer that already exists. So the reply path is *never-drop* by the same
argument as the one-shot queue, and an implementation that put replies on a
lossy or coalescing queue would be trading a session for a byte.

**The host request queue's bound is not what it looks like.** The obvious
reading — one entry per host thread blocked on a reply — is wrong, because **a
caller that times out leaves its entry behind** unless something removes it:
the queued request is sent later and occupies the in-flight slot for a reply
nobody is waiting for. So the depth would track *calls made*, not *callers
waiting*. Two requirements follow, and neither is exotic:

- **the queue has a documented capacity**, and a `call` that cannot be admitted
  fails immediately rather than queueing without limit;
- **an abandoned request is dropped rather than sent.** A caller that has given
  up wants its work cancelled, not delivered late into the slot that the next
  caller needs — and §5.1 already makes a lost reply unrecoverable, so spending
  the slot on one nobody will read is the worst available use of it.

The reference host implements the second (a caller that gives up marks its
request, and the actor skips it), which makes the depth *callers waiting* plus
abandoned entries not yet reached; it enforces no separate capacity.

**The host→plugin direction has a one-shot queue of its own.** `ReleaseLoan`
(§8.7) is a host→plugin push that is neither a reply nor an arrangement. Its
queue is split from the request queue for §5.4's reason — a release must not
queue behind a request, and a request must not be dropped to make room for a
release — and the plugin end needs a dispatcher that routes by kind before it
reaches the reply matcher (§4's classify-by-kind rule;
`control_plane_implementation_details.md` §3.1).

**The one-shot queue's overflow is a fault because there is no honest
alternative.** Dropping an acknowledgement makes an arrangement permanently
pending; dropping a release leaks a reservation for the life of the session;
dropping a fault report loses the reason a session died. If the host cannot
drain one-shot messages fast enough to stay under a bound sized for
session-rate traffic, the session is already broken and saying so is the
proportionate response (`plugin_principles.md` 9).

**The drain is periodic, bounded per session per pass, and its interval is
published.** A per-pass cap is what keeps one plugin from starving the others on
a shared drain; the interval is what
`doorbell_D_notification.md` §5's advisory exemption depends on. Shipping values
are a 40 ms timer and 32 messages per session per tick — about 800 messages per
second per session — and both are named constants rather than incidental.

**The per-pass budget is DIVIDED between the queues, and the one-shot queue is
served first.** A strict "requests before pushes" priority starves the one-shot queue outright: a plugin holding
enough concurrent reads
refills the request queue every tick, the budget is consumed entirely by
requests, and no `ReleaseDeposit`, `GrantAck` or `EpochAck` is ever drained. The
session then fails in the least legible way available: reservations are never
reclaimed, arrangements time out, and a **healthy** session faults on a full
one-shot queue caused by the host not reading it.

Serving one-shot first cannot invert the problem, and the reason is worth
stating because it is not symmetry:

- **One-shot traffic is bounded by work already done.** A `ReleaseDeposit`
  exists because a read was served; an ack exists because an arrangement was
  sent. It cannot exceed the rate of the thing that caused it, so draining it to
  empty is bounded by definition.
- **It is the completion half of the requests it is competing with.** A release
  is what makes the *next* read serviceable, so deferring it behind more reads
  defers the capacity those reads need.
- **A peer fabricating one-shot pushes is an ordinary flood**, answered by the
  per-session share and the queue's own bound, not by priority.

So a pass runs the three queues twice in that priority order: once with each
queue capped at its share (§9), then again uncapped to spend whatever the first
pass left. `plugin_principles.md` 9's allocation rule, applied to three queues
rather than two sessions: **ordinary work must not starve the messages that
finish it.**

Three properties are required of any allocation here, and the obvious
single-pass reading of the paragraph above fails two of them:

- **No pass ends under budget with work still queued.** One-shot capped at its
  share with the other queues empty must not leave half the tick unspent.
- **No queue holding work is served zero.** Two saturated push queues must
  still leave room for a request, because a plugin thread is blocked on each
  one; a saturated request queue must still leave room for the
  acknowledgements that finish those very requests.
- **§9's numbers hold where they were derived**, which was two busy queues.

A single pass with a reserve satisfies the third and neither of the others,
which is why the rule is two passes rather than a priority with a carve-out.

**A per-pass cap must not be able to stall a queue.** If a pass ends at the cap
with messages still queued, the next pass MUST follow without waiting on an
external event. A periodic timer satisfies this; an edge-triggered drain does
not, and that is the same trap `doorbell_protocol.md` §11 names for the unix
reactor.

**Every reporting message is separately rate-bounded**, because the reporting
path must not become the failure path (`control_plane_principles.md` E4).
`ContractViolation`'s bound is specified in
`shared_memory_implementation_details.md` §2 and is a requirement, not an
optimisation: its highest-rate producers are decided per doorbell record, so an
ordering bug would otherwise put ~1 kHz of control-plane traffic on the channel
the session needs in order to recover.

**Fairness is per session.** One session's traffic may not consume another's
share of any host-side drain, queue or worker. A plugin serves one session and
has nothing to be fair between (§11a).

---

## 8. Operations

Every message, by area. **Class** is §5's.

### 8.1 Session

| Message | Direction | Class | Notes |
|---|---|---|---|
| `Hello` | host→plugin | one-way | first message; `id` = `Hello from <version string>` (§3.1) |
| `Ready` | plugin→host | push (one-shot) | first message; `id` = `Ready; I am <version string>` (§3.1) |
| `Shutdown{reason, deadline_ms, detail}` | host→plugin | one-way | sent before every voluntary teardown, and after a lost doorbell — §3.3, §8.13 |

### 8.2 Readers

| Message | Direction | Class | Reply |
|---|---|---|---|
| `OpenReader{uri, reader_id, file_state_dir, series?}` | host→plugin | request | `ReaderOpened` \| `SeriesList` \| `Error` |
| `ReadVolume{handle,t,c,level}` | host→plugin | request | `SegmentRef` \| `Error` |
| `ReadPlane{handle,t,z,c,level}` | host→plugin | request | `SegmentRef` \| `Error` |
| `Metadata{handle}` | host→plugin | request | `MetadataResult` \| `Error` |
| `CloseReader{handle}` | host→plugin | request | `Closed` \| `Error` |

**The exchange — when `G` is granted, what a reply's descriptor carries, levels,
handles, close — is `reader_writer_contract.md` §1's, cited here rather than
restated.** `ReaderOpened.shape_tczyx` is canonical vizi axis order, always 5
elements. `SegmentRef` is a **descriptor** into the reader segment: the host bounds-checks
`len` against `shape_zyx × dtype` and against the segment's capacity before
reinterpreting a single byte (§11). The bytes it names are C-order, X fastest,
little-endian, unpadded — `shared_memory_protocol.md` §2 owns that sentence —
and `ReaderOpened.little_endian` is informational, `true` being the only
conforming value at `1.0`.

`MetadataResult.form` is `"ome-xml" | "ome-ngff" | ""`, and the empty form means
the reader has no document — **not** an empty document.

**`ReaderOpened` carries the calibration the reader KNOWS** — `spacing_z/y/x`,
`spacing_unit`, `time_increment`, `time_unit`, the same shape §8.11's
`Geometry` publishes the other way — and `0` / `""` mean *unknown*, which is
an honest and common answer: a host MUST NOT read the zero as a measurement,
and a reader that knows only some axes leaves the others at zero. A reader
that is the dataset's source is the one end that can know the voxel pitch;
a viewer that does not learn the
pitch shows a six-to-one Z extent as isotropic while looking correct. When a
metadata document (`Metadata`) also carries a pitch and the two disagree, the
fields win — the reader chose them deliberately, and the document was written
by whoever wrote the file. *(The reference host consumes both — `Metadata`
asked once per open, the fields laid over it per axis;
`control_plane_implementation_details.md` §2a.)*

**And the channels' names**: `ReaderOpened.channel_names`, by index, `""`
for a channel the reader does not name, and it may stop short of C — a list
longer than C is malformed, refused by the sender and by the host. By the same
rule as the calibration, a name the reader gives wins over the document's for
that channel, and a channel it leaves `""` keeps the document's. A reader is
the source; what it names is `FILE` everywhere the name travels afterwards
(§8.11).

**`OpenReader` names WHICH reader contribution it is opening**, by the
manifest `id`, and the host is the end that names it. A plugin may declare
several readers, and a message carrying only a URI would make the plugin
re-derive from that string the decision the host had already made in order to
send the message at all. Two implementations agreeing
on a URI-matching rule is an agreement this protocol asks for nowhere else, and
it is one they would reach differently: extension lists, magic bytes and
directory shapes all disagree at the edges, which is where formats overlap.

**Empty means the host named none**, which a plugin declaring exactly one reader
may treat as that one. **A plugin declaring several refuses an empty
`reader_id`** rather than picking, because the ambiguity is the host's to
resolve and a guess is how one SDK opens a file another rejects.

**One file can hold several images, and `OpenReader.series` says which**
— an NWB file's `ImageSeries`, an OME-Zarr's numbered series, an OME-TIFF's
`<Image>`s. The id is the READER's: it gives it, the host stores it and sends
it back, and the host never interprets it, so it can be an HDF5 path, an index
or a member name without the host learning any format's addressing. It is
`optional` because presence is the signal: `""` may be a reader's id.

- **Unset asks the reader to open the file or to list it.** A file holding one
  image opens (`ReaderOpened`). A file holding several answers
  `SeriesList{entries, truncated}`, and **`SeriesList` changes nothing** — like
  `Error`, it leaves the open reader open (`reader_writer_contract.md` §1.2 H2),
  with §1.2's one exception: a listing of the file the open reader holds closed
  that reader first. The host asks the user and repeats `OpenReader` with the
  chosen id. A reader may instead open a default of its own when one is
  honestly the image a user means.
- **Set names one.** An id the reader does not recognise is `NOT_FOUND` — how a
  recent-files entry learns its image is gone. A reader whose files hold one
  image ignores the field.
- **`SeriesEntry{id, label, shape_tczyx, dtype, description}`**: the label is
  what a user is shown (`""` shows the id); shape and dtype may be left empty /
  `DTYPE_UNSPECIFIED` when learning them would mean reading more than a listing
  should.
- **A malformed list is the sender's defect, refused at both ends**: a list with no entries (a file holding no image fails its
  open instead); a `shape_tczyx` that is present but not five positive sizes;
  or **one id listed twice** — the host answers with the id alone, so two
  entries sharing one cannot be told apart. A sender refuses to send one and
  answers the open `INVALID_ARGUMENT`, naming what is wrong; a host that
  receives one anyway fails the open with that reason and does not offer a
  choice. A dtype the receiver does not know is shown as unknown, not refused.
- **A listing is bounded by the message ceiling and nothing else** (§9): a file
  can honestly hold tens of thousands of images — a timelapse unfolded into one
  series per volume — and saying so is the point. A reader with more than one
  message carries sends a prefix, in order, and sets `truncated`. How much room
  to leave for the envelope is the sender's: the reference SDK stops 64 bytes
  short of the ceiling, and any margin that keeps the message inside it
  conforms.
- **`ViewState.dataset_series` is `optional` for the same reason `series` is**:
  absent when the open file holds one image, present — `""`
  included — when a series is open. The identity of an open dataset is the pair
  `(dataset_uri, dataset_series)`, presence included.

**The reader is the host's to choose, from declarations alone.** Nothing is
sent to a plugin to ask whether a file is its own: several plugins probing a
large file on a network filesystem is exactly the cost this protocol does not
impose. A reader that declared a suffix it cannot read after all fails its
`OpenReader`, and the host offers the other candidates
(`manifest_schema.md` §3.1).

### 8.3 Commands

| Message | Direction | Class | Reply |
|---|---|---|---|
| `InvokeCommand{command_id,args}` | host→plugin | request | `CommandDone` \| `Error` |

`args` is `map<string, Value>`; `Value` is a oneof rather than a string map so
that a new kind appends as a field number instead of every plugin hand-parsing
floats.

**`CommandDone.ok` is the outcome and `message` is what the user sees** — shown
on failure, and on success when non-empty. A handler that raises reports
`ok=false` with the exception's text.

**An unsupervised command handler has 10 seconds**, and expiry marks the session suspect (§5.1). **A supervised
one has no deadline**: it reports its progress, the person can cancel it, and
they decide how long it runs (§8.16). A plugin's own request timeouts are
dead-session backstops, not a command budget. Long work that should not hold
the session's slot returns promptly and continues on a plugin-owned thread,
reported as a background task (§8.16).

`CommandDone.result` is reserved and is dropped by the host.

### 8.4 Panels

| Message | Direction | Class | Notes |
|---|---|---|---|
| `PanelPatch{panel_id, ops}` | plugin→host | push (**coalescing**, key = panel+widget, +row for a list's cell) | last-write-wins per property, order-preserving within a widget |
| `PanelOpened{panel_id}` | host→plugin | one-way | a control-plane message, not a doorbell record |
| `GetColormap{name}` | plugin→host | request | `ColormapTable{name, rgba}` \| `host_error` |

`PanelPatch` is the canonical coalescing push, and it is the message that fixes
the coalescing key in both directions at once. Too coarse — per message, or per
panel — and a later patch supersedes updates it never mentioned. Too fine — per
property — and `set_options` followed by `set_value` on a dropdown can be
applied in the wrong order, which is a silently lost
value, because `set_options` clears the combo and restores the prior selection
by text. **(panel, widget), merged per property in place**, is the key that
keeps both (§5.4).

**An op the host does not apply is reported to the producer, per op**: a panel or widget the manifest does not declare, **an image for a
panel that is not open** (its segment is granted when it opens), an image ref
off the panel segment or unreadable, a PNG that does not decode, an op the widget's declared kind does
not take — `ContractViolation.where = panel_patch_refused`, with the panel, the
op, the widget and the rule in `detail`. Per op because each op is its own statement about one widget, so
its neighbours in the same patch still apply. Reporting it is how the plugin
learns why nothing changed.

**Its live key count is bounded by the manifest**, since panels and their
widgets are declared statically and read at scan time — so the queue is sized
before the plugin starts and cannot overflow.

`set_image` additionally carries a **descriptor**, which makes it a loan: the
plugin may not reuse those bytes until the host releases them (§5.6).

**`set_progress` is its own op rather than a `set_value` with a convention.**
A `progress` widget takes a fraction and an *indeterminate* state, and
the second is not a number — encoding it as a sentinel value would make "how
long this takes is not known" indistinguishable from a measurement, which is
`plugin_principles.md` 11's expressible absence applied to a fraction. It is
also why the kind is not interactive: a plugin driving a slider to show
progress would have to bind a command and then receive its own echo on every
step, and there is no way to bind nothing.

`PatchOp.set_image` carries an `ImageRef` into the **panel** segment. A ref
naming any other segment is rejected — images come from the panel segment and
voxels from the reader segment, and the host enforces that rather than trusting
the id.

**`PanelOpened` is a control-plane message.** It needs neither a descriptor
nor interactive latency (`doorbell_principles.md` A1), so it is not a doorbell
record. It is a host→plugin one-way, not an arrangement:
nothing on the fast path references it.

**A host picker's choice arrives like any widget's**
(`manifest_schema.md` §3.3.1): `InvokeCommand.args["value"]` is a channel index
(`Value.i`), a list of them for a multi-select channel picker (`Value.ints`),
`"#RRGGBB"`, a colormap name, or a region id with `args["name"]` beside
it. **`GetColormap{name}` returns the table vizi renders a colormap with** —
256 RGBA entries of u8, 1024 bytes, built-in and user colormaps alike — so a
plugin drawing its own image with a picked colormap colours it as vizi would;
an unknown name is `host_error{NOT_FOUND}`. It needs no permission and no open
dataset: a colormap is not data.

**State patched before a panel opens is kept and shown when it opens.**
The last statement is the current
state, here as for every coalescing push, so the host keeps the latest op per
(widget, property) for a declared panel that is not open and applies them when
it opens — options first, so a kept value is set against the latest options.
What is kept is bounded by the panel's declared widgets and, for a list's cells,
by the rows the list can have: a held `set_rows` drops what was held for
rows it no longer names, a cell for a row it does not name is refused, and cells
sent before any `set_rows` are held for at most `max_rows` rows. **The one exception is
an image**: the panel's segment is granted when the panel opens, so no image
can yet be in it, and the host refuses one with a reason. **So an SDK SHOULD
hold an image pushed before its panel's segment is granted** -- the latest per
(widget, row), dropping a row's when a `set_rows` removes it -- and send it
when the panel's segment is **installed**, rather than fail the push. Not on
`PanelOpened`: that can arrive before the segment does, and a session
relaunched with its panel already open is granted the segment and may never
hear one (the host does send `PanelOpened` after a relaunch, but
as a notification, not as the trigger). Holding is better than failing:
otherwise every plugin has to track which of its panels are open, and one that
does not reports a failure for a push the person simply had not looked at yet.
The reference SDK does. A plugin needs no mirror of its own panel and need not
re-send on `PanelOpened`, which still arrives on every open as a notification. **Where an open panel does matter is a plugin moving the
person's view** (set-view, §8.15): the panel is how the person sees who is
moving it, and that rule is set-view's to state.

**A `list` widget's rows come from the plugin** (`manifest_schema.md`
§3.3.1).

- **`PatchOp.set_rows{keys}`** on the list's id gives the whole ordered key
  list and replaces the last, like every patch. Order is display order. A new
  key's row starts from the template's declared values; a key that leaves drops
  its row and whatever it showed, and one that comes back starts fresh. Keys
  are non-empty, distinct and at most 128 characters; more keys than the list's
  `max_rows` is **refused whole, never truncated** (`panel_patch_refused`).
  **A manifest bounds what its lists may build**: one
  list's `max_rows` × its row's cells, and all of one panel's lists together,
  are each at most 4096 widgets; a manifest past either is not loadable.  The
  host builds every row a patch asks for on its UI thread, and one patch — or
  the replay of patches held for a closed panel — may fill every list at once.
- **`PatchOp.row_key` addresses one row's cell**: `widget_id` names the template
  widget, `row_key` the row. A cell op naming a key the list does not hold is
  refused with that reason, as is a `row_key` on a widget that is not a list's
  cell, or a cell op without one. **The coalescing key is (panel, widget, row)**,
  and it stays structural: the manifest's `max_rows` bounds it, so the host
  sizes the queue before the plugin starts (§5.4).
  A coalescer that lets a newer `set_rows` replace a pending one in place
  drains it ahead of cell ops queued since, so it **SHOULD drop pending cell
  ops for the rows the newer `set_rows` removes** — sent, they would reach
  the host after their row is gone and be refused for nothing the plugin did.
- **Events from a row carry `args["row"] = key`** beside the widget's usual
  value, so one command serves every row. A `selectable` list fires its own
  command with `{"row": key}` when the person selects a row, and takes
  `set_value` (the key, or `""` for none) to set the selection. It is not vizi's
  region selection; tying the two is set-view's to decide.
- Rows are panel state: set before the panel opens, they are kept and shown
  when it opens, as any patch is.

**Closing a panel's dock hides it; it does not end the session.** The
open panel holds a lease on its plugin until the plugin is disabled or vizi
exits, so a plugin keeps its process, its background work and everything it has
computed while its dock is closed, and reopening the dock (its entry in the
Tools menu, or in the View menu) sends `PanelOpened` again, with the panel's
view switch off (§8.15 rule 3). A plugin with work that must outlive its panel needs
nothing more than an open panel.

### 8.5 Overlays

| Message | Direction | Class | Notes |
|---|---|---|---|
| `OverlayUpdate{overlay_id, items, revision}` | plugin→host | push (**coalescing**) | full replacement per id |
| `OverlayRemove{overlay_id, revision}` | plugin→host | push (**one-shot**) | see below; on a chunked overlay it clears the chunks and the table |
| `ChunksWanted{overlay_id, wants[{level, t?}]}` | host→plugin | push (**coalescing**, §5.7) | the whole set, in the host's order; ≤ 8 |
| `OverlayChunk{overlay_id, level, t?, labels, position_zyx}` | plugin→host | push (**coalescing** per `(overlay id, level, t)`) | one bbox-tight volume on level L's lattice; absent labels = empty |
| `OverlayLabelTable{overlay_id, colormap, label_names, label_visible, style}` | plugin→host | push (**coalescing** per overlay) | what a label volume carries per item, once per overlay |

`OverlayUpdate` replaces an overlay id's content wholly, and `revision` is
monotonic per overlay **across both lanes** — the live shared-memory register
and this channel — so the host can arbitrate a live update against a control-
plane push without a cross-channel ordering assumption.

**Bulk overlay geometry is a loan too** (§5.6), exactly as `set_image`'s bytes
are. The live lane is different: a Pulsed register has a handoff
discipline in the memory itself (`shared_memory_protocol.md` §6).

**A push the host does not apply is reported, not just logged.** An item the
wire vocabulary refuses (a bad shape, an unknown frame, explicit mesh
vertices) or a label volume that is not a voxel selection (§8.8) refuses the
whole `OverlayUpdate` — a full replacement applied in part would show a state
the plugin never sent — and the host sends `ContractViolation.where =
overlay_item_refused` with the overlay, the item and the rule in `detail`,
so the author of a malformed item learns why it never appeared.

**`OverlayRemove` is one-shot.** A
plugin retracting an overlay because it has *finished* will never send another
message about it, so a dropped `OverlayRemove` leaves a stale overlay on screen
until session teardown. It belongs on the one-shot queue (§7). As region state
it would be loss-proof, `D`-accelerated and poll-backstopped
(`doorbell_D_notification.md` §6).

Geometry rides the overlay segment or inline bytes for small payloads; all
overlay geometry is ZYX f64 (`vizi_plugin.proto`).

**Five geometry arms, and where each is drawn.** `OverlayItem.geometry` is
`line_set`, `marker`, `text`, `mesh` or `label_volume`; the IDL comment on
each is its contract (the frame of its points, what `size_px` and `scale`
mean, what is deferred). The reference host draws all five in the **3D**
pane and, in the **2D** panes, `label_volume`, `line_set` and `marker` —
composited in that order, each over the one before — while `text` and
`mesh` are 3D chrome there, skipped rather than approximated. The reference
SDK builds every arm (`marker_item`, `line_item`, `text_item`, `mesh_item`,
`label_volume_item`).

**A chunked overlay is a time series of label volumes, spoken of one at a
time** (§5.7, manifest `chunked: true`). It takes `OverlayChunk` and
`OverlayLabelTable` only — an `OverlayUpdate` sent to one is refused on
`overlay_chunk_refused` — and its chunks are on the **wanted level's lattice**:
`position_zyx` is in level-L voxel indices and the box lies inside level L's
shape, which is the coarser lattice a label volume cannot express, legal here
because the host chose the level. The reference host draws a chunk at level L
in the 3D pane only while that pane draws level L, handing it over undecimated,
and in the 2D panes only when L is 0; until the table arrives it draws chunks
with a default LUT and no names. `label_names` and `label_visible` are keyed by
label id and `colormap` follows §8.8's LUT rule; labels are u8 or u16 only, so
more than 2¹⁶ ids is refused and the table always fits §9's bound.

**A line in 2D is projected and dashed, never sectioned.** The
section of a curve by a plane is a point, so a polyline straddling the slice
has no section to draw; the host does what it does for its own region
outlines out of their Z extent — every segment is drawn where it projects,
**solid when both endpoints round into the slice, dashed (by arc length,
6 on / 4 off voxels) otherwise**. A lasso traced in the pane is all solid; a
3D path shows its in-plane run solid and the rest dashed, in every slice.
**`topology` is `""` (or `"polyline"`) for an open polyline through every
vertex in order, `"loop"` to close it, and `"segments"` for independent vertex
pairs**. vizi v0.9.0 draws `""` as pairs and refuses an explicit
`"polyline"`, so a plugin that must draw pairs on every `1.0` host says
`"segments"`. Per-vertex colours are honoured, a dash taking
its segment's start-vertex colour; the transform's `position_zyx` places the
item, and rotation and scale do not apply to a line set or a marker in
either pane. **In the 2D panes a screen pixel is a voxel**: the layer lives
on the level-0 lattice and follows the pane's zoom, so `size_px` is a
diameter in voxels and `thickness` a width in voxels there, whereas the 3D
pane draws both as screen pixels — the one place the two panes read a
pixel field differently, stated here rather than discovered. A line thinner
than a voxel is drawn one voxel wide.

### 8.6 Exporters

| Message | Direction | Class | Reply |
|---|---|---|---|
| `OpenExport{exporter_id, locator, shape, dtype, source_metadata, spacing, time, file_state_dir, filter?, channels, provenance}` | host→plugin | request | `ExportOpened` \| `Error` |
| `ExportChunk{handle, t, channels, source, filtered_channels}` | host→plugin | request | `ChunkWritten` \| `Error` |
| `FinishExport{handle}` | host→plugin | request | `ExportDone` \| `Error` |
| `AbortExport{handle}` | host→plugin | request | `ExportDone` \| `Error` |

The write-side inverse of a reader: the **host** writes voxels into the export
segment and sends a descriptor; the plugin reads them and writes its artifact.
**The exchange — when `X` is granted, a chunk's layout and channel order, the
reply as the handoff, finish and abort — is `reader_writer_contract.md` §2's.**

**`OpenExport` carries the calibration the host KNOWS**, in `ReaderOpened`'s
exact fields (§8.2: `spacing_z/y/x`, `spacing_unit`, `time_increment`,
`time_unit`; `0` / `""` unknown). An exporter writes a measurement it was
given and never one it was not — an artifact that claims 1 µm for an
uncalibrated source is a false claim the next reader will believe. The reference host fills these from the
open dataset's parsed metadata, per axis, and leaves the rest unknown.

**And it says what the artifact was derived from**, in three fields
that answer two different questions:

- **What the artifact IS** — `shape_tczyx`, the calibration above, and
  `channels`: the artifact's channels in its C order, each as the SOURCE
  channel `index` with the name the person sees and its `source` (§8.11). An
  exporter writes a channel's name into the artifact only when `source` is
  `FILE` or `USER`; a `PLACEHOLDER` written there would read back, from the
  artifact, as a name the file gave.
- **What it CAME FROM** — `source_metadata`, the source's own document as its
  reader gave it, and `provenance`, the host's record as JSON: the same record
  vizi embeds in its own exports, naming the source and its
  fingerprint, the selection with its channel names and the crop in voxels and
  physical units, the filter and projection applied, and the host. An
  exporter SHOULD embed the record where its format keeps extra metadata —
  vizi puts it under `vizi:provenance` in an OME-Zarr's attributes and in an
  XML annotation in OME-XML — and MAY embed the source document beside it.

**The source document describes the SOURCE, never the artifact**, and an
exporter MUST NOT write it as the artifact's own description: its sizes,
channel list and pixel sizes are the source's, and an export can hold a subset
of the channels and timepoints, a crop, a projection or a filter's output.
Neither OME-XML nor OME-NGFF has a general "derived from" field (NGFF's
`image-label.source` links a label image to its image, nothing wider), which is
why the relationship is carried as provenance and not as a description.
**`provenance` is built at open**: which timepoints of a filtered export
went out unfiltered is each chunk's `filtered_channels`, not the record.

**And it says whether the voxels are a filter's output.**
`OpenExport.filter` is the `AppliedFilter` the export reads through — present
when the person exporting chose a filter's output, absent when the export is
the file's own voxels. It names the filter, not a promise about every chunk.
**By default the host holds every timepoint to the filter**: one the
stage refused, failed or ran out of time on is asked again once, and if it
still is not processed the host **aborts the export** (`AbortExport`) rather
than write it. Only when the person exporting chose *Write unfiltered data if
filter fails to run* is such a timepoint exported as the file has it. So
**each chunk's `filtered_channels` lists which of its `channels` are the
filter's output**, and every other channel in it is the file's own. An
exporter that records provenance records it from the chunks, not from the
open. The fields are §8.7's; see there.

**A chunk that contradicts its export is a host error, and the plugin refuses
it**: `filtered_channels` non-empty when `OpenExport.filter` is
absent, or naming a channel not in the chunk's `channels`. The plugin answers
that chunk `Error{INVALID_ARGUMENT}` naming the contradiction, and writes
nothing from it, which ends the export: an artifact whose provenance the host
contradicted is worse than none. The reference host never sends one.

**`ChunkWritten.total_bytes` is authoritative** — ingest is a hint, committed is
truth. **`AbortExport` before `FinishExport` means the partial artifact is
abandoned** and the plugin cleans it up; it never silently finalises. **The host
sends it however an export ends early**: the person's Cancel, a chunk the plugin
answers `Error{CANCELLED}` (its activity row's Cancel, §8.16), a chunk refused or
failed, or a read on the host's side that failed. So an export that does not
finish leaves no partial file from a conforming exporter.

**`AbortExport` replies `ExportDone`, and "no reply" is not an option for it.**
It is a request, so it holds the single in-flight slot (§5.1) — a plugin that
answered nothing on a successful abort would wedge every later request on that
session until the 10-second timeout, and the timeout marks the session suspect.
The SDK returns `ExportDone` so the host's id-matched call completes, and the
host requires exactly that kind. An abort that fails still replies —
the artifact is abandoned either way, so `Error` here means the *handle* was
unknown, not that cleanup went badly.

Shipping timeouts: open 30 s, chunk 120 s, finish 60 s, abort 10 s. Each is a dead-session backstop and each marks the session
suspect on expiry (§5.1). **A chunk the person can see has no deadline**
(§8.16): the reference host shows each chunk in its activity list —
*Exporting timepoint T, channel C* — with the exporter's progress on it and
Cancel, so the 120 s applies only where nobody is watching.

### 8.7 Host data access

Gated on the manifest's `data_access` permission, which the host enforces.

| Message | Direction | Class | Reply |
|---|---|---|---|
| `GetViewState` | plugin→host | request | `ViewState` \| `host_error` |
| `ReadDataVolume{t,c,level,roi,raw}` | plugin→host | request | `DataVolume` \| `host_error` |
| `ReadMaskVoxels{region_id}` | plugin→host | request | `MaskVoxels` \| `host_error` |
| `ReadRoiInclusion{region_id, t, roi_min_zyx?, roi_max_zyx?}` | plugin→host | request | `RoiInclusion` \| `host_error` |
| `ReleaseDeposit{segment_id, region, offset, len}` | plugin→host | push (**one-shot**) | names its `region` |
| `ReleaseLoan{segment_id, region, offset, len}` | host→plugin | push (**one-shot**) | the mirror, §5.6 |
| `ViewChanged{open_generation, t, z, visible_channels, selected_region_id, filter?}` | host→plugin | push (**coalescing**) | subscription `view`; `filter` as `ViewState.filter`, so applying or removing one is heard |
| `RegionsChanged{open_generation, reset, changes[{region_id, revision, origin, deleted}]}` | host→plugin | push (**coalescing**) | subscription `regions` |

`ViewState` reports the **requested** navigation state — what the user last set
— not what happens to be decoded, so it is deterministic under scrubbing. Its
crop box is a render clip: reads always return uncropped data and the plugin
applies the box itself if it wants to. `open_generation` changes on every
successful open and is the u64 whose low 32 bits the doorbell's `gen` carries
(`doorbell_E_input.md` §1). `file_state_dir` is this plugin's folder for the
open file (§2.3).

**`ViewState.c` is DEPRECATED and always 0; `visible_channels` is what the
user sees.** There is no C picker and no active channel in vizi, so
nothing can move `c`: it is zeroed on every open. `visible_channels` is the set enabled for display,
ascending, one entry under a solo. It is **host-wide, not per pane** — this
message carries no view id — and the 3D pane composites at most 8 channels
(`_NATIVE_MC_MAX`, logged when it truncates), so past eight it shows a subset
of this set while the 2D panes show all of it. This message has no per-pane
field: the plugin knows the user's set, not the pane's. Unlike the `E` record's 16-bit mask
(`doorbell_E_input.md` §1), which names channels ≥ 15 only through the pane
epoch's `channels_high`. It is polled like the rest of this message and can be
a tick stale, so a **tool** reads the epoch, which is exact to the record; a
plugin with no tool — a panel, a reader, an exporter — reads this, because
**`EpochChange` is arranged only for sessions that declare a tool**: the epoch
exists to make `E` records interpretable, and a session that will never
receive one has nothing for it to gate. That split is the rule, not a gap.

**And the fallback carries the pitch.** `ViewState` has
`spacing_zyx`, `spacing_unit` and `spacing_provenance`:
host-wide, polled like the rest of this message and a tick stale like the rest
of it, and the empty unit with `DISPLAY_FALLBACK` meaning *we do not know how
big a voxel is* — which a plugin MUST NOT read as a measurement.

**`ViewState`'s pitch is per axis, and an unknown axis is NaN.** A file
may record some axes and not others — XY in µm, Z not at all — and a plugin
measuring with it must scale what is known and leave the rest in voxels. So
when **some** axes are recorded, `spacing_zyx` carries those in `spacing_unit`
and **NaN** for each axis that is not, with the file's provenance (`FILE`). NaN
appears only then. With every axis recorded it is the measurement;
with none, the display fallback. A plugin reads an axis as measured
only when the unit is non-empty **and** that axis is finite.
**`Geometry` is deliberately different:** a tool rebuilds physical space from
its spacing — angles, a fronto-parallel plane, the camera's up direction (E
§3) — so it keeps the finite, display-consistent pitch, and a partly recorded
file reaches a tool as `DISPLAY_FALLBACK`. Both say the same thing when every
axis or no axis is known; they differ only for a partly recorded file, by
design. A tool-less plugin doing an anisotropic fit reads the pitch here
rather than asking a person to type three numbers, which would be a pitch with
**no provenance at all** — precisely what `SpacingProvenance` exists to
prevent.

**And the channels' names, for the same reason.** `ViewState.channels`
is every channel of the open dataset, in index order, as `ChannelInfo` —
exactly what `Geometry.channels` carries (§8.11): the name the person sees and
where it came from, so a panel plugin labelling a trace can tell the file's
`"GCaMP"` from a host's padding.

**Where the voxels came from.** With a processing stage applied as a
filter (`processing_stages.md`), data access still serves the unprocessed
source — unless the person has checked *Plugins See Filtered Data*, when a
read of a channel the stage rewrites is the stage's output. A plugin must be
able to tell which it got, or it cannot say what it analysed, and a plugin whose
own stage is the filter would analyse its own output and report nothing wrong:
a stabilizer tracking stabilized data finds corrections near zero. So:

- **`DataVolume.filter`** is the `AppliedFilter` these voxels passed through
  — plugin id and version, stage id, every param as the stage received it, and
  the channels the stage rewrites. **Absent means the file's own voxels.** It
  describes THIS reply, not the session: a channel the stage does not rewrite,
  a pyramid level above 0 (a stage runs at level 0 only, §1), and a timepoint
  the stage refused, failed or ran out of time on are all the file's own, and
  carry none even while the filter is shared.
- **`ViewState.filter`** is the filter applied to what the views **show**,
  absent when none. It is **session-level**: it names the filter even at a
  timepoint the stage refused, which the views show unfiltered, so it is not
  a claim about any voxel. It does not say what a read returns — reads are the
  source unless shared — and a plugin never infers one from the other.
- **`ReadDataVolume.raw`** asks for the file's own voxels whatever the person
  shares: a plugin that must measure the source — a quantification
  plugin recording the view's filter as provenance, never measuring through it
  — reads with `raw` and gets the source with **no** `DataVolume.filter`, and
  learns what the views show from `ViewState.filter`.
- **`OpenExport.filter`** and **`ExportChunk.filtered_channels`**, §8.6.

A plugin that must not analyse its own output compares `plugin_id` with its
own and refuses; one that records provenance records the whole message — the
reply's, never `ViewState`'s. The host never claims, for a read, a filter the
voxels did not pass through: a false claim is
worse than none, because it is believed.

**Subscribed state pushes: a plugin hears about changes instead of polling
for them.** A panel plugin that re-measures when regions change, or
follows the frame the person is on, could otherwise only poll `ViewState` and
`GetRois` — and `GetRois` returns every region with its whole pose table, which on a
tracked dataset is prohibitive several times a second just to learn nothing
changed. Two manifest subscriptions (`manifest_schema.md` §5.1), each needing
`data_access`:

- **`view` → `ViewChanged`**: the open generation (0 when nothing is open, a
  new value for a new dataset, even the same file opened again — which is how
  a plugin hears of opens and closes), `t`, `z`, the visible channels, the
  selected region and the applied filter, as `ViewState` has them. A close is
  a push at generation 0 with nothing else set.
- **`regions` → `RegionsChanged`**: which regions were created, edited or
  deleted, by id, with the region's `revision` and `origin` — **never their
  geometry**, which `GetRois{region_ids}` reads for just those regions. Every
  change is reported, the plugin's own contributions included; `origin` says
  whose each is. `reset` marks the session's first push and a dataset change:
  `changes` is then every region there is, and anything held before is void.

**Each is state, not an event stream.** The host sends the first when the
session starts and another only when something changed, at most once per pump
tick (40 ms), never from the paths that move the view — so a scrub or a drag
costs at most one message per tick, and a push the plugin has not handled yet
is simply superseded (§5.7's coalescing push). The host side is bounded the
same way: a `ViewChanged` the session has not yet written to the stream is
replaced by the next, so a plugin that stops reading holds at most one.
`RegionsChanged` is never replaced — each lists different
regions — so it grows only with editing, not with playback. `RegionsChanged` lists what
changed since the last push the host SENT; a plugin that folds pushes merges
them by region, **the newer push's entry winning** — a deletion included, whose
`revision` is 0 and would lose a comparison of revisions (the
reference SDK merges in arrival order). **A revision
changes exactly when its region does, and is never reused**; `RoiRecord.revision`
carries the same number, so comparing the two tells a plugin its copy is
current. Mid-drag reshapes are not changes: a region is reported when the edit
is committed.

**Which region is selected, and which region is which across sessions.**
`ViewState.selected_region_id` / `ViewChanged.selected_region_id` name
the region the person has selected — the Regions panel's current row, which a
pick in the 3D view also sets — so a plugin that shows data about regions shows
the right one. **A plugin keeps its own results, and keys them on each region's
`name` and `RoiRecord.content_hash`**: a digest of what decides the region's
voxels (its geometry and placement, and its parents'), which a rename, a
recolour or a visibility toggle does not change. `revision` answers "did this
change since I last looked" within a session; `content_hash` answers "is this
still the region I computed from" across sessions, which is what saved results
need. The host holds no plugin's results, and does not need to: a plugin is
responsible for what it computes.

**And the frame interval**: `ViewState.time_increment` / `time_unit`,
`ReaderOpened`'s fields with its rule — `0` / `""` unknown, never a measurement.
It is the uniform interval `TimeMapping` is built from, so a panel plugin can
write time in seconds without a tool; a tool reads the arranged mapping, which
can also say a non-uniform one.

**And a session with no tool may ASK for the epoch** — `subscriptions:
["epochs"]` in the manifest (`manifest_schema.md` §5.1). The rule above
and its default stand; the subscription makes the inference a declaration.
What a subscriber hears is what the
epoch stands for: a pane's geometry — extent, crop, pitch, levels — its visible
channels, and the time mapping.

**The subscription does NOT deliver the time slider.** The epoch excludes the
current `t` by design (a slider drag would otherwise burn an epoch and a round
trip per frame), and no field of `EpochChange` carries it. A tool session hears
`t` in each `E` record, not from the epoch. A tool-less plugin drawing a
per-frame overlay still reads `t` from `ViewState`, a tick behind whatever the
person just did with the slider. A pushed
displayed-frame notice would be a separate subscription name, and none exists
at `1.0`.

A subscriber is an ordinary participant in §8.10 from then on: it receives
`TimeMapping` first, must `EpochAck`, and is escalated if it does not — which
is exactly why this is opt-in and not implied by a permission. **A tool session
is on the chain whether or not it declares the subscription**, because its `E`
records are uninterpretable without an epoch.

**Not provided:** a plugin-provided dataset with no file behind it. A reader serving a synthetic URI is the
workaround that fits the current model.

`ReadDataVolume`'s ROI is in the **requested level's** coordinates, min
inclusive and max exclusive, and the reply's `shape_zyx` is the ROI's shape.

**`ReadRoiInclusion` asks the host which voxels a region covers at frame `t`**,
for any region `GetRois` lists. `ReadMaskVoxels` answers a mask alone
and takes no frame; for a box, ellipsoid, cylinder, polygon or mesh a plugin
measuring per-region signal would otherwise re-implement the host's rasteriser
— the shape, its inclusion mode, the pose at `t`, the parent chain, the pitch —
and each of those is a place to disagree with what vizi draws and measures. The
host answers with **its own measurement's computation**: the region posed at
`t` through its parent chain (`roi.proto`'s `RoiBehavior.VoxelInclusion`
contract), at level 0, clipped to the dataset. A mask answers its stored
membership, which the host holds static across `t`.

**An optional box clips the answer further**: `roi_min_zyx`/`roi_max_zyx`,
level 0, min inclusive and max exclusive, exactly as `ReadDataVolume`'s, both
empty for the whole dataset. A tracker watching part of a large region asks for
that part, and the host builds only that part.

The reply is `MaskVoxels`'s shape: a bbox-local U8 (0/1) volume through the
data segment, `segment.shape_zyx` its shape and `origin_zyx` its level-0 corner,
released like every deposit (§5.6). It carries `t`, `present` and
`voxel_count` besides:

| Outcome | Reply |
|---|---|
| the region exists at `t` and covers voxels | `present`, `voxel_count` > 0, a `segment` |
| it exists and covers no voxel of the dataset | `present`, `voxel_count` 0, **no** segment |
| a segment of its domain hides it at `t` | `present` false, no segment — a state, not an error |
| no such region, or one outside the trust surface (a viewpoint) | `host_error` `NOT_FOUND` |
| `t` outside the dataset's frames | `host_error` `OUT_OF_BOUNDS` |
| a box that is empty or leaves the dataset | `host_error` `OUT_OF_BOUNDS` |
| a box with other than 3 values at either end | `host_error` `INVALID_ARGUMENT` |
| a region whose clipped box exceeds the host's voxel budget | `host_error` `OUT_OF_BOUNDS` |
| an inclusion mode the host does not rasterize (`SHELL`, `POINTS`) | `host_error` `UNSUPPORTED` |

**One frame per request, deliberately.** A span would put several deposits in
one reply, each its own §5.6 loan, for a saving a tracker does not need: the
request is small, and a tracker iterating frames already asks per frame for the
voxels it measures (`ReadDataVolume`).

**`ReleaseLoan` is its mirror**: host→plugin, naming a
reservation the *plugin* allocated and **the host is done with** — applied,
superseded on the coalescing queue, or rejected, all three of which **decrement**
the reservation's declared `uses`, with the release following at zero (§5.6's
disposition table). Same one-shot class, same reasoning — losing one
leaks the plugin's own capacity until teardown.

**It is one-shot even though the message it returns is coalescing**, which is
the point worth stating: a release is a transition nothing restates, so it may
not sit on the queue whose policy is to replace. Two loans on one key produce
two releases, in order, and the second push's descriptor is not a supersession
of the first push's *bytes*.

**`ReleaseDeposit` is one-shot.** Losing it leaks ring capacity until teardown
but can never permit reuse of live bytes, so it is safe against corruption and
unsafe against exhaustion — which is exactly a one-shot push and exactly why it
must not sit on the coalescing queue. Its `region` field
(`shared_memory_implementation_details.md` §2) defaults to 0, which
is the one-ring-per-segment case.

### 8.8 Native ROIs and label volumes

Gated on `contribute_rois`.

| Message | Direction | Class | Reply |
|---|---|---|---|
| `ContributeRois{rois, replace_all}` | plugin→host | request | `RoisContributed` \| `host_error` |
| `GetRois{pose_t_start?, pose_t_end?}` | plugin→host | request | `RoiList` \| `host_error` — optional pose window; a reply past the message ceiling is refused `OUT_OF_BOUNDS` |
| `ExtendRoiPoses{extensions}` | plugin→host | request | `RoiPosesExtended` \| `host_error` |
| `CompileLabelVolume{overlay_id, item, replace_all}` | plugin→host | request | `LabelVolumeCompiled` \| `host_error` |
| `shape_drag: ShapeEdit{region_id, contrib_id, type_name, drag_id, seq, move}` | host→plugin | push (**coalescing**, key `drag_id`) | answered by `shape_proposal`, or not at all |
| `shape_proposal: ShapeResult{drag_id, seq, positions, resolution, message}` | plugin→host | push (**coalescing**, key `drag_id`) | |
| `shape_commit: ShapeEdit{…, seq = 0}` | host→plugin | push (**one-shot**) | answered by `shape_answer` |
| `shape_answer: ShapeResult{drag_id, seq = 0, …}` | plugin→host | push (**one-shot**) | |
| `ShapeEditEnded{drag_id, outcome, detail}` | host→plugin | push (**one-shot**) | |

> **A host older than vizi v0.9.4 does not implement** the two subsections at
> the end of this section — *What a contributed region may be* and *Region
> types a plugin shapes* — or the five `shape_*` / `ShapeEditEnded` rows above.
> Such a host refuses every `type_name` but `box`,
> `ellipsoid`, `cylinder` and `surfq6` (`INVALID_ARGUMENT`), never sends a
> `ShapeEdit`, and leaves `type_owner` empty.

`contrib_id` is stable only inside the contributing plugin's namespace:
re-emitting one replaces that plugin's region and can never name a user region
or another plugin's contribution. `replace_all` additionally removes this
plugin's prior regions absent from the batch.

**`replace_all` may be true on the FIRST batch alone.** A contribution too
large for one message is split by region count, and since `replace_all` removes
every id absent from *that batch*, setting it on each one silently keeps only
the last. Nothing reports this: each batch succeeds, and the count in
`RoisContributed` is the batch's. Said here because a plugin working within
§9's ceiling has to batch, and this is the trap on the way.

#### A pose domain that grows

`ContributeRois` carries the whole table, and `Roi`'s pose is **dense** —
`roi.proto` makes it one row per frame — so a tracker that extends its regions
as it goes re-sends every pose of every region on every contribution, which is
quadratic in the frames tracked. **`ExtendRoiPoses` is the mechanism for
that case.**

An extension names one region by `contrib_id` and carries `[t_start, t_start +
span - 1]` of dense rows. One outcome per failure, and none of them mutates
anything — the whole batch is checked against the store before the first write:

| Condition | Answer |
|---|---|
| `contrib_id` names no region of this plugin's | `NOT_FOUND` |
| a gap between the extension and the region's domain | `INVALID_ARGUMENT` |
| the components present ≠ the components the region carries | `INVALID_ARGUMENT` |
| lengths not `3*span` / `3*span` / `6*span` | `INVALID_ARGUMENT` |
| the region has no pose domain yet | `INVALID_ARGUMENT` |
| two extensions for one region in one batch | `INVALID_ARGUMENT` |
| any frame outside `[0, T)` of the open dataset | `INVALID_ARGUMENT` |
| a NaN or infinite value in any row | `INVALID_ARGUMENT` |

`ContributeRois` refuses a NaN or infinite position, direction, pose row or
frame basis the same way, and a pose domain outside `[0, T)`. Stored, one poisons every bound, raster and
measurement the region takes part in, long after the request was answered
success.

**Overlap replaces**; a re-tracked stretch is an ordinary correction. **A gap is
refused** because the pose is dense by contract: filling one with zeros would
mint frames nothing defines, and reading them back would be indistinguishable
from a measurement — `plugin_principles.md` 11 again. The components rule keeps
*the creator fills every frame* true per component, so a reader can index a row
without first asking whether it exists. A region with no domain is started with
`ContributeRois`, not here.

**The read side.** `Roi`'s pose is dense and `RoiList` carries every region in
one message, but `GetRois` returns one `Roi` per region **posed at the frame on
screen, with its pose cleared**, so an unwindowed reply is small.

`GetRois` takes an optional window. With neither bound the reply is as above.
With one it carries the **rest** form instead, its pose rows clipped
to the window, and `Roi.t_start`/`t_end` then describe the rows returned —
they must, since the density rule is checked against them. **Every `RoiRecord`
of a region that has a pose domain carries it** in `pose_domain_t_start`/`_t_end`,
windowed or not, and a region with no domain carries neither. A record
holds the whole pose **exactly when its domain equals `Roi.t_start`/`t_end`**;
that comparison, not the pair's presence, is the "you hold a slice" test.
**An absent `Roi.t_start`/`t_end` means the rows were cleared, never "the whole
domain"**: an unwindowed record is posed at the frame on screen and carries no
rows, so it does not hold the whole pose, and contributing it back would wipe
the pose.
A region vizi holds as several stitched segments (a `RoiSeq`, which vizi's own
editing can produce) has no single dense pose to carry: it answers a windowed
read in the same posed form, rows absent, with its domain spanning every
segment.
Without the pair a read-modify-write truncates in silence: contributing a
windowed `Roi` back would drop every frame outside the window and nothing in
the message would have said so.

**A plugin sizes a window before it asks for one.** An unwindowed `GetRois` is
small — its rows are cleared — and names every region's domain, so a plugin
knows how many frames it is about to request. It has to: a windowed reply is
where §9's message ceiling is reachable (96 bytes per fully posed ROI-frame, so
360 regions over 970 frames is already 32 MiB). **A reply that would exceed the
ceiling is refused, never clipped**: the host answers `host_error`
`OUT_OF_BOUNDS` naming the reply's size and the limit, and the plugin asks
again with a narrower window. A clipped reply would be the truncation above one
level up — rows `[a, m]` handed back for a request for `[a, b]` — and sending
the reply whole would end the session at the receiver (§9's row).

`CompileLabelVolume` repeats the small descriptor deliberately — plugin requests
drain ahead of pushes (§5.3), so the item cannot be assumed to have arrived by
an earlier `OverlayUpdate`. No voxel bytes are re-transferred. **The repeat is a
second use of the item's loans, and the sender declares it** — `SegmentRef.uses
= 2` on the labels and the LUT, in both messages (§5.6). Left at one, the host
releases after whichever message it finishes first, and the other then names
bytes the plugin's arena is free to hand out again; the reference SDK's
`label_volume_item(..., uses=2)` is the declaration. **`uses = 2` is for a push
whose compile is certain.** A use once declared cannot be withdrawn — nothing
in the vocabulary says *the remaining uses will not come*, and an
`OverlayUpdate` that drops the item cannot mean it, because a compile does not
consult the draw list — so a push whose compile may never come (a tool that
pushes per gesture and commits rarely) leaves the reservation lent until the
segment retires. Such a push declares one use, and the compile, when it comes,
reserves the item again: one copy at commit, no bytes pinned on the gestures
that were superseded.

**A label volume's voxels are on the dataset's LEVEL-0 lattice**, whatever
pyramid level the pane draws or the plugin read (`OverlayTransform.frame =
"voxel"` names that lattice for every item kind). A plugin that computed on a
decimated level converts before it sends — §8.11's edge map is the rule — and
cannot say "these are level-L voxels" instead, because `scale_zyx` is exactly
what the voxel-selection constraints below reject.

**The LUT rule, and names by id.** `colormap` is a `(256, 4)` RGBA LUT. Row 0
is looked up for label 0 alone and is the background — conventionally
transparent; a foreground label `id ≥ 1` is drawn with row `1 + (id − 1) mod
255`, so ids 1..255 use rows 1..255 directly and ids above 255 wrap over the
foreground rows with period 255, **never onto row 0**. A row's alpha is the
**ROW's** — shared by every id that lands on it — and the compile persists it
as the region's `visible` unless `label_visible` names that id.
`label_visible` is `map<uint32, bool>`, keyed by label id for the same reason
`label_names` is: the alpha cannot say "hide 260 but not 5". **It governs the
compiled REGION, not the voxels** — those are painted from the LUT by the GPU,
which has only the row, so a 256-row LUT cannot express per-label anything
above 255. The two are different visuals (an annotation entry versus painted
voxels) and are allowed to differ; this is the disclosure, not a defect.
`label_names` is `map<uint32, string>` **keyed by label id, not by LUT row**,
so labels that share a row keep their own names; an unnamed label compiles to
`"Label <id>"`, and a name for id 0 is ignored.

**A label volume IS a voxel selection, and the host applies that definition
at the push.** The definition: `frame = "voxel"`, unit scale in
`"world"` mode, identity rotation, an integer `position_zyx`, and the item's
whole box — position plus the labels' shape, zeros included — inside the
dataset. An `OverlayUpdate` carrying a label volume that is not one is not
applied — the whole push, because an update is a full replacement — and the
plugin is told which rule, on `ContractViolation.where =
overlay_item_refused` (§8.5); `CompileLabelVolume` refuses the same item
again with a `host_error` (`INVALID_ARGUMENT`, or `OUT_OF_BOUNDS` for the
box), because a request drains ahead of the push whose descriptor it repeats
(§5.3) and cannot rely on the push having been checked. The box is checked
only while a dataset is open; a push with none open is applied unchecked and
its compile is refused `NOT_FOUND`. So display and compile agree on what a
label volume is, and the **one** thing that displays and does not compile is
**inline geometry**: the compile's transport rule above (no voxel bytes on
the control plane), not part of the definition — a label volume that will be
committed is sent segment-backed.

**The arena bound is the bound.** A label volume's bytes ride the overlay
LOANED arena, sized by the manifest's `segment_mib` (32 MiB default, 512
max) and never grown, while the host's read side grows without bound — so
the host will deposit a level-0 volume of any size and refuse a label array
of the same shape. A whole-volume segmentation of a large dataset is
bbox-tight, or the manifest declares the arena it needs; there is no coarser
lattice to fall back to. **A segmentation of a whole MOVIE is a chunked
overlay** (§5.7): one volume per (level, t) on the wanted level's lattice, in
an arena the host sizes from the dataset — and **a chunked overlay never
compiles**: `CompileLabelVolume` naming one is refused `INVALID_ARGUMENT`,
because compiling a time series of volumes into native masks is the
"masks too big to fit" scheme this feature deliberately is not.

#### What a contributed region may be

A region is a `pois_math.v1.Roi`, and it may be **any** geometry
that vocabulary describes — not only the four shapes vizi draws itself. A
plugin expresses a shape; **what is inside it is vizi's to decide**, by
the ROI scheme's §8, with a mask the one exception (its voxels are its
definition, and it arrives through `CompileLabelVolume`).

*"The ROI scheme" is the specification `roi.proto` implements — vizi's
`pois_math`, which is not published with this corpus. Where its rules decide
what this host accepts, refuses or counts as inside, they are restated below
(**The three rules**); the scheme's text is the source, and the restatement
must say what it says.*

**`type_name` decides what the host does with the rest:**

| `type_name` | What it is |
|---|---|
| `box`, `ellipsoid`, `cylinder`, `surfq6` | the built-in shape of that name |
| beginning `vizi.` | **refused** — reserved for the host's future built-ins, so a plugin's name never changes meaning under it |
| one of the contributing plugin's `region_types` (manifest §3.8) | an **asked** region: its shape is the plugin's to compute (the next subsection) |
| anything else | a **declared** region: the `Roi` is the whole definition, and the host enacts it |

**A declared region is pois_math's data model, enacted as written.** The host
draws its edges, facets and patches (the ROI scheme's §9); a vertex with a
`HandleSpec` is a handle the person can drag, along its rail (`PRIMARY`), in its
plane (`SECONDARY`) or freely (`ALL`), as the host turns a pointer into a
position. A drag of handle `h` from `p0` toward `p1` runs the ROI scheme's
§4 order: `p1` is projected onto the handle's DoF and clamped to its extent
bounds (`min_extent`/`max_extent`, measured from `extent_anchor`), giving the
handle's move `δ`; then **one level** of propagation (the scheme's §5) — every
`LinearBinding` whose `target` is `h` moves its `moved` vertex by `M·δ`, and
nothing propagates further, so a binding table may be cyclic. Display
positions come through the `CompileOperator` when the `Roi` carries one, and
are the control positions when it does not. No
behaviour is served: there is no constraint callback, no refurbish and no
`Compile` RPC, so a declared region's response to a drag is fixed by its data.
A shape that a fixed matrix cannot describe is an asked one.

**The surface is derived for a producer that does not want to author one.**
A region that declares no enclosing facet or patch gets its surface from
`Roi.auto_surface` (rule 2 below): `HULL`, the convex hull of its enclosing
vertices — or `STAR`, a surface
star-shaped about the `root` vertex, built from the points alone in any order.
So a tetrahedron is its four corners, and a blob is a cloud of points around a
centre; neither needs a mesh. A region that declares no edge, facet or patch
is drawn as that derived surface: its triangles' sides, as a wireframe.

**Inclusion modes from a plugin are `ENCLOSED` (the default), `INTERSECTED` and
`NONE`.** `INTERSECTED` is how a sheet, a curve or a point set is a region: the
voxels its geometry passes through, and no interior (rule 3 below). `SHELL` and
`POINTS` are **refused**: their thickness and radius would make the plugin the
one deciding what is inside.

**Refused, naming the rule** — `host_error` `INVALID_ARGUMENT`, and the whole
batch is not applied, as for every `ContributeRois` refusal:

- a `type_name` beginning `vizi.`;
- `inclusion.mode` `SHELL` or `POINTS`;
- an `ENCLOSED` region whose enclosing facets and patches are **not closed**,
  by rule 1 below; the detail names one unmatched side or row. The rasterizer counts crossings, which agrees with the winding
  number only on a closed surface — so an open one would contain whatever the
  algorithm happened to give, and refusing is the only answer that is the same
  on every host. An open surface that means to be one is `INTERSECTED`;
- an `ENCLOSED` region asking `STAR` whose points do not admit it (rule 2
  below: fewer than four, one at the centre, two on one ray, or a centre they
  do not strictly surround);
- a region with no enclosing facet or patch whose surface would be derived
  (`HULL` or `STAR`) from **more than 4096 enclosing points**: the host builds
  that surface whenever the region is drawn, picked or rasterized.  A larger
  surface declares its facets or patches;
- an `ENCLOSED` region with no enclosing facet or patch and fewer than four
  enclosing points: there is nothing to close.  A drawing that encloses nothing
  is `NONE`; a sheet, curve or point set is `INTERSECTED`;
- a vertex marked `enclosing` that the `CompileOperator` exposes in no display
  row (rule 0 below), in any inclusion mode;
- on an asked region, a `DirectionHandle`: a direction's shape is not part of
  the exchange below yet, and refusing it now leaves adding it additive;
- a patch whose control net is more than 16 on a side, or whose `weights` are
  neither empty nor one per control;
- an index out of range (a handle's `extent_anchor` included),
  `views`, `direction_specs`, a non-finite number (any number that reaches
  geometry: positions, directions, poses, frames, handle frames and extents,
  compile coefficients, edge and patch weights, binding matrices, inclusion
  thickness and radius, handle style sizes).

**The three rules.** Restated from the ROI scheme's §8.2 and §8.3, because
they decide what the host refuses and what it counts as inside. *(Mandated: a
plugin that wants its region accepted builds it to them; an SDK MAY check them
before sending, and is conforming either way.)*

0. **Two index spaces** — `Roi.vertices` are the **controls**; edges, facets
   and patches index the **display** positions, which are the rows of the
   `CompileOperator` `S` (`display = S · controls`), or the controls
   themselves when the `Roi` carries none. Every rule below reads **display**
   space. `enclosing`, `root`, handles and bindings belong to controls; a
   control marked `enclosing` counts as the display vertex that **exposes**
   it — the first row of `S` whose only nonzero is exactly `1` in that
   control's column — and one that no row exposes is refused. Indices are
   never read across spaces by coincidence.
1. **Closure** — checked on an `ENCLOSED` region's ENCLOSING/BOTH facets and
   patches, combinatorially, so it is exact. A facet contributes its **sides**,
   each consecutive pair of its cycle (a pair `(a, a)` of one repeated vertex
   is no side, and contributes nothing); a patch contributes its four **boundary
   rows** (its first and last control row and column, as index sequences),
   except a row whose controls are all one index (a collapsed corner), which
   contributes nothing. The surface is closed **iff every side and row occurs
   exactly twice**, as one index sequence read forwards or backwards **with its
   weights read the same way** (a facet side's are `1, 1`; compared exactly —
   the same controls under different weights are a different curve, and leave
   a crack); a facet side may pair with a patch row of exactly two controls,
   and a longer row only with a patch row. Direction is not part of it — orientation is decided
   separately, by the centroid test and `flip` — and a vertex is shared by
   **index**, never by coincident position.
2. **`STAR`** — for a region with no ENCLOSING/BOTH facet or patch. The centre
   `c` is the `root` vertex, and is not on the surface; the cloud `V` is every
   other enclosing vertex (an `enclosing` vertex, or an endpoint of an
   ENCLOSING/BOTH edge). Each `v` gets its direction `d = (v − c)/|v − c|`, in
   voxel-index ZYX. **Which points are joined is the Delaunay triangulation of
   the directions on the unit sphere** — the faces of the convex hull of the
   `d`, which is a hull of *directions*, never of the points. Each face
   `(i, j, k)` becomes the triangle `(v_i, v_j, v_k)` at the points' own
   positions, facing away from `c`. Four or more directions on one circle of
   the sphere make a face with more corners; it is **fanned from its
   lowest-indexed vertex** (its lowest display index), in the face's outward
   counter-clockwise order. The result is star-shaped about `c` by
   construction: the cone of rays from `c` through `(v_i, v_j, v_k)` is the
   cone through `(d_i, d_j, d_k)`, and those tile the sphere. **Refused** when
   `V` has fewer than 4 points, one coincides with `c`, two share a direction,
   or the directions do not strictly surround `c`. `HULL`, the default, is the
   convex hull of the points themselves. **Tolerances**, absolute, on unit
   directions: `d` is on a face's circle when `|n·d − h| ≤ 1e-9` (`n` the
   face's unit outward normal, `h` its offset); a point is at `c` when
   `|v − c| ≤ 1e-12`; two share a ray when `1 − d·d′ < 1e-12`; they strictly
   surround `c` when every face has `h > 1e-12`. A cloud nearer a threshold
   than rounding resolves is not a conformance case.
3. **`INTERSECTED`** — a voxel `k` (integer ZYX) is inside iff some point `q`
   of the enclosing geometry lies in its **half-open** cell, `k ≤ q < k + 1` on
   every axis. The enclosing geometry is the ENCLOSING/BOTH facets and patches
   (triangulated as `ENCLOSED` would, with no closure required), every
   ENCLOSING/BOTH edge (a straight edge as its segment, an arc as its
   tessellation), and every `enclosing` vertex as its point. `auto_surface` is
   not consulted: a sheet or a curve is what it declares. Half-open makes a
   sheet lying exactly on `z = 5` one layer, not two. Two details fix the
   answer to the voxel:
   - **The cell is shifted by ε = 1e-9**, `k − ε ≤ q < k + 1 − ε` (a point is
     in cell `⌊q + ε⌋`), so a point on a lattice plane in exact arithmetic —
     an arc in `z = 2` evaluating to `1.9999999999999998` — is on it here too.
     Segments and triangles are tested against the closed box
     `[k − ε, k + 1 − ε]`, as declared, never re-subdivided.
   - **Curved geometry is the host's to tessellate, within a quarter voxel.**
     An arc or a patch covers the voxels its tessellation passes through, and
     the tessellation is the host's own, under a contract rather than a rule:
     every point of the curve within ¼ voxel of the pieces and the reverse,
     **certified** from the control net; closed wherever the declaration is
     closed (shared sides sampled identically); the tolerance doubled only past
     4 000 000 pieces in one region, and the region refused past 4 voxels.
     Two hosts may differ in the voxels right at a curved boundary, so an SDK
     that checks membership before sending has nothing exact to match, and need
     not. A plugin that wants exact control declares facets, which are used as
     given. The ROI scheme's §8.3 says how vizi meets the contract.

**Everything the host does with a region defined by its data, it does with a
generic one**: draw it in 2D and 3D, crop to it, pulse it, measure it, answer
`ReadRoiInclusion` for it, export through it, save and reload it with the
whole `Roi`. Grow, which reshapes a region by the host's own rule, is not
offered for an asked one.

#### Region types a plugin shapes

**A type, not a region, is what a plugin answers for.** pois_math's founding
rule is that behaviour resolves by `type_name` (the ROI scheme's §0), so a
plugin declares in its manifest the types whose shape it computes —
`contributions.region_types` (manifest §3.8), which needs `contribute_rois` —
and every region of such a type is **asked**. Four things follow from putting
it on the type:

- **Names are the plugin's own.** Two plugins' `tetra` are two types; the host
  records which plugin shapes each region as `RoiRecord.type_owner`.  The
  manifest that is installed now decides: a region whose recorded plugin is
  installed and no longer declares its type is **not asked** (its
  `type_owner` reads empty, and the host enacts its edits), and declaring the
  type again makes it asked again.  A plugin that is not installed, disabled
  or failed still owns its regions, which stay locked (below).
- **A copy keeps its type.** A region the person duplicates is still shaped by
  the plugin, so `type_owner` need not be the plugin in `origin`, and a
  `ShapeEdit` for a region the plugin did not contribute carries an empty
  `contrib_id`. Every message carries the whole region, so that costs nothing.
- **It is declared before it runs**, so the Plugin Manager shows it, and the
  host knows which plugin a region is waiting on before that plugin starts.
- **It is all or nothing per region.** Every handle of an asked region is
  asked; none is enacted from a matrix. A round trip costs far more than a
  matrix, and a region half one and half the other would answer a drag two
  ways depending on which handle was grabbed.

**Shape is asked; placement is not.** The shape is where the vertices are
relative to one another. Moving the whole region, turning its orientation frame,
editing its pose rows (Center) or its parent leave the shape alone, and the host
enacts them in either mode; `RegionsChanged` reports them as it reports any
edit. **Undo and redo** restore a state that was committed, without asking.
**No other path changes an asked region's shape** — Grow is not offered for it.

**When the type's plugin has no live session, the shape cannot change.** The
region still draws, measures, crops and exports; only its handles are locked,
and say why. A plugin that is installed and enabled is started when a region of
its type is selected (manifest §6's lazy launch), and its handles unlock when
its session is live; one that is not installed, is disabled, or has failed
keeps them locked until that changes. **A start that fails is not retried by
selecting or pressing again** — the handles say it could not start and why,
and the next attempt follows something changing in the Plugin Manager (enabled,
rescanned) or the plugin coming up another way.

**The exchange.** One drag is one gesture, from pointer down to release.

| Step | Who | What |
|---|---|---|
| 1 | host | Projects the pointer onto the handle's declared DoF (the ROI scheme's §3 — the camera-aware half, which is the host's alone), clamps it to the handle's `min_extent`/`max_extent` about its `extent_anchor`, and sends `shape_drag` — so `p1` is always a position the handle's own declaration allows |
| 2 | plugin | Answers with `shape_proposal` naming the `seq` it answers — or does not, if a newer `shape_drag` has arrived. A proposal already computed when a newer drag arrives MAY still be sent: step 3 shows it only if it is newer than what is on screen |
| 3 | host | At each draw: the newest proposal whose `seq` is above the one on screen is the region's shape; the grabbed handle is drawn at the newest `p1` if that is newer |
| 4 | host | On release, sends `shape_commit` |
| 5 | plugin | Answers with `shape_answer`. **That answer is the commit** |
| 6 | host | Applies it as one undo step, sends `ShapeEditEnded{COMMITTED}`, and reports the edit in `RegionsChanged` |

**Full state in** (the ROI scheme's §4). Every `ShapeEdit` carries the
region's rest-form `Roi` as it stood when the drag began, the handle, its DoF
mode and axis, where the handle started (`p0`) and where the person has put it
(`p1`) — pois_math's own `ConstrainMoveRequest`. Coordinates are the `Roi`'s
own: voxel-index ZYX, before pose, placement frame and parent, which the host
has already undone. So **a plugin answers each message from that message
alone**; it may remember a drag, and never needs to. A `ShapeResult` carries a
position for **every** vertex — every control, one per `Roi.vertices`, in the
same coordinates; the host compiles them through the `CompileOperator` as for any
edit (rule 0), so a type whose surface is not a linear image of its controls
carries its surface points as vertices of their own; topology, directions
and the rest of the `Roi` are unchanged, and a plugin that wants to change them
contributes the region again afterwards. **An answer reshapes only the region
asked about.** Its parent's and children's placement follow from the frame graph,
as they would for any edit; a plugin that links regions in some other way
contributes the others itself.

**The host never waits during a drag.** It sends at most one `shape_drag` per
pump tick (40 ms), only when `p1` has moved, merging on its own side as for
`ChunksWanted` (§5); what is on screen between proposals is the last shape and
the moving handle. That keeps §8.7's rule — nothing on a path that moves the
view waits on a plugin — and it means a slow plugin is a lagging shape, never a
frozen viewer.

**The host waits at the commit, and only there.** From step 4 until step 6 the
region is **held**: it takes no other edit and no new drag, and nothing about
it is committed — no undo entry, no `RegionsChanged`, no stale measurement
cleared. The wait is supervised (§8.16): it is a row in the activity list,
*Reshaping «region» — waiting on «plugin»*, with Cancel, which becomes Stop
plugin as for any request. Without supervision, §9's deadline applies.

**Every way it ends, and the one outcome of each:**

| Condition | Outcome |
|---|---|
| `shape_answer` with `OK` or `CLAMPED` and a valid shape | committed — `ShapeEditEnded{COMMITTED}` |
| `shape_answer` `REJECTED` | the region as before the drag — `REVERTED`, `detail` carrying the plugin's `message` |
| `shape_answer` whose positions are not one finite position per vertex | the region as before — `REVERTED`, and `ContractViolation{shape_refused}` |
| `shape_answer` whose positions leave the region no inside — a shape the scheme refuses (§8.2: an open enclosing surface, a star whose centre left it), the rule a declared drag stops at | the region as before — `REVERTED`, and `ContractViolation{shape_refused}` naming the rule |
| the person cancels, during the drag or while the commit waits | the region as before — `CANCELLED` |
| §9's deadline passes, unsupervised | the region as before — `REVERTED` |
| the region is deleted or contributed again, or the dataset closes or changes | nothing is committed — `CANCELLED` |
| the session ends | nothing is committed; no message (there is no session to send it on) |

A `shape_proposal` with a bad shape — either kind above — is not applied: the
last shape stays on screen and `shape_refused` is reported.

**Which orderings are NOT errors.** The wire orders each queue, not one queue
against another (§5.3), so these happen to a conforming plugin and are
**dropped silently**:

- a `shape_drag` arriving after its drag's `shape_commit` — the plugin may answer
  it or not; the host drops the answer;
- a `shape_proposal` or `shape_answer` for a drag that has already ended — the
  plugin cannot know it ended until `ShapeEditEnded` arrives.  That includes a
  second `shape_answer` after the host ended the drag (a cancel during the
  commit): every answer for an ended drag is dropped;
- a `shape_proposal` older than the one on screen.

**Which are**, reported as `ContractViolation{shape_out_of_step}`: a `drag_id`
the host never sent this session, a `seq` it never sent within that drag, or a
second `shape_answer` to one commit **while that drag is still open**. The
violation changes nothing on screen.

**What an SDK owes.**

- **Answer every `shape_commit`.** The host waits on it with a person watching.
  A plugin with no shaper registered for a type it declared answers `REJECTED`
  with a message saying so — never silence, which would leave the region held
  until the person cancels. *(Mandated: the host relies on an answer.)*
- **Stop proposing for a drag once its `shape_commit` or `ShapeEditEnded`
  arrives.** That is what keeps the host's coalescing keys for proposals
  bounded (§9: a slack of 4 per session), since a person drags one region at a
  time. *(Mandated: the bound relies on it.)*
- **Answer from the message.** A plugin need not hold the region's state between
  drags; whether it caches anything is its own. *(Advice.)*
- **Never answer a commit behind other work.** Run shapers off the command
  worker, and give commits capacity of their own, so a running command or a slow
  proposal does not keep the person waiting; shape only the newest drag and drop
  the superseded ones. A proposal already computed when a newer drag arrives may
  still be sent (step 2). *(Advice.)*
- **The SDK, not the plugin, owns the rules above**: checking a result's
  positions, answering `REJECTED` when the shaper fails, withdrawing proposals at
  the commit or end, dropping late drags. A type may be declared with no shaper,
  and its commits are then answered `REJECTED`. *(Advice.)*
- How the shaper is registered, whether drags and commits reach one function or
  two, and on which thread, are each SDK's own. *(Not specified.)*

A spinbox edit in the generic vertex editor is a drag with no `shape_drag`
before its `shape_commit`. Drags of different regions are independent by
`drag_id`: a commit may be waiting on one region while the person drags
another.

**Why pushes, and not a host request.** A host request queues behind the one in
flight (§5.1), and a plugin's command can hold that slot for minutes; a commit
with a person waiting on it is exactly the message §5.1 says may not be a
request. Pushes take no slot, and the pairing the reply would have given is
carried by `drag_id` instead.

**What it costs to be wrong about.** A `ShapeEdit` carries the whole `Roi`, which
for the shapes this is for — a tetrahedron, a few dozen star points, SurfQ6's 26
vertices — is a few kilobytes at 25 per second. A region large enough to make that
matter is a declared one, or a label volume.

### 8.9 Assets

| Message | Direction | Class | Reply |
|---|---|---|---|
| `RequestAsset{slot, force_reprompt}` | plugin→host | request | `AssetResult` \| `host_error` — the reply must be routable, see below |
| `RequestStatusQuery{subject_id}` | plugin→host | request | `RequestStatus` — §8.9's liveness probe |
| `RequestSavePath{suggested_name, extension, title}` | plugin→host | request | `SavePathResult{path \| declined}` \| `host_error` |

**A save path is the same kind of request, pointed the other way.** A
plugin with no UI cannot show a save dialog, and a typed path has no folder
browsing and no overwrite prompt; registering an exporter is the wrong shape
when the plugin only wants to write a file of its own. `RequestSavePath` shows
the host's save dialog, starting beside the open file, filtered to
`extension` (appended when the person leaves it off) and offering
`suggested_name`, which is a file name and never a path
(`INVALID_ARGUMENT`). `extension` is one or more dot-separated name parts
(`csv`, `ome.tif`; anything else is `INVALID_ARGUMENT`), matched against the
end of the whole name, so `x.ome.tif` is not given a second one. When the
appended name already exists, the dialog's overwrite prompt was about the name
as typed, so the host asks again with the full name before it answers. It **needs no permission**, as an exporter does not: the
person picks the path in the host's dialog, and the host creates nothing — the
plugin writes the file. Everything below about the picker's lifecycle applies
to it unchanged: one picker per session at a time, whichever kind, and the
command deadline held while it is up. **Either picker is non-modal**: vizi's
windows stay usable while it is open, so a plugin that asks again the moment
it is cancelled cannot keep the person from the Plugin Manager that disables
it. It is vizi's own file dialog rather than the platform's, whose native
dialog may block its window whatever modality is asked for.

The host shows a single-file picker, copies the chosen file into a
persistent per-plugin slot, and records its size and SHA-256 **at import time**.
Those are an identification of what was selected, not an author-supplied
acceptance constraint — which is the difference between this surface and
manifest `assets`, whose digests *are* a constraint.

**Cancellation sets `declined` and leaves `path` empty.** That is
`plugin_principles.md` 11 in one field: a cancelled pick is expressible and is
not an error.

**`AssetResult` is routed like every reply, by kind.** §3.1 and §4 require
dispatch by kind with nothing falling through silently; an allow-list of reply
kinds that omitted this arm would leave every `request_asset` waiting out its
timeout with the reply already in hand.

#### The picker waits on a PERSON, and three things follow

Nothing else on this channel does.

**The command-handler deadline does not run while the host is showing a picker
it was itself asked for by that session.** §9 gives a command handler 10 s and
expiry marks the session **suspect**. A plugin that requests an asset from
inside a command handler is blocked on a reply that cannot come until the
person chooses, and it cannot shorten that: the delay is the host's dialog, in
front of the host's own user. Without the rule, browsing for eleven seconds
would mark a healthy session suspect. The budget still bounds the
plugin's own latency; only the host's contribution to the wait is excluded, and
the hold is counted so overlapping pickers nest.

**The hold is unbounded, and that is the decision rather than an omission.** A
picker left open keeps its command pending for as long as it is open, and a
session is single-in-flight, so the next call waits too. Capping it would
reintroduce the same failure at a longer and more arbitrary horizon. §9's
deadlines exist because a silent peer is *unknown*; a dialog the host itself put
on screen is known, visible, and ended by the person in front of it. A hold
cannot outlive its session. A **supervised** command has no deadline to hold at
all (§8.16); the hold still matters to a host that does not supervise.

**A command MAY open a picker and wait for it inside its handler, and an SDK
MUST NOT refuse that.**  Whether a call works is the one thing
an SDK cannot vary without breaking the portability of every plugin that makes
it: a plugin written against an SDK that allows it would fail on one that
refuses.  **But the wait has a cost, and it is the plugin's to weigh:** the
command holds the session's one in-flight slot (§5.1) for as long as the person
takes, so every other host request to that plugin — a read of the dataset it
serves, its next command — waits behind the dialog.  So a plugin that also
serves reads or other commands **SHOULD** open a picker off the command path,
through its SDK's asynchronous form (the reference SDK's
`request_asset_async`), and return promptly.  An SDK may warn about the
blocking form; it may not refuse it.

**One picker per session at a time.** A `RequestAsset` that would open a picker
while the same session's is still up is answered `host_error` `OUT_OF_BOUNDS`
and opens nothing: a person answers one dialog at a time, and nothing else
bounded how many a plugin could stack on screen. Ask again once the first is
answered.

**A late `AssetResult` is delivered and discarded, with a diagnostic** — rule 5
of §4 forbids discarding an unmatched id silently. If an SDK gives up
at its own deadline and the person chooses a file a minute later, the reply
still goes out against the retired id. A receiver that no longer has a waiter
for it drops it and does NOT treat it as unroutable or fault the session —
"the reply must be routable" above is about the host addressing it correctly,
not about the plugin still caring. SDKs may choose their own patience for a
pick and two may choose differently; that latitude is fine, and this is the
part an SDK cannot settle for itself.

**A plugin that is waiting can ask whether the host still has its request.**
`RequestStatusQuery{subject_id}` names one in-flight request and is answered
`PENDING` (the host took it in and has not answered) or `UNKNOWN` (no record:
answered, abandoned, or never arrived), with `awaiting_user` set while a picker
is up for it. The three UNKNOWN cases are not separated because the plugin's
move is the same for all of them and telling them apart would mean retaining
every answered id.

`PENDING` means the host's bookkeeping and nothing more — never that the request
will finish, or when — and UNKNOWN is not evidence the work failed. §9's *every
timeout means unknown, never dead* governs the answer as much as its absence,
so a query that itself times out says the host is not answering and not that it
is gone.

**This is not the heartbeat §14 rejects.** That rejection is that a heartbeat
*"proves the stream works, not that the peer's handlers do"* — it rules out an
unsolicited, periodic, content-free signal. This is solicited, names one
request, and answering it makes the host consult the bookkeeping for that
request. It is the shape of `GrantStateQuery`/`GrantState` (§8.12) in the other
direction and for the same reason: a thing can be in flight with the send
reporting success. The host's ledger is written at two choke points — the pump
records a request as it dispatches it, `post` clears it as the reply leaves —
so the answer cannot drift from what the host is doing, and a request kind
added later is covered without anyone registering it.

### 8.10 Tools, bindings and input arrangement

Everything the doorbell's `E` records reference. All of it is arrangement (§5.5).

| Message | Direction | Class |
|---|---|---|
| `ActivateTool{tool_id, deactivate}` | plugin→host | request |
| `ToolActivationChanged{tool_id, active}` | host→plugin | one-way |
| `ToolBinding{tool, tool_id, triggers[], mode, cancel_on_camera_move, retired, revision, actions[]}` | host→plugin | **arrangement** |
| `ToolBindingAck{tool, revision}` | plugin→host | ack |
| `EpochChange` | host→plugin | **arrangement** |
| `EpochAck` | plugin→host | ack |

The fourth thing an `E` record references — the `gen`'s index→time mapping — is
in §8.11 with `Geometry`, because it is dataset state rather than input
arrangement. It is an arrangement all the same, and the epoch references it.

**`revision` is listed above because this section prints the message and an
implementer reads it here.** Its rule is §5.5 rule 4's — monotonic per identity,
unchanged by a re-send, incremented when the content changes, beginning at 1 for
each identity, and `0` naming no statement — and it lives there because it
belongs to the arrangement class rather than to tool bindings. It is named here
so that reading §8.10 for the message and §5.5 for the rules does not show the
field in one and not the other.

**`ActivateTool` is not an arrangement and is easy to mistake for one.** It is a
plugin *asking* the host to toggle one of that same plugin's declared tools, and
`ToolActivationChanged` is its answer. The arrangement is what the host does
next: allocating a handle and sending `ToolBinding`. Confusing the two would put
the handle allocation on a plugin-initiated path, where the host is the only end
that can own the counter.

**A plugin REQUESTS its triggers in the manifest, and the host assigns them.**
A trigger ENGAGES a tool. Once engaged, the tool owns the **primary pointer** in
its views: the bare left button, and every left chord the host does not keep,
with the modifiers in each record's `mods`. **The primary button means whatever
the engaged tool means, so no tool ever names it** — the modal rule. One
tool is engaged at a time, host-wide. A tool is engaged from the Tools menu,
from the control pane's sticky button, or from a trigger:

| Trigger | `hold` | What it does |
|---|---|---|
| a key | `TOGGLE` | press once: engaged; press again: not — the sticky button by keyboard |
| a key | `MOMENTARY` | engaged while held; on release, whatever was engaged before returns |
| a mouse chord | `MOMENTARY` only | engaged for exactly that press/drag/release, then the prior tool returns |

A mouse chord is down or up and nothing else, so it is always momentary; a
tool that only makes sense on a right-drag asks for a momentary `right`
trigger and receives that drag as its own PRESS / DRAG / RELEASE. A momentary
engage arriving **while a gesture is open** is dropped: the pointer is busy,
and engaging would cancel the gesture under the user's hand. The tool a
momentary trigger displaces is told `deactivated` and, on release,
`activated` again — no half state.

> **Conflict: when two plugins request the same `(button × mods)`, or the same
> key, NEITHER is bound, and the host tells the user which plugins collided and
> on what.**

**Not first-come, not last-wins, and never a silent re-map.** A user has to be
able to know what their keys do; a binding that quietly went to one of two
plugins is precisely the thing they cannot find out, and they will conclude the
key is broken rather than contested. Burning it for both is the only outcome
that is *visible*, and the recourse is theirs and needs no protocol: stop both
plugins, start the one whose binding they want. The conflict notice is a
user-facing surface, and it is the point of the rule rather than a
side effect of it.

**A refused trigger is not a refused tool.** The tool still engages from the
menu and the pane, still gets a handle, and still receives every `E` record its
other triggers produce; what it loses is one way in. So the failure is partial
and the plugin is told which request was dropped, rather than the activation
failing whole.

**The host's own gestures are RESERVED, not contested.** Its keys are read off
the live window — `Ctrl+R`, `Ctrl+H`, `M`, `Ctrl+,`, the `Ctrl+1`…`Ctrl+9`
nine and whatever else is bound the day the plugin loads. Its pointer chords
are declared once, as the **union over camera
modes** so an assignment never flickers with the camera combo:

| View | Chords the host keeps |
|---|---|
| 3D | `left`, `Shift+left`, `Ctrl+left`, `right`, `Shift+right`, `Ctrl+right`, wheel |
| 2D | `left`, `right`, `Ctrl+left`, `Ctrl+right`, wheel |

A trigger is checked against the chords of the views its tool declares, so a
2D-only tool may take `Shift+right`. Free for plugins by construction: Meta
chords, the middle button and its chords, and multi-modifier chords such as
`Shift+Ctrl+left`. `Alt` is refused on every mouse trigger at manifest
validation — it is the window manager's (Alt+drag moves the window before the
application sees it), not the host's. A plugin requesting a reserved gesture is
refused and told why, and the host keeps it. The nobody-gets-it rule is for two
plugins, because its recourse — stop both, start the one you want — exists only
between plugins. A user cannot stop the host, so burning a host binding would
leave the key dead with no way back, which is the failure the rule was written
to prevent rather than an instance of it.

**An action is what the ENGAGED tool can be told, with or without a gesture
open, without ending one.** The tool receives it either way and decides for
itself what it means in each state.
The standard modality: the mouse is the target and a key is the fire — a node
placed on a polygon *mid-drag*. A tool declares `actions` in its manifest; each
may name a key and may ask for a pane button. The host delivers an action as an
`E` record with `phase = FIRE` and `action` = the action's **1-based index in
the manifest's order** (`doorbell_E_input.md` §1), carrying the pointer's
current sample; an open gesture is neither closed nor disturbed. An action's key
is live only while its tool is engaged, so two plugins may both use `N` — the
host refuses an action key only where a *global* key would fire at the same
press, one the host holds or one another plugin's trigger holds, and announces
`""` for it. A pane button reaches the same action with no key at all.

**Withdrawing a trigger is a `ToolBinding` for the SAME handle without it**, and
it is not `retired`: `retired` withdraws the *tool*, which is the opposite of
what a conflict calls for. A `ToolBinding` naming a handle the receiver already
knows **updates it in place and does not advance the high-water mark** — the
never-reused, strictly-increasing rule governs the allocation of new handles,
not re-statements of existing ones. That is the message the recourse above
needs: a second plugin starting must be able to take a binding away from one
already running.

#### Triggers, actions and handles

The `tool` handle the doorbell carries is assigned here, with what the host
honoured of the manifest's requests.

```
ToolBinding {
  tool                    // u32 activation handle; never 0, never reused
  tool_id                 // the manifest string this handle stands for
  triggers[] { key, button, mods, hold }   // the ASSIGNED subset of the requests
  mode                    // FIRE | GESTURE
  cancel_on_camera_move   // bool
  retired                 // true => this handle is being withdrawn
  revision                // §5.5 rule 4
  actions[] { action, action_id, key }     // every declared action, in order
}
```

A trigger absent from `triggers` was refused, and the user was told why. Every
declared action is listed whether or not its key was honoured: the plugin needs
the index an `E` will name and the key it may tell a user about, and `""` says
there is none.

**The binding is in the tool's hands at the moment it is engaged.** The
natural time to tell a user *P toggles, middle button holds; U undoes* is the
activation, and an SDK's activation hook runs with no event to look a binding
up by — so an SDK hands the hook the arrangement just installed, and lets a
tool ask for its arrangement by manifest id as well as by event (the reference
SDK: `activated(self, ctx, binding)` by arity, and `ctx.arrangement("probe.point")`).
At retirement the hook is handed the binding just
withdrawn, which an id lookup cannot find.

**A binding IS the activation, so it goes out when the tool is engaged — never
at session start — and is retired when the tool stops being engaged.** Each
engagement is a new handle; a tool engaged, released and engaged again is two
activations and two handles. A session a panel, a command or a reader launched
has no binding until a user engages one of its tools, and a plugin's `activated`
hook therefore means what it says. **Input made before the plugin has
acknowledged the binding is HELD by the host, not dropped**, and delivered in
order when the acknowledgement lands: a momentary chord engages and presses in
one motion, so its `PRESS` always precedes the acknowledgement, and dropping it
would leave a gesture with no opening record. A binding that is never acknowledged
escalates (§9), and what was held for it is discarded — it was never sent, so
there is nothing to cancel.

> **`Trigger` carries no platform code in any of its fields, and that is
> `doorbell_E_input.md` §3's rule rather than a new one.**

That section fixes `mods` as a platform-neutral bit set and says why — *"the
host maps its own platform modifiers onto these; a plugin never sees a
platform-specific code"* — and the rule is channel-independent. So:

- **`mods` is the doorbell's set and the same bits**, `SHIFT` `0x01`, `CTRL`
  `0x02`, `ALT` `0x04`, `META` `0x08`, reserved above. **The same encoding
  deliberately**, because an author writes one trigger and reads it back off an
  `E` record, and two encodings for one concept is a conversion nobody remembers
  is there. §3 owns it and this cites it.
- **`button` is a `MouseButton` enum.** This section defines the
  (button × modifier) space; `doorbell_E_input.md` §9 defers to it.
- **`key` is a portable name string**, `""` for none — `"Ctrl+J"`, `"F5"`,
  `"Escape"` — modifiers first in `SHIFT`, `CTRL`, `ALT`, `META` order. A string
  for the reason the manifest already gives for its own field: it is what an
  author writes and what a plugin shows a user when it says what engages its
  tool. The manifest's shape and this one are the same shape on purpose.
- **`hold` is `TOGGLE` or `MOMENTARY`**, per the table above.

> **`mods` qualifies `button`. A key trigger carries its own modifiers inside
> `key` and leaves `mods` at 0.**

A trigger names a button **or** a key, never both, so the two never describe one
gesture and there is nothing to keep consistent: `"Ctrl+J"` is one gesture and
the author never writes `mods` beside it. **A cross-check is only worth its cost where two
independently-derived values meet** — `offset` against `region` on a release is
one, because the sender computes them from different things. Two spellings of the
same modifier set are not; they are one fact written twice, and the second
spelling is a thing to get wrong rather than a thing to check against.

- **Handles start at 1, increase, and are never reused, per plugin session,
  from a counter that plugin alone consumes** (`doorbell_E_input.md` §5). The
  scope is load-bearing: the receiver's "never seen" test is a high-water mark
  over the handles *that plugin* has been told about, and a host-global counter
  would hand one plugin 5, 9, 14 and break it.
- **Monotonic, not consecutive.** A handle may be allocated and never
  registered, and a receiver that required the next consecutive value would
  reject every later activation and leave all tool input dead for the session.
- **`ToolBinding` messages for one session MUST be emitted in increasing
  `tool` order.** Serialization alone does not supply this: 12 before 11 moves
  the receiver's mark past 11, which is then rejected forever as retired. The
  host achieves it structurally: a handle is allocated from the session's
  counter **at engagement**, and a displaced activation's statement is replaced
  by its retirement, so a retry of an older activation can only ever carry
  `retired` — which never advances the mark.
- **Retirement rides the same message** with `retired` set, and never advances
  the mark.
- **The plugin calls its transport's `tool_activated` / `tool_retired` before
  acknowledging**, per §5.5's install-before-ack rule.

`mode` decides which phases the binding emits — `FIRE` only, or the
`PRESS`/`DRAG`/`RELEASE`/`CANCEL` set. `cancel_on_camera_move` defaults on for
anything screen-defined; a binding that turns it off forfeits nothing, because
each ray record carries the camera it was cast from.

#### Epochs

An **epoch** is the version of one pane's interpretation parameters: voxel
spacing, extent, crop, the level table, the index→time mapping, and which
channels at index ≥ 15 are visible. The camera is not one of them — every ray
record carries its own — and `gen` is not one either.

```
EpochChange {
  repeated DatasetState datasets   // whole state, one per `gen` named below
  repeated PaneEpoch    panes      // one or more; each names a `gen` above
}

DatasetState {
  gen                              // u64 open generation
  Geometry geometry                // §8.11; whole, never a delta
  time_mapping_revision            // u32; 0 = no mapping arranged
}

PaneEpoch {
  view_id                          // u8, host-assigned; 0 is a valid pane
  epoch                            // u32, never 0, monotonic per view_id
  gen                              // which DatasetState above applies
  channels_high                    // which channels at index >= 15 are visible
}
```

The exchange, from `doorbell_E_input.md` §1a:

1. A parameter changes. **The host stops emitting `E` for that pane**, to every
   plugin.
2. The host sends the **full state** for the new epoch. One message may carry
   several `(pane, epoch, state)` entries, because one change can affect several
   panes at once.
3. The plugin drains its doorbell queue and processes or discards what it finds.
4. The plugin calls `epoch_acknowledged(view_id, epoch)` on its transport, **then**
   sends `EpochAck` naming the pairs it has installed. It may acknowledge some
   panes and not others; each resumes on its own.
5. The host emits under the new epoch **to that plugin**, and to any other that
   has acknowledged.

**As specified in `doorbell_E_input.md` §1a, events flow for a
`(plugin, pane)` exactly while that plugin's acknowledged epoch is the pane's
current one** — that single condition is the whole gate, it is that document's
to state, and what follows here is the *messages* that carry it.

**A pane has no default epoch and no epoch 0.** Establishment is the same
exchange as a change, triggered by subscription: on subscribing, a plugin
receives the current epoch and state for each pane it will be sent events for. A
plugin joining an hour into a session gets whatever the pane is currently at —
nothing is replayed, because an epoch is state rather than a transition.

**A repeated `EpochChange` carrying the same `(view_id, epoch, state)` is
acknowledged again and closes nothing** (§5.5's re-ack rule). The same
`(view_id, epoch)` carrying *different* state is a host bug and is reported.

**`DatasetState` is whole state with exactly one reference in it**, and the
exception is deliberate. Whole-state resend is free while the state is O(1); the
index→time mapping is O(T) and would be re-sent on every crop drag, which is why
`doorbell_implementation_details.md` §3 keeps it out of `Geometry` and arranges
it per `gen` on its own (§8.11). So the epoch *names* the mapping revision it
means rather than carrying it — which is legitimate precisely because that
mapping is itself an arrangement the plugin has already acknowledged, so the
reference points at something established rather than assumed.

That makes the failure case a host bug rather than a race:

> **A plugin receiving an `EpochChange` naming a `time_mapping_revision` it has
> not acknowledged MUST NOT acknowledge that pane, and MUST report
> `ContractViolation{where = missing_arrangement}`.** The host has emitted an
> arrangement referencing an unacknowledged one, which violates §5.5's gate
> rule; its
> remedy is to send the mapping, wait for its acknowledgement, and re-send the
> epoch.

**A reference is the only thing an arrangement may inherit, and only to another
acknowledged arrangement.** Anything else would make "whole state" a claim the
receiver cannot check.

**Deadline**: §9. On expiry the host re-sends the epoch idempotently; on
continued silence it escalates (§9: reported where a person sees it). A plugin that
never acknowledges otherwise goes silently dead, which is the degradation
`plugin_principles.md` 9 forbids.

**The trade is stated rather than avoided.** Every interpretation change costs a
round trip before events resume, paid at user rate. Latency is cheaper than
acting on the wrong data, and this is the one place in the platform where that
trade is made deliberately.

### 8.11 Dataset geometry and time

| Message | Direction | Class |
|---|---|---|
| `Geometry` | host→plugin | inside `EpochChange` |
| `TimeMapping{gen, revision, t}` | host→plugin | **arrangement** |
| `TimeMappingAck{gen, revision}` | plugin→host | ack (**one-shot**) |

```
Geometry {
  gen
  channels      [ {index, name, source} ]      // source: file | placeholder | user
  extent        z0, zN · y0, yN · x0, xN     // half-open, level-0 voxel indices
  crop          z0, z1 · y0, y1 · x0, x1     // EdgeBox, doubles: the clip box.
                                             // Compare against extent in EDGE
                                             // terms — a full-dataset crop is
                                             // {-0.5, N-0.5}, not {0, N}
  spacing       uZ, uY, uX                   // physical size of one level-0 voxel
  spacing_unit                               // "um" | "" when unknown
  spacing_provenance                         // file | override | display-fallback
  levels        [ [lZ, lY, lX] ]             // per level, the SAMPLING FACTOR
                                             // from level 0 -- see below
}
```

> **`levels` carries the SAMPLING FACTOR, not the ratio of the array shapes.**

The two part on an odd dimension, and the difference is not cosmetic: vizi
block-downsamples by 2 and discards an odd trailing sample, so a 31-voxel axis
becomes 15 and the factor is **2** while the shape ratio is 2.0667. This
section's own level map is `(e + 0.5)/scale − 0.5` applied directly to the
number, so a ratio puts every converted coordinate and every crop face slightly
wrong at every level ≥ 1 of any odd dimension. It is stated because an
implementation reading a `Geometry` has no way to tell which it was given.

**Everything, re-sent whole on any change.** A plugin cannot be asked to reason
about coordinates it only half understands, and a delta protocol for something
that changes at human rate is strictly more expensive to get right than a
resend (`plugin_principles.md` 2). The reasoning and the three things it settles
— anisotropy, expressible absence, and that it changes mid-session — are
`doorbell_implementation_details.md` §3.

**`spacing_unit` empty with `spacing_provenance = display-fallback` means "we do
not know how big a voxel is"**, which vizi genuinely often does not. A plugin
MUST NOT read the fallback 1.0 as a measurement. This is the field most likely
to be believed, and it is `plugin_principles.md` 11 applied to it.

**A channel's `name` is never empty, and `source` says whose it is.** A
channel nothing named carries the label the person sees for it — `"Channel N"`,
1-based — with `source = PLACEHOLDER`, and a plugin MUST NOT present that name
as the file's: label a trace with it, but write it into no artifact as a
declared channel name. `FILE` is a name the file or its reader gave; `USER` is
one the person gave in the host (reserved: vizi has no channel rename).
A bare name would hand plugins the host's padding as though the file had said
it, and give a file that genuinely calls a channel `"C0"` no way to be believed.

**There is no dataset world transform.** vizi's volume is axis-aligned with
per-axis spacing; the only affines in the tree are per-object and belong to the
objects that own them. The field is deliberately absent rather than
present-and-identity, because an identity transform is a claim.

**`crop` is the CLIP box, and clipping is not lighting.** That distinction is
this layer's, everything below follows from it, and it is what a plugin author
has to hold:

- **Clipping bounds what an INTERACTION MEANS.** The global crop box is
  axis-aligned, exactly representable, and it is what makes a position
  meaningful: what vizi promises by reporting one is *this content is relevant
  to the click*. Every coordinate on both channels is defined against it, and
  `crop` carries it — the only crop on either channel.
- **Lighting changes what a user SEES.** Crop-active named regions
  (`set_named_crop(dims, boxes, frames)`, which may be rotated) and per-voxel
  mask crops (`set_crop_mask_planes`, 0/1 per texel) are display state. They are
  **not** carried here and they narrow no coordinate on either channel — which
  is the whole of their protocol consequence, since *nothing* on either channel
  bounds what a plugin may read.

**So `crop` describes the right set**, not a lossy envelope around a visible
one: along one column a region crop may leave `[2,4)` and `[8,10)` lit with a hidden gap,
and `crop` reporting `[2,10)` is **right**, because every voxel in `[2,10)` is
addressable and a plugin reading one gets data.

**What makes a single interval sufficient is CONVEXITY, not axis-alignment.** A
ray meets the intersection of convex sets in one segment. A rotated region box
is convex too and would also give one segment; what cannot be an interval is the
**union** of several, which is what a set of region crops is. Stating the
property as axis-alignment makes the argument look narrower than it is and
invites the wrong counter-example.

**And the crop travels as EDGE coordinates in floats, not as indices.** A
boundary sits at a pixel *edge*, so integer pixel `i` occupies
`[i − 0.5, i + 0.5]` and a crop of pixels 25 through 1519 is
`{24.5, 1519.5}` — which is the half-open integer range `[25, 1520)` said
exactly rather than approximately. `EdgeBox` carries it; `extent` stays an
integer `Extent`, because the dataset's pixel range is exactly integral and has
no precision question to answer.

> **Converting to included pixel centres is `ceil(lo) ..= floor(hi)`, inclusive
> at both ends. An exactly-integral bound is a hit.**

**The .5 is the point, not an artefact.** A crop the user sets on pixel
boundaries always lands on a half-integer, so the conversion above sits half a
pixel away from a tie rather than on one — that half-pixel is the margin
absorbing rounding and float error, and it is why call sites converting
slightly differently do not diverge. An integer wire would let them diverge by
a whole plane.

**Edges also make the level conversion well-defined, which indices cannot.**

> **A level-0 edge `e` is exactly `(e + 0.5) / scale − 0.5` at level N.**

**The map is AFFINE, not proportional, and the offset term is the grid's phase.**
The host's pyramid block-downsamples anchored at index 0, so level-N
pixel `j` covers level-0 pixels `[j·s, j·s + s − 1]` and its *centre* sits at
level-0 coordinate `j·s + (s − 1)/2` — not at `j·s`. Inverting that is where the
`± 0.5` comes from. Dropping it shifts every bound by `0.5·(s − 1)/s` level-N
pixels, which is 7.5 level-0 pixels at level 4, and it fails **inward** at the
low face: it silently omits a plane that is inside the crop.

The proportional form `e / scale` is wrong on roughly 63% of crops at every
level ≥ 1 — the worked example above is one of them, giving `13..759` at
level 1 where the truth is `12..759` — and agrees with the affine map at
level 4 of that example, so a check at one level does not distinguish them.

**`LevelScale` is the SAMPLING factor — the level-0 samples one level-N
sample stands for — and it STOPS when the axis does.** For vizi's pyramid
the factor is `2^N` per axis while the axis is still folding: the downsampler
discards an odd trailing sample, so `31 → 15` is a scale of 2 and not
`31/15`, and the level-N shape of an axis is `max(1, floor(shape_0 / 2^N))`.
A fold needs two samples, so **an axis that has shrunk to one sample keeps
the factor of its last fold**, `2^min(N, floor(log2 shape_0))` — the pyramid
copies the lone sample from there, and that is how many level-0 samples it
covers. (`2^N` would say the sample stands for twice as much data at every
deeper level while the data does not change: a 3-sample axis's lone sample
sits at level-0 `0.5`, and `2^N` puts it at `1.5`, then `3.5`, past the
axis's end.) With the
factor that stopped, the map above applies unchanged at every level with no
special case; a plugin converting a crop into a level still clamps the
included centres into the level's shape, because a face past a discarded
trailing sample converts to a centre the level does not have — true of any
odd axis at any level, not only a frozen one.

The map is exact for any scale and composes across chained
levels — `((e+0.5)/2 − 0.5 + 0.5)/2 − 0.5 = (e+0.5)/4 − 0.5` — so the reason for
choosing edges over indices holds: a level-0 *index* has no exact level-N
index at all, which is why `doorbell_E_input.md` §3 forbids normalising
coordinates between levels. A plugin converts the crop into its record's level,
then to centres, in that order; doing it the other way rounds twice.

**This is also the representation an ROI-shaped crop would need**, if one is
ever allowed to specify the same thing.

**One case is deliberately NOT excepted: MIP.** A projection pane reports the
clip bounds like any other, because the crop is what an interaction *means* and
that does not change with how the pane draws. The reference host does not
apply the crop's Z range in MIP, so a projection can light voxels the report
excludes — a renderer discrepancy, not a second rule here. Carving out an exception would make a plugin's coordinate depend on a
display mode, which is precisely the coupling §8.11 exists to prevent.

**This agrees with the doorbell layer.** `doorbell_E_input.md` §3's column ranges and `RAY` segments
intersect the projection window, the global crop and the dataset extent — three
**convex** terms, so a ray meets their intersection in one segment and a single
interval is exact rather than a compromise. (Convexity is what matters, not
axis-alignment: a rotated region box is convex too; what cannot be an interval is
a *union* of them.) The
rejected alternative was carrying per-column or per-ray visible intervals, on
two grounds: the count is unbounded, and it puts state that changes at human
rate onto a 1 kHz record whose consumer is choosing *which voxels to operate
on*, not what to draw.

**What a plugin must not infer: that an unlit voxel is absent.** The converse
needs no warning — lighting is applied *inside* the crop, so a lit voxel is in
the crop by construction and the containment runs one way only. A plugin that
genuinely needs the lit set needs a scene feed keyed on `view_id`, which §15
records as absent — a better home for it than a coordinate field, because
lighting is scene state.

**Clipping does NOT bound what a plugin may READ, and that is not an
inconsistency.** "Out of bounds" here is *conceptual, and about the user*: a
click is the user pointing at something, so its coordinate has to mean what the
user meant, and the user means the volume they are working in. A read is the
*plugin* asking, and a plugin is entitled to its own reasons — an affine
transform needs samples the crop excludes, and a tool may legitimately reason
about geometry outside the crop, or outside the dataset entirely. So
`ReadDataVolume` serves what it is asked for, `ViewState`'s IDL comment is right
as written (*"a render clip — reads always return uncropped data, the plugin
applies the box itself if it wants to"*).

**A coordinate outside the crop is still delivered, flagged rather than
suppressed.** That is `E`'s `MISS`
(`doorbell_E_input.md` §3, "Pointing outside the data"): the host reports the
coordinate it has and flags it as not *relevant* — which is not the same as not
existing, and the rule above is explicit that a plugin may read those voxels — so
a lasso drawn
*around* what it selects stays expressible and a plugin doing something
deliberately odd is not prevented — it is merely told. Which is the shape of the
whole rule: **vizi says what is relevant. It does not say what is permitted.**

```
TimeMapping {
  gen
  revision            // u32, monotonic per gen; 1 for the first
  repeated double t   // one per time index, ascending
}
```

**An ordinary arrangement** (§5.5): sent once per `gen`, re-sent with a new
`revision` if it changes, acknowledged, deadlined. That it is one is
`doorbell_implementation_details.md` §5's rule — the host may not emit an `E`
record naming *"a `gen`'s time mapping"* the plugin has not acknowledged — and
it is why the epoch can reference it by revision instead of carrying it.

**Absent an arranged mapping a plugin may not assume uniform spacing** — silence
means unknown, not "multiply by a constant". `revision = 0` in a `DatasetState`
is that state, expressibly, and it is the common case: most datasets have no
mapping and never will.

**The ordering is the usual one and it costs nothing here.** The mapping is
arranged at open, long before any epoch change, so a `TimeMapping` almost never
sits on the path of one. Where it does — a mapping arriving mid-session — the
host sends the mapping, waits for its acknowledgement, and only then sends the
`EpochChange` naming the new revision. Two round trips at human rate for the
rare case, rather than an O(T) payload on every crop drag for the common one.

### 8.12 Segments

The memory layer's half. Inventoried in `shared_memory_implementation_details.md`
§2 and specified in `shared_memory_protocol.md` §3, §8 and §9; the delivery
rules are this document's.

| Message | Direction | Class | Notes |
|---|---|---|---|
| `InstallSegment{segment_id, purpose, supersedes, regions}` | host→plugin | **arrangement** | each `Region` carries `producer` (`shared_memory_protocol.md` §3.1) |
| `GrantAck` — success or a classified failure | plugin→host | ack (**one-shot**) | |
| `GrantStateQuery{segment_id}` | host→plugin | one-way | |
| `GrantState{segment_id, state, …}` | plugin→host | push (**one-shot**, echoes the query's id) | |
| `RetireSegment{segment_id}` | host→plugin | one-way | |
| `SegmentReleased{segment_id}` | plugin→host | push (**one-shot**) | |
| `ReleaseRequest{segment_id, reason}` | plugin→host | push (**one-shot**) | |
| `SegmentShortfall{segment_id, region, wanted_bytes, largest_free_bytes, dropped}` | plugin→host | push (**coalescing**) | §5.6 |

**A chunked overlay's `O` grant is host-sized and follows the dataset** (§5.7):
it is not granted at session start with the manifest's other overlays but when
a dataset is open, sized from that dataset's level-0 and level-1 volumes,
and re-granted at a new `segment_id` naming the
old in `supersedes` when the dataset changes — `shared_memory_protocol.md`
§9.5's idiom, applied to a plugin-produced region. It uses the ordinary overlay
table (the lane goes unused) so every rule about region 1 holds. §9's growth
ceiling is not measured on it, since the host chose the size; the session
ceiling is. A dataset it cannot fund is not granted at all -- `GrantFailed`
is the PLUGIN's answer and travels only plugin→host -- so the host marks the
overlay unavailable for that dataset (Plugin Manager shows why), retires the
arena it held for the previous one, and the overlay's wants stay empty.

**A grant is two halves on two channels and either may arrive first**: the
capability on the doorbell as `M`, the meaning here as `InstallSegment`. Neither
waits for the other. The plugin keys both by `segment_id`, holds whichever
arrives first, and **emits the success acknowledgement when the second lands** —
the condition being *mapped ∧ preamble validated ∧ meaning applied*, as specified
in `doorbell_M_memory.md` §7, whose failure classes go with it. What this section
owns is how those messages are delivered. **A
failure is reported as soon as it is known**, because the missing half cannot
rescue a mapping that did not work.

**The receiver's rule, which every form below rests on: a descriptor into a
segment the receiver has not yet installed is HELD unread, whichever half is
missing, and read when the installation completes.** Not failed, not reported,
not treated as stale — held, and dispatched in arrival order when the segment
reaches *installed*. The grant's own deadline (§9) is the backstop against a
half that never comes: the segment stays *pending*, `GrantStateQuery`'s
`pending` row is exactly that case, and a descriptor held for a grant that
fails or escalates is dropped with it. The unlock is the handoff — the moment
the receiver has the mapping *and* its meaning — and nothing the host does
about ordering on its side can substitute for it, because the two halves
travel on two channels and either may land first.

**Given that rule, `InstallSegment` travels in one of two forms, and both are
conforming:**

- **Embedded — "rides" — as `DataVolume.install`**, present on the first
  transfer into a data segment and on a ring re-grant, absent otherwise.  What
  it buys is a round trip on a drag-rate path, and a structural guarantee: a
  receiver cannot observe the descriptor without the installation, so on that
  path there is nothing for a queue to separate.
- **Standalone, ahead of the first descriptor, on §6's totally ordered stream**
  — the export grant.  `X` is granted once per export job, so a round trip
  there is nothing; the host sends `InstallSegment`, then `ExportChunk`s, and
  waits for no acknowledgement in between, because the receiver holds a chunk
  until the segment is installed.  `ExportChunk.install` remains a legal field
  (a receiver applies it exactly as `DataVolume.install`) and this host does not
  set it.  **`MaskVoxels` takes this form too**: it allocates from the same
  ring as `DataVolume` and has no `install` field to ride, so a mask reply
  that declared the ring — a mask-first request, or a mask the ring had to
  grow for — is preceded by the standalone `InstallSegment`, and the receiver
  holds the reply as it holds a chunk.

`InstallSegment` is its own `HostMsg` arm, so it can always be sent alone; the
standalone form would be unsafe only on a receiver that fails a descriptor it
cannot yet read, which the hold rule above forbids.

**Applying an installation is not the same as being able to read**, and this is
the case a first implementation gets wrong — it is the hold rule's other half.
The installation is the *meaning*; the mapping arrives on the doorbell as `M`,
and either may be first.  A receiver holding the meaning and not the mapping
records the region table, holds every descriptor into that segment unread, and
completes the reads and the acknowledgement when `M` lands.  A receiver holding
the mapping and not the meaning holds the descriptors likewise until the
installation arrives.  `ExportChunk` carrying `inline` names no segment, so an
`install` beside it is a `ContractViolation` (`install_without_segment`)
rather than a grant.

**And a request's deadline is SUSPENDED while a descriptor it carries is held
for a half the host has not delivered.** `ExportChunk` is a request with a
120 s deadline (§9) whose reply the receiver cannot produce while it is holding
the descriptor unread — so without this the host would fault a peer for waiting
on a half the *host* owes.  The clock resumes when the missing half lands.
**A host meets this by ORDER rather than by a timer**: it dispatches `M` at the
grant and sends the installation before any descriptor, so by the time a
request is issued both halves are already in flight on their channels and the
only wait left is delivery — and a capability that never lands is the grant's
§9 escalation, which fires before the request's deadline does.  `DataVolume`
needs no equivalent: it is a reply rather than a request, and the grant's own
missing-ack path already covers it.

**A success acknowledgement carries the identity proof, READ OUT OF THE
MAPPING** — the 16 bytes at `[12..28)` of the segment preamble
(`doorbell_M_memory.md` §5) — and **the host MUST compare it against what it
wrote.** Both halves of that are load-bearing and the second is the one an
implementation skips:

- **The proof is never in the `M` record**, so it cannot be echoed. Echoing what
  you were told proves that a message arrived, which the acknowledgement already
  established; only a value read back through the shared pages proves the peer
  mapped *this* object.
- **The failure it exists for succeeds at every other check.** On Windows the
  SDK's `mmap(tagname=…)` reaches `CreateFileMappingW`, which is create-**or**-
  open: a name that does not exist yields a fresh, private, zero-filled
  section. Mapping succeeds, the size is right, an acknowledgement is sent, and
  the plugin writes results into pages the host will never read. A grant id, a
  duplicate-tolerant install and a status field all pass.
- **A fresh section is self-detecting on the plugin side too**, since
  `crc32` over 28 zero bytes is `0x807077e9` and a zero preamble fails its own
  checksum — but that is the plugin catching it. A host that receives a proof and
  does not compare it has collected evidence and thrown it away.

A mismatch is not `retryable`: it means the two ends are looking at different
objects, so the host marks that grant failed and allocates a new `segment_id`.

**The acknowledgement carries a failure *class*, not only a string**, and every
validation failure maps to exactly one — otherwise two SDKs classify the same
fault differently and the host recovers differently. The five classes and their
membership are `doorbell_M_memory.md` §7's table, `retryable` being a **closed**
list precisely so that the default is permanent.

**A duplicate grant is answered again with the same answer**, never held and
never re-derived: the per-segment state machine's terminal answers are
replayable, which is what makes "acknowledged once" a statement about *depth*
rather than about how many messages exist
(`shared_memory_protocol.md` §8). A duplicate arriving while the segment is
still *pending* is held, because there is still no answer to give — which is why
`GrantStateQuery`'s `pending` row exists and why it re-sends nothing.

**On a missing acknowledgement the host issues `GrantStateQuery`**, which is
idempotent, and the four answers and what each obliges are part of the protocol
rather than each host's invention (`doorbell_M_memory.md` §7). A failure of the
query itself is an ordinary session failure.

**The query is a one-way and its answer is a push, not a request/reply pair**,
for the same reason arrangement is (§5.1): a recovery message that queued behind
the in-flight slot could wait on the very command whose data the missing grant
carries. It echoes the query's id so the answer is correlatable without
occupying anything.

**This query is the grant path's only backstop.** A grant can be lost with the
send reporting success — on Windows the first doorbell send after an orderly
peer close returns `Ok` and only the next write reports — and nothing at the
transport layer can see that. The ack that never arrives is the sole evidence,
which makes the missing-ack deadline (§9) the load-bearing one.

**`ContractViolation` is symmetric.** Several of its sites — a `D` bit naming
an Owned region, an impossible Pulsed state word, a failed release CAS — are
equally detectable in a *plugin's* traffic, and `DoorbellShutdown` cannot
stand in: it is terminal and these are report-and-continue. Both directions
carry the arm, under the same rate bound and the same
rule that a repeat is not a new defect. `ViolationSite`'s `E`-record members
stay plugin→host in practice, because `E` flows one way.

**A release's `offset` is SEGMENT-relative — the descriptor's number, copied
back unchanged.** Not region-relative, and the reason is which mistake each
convention produces. Segment-relative has no arithmetic: a sender echoes what it
received, so getting it wrong takes a real bug. Region-relative requires
subtracting `region.offset`, and a sender that forgets produces a *small* offset
that is still a perfectly valid one — so the release lands on live bytes inside
another reservation and corrupts an allocator's bookkeeping silently, which is
the failure mode with no symptom until much later.

**`region` is redundant with `offset` on purpose, and that redundancy is the
check.** The receiver verifies the offset falls inside the named region; a
disagreement is a `ContractViolation` rather than an applied release.

**The RELEASING end resolves the region, from the region table it was
installed with** — `SegmentRef` carries no region, so there is nothing to echo
and the redundancy above assumes the releaser holds the table. This has to be
said because the alternative an implementation reaches for is defaulting to
region 0, which produces exactly the `release_region_mismatch` the field exists
to catch, against itself, on any segment whose ring is not the first region.
Two fields
that must agree turn a class of mistargeting into something *detected* instead
of something *performed* — which is the only reason to carry a field a correct
sender never needs.

**Release has no timeout.** Memory cannot be withdrawn on any supported
platform, so a timer could only report, and an outstanding-segments count
belongs in diagnostics (`shared_memory_principles.md` B2).

**`ReleaseRequest` is a hint and the host owes it nothing** — not an
acknowledgement, not a retirement, not a deadline. It is the plugin's only way to
say it is done with a segment it may not release on its own, and the plugin keeps
serving that segment unchanged until `RetireSegment` arrives, which is what keeps
a descriptor already in flight legal. Its `reason` is diagnostic and is never
acted on. `shared_memory_protocol.md` §9.6 is the rule; what belongs here is the
delivery class, and it is **one-shot for the ordinary reason**: losing one costs
the plugin a mapping until session end, which is waste and never corruption, and
a queue that replaces would drop the only copy of a message that is never
restated.

**It is also the one plugin→host message the host may ignore entirely**, which is
worth naming because §5.4's one-shot class is otherwise about things that must
not be lost. The distinction is between *delivery* and *action*: the message must
arrive, because a diagnostic that says which segments a plugin has given up on is
exactly what B2's outstanding-segments line is for; what happens next is the
host's memory policy and no plugin steers it (§11).

**`RetireSegment` carries one obligation on the HOST, and it is this document's
because it is about a message here.** Before retiring a segment the host must
have released every loan it holds inside it (§5.6), because a plugin whose arena
is full is waiting on exactly that release and cannot finish work it may not
start afresh. The rest of the retirement lifecycle — what *retiring* permits,
what "released" means on each platform, and what happens to a segment that was
never installed — is `shared_memory_protocol.md` §9.1 through §9.4.

### 8.13 Doorbell faults and teardown

The messages `doorbell_protocol.md` §8, §10 and §12 name.

| Message | Direction | Class | Meaning |
|---|---|---|---|
| `DoorbellFault` | plugin→host | push (**one-shot**) | the plugin's doorbell hit a framing fault; it is closing |
| `DoorbellShutdown` | host→plugin | one-way | the host is closing the doorbell; here is why and how long you have |
| `DoorbellFinalReport{detail}` | plugin→host | push (**one-shot**) | the peer's diagnostic inside the grace window |
| `ShutdownReport{outcome, detail}` | plugin→host | push (**one-shot**) | the plugin's own account of its SESSION shutdown, §3.3; distinct from the doorbell's report above |
| `ContractViolation` | **both** | push (**one-shot**, rate-bounded) | a well-formed record whose *content* was wrong; symmetric (§8.12) |

**`DoorbellFault` ends the doorbell at once, and the session after the grace
window** (`Shutdown{session_fault}`, naming the doorbell's failure): a session
without its doorbell can be granted no memory. A plain hang-up opens the same
window, as `DoorbellShutdown{peer_closed}`. Its full field set,
the per-class presence table, and the rule that every absence is representable
rather than zero-filled are `doorbell_protocol.md` §12's; this document adds
only the delivery rules:

- it is **one-shot** and MUST NOT share a coalescing queue;
- **exactly one is sent per terminal event.** A transport may surface that event
  through more than one view — a non-blocking terminal mailbox the supervisor
  reads immediately, *and* the ordered outcome stream where it appears in
  sequence — because the first is what makes reporting-before-release possible
  while a consumer is blocked. Those are two views of one fault, not two faults,
  and a supervisor that reports both gives the Plugin Manager two diagnostics for
  one event and invites a reader to treat the second as a new defect;
- it is sent **before** the doorbell socket is released, while this channel is
  still usable — that ordering is the entire point of reporting on the other
  channel;
- **delivery is not waited on.** The peer hands it to its transport and closes;
  gRPC is a failure detector, not an acknowledgement.

**And it is COMPOSED, not forwarded.** `doorbell_protocol.md` §12's field list
asks for plugin id, manifest source, declared and SDK versions, both peers'
protocol versions, and whether the process and this stream were healthy at
detection. **The doorbell transport owns none of those** — its boundary
(`doorbell_implementation_details.md` §6) excludes supervising a session, so it
cannot know a manifest source or a stream's health, and an implementation that
asks it to will get an empty field or an invented one. So:

> **The transport supplies what it observed; the supervisor adds the session
> facts and sends the message.** Neither half is optional and neither can
> produce the other's.

Two consequences worth stating, because both are ways the composed message ends
up lying:

- **A field the supervisor cannot establish is absent, not zero-filled.** §12's
  presence table already requires that for the transport's half; the same rule
  covers the supervisor's, and `plugin_principles.md` 11 is why.
- **"Healthy at detection" is a snapshot with a known blind spot, and it must be
  labelled as one.** §9 records that the process check and the stream's health both
  stay green for a peer whose doorbell listener has died. A report that says
  *"gRPC healthy"* is saying only that those two checks passed at that moment —
  which is exactly the evidence a reader needs, and exactly not a claim that the
  peer was fine.

**`DoorbellShutdown{reason, failure_class?, deadline_ms, detail}` is the host's
counterpart**, because `DoorbellFault` is plugin→host and the host has no
symmetric op. Its six reasons — `retry_exhausted`, `peer_not_draining`,
`framing_fault`, `peer_fault`, `local_transport_fault`, `host_shutdown` — are
`doorbell_protocol.md` §10's, and two of them exist so that both ends' accounts
of one event agree: `framing_fault` says the host saw the peer's traffic go
wrong, `peer_fault` says the host is answering the peer's own `DoorbellFault`.

**A doorbell that closes ends its session, politely.** Whatever closed it — the
peer's `DoorbellFault`, the host's own framing or transport fault, a send-side
escalation, or the peer hanging up — the host stops emitting at once, answers
with `DoorbellShutdown`, and when the grace window closes (or the peer's
`DoorbellFinalReport` closes it early) sends **`Shutdown{session_fault}`** whose
`detail` names the doorbell's failure, and records the same reason where Plugin
Manager shows it. The session is not kept: every grant is an `M` on the doorbell,
so a session without one can be given no new memory, and keeping it only moves
the failure to whatever it tries next, where nobody can attribute it. There is no
repair. A plugin therefore **does not end the session itself** on its own fault
or on the doorbell's close: it reports, keeps its stream open, and ends when the
host's `Shutdown` arrives (§3.3), which is what lets the report be delivered and
answered.

**`deadline_ms` is a duration measured from the host's send**, never an absolute
time — two processes agreeing on a clock is a problem this exchange has no
reason to acquire — and at the receiver it is an **upper bound, not a budget**,
because the host's timer starts when it sends and expires whether or not the
message arrived.

**`ContractViolation` is not `DoorbellFault`**, and the two cannot be one
message: a fault is terminal, a violation reports and drops and the session
continues. Its shape, its `where` enum, its rate bound and the `zero_id`
exemption are `shared_memory_implementation_details.md` §2's.

**The host MUST consume a `ContractViolation` that names a grant**
(`where = zero_id`), because that grant can never be acknowledged: the ack keys
on `segment_id` and the record named none. Without consumption the grant is
pending forever, the missing-ack deadline fires, and the query-then-resend path
re-sends the same malformed record indefinitely. On receipt the host marks that
grant **permanently failed** and does not retry — the defect is in vizi's own
send and re-sending reproduces it.

### 8.14 Processing stages

**The reference host drives a chain of one stage** — an applied filter
(vizi v0.9.0 and later) — and no chain of several. `processing_stages.md` is
the whole of it; the messages are listed here so this section's inventory
stays complete.

| Message | Direction | Class | Reply |
|---|---|---|---|
| `PlanStage{stage_id, input, params}` | host→plugin | request | `StagePlanned` \| `Error` |
| `StageArm{stage_id, revision, segment_id, pipeline, position, input, output, new_channels, params}` | host→plugin | arrangement (§5.5), identified by `(pipeline, position)` | `StageArmAck{stage_id, revision, pipeline, position}` |

A turn itself carries no control-plane message: it is the slot's turn word,
hinted by `D` in each direction (`processing_stages.md` §6). A stage that finds a
slot invalid reports `ContractViolation` at `STAGE_SLOT_INVALID`, and one that
cannot use an arm reports at `STAGE_ARM_REFUSED` and does not acknowledge it.

### 8.15 Set-view: a plugin moves the view, when the person asked it to

> **vizi's host implements every target, both kinds of cause and Follow**, the
> panel row, the notice and Back; the orbit point is held through Fly.

| Message | Direction | Class | Reply |
|---|---|---|---|
| `InvokeCommand.cause` | host→plugin | field | a `u64` token per interaction with a `moves_view` widget; 0 otherwise |
| `ViewRequest{cause \| tool_cause, decline, t?, z?, select_region?, frame_region?, look_at?, orbit_point?}` | plugin→host | request | `ViewRequestResult{cause, outcome, reason, detail}` |
| `OfferFollow{panel_id, label}` | plugin→host | coalescing, key (panel) | none; `""` withdraws |
| `FollowStarted{follow_id, panel_id, targets, resumed}` | host→plugin | one-shot | none |
| `FollowState{follow_id, seq, t?, z?, select_region?, frame_region?, look_at?, orbit_point?}` | plugin→host | coalescing, key (follow) | none; a bad state ends the Follow |
| `EndFollow{follow_id}` | plugin→host | one-shot | `FollowEnded{PLUGIN_ENDED}` |
| `FollowEnded{follow_id, reason, detail}` | host→plugin | one-shot | none |

A plugin's traces, heatmaps and lists are for finding the frame or the region
worth looking at, and then the person has to go and look. Without this, a
plugin can only print "look at t=430". **Arbitrary set-camera or set-selection
requests would give viewport chaos**: plugins fighting each other and the
person, and moves nobody can trace to a cause. So the controls are a closed,
enumerated list, and **every move must be caused by something the person did
to that plugin.**

**Four rules, all enforced by the host.**

1. **No cause, no move.** Every `ViewRequest` names a cause: a token the host
   minted when the person interacted with that plugin's own surface. A plugin
   cannot make one up, and one running in the background has nothing to name.
2. **The person always wins.** A cause is valid only until the person next
   changes what it would change. If they moved `t` after the click, a request
   to set `t` from that click is refused `SUPERSEDED`. A request never undoes
   something the person did afterwards.
3. **Declared in advance, off at every launch, and only while the panel is
   up.** A manifest declares, per panel and per tool, what may be moved
   (`view_targets`, `manifest_schema.md` §3.3). The person turns it on with
   **the first row of the plugin's panel**, which the host draws: off every
   time the panel opens, never remembered, not part of the plugin's widget
   tree, and **never communicated to the plugin**. It governs the panel's
   widgets and, on a panel that declares `view_targets`, the plugin's tools
   (a panel that only offers a Follow does not), and it counts only while
   that panel is open and showing. Otherwise requests are refused `NOT_ENABLED`, which is
   all a plugin learns of the row.
4. **Every move is visible and can be undone.** The host shows which plugin
   moved the view and to what, and one Back returns the person to the view
   they had before any plugin took over.

**What can be moved (`view_targets`), append-only:**

| Target | What happens |
|---|---|
| `t`, `z` | set the frame / plane |
| `select_region` | as a single click on the region |
| `frame_region` | as choosing the region in its list |
| `look_at` | aim the camera at a voxel (`LookAt{target_zyx, fit_extent, direction_zyx}`) |
| `orbit_point` | set the one orbit point (`OrbitPoint{zyx}`) |

`select_region` and `frame_region` behave exactly as the host's own gestures
do, including their conventions. For example, a region that carries a camera
view is selected but not flown to, as a casual click on it is. `look_at` is
deliberately not a matrix: the host works out the pose for its current camera
mode, and the target becomes the orbit point unless the same request sets
`orbit_point`. There is one orbit point, and `orbit_point` sets it in every
camera mode. Deliberately excluded: crop, channel visibility, contrast and
LUTs, camera mode, dataset open and close, layout, and anything saved to
preferences. Those are the person's settings, not navigation.

**Where a cause comes from.**

- **A `moves_view` widget**: button, slider, dropdown, checkbox, line edit,
  picker, a selectable `list`, or a clickable `image`. Each interaction's
  `InvokeCommand` carries a fresh `cause`.
- **A slider is a cause stream.** Every change to a `moves_view` slider — a
  drag, the wheel, the keys — is a value in the stream, at most 30 a second,
  the first at once and the newest when the window closes, so a drag that
  pauses is sent where it paused. **One call is in flight per stream**: while
  the plugin is answering one, later values wait and only the newest is sent
  when it returns, with a cause minted then but dated when the person moved
  the slider, so a change they made in between still wins. A newer cause
  supersedes an older, unanswered one from the same widget of the same
  plugin. The person's hand is on it throughout, so it is the person doing
  the driving.
- **A tool, click only**: a `FIRE`-mode tool, or a declared action's `FIRE`,
  with the tool's `view_targets`, governed by the plugin's panel row (rule 3). The cause is the record's `(tool,
  interaction)` pair, sent as `ViewRequest.tool_cause`. A PRESS, DRAG or
  RELEASE is never a cause: a view that moves during a drag fights the hand
  doing the dragging.
- **Never a cause:** timers, pushes the plugin received from the host, or
  other plugins. Movement not driven by the person's hand is Follow (below).

**The request, and its one answer.**

- **Exactly one cause**, and at least one target unless `decline` is set. A
  request with no cause, two causes, or a decline with targets is `INVALID`.
- **All or nothing.** Every target must be valid (declared, in range, the
  region existing), or none applies.
- **A plugin answers every cause from a `moves_view` surface, moving or not.**
  `decline: true` with no targets releases the cause (`DECLINED`), for example
  a click on an empty part of a heatmap. A plugin that stays silent is not in
  error, but the widget shows as pending until the cause expires, so declining
  promptly is what keeps a panel responsive.
- **One answer per cause, idempotent by the cause.** Re-sending gets the same
  result without applying twice; a different request under a spent cause is
  `CONSUMED`.
- **The window is bounded** (`plugin_principles.md` 8). A cause expires 2 s
  after the interaction (`STALE`). A plugin SHOULD answer within 1 s. The host
  marks the originating widget pending after 0.5 s, so a slow jump still reads
  as caused by the click.
- **The reply is the acknowledgement.** A plugin must not infer success from a
  later `ViewChanged`.

**The person's own animations.** A request behaves like the host's equivalent
gesture: setting `t` or `z` pauses playback, as stepping does, and `look_at`,
`frame_region` and `orbit_point` leave Autospin running about the new pivot.
**While a movie export runs, it owns `t` and the camera**, and every request is
refused `EXPORT_ACTIVE`.

**Follow: timed, plugin-driven movement** ("tour these twelve cells", "step
through the flagged frames"). The person starts it, the plugin drives it, and
any input from the person ends it.

- **Declared.** A panel names what a Follow may move in `follow_targets`
  (`manifest_schema.md` §3.3.1), from the same list as `view_targets`.
- **Offered, never started, by the plugin.** `OfferFollow{panel_id, label}`
  shows a **Follow** button, labelled with `label`, in the panel's first row
  (rule 3); `label: ""` withdraws it, and a Follow running from that offer
  ends `WITHDRAWN`. An offer is panel state: sent before the panel opens, it
  is shown when it opens. It is a coalescing push, keyed by the panel.
- **The first row is the master switch.** While the person's switch is off
  the Follow button is disabled, and turning the switch off ends a running
  Follow (`STOPPED`). The plugin still learns nothing of the row.
- **The person starts it** by pressing Follow. The host sends
  `FollowStarted{follow_id, panel_id, targets, resumed}`: a fresh `follow_id`
  and the panel's `follow_targets`. **Only one Follow runs at a time,
  host-wide**: starting one ends any other (`REPLACED`). Starting one is a
  hand-over, so Back returns to the view from before it.
- **The plugin streams states, not deltas.** `FollowState{follow_id, seq, …}`
  names any of the Follow's targets, as `ViewRequest` does, with no cause. A
  coalescing push keyed by the Follow, so the latest wins; the host applies at
  up to 30 Hz, ignores a `seq` not above the last one applied, and drops any
  state for a Follow that is not running. A state applies whole or not at all,
  and **a state that cannot apply** — a target not declared, `t` or a point
  out of range, a region that does not exist — **ends the Follow `INVALID`**
  with a `detail` saying why, since a coalescing push has no reply to carry a
  refusal. The host eases the camera between states, and how is the host's
  own: nothing on the wire says how long a move takes.
- **It is always visible**: the panel's first row is lit, with **Stop**; the
  views carry a coloured border and a "Following *plugin*" note; the playback
  bar says so while `t` is driven.
- **The person wins, and winning ends ALL of it.** Any input from the person
  on something the Follow drives — scrubbing or Play for `t`, a plane change
  for `z`, any press in the 3D view (a click that only picks a region included:
  the person has taken the view), the wheel or keys for the camera targets, selecting
  a region for the region targets — ends the whole Follow, not just that
  target (`USER_TOOK_CONTROL`). Input on something it does not drive (turning
  the camera during a tour of frames) leaves it running. The host then offers
  **Resume**, which starts the same offer again with `resumed: true` and
  leaves where to pick up to the plugin. Starting Follow
  on `t` pauses playback; starting it on the camera turns Autospin off, and
  turning Autospin on ends it (`AUTOSPIN`). While a Follow runs, a
  `ViewRequest` that would change anything it changes is refused
  `FOLLOW_ACTIVE` — judged by what the targets touch, not by their names:
  `frame_region` changes the selection and the camera, so a Follow on it also
  holds `select_region`, `look_at` and `orbit_point`.
- **It also ends** on Stop (`STOPPED`), a movie export starting (`EXPORT`),
  a different dataset (`DATASET_CHANGED`), the panel closing or hiding
  (`PANEL_CLOSED`), the plugin's own `EndFollow{follow_id}` (`PLUGIN_ENDED`,
  "the tour is done"; one naming a Follow that is not running is ignored, as
  it is most often a Follow that ended as it was sent), and the session
  ending, which sends nothing. Every
  other end sends `FollowEnded{follow_id, reason, detail}` exactly once.

**Back.** The host keeps a single saved view — `t`, `z`, the camera, the orbit
point and the selection — as it was when control first passed from the person
to a plugin. Further plugin moves do not overwrite it; any view input from the
person makes the view theirs again, and the next hand-over saves anew. Every
move shows a notice naming the plugin and what it changed, with Back.

### 8.16 Long-running work: progress, cancellation and supervision

| Message | Direction | Class | Reply |
|---|---|---|---|
| `WorkProgress{request_id \| task_id, title, progress?, text, done, cancellable}` | plugin→host | push, **coalescing** by its subject | — |
| `CancelWork{request_id \| task_id}` | host→plugin | **one-way** | — (the named request's own reply) |
| `Error.kind = CANCELLED` | plugin→host | a request's reply | — |

Work a plugin does comes in two kinds, and the person is shown both:

- **A request the host is waiting on** — a command, a plugin reader's open or
  read, an export chunk. Its subject is the request's own `id` (§4). It holds
  the session's in-flight slot for as long as it runs (§5.1), so the plugin's
  next request waits behind it.
- **A background task** the plugin runs alongside, blocking nothing: what a
  command that returned promptly goes on doing on its own thread (§8.3). Its
  subject is a `task_id` the plugin chooses; its first `WorkProgress` begins it
  and one with `done` ends it, `text` then being its outcome.

**`cancellable` is advice to the host's UI, not a contract.**
It tells a host whether to offer Cancel for a task; a host SHOULD NOT send
`CancelWork` for a task that said `false`.  If one arrives anyway — a host out
of step, or a race with a task that has just changed its mind — the plugin sets
the task's cancelled flag as for any other, and MAY ignore it: cancelling is
cooperative, so a task that cannot stop simply goes on.  It is not a violation.

**`WorkProgress` is a coalescing push** (§5.4) keyed by its subject, so a
plugin reports as often as is convenient and the host shows the latest.
`progress` is §8.4's `Progress` — a fraction, or `indeterminate`; absent means no
measure, only `text`. `title` names a task and is ignored for a request, which
the host names itself (a command by its manifest `title`). A report naming a
request the host is not waiting on is dropped: its answer overtook it.

**`CancelWork` is cooperative, and a request keeps its slot until it is
answered.** It is a host one-way (§5.2), so it leaves at once rather than
queueing behind the request it names — the request that holds the slot. The
plugin stops when it notices and answers the request as it would any other:
with `Error{CANCELLED}`, or with whatever it had. Nothing is freed early,
because the plugin is still doing the work until it answers, and a host that
sent the next request would only queue it. Naming work already finished, or
unknown, is a no-op. **`CANCELLED` is not a fault**: a host records nothing
against the plugin for it and does not mark the session suspect.

**A request is supervised when a person can see it and stop it, and a
supervised request has no deadline.** §9's fixed timeouts exist because a silent
peer is *unknown* (§9's first rule), and nobody is watching to decide. Once the
person can see the work — how long it has run, when it last reported — and can
cancel it, the decision is theirs, not a clock's: a deadline would abandon work
they chose to wait for, while the request kept its slot anyway. So a host that
shows a request to the person MAY wait on it without a deadline, and a host
that does not keeps §9's. The reference host shows every command, plugin
reader read and export chunk in its activity list, and an open behind
the load dialog, whose Cancel sends `CancelWork` for it; with no window (a script, a probe) every
fixed timeout stands. It is the principle vizi applies to its
own long work: stalled is judged by the absence of progress, never by a
deadline sized on one machine.

**A cancel that goes unanswered ends in `Shutdown{USER_STOPPED}`**, at the
person's choice: the reference host offers *Stop plugin* when a cancel has gone
unanswered for 10 s — the unsupervised command budget, which a plugin that is
listening meets easily — and the plugin starts again when next used.

**Bounds** (§9): at most **64** open tasks per session, and a `title` or `text`
of at most **256** UTF-8 bytes. A sender cuts text at a character boundary; a
receiver cuts anything longer and does not show tasks past the bound. Progress
is coalesced, so it needs no rate bound.

The reference SDK's surface: `ctx.progress(fraction, text)` and
`ctx.cancelled` on a command's context; `report_progress(...)` and
`cancelled()` for any handler (a reader's `read_volume`, an exporter's
`write`), from the thread serving the request; `Cancelled`, raised to answer
`CANCELLED`; and `ctx.start_task(title, cancellable=)` returning a handle with
`progress`, `cancelled` and `done`, which also ends the task as a context
manager. A `CancelWork` is handled on the dispatch loop, never a handler's
pool, since that pool may be busy with the very work it cancels (§6).

---

## 9. Deadlines and liveness

**Every timeout on this channel means *unknown*, never *dead*.** They are sized
against a busy control plane — seconds, not RTT — and expiry triggers a query or
a re-send, not a teardown. Inferring death from slowness is the trap that makes a
refused doorbell write look like a dead plugin.

**And liveness cannot be inferred from the two checks a host has.** The
process being alive and this stream being healthy both stay true for a plugin
whose doorbell listener thread has died while this channel keeps answering, so
neither is evidence about the doorbell.

| Deadline | Owner | Value | On expiry |
|---|---|---|---|
| launch, spawn to `Ready` | host | **host policy**, one deadline over both phases; vizi: **60 s**, or the manifest's `entry.startup_s` held to 10–300 s (`manifest_schema.md` §2.3); **cancellable** by the person | kill, retain a diagnostic; the next request is a fresh launch |
| command handler | host | 10 s **unsupervised**; **none while supervised** (§8.16) | session **suspect**; offer restart — **held**, not spent, while the host shows a picker it was asked for (§8.9) |
| plugin reader open / read / metadata | host | 30 s **unsupervised**; **none while supervised** (§8.16) | as above |
| an unanswered `CancelWork` | host | **10 s** | offer the person *Stop plugin* (`Shutdown{USER_STOPPED}`, §8.16) |
| an unanswered `shape_commit` | host | **10 s unsupervised**; **none while supervised** (§8.16) | revert the edit, `ShapeEditEnded{REVERTED}` (§8.8) |
| export open / chunk / finish / abort | host | 30 / 120 / 60 / 10 s; a chunk: **none while supervised** (§8.16) | as above |
| plugin request | plugin | 30 s default | raise `HostError`; dead-session backstop |
| grant acknowledgement | host | **10 s** | `GrantStateQuery` (§8.12) |
| no progress on one `segment_id` | host | **30 s** | escalate; never loop |
| epoch acknowledgement | host | **10 s** | re-send idempotently, then escalate |
| tool activation acknowledgement | host | **10 s** | re-send idempotently, then escalate |
| doorbell grace window | host | **2 s** | tear down regardless |
| session shutdown grace window | host | **5 s** | tear down regardless (§3.3) |

**`Shutdown.deadline_ms` is ALWAYS set by the host**, and a receiver is entitled
to treat its absence as this table's 5 s rather than waiting for a number that
is not coming. Waiting is the reading that hangs against a non-conforming peer while
adopting merely agrees with one that is conforming. The deadline is the host's
to set and the plugin's to respect; a missing one is a host defect the plugin
reports — `where` = **`missing_deadline`** — and does not stall on.

**`deadline_ms` is `optional` for this rule's sake, and that is the whole reason
the field carries presence.** proto3 encodes an unset scalar and a zero-valued
one identically, so against a plain `uint32` a receiver cannot tell a host that
forgot the field from one that meant no grace at all — and the report would fire
on a conforming peer. The absence has to be representable before it can be
reported, which is `plugin_principles.md` 11's *expressible absence* applied to a
duration rather than to a measurement.

And the size and capacity bounds, which are not deadlines but belong with them
because they are the other half of `plugin_principles.md` 8:

| Bound | Owner | Value | When exceeded |
|---|---|---|---|
| maximum decoded message | host | **32 MiB** | refuse the message, fault the session |
| maximum inline byte field (`bytes` **and `string`**) | host | **8 MiB** (enforced at both ends: the host walks every plugin message at the drain, the SDK every host message on receive and its own on send) | refuse to apply; answer `Error.kind = PROTOCOL` (§1) where there is a reply, else report on the push's own refusal site where one exists |
| maximum repeated-field count | host | **65 536**, with the exceptions below (`SeriesList.entries` among them: bounded by the message ceiling alone) (enforced at both ends) | refuse to apply; answer `Error.kind = PROTOCOL` where there is a reply, else report `repeated_field_overflow` |
| host request queue capacity | host | **32** (the reference host's `call` queue is unbounded) | fail the `call` at admission (§7) |
| one-shot queue capacity | host | **256** | session fault (§7) |
| held-whole ops per widget | host | **256** per PENDING batch (§5.4) | session fault (§7) — the RECEIVER's backstop; §5.4 gives the sender's rule |
| coalescing live keys | host | **structural** — from the manifest (§5.4: widgets, overlays and the Follow keys), plus a fixed slack per chunked overlay: its table and 2 × the wants below (§5.7), plus 68 for `WorkProgress` (§8.16: the 64 tasks and a few requests), plus 4 for `shape_proposal` (§8.8: the drag in progress and the ones whose end is in flight) | unreachable for a conforming sender; a plugin putting more chunk keys in flight than the slack faults the session as an undeclared widget does (§7) |
| wants per `ChunksWanted` | host | **8** per chunked overlay (`MAX_CHUNK_WANTS`) — the 2D level, the 3D level, one prefetch, and room for policy | the host's own policy; never exceeded by construction |
| open background tasks per session | plugin | **64** (§8.16) | the SDK refuses to start another; a host does not show tasks past it |
| `WorkProgress.title` / `.text` | plugin | **256** UTF-8 bytes (§8.16) | cut at a character boundary, by the sender and again by the receiver |
| per-pass drain shares | host | **16 one-shot / 8 request / 16 coalescing, capped in the first pass and uncapped in the second** | — |
| retry budget before escalation | host | **3** | escalate (§9) |
| per-session shared-memory budget | host | **declared + 2× largest deposit + arena headroom, ceiling 4 GiB** (the ceiling is enforced on the sum of what the session holds mapped — released and failed segments leave it, a superseding grant's retiring ring is left out; the SDK refuses a grant it cannot map) | refuse the grant before anything is created: a deposit ring the budget cannot fund answers its read `Error{INTERNAL}` naming the budget; a peer-refused grant is `too_large` |
| arena growth ceiling | host | **4× the manifest's declared size** (enforced on a superseding `O` / `P` grant against the subject's first grant); **not measured on a host-sized arena** — a chunked overlay's, sized from the dataset (§5.7) | refuse the re-grant before anything is created |
| pending `D` hint segments per session | host | **256** (`PENDING_HINT_SEGMENTS`) — distinct granted segments hinting between two pump drains; hints for a segment already pending fold into it (`doorbell_D_notification.md` §4) | a hint for a 257th segment is dropped and counted; the count reaches the host as one diagnostic naming the cap, and the mandatory poll absorbs the loss |
| pending cancellations / unexpected frames per session | host | **256** each (`PENDING_REPORTS`) — held between polls, one per record from the peer | past the cap, dropped and counted; the same diagnostic as above |
| host-side outcomes taken per poll | host | **256** (`OUTCOMES_PER_POLL`) — escalations, faults, teardown from the channel; peer-driven records are folded on the socket thread and handed over whole, so this budget is never spent on a peer's flood | the rest wait for the next poll; nothing is dropped |
| retained superseded descriptors per session | host | **65 536** undrained (`SUPERSEDED_CEILING`) — the coalescing queue keeps what a displaced message named until the pump disposes it (§5.6, §12) | session fault (§7); host-caused when the pump is not draining, and the fault must say so |

**"Escalate" keeps the session.** When an arrangement or a grant exhausts its
budget, the host gives that one up, says so where a person sees it (the status
bar, the plugin's log and Plugin Manager's report), and leaves the rest of the
session running: a plugin whose epoch acknowledgement never came may still be
serving a reader or a panel, and ending it would take those away to report a
failure the report already names. Escalation starts no descriptor-producing
retries and does not re-arm the deadline. **A doorbell is the exception**,
because nothing on the fast path works without it: a doorbell that closes, for
any reason, ends its session after the grace window (§8.13).

**The reasoning for every value above is below**, so that a better
one can be argued for rather than merely preferred. None of it is a
*measurement*: each value is derived, from a constant already in the tree or
from arithmetic over the others, and each derivation names what it rests on so
that a measurement can be seen to overturn it.

#### Why these values

**8 MiB for an inline byte field.** Everything large on this platform is
supposed to travel through shared memory, so the ceiling only has to clear what
legitimately does not: `GeomBuffer.inline`, which is a real production path
(`set_items(inline=True)`, and `set_items_live` requires it), sized by the
manifest's `segment_mib` default of 32 MiB for *"~10⁵ f64 points"* — about
2.4 MB of geometry. `ExportChunk.inline` is *"small payloads / tests only"* by
its own comment. 8 MiB clears both several times over.

**A `string` is an inline byte field for this bound.** The bound exists to keep
large payloads off this channel, and a string is bytes with an encoding — so a
receiver that walks a message for the check walks `bytes` *and* `string`
fields. `MetadataResult.text` is the case that makes it matter: an OME-XML
document is a `string`, and one that clears 8 MiB belongs in a segment, not
in a reply. The dependency is the host's decode budget, so the answer is
mandated.

**A message refused over any bound in this table is answered `Error{kind:
PROTOCOL}`** where it has a reply, by §3.3's own reasoning for the window
refusal: the peer broke a stated bound, and `INTERNAL` would report the
refuser's machinery for the sender's mistake. **The reference
host does it too**: every plugin message is walked at the
pump's drain before it is dispatched, a request over a bound is answered
`PROTOCOL`, and a push over one is not applied and is reported —
`repeated_field_overflow` for a count, the push's own refusal site
(`overlay_item_refused`, `panel_patch_refused`) for an inline field, which has
no site of its own. The two size policies below are the broker's, applied
before a grant creates anything; a plugin never sees the machinery, only the
refusal.

**32 MiB for a message, sized by the ROI pose.** Nothing else the control plane carries comes near it: the inline
ceiling above, a metadata document, a panel patch, a time mapping at its own
bound (8 MiB of doubles). What can is a message carrying ROI poses — a
windowed `GetRois` reply, or a `ContributeRois` — because `Roi`'s pose is
*dense*: `roi.proto` makes it one row per frame, `3 + 3 + 6` doubles, so a
fully posed ROI costs **96 bytes per frame** (24 with only `pose_translation`
set). 32 MiB is therefore about **350 000 posed ROI-frames** (1.4 million
translation-only): a few hundred regions tracked over a thousand frames, or a
few dozen over a whole calcium timelapse. It is **not** a few hundred regions
over a few thousand frames — 360 ROIs × 6000 volumes is 198 MiB fully posed and
49 MiB translation-only — and that case is deliberately outside this ceiling
rather than the reason to raise it: a 256 MiB message is the wrong shape for
state that changes one region at a time. An unwindowed `GetRois` reply is
posed at the frame on screen with its pose cleared, so it never carries a
dense table; a windowed read is narrowed until it fits (§8.8); and a tracker
that grows its table sends the growth with `ExtendRoiPoses` (§8.8) rather than
re-sending every pose with `ContributeRois`. A shared-memory pose buffer was
the other candidate and was not taken — a segment grant and arena lifecycle
per increment is a great deal of machinery to move 24 bytes per region.

**The message ceiling stands between a peer's `repeated` field and the host's
address space.** The enforced value is four times the largest thing the
protocol sends outside ROI poses (an inline field or a time mapping at its own
bound, 8 MiB each) — which is the property a bound wants, not a value that
"should be enough".

**65 536 for a repeated field, except where the field scales with the data.**
Most repeated fields scale with the *manifest* — `PanelPatch.ops`,
`Geometry.channels`, `EpochChange.panes` — and are already structurally bounded
(§5.4); the numeric cap is a backstop against a peer that ignores its own
manifest. The fields that scale with something else get their own:

| Field | Bound | Scales with |
|---|---|---|
| `TimeMapping.t` | **1 048 576** | the dataset's time axis, at 8 MiB of doubles |
| `Roi.pose_translation`, `.pose_rotation`, `.pose_stretch` | **1 048 576** | the time axis again, via the span an ROI is posed over |
| `RoiPoseExtension.pose_translation`, `.pose_rotation`, `.pose_stretch` | **1 048 576** | the same span, arriving a batch at a time |
| `RoiList.rois` | **65 536** | user annotations; a compiled label volume is the case that grows it |
| `Geometry.levels` | **64** | pyramid depth, which is logarithmic in extent and cannot plausibly exceed it |

**The pose rows exist because 65 536 is reachable.** `roi.proto` makes those
three arrays dense — *"ONE ROW PER FRAME in the domain"* — at `3*span`,
`3*span` and `6*span` doubles, so the general backstop would fire at **10 922
frames** on `pose_stretch`, which a calcium timelapse reaches. `TimeMapping.t`'s
figure is reused because the argument is the same one.

**The extension rows are there for the same reason.** `ExtendRoiPoses`
carries the same three dense arrays, so a tracker can extend a pose by as many
frames per message as it can contribute.

> **The per-field bound is not the whole story here, and the message ceiling
> bites first.** One ROI posed over 174 762 frames is ~8 MiB of `pose_stretch`
> alone and 16 MiB fully posed, so at a 32 MiB maximum decoded message a
> `GetRois` reply carrying two such ROIs fails as a *message* whatever this
> table says. No repeated-field bound can fix that; see the 32 MiB paragraph
> above for what the ceiling holds.

**32 for the host request queue, matching `REQUEST_CAP`.** The two are the same
shape — a bounded set of in-flight requests in one direction — and giving them
different numbers invites a question with no answer. The bound must exceed the
plausible number of *concurrent timeouts*, not the number of callers, because
a timed-out caller leaves its entry behind (§7); 32 does, by a
wide margin, for a host whose requests originate on one UI thread and a small
pool.

**256 for the one-shot queue** (`ONE_SHOT_CAP`). Overflow is
a session fault, so the value has to sit comfortably above any legitimate burst
rather than merely above the average. One-shot traffic is session-rate with a
single exception — `ReleaseDeposit`, one per read — and 256 outstanding releases
means the host has served 256 reads the plugin has not acknowledged copying,
which is a peer that is not draining rather than a peer that is busy.

**256 again for the held-whole ops on one widget**, and it is the same number
for the same reason rather than a coincidence worth hiding: these ops can no
more be dropped than an acknowledgement can — nothing on an order-sensitive
widget may be superseded (§5.4) — so the figure the protocol already uses for
session-rate traffic a peer is not draining is the right one. It bounds **one
pending batch**, not a widget's lifetime, which is what makes it sufficient: a
drain empties it, and a plugin that queues 256 list changes between two 40 ms
drains is not one the host can help.

**16 / 8 / 16 of the 32-per-tick drain, one-shot first, and they deliberately
sum to more than the budget.** The total is the per-tick drain cap; dividing
it keeps a burst of coalescing pushes from consuming
the whole tick and starving an acknowledgement behind it. Each number is the cap
a queue may take in the *first* pass, so when only two queues are busy each
still reaches its published figure, and when all three are busy the budget
itself binds and §7's priority order decides who is cut: 16 one-shot, 8
requests, 8 coalescing. The second pass is uncapped and spends the remainder in
the same order, which is what keeps 3 one-shot messages leaving 29 for
coalescing — and 32 one-shot messages with nothing else pending draining 32
rather than 16.

**The request share is a floor, not a ration.** It exists so that two saturated
push queues cannot stop a blocked plugin thread from progressing at all; 8 per
40 ms tick drains a full `REQUEST_CAP` in four ticks, so it never binds on
traffic that is not already pathological.

**Arena headroom is the third term, and without it the budget cannot fund the
growth §5.6 specifies.** A plugin's arena is part of the *declared* total and
the second term counts host→plugin **deposits**, so without a third term a
panel or overlay plugin serving no large deposits would have an allowance of
exactly `declared` — and every re-grant it asked for would be refused
`too_large`, for the one profile whose arena fills at drag rate. Two rules
provide it, and they are separate because they bound different things:

- **An arena may grow to 4× its manifest's declared size, and no further.** The
  factor is not arbitrary: the sizes that outrun an arena are a widget's pixel
  dimensions (a 4K panel against a 1080p one is ~4× the pixels) and a
  triple-buffered live region (3×). 4× covers both with the item-count case
  partly funded; a plugin that outruns *that* has a manifest that is wrong, and
  telling it so is better service than handing it memory.
- **A superseding grant does not double-count during the handoff.** The pair —
  the retiring arena and its replacement — is allowed concurrently, because
  `shared_memory_protocol.md` §9.5's worst case *is* one segment at the
  high-water capacity beside the current one, and the old one leaves the ledger
  on `SegmentReleased`. Without this the safe install-before-retire ordering
  (§5.6) would itself be what breaks the budget.

Both are cheap to implement: the host already tracks a per-segment granted size,
so a re-grant checks the new size against 4× declared and the running total
against the ceiling. **An entry leaves the ledger on `SegmentReleased` OR on
`GrantFailed`** — the second matters because §5.6's re-grant is *safe to attempt*
precisely when it fails, and a failed grant was never installed, so it never
occupied anything. Without that row a plugin whose grants keep failing
accumulates ledger entries monotonically until it is refused `too_large`, which
is the refusal this term exists to prevent.

**4× is a policy ceiling with a named cost, not a bound that covers every
case.** It covers the panel-resolution driver exactly and the multi-buffering one
on its own; it does *not* cover their product — a 4K panel that is also
triple-buffered is 12× — and the third driver §5.6 names, an item count that
scales with the data, is unbounded by construction and only partly funded here.
That is deliberate: past this point the right answer is that a plugin's manifest
is wrong, and telling it so serves it better than handing it memory. Saying which
cases the number does *not* cover is the part that lets someone argue for a
different one.

**The shared-memory budget is a shape plus a default, and saying which is
which matters.** What counts toward it is a decision: **the manifest's declared
segment total, plus twice the largest single deposit served this session, plus
the arena headroom above.** The
second term is not padding — it is what `shared_memory_protocol.md` §9.5's
doubling actually needs, since
worst-case retention is one ring at the high-water capacity beside the current
one. The absolute ceiling is a **deployment** choice, not a protocol constant,
because it depends on the machine; **4 GiB** is the default. A grant beyond it is
refused with `too_large` (§8.12), which is the one failure class that means
*re-grant smaller* — and that is only truthful if the refusal was a size-policy
decision rather than a mapping failure, which this one is.

**10 s for the three acknowledgements, which are one deadline wearing three
hats.** Under §6's routing rule the work behind each is bounded and small — a
mapping plus a 28-byte checksum for a grant, one lifecycle call for an epoch or
a tool activation — so what the deadline has to clear is queueing on a busy
control plane, and that is the same quantity in all three cases. Giving them
three numbers would invite a question with no answer, which is why §7's request
queue takes `REQUEST_CAP`'s 32 rather than a number of its own. 10 s is what the
session already uses for *"this peer should have answered by now"*: the
command handler sits there. The launch does not: a plugin starting is not a peer that should have answered, it is a
program loading, and an SDK must not size its startup against any number here.

**30 s of no progress on one `segment_id` is the other two multiplied**, not an
independent choice: the host queries at 10 s and the retry budget is 3, so 30 s
is where the budget is **spent** — the third query goes out and there is nothing
left to escalate through. (Not "three queries have gone unanswered", which is
40 s; the deadline marks the end of the budget, not the last silence.) Deriving
it keeps the three consistent when one of them changes.

**3 retries, because each one costs a descriptor.** §9's closing rule already
says a recovery path that loops is worse than one that fails, and gives the
reason: every grant re-send puts another descriptor in flight against the host's
user-wide `RLIMIT_NOFILE`, so a host that retries hard degrades itself to rescue
one session. 3 is the smallest budget that separates a lost message from a dead
peer — one loss is ordinary, two is unlucky, three is not a coincidence.

**This is not the doorbell's `RETRY_BUDGET`, which is 8**
(`plugin_v2/doorbell/writer.rs`), and the two must not be reconciled. That one
counts re-offers of a refused *edge* on a socket, where a retry costs a syscall
and the peer is presumed alive; this one counts re-sends of an *arrangement*,
where a retry costs a descriptor and the question being asked is whether the
peer is alive at all. Same word, different quantity, and the layers are
independent.

**2 s of doorbell grace and 5 s of session shutdown grace are one budget split
in two.** They run in sequence (§10) and a user closing vizi waits for both and
then for the process to die: the host sends SIGTERM to the
plugin's **process group** — the whole tree `entry.command` started, launcher
and all (`manifest_schema.md` §2.2) — waits `TERM_GRACE_SECS` = `3.0`, then
SIGKILLs whatever of it survives (on Windows the plugin's job object is
terminated outright). So `2 + 5 + 3` = **10 s from the start of teardown to
SIGKILL**.

**That is deliberately far tighter than the launch**, which gets vizi's launch
budget — 60 s by default, one deadline from spawn to `Ready`. Teardown is
not the launch's mirror and should not be sized as one: a user waiting for a
plugin to *start* is waiting for something they asked for, while a user waiting
for one to *stop* is waiting to close an application they are finished with, and
the second is the worse wait. The two windows stand on their own reasoning
below.

The split inside that budget follows from what each window is for. The doorbell
window carries **one message** on a stream that is already open and already
draining on §7's 40 ms tick, so 2 s is some fifty drain intervals for a single
`DoorbellFinalReport` — generous by two orders of magnitude, with nothing it
could usefully wait longer for. The session window is where the plugin's
shutdown hook runs, and §3.3 already forbids that hook starting new work, so it
is bounded by flushing what exists rather than by computation: 5 s covers a
final report, a last `ReleaseDeposit` and a diagnostic with room to spare.

**These are derived, not measured.** §6's routing rule is what makes the
acknowledgements bounded at all — a peer permitted to run a 120 s export chunk
ahead of an ack admits no ack deadline. A load measurement against a stalling
peer can tighten any of these, and none has been taken.

**Of them, the grant acknowledgement is the one to get wrong last.** Mis-setting
the others degrades visibly — a re-send too early, an escalation too late —
while this one, set too long, simply never fires, and it is the sole backstop
for a grant lost through a doorbell send that reported success.

**A deadline that fires does not by itself end anything.** Its first action is
always idempotent — query, or re-send the same arrangement unchanged — and only
a *bounded number* of those escalates. A recovery path that loops is worse than
one that fails: each pass of a grant re-send puts another descriptor in flight
against the host's user-wide `RLIMIT_NOFILE`, which degrades the host to rescue
one session.

---

## 10. Faults and teardown

**Only the affected session ends. Nothing a plugin sends may crash or block the
host**, and nothing a plugin does may consume another session's share of any
host resource.

This channel is deliberately **not** fail-stop, which is the opposite posture
from `doorbell_protocol.md` §8, and the asymmetry is the point: the doorbell is a
small bespoke channel where a violation means someone got the contract wrong, so
exposing it and stopping is right. Here a malformed message is a decoder error
in a generated stub and the stream is already broken if it happens, so there is
no equivalent ladder to build.

What ends a session:

- **A stream error or end-of-stream.** Every blocked caller — the in-flight
  request and everything queued behind it — is failed with the real reason
  rather than a generic timeout.
- **A version mismatch at handshake** (§3.1).
- **A one-shot queue overflow** (§7).
- **An arrangement that never acknowledges, past its bounded re-sends** (§9).
- **A `Shutdown`, or the host dropping the session** (§3.3).

What does *not*:

- a `ContractViolation`, at any rate below its bound;
- a `DoorbellFault` — it ends the doorbell, and the session ends separately and
  afterwards, so that the report can be delivered and answered;
- a request that fails; `host_error` and `Error` are ordinary outcomes;
- an unknown field number, which is append-safety working.

**Order matters at teardown and is specified once:** close the doorbell to
traffic → hand the fault to the control plane → release the doorbell socket →
send `DoorbellShutdown` and open its grace window → send `Shutdown` and open
**its** window (§3.3) → **the plugin closes the stream**, or the host tears down
at the deadline → reap the process. A host that closes the stream first has
nothing left to report with, which is the whole reason the report is on this
channel — and that applies to the shutdown window as much as to the fault: the
host does not close this stream on a voluntary teardown, the plugin does, and
closing it at `Shutdown` would collapse the window into nothing.

The host therefore posts `Shutdown` and waits for the session to close, with
the window's deadline, before it reaps.

---

## 11. Validation and trust

**The host validates every plugin-supplied value before acting on it.** A
descriptor is a request to read host memory and a plugin is untrusted input, so:

- **`segment_id`** must name a segment granted to *this* session, and the
  segment's `purpose` must permit the use — a `set_image` naming the reader
  segment is rejected, not read.
- **`offset` and `len`** must lie wholly inside that segment; `len` must equal
  `shape_zyx × dtype` where a shape is claimed.
- **Ids in a plugin's own namespace** — `overlay_id`, `panel_id`, `widget_id`,
  `command_id`, `tool_id`, `contrib_id` — must name something that plugin
  declared, and can never name a user object or another plugin's.
- **Indices** — `t`, `c`, `z`, `level`, ROI bounds — are range-checked against
  the dataset, and a failure is `OUT_OF_BOUNDS`, never a clamp. Clamping would
  answer a question the plugin did not ask.
- **A grant acknowledgement's identity proof** is compared against what the host
  wrote into the preamble (§8.12). It is the only field in the protocol whose
  whole purpose is to be checked, and the failure it catches passes every other
  check there is.

**A plugin may take the host's values as well-formed**, because the host is the
supervisor: a host that lies to a plugin has already failed at something larger,
and a plugin defending against it cannot succeed anyway
(`plugin_principles.md` 9). The exceptions are the two places where a cheap
check catches a *vizi* bug rather than an attack: an
export chunk's `len` against `channels × prod(shape) × dtype`, and a granted
segment's preamble.

**A host bug that a plugin can see is reported, not tolerated silently.** An
epoch that was never arranged, a tool handle never activated, an arrangement
re-sent with different content under the same identity
(`arrangement_content_mismatch`), an arrangement whose identity went *backwards*
(`arrangement_regressed`): each is a
`ContractViolation` with a stable `where`, subject to §7's rate bound. Reporting
them is what makes an ordering defect findable at all; the alternative is a
plugin that quietly does nothing. **Stable means one site per rule**: two rules
sharing a site are indistinguishable to the aggregation §12 keys on `where`.

**An arrangement that REGRESSES is its own site**, `arrangement_regressed`, and
it is not the case above. An `EpochChange` older than the one installed, or a
`ToolBinding` whose revision moves backwards, is not one identity carrying
different content — the identity itself went the wrong way, and the receiver
refuses it rather than installing it. Neither neighbour fits either:
`interaction_mismatch` is an `E`-record replay whose bytes changed, and
`epoch_mismatch` is a record arriving under an epoch that was never installed.
It has a site of its own rather than a widened meaning because §12 aggregates per
`where`, and one counter for two defects is one counter nobody can act on.

**Forward compatibility is not a violation.** An unknown enum value, an unknown
field number, a reserved bit set — these are a newer peer naming something this
one predates, and reporting them would make every protocol addition look like a
defect at the far end. They are local diagnostics, aggregated
(`doorbell_E_input.md` §1's table states the doorbell's version of this rule;
this channel's is the same).

**An unknown message KIND is the same, with one obligation added.** A
kind the receiver predates decodes to an empty oneof, and only its `id` says
what it was: an `id` naming a request the receiver has in flight is a newer
peer's reply, and goes to that request's waiter; any other non-empty `id` is a
request whose sender is blocked on it, and **is answered
`Error{UNSUPPORTED}`** — silence there costs the sender its whole timeout; an
empty `id` is a push, and is a local diagnostic. Neither end faults the session
over one.

---

## 11a. What binds a plugin

Most of this document is host machinery. An SDK author needs to know which parts
are theirs.

**A plugin must:**

- **serve the `Session` stream and send `Ready` as its first message** —
  before or after reading `Hello`, and reading the stream from then on — with
  `Ready.id` = `Ready; I am <its version string>`, and fail the session on a
  version or token mismatch or a malformed `Hello.id` (§3.1);
- **dispatch by kind, and classify before matching an id** (§4's
  classify-by-kind rule) — getting it wrong fails silently;
- **never run work on the dispatch path** (§6);
- **answer every host request**, including with an error;
- **acknowledge every arrangement, after installing it locally** (§5.5's
  install-before-ack rule), and acknowledge a repeat again (its re-ack rule);
- **carry an empty `id` on every unsolicited push it sends** — `Ready` excepted,
  whose `id` is the identity string above — and never reject an incoming one
  for carrying one (§4's empty-id rule);
- **release what an abandoned reply names, without delivering it** (§4's resource
  rule). This is the plugin's obligation as often as the host's: a `ReadDataVolume`
  that times out leaves the reply arriving into no waiter, carrying a descriptor
  into a live ring reservation, and dropping it as an unmatched id costs the ring
  rather than the slot;
- **expose a shutdown hook and run it off the dispatch path**, on a dedicated
  worker rather than the command pool (§3.3), and **close the stream when it is
  done** — that close is what "finished" means and nothing else says it;
- **treat a descriptor it sends as a loan** (§5.6): do not reuse or overwrite the
  bytes until `ReleaseLoan` names them, allocate from **one cursor per arena —
  that is, per plugin-produced `LOANED` region, shared by every writer into it**
  rather than per sender, and when the arena is full **fail the push and count
  it** — never block, never reuse;
- **hold up its half of the memory lifecycle** — the two-halves grant, the
  identity proof read back out of the mapping, the per-segment state machine, and
  what retirement permits. That is `shared_memory_protocol.md` §9a in full, and
  §8.12 here is only the delivery half of it;
- **report `DoorbellFault` before closing its doorbell**, while this channel is
  still usable (§8.13);
- **report `ContractViolation` for a host bug it can see**, under a bound it
  enforces itself (§7);
- **keep an unconditional re-check schedule for every Pulsed region it consumes**,
  independent of `D` and of any other traffic
  (`doorbell_D_notification.md` §5) — this channel's dispatch loop is one
  acceptable mechanism for it, which is why the requirement is named here.

**A plugin must not build:** per-session fairness scheduling, the drain cap, any
of §9's host-side deadlines, the grant state machine's host half, or the
escalation ladder. Those exist because the host serves every session from shared
machinery; a plugin serves one session and has nothing to be fair between.

**A plugin sends no arrangement and answers no state query about the host.** The
arrangement direction is host→plugin, always: the host owns the namespaces the
fast path references (`control_plane_principles.md` F3).

---

## 12. Diagnostics

**Every failure this channel can see reaches a surface a developer can find**,
and a counter nobody can read is not one (`plugin_principles.md` 6).

The host retains, per plugin, a structured record that survives session cleanup
and is viewable and copyable from Plugin Manager: the stage the failure occurred
in, the return code or Unix signal, the transport error, and a 64 KiB redacted
tail of the plugin's stderr. **Expected host shutdown is not reported as a
crash.**

What a control-plane diagnostic must carry beyond that:

plugin id and manifest source; both peers' protocol versions; the session id;
the message kind and its `id`; the class it was routed as; the queue it was on
and that queue's depth at the time; the deadline that fired, if one did; and
what action was taken — dropped, refused, re-sent, escalated, torn down.

**Repeated failures aggregate rather than flood**, preserving the first
occurrence and the final threshold event — §7's rate bound is the mechanism, and
the reason is that the reporting path must not become the failure path.

**A replacement is not a drop, and a drop is always counted.** A coalescing
queue superseding a pending value for its key is the class working; there is
nothing to count and nothing was lost.

> **Unless the superseded message named a loan.** Then something *is* lost
> until the receiver returns it, and a supersession that skips the release is
> not the class working — it is the class stranding an arena. §5.6's disposition
> table makes the release mandatory at replacement time, and this section's
> "nothing was lost" is about the *value*, never about the bytes that carried
> it. A receiver that cannot release at replacement time must not coalesce.

What must be counted is every case where a value left without a successor: a
reply dropped after its resources were released (§4's resource rule), a push a
plugin could not send because its arena was full (§5.6), and — were it
reachable, which §5.4 argues it is not — a key evicted to make room for a
different key. The count must be reachable, because a session doing that
continuously is a session whose plugin is not being drained, and the only
difference between the two is the number.

---

## 13. Conformance

**The encoding needs no corpus.** The `.proto` is the contract and every
language's protobuf toolchain implements it from that file, which is why the
SDK's cross-language corpus covers the doorbell and explicitly not this channel.
Worth knowing all the same: the
stacks differ in independence — the host is `prost`/`tonic`, Python's `grpcio`
wraps the C core, while `grpc-go`, `grpc-java` and `@grpc/grpc-js` are
from-scratch — so a Go or JVM plugin cross-checks the wire in a way a Python or
Rust one cannot.

**What needs testing is behaviour**, and none of it is byte-exactness:

- a version mismatch fails the session at handshake, in both directions, with
  both versions named;
- a Windows token mismatch drops the session, at both ends;
- a push arriving while a request is pending is delivered, not dropped as a
  stray id (§4's classify-by-kind rule);
- a reply whose id matches no waiter is surfaced, not discarded (§4's unmatched-id rule);
- a coalescing queue holds one item per key: a burst on widget B does not evict
  the pending item for widget A, and `set_value` does not displace a pending
  `set_enabled` for the same widget;
- **a coalesced sequence leaves the widget where the unmerged one would**,
  checked by replaying both through the real builder rather than by asserting
  an operation order — including `options`, `value`, `options` and two
  `options` alone, the two cases an ordering rule gets wrong;
- a coalescing queue sized from the manifest cannot be driven out of keys by any
  legal traffic; a patch naming an undeclared widget is refused as validation,
  not absorbed as overflow;
- a one-shot queue at capacity faults;
- a plugin that reuses bytes a pending descriptor names is detectable: the host
  reads what the descriptor promised, not what replaced it;
- a message or repeated field over its bound is refused rather than **applied**,
  and reported as `repeated_field_overflow` (§11). Not *refused rather than
  decoded*, which a protobuf receiver cannot
  do: decoding is what produces the count, so the obligation is to decode, refuse
  to act, and report — the message's effect is what must not happen, not its
  parse;
- a `Shutdown` runs the plugin's hook off the dispatch path, permits its final
  messages, and completes on stream close — and overruns tear down anyway;
- a reply whose waiter has timed out has its deposit released by the receiving
  dispatcher, and the ring reclaims — including when the abandoned deposit is at
  the ring's head;
- a request stream held at capacity does not starve acknowledgements and
  releases;
- the first `DataVolume` into a new segment installs and is usable, with no
  wait for an acknowledgement of the message carrying the installation;
- `AbortExport` on a live handle replies `ExportDone`, so the in-flight slot
  clears without a timeout;
- every request failure path answers, including queue overflow and a missing
  permission;
- a `RequestStatusQuery` naming an in-flight request answers `PENDING`, one
  naming an answered or unheard-of id answers `UNKNOWN`, and the query is
  answered while the host has a picker up for the session that asked — the
  case it exists for (§8.9);
- a command handler that requests an asset is not marked suspect for a pick
  that outlasts the handler budget, and the budget still bounds the plugin's
  own latency once the picker closes;
- a re-sent arrangement is acknowledged again and closes nothing; the same
  identity with different content is reported;
- an `EpochChange` naming an unheld `time_mapping_revision` is not acknowledged
  and is reported;
- `tool` handles arriving out of order are rejected, and a gap is accepted;
- a grant acknowledged only after both halves arrive, in **both** arrival
  orders, and a failure reported without waiting for the missing half;
- a grant acknowledgement whose identity proof does not match what the host
  wrote fails the grant and allocates a new `segment_id`, rather than being
  accepted;
- a duplicate grant is answered with the same terminal answer, and a duplicate
  arriving while pending is answered with nothing;
- one terminal doorbell event produces exactly one `DoorbellFault`, however many
  views of it the transport offers;
- each `GrantStateQuery` answer produces the obliged host action, including
  `pending` producing **no** re-send of `M`;
- a `DoorbellFault` is delivered before the doorbell socket is released;
- teardown sends `Shutdown` and the plugin's cleanup runs;
- one session flooding pushes does not delay another session's drain.

**Two of these are host-only** — the fairness case and the queue-policy cases —
because a plugin serves one session. Everything else is a plugin's too.

---

**Chunked overlays (§5.7).** A host names the `(level, t)` volumes it wants
in `ChunksWanted`, the whole set per overlay, and sends it only when it
changes; it drops and counts a chunk it did not name, releasing its loan, and
refuses on `overlay_chunk_refused` one it named but cannot apply, or an
`OverlayUpdate` sent to a chunked overlay; it sizes the arena from the dataset
and re-grants it superseding the old on a dataset change; it never compiles a
chunked overlay. A plugin publishes a chunk only for a key in the host's
current set, on that level's lattice, u8 or u16; a chunk its arena cannot hold
it defers until a `ReleaseLoan` rather than failing or reporting a shortfall;
it sends the LUT, names, visibility and style in `OverlayLabelTable`, once per
overlay.

## 14. Rejected alternatives

| Alternative | Why not |
|---|---|
| Documenting behaviour in `.proto` comments | The IDL is copied verbatim into the published SDK; a comment there is a contract with no home for its reasoning, and behaviour is the half comments cannot express |
| A second stream for arrangement | The one-way class already bypasses the in-flight slot, so the head-of-line problem it would solve is solved; a second stream doubles the lifecycle and gives two orders where there was one |
| Making arrangement a request/reply | It would queue behind a long command, and the thing waiting is tool input for a whole pane |
| One queue with a priority flag | Priority is a plugin telling the host how to schedule; the class already says what overload must do, and a flag makes it a per-message choice (`plugin_principles.md` 3) |
| Drop-oldest on a FIFO as "coalescing" | It evicts the oldest *message*, not the oldest value for a key, so a burst on one key destroys another key's only pending update and nothing ever restates it |
| Dropping one-shot messages under load, with a counter | A dropped acknowledgement is an arrangement pending forever; the counter records a session that will never recover |
| Serving requests strictly ahead of pushes | A plugin that keeps the request queue full starves the releases and acknowledgements its own requests generate, and faults a healthy session |
| Inferring a dead plugin from a timeout | Timeouts mean unknown; the two liveness checks that exist stay green for a plugin whose doorbell thread has died |
| A doorbell copy of a control-plane message, for speed | Dual-path delivery reintroduces ordering and duplicate ambiguity, and a hint can only accelerate state already durable in shared memory (`doorbell_principles.md` A3, A4) |
| Sending voxels inline for small payloads | Exists for export chunks and tests only; making it general would put a size threshold in every implementation and a copy in the common path |
| A cumulative acknowledgement over arrangements | Deliberate partial acknowledgement is legal — a plugin may ack some panes and not others — so "everything up to N" would confirm what was not installed |
| Clamping an out-of-range index instead of refusing | Answers a question the plugin did not ask, and the plugin cannot tell the answer apart from a real one (`plugin_principles.md` 11) |
| A heartbeat | Proves the stream works, not that the peer's handlers do — and the dispatch loop is what dies.  §8.9's `RequestStatusQuery` is not one: it is solicited, names a single in-flight request, and is answered from the bookkeeping for that request |
| Separating *answered* from *never arrived* in `RequestStatus` | The plugin re-requests either way, and the distinction costs a retained id per answered request, forever |

---

## 15. Open

- **`purpose` as a closed enum**, or admitting plugin-defined values
  (`shared_memory_protocol.md` §11).
- **A resolvable namespace for host-owned targets** — annotations, ROIs and
  handles need stable session-scoped ids a plugin can hold across events
  (`doorbell_E_input.md` §9).
- **Whether double-click is a binding** rather than a per-record `click_count`.
  (Conflict resolution over (button × modifier) is settled in §8.10: nobody gets
  a contested binding and the user is told.)
- **Whether `Geometry` carries per-channel display state** (contrast, LUT) or
  whether that belongs to a separate scene feed.
- **`CommandDone.result`** has no consumer; it is plumbed at neither end.
- **A load measurement against a stalling peer**, which nobody has taken. §9's
  deadlines are derived, and derived is not measured,
  and each derivation names what it rests on so a measurement can overturn it.
- **A scene feed keyed on `view_id`, and the lit set with it** (§8.11). Nothing
  on either channel carries what is *lit* rather than clipped — the crop-active
  named regions with their frames, and the per-voxel mask crops. §8.11 sends a
  plugin that needs the lit set here, so it has to be recorded here: a tool that
  must reason about what the user can actually see cannot be written.
  Same home as the per-channel display-state question above, and the two should
  probably be answered together.
- **Host event pushes.** These are *coalescing* host→plugin pushes, and
  `ChunksWanted` (§5.7) shows the shape: no queue at either end, the host
  merging on its own side and the plugin keeping the latest. What remains open
  is the vocabulary — which host events, keyed how — not the mechanism.
- **A crop on a chunk want, a compile of a chunked overlay, a lattice coarser
  than the dataset's levels** (§5.7). Each is additive if ever needed; none is
  taken, and the last two are the "masks too big to fit" scheme by another
  name.
- **A host policy for answering a shortfall** (§5.6). The mechanism — retire
  and re-grant, never in-place growth — and the trigger, `SegmentShortfall`
  naming the amount, are specified, and §9's budget funds a re-grant: an arena
  may reach 4× its declared size and a superseding pair is not double-counted
  during the handoff. What no document says is *when a host should agree*,
  which is memory policy (`shared_memory_principles.md` B1) rather than
  protocol.
- **What the freeze covers**: this document's messages, the
  doorbell format and the shared-memory protocols are one version (§3.2), so
  freezing `v1` freezes all three or the version stops meaning what §3.2 says it
  means.

---

## Not established here

No value in §9 is a measurement: each is derived from a constant or from
arithmetic over the others, and §9 says which for each.
