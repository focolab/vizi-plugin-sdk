# Getting started — your first plugin

> Protocol `1.0`. You need vizi v1.0.0 or later, or the host emulator described
> at the end.

You will build a **reader plugin**: vizi's File → Open gains a new file type,
and opening a matching file streams volumes out of your process into the
viewer. Everything happens on your machine; nothing is submitted anywhere.

Requirements: Python 3.11–3.13 and a vizi that speaks protocol `1.0` (Linux,
Windows, or macOS — the walkthrough uses Linux/macOS shell syntax; on
Windows, `.venv/bin/python` becomes `.venv\Scripts\python.exe`, and vizi
accepts either spelling in the manifest).

## 1. Make the plugin directory and a venv

Plugins live in `~/.vizi/plugins/` — one subdirectory per plugin, holding a
`vizi-plugin.json` manifest next to your code. (`$VIZI_PLUGINS_DIR`
overrides the location; handy for a dev checkout.)

```bash
mkdir -p ~/.vizi/plugins/ramp_reader
cd ~/.vizi/plugins/ramp_reader
python3 -m venv .venv
.venv/bin/pip install git+https://github.com/focolab/vizi-plugin-sdk.git
```

**On Windows**, `python3` is usually the Microsoft Store stub, so make the venv
with the launcher and use its `Scripts` directory:
`py -3 -m venv .venv` then `.venv\Scripts\pip install …`. Keep
`.venv/bin/python` in the manifest regardless — the host translates that
form to `.venv\Scripts\python.exe` (`spec/manifest_schema.md` §2.2).

Your plugin runs in **this venv** — vizi is not installed in it and never
needs to be. Plugins run unconfined under the installing user's trust;
install and run only code you trust.

## 2. Write the reader

`~/.vizi/plugins/ramp_reader/ramp_reader.py`:

```python
import numpy as np
from vizi_plugin_sdk import ReaderInfo, reader, serve

SHAPE_TCZYX = (10, 1, 16, 64, 64)  # 10 timepoints, 1 channel, 16x64x64 voxels


@reader("ramp")
class RampReader:
    def open(self, uri: str) -> ReaderInfo:
        # `uri` is the path the user picked.  A real reader opens and
        # inspects the file here; this demo synthesizes data instead.
        return ReaderInfo(shape_tczyx=SHAPE_TCZYX, dtype="u16")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        _, _, z, y, x = SHAPE_TCZYX
        ramp = np.arange(z * y * x, dtype=np.uint16).reshape(z, y, x)
        return ramp + 500 * t  # brightens over time so scrubbing shows life


if __name__ == "__main__":
    serve(plugin_id="org.example.ramp_reader")
```

That's the whole contract: `open(uri)` returns the dataset's shape (TCZYX
order) and dtype (`"u8"`, `"u16"`, or `"f32"`); `read_volume(t, c)` returns
one `(Z, Y, X)` numpy array of exactly that dtype. The SDK handles the
transport — volumes cross to the host through shared memory, not the
network.

`serve()` names the plugin in the handshake from the manifest in the working
directory — the host always launches you there — so a bare `serve()` announces
`Ready; I am org.example.ramp_reader 0.1.0; vizi-plugin-sdk <version>` and you
never keep the id or the version in two places. `plugin_id=` overrides
it; the examples in this guide pass it only to make the id visible beside the
manifest.

## 3. Write the manifest

`~/.vizi/plugins/ramp_reader/vizi-plugin.json`:

```json
{
  "manifest_version": 1,
  "id": "org.example.ramp_reader",
  "name": "Ramp Reader",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": [".venv/bin/python", "ramp_reader.py"] },
  "contributions": {
    "readers": [
      { "id": "ramp", "name": "Ramp demo", "extensions": [".ramp"] }
    ]
  },
  "permissions": []
}
```

Two things to know about `entry.command`:

- It runs with the plugin directory as its working directory, so the
  relative `.venv/bin/python` and `ramp_reader.py` paths resolve there —
  this keeps the manifest portable between machines, and it is where
  `serve()` finds the manifest for the identity string. An absolute
  interpreter path works too.
