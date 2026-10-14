# tetra_shape — Python

The Python implementation of [`../SPEC.md`](../SPEC.md): four tetrahedra, two the host enacts from their data
and two it asks this plugin to shape.

| File | What it is |
|---|---|
| `tetra_shape.py` | the plugin: the four regions (§2), the volume shaper on `@region_shaper` (§3), `region_type` for the type with no shaper and the `@on_shape_edit_ended` log line (§4) |
| `vizi-plugin.json` | the manifest the code declares, with the relative launch line the driver uses |
| `hostsim.py` | the driver: `SPEC.md` §5's fourteen checks against `examples/vizi_hostsim` |

## Run

From this directory, on Linux or macOS:

```
uv sync --project ../../..
uv run --project ../../.. python hostsim.py
```

`--plugin-dir ../<language>` runs the same checks against another implementation.

## Install

The manifest is declared in the code (`plugin(...)`, `@command`, `@region_shaper` and `region_type`), so the
plugin installs itself. From this directory, in any shell (PowerShell, cmd, bash, zsh):

```
uv sync --project ../../..
uv run --project ../../.. python tetra_shape.py --install
```

The second command installs into `$VIZI_PLUGINS_DIR`, or else `~/.vizi/plugins` (`%USERPROFILE%\.vizi\plugins` on
Windows); `--install=/path/to/plugins` installs elsewhere. It prints `INSTALLED: <id> <version> at <directory>`, then
the `Launch:` command vizi will run; a failure exits nonzero with `INSTALL FAILED`, the cause, and a `To fix:` section
with the exact command. The install is in place: its launch line names this
interpreter and this script by absolute path, so run it again after moving either. `--manifest[=DIR]` writes the
manifest without installing.
