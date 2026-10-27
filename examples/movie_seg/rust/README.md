# Movie seg on the Rust SDK

The implementation of [SPEC.md](../SPEC.md) on the Rust SDK: two chunked
overlays, intensity bands per frame and a static footprint across all
timepoints. Both use the host's wanted level and return a tight label box. A
panel starts the plugin and controls the threshold and band visibility.

## Install into vizi

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.movie_seg`; the other implementations of this example use
the same ID, so install one at a time.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.movie_seg` beneath it:

```text
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --manifest "manifest-output/movie_seg"
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/movie_seg/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/movie-seg --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\movie-seg.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\movie-seg.exe --install
```

These executable invocations accept all the same management options. Cargo's
`install --force` can replace an existing binary on rebuild; it is separate from
the plugin's `--install --force` option. The latter copies the executable by default.
For a custom runner or argv, change `declaration::builder()` to call
`PluginBuilder::launch_command(...)` and install with `--copy=false`. That API
records the chosen command; it does not copy or validate the runner. Use a runner only when
it preserves the host's inherited connection handles and launches the plugin
reliably on the target platform. There is no CLI launch-command override.

## In vizi

Open a movie, then **Tools → Show Movie seg** to start the plugin. Opening the
panel starts its session; the host requests the volumes the panes show and the
overlays fill in as they are served. Enter a threshold (default 400) and press
Enter to re-serve the current volumes; untick a band to hide it. Closing the
panel may end the session and its overlays.

`movie_seg.threshold` takes a finite nonnegative numeric `value` or numeric
text with optional surrounding whitespace, and re-serves both current want
sets. `movie_seg.band_visible` takes integer `band` 1–3 and boolean `visible`;
the checkbox commands `movie_seg.band_1`–`band_3` take boolean `value`. Both
forms update the bands table and corresponding checkbox. Accepted threshold
changes update the box to the formatted number. These patches are sent even
while the panel is closed; refused commands change nothing.

Bands draw at priority 1 over the enclosing footprint at priority 0. Hiding a
band keeps its LUT color but sets alpha to zero; `label_visible` also records
the choice for compilation. Opacity alone cannot reveal an enclosed overlay
in 3D, because it accumulates through the enclosing volume.

## Development manifest and installation check

[src/declaration.rs](src/declaration.rs) is the declaration source;
[vizi-plugin.json](vizi-plugin.json) is its checked-in development snapshot,
including explicit SDK defaults. A test compares them outside `entry`.
The snapshot retains its relative launch command for the development drivers.
The serving executable uses its compiled declaration, so it does not load a
manifest from the working directory.

On Linux, with the repository's Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/movie_seg/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/movie_seg/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.movie_seg"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options.

## Build, test and run against the emulator

From the repository root, using the development Python environment with
`atomicshm` installed:

```sh
cargo test --locked --manifest-path examples/movie_seg/rust/Cargo.toml
cargo clippy --locked --all-targets --manifest-path examples/movie_seg/rust/Cargo.toml -- -D warnings
cargo fmt --check --manifest-path examples/movie_seg/rust/Cargo.toml
.venv/bin/python examples/movie_seg/python/hostsim.py --plugin-dir examples/movie_seg/rust
.venv/bin/python examples/movie_seg/rust/verify_panel.py --plugin-dir examples/movie_seg/rust
```

The tests cover declaration/snapshot parity, band edges and nonfinite input, tight boxes, the union,
tables, numeric-text argument validation, reply formatting, and manifest restrictions.
`verify_panel.py` additionally checks all three checkbox commands and the scripted
form before opening, plus refused commands emitting no patches. It waits for a
later accepted panel update to arrive, without relying on the command reply's
timing relative to patches.

The SDK owns each source's worker, membership tracking, reservations, release
notifications, deferred answers and shutdown. The plugin keeps only its
threshold, visibility and table-sent flags. It reads every requested frame;
the footprint also reads `ViewState` and checks the dataset generation across
its frames.