- A bare `"python3"` also works **if** that interpreter can
  `import vizi_plugin_sdk` — it resolves against the host's `PATH`, which is
  usually not your venv.

For a real plugin, commit a tested Python range and platform/profile lockfiles
instead of installing an unconstrained environment by hand. The
[reproducible-environment convention](environments.md) gives a fresh-machine
recipe and a bootstrap/doctor contract.

## 4. See it in the viewer

```bash
touch ~/demo.ramp        # the demo reader never reads the bytes
```

1. Start vizi. (If it was already running: **Tools → Plugin Manager… →
   Rescan**.)
2. **Tools → Plugin Manager…** — "Ramp Reader" is listed, status *stopped*.
   Plugins launch lazily; nothing runs until first use.
3. **File → Open** — the file-type filter now includes `*.ramp`. Pick
   `~/demo.ramp`.
4. The volume appears. Scrub the T slider — the brightness ramps. The
   Plugin Manager now shows *running*, and the plugin's log opens with a
   `handshake` record naming the host's build and yours.

If instead you get an error, the [debugging page](debugging.md) covers the
common first-plugin failures (wrong interpreter path, SDK not installed in
the venv, dtype mismatch).

## 5. Let a user choose a runtime file

A plugin can ask vizi to show a native single-file picker. This is useful for
user-specific model weights whose size and hash cannot be declared in the
manifest.

The picker waits on a person. The host does not run the command's 10-second
deadline while its own file dialog is up (`spec/control_plane_protocol.md`
§8.9), but the command worker is serialized: every later command waits behind
a handler that is waiting on a person. So ask for it from a background thread
and let the command return at once:

```python
import threading

from vizi_plugin_sdk import CommandContext, command


def _choose(ctx: CommandContext) -> None:
    # the default 30 s backstop is meant for a dead session, not for browsing
    weights = ctx.host.request_asset("weights", timeout=600)
    if weights is None:
        return                      # the user canceled
    load_model(weights)


@command("analysis.weights")
def pick_weights(ctx: CommandContext) -> None:
    threading.Thread(target=_choose, args=(ctx,), daemon=True).start()
```

