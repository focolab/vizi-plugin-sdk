# Control-plane forward-compatibility cases

What a 1.0 receiver does when a later 1.x peer sends a message kind, a field or
an enum value this build does not know. The rules are
`docs/spec/control_plane_protocol.md` §11 (an unknown field or enum value is a
local diagnostic, never a violation; an unknown kind with an empty id is a
diagnostic, and with a non-empty id that names nothing in flight is answered
`Error{UNSUPPORTED}` echoing it; neither is a session fault), §3.1 (the
handshake matches on the
major, and a first message that is not the handshake message fails the
session) and §3.2 (within a major the wire is append-only). The id cases also
state the id rules: ids are never reused, so a second message under a used id
faults the session unless it is a byte-identical re-send, which is
deduplicated (a receiver remembers the last 65,536 ids it received); the host
mints `r<n>` and the plugin `q<n>`, so the two id spaces are disjoint by
prefix; and a pending request's id belongs to its reply alone, so a known
reply of a kind the request does not take, or a message of a kind the receiver
does not know, under the id of the receiver's own request in flight, faults the
session. A receiver keeps the ids of the requests it received apart from the
ids of the replies to its own requests, so that fault is never read as a
reused id, and a reply is never taken for a request.
Every rule a case cites is anchored in `docs/spec/` (§4 rules 1, 7 and 8,
§11, §3.1).

| file | what |
|---|---|
| `gen.py` | the generator |
| `record.py` | the recorder that supplies the baselines |
| `consume.py` | runs the host→plugin cases through the Python SDK and checks them |
| `known.json` | known mismatches, one line of reason each |
| `cases/manifest.jsonl.gz` | the cases, one JSON object per line |
| `cases/counts.json` | the summary: counts by injection and expected outcome, per family, whether the recording was made from a clean tree, the input digests and the spec anchors |
| `cases/recording.jsonl` | the recorded instances the cases are built from, and whether the SDK, the examples and the proto were clean when they were recorded |

## Regenerating

```bash
uv run python -I -B scripts/control_cases/gen.py              # from cases/recording.jsonl (under a second)
uv run python -I -B scripts/control_cases/gen.py --record     # re-record first (a few minutes)
uv run python -I -B scripts/control_cases/gen.py --check      # fail when cases/ is stale
uv run python -I -B scripts/control_cases/consume.py          # run and check the host→plugin cases
```

`gen.py` reads this working tree: the descriptor set from the compiled stubs in
`vizi_plugin_sdk/_proto/` (parsed, not imported), the kind→class partition from
`vizi_plugin_sdk/control.py` (`HOST_CLASSES`, `PLUGIN_CLASSES`,
`REQUEST_REPLIES`, `ALTERNATE_REPLIES`, `REPLY_KINDS`, and
`PLUGIN_REQUEST_REPLIES` where it has one, or else the request each recorded
host reply answered), and the rule anchors from `docs/spec/`.
Nothing per message is written by hand, so a new arm or field in the proto is a
new case after a regeneration. Regenerating from the committed recording is
byte-identical; `--check` compares the decompressed manifest, so it does not
depend on the local zlib.

`--record` runs `record.py --all`: the emulator self-check
(`examples/vizi_hostsim/selfcheck.py`) and every `examples/*/python/hostsim.py`,
unmodified, each in its own process. `PluginStub` is wrapped so every `HostMsg`
and `PluginMsg` on the Session stream is teed with its session, sequence,
direction, kind, id and bytes. The full recording is tens of megabytes, so
`gen.py` keeps only the instances it uses, plus the per-kind counts of the full
run, in `cases/recording.jsonl`. Re-recording can choose different instances
(timing moves sequence numbers), so a re-recorded fixture is reviewed like any
other change.

## Families and baselines

A family is one arm of `HostMsg.msg` (host→plugin) or `PluginMsg.msg`
(plugin→host): 101 at present. Each family's root is the smallest recorded
instance with a non-empty payload, which a live peer accepted when it was
recorded. A reply family carries, as `precondition`, the recorded request it
answers.

