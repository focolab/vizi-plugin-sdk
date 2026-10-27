# Region quant — Python

The plugin of [`../SPEC.md`](../SPEC.md) on the Python SDK, and the driver
that checks every implementation of it.

- `region_quant.py`: the plugin. The `view` and `regions` subscriptions
  (`@on_view_changed`, `@on_regions_changed`), a `list` re-stated whole, a
  swatch per row, set-view through `ctx.move_view` /
  `ctx.decline_view`, and the export through `ctx.host.request_save_path` on a
  worker. The PNG is written by hand (`png_rgb`), with no image library.
- `hostsim.py`: the driver, `SPEC.md`'s twenty-one checks (the last two each in a session of its own).
- `shutdown_check.py`: a second driver, for how an implementation shuts down.
  It takes the same `--plugin-dir`.

## Run against the emulator

From the repository root, after `uv sync` (and `uv pip install atomicshm`):

```
.venv/bin/python examples/region_quant/python/hostsim.py
.venv/bin/python examples/region_quant/python/hostsim.py --plugin-dir examples/region_quant/<language>
```

It ends `host emulation passed`, in about fifteen seconds.

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python region_quant.py --install
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
(`.venv/bin/python region_quant.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.
