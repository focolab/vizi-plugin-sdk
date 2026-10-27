# Ray probe in Python

The implementation of [`../SPEC.md`](../SPEC.md) on the Python
SDK (`vizi_plugin_sdk`, protocol `1.0`). A tool that turns a click in the
3D pane into the brightest voxel along the segment the host reports,
refined at level 0; an aim line on the overlay's Pulsed lane while the user
drags; a numbered pick list with markers and labels; undo and clear as
actions fired into an open gesture; a profile plot through a second
panel's arena; the picks handed over as ellipsoid ROIs.

## Build and run against the emulator

```bash
cd examples/ray_probe/python
uv venv --python 3.11
uv pip install --editable ../../.. atomicshm   # the SDK, and the atomics the emulator's lane reader needs
.venv/bin/python selftest.py
.venv/bin/python hostsim.py
```

`uv` is not required — `python3.11 -m venv .venv && .venv/bin/pip install
../../.. atomicshm` gets the same result.

- **`selftest.py`** — everything that needs no host, against expectations
  written independently: `control_plane_protocol.md` §8.11's two level maps
  on its own worked numbers (`{24.5, 1519.5}` at factor 2 is `12 ..= 759`,
  not `13`), the traversal against dense sampling and hand-drawn segments
  (one cell; a face at the start and the end; a segment in a boundary
  plane; a corner-tie diagonal), the crop clip, the column, both tie rules,
  the block of a coarse voxel at factors 2 and 4, the ellipsoid, the PNG,
  the pinned label and count forms, the manifest and its icons. 13 checks.
- **`hostsim.py`** — the protocol, over a real process. It launches the
  plugin exactly as the host does and runs `SPEC.md`'s acceptance list on
  `examples/vizi_hostsim`: a synthetic dataset with a three-level pyramid
  (factors 1, 2, 4, block-mean), an anisotropic spacing in µm and a time
  mapping; two `O` grants, two `P`s by subject and a `V`; the arrangement
  with both triggers and both action keys honored; an axis-aligned ray at level 0 with the
  aim read off the live lane, the pick, the marker and its text label, the
  count, the status, the profile PNG and label (`t=2 (2.5 s)`); the same
  ray at level 2 refined (two reads, ROIs checked against the segment's
  box and one block) and unrefined (one read, the coarse center); an
  oblique ray against the driver's own dense sampling; a `MISS`; a 2D voxel
  and a projection column; `undo` and `clear` as `FIRE` records inside a
  gesture and from the panel; contribute (ellipsoids, `replace_all`, the
  radius, a refused radius); the generation moved under a read; no visible
  channel; an undeclared level (`unusable_context` reported); busy; a
  re-arrangement with the key refused; the profile panel re-pushed;
  violations accounted for; teardown. 22 checks.

  **It drives any implementation of `SPEC.md`.** `hostsim.py --plugin-dir
  ../<language>` reads that directory's `vizi-plugin.json`, launches its
  command there (as written; only a missing `.venv/` interpreter is
  substituted), and runs the same scenarios — the behavior is the
  specification's, not this implementation's. Unix only. It needs an
  interpreter carrying the Python SDK, `grpcio` and `atomicshm`; nothing
  from another language.

Last lines of a passing run:

```
ok    re-arranged with the key trigger refused: handle 2 with the key refused and clear's key withheld: a pick still lands
ok    profile panel re-pushed on open: PNG and 'Pick 4: (18, 22, 42) = (36, 11, 21) um, value 2901, c=0, t=2 (2.5 s)' re-pushed
ok    no violations: no ContractViolation beyond the two undeclared-level records
ok    teardown: ShutdownReport NO_HOOK, then a clean end of stream; 2 violation report(s) in all, none unexpected

host emulation passed
```

## Layout

| | |
|---|---|
| `ray_probe.py` | the plugin: `@tool("probe.point")` (press/drag/release/cancel/action, `activated`/`deactivated`), the pick worker, the five commands, `@panel_opened` for each panel, the model |
| `probe/geometry.py` | the two level maps, the crop clip, the voxel traversal, the column, the block |
| `probe/pick.py` | the first maximum, the block maximum, distances, the plot's sample set |
| `probe/plot.py` | the profile plot and a minimal PNG encoder |
| `probe/ellipsoid.py` | the ellipsoid ROI builder (the same as `../../peak_roi`'s) |
| `icons/` | the tool's and the two actions' pane-button SVGs |
| `selftest.py` | the offline checks |
| `hostsim.py` | the driver, on `examples/vizi_hostsim` |
| `vizi-plugin.json` | the manifest, as `SPEC.md` gives it |

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python ray_probe.py --install
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
(`.venv/bin/python ray_probe.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.

`--install` copies the three pane icons under `icons/` beside the manifest,
where the host looks for them. The manifest is `manifest_version: 1`, the
version that admits triggers, actions and pane buttons.

The driver checks the protocol and the geometry, not rendering: how the
aim line, the markers and the labels look in vizi's panes is seen only in
vizi itself.
