# Ray probe on the Rust SDK

The implementation of [SPEC.md](../SPEC.md) on the Rust SDK: pick the brightest
voxel along a 3D ray or 2D projection column, optionally refine a pyramid pick
at level 0, plot its intensity profile, and contribute the picks as ellipsoid
ROIs.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.ray_probe`; the other implementations of this example use
the same ID, so install only one at a time.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.ray_probe` beneath it:

```text
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --manifest "manifest-output/ray_probe"
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/ray_probe/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/ray-probe --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\ray-probe.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\ray-probe.exe --install
```

These executable invocations accept all the same management options. Cargo's
`install --force` can replace an existing binary on rebuild; it is separate from
the plugin's `--install --force` option. The latter copies the executable by default.
For a custom runner or argv, change `declaration::builder()` to call
`PluginBuilder::launch_command(...)` and install with `--copy=false`. That API
records the chosen command; it does not copy or validate the runner. Use a runner only when
it preserves the host's inherited connection handles and launches the plugin
reliably on the target platform. There is no CLI launch-command override.

All three pane SVGs are embedded with `PluginBuilder::icon` and written with the
manifest. Installed icon paths refer to an SDK resource directory; no icon path
points back into the checkout. Reinstalling identical icons reuses those files.

Engage Probe with its pane button, Shift+P key, or middle mouse trigger. Drag to preview
the ray and release to pick; U/X undo/clear, and Contribute publishes picks as ROIs.

### Development manifest and installation check

[src/declaration.rs](src/declaration.rs) is the declaration source;
[vizi-plugin.json](vizi-plugin.json) is its checked-in development snapshot,
including explicit SDK defaults. A test compares them outside `entry`.
The snapshot retains its relative launch command for the development drivers.
The serving executable uses its compiled declaration, so it does not load a
manifest from the working directory.

On Linux, with the repository's Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/ray_probe/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/ray_probe/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.ray_probe"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options.

## Test and run against the emulator

From the repository root:

```sh
cargo test --locked --manifest-path examples/ray_probe/rust/Cargo.toml
cargo clippy --locked --all-targets --manifest-path examples/ray_probe/rust/Cargo.toml -- -D warnings
cargo fmt --check --manifest-path examples/ray_probe/rust/Cargo.toml
env PYTHONDONTWRITEBYTECODE=1 .venv/bin/python examples/ray_probe/python/hostsim.py --plugin-dir examples/ray_probe/rust
```

The Python driver needs the repository environment with grpcio, numpy and
atomicshm, and permission to create local sockets. The bytecode setting keeps
Python caches from being written.

The unit tests cover declaration/snapshot parity; axis/oblique rays; closed face, edge and corner contacts;
reverse rays and boundary endpoints against an independent slab oracle;
crop conversion; a non-power-of-two sampling factor; coarse blocks and read
budget; NaN/infinity and first-maximum tie rules; physical distance; column aim
endpoints; manifest/icons; and the ellipsoid's controls, compiled conic arcs,
exact fractional center and timelessness; a thin axis
with per-axis factors `(2, 4, 4)` and a centered ray from `-0.5` to `99.5`;
and, at activation, mouse holds and omitted/refused keys.

The driver verifies live aim and reliable clearing, marker/text
publication, the profile PNG and physical time, refined/unrefined multilevel
reads, 2D shapes, actions during gestures, panel restore, ROI batches, refused
contexts, busy handling, and shutdown. Its final lines are:

```text
ok    no violations: no ContractViolation beyond the two undeclared-level records
ok    teardown: ShutdownReport COMPLETED, then a clean end of stream; 2 violation report(s) in all, none unexpected

host emulation passed
```

Aim clearing uses an empty reliable `OverlayUpdate` with the next shared overlay
revision; the driver accepts that or an `OverlayRemove`.

The driver checks the protocol and geometry, not rendering in vizi. 2D panes
project line sets into every slice and dash out-of-slice segments; line
thickness and marker size are level-0 voxels in 2D, screen pixels in 3D. Text
is 3D-only. The assigned mouse/key triggers are reported at activation.
