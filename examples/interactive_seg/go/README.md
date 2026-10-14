# Interactive segmentation in Go

An implementation of [SPEC.md](../SPEC.md) on the Go SDK (`polyglot/go-sdk`). Draw a loop around a
bright structure in a 2D slice, inspect the anchored segmentation, accumulate
labels, commit native masks, and measure a selected mask across time. Measurement
uses the first visible channel from a fresh view snapshot, or channel 0 when no
channels are visible.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/interactive_seg/go
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
  your home directory. The plugin lives in the `org.example.interactive_seg` subdirectory.
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
CGO_ENABLED=0 go build -o build/interactive-seg .
./build/interactive-seg --install
./build/interactive-seg --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/interactive-seg.exe .
.\build\interactive-seg.exe --install
.\build\interactive-seg.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/interactive-seg.exe .
build\interactive-seg.exe --install
build\interactive-seg.exe --manifest=manifest-preview
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

## Build and run against the emulator

Build the relative development entry from this directory:

```sh
go build -o interactive-seg .
go test -race ./...
go vet ./...
```

From the repository root, using the repository's prepared Python environment:

```sh
.venv/bin/python examples/interactive_seg/python/hostsim.py --plugin-dir examples/interactive_seg/go
```

`hostsim.py` is the shared driver: it checks the live trail,
segmentation and refusals, Add/Replace, repeat commits, the measurement PNG,
panel state, the busy guard and teardown.

The plugin uses the SDK's activation/deactivation callbacks and typed
`LabelVolume` push/compile methods. Labels use fresh one-use loans for every
push and compile, managed by the SDK; commit preserves accumulation, while
Clear preserves native regions. Intensity reads are limited to 16 million
voxels per window, label arrays to 64 MiB, and measurement plots to one
million timepoints. PNG axes are drawn with a small embedded raster font.
