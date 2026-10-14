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
cd polyglot/go-sdk    && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/go.json    go test -run TestCrossCheck ./...
cd polyglot/rust-sdk  && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/rust.json  cargo test --test crosscheck -- --ignored
cd polyglot/java-sdk  && VIZI_V2_CORPUS=/tmp/corpus.json VIZI_V2_VERDICTS=/tmp/java.json  ./mill --no-daemon sdk.test.testOnly vizi.plugin.javasdk.doorbell.CrossCheckTest

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
the Python encoder. It says nothing about the control plane, the memory
registry, readers or exporters, and it does not grade a host. The live socket
exercises `D` only, because it is the only record a plugin sends.

## Adding an implementation

Build and judge from the specification alone. Do not resolve a question by
reading another implementation: that is how one becomes the reference, and how
an ambiguity in the documents stops being findable. A question the documents do
not answer is a finding against the documents, and is recorded in
`unsettled.json` until they do.
