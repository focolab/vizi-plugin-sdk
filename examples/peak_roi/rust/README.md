# Peak ROI on the Rust SDK

An implementation of [SPEC.md](../SPEC.md), using the path dependency
`../../../polyglot/rust-sdk`.
The SDK supplies the registry, runtime, protobuf types, doorbell, memory grants,
request correlation, deposit release, and shutdown protocol.

The panel searches the first visible channel (or channel 0 when none are
visible) in uncropped level-0 data.
Extents are Z/Y/X voxel semi-axes; defaults are `(4, 8, 8)`. Analysis uses a
plugin-owned worker and reports progress in the panel. Re-running replaces the
`peak` ROI. All-timepoints mode writes a dense translation pose, keeping the
channel and extents captured at run start.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.peak_roi`; the other implementations of this example use
the same ID.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.peak_roi` beneath it:

```text
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --manifest "manifest-output/peak_roi"
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/peak_roi/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/peak-roi --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\peak-roi.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\peak-roi.exe --install
```

These executable invocations accept all the same management options. Cargo's
`install --force` can replace an existing binary on rebuild; it is separate from
the plugin's `--install --force` option. The latter copies the executable by default.
For a custom runner or argv, change `declaration::builder()` to call
`PluginBuilder::launch_command(...)` and install with `--copy=false`. That API
records the chosen command; it does not copy or validate the runner. Use a runner only when
it preserves the host's inherited connection handles and launches the plugin
reliably on the target platform. There is no CLI launch-command override.

Open **Tools → Show Peak ROI** after rescanning. All five widget commands remain hidden from the Tools menu.

### Development manifest

[src/declaration.rs](src/declaration.rs) is the declaration source;
[vizi-plugin.json](vizi-plugin.json) is its checked-in development snapshot,
including explicit SDK defaults. A test compares them outside `entry`.
The snapshot retains its relative launch command for the development drivers.
The serving executable uses its compiled declaration, so it does not load a
manifest from the working directory.

For the development driver commands below, build and copy the binary on
Linux/macOS first:

```text
cargo build --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml
cp examples/peak_roi/rust/target/release/peak-roi examples/peak_roi/rust/peak-roi
```

That copy is only for the relative development entry. The installation commands
above do not need it.

To drive an installed copy on Linux, with the repository's Python driver
dependencies available:

```text
cargo run --release --locked --manifest-path examples/peak_roi/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/peak_roi/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.peak_roi"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options. Installation tests must use a scratch
`--install=DIR` or `VIZI_PLUGINS_DIR`.

## Test and run against the emulator

From the repository root, after building and copying the binary:

```sh
cargo test --locked --manifest-path examples/peak_roi/rust/Cargo.toml
cargo clippy --locked --all-targets --manifest-path examples/peak_roi/rust/Cargo.toml -- -D warnings
cargo fmt --check --manifest-path examples/peak_roi/rust/Cargo.toml
cargo test --locked --all-targets --manifest-path polyglot/rust-sdk/Cargo.toml
```

Eleven Rust tests cover declaration/snapshot parity, the manifest, little-endian scalar decoding, an
independent full 3D convolution comparison, clamp edges, a synthetic blob,
raster-order ties, strictly positive UI extents, zero/tiny Gaussian kernels,
non-finite voxel handling, cancellation, four ROI controls,
independently expected compiled display coordinates, conic arcs, and dense poses.
The shared host emulator drives this implementation too:

```sh
.venv/bin/python examples/peak_roi/python/hostsim.py --plugin-dir examples/peak_roi/rust
```

The SDK's `unmatched host reply id "nobody-asked"` diagnostic during this test
is expected: the emulator deliberately sends an unsolicited 24 MiB reply.

## Behaviour choices

Invalid extent edits fail and restore the previous accepted widget value.
Extents must be finite and strictly positive; zero would create a singular
ellipsoid and is rejected. A Gaussian radius above 100,000 voxels is refused
before allocation. Before blurring, NaN and −Inf voxels become the volume's
minimum finite value; +Inf remains a possible peak. A volume with no finite
voxel fails with a status message. Numerically zero kernel taps are skipped
to prevent `0 × +Inf` from producing NaN at tiny positive extents.
The worker refuses changed dataset generations, including a final view check
before contribution. The protocol cannot make that check and contribution atomic.

The selected channel is `visible_channels[0]`, or `0` when the list is empty.
`ViewState.c` is deprecated and always zero; it is not used.
