# Ray probe in Go

The implementation of [SPEC.md](../SPEC.md) on the Go SDK: pick the brightest voxel
along a 3D ray or 2D projection column, optionally refine a pyramid pick at
level 0, inspect its intensity profile, and contribute numbered ellipsoid ROIs.

The probe toggle is **Shift+P**, avoiding vizi's Play/Pause key; the middle mouse
trigger also works. Picks can arrive while the profile panel is closed: the SDK
holds the latest PNG until its P grant is available, and the example still
re-pushes its model on PanelOpened. A profile update failure is diagnosed
separately and does not replace a successful pick's status.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/ray_probe/go
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
  your home directory. The plugin lives in the `org.example.ray_probe` subdirectory.
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
CGO_ENABLED=0 go build -o build/ray-probe .
./build/ray-probe --install
./build/ray-probe --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/ray-probe.exe .
.\build\ray-probe.exe --install
.\build\ray-probe.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/ray-probe.exe .
build\ray-probe.exe --install
build\ray-probe.exe --manifest=manifest-preview
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

The three pane icons are embedded and written under `icons` in both install
and manifest-only output. No checkout-relative icon paths are needed.

## Build

Build the relative development entry before running the Unix driver, from this
directory:

```sh
go build -o ray-probe .
```

## Run against the emulator

From the repository root with its prepared Python environment (`uv sync`,
then `uv pip install atomicshm` if the emulator is not yet installed):

```sh
.venv/bin/python examples/ray_probe/python/hostsim.py --plugin-dir examples/ray_probe/go
```

The driver runs 22 checks. The offline tests (`go test ./...`) cover sampling
factors including anisotropic non-powers of two, crop edges, supercover
traversal and dense sampling, both maximum tie rules, NaNs and infinities,
refinement blocks, ellipsoid control/arc geometry, PNG generation, and
manifest/icon parsing. A closed-cube slab test additionally checks membership,
uniqueness and first-contact order across segment/clip cases, including
crossing drift, corner endpoints, reverse directions and crop boundaries.

Last lines of a passing driver run:

```text
ok    re-arranged with the key trigger refused: handle 2 with the key refused and clear's key withheld: a pick still lands
ok    profile panel re-pushed on open: PNG and 'Pick 4: (18, 22, 42) = (36, 11, 21) um, value 2901, c=0, t=2 (2.5 s)' re-pushed
ok    no violations: no ContractViolation beyond the two undeclared-level records
ok    teardown: ShutdownReport COMPLETED, then a clean end of stream; 2 violation report(s) in all, none unexpected

host emulation passed
```

The driver verifies the protocol and geometry, not rendering in vizi. The host
draws line sets projected into 2D slices, dashed outside their own slice; text
is 3D-only. The tiny embedded plot font supports this example's labels and
units; arbitrary Unicode unit names are not fully rendered. A singleton axis
keeps its declared sampling factor at its last fold; the offline tests cover
this with a 3 × 1 × 32 dataset, and the driver's dataset has no singleton
axis.

## Implementation

`geometry.go` implements the level maps, clipping, supercover DDA and blocks.
`search.go` owns read validation, maxima, refinement and publication.
`tool.go` handles gesture events and aim updates. `app.go` serializes picks,
actions and panel commands through one worker; input handlers never wait on a
volume request. `plot.go` and `font.go` produce a 480 × 240 PNG using Go's
standard library. `roi.go` builds timeless equal-radius ellipsoids.

## Exact manifest

This is [vizi-plugin.json](vizi-plugin.json), including its built entry point:

```json
{
  "manifest_version": 1,
  "id": "org.example.ray_probe",
  "name": "Ray probe",
  "version": "0.1.0",
  "engines": {
    "vizi": "1.0"
  },
  "entry": {
    "command": [
      "./ray-probe"
    ]
  },
  "permissions": [
    "data_access",
    "contribute_rois"
  ],
  "contributions": {
    "tools": [
      {
        "id": "probe.point",
        "menu": "tools",
        "name": "Probe",
        "views": [
          "2d",
          "3d"
        ],
        "mode": "gesture",
        "cancel_on_camera_move": false,
        "triggers": [
          {
            "key": "Shift+P",
            "hold": "toggle"
          },
          {
            "button": "middle"
          }
        ],
        "actions": [
          {
            "id": "undo",
            "name": "Undo pick",
            "key": "U",
            "pane": {
              "icon": "icons/undo.svg",
              "tooltip": "Undo the last pick"
            }
          },
          {
            "id": "clear",
            "name": "Clear picks",
            "key": "X",
            "pane": {
              "icon": "icons/clear.svg",
              "tooltip": "Clear every pick"
            }
          }
        ],
        "pane": {
          "icon": "icons/probe.svg",
          "tooltip": "Probe: the brightest voxel under the pointer",
          "hold": "toggle"
        }
      }
    ],
    "overlays": [
      {
        "id": "probe.picks",
        "name": "Picks",
        "segment_mib": 4
      },
      {
        "id": "probe.aim",
        "name": "Aim",
        "segment_mib": 4
      }
    ],
    "commands": [
      {
        "id": "probe.set_radius",
        "title": "Set ROI radius",
        "menu": null
      },
      {
        "id": "probe.set_refine",
        "title": "Refine at level 0",
        "menu": null
      },
      {
        "id": "probe.undo",
        "title": "Undo pick",
        "menu": null
      },
      {
        "id": "probe.clear",
        "title": "Clear picks",
        "menu": null
      },
      {
        "id": "probe.contribute",
        "title": "Contribute picks as ROIs",
        "menu": "tools"
      }
    ],
    "panels": [
      {
        "id": "probe",
        "title": "Ray probe",
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            {
              "kind": "label",
              "id": "status",
              "text": "Ready"
            },
            {
              "kind": "row",
              "id": "rradius",
              "children": [
                {
                  "kind": "label",
                  "id": "lradius",
                  "text": "ROI radius (voxels)"
                },
                {
                  "kind": "line_edit",
                  "id": "radius",
                  "command": "probe.set_radius",
                  "value": "3"
                }
              ]
            },
            {
              "kind": "checkbox",
              "id": "refine",
              "command": "probe.set_refine",
              "text": "Refine at level 0",
              "value": true
            },
            {
              "kind": "row",
              "id": "rbuttons",
              "children": [
                {
                  "kind": "button",
                  "id": "undo",
                  "command": "probe.undo",
                  "text": "Undo"
                },
                {
                  "kind": "button",
                  "id": "clear",
                  "command": "probe.clear",
                  "text": "Clear"
                },
                {
                  "kind": "button",
                  "id": "contribute",
                  "command": "probe.contribute",
                  "text": "Contribute"
                }
              ]
            },
            {
              "kind": "label",
              "id": "count",
              "text": "No picks"
            }
          ]
        }
      },
      {
        "id": "profile",
        "title": "Ray profile",
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            {
              "kind": "image",
              "id": "plot"
            },
            {
              "kind": "label",
              "id": "pick",
              "text": "No pick yet"
            }
          ]
        }
      }
    ]
  }
}
```
