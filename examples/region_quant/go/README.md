# Region Quant — Go

The plugin of [SPEC.md](../SPEC.md) on the Go SDK. It measures the
host's ROI inclusion masks, updates a row per region from subscriptions, colors
rows with viridis swatches, answers row/brightest-frame view causes, offers a resumable region tour, and exports an exact integer-rounded CSV through
the host's save dialog.

## Install

Requires Go 1.25 or newer and this repository's directory layout. From the
repository root, in any shell:

```text
cd examples/region_quant/go
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
  your home directory. The plugin lives in the `org.example.region_quant` subdirectory.
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
CGO_ENABLED=0 go build -o build/region-quant .
./build/region-quant --install
./build/region-quant --manifest=manifest-preview
```

Windows PowerShell:

```powershell
$env:CGO_ENABLED="0"
go build -o build/region-quant.exe .
.\build\region-quant.exe --install
.\build\region-quant.exe --manifest=manifest-preview
```

Windows cmd:

```bat
set CGO_ENABLED=0
go build -o build/region-quant.exe .
build\region-quant.exe --install
build\region-quant.exe --manifest=manifest-preview
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

## Build and run

From the repository root, with Go 1.25 or newer:

```sh
cd examples/region_quant/go
go build -o build/region-quant .
go test -race -count=1 ./...
go vet ./...
cd ../../..
.venv/bin/python examples/region_quant/python/hostsim.py --plugin-dir examples/region_quant/go
```

The repository's `.venv` must already contain the Python SDK and emulator
requirements. The driver is Unix-only. The executable `region-quant` is a shell
launcher: it changes to its directory and **execs** `./build/region-quant`.
It does not compile or resolve dependencies at launch. Build once after a fresh
checkout; independent host sessions may share the resulting files.

The manifest test detects drift between the checked-in file and the runtime
declaration. Empty label text is omitted by the SDK's JSON generator, preserving its empty
initial value. The precise manifest appears below.

## Verification

The driver runs the checks of [SPEC.md](../SPEC.md) and ends
`host emulation passed`. To run it against a race-enabled build, and then the
shutdown check:

```sh
cd examples/region_quant/go
go build -race -o build/region-quant .
cd ../../..
GOMAXPROCS=1 .venv/bin/python examples/region_quant/python/hostsim.py --plugin-dir examples/region_quant/go
.venv/bin/python examples/region_quant/python/shutdown_check.py --plugin-dir examples/region_quant/go
```

The shutdown check covers a parked trace read, startup while handlers are
making requests, true idle after traces finish, and control-stream loss. Each
scenario must exit within five seconds, issue no new reads after shutdown, and
produce no error lines. Orderly shutdown must report COMPLETED or NO_HOOK and
exit zero.

Offline tests check half-up rounding (including overflowing scaled intermediates),
empty means, swatch clamping, decoded PNG pixels, name/id order, RFC 4180 quoting,
LF line endings, empty-frame skipping and earliest ties. A virtual-time test
checks cancellation while waiting for the state lock; resume tests check the
display-name/id boundary. Trace tests hold reads explicitly across channel and
region invalidation, reject stale results, and check recovery after NOT_FOUND.
Cross-products use
128-bit integer multiplication; CSV mean rounding uses arbitrary-precision
intermediates. No float rounding decides a cell or best frame.

## Runtime choices and limits

The application serializes state changes, measurement and panel publication with
one cancellation-aware lock. The SDK's receive path remains free to process host
replies while a callback holds that lock. Each update publishes one coherent frame/channel/table;
region changes fetch only changed IDs. Export acknowledges with `AfterReply`,
waits for the dialog outside the lock, and takes a coherent table snapshot when
the person chooses a path. File writing also happens outside the state lock.

The plugin handles U8 and U16 voxels (the driver's dataset is U16) in little-endian
payloads. Each new dataset generation
refreshes T, channel count and dtype; a channel outside the new range resets to
zero along with its picker. F32 rows retain their names and entry-zero swatches,
with blank numbers in the panel and CSV, no measurements or traces, and the
integer-data status until an integer dataset opens again. Owned mask/volume
copies have a 512 MiB per-copy budget. The list shows the first 1024 sorted rows
and reports truncation in status. The plugin puts ten-second caller deadlines on its outgoing
data/view calls. These are application choices; supervised reader reads have
no host deadline (§9).
Row clicks and Brightest allow 900 ms to acquire the model lock, then decline
without changing state on timeout. Brightest reads only a cached trace snapshot. The original command context supplies the cause.
A request attempt is never followed by a different answer if its outcome is
unknown. Save requests use the SDK's separate 30-minute `Config.PickerTimeout`
dead-session backstop and are canceled with the session.

A region that disappears during measurement is removed while the other changes
in the push are committed. Other measure failures retain that row's old numbers,
commit the remaining updates, and set `Measuring failed: <message>`. Every trace
read failure except NOT_FOUND retries after exponential backoff (100 ms up to
5 s), recomputing from frame zero so partial samples are never duplicated.
Success resets the delay; invalidation and shutdown cancel the old work.
Choosing the current channel performs no reads or publication. Cause-zero
commands send no ViewRequest and report `No dataset is open`. CSV quotes only
commas, double quotes and line breaks; leading spaces remain unquoted.

A dedicated worker builds one region's trace at a time, reading all frames on
its channel. Replacing a region or changing the channel cancels and replaces
its trace, restarting at frame zero; deletion drops it. In-flight results are
accepted only for the same trace instance. NOT_FOUND abandons that trace while
the worker continues with other rows. The `tracing` label reports completed
frames and clears when the queue is idle. The worker incrementally caches the
best nonempty frame with exact mean comparison and earliest ties, so Brightest
only reads a constant-size snapshot. An incomplete trace produces a decline and
`Measuring… k of T frames`. Each cached sample uses two uint64 values; total
trace storage grows with frame count and region count.

The panel declares `follow_targets: ["frame_region"]`. Every table refresh
updates the offer to `Tour <n> regions` (singular for one, withdrawn at zero).
`Definition.OnFollow` runs the tour on an SDK worker; each admitted frame_region
state is followed by a cancellable 0.5 s dwell. The application snapshots the
initial order, skips removed rows and computes each status position from the
current rows. Resume uses the display-name/id key of the last row shown. The
state lock is released during every dwell. A host end produces `Tour stopped:
<REASON>`; natural completion says `Tour finished`, then the SDK ends the Follow.

Swatches accompany every row publication, including before PanelOpened. The SDK
holds their bytes until P installs, supporting a relaunch where PanelOpened
never arrives. The SDK publishes PNGs through the panel's P arena and owns their
release lifecycle. It copies and releases every host data
deposit before the arithmetic consumes the owned arrays.

The plugin always asks the host for inclusion; it implements no box rasterizer.

## Manifest

```json
{
  "manifest_version": 1,
  "id": "org.example.region_quant",
  "name": "Region Quant",
  "version": "0.1.0",
  "engines": {
    "vizi": "1.0"
  },
  "entry": {
    "command": [
      "./region-quant"
    ]
  },
  "contributions": {
    "commands": [
      {
        "id": "quant.channel",
        "title": "Measure a channel",
        "menu": null
      },
      {
        "id": "quant.pick",
        "title": "Select a region",
        "menu": null
      },
      {
        "id": "quant.brightest",
        "title": "Go to the brightest frame",
        "menu": null
      },
      {
        "id": "quant.export",
        "title": "Export region means",
        "menu": null
      }
    ],
    "panels": [
      {
        "follow_targets": [
          "frame_region"
        ],
        "view_targets": [
          "t",
          "select_region"
        ],
        "id": "quant",
        "title": "Region Quant",
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            {
              "kind": "channel_picker",
              "id": "channel",
              "command": "quant.channel",
              "value": 0
            },
            {
              "moves_view": true,
              "kind": "list",
              "id": "regions",
              "command": "quant.pick",
              "row": [
                {
                  "kind": "label",
                  "id": "name"
                },
                {
                  "kind": "label",
                  "id": "voxels"
                },
                {
                  "kind": "label",
                  "id": "mean"
                },
                {
                  "kind": "image",
                  "id": "swatch"
                }
              ],
              "max_rows": 1024,
              "visible_rows": 8,
              "selectable": true
            },
            {
              "moves_view": true,
              "kind": "button",
              "id": "brightest",
              "text": "Brightest frame",
              "command": "quant.brightest"
            },
            {
              "kind": "button",
              "id": "export",
              "text": "Export CSV…",
              "command": "quant.export"
            },
            {
              "kind": "label",
              "id": "tracing"
            },
            {
              "kind": "label",
              "id": "status",
              "text": "Ready"
            }
          ]
        }
      }
    ]
  },
  "permissions": [
    "data_access"
  ],
  "subscriptions": [
    "view",
    "regions"
  ]
}
```
