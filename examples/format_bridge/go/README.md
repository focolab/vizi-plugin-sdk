# Crate format bridge in Go

The crate format bridge of [SPEC.md](../SPEC.md), built on the Go SDK in this
repository: a protocol 1.0 reader and exporter. It reads and writes the crate
container, exposes the verification/description panel, and supports the
manifest calibration asset and a runtime override.

Exports take calibration only from the host's fields; unknown values stay zero.
Only partially known calibration produces a log. Exported crates are DERIVED:
they preserve `source_metadata` as source provenance, keep the host's raw JSON
record in its own section, and write only FILE/USER channel names. Reopening
declares those names and serves no source document as the artifact's own. The
panel reports the exported file, block count and voxel size.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/format_bridge/go
```

On Linux or macOS (POSIX shell):

```sh
CGO_ENABLED=0 go run . --install
```

On Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go run . --install
```

On Windows cmd:

```bat
set CGO_ENABLED=0
go run . --install
```

The SDK copies the temporary `go run` executable before Go removes it, writes
its generated manifest and packaged resources, and prints the installed path.
The installed copy needs neither Go nor this checkout. `CGO_ENABLED=0` is
required and checked because the installer does not package native libraries.
The binary targets the OS and architecture on which these commands run.

Installation choices:

- `--install` uses `VIZI_PLUGINS_DIR` when set, otherwise `.vizi/plugins` in
  your home directory. The plugin lives in the `org.example.format_bridge` subdirectory.
- Replace `--install` above with `--install=installed-plugins` to use that
  directory as the plugin root. Relative paths are relative to the current
  directory; absolute paths work too. Quote the whole argument for spaces,
  for example `"--install=installed plugins"`.
- Add `--force` to either install form to adopt an existing, nonempty plugin
  directory not marked by this installer. Unrelated files are retained.
  Repeat installs into SDK-managed directories need no `--force`.
- `--manifest=manifest-preview` writes only `vizi-plugin.json` and resources
  directly into `manifest-preview`. Bare `--manifest` writes them into the
  current directory. It prints the file path, not JSON. Use a durable built
  binary for this option, as shown below: its absolute path becomes the launch
  entry. Keep that binary there. `go run . --manifest` would name a temporary
  executable that disappears.
- `go run . --help` prints the available flags without connecting to a host.
  No arguments serves a session and requires vizi's launch environment.

To retain a build for manifest-only output or install it later, run these
commands from the same Go directory. Linux/macOS:

```sh
CGO_ENABLED=0 go build -o build/format-bridge .
./build/format-bridge --install
./build/format-bridge --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/format-bridge.exe .
.\build\format-bridge.exe --install
.\build\format-bridge.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/format-bridge.exe .
build\format-bridge.exe --install
build\format-bridge.exe --manifest=manifest-preview
```

Prefer `--install` for a movable, self-contained package: its launch entry
points directly to a copied native executable under `bin`, with `.exe` on
Windows. The SDK publishes the manifest last and gives each binary a
content-addressed path, so updates do not overwrite a running Windows
executable. Old binaries remain; stop their sessions before removing unused
`bin` entries. Installation does not restart sessions. Rescan in vizi's Plugin
Manager, and install only one implementation with this plugin ID at a time.

The typed declaration registers the interface and handlers together. An offline
test compares its generated manifest with [vizi-plugin.json](vizi-plugin.json)
outside `entry`; that file keeps the relative development launch line for the
Unix drivers. Installation uses the generated native launch entry.

The calibration fixture is embedded and written as `vizi-crate-cal.bin` in
the installed plugin directory. In Plugin Manager, select **Crate Format
Bridge → Import…** and choose that file. The manifest deliberately retains its
manual provisioning contract: an unprovisioned calibrated crate still refuses
to open, while uncalibrated crates work. The source fixture in
`examples/format_bridge/fixtures/vizi-crate-cal.bin` is another import source.

## Development

Build the relative development entry before running the Unix driver, from this
directory:

```sh
go build -o format-bridge .
```

Use **File → Open** for a `.crate`, **Export Data → Crate volume container**
to write one, and **Tools → Crate bridge…** for the panel. The CRC checkbox
applies to volume reads; plane reads deliberately read only the plane without
checking the whole-block CRC. An accepted picker command acknowledges before
prompting; its eventual result appears in the panel. Cancelling changes nothing.
A valid override affects subsequently opened calibrated crates; a malformed
replacement is reported and leaves the previous valid selection intact.

Describe uses the first visible channel, or channel 0 when none is visible,
for both host data access and the direct-reader fallback. It does not use the
deprecated `ViewState.C` field.

Plugin volume/chunk/description copies are bounded to 256 MiB per payload.
There is one active reader, multiple export handles, and one asynchronous
panel job at a time. A picker command uses `control.AfterReply`, which
starts its work only after the acknowledgement is sent. Reader replacement opens a different URI first and closes
the old file only on success; a refused open preserves it. A same-URI reload
closes first. Separate local handles let the old Close release its own file
without clearing the new reader's panel. Replaced wire handles never route to
the new file. Ordinary host calls have 30-second deadlines; pickers
use the SDK's separate 30-minute `Config.PickerTimeout` backstop. Source documents
never supply missing export calibration; unknown calibration stays unknown.

## Host-free tools

The same executable provides the comparator and writer:

```sh
./format-bridge compare ../fixtures/sample.crate ../interop/python.crate
./format-bridge compare ../fixtures/sample.crate ../fixtures/sample-calibrated.crate --calibration ../fixtures/vizi-crate-cal.bin
./format-bridge recreate ../fixtures/sample.crate ../interop/go.crate
./format-bridge recreate ../fixtures/sample.crate /tmp/scrambled.crate --order scrambled
./format-bridge check ../fixtures/badcrc.crate
```

`compare` prints `byte-equal`, `semantically equal`, `voxel-equal`, or
`different`, with reasons. Exit 0 means a verdict, including `different`;
2 means a file could not be opened/decoded; 1 means bad usage. CRC failures
are reported while comparison proceeds on decoded bytes. `check` validates
stored CRCs without requiring a calibration table; it exits 3 for failed block
CRCs, 2 for a malformed/unopenable file, and 0 for a clean check.

The third command above produces the committed `../interop/go.crate`. It is
byte-identical to `python.crate` and semantically equal to the reverse-order
sample fixture. `--order` accepts `ascending`, `reverse`, and `scrambled`;
the latter uses a fixed Go random seed, not the Python writer's shuffle.

## Tests and the emulator

From this directory:

```sh
go test -race ./...
go vet ./...
```

From the repository root, using the repository's Python environment:

```sh
.venv/bin/python examples/format_bridge/python/hostsim.py --plugin-dir examples/format_bridge/go
.venv/bin/python examples/format_bridge/go/verify_interop.py
```

Socket integration requires permission to use local sockets.

Offline tests cover the worked index and independently calculated voxels,
calibration, localized CRC failures, plane reads, malformed headers/metadata,
zero axes, reserved fields, index corruption, deterministic/scrambled writing,
big-endian source bytes, incomplete finish and abort, per-axis calibration
field conversion and both micro characters, comparator tiers/exit codes, source-index
ranking, a read before G, acknowledgement-before-picker ordering, hand-built
section offsets, DERIVED metadata suppression, malformed names, declared-name
filtering, embedded NULs, recreate preservation, and names in semantic
comparisons. DERIVED and provenance differences are noted but do not change
semantic equality.
`verify_interop.py` runs this comparator and the Python one over every fixture
pair, with and without calibration, and checks the files in `../interop/`.
