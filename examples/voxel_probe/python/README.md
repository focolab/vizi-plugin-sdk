# Voxel probe — the guide's tool sample, run against an emulated host

`docs/contributions/tools.md`'s `VoxelProbe` verbatim — a marker that follows the cursor on the live lane during a gesture
and persists where you release — plus what the sample leaves out and the
emulator wants to drive: a second overlay carrying the gesture's trail as a
segment-backed line on release, a panel whose image widget a command pushes a
PNG into, and a `@panel_opened` re-push. So a run here exercises every push
path the SDK has — the Pulsed lane, a control-plane overlay update out of
each of two `O` arenas, a panel image out of the `P` arena — over a real
process, which nothing under `tests/` does; and it is a check that the
guide's sample still runs.

## Run it

```bash
cd examples/voxel_probe/python
uv venv --python 3.11
uv pip install --editable ../../..    # the SDK, which brings atomicshm for the live lane
.venv/bin/python hostsim.py
```

`atomicshm` is a dependency of the SDK: `set_items_live` is what needs it, and
it installs with the SDK.

`hostsim.py` launches `vizi-plugin.json`'s command in this directory as the
Broker would and runs seventeen scenarios — handshake; two `O` segments and a
`P` granted in two halves and acknowledged with the mapping's proof;
`ToolBinding`, `TimeMapping` and one `EpochChange` per pane; a 2D gesture
whose `PRESS` and `DRAG`s reach the marker lane with the pane pair and whose
`RELEASE` pushes two loans out of two arenas; a `MISS` and a 3D
`RAY_AND_PICKED` gesture the sample ignores; a `CANCEL` that retracts the
marker and pushes no trail; a value read after the pane's epoch moved,
consumed and not applied; a snapshot PNG out of the panel arena; an action
`FIRE` inside an open gesture; a record with an unusable context; no
`ContractViolation`; teardown. The emulator is
[`examples/vizi_hostsim`](../../vizi_hostsim/README.md).

## What is verified, and what is not

The driver checks everything above over real sockets and a real mapping. It
does not check rendering: whether the marker is visible where the user is
drawing, or whether `size_px = 10` is legible. That needs a run in vizi.

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python voxel_probe.py --install
```

- The first command, run once, makes the repository's `.venv`, with the SDK in it.
- The second installs into `$VIZI_PLUGINS_DIR`, or else `~/.vizi/plugins`
  (`%USERPROFILE%\.vizi\plugins` on Windows). To install elsewhere, give the
  directory: `--install=/path/to/plugins`.

It writes `<plugins dir>/<plugin id>/vizi-plugin.json` and prints `INSTALLED: <id> <version> at <directory>`,
then the `Launch:` command vizi will run. A failure exits nonzero with `INSTALL FAILED` (or `MANIFEST FAILED`),
the cause, and a `To fix:` section with the exact command. **The install is in place**:
the manifest's `entry.command` names this interpreter and this script by absolute path,
so after moving either, or rebuilding the venv, run `--install` again. `--manifest[=DIR]`
writes the manifest without installing. If vizi is already running, **Tools → Plugin
Manager… → Rescan**.

`vizi-plugin.json` here is the same manifest with a relative launch line
(`.venv/bin/python voxel_probe.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.
