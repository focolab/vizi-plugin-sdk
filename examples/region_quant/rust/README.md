# Region Quant — Rust

The region-quant example on `polyglot/rust-sdk`: live region measurements,
row selection, brightest-frame navigation, a resumable region tour, viridis
swatches, and CSV export.
Implements [the example's specification](../SPEC.md) using the SDK's control,
subscription, Follow, panel and shared-memory APIs.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.region_quant`; the other implementations of this example use
the same ID.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.region_quant` beneath it:

```text
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --manifest "manifest-output/region_quant"
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/region_quant/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/region-quant --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\region-quant.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\region-quant.exe --install
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

The existing POSIX `launch.sh` is only a development launcher. It execs the prebuilt release binary; self-installed plugins use the release executable directly.

On Linux, with the repository's Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/region_quant/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/region_quant/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.region_quant"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options.

## Test and run against the emulator

From the repository root, using the repository's Python emulator environment:

```sh
cargo test --locked --manifest-path examples/region_quant/rust/Cargo.toml
cargo clippy --locked --manifest-path examples/region_quant/rust/Cargo.toml --all-targets -- -D warnings
cargo fmt --manifest-path examples/region_quant/rust/Cargo.toml -- --check
cargo build --release --locked --manifest-path examples/region_quant/rust/Cargo.toml
.venv/bin/python examples/region_quant/python/hostsim.py --plugin-dir examples/region_quant/rust
uv run python examples/region_quant/rust/verify_shutdown.py
uv run python examples/region_quant/rust/verify_row_clicks.py
uv run python examples/region_quant/rust/verify_batch_b.py
for run in 1 2 3; do
    uv run python examples/region_quant/python/shutdown_check.py --plugin-dir examples/region_quant/rust
done
```

Use `--offline` on the Cargo commands when dependencies are already cached.
The eighteen offline tests cover declaration/snapshot parity, integer rounding and overflow boundaries, empty
measurements, swatch indices, ordering, CSV quoting, earliest-frame ties, resume
ordering after deletion, trace invalidation/worker survival, cancellation before
the shutdown hook runs, late successful reads, retry/backoff/cancellation, partial
measurement errors, and repeatable idle-worker cleanup.
`verify_row_clicks.py` checks a prompt row-click decline while a view read is
parked, and partial commits when a region disappears during region, view, and
channel measurements. `verify_shutdown.py` parks a real trace read at the host and verifies shutdown
and process exit without answering it. It also checks shutdown immediately after
panel opening, an idle worker after both traces finish, and abrupt host loss,
with no new reads or trace cancellation diagnostics. `verify_batch_b.py` covers
same-channel no-ops, partial commits after non-NOT_FOUND errors, cause 0, export
write failures, both bounded navigation commands, 1025 regions capped at 1024,
held swatches on grant, and f32 reads/CSV (including leading-space names).
The shared shutdown check
covers the same four scenarios and is repeated to exercise timing differences.
The driver additionally decodes the PNGs, exercises the plugin over
sockets, and verifies prompt brightest-frame replies while reads take 0.5 s.
It ends `host emulation passed`.

## Implementation

`src/app.rs` serializes table changes and their publication with one mutex.
It obtains each mask from the host, reads only that mask's volume box and sums
the included integer voxels. A measurement returning `NOT_FOUND` removes that
region and its trace while the other changes are committed. Other read failures
preserve that row's numbers, commit the rest, and report the error. Row selection
and brightest-frame commands wait at most 900 ms for the model; if a re-measure
still holds it, the command declines its view cause
so the host receives a prompt answer. CSV export starts after the command reply, waits
for the save dialog without holding the lock, then snapshots the current table.

`src/trace.rs` runs one dedicated worker, computing each row's counts and sums
over all frames ahead of time. It reads outside its separate cache lock,
checks dataset generation/dtype before starting and a revision token before
accepting each result, and restarts invalidated
rows from frame 0. A missing region abandons its trace; other read errors retry
from frame 0 after a delay starting at 100 ms and doubling up to 5 s. A successful
trace resets the delay; shutdown interrupts the wait. The `tracing` label shows
progress. Brightest-frame commands
read only a brief cache snapshot and answer from the precomputed best frame;
incomplete traces are declined with their progress and the dataset's actual
frame count. Slow trace reads hold no cache lock; a busy table causes a decline
after at most 900 ms. Cause-zero commands send no view request and report that
no dataset is open. Selecting the channel already in use issues no reads.

The shutdown hook stops, wakes and joins the trace worker. Cleanup after serving
returns covers abrupt host loss too. A read canceled by SDK shutdown ends the
worker without starting another row; late successful results are discarded.
Expected NOT_FOUND from a deleted region abandons only that revision quietly.
This handles plugin work; the host must separately cancel publication of any
storage read it already started before session teardown.

The panel offers a tour while rows exist. Its Follow worker visits a snapshot
in list order, frames each remaining region and waits 0.5 seconds interruptibly.
A host end reports its reason; Resume starts after the last shown display-name
and ID pair, including when that row has since been deleted.

`src/table.rs` contains exact integer arithmetic, sorting, CSV encoding and a
small RGB PNG encoder. Intermediate mean calculations and comparisons use
`u128`; counts and sums use `u64`. Intensity reads support u8/u16. On f32,
rows retain names with empty measurements in the panel and CSV, use swatch 0,
and report why they are not measured; no measurements or traces run. Each new
dataset refreshes shape and dtype and resets an invalid channel to 0 in both
the model and picker. Returning to integer data restores measurements.

The list shows the first 1024 regions in order and reports the total when
truncated. Labels share a patch and `Context::push_image_rows` batches swatches
64 at a time. Swatches accompany every publication; the SDK holds them until
the panel's segment installs, including a relaunch without `PanelOpened`.
