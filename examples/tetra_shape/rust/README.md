# Tetra Shape — Rust

The plugin of [SPEC.md](../SPEC.md) contributes one irregular tetrahedron four ways:
free corner movement, uniform scaling through twelve linear bindings, a
volume-preserving shaper, and a declared type without a shaper. The Rust SDK owns
the shape exchange, missing-shaper rejection, and drag lifecycle. This plugin
only supplies geometry, the closed-form calculation, and an informational end hook.

## Build and install

Requires Rust 1.88 or newer and the repository's Rust SDK. Run these commands from
the repository root in a POSIX shell, PowerShell, or Windows Command Prompt:

```text
cargo build --release --locked --manifest-path examples/tetra_shape/rust/Cargo.toml
cargo run --release --locked --manifest-path examples/tetra_shape/rust/Cargo.toml -- --install
```

The installer copies the native executable and generates its manifest under
`VIZI_PLUGINS_DIR`, or `~/.vizi/plugins` (`%USERPROFILE%\.vizi\plugins` on Windows)
when that variable is unset. The plugin directory is `org.example.tetra_shape`.
An explicit installation root is also supported:

```text
cargo run --release --locked --manifest-path examples/tetra_shape/rust/Cargo.toml -- --install rust-plugin-test
```

The installed manifest launches the native executable directly on Windows,
macOS, and Linux. It needs neither Cargo nor Python at launch. `--manifest DIR`
writes a native-entry manifest without installing; `--help` lists management
options. All contributions, including both region types, are declared in code.

Installation success prints `INSTALLED: <id> <version> in <directory>`.
Failures exit nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`), the cause
and a separate `To fix:` block on stderr. Use the named flag or path to recover;
`--help` lists the management options. To try an installation without touching
your plugins directory, use a scratch `--install=DIR` or `VIZI_PLUGINS_DIR`.

## Run against the emulator

After the release build, the checked-in development manifest runs `launch.sh`,
which changes to its directory and immediately execs the compiled plugin. It does
not compile at launch. This development entry and the shared socket driver are
Unix-only; use the native installation above on Windows.

From the repository root with the Python environment prepared (`uv sync`):

```sh
.venv/bin/python examples/tetra_shape/python/hostsim.py --plugin-dir examples/tetra_shape/rust
```

Or from `examples/tetra_shape/python`:

```sh
uv run --project ../../.. python hostsim.py --plugin-dir ../rust
```

The command `tetra.contribute` is deliberately hidden from menus. The driver
invokes it and performs all fourteen acceptance checks. It can also target an
installed `org.example.tetra_shape` directory using `--plugin-dir`.

vizi v0.9.4 and later implement this exchange. The driver runs against
`vizi_hostsim`, so no vizi installation is needed to test it.

## Offline checks

From the repository root:

```text
cargo test --locked --manifest-path examples/tetra_shape/rust/Cargo.toml
cargo +1.88.0 test --locked --manifest-path examples/tetra_shape/rust/Cargo.toml
cargo fmt --check --manifest-path examples/tetra_shape/rust/Cargo.toml
cargo clippy --locked --manifest-path examples/tetra_shape/rust/Cargo.toml --all-targets -- -D warnings
```

Eight tests check the manifest, all four geometries, the twelve ordered bindings
against the analytic matrix and uniform scaling, pulled-out and pushed-in volume
preservation, the rejection boundary and face crossing, finite result cardinality,
and calculations from a different incoming rest shape without preview history.
