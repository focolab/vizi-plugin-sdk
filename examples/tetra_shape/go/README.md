# Tetra Shape in Go

Implements [SPEC.md](../SPEC.md) using the Go SDK's typed declarations and
shaper API. `tetra.contribute` contributes four point-cloud tetrahedra in one
replacement batch:

- `free`: four free corner handles; the host moves the selected corner.
- `scale`: radial handles and twelve linear bindings; the host scales all
  corners about the center, with STAR surface derivation and a radius clamp.
- `volume`: a registered shaper scales the opposite face to preserve volume.
- `unshaped`: a declared type with no shaper; the SDK rejects its commits.

The shaper reads only the current ShapeEdit. Preview and commit use the same
calculation. The end hook logs the drag ID, outcome and detail to stderr.

Every protocol 1.0 host implements this exchange. The driver runs the plugin
as a real process over real sockets against the emulator. The command has
`menu: null`, as [SPEC.md](../SPEC.md) §1 requires, so it is invoked by the
driver rather than listed in Tools.

## Build and run the driver

Requires Go 1.25 or newer, this repository's directory layout, and the prepared
Python environment for the emulator. From the repository root:

```sh
cd examples/tetra_shape/go
go build -o build/tetra-shape .
go test -race -count=1 ./...
go vet ./...
cd ../../..
.venv/bin/python examples/tetra_shape/python/hostsim.py --plugin-dir examples/tetra_shape/go
```

Alternatively, from `examples/tetra_shape/python`:

```sh
uv run --project ../../.. python hostsim.py --plugin-dir ../go
```

The checked-in manifest launches `sh ./launch.sh`, which checks for the prebuilt
executable and **execs** it, preserving the serving PID and inherited descriptors.
It never compiles at launch. Build once after a fresh checkout and rebuild after
source changes; a missing binary produces an actionable error. This development
launcher and the emulator are Unix-only. Use the native installer below on
Windows.

To exercise the process with race detection too:

```sh
cd examples/tetra_shape/go
go build -race -o build/tetra-shape .
cd ../../..
.venv/bin/python examples/tetra_shape/python/hostsim.py --plugin-dir examples/tetra_shape/go
```

## Install on Linux, macOS or Windows

From `examples/tetra_shape/go`, Linux/macOS POSIX shell:

```sh
CGO_ENABLED=0 go run . --install
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go run . --install
```

Windows cmd:

```bat
set CGO_ENABLED=0
go run . --install
```

The SDK copies the native executable before `go run` removes it, generates the
manifest from the code declaration (including `region_types`), and prints the
installed directory. The installed plugin needs neither Go nor this checkout;
its manifest launches the copied executable directly, with `.exe` on Windows.
`CGO_ENABLED=0` is required because the installer does not package native libraries.

`--install` uses `VIZI_PLUGINS_DIR` when set, otherwise `.vizi/plugins` in the
user's home directory. The plugin subdirectory is `org.example.tetra_shape`.
Use `--install=installed-plugins` to choose another plugin root; relative paths
are relative to the current directory. Quote the whole argument for spaces.
Repeat installs into an SDK-managed directory need no flag; `--force` permits
adopting an existing nonempty directory that the installer does not manage.
Installation does not restart running sessions. Rescan in vizi's Plugin
Manager. Install only one implementation with this ID.

A durable native build also supports installation and manifest-only output.
Linux/macOS:

```sh
CGO_ENABLED=0 go build -o build/tetra-shape .
./build/tetra-shape --install
./build/tetra-shape --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/tetra-shape.exe .
.\build\tetra-shape.exe --install
.\build\tetra-shape.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/tetra-shape.exe .
build\tetra-shape.exe --install
build\tetra-shape.exe --manifest=manifest-preview
```

`--manifest=DIR` writes a manifest into DIR that points to the current executable;
keep that binary in place. Prefer `--install` for a movable package. Do not use
`go run . --manifest`, because its temporary executable disappears. `--help`
prints the supported flags without starting a session.

## Tests

`go test -race -count=1 ./...` covers all twelve matrices and their
uniform-scale effect, the exact four region declarations, volume preservation
for outward and inward moves of every corner, rejection past the 1/16 height
limit and across the opposite face, finite complete results, input immutability,
already-edited/copied input, and code/manifest agreement.

Build outputs, installation scratch space and manifest previews are ignored by
Git.
