# Movie seg in Python

The implementation of [`../SPEC.md`](../SPEC.md) on the Python SDK
(`vizi_plugin_sdk`, protocol `1.0`). A band segmentation of
every frame of a movie served as a **chunked overlay**: the host names the
`(level, t)` volumes it wants, the plugin reads each frame at that level
and answers with the tight box of `uint8` band labels; a `static` overlay
serves the footprint — foreground at any timepoint — one volume per level;
each overlay's LUT and names travel once in a label table; a threshold
command re-serves whatever the host is looking at; a band can be hidden.

## Run against the emulator

```bash
cd examples/movie_seg/python
uv venv --python 3.11
uv pip install --editable ../../.. atomicshm   # the SDK, and the atomics the emulator's lane reader needs
.venv/bin/python selftest.py
.venv/bin/python hostsim.py
```

`uv` is not required — `python3.11 -m venv .venv && .venv/bin/pip install
../../.. atomicshm` gets the same result.

- **`selftest.py`** — the arithmetic alone (`bands.py`), against hand-worked
  expectations: the band rule at every edge `SPEC.md` §2 names, NaN and the
  infinities, integer input, the tight box (an off-origin box, a full
  volume, an empty one), the footprint's union, both tables, the threshold's
  spelling in a reply.
- **`hostsim.py`** — the protocol, over a real process. It launches the
  plugin exactly as the host does and runs `SPEC.md`'s acceptance list on
  `examples/vizi_hostsim`: two chunk arenas host-sized from a six-frame
  three-level movie; three wants served once in order, segment-backed, on
  the wanted lattice, released; the table; a restated set; eviction when
  `t` moves; `t = 0` present; the static overlay with `t` absent and every
  timepoint read; a threshold change re-serving every want; empty chunks;
  the table re-sent on a visibility change; four refusals; an empty set;
  then a **dataset change** — both arenas re-granted superseding the old —
  and, with the host holding its releases, the **deferred publish** (two
  whole-volume chunks fill the arena, the third waits with no shortfall and
  follows the releases) and a deferred want that leaves and returns;
  two sets back to back; level 1 on the new dataset; the accounting; the
  panel (the threshold as text, a checkbox per band, each widget following
  the state); the teardown. `--plugin-dir <dir>` drives another
  implementation of the same specification.

## Layout

| | |
|---|---|
| `movie_seg.py` | the plugin: two `@chunk_source`s, the tables, the commands, the panel; `serve()` |
| `bands.py` | the arithmetic: the band rule, the footprint's union, the tight box, the tables, the threshold's spelling |
| `selftest.py` | the offline checks |
| `hostsim.py` | the driver — the emulator's scenarios for this example, in `SPEC.md`'s order |
| `vizi-plugin.json` | the manifest, as `SPEC.md` §1 gives it (`.venv/bin/python movie_seg.py`) |

## Install into vizi

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python movie_seg.py --install
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
(`.venv/bin/python movie_seg.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.

## In vizi

Open a movie, then **Tools → Show Movie seg**. Opening the panel is what starts
the plugin: vizi launches a plugin only when something of it is used, and an
overlay is not such a thing. The host then names the volumes the panes show,
and the overlays fill in as they are served. Type a threshold in the box
(intensity units, default 400) and press Enter to re-serve every shown volume;
untick a band to hide it. Closing the panel may end the session, and the
overlays with it.
