# Peak ROI in Go

An implementation of [the peak_roi specification](../SPEC.md), built
on the repository's [Go SDK](../../../polyglot/go-sdk/plugin/README.md).
The SDK supplies the transport, grant lifecycle, typed handlers, payload copying,
deposit releases, panel patches and host client. No generated bindings or
transport implementation are copied into this example.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/peak_roi/go
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
  your home directory. The plugin lives in the `org.example.peak_roi` subdirectory.
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
CGO_ENABLED=0 go build -o build/peak-roi .
./build/peak-roi --install
./build/peak-roi --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/peak-roi.exe .
.\build\peak-roi.exe --install
.\build\peak-roi.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/peak-roi.exe .
build\peak-roi.exe --install
build\peak-roi.exe --manifest=manifest-preview
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

## Development

Build the relative development entry before running the Unix driver, from this
directory:

```sh
go build -o peak-roi .
```

## Use

Enter Z/Y/X semi-axes in **voxels** (defaults `4`, `8`, `8`), choose **Current
timepoint** or **All timepoints**, then press **Find peak**. The run returns
promptly and reports progress and its outcome in the panel. It searches the
first visible channel captured at run start (channel 0 if none is visible), at
level 0, over the whole uncropped volume. Re-running replaces the same `peak` ROI. All-timepoint results have a
dense translation pose; current-timepoint results are timeless.

Invalid edits fail immediately and restore the last accepted widget value.
Panel reopening restores the model. Edits during a run apply to the next run;
overlapping runs are refused. Shutdown cancels the worker and waits within the
host's deadline. Read replies and the final view are checked for a dataset
generation change before contributing an ROI.

There is no fixed volume-size or voxel-count cap. Copy and mapping sizes are
bounded by the process's addressable slices; intensity workspaces and shape
products are checked before allocation. The machine still needs enough memory:
processing owns the input bytes, decodes their scalar type, and converts them to
one float64 workspace (about 1.56 GiB for 100×1024×2048 voxels). The blur works
in place on independent axis-line blocks, targeting 256 KiB of scratch per
worker (at least one whole axis line), rather than a second full-volume workspace. It uses Go's GOMAXPROCS worker count.

The Gaussian radius is limited by addressable kernel storage. All-timepoint contributions are bounded by the protocol's
1,048,576-scalar pose_translation field: three scalars per frame, or 349,525
frames. Current-timepoint mode has no such time-axis restriction. Extents must
be finite and strictly positive; zero would create a singular ellipsoid.
U8, U16, F32 and defensively F64 are decoded. Before blurring, NaN and −Inf
voxels are replaced with the volume's minimum finite value; +Inf remains a
valid maximum. A volume with no finite voxel fails the run. The numerical
kernel still supports zero sigma independently of UI validation.

## Test and run against the emulator

From this directory:

```sh
go test -race ./...
go vet ./...
```

Tests cover a known Gaussian peak, independent direct-convolution expectations,
clamped edges, anisotropic strides, tie breaking, dtypes/endianness, four ordered
ROI controls, all 18 compiled vertices, rational quarter arcs, dense time poses,
panel state, invalid inputs including zero extents, non-finite voxel handling,
infinite-peak ties, recovery after a volume with no finite data, busy rejection,
generation changes, worker errors,
command-context independence, shutdown cancellation, early SDK callbacks, and
a `COMPLETED` shutdown report followed by gRPC EOF. Manifest checks cover this
complete declaration, validated by the SDK against the manifest schema.

From the repository root, using an environment containing the Python SDK,
`grpcio` and the emulator's dependencies:

```sh
.venv/bin/python examples/peak_roi/python/hostsim.py --plugin-dir examples/peak_roi/go
(cd polyglot/go-sdk && go test ./...)
```
