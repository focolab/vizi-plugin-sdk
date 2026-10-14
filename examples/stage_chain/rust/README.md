# Stage chain — Rust

The four stages of [SPEC.md](../SPEC.md), implemented on
`polyglot/rust-sdk` (`vizi-doorbell`). The plugin owns the plans, voxel arithmetic
and drift history. The SDK owns the control session, grants, arms, shared
mapping, header codec, turn polling, publication and doorbell hints.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.stage_chain`; the other implementations of this example use
the same ID, so install one at a time.

If a rebuild changes stage output, bump the plugin version in Cargo.toml and
refresh the development manifest before reinstalling. Vizi reuses saved stage
output when plugin ID, version, stage and parameters match; rebuilding alone
does not invalidate it. Settings outside the declared parameters are not part
of that comparison, so output changes caused by those settings also require a
version change.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.stage_chain` beneath it:

```text
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --manifest "manifest-output/stage_chain"
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --help
```

`--manifest DIR` and `--manifest=DIR` write directly into DIR, without adding the
plugin ID. Omitting DIR writes into the current directory and replaces its
manifest. `--force` applies only to `--install`. Management commands need no Vizi
launch environment and start no processing workers.

For development, `--install --copy=false` keeps the declared launch path in
place. Prefer it when the next rebuild should be used without reinstalling;
keep that path available, since cleaning or moving the checkout breaks it.
The default copy is preferable for normal use and updating while Vizi is running.
`--manifest` only writes a manifest and resources; it never copies the binary.

Alternatively, Cargo can put a build in a chosen location before you install it.
This optional build command is the same in all three shells:

```text
cargo install --locked --path examples/stage_chain/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/stage-chain --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\stage-chain.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\stage-chain.exe --install
```

These executable invocations accept all the same management options. Cargo's
`install --force` can replace an existing binary on rebuild; it is separate from
the plugin's `--install --force` option. The latter copies the executable by default.
For a custom runner or argv, change `declaration::builder()` to call
`PluginBuilder::launch_command(...)` and install with `--copy=false`. That API
records the chosen command; it does not copy or validate the runner. Use a runner only when
it preserves the host's inherited connection handles and launches the plugin
reliably on the target platform. There is no CLI launch-command override.

### Development manifest and installation check

[src/declaration.rs](src/declaration.rs) is the declaration source;
[vizi-plugin.json](vizi-plugin.json) is its checked-in development snapshot,
including explicit SDK defaults. A test compares them outside `entry`.
The snapshot retains its relative launch command for the development drivers.
The serving executable uses its compiled declaration, so it does not load a
manifest from the working directory.

The existing POSIX `launch.sh` is only a development launcher. It builds and execs the debug binary; self-installed plugins use the release executable directly.

On Linux, with the repository's Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/stage_chain/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/stage_chain/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.stage_chain"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options. Installation tests must use a scratch
`--install=DIR` or `VIZI_PLUGINS_DIR`.

## Offline checks

```sh
cargo test --locked --manifest-path examples/stage_chain/rust/Cargo.toml
cargo clippy --locked --manifest-path examples/stage_chain/rust/Cargo.toml --all-targets -- -D warnings
cargo fmt --manifest-path examples/stage_chain/rust/Cargo.toml -- --check
cargo +1.88.0 test --locked --manifest-path examples/stage_chain/rust/Cargo.toml
```

The tests cover declaration/snapshot parity, both integer tie
directions, both clamps, floating-point gain and differences, equal-channel
differences, zero padding, crop windows, f64-to-f32 rounding, extent/origin
placement, exact centroid sums, empty centroids, drift gaps and independent
arms, composition with a nonidentity pose, and manifest validation.

## Run against the emulator

The repository's existing Python `.venv` supplies the emulator and its
dependencies. From the repository root:

```sh
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust --only planning
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/python --other-dir examples/stage_chain/rust
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust --other-dir examples/stage_chain/python
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust --other-dir examples/stage_chain/go
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go --other-dir examples/stage_chain/rust
```

`--plugin-dir` launches session A and `--other-dir` session B (default: the
same directory, as a second process); stages alternate between the two within
a chain. The lines above chain this implementation to itself, to the Python
one and to the Go one, in both orders. `--only NAME` runs one scenario. A run
that passes ends `all passed`.

The driver runs the eleven scenarios of [SPEC.md](../SPEC.md) against the
emulator's host (`examples/vizi_hostsim/chain.py`).
