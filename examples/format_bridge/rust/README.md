# Crate Format Bridge on the Rust SDK

The crate format bridge of [SPEC.md](../SPEC.md), built on the in-tree Rust
SDK: a reader, an exporter, a panel, four commands, and host-free crate tools.
A picker command uses `Context::after_reply`, which sends its picker request
after the command's acknowledgement.

## Install

Requires Rust 1.88 or newer and a native linker. From the repository root, this
command works in POSIX shells on Linux/macOS, Windows PowerShell, and cmd:

```text
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --install
```

The executable declares its whole interface through `PluginBuilder`; installation
writes that interface and copies the executable into the plugin directory as
`bin/plugin-<sha256>` (`.exe` on Windows). Vizi launches that copy directly,
without Cargo or a repository script. Cleaning or moving the checkout does not
break the installed copy. Old binary generations remain for running hosts;
identical builds reuse the same copy. Rebuild and reinstall
after changing declarations, then rescan plugins or restart Vizi. The plugin ID
is `org.example.format_bridge`; the other implementations of this example use
the same ID, so install only one at a time.

With no directory, the SDK uses `VIZI_PLUGINS_DIR` when set, otherwise the user's
`.vizi/plugins` directory (`%USERPROFILE%\.vizi\plugins` on Windows). An explicit
directory overrides that choice. It is the **plugins root**, not the individual
plugin directory; the SDK creates `org.example.format_bridge` beneath it:

```text
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --install "rust-plugin-test"
```

Replace `rust-plugin-test` with any desired relative or absolute plugins root;
quote paths containing spaces. Both `--install DIR` and `--install=DIR` work.
Reinstalling an SDK-owned directory updates it. To replace an unmanaged manifest,
use `--force`; unrelated files survive:

```text
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --install "rust-plugin-test" --force
```

To inspect or manually deploy a generated manifest and its packaged resources:

```text
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --manifest "manifest-output/format_bridge"
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --help
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
cargo install --locked --path examples/format_bridge/rust --root rust-plugin-bin
```

Choose a durable path instead of `rust-plugin-bin` if the checkout will be moved
or removed. Then register the executable **from that location**. Linux/macOS:

```text
./rust-plugin-bin/bin/format-bridge --install
```

Windows PowerShell:

```text
.\rust-plugin-bin\bin\format-bridge.exe --install
```

Windows cmd:

```text
rust-plugin-bin\bin\format-bridge.exe --install
```

These executable invocations accept all the same management options. Cargo's
`install --force` can replace an existing binary on rebuild; it is separate from
the plugin's `--install --force` option. The latter copies the executable by default.
For a custom runner or argv, change `declaration::builder()` to call
`PluginBuilder::launch_command(...)` and install with `--copy=false`. That API
records the chosen command; it does not copy or validate the runner. Use a runner only when
it preserves the host's inherited connection handles and launches the plugin
reliably on the target platform. There is no CLI launch-command override.

The calibration table is embedded in the binary and packaged as
`vizi-crate-cal.bin` beside the generated manifest by both management commands.
Through **Plugin Manager → Crate Format Bridge → Import…**, choose that installed
file. The host verifies its declared 32-byte size and SHA-256 before use. A
conflicting file is preserved and reported; move it aside and retry. Ordinary
crates work without provisioning, but calibrated crates require it. A runtime
**Use a different calibration…** override still takes precedence.

The `check`, `compare` and `recreate` file tools are dispatched before management
parsing and continue to work without a host. Open `.crate` files normally;
**Tools → Crate bridge…** opens the panel.

Exports use only the host's calibration fields; unknown values stay zero.
The source document is never scraped. Stderr is quiet when all four values
(z, y, x, t) are known or all are unknown; partial calibration logs one
diagnostic. The panel shows status and voxel size.

Every export sets `DERIVED`, keeps `source_metadata` verbatim as the source's
document, and stores the raw host provenance record in its own section.
Channel names are written only for FILE or USER sources; placeholders and
names containing NUL become unnamed, and trailing unnamed entries are dropped.
The reader declares these names in `ReaderOpened.channel_names`. Reopening a
DERIVED crate serves no metadata document, because the stored document
belongs to its source. Malformed names are ignored and diagnosed in status;
voxels remain readable.

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
cargo build --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml
cp examples/format_bridge/rust/target/release/format-bridge examples/format_bridge/rust/format-bridge
```

That copy is only for the relative development entry. The installation commands
above do not need it.

To run the driver against an installed copy, on Linux, with the repository's
Python driver dependencies available:

```text
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- --install "rust-plugin-test"
uv run python examples/format_bridge/python/hostsim.py --plugin-dir "rust-plugin-test/org.example.format_bridge"
```

The host drivers need Unix socket/shared-memory support.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options.

## Offline tools

Run from the repository root:

```sh
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- compare examples/format_bridge/fixtures/sample.crate examples/format_bridge/interop/rust.crate
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- check examples/format_bridge/fixtures/badcrc.crate
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- recreate examples/format_bridge/fixtures/sample.crate recreated.crate --order scrambled
```

`compare A B [--calibration TABLE]` reports `byte-equal`, `semantically equal`,
`voxel-equal`, or `different`, with document/spacing/channel-name/CRC notes.
DERIVED and provenance are noted but do not affect semantic equality. It exits
**0 for any verdict**, **2 for an unopenable or undecodable input**, **1 for usage**.
CRC-bad blocks are compared with an explicit note. Calibrated inputs require
`--calibration examples/format_bridge/fixtures/vizi-crate-cal.bin`.

`check FILE` verifies the footer, index and every block CRC, counts all failed
blocks and names the first. It exits 3 for bad block CRCs, 2 for an invalid file,
0 for a clean file. It does not need calibration to check stored bytes.

`recreate IN OUT [--order ascending|reverse|scrambled] [--calibration TABLE]`
decodes the input and writes an uncalibrated crate, preserving header spacing
and the valid UTF-8 document, DERIVED flag, provenance record and channel
names, clearing CALIBRATED. The default is ascending block order. It refuses
an existing destination to prevent accidentally overwriting the input.

This command produces the committed [rust.crate](../interop/rust.crate):

```sh
cargo run --release --locked --manifest-path examples/format_bridge/rust/Cargo.toml -- recreate examples/format_bridge/fixtures/sample.crate examples/format_bridge/interop/rust.crate
```

It is byte-identical to the other files in `../interop/`. The destination
already exists in this checkout; choose a fresh path when reproducing the
command.

## Tests and the emulator

```sh
cargo test --locked --manifest-path examples/format_bridge/rust/Cargo.toml
cargo clippy --locked --all-targets --manifest-path examples/format_bridge/rust/Cargo.toml -- -D warnings
cargo fmt --check --manifest-path examples/format_bridge/rust/Cargo.toml
.venv/bin/python examples/format_bridge/rust/verify_offline.py
.venv/bin/python examples/format_bridge/python/hostsim.py --plugin-dir examples/format_bridge/rust
```

The Python checks need the repository `.venv` and its existing dependencies.
The emulator needs local socket permissions.

The selected channel is `visible_channels[0]`, or `0` when the list is empty.
`ViewState.c` is deprecated and always zero; it is not used.

## Reader replacement

The SDK opens a different URI before closing the previous reader; a refused
open leaves that reader usable. Reloading the same URI closes it first, and a
failed reload leaves no reader. Handles are never reused, and closing a replaced
handle cannot close its successor. The old reader's close hook clears the panel
only if it is still the active reader, preserving the successor's header and
voxel-size display.
