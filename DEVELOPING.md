# Local development environment

Repo-local tooling for working on the SDK and for developing plugins against
it. This file is **not** part of the published developer guide — that lives in
[`docs/`](docs/README.md) and is what plugin authors read.

## Baseline environment

```bash
uv sync                       # creates .venv from pyproject.toml + uv.lock
uv run scripts/check-env.py   # verify it can speak the protocol
```

`uv sync` installs `vizi_plugin_sdk` **editable**, so an edit to the package
takes effect without reinstalling.

Python is pinned to **3.11.15** in `.python-version`. 3.11 rather than 3.12/3.13
because it is the interpreter every lock target in
[`docs/environments.md`](docs/environments.md) claims, it is the widest-compatible
choice for scientific stacks (e.g. TensorFlow 2.15 plugins select only 3.11), and
the pinned stub generator has no 3.12+ wheels (see below). The SDK itself
supports 3.11–3.13; change `.python-version` and re-run `uv sync` to check
another minor.

`uv.lock` is committed. It locks only this dev baseline — a *shipping* plugin
needs its own hashed, per-platform/profile locks per `docs/environments.md`,
which `uv.lock` is not a substitute for.

## Protocol codegen

`proto/vizi_plugin.proto` (which imports `proto/roi.proto`) is the contract.
Both scripts run the generator in a throwaway env, never in `.venv`:

```bash
scripts/regen-stubs.sh --check   # committed Python stubs match proto/?
scripts/regen-stubs.sh           # rewrite them
scripts/protoc.sh --cpp_out=OUT proto/vizi_plugin.proto proto/roi.proto
```

The generator is pinned to `grpcio-tools==1.60.1`, matching CI. Two consequences worth knowing: it caps
protobuf at `<5` and ships no Python 3.12+ wheels, which is why it is kept out
of `.venv` — the baseline stays on the current protobuf 6.x / grpcio 1.8x
runtime, and codegen borrows a 3.11 interpreter of its own.

`vizi_plugin_sdk/` (including `_proto/`), `tests/`, `proto/` and `docs/spec/`
are published into this repository from vizi's own source tree, and the next
publish replaces them. A change to the SDK, its tests, the IDL or a
specification document is best raised as an issue here.

So `scripts/regen-stubs.sh --check` is the routine use: it verifies that the
committed stubs match the committed IDL. The stubs arrive with the proto they
were generated from.

## Polyglot plugins

A plugin is a process, not a Python import — the SDK is a convenience layer,
and any language that can speak gRPC over the control plane, mmap the data
plane, and poke the doorbell socket is a valid plugin host. For a non-Python
plugin, `proto/` is the whole interface; `scripts/protoc.sh` bundles a full
protoc, so message codegen works for every built-in language backend. Only the
*Python* gRPC service plugin ships with it, so a non-Python service stub needs
that language's grpc plugin binary (or use `buf`/your own protoc against
`proto/`). Below the SDK, [`docs/concepts.md`](docs/concepts.md) describes the
three channels.

## Developing a plugin against this checkout

Plugins are discovered in `~/.vizi/plugins/`, or wherever `$VIZI_PLUGINS_DIR`
points — set that to a scratch directory to keep dev plugins out of your real
plugin dir.

Each plugin gets its own environment, because the manifest's `entry.command`
names the interpreter vizi launches. Give it one that resolves the SDK from
this checkout, so SDK edits are picked up live:

```bash
cd "$VIZI_PLUGINS_DIR/my_plugin"
uv venv --python 3.11
uv pip install --editable /path/to/vizi-plugin-sdk
/path/to/vizi-plugin-sdk/scripts/check-env.py   # run with THIS venv's python
```

with `"entry": {"command": [".venv/bin/python", "my_plugin.py"]}`. The command
runs with the plugin directory as its working directory, so relative paths
resolve there.

For a throwaway experiment, pointing `entry.command` at this repo's
`.venv/bin/python` by absolute path also works — but a plugin that is going to
ship needs its own pinned environment, not a shared dev one.

## Tests

```bash
uv run python -m unittest discover -s tests   # all of them
uv run python -m unittest tests.test_doorbell -v
uv run --with pytest pytest tests -q          # the same suite; pytest is not a dev dep
```

