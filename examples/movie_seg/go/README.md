# Movie seg in Go

Two chunked overlays implement [SPEC.md](../SPEC.md): intensity bands per
movie timepoint, and a static footprint of foreground across all timepoints.
The SDK owns want-set membership, source workers, segment loans, deferred
publication, and shutdown. The plugin owns arithmetic, tables, commands and its panel.

Bands draw at priority 1, above the enclosing footprint at priority 0. Hiding a
band preserves its LUT color and sets alpha to 0, while also setting
`label_visible` to false. Voxel drawing uses the LUT; `label_visible` controls
compiled annotation visibility only.

## Install into vizi

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/movie_seg/go
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
  your home directory. The plugin lives in the `org.example.movie_seg` subdirectory.
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
CGO_ENABLED=0 go build -o build/movie-seg .
./build/movie-seg --install
./build/movie-seg --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/movie-seg.exe .
.\build\movie-seg.exe --install
.\build\movie-seg.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/movie-seg.exe .
build\movie-seg.exe --install
build\movie-seg.exe --manifest=manifest-preview
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

## In vizi

After installation and a Plugin Manager rescan, choose **Tools → Show Movie seg**
to start the plugin and open its panel. With a dataset open, the overlays serve
vizi's requested levels and timepoints automatically. Edit Threshold (initially
400) or toggle Band 1–3 to change the state. Threshold text accepts surrounding
whitespace and must parse to a finite nonnegative number. Both the checkboxes
and the general band_visible command keep panel state synchronized, even while
the panel is closed. Invalid commands leave the state unchanged.

## Build and test

Build the relative development entry from this directory:

```sh
go build -o movie-seg .
go test -race ./...
go vet ./...
```

## Run against the emulator

From the repository root, with the repository's Python environment:

```sh
.venv/bin/python examples/movie_seg/python/hostsim.py --plugin-dir examples/movie_seg/go
```

Offline tests cover every band edge, NaN/infinities and integer input, a tight
box with internal zeros and a nonzero origin, whole/empty volumes, the static
union, every LUT row, visibility, command refusals, reply spelling and manifest
fields. The SDK validator runs against the published manifest fixtures.

Reads use a 512 MiB copy budget and assume a little-endian host.

The complete manifest (also checked in as `vizi-plugin.json`):

```json
{
  "manifest_version": 1,
  "id": "org.example.movie_seg",
  "name": "Movie seg",
  "version": "0.1.0",
  "engines": {
    "vizi": "1.0"
  },
  "entry": {
    "command": [
      "./movie-seg"
    ]
  },
  "permissions": [
    "data_access"
  ],
  "contributions": {
    "overlays": [
      {
        "id": "bands",
        "name": "Intensity bands",
        "chunked": true,
        "time": "per_t"
      },
      {
        "id": "footprint",
        "name": "Footprint",
        "chunked": true,
        "time": "static"
      }
    ],
    "commands": [
      {
        "id": "movie_seg.threshold",
        "title": "Set threshold",
        "menu": null
      },
      {
        "id": "movie_seg.band_visible",
        "title": "Show or hide a band",
        "menu": null
      },
      {
        "id": "movie_seg.band_1",
        "title": "Show band 1 (dim)",
        "menu": null
      },
      {
        "id": "movie_seg.band_2",
        "title": "Show band 2 (mid)",
        "menu": null
      },
      {
        "id": "movie_seg.band_3",
        "title": "Show band 3 (bright)",
        "menu": null
      }
    ],
    "panels": [
      {
        "id": "movie_seg",
        "title": "Movie seg",
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            {
              "kind": "row",
              "id": "rt",
              "children": [
                {
                  "kind": "label",
                  "id": "lt",
                  "text": "Threshold"
                },
                {
                  "kind": "line_edit",
                  "id": "threshold",
                  "command": "movie_seg.threshold",
                  "value": "400"
                }
              ]
            },
            {
              "kind": "checkbox",
              "id": "band_1",
              "command": "movie_seg.band_1",
              "text": "Band 1 (dim)",
              "value": true
            },
            {
              "kind": "checkbox",
              "id": "band_2",
              "command": "movie_seg.band_2",
              "text": "Band 2 (mid)",
              "value": true
            },
            {
              "kind": "checkbox",
              "id": "band_3",
              "command": "movie_seg.band_3",
              "text": "Band 3 (bright)",
              "value": true
            }
          ]
        }
      }
    ]
  }
}
```