`HostMsg.widget_geometry` is a host push recorded from the emulator, which
sends it for each `image` widget when a panel opens and on a resize. To a
receiver whose IDL lacks `WidgetGeometry`, the message is an unknown kind with
an empty id, which it ignores, with only a local diagnostic. That is the
envelope's `unknown-oneof-arm` case, `diagnostic-only`.
`HostMsg.appearance` is the same: a coalescing host push the emulator sends
once as the plugin's `Ready` arrives and again on a theme switch, and to a
receiver whose IDL lacks `HostAppearance` an unknown kind with an empty id,
ignored with a local diagnostic (§11).
The table ops `PatchOp.set_table` and `PatchOp.patch_table` are reached under
`PluginMsg.panel_patch.ops[]` like every other arm, materialized from the
recorded root. The `example:new-arm` cases are `PatchOp.12` and `Value.7`.

`PluginMsg.release_request` is the one family no driver sends. Its baseline is
synthesized, mechanically: string fields named `id` or `*_id` get `gen-<name>`,
`protocol_major` is 1, and every real oneof gets its lowest-numbered arm. For
`ReleaseRequest` that leaves an empty message. No peer has accepted it.

For a node deeper than the recorded root reaches, the path is materialized the
same way, and a oneof displaced to reach it is named in `baseline_source`.
Only the recorded root is known to be valid. Materialized nodes are
structurally valid, and nothing more.

## Injections

| injection | what |
|---|---|
| `unknown-field:{varint,length-delimited,fixed32,fixed64}` | field max+1 appended to the node |
| `unknown-enum:{max+1,1000}` | each enum field at the node set to the value |
| `unknown-oneof-arm` | each real oneof at the node: its arm cleared, and field max+1 carrying `08 01` in its place. At the envelope root this is an unknown kind |
| `id-collision:peer` | a new kind from the other side carrying the id of a request in flight (`r1` or `q1`): the receiver's own request, so the session faults |
| `id-collision:same-direction` | the same, from the request's own side: the sender reuses its own id |
| `id-reuse:{identical-resend,different-content}` | a request (or `GrantStateQuery`), or a reply with its request in flight, sent twice under one id: byte for byte, or with field max+1 on the second envelope |
| `reply-kind:wrong` | a request in flight under its requester's id form, and a known reply of each kind it does not take (`Error` aside) under that id; a reply whose baseline names a `SegmentRef` is left out |
| `protocol-minor:{1,99}` | `Hello` and `Ready` at a later minor |
| `paired:plan-and-arm:*` | a `PlanStage` and the `StageArm` that follows it, with field max+1 on both, on both inputs, or on one input only |
| `paired:restatement:*` | an arrangement sent twice, with field max+1 on both statements or on the restatement only |
| `example:*` | one of each in a recognizable form: a new kind (push and with an id), a new field on `InvokeCommand`, new arms in `Value` and `PatchOp`, a new widget kind in a manifest |

"max" is the highest field or reserved number in the message. The bytes are
spliced by hand, so `baseline_b64` and `envelope_b64` differ only by the
injection. On every generation, each unknown-field and unknown-arm case is
checked to decode, after `DiscardUnknownFields`, to its baseline, and each
enum case to read back its value. There is no unknown-field case inside a map
entry: an entry carries only its key and value and can never gain a field, so
no peer can send one.

## Expected outcomes

`expected` is a code, and `rule` cites the sentence of the spec it comes from.

| expected | the receiver |
|---|---|
| `ignored` | behaves exactly as for the baseline, with at most a local diagnostic |
| `not-a-violation` | reports no `ContractViolation` the baseline did not, and does not fault the session. Nothing else is stated |
| `item-refused` | refuses that one item and nothing else: a request is answered `Error{UNSUPPORTED}` echoing its id, an arrangement is not acknowledged; a local diagnostic names the value, and there is no `ContractViolation` and no session fault |
| `unsupported` | answers `Error{UNSUPPORTED}` echoing the id, and delivers nothing to a waiter |
| `diagnostic-only` | sends nothing |
| `session-continues` | passes the handshake and proceeds |
| `handshake-refused` | refuses at the handshake with the sentence the `rule` quotes |
| `session-faulted` | ends the session, naming the reused id, or the id of the request a wrong reply kind or an unknown kind came under, where there is one; neither reaches the waiter, which gets the fault |
| `deduplicated` | drops the second, byte-identical copy without dispatching it again |
| `manifest-refused` | refuses the manifest at parse |
| `UNSTATED` | the spec does not decide; the outcome is reported and never judged |

No case is `UNSTATED` at present; the code is for a rule the spec does not
decide.