`tests/_layout.py` resolves the repository paths the suite reads (the IDL, the
package and the specification documents).

`tests/test_doorbell.py` drives real socketpairs; `test_memory.py` maps real
objects; `test_control.py` and `test_server.py` need neither a socket nor a
host. `test_server.py` is the **supervisor's** suite — the layer that orders the
two channels against each other, which is the one no single-layer test reaches.

The only live end-to-end checks that run from this tree are the host
emulator's runs, and they are the ones to run after any publish that
touches `server.py`:

```bash
cd examples/peak_roi/python
.venv/bin/python selftest.py    # the pure computation
.venv/bin/python hostsim.py     # a real plugin process over real sockets: reads, panel, ROIs
cd ../../voxel_probe/python
.venv/bin/python hostsim.py     # the fast path: a tool, two overlays, the live lane, a panel image
cd ../../vizi_hostsim
fixture/.venv/bin/python selfcheck.py   # the emulator's own check: reader (and the §1.2 replacement handshake), exporter, compile, masks, ROIs, assets, a chunked overlay
cd ../format_bridge/python
.venv/bin/python selftest.py    # the crate format against the fixtures, the comparator, the writer
.venv/bin/python hostsim.py     # a reader and an exporter over G and X, the §1.2 replacement handshake, both calibration paths, two sessions
cd ../../interactive_seg/python
.venv/bin/python selftest.py    # the lasso, the threshold, the components, the anchor, the label map, the plot
.venv/bin/python hostsim.py     # a lasso on the lane, a label volume as a loan, the commit into masks, the measure
cd ../../ray_probe/python
.venv/bin/python selftest.py    # the level maps, the traversal, the crop clip, the tie rules, the block, the ellipsoid
.venv/bin/python hostsim.py     # rays through a three-level pyramid, the aim on the lane, actions inside a gesture, two panels, ROIs
cd ../../movie_seg/python
.venv/bin/python selftest.py    # the band rule at its edges, the tight box, the footprint's union, the tables
.venv/bin/python hostsim.py     # a chunked overlay: host-sized arenas, the want set served in order, eviction, a static overlay, the dataset change, the deferred publish, two sets back to back
```

The emulator is `examples/vizi_hostsim/` (its README says what it does and
where each rule comes from); each `hostsim.py` is that exercise's dataset and
scenarios on top of it, and `--plugin-dir` points either at another
implementation of the same specification. Without a per-example `.venv`, set
`VIZI_HOSTSIM_PYTHON` to an interpreter that carries the SDK (the repo's
`.venv/bin/python` after `uv sync` plus `uv pip install atomicshm`) and run
each `hostsim.py` with the same interpreter -- which is what
`.github/workflows/python.yml` does on Linux and macOS.

**CI (`python.yml`)** runs the suite on Linux, macOS and Windows under Python
3.11 and 3.13, the emulator checks on Linux and macOS, and stub freshness.

`hostsim.py` plays host: a Unix-domain gRPC socket, an inherited
`SOCK_SEQPACKET` doorbell, an `SCM_RIGHTS` grant, and both orders of the
two-halves memory grant. Its doorbell encoder is deliberately its own — written
from `docs/spec/doorbell_protocol.md` and checked against the
cross-check corpus rather than against `vizi_plugin_sdk.doorbell`, because a
check that reuses the thing under test proves self-consistency and nothing else.

## The Go, Rust and Java SDKs

Each SDK under `polyglot/` builds and tests with its own toolchain; its README
has the details and `.github/workflows/polyglot-*.yml` is what CI runs.

```bash
cd polyglot/go-sdk   && go vet ./... && go test -count=1 ./...
cd polyglot/rust-sdk && cargo fmt --check && cargo test --locked --all-targets
cd polyglot/java-sdk && ./mill --no-daemon sdk.test
```

The Go protobuf code is regenerated with `polyglot/go-sdk/regenerate-proto.sh`
(`--check` verifies it), and the Rust SDK keeps a copy of the IDL in
`polyglot/rust-sdk/proto/` that a test compares with `proto/`.
[`polyglot/CROSSCHECK.md`](polyglot/CROSSCHECK.md) describes the cross-check
between the four doorbell implementations.
