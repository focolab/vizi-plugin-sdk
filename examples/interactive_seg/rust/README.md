# Interactive segmentation in Rust

An implementation of [SPEC.md](../SPEC.md) on the Rust SDK
(`polyglot/rust-sdk`): a lasso tool whose marker trail rides the overlay's
Pulsed lane, a segmentation on a worker, a label volume, a commit into native
masks, and a mean-versus-t plot in the panel.

Measurement takes the first visible channel from a fresh view state, falling
back to channel 0 when none are visible. That channel is used for every
timepoint in the measurement, independently of the channel used to draw the
region. The deprecated `ViewState.c` is not used.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.interactive_seg`; the other implementations of this example use
the same ID, so install only one at a time.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.interactive_seg` beneath it:

```text
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --manifest "manifest-output/interactive_seg"
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/interactive_seg/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/interactive-seg --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\interactive-seg.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\interactive-seg.exe --install
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

On Linux, with the repository's Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/interactive_seg/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/interactive_seg/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.interactive_seg"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options. Installation tests must use a scratch
`--install=DIR` or `VIZI_PLUGINS_DIR`.

## Build and run against the emulator

From the repository root:

```sh
cargo test --locked --manifest-path examples/interactive_seg/rust/Cargo.toml
cargo clippy --locked --all-targets --manifest-path examples/interactive_seg/rust/Cargo.toml -- -D warnings
cargo fmt --check --manifest-path examples/interactive_seg/rust/Cargo.toml
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/interactive_seg/python/hostsim.py --plugin-dir examples/interactive_seg/rust
```

The Rust tests cover declaration/snapshot parity, polygon parity including self-intersection
and boundary points, outside-volume selection, margin rounding, threshold
non-finite handling, connectivity, largest-component/raster ties, anchor and
single-plane behavior, clipping, tight label accumulation/overlap/cap, LUT,
preview decimation, gesture context after a miss/unusable press, manifest
validation, and PNG chunk structure/CRCs.

The simulator requires the repository Python environment with grpcio, numpy and
atomicshm, plus permission to create local Unix sockets.
`PYTHONDONTWRITEBYTECODE` prevents generated caches outside this directory.

Label volumes leave `label_visible` empty, preserving visibility from the LUT's alpha values.

The shared simulator verifies the 361-voxel ellipsoid, Add/Replace,
projection/level/channel refusals, miss press recovery, repeated commit with
stable region IDs, measurement means `[1000.0, 1040.0, 1080.0]`, panel restoration,
busy handling, activation status, Reset and completed shutdown.

Additional checks use only the shared driver/emulator:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/interactive_seg/rust/verify_extra.py
```

They cover a 2,048-record live preview with 512-marker decimation, a slow
consumer, stale-pane rejection, every LUT row and transform defaults, Reset
during Add/measurement, preservation of committed regions, empty loan ledgers,
and independent PNG decompression/CRC validation. The image is saved to
`target/measurement.png` for inspection.

The long preview sends at most 64 input records before waiting for their final
published revision and checking its endpoint and decimated shape. The driver
scans shared memory independently of disposable doorbell hints. The slow-consumer
check waits on READY publication words without consuming them, then consumes the
newest value once; it does not require one hint per publication. Elapsed time is
reported as an observation, not a throughput requirement.

The Reset checks park volume reads with the emulator's `hold_reads()` and wait
for `await_reads()` before invoking Reset. During measurement, they also wait
for a new panel update disabling Commit: the command reply can arrive before
that patch. Reads remain held through these checks, so they do not depend on
beating a fixed read delay under load.