An arrangement's content and a plan's identity are the fields the receiver
knows. So a restatement whose only difference is an unknown field is the same
content, acknowledged again, and a `StageArm` whose input or params differ from
its `PlanStage` only by unknown fields matches it: both are `ignored`. An
unknown oneof arm under `StageArm.input` or `StageArm.params` is a kind the
receiver does not know, and is `item-refused` exactly as an unknown `DType`
there: the arm is not acknowledged, a
local diagnostic names the kind, and the plan stays planned, so the host may
re-arm with a kind the plugin knows. A `StageArm` case whose node had to be
created (a params entry the recorded arm lacks) carries a `PlanStage` stating
the same content in its `setup`, so its baseline is acknowledged.

An unknown enum value is `not-a-violation`, except where it refuses its item:
a `PlanStage` or `StageArm` naming a `DType` this build does not know, and a
`ToolBinding` whose `ToolMode` it does not know, are `item-refused`. The
`ToolBindingAck` of protocol 1.0 carries no reason, so the binding is refused
by not acknowledging it and the reason is logged locally.

The handshake at a later minor is stated both ways. A `Hello` at a later minor
continues: a newer host serves an older plugin, which answers with its `Ready`
clamped to the lower of the two minors and runs at that minor. A `Ready` above
the host's `Hello` minor (the recorded `Hello` is `1.0`) is refused at the
handshake with `this plugin speaks protocol 1.<ready>; this vizi speaks
1.<hello>` (§3.1, "`Ready.protocol_minor` MUST NOT exceed the host's `Hello` minor"). Those
two cases have the host as receiver, so `consume.py` does not run them; the
emulator implements the check (`vizi_hostsim.ready_refusal`).

An unknown arm in place of `Hello` or `Ready` is stated: the unknown-kind rule
applies only after the handshake, so it fails the session.

## The consumer

`consume.py` runs every host→plugin case through
`vizi_plugin_sdk.control.ControlPlane.receive`, in a fresh receiver, with a
violation reporter and an empty segment store, and runs the baseline the same
way. A `Hello` case's message is the session's first, so it goes through
the servicer's own dispatch with the session awaiting `Hello` (`_route_one`):
the handshake check, the `Ready` clamped to the lower minor, and the rule that
a first message of any other kind, an unknown one included, fails the session.
A `PlanStage` goes through the servicer's planner. Those rules live there.
The plugin→host cases (receiver: host) are not run here.

It checks what the control plane does with a message: its routing, the
violations, the bytes sent, what reached a waiter, and whether anything
raised. A host request whose handling is the plugin author's code is checked
up to its dispatch, and no further.

A mismatch listed in `known.json` is reported as known; an entry that matches
no mismatch fails the run. Anything else that mismatches fails it.

## What lives elsewhere: a reply's content

These cases are injections on recorded wire forms, judged by what the receiver's
control plane does with them; a host request whose answer is the plugin
author's code is checked up to its dispatch. So "an argument-validation
failure's `CommandDone` carries `command_id`" (§8.3) is not a case here: it is
a property of the plugin's answer, not of the receiver's routing, and the
sentence is not yet in `docs/spec/` to anchor.
It is checked where a reply's content can be:

- the emulator self-check (`examples/vizi_hostsim/selfcheck.py`): the host side
  in `check_id_rules` (a `CommandDone` without the invoked id, or naming
  another, faults the session and reaches the waiter as `Error{PROTOCOL}`), and
  the Python SDK's answers to a missing and an ill-typed argument in
  `test_command_id_on_refused_arguments`; every example driver run under the
  emulator is held to it too;
- each port's own test: Go `control/command_id_test.go` and
  `plugin/command_id_test.go`, Rust `tests/session_cases/commands.rs`, Java
  `ControlTest.everyCommandDoneEchoesTheInvokedCommandId`.

The same holds for the command outcomes, which are the same in every SDK (an
undeclared command `Error{NOT_FOUND}`, a declared one with no handler
`CommandDone{ok=false}` naming it, a cancellation `Error{CANCELLED}`): the
emulator self-check's `test_command_outcomes`, where a case the Python SDK
does not give yet is listed in its `PENDING_VIZI` as this file's
`known.json` marks a mismatch, and Go `plugin/command_outcomes_test.go`, Rust
`tests/session_cases/commands.rs`, Java `ControlTest`.