That is the same rule as any slow work in a command
([commands](contributions/commands.md#long-work-belongs-on-a-background-thread)),
and a picker is the case where it is easiest to forget, because the plugin is
doing nothing — it is the person who is slow.

Declare `"data_access"` in the manifest. The host copies the selection into a
persistent plugin+slot location, so later calls reuse it without prompting.
An explicit “Choose different weights…” control can call
`request_asset("weights", force_reprompt=True)`. Only single files are
supported; cancellation returns `None`.

## 6. Without vizi: the emulator

Protocol `1.0` hosts are vizi v1.0.0 and later. Without one, the host
emulator in [`examples/vizi_hostsim/`](../examples/vizi_hostsim/README.md)
plays host. It launches a plugin exactly as the host would — its manifest's
command in its directory, gRPC on a Unix socket, an inherited doorbell,
segments granted over `SCM_RIGHTS` — and plays host: grants, arrangements,
tool input, the live lane, loans, teardown. Unix only.

Nine runs use it, each a script of scenarios against a real plugin process.
Every one but `voxel_probe` takes `--plugin-dir ../<language>` to run the
same scenarios against another implementation of its example:

| run | what it drives |
|---|---|
| [`peak_roi/python/hostsim.py`](../examples/peak_roi/python/hostsim.py) | the human-rate surface: reads, a panel, ROI contribution |
| [`voxel_probe/python/hostsim.py`](../examples/voxel_probe/python/hostsim.py) | the fast path: a tool, two overlays, a panel image |
| [`format_bridge/python/hostsim.py`](../examples/format_bridge/python/hostsim.py) | a data source: a reader and an exporter, both calibration paths |
| [`interactive_seg/python/hostsim.py`](../examples/interactive_seg/python/hostsim.py) | a lasso tool, its trail on the live lane, the label volume and ROIs it publishes |
| [`movie_seg/python/hostsim.py`](../examples/movie_seg/python/hostsim.py) | a chunked overlay: the volumes the host wants, served into an arena |
| [`ray_probe/python/hostsim.py`](../examples/ray_probe/python/hostsim.py) | 3D picking: rays through a multi-level dataset, clipped to the crop box |
| [`region_quant/python/hostsim.py`](../examples/region_quant/python/hostsim.py) | view and region subscriptions, a table, set-view, a Follow tour, an export through the save dialog |
| [`stage_chain/python/hostsim.py`](../examples/stage_chain/python/hostsim.py) | processing stages: one chain across two plugin sessions |
| [`tetra_shape/python/hostsim.py`](../examples/tetra_shape/python/hostsim.py) | region types the plugin shapes |

### Drive your own reader

The emulator ships in the SDK's repository, not in the package, so clone it
(`git clone https://github.com/focolab/vizi-plugin-sdk.git`). This driver
plays File → Open against the ramp reader above, in the order the host uses
(`spec/reader_writer_contract.md` §1): open, then grant the `G` arena sized
from the reply, read, close, retire the arena.

`~/.vizi/plugins/ramp_reader/drive.py`:

```python
import sys
from pathlib import Path

import numpy as np

SDK = Path.home() / "vizi-plugin-sdk"          # your clone
PLUGIN = Path(__file__).resolve().parent
sys.path.insert(0, str(SDK / "examples"))      # for vizi_hostsim
from vizi_hostsim import Dataset, Host, Segment, check, failures, segment_id
from vizi_plugin_sdk import pb2

# The host's own open dataset.  A reader never reads it; the emulator needs one.
HOST_DATA = Dataset(shape_tczyx=(1, 1, 1, 1, 1), np_dtype=np.dtype(np.uint16),
                    wire_dtype=pb2.DTYPE_U16, open_generation=1,
                    volume=lambda t, c: np.zeros((1, 1, 1), np.uint16))

host = Host(PLUGIN, HOST_DATA)                 # reads PLUGIN/vizi-plugin.json
with host:                                     # launches its command, handshakes
    check("handshake", host.check_handshake)

    def open_and_read():
        opened = host.open_reader(str(Path.home() / "demo.ramp"), "ramp")
        g = segment_id(10)                     # G is granted AFTER the open,
        host.add(Segment.reader_arena(g, opened, host.directory))  # sized from the reply
        assert host.grant(g).WhichOneof("outcome") == "installed"
        got = host.read_volume(opened.handle, 3, 0)                # t=3, c=0, out of G
        assert got.array.shape == (16, 64, 64) and got.array[0, 0, 0] == 1500
        assert host.close_reader(opened.handle).handle == opened.handle
        host.retire_segment(g)                 # the host retires G after a close
        return f"shape {tuple(opened.shape_tczyx)}; t=3 starts at {got.array[0, 0, 0]}"

    check("open, read, close", open_and_read)
    host.shutdown()
sys.exit(1 if failures or host.unhandled else 0)
```

```bash
cd ~/.vizi/plugins/ramp_reader && .venv/bin/python drive.py
```

Each `check` prints `ok` or the failure; the exit status is non-zero if any
failed or the plugin sent something the emulator did not expect
(`host.unhandled`). A reader error comes back as `PluginError`, with the
reply's `kind`. `read_plane(handle, t, z, c)` and `metadata(handle)` are the
other reads; [`format_bridge/python/hostsim.py`](../examples/format_bridge/python/hostsim.py)
uses all of them, replaces an open reader, and adds an exporter, and the
[emulator's README](../examples/vizi_hostsim/README.md#writing-a-driver-for-your-own-plugin)
covers tools, overlays and panels.

## 7. Where to go next

- Add more contribution kinds — [commands](contributions/commands.md),
  [panels](contributions/panels.md), [overlays](contributions/overlays.md),
  [tools](contributions/tools.md), [exporters](contributions/exporters.md).
- Read the [manifest reference](manifest.md) for every field.
- Read [concepts](concepts.md) for the process model, threads and lifecycle.
- Make the environment reproducible before adding heavy dependencies; see
  [reproducible environments](environments.md).
