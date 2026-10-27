# Interactive segmentation in Python

An implementation of [`../SPEC.md`](../SPEC.md) on the Python SDK
(`vizi_plugin_sdk`, protocol `1.0`). A lasso tool whose trail rides the
overlay's Pulsed lane, a segmentation on a worker, a label volume pushed as
a loan, a commit into native masks, and a mean-versus-t plot through the
panel arena.

## Run against the emulator

```bash
cd examples/interactive_seg/python
uv venv --python 3.11
uv pip install --editable ../../.. atomicshm   # the SDK, and the atomics the emulator's lane reader needs
.venv/bin/python selftest.py
.venv/bin/python hostsim.py
```

`uv` is not required — `python3.11 -m venv .venv && .venv/bin/pip install
../../.. atomicshm` gets the same result.

- **`selftest.py`** — everything that needs no host, against expectations
  worked by hand: the even-odd selection on convex, non-convex,
  self-intersecting (a bow-tie and a pentagram) and volume-leaving loops;
  the decimation; the search window's margins; the median threshold with
  even and odd counts and with NaN and ±inf; face connectivity in 3D and 2D
  (the corner-touch case), a long thin object; the anchor condition and its
  diagnostic; largest-wins and its tie-break; single-plane mode; the
  minimum size; the clip report; the LUT; the label map's newer-wins and
  255 cap; a 512-point trail's encoded size against the slot; the plot's
  PNG; the manifest against the registry.
- **`hostsim.py`** — the protocol, over a real process. It launches the
  plugin as the Broker would and plays host on a synthetic dataset — a
  bright ellipsoid on planes 3–9 and a bright square on plane 3 only — in
  `SPEC.md`'s order: handshake; two `O` grants in the two orders a grant can
  arrive, a `P` and a `V`; the panel's defaults (`commit` disabled, the
  measure row hidden); the arrangements and the `activated` status; `seg.pick`
  → `ActivateTool`, and the MIP refusal that sends none; a loop around the
  ellipsoid — markers on the lane at `z_g` while dragging, the trail
  retracted on release, the label volume as two loans (bbox-tight `u8`,
  the LUT, an integer origin, `t`, names) whose label 1 is exactly the
  ellipsoid; a loose loop that floods the window and says *clipped*; a
  bare click; a `CANCEL`; level 1 refused; two channels refused; an
  unusable record (nothing taken, nothing said, the next gesture live); the
  anchor rule (the square's footprint drawn six planes off — the refusal
  names both planes); a `Column` gesture refused; Add (label 2); commit (two masks, named and colored,
  the overlay retracted, the dropdown patched) and a re-commit that keeps
  the rows; measure (one mask read, one volume read per timepoint, a PNG
  out of the `P` arena); Grow through Z off; the minimum size; the panel
  re-push; the busy guard; reset; no `ContractViolation`; teardown.

  **It drives any implementation of `SPEC.md`.** `hostsim.py --plugin-dir
  ../<language>` reads that directory's `vizi-plugin.json`, launches its
  command there (as written; only a missing `.venv/` interpreter is
  substituted), and runs the same scenarios — the behavior is the
  specification's, not this implementation's. Unix only. It needs an
  interpreter carrying the Python SDK, `grpcio` and `atomicshm`; nothing
  from another language.

## Layout

| | |
|---|---|
| `interactive_seg.py` | the plugin: `@tool("seg.lasso")` (press/drag/release/cancel, `activated`/`deactivated`), the segmentation worker, the nine commands, `@panel_opened`, the model |
| `seg/lasso.py` | the decimation, the even-odd selection, the search window |
| `seg/grow.py` | the threshold, face-connected components, the anchor and the diagnostic, the choice, the LUT, the label map |
| `seg/plot.py` | the mean-versus-t plot and a minimal PNG encoder |
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
uv run --project ../../.. python interactive_seg.py --install
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
(`.venv/bin/python interactive_seg.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.

`atomicshm`, which the live lane (`set_items_live`) needs, is one of the SDK's
own dependencies, so the venv above has it.

## What using it looks like

Open a dataset. **Tools → Lasso segment** (or the panel's **Select
region…**) engages the tool; the status line says *Lasso active — drag a
loop in a 2D view*. Drag a loop around something bright on the plane you
are looking at: a white marker trail follows the cursor and disappears on
release, then the region grows and paints in color, and the status line
reports the label, its voxel count and the threshold. **Add** accumulates
more labels; **Replace** starts over. **Commit to annotations** turns the
labels into native masks, the overlay disappears, and the measure row
appears; pick a region and **Measure** to plot its mean intensity over
time.

Refusals are all in the status line and change nothing: a projection
(*Turn off Z-MIP…*), a coarser pyramid level (*Zoom in to full
resolution…*), a composite of channels (*Show a single channel…*), a bare
click (*Drag a closed loop around a region.*), a loop drawn planes away
from what it encloses (*Nothing above the threshold on plane N…*), too few
voxels, too many.
