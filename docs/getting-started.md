# Getting started — your first plugin

> Protocol `1.0`. You need vizi v0.9.0 or later, or the host emulator described
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
never keep the id or the version in two places. `plugin_id=` still overrides
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
        return                      # the user cancelled
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

## 6. Without a `1.0` vizi: the emulator

Until a vizi release ships the `1.0` host, the only host outside vizi's tree
is the emulator in [`examples/vizi_hostsim/`](../examples/vizi_hostsim/README.md).
It launches a plugin exactly as the host would — its manifest's command in
its directory, gRPC on a Unix socket, an inherited doorbell, segments granted
over `SCM_RIGHTS` — and plays host: grants, arrangements, tool input, the
live lane, loans, teardown. Three runs use it, each a few hundred lines of
scenarios against a real plugin process:
[`examples/peak_roi/python/hostsim.py`](../examples/peak_roi/python/hostsim.py)
for the human-rate surface (reads, a panel, ROI contribution),
[`examples/voxel_probe/python/hostsim.py`](../examples/voxel_probe/python/hostsim.py)
for the fast path (a tool, two overlays, a panel image), and
[`examples/format_bridge/python/hostsim.py`](../examples/format_bridge/python/hostsim.py)
for a data source (a reader and an exporter, both calibration paths). Copy
the shape of any to drive your own plugin. Unix only.

## 7. Where to go next

- Add more contribution kinds — [commands](contributions/commands.md),
  [panels](contributions/panels.md), [overlays](contributions/overlays.md),
  [tools](contributions/tools.md), [exporters](contributions/exporters.md).
- Read the [manifest reference](manifest.md) for every field.
- Read [concepts](concepts.md) for the process model, threads and lifecycle.
- Make the environment reproducible before adding heavy dependencies; see
  [reproducible environments](environments.md).
