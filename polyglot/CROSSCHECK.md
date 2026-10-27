# Cross-checking the doorbell implementations

The Python, Go, Rust and Java SDKs each implement the plugin side of the
doorbell from the specification. They are checked against each other, with no
one of them as the reference, in three layers.

## The three layers

**The corpus cross-check** (`scripts/gen_v2_crosscheck.py`, and a replayer in
each SDK) fixes the received bytes and compares what each implementation
decided about them. It is the only layer that reaches the cases a well-behaved
host never produces, such as a bad CRC, a reserved opcode or a non-zero fixed
pad, because those have to be injected.

**The comparator** (`scripts/compare_v2_verdicts.py`) merges any number of
columns and compares every pair. It grades no implementation against another:
it reports, and returns non-zero only for a difference the specification
settles. Which differences those are is data, the `unsettled` list in
[`unsettled.json`](unsettled.json), not judgement. The list is empty, so the
comparison is strict everywhere.

**The live socket** (`scripts/head_to_head.py`, and a peer per SDK) opens one
`AF_UNIX SOCK_SEQPACKET` socketpair, hands one end to a peer built from one SDK
and the other to a peer built from another, and has the first send `D` hints
its own encoder produced while the second decodes them with its own receiver.
Nothing is stored and nothing is expected: the comparison is between two
implementations. It runs every ordered pair, the same-SDK pairs included as the
control showing the harness itself agrees.

The peers are `scripts/doorbell_peer.py`, `polyglot/go-sdk/cmd/doorbell-peer/`
and `polyglot/rust-sdk/examples/doorbell_peer.rs`; Java has no peer. Each
adopts the descriptor in `VIZI_DOORBELL_FD` and prints one canonical line per
hint.

## Running it

```bash
# 1. The scenarios, plus Python's column. --scenarios-only writes the fixture
#    bytes with no verdicts at all, for when another SDK grades them first.
uv run scripts/gen_v2_crosscheck.py /tmp/corpus.json

# 2. Every other SDK replays the same records and writes its own column.
(cd polyglot/go-sdk    && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/go.json    go test -run TestCrossCheck ./...)
(cd polyglot/rust-sdk  && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/rust.json  cargo test --test crosscheck -- --ignored)
(cd polyglot/java-sdk  && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/java.json  ./mill --no-daemon sdk.test.testOnly vizi.plugin.javasdk.doorbell.CrossCheckTest)

# 3. Compare every pair, and write the merged corpus.
uv run scripts/compare_v2_verdicts.py /tmp/corpus.json /tmp/{go,rust,java}.json --out /tmp/cross_sdk.json

# 4. Hand that back to each SDK, and its own test grades it against all the
#    others.
VIZI_V2_CORPUS=/tmp/cross_sdk.json ...

# 5. Two SDKs on opposite ends of one socket, for every ordered pair.
uv run scripts/head_to_head.py
```

Go and Java skip the replay when `VIZI_V2_CORPUS` is unset. Rust marks it
ignored in ordinary test runs; `--ignored` opts in, and a missing corpus then
fails. The `crosscheck.yml` workflow supplies the corpus and opts in.

## What is compared

The harness compares the frame disposition, whether a violation rides the grant
acknowledgement or `ContractViolation`, the `where` names `doorbell_E_input.md`
§2 fixes, the cancellations, and the failure classes `doorbell_protocol.md` §12
names. It does not compare diagnostic spellings or cancellation reason strings,
which are each implementation's own. Two labels are folded because the
specification names the concept without fixing a spelling: `unknown_op` and
`unknown_opcode`, and the §12 failure classes (`BadVer`, `BadVersion`,
`bad_ver`).

## What it covers

The cross-check grades the plugin-side doorbell receivers on records written by
the Python encoder. It says nothing about the control plane (see the next
section for the part of it that is covered), the memory registry, readers or
exporters, and it does not grade a host. The live socket exercises `D` only,
because it is the only record a plugin sends.

## Control-plane forward compatibility

`scripts/control_cases/` holds a second corpus: what a 1.0 receiver does when
a later 1.x peer sends a message kind, field or enum value it does not know
(`control_plane_protocol.md` §11, §3.1, §3.2). Its expected outcomes come
from the specification, not from any implementation's output: each case
carries an `expected` code and the `rule` sentence it comes from, and where
the specification does not decide, the code is `UNSTATED` and the outcome is
reported but never judged. The generator, the injections, the codes and the
`UNSTATED` list are documented in
[`scripts/control_cases/README.md`](../scripts/control_cases/README.md).

```bash
uv run python -I -B scripts/control_cases/gen.py            # regenerate cases/ from the committed recording
uv run python -I -B scripts/control_cases/gen.py --record   # re-record the baselines from the emulator first
uv run python -I -B scripts/control_cases/consume.py        # the Python SDK, checked; CI runs this
```

The cases are generated from this tree's descriptor set and the class
partition in `vizi_plugin_sdk/control.py`, over baselines recorded on the real
transport while the emulator's self-check and the example drivers ran. The
fixture is `cases/manifest.jsonl.gz`, one JSON object per line.

**A replayer in another SDK** reads only the lines whose `receiver` is
`plugin` (the `host` lines are for a host harness), and from each:

- `case_id`, `family`, `path` and `injection`, to name the case;
- `setup`, when present: what the receiver must hold first. `stage_ring` is a
  granted `S` segment with that id, `declare_stage` a stage with those param
  keys and `Value` kinds whose plan needs nothing, `capability` the
  doorbell's half of a grant, and `prior` a list of messages to receive first;
- `precondition` and `request_context`, when present: a request whose
  `direction` is `plugin->host` is this plugin's own, outstanding, and an
  unknown kind under its id is never delivered to its waiter, nor is a known
  reply of a kind it does not take (`reply-kind:wrong`); one whose
  direction is `host->plugin` is received first;
- `envelope_b64` and `baseline_b64`: one serialized `HostMsg` each, received
  by two fresh receivers. Or, for `paired:*`, `sequence` and
  `baseline_sequence`: lists of `{direction, kind, b64}` received in order;
  for `id-reuse:*`, `sequence` alone, two messages under one id, and
  `fault_id`, the id a `session-faulted` outcome names (on `reply-kind:wrong`
  too);
- `expected`, which says what to compare. `ignored` compares everything the
  receiver did with the baseline's; `not-a-violation` compares only the
  violations and faults; the rest are absolute.

A replayer reports a mismatch against the specification, not against the
Python SDK. The Python consumer's known mismatches are in
`scripts/control_cases/known.json`. The Go, Rust and Java replayers are not
written yet.

## Adding an implementation

Build and judge from the specification alone. Do not resolve a question by
reading another implementation: that is how one becomes the reference, and how
an ambiguity in the documents stops being findable. A question the documents do
not answer is a finding against the documents, and is recorded in
`unsettled.json` until they do.
