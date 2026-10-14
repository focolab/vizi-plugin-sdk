# Stage chain in Go

Four declared stages implement [SPEC.md](../SPEC.md): gain, absolute
difference, float32 widening/cropping, and drift pose estimation. The SDK owns
planning validation, arm revisions, shared-memory grants, turn ownership and
handoff.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/stage_chain/go
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
  your home directory. The plugin lives in the `org.example.stage_chain` subdirectory.
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
CGO_ENABLED=0 go build -o build/stage-chain .
./build/stage-chain --install
./build/stage-chain --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/stage-chain.exe .
.\build\stage-chain.exe --install
.\build\stage-chain.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/stage-chain.exe .
build\stage-chain.exe --install
build\stage-chain.exe --manifest=manifest-preview
```

Prefer `--install` for a movable, self-contained package: its launch entry
points directly to a copied native executable under `bin`, with `.exe` on
Windows. The SDK publishes the manifest last and gives each binary a
content-addressed path, so updates do not overwrite a running Windows
executable. Old binaries remain; stop their sessions before removing unused
`bin` entries. Installation does not restart sessions. Rescan in vizi's Plugin
Manager, and install only one implementation with this plugin ID at a time.

When modifying a stage's output, update the plugin version in the typed
declaration and checked-in manifest before reinstalling. Vizi reuses saved
filter output when plugin ID, version, stage and parameters match; rebuilding
alone does not invalidate it. Settings outside `params`, such as a model chosen
in a panel, are invisible to that comparison and also require a version change
when they change the output.

The typed declaration registers the interface and handlers together. An offline
test compares its generated manifest with [vizi-plugin.json](vizi-plugin.json)
outside `entry`; that file keeps the relative development launch line for the
Unix drivers. Installation uses the generated native launch entry.

## Build and launch

Requires Go 1.25+ and this checkout's directory layout. From the repository root:

```sh
cd examples/stage_chain/go
go build -o stage-chain .
go test -race ./...
go vet ./...
```

The checked-in manifest uses `entry.command: ["sh", "./launch.sh"]`.
The launcher works from a fresh source checkout: it runs `go build` and then
**execs** `./stage-chain`, preserving the inherited doorbell descriptor and
making the host's PID the serving process. It requires Go on PATH, a writable
source directory and available dependencies. Prebuild as above before launching
under a host so initial toolchain/dependency work is outside its 10-second
launch-readiness deadline (§9). Each launch checks the build again; it never trusts a stale
binary. The shell launcher and shared driver are Unix-only.

The module replaces the SDK with `../../../polyglot/go-sdk`. Dependencies
are pinned by go.mod/go.sum.

The offline test compares that declaration with the checked-in manifest and
runs the SDK's validator. It also checks rounding, channel identity, extent
boundaries, crop placement, causal resets and separate arm state.

## Run against the emulator

With the repository's prepared Python `.venv`, from the repository root:

```sh
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go --only planning
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/python --other-dir examples/stage_chain/go
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go --other-dir examples/stage_chain/python
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go --other-dir examples/stage_chain/rust
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust --other-dir examples/stage_chain/go
```

`--plugin-dir` launches session A and `--other-dir` session B (default: the
same directory, as a second process), so the lines above chain this
implementation to itself, to the Python one and to the Rust one, in both
orders. A pairing needs the other implementation's launch prerequisites. A
full run ends:

```text
ok    a session that ends while it holds a turn loses it: B killed holding position 2: the host stored -2 and discarded the volume; A alone carried on
ok    the chain outgrows its ring: a larger one supersedes it: ring 0x300000001 superseded by 0x300000002 in both sessions, both released the old; every position re-armed

all passed
```

The planning-only run ends:

```text
ok    planning: four plans and nine refusals, in both sessions: both sessions: four plans as SPEC states them, nine refusals of the right kind

all passed
```

## Manifest

The complete generated [vizi-plugin.json](vizi-plugin.json):

```json
{
  "manifest_version": 1,
  "id": "org.example.stage_chain",
  "name": "Stage Chain",
  "version": "0.1.0",
  "engines": {
    "vizi": "1.0"
  },
  "entry": {
    "command": [
      "sh",
      "./launch.sh"
    ]
  },
  "contributions": {
    "stages": [
      {
        "id": "gain",
        "name": "Gain",
        "params": [
          {
            "key": "factor",
            "name": "Factor",
            "type": "float",
            "value": 1,
            "min": 0,
            "max": 16
          },
          {
            "key": "channel",
            "name": "Channel",
            "type": "int",
            "value": 0,
            "min": 0,
            "max": 65535
          },
          {
            "key": "stall_ms",
            "name": "Stall (ms)",
            "type": "int",
            "value": 0,
            "min": 0,
            "max": 60000
          }
        ]
      },
      {
        "id": "diff",
        "name": "Absolute difference",
        "params": [
          {
            "key": "a",
            "type": "int",
            "value": 0,
            "min": 0,
            "max": 65535
          },
          {
            "key": "b",
            "type": "int",
            "value": 1,
            "min": 0,
            "max": 65535
          }
        ]
      },
      {
        "id": "widen",
        "name": "Widen to f32",
        "params": [
          {
            "key": "crop",
            "name": "Crop (voxels, y and x)",
            "type": "int",
            "value": 0,
            "min": 0,
            "max": 1024
          }
        ]
      },
      {
        "id": "drift",
        "name": "Drift (pose only)",
        "params": [
          {
            "key": "channel",
            "type": "int",
            "value": 0,
            "min": 0,
            "max": 65535
          }
        ]
      }
    ]
  }
}
```
