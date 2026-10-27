# Stage chain in Python

The four stages of [`../SPEC.md`](../SPEC.md) on the Python SDK
(`@stage`, `vizi_plugin_sdk/stages.py`), and the driver every implementation
of this example runs against.

- `stage_chain.py`: the plugin. `vizi-plugin.json` names
  `.venv/bin/python stage_chain.py`; the params are declared twice, in the
  manifest (what the host reads) and in each `@stage(params=...)` (what the
  SDK checks), and must agree.
- `hostsim.py`: the driver: eleven scenarios, each across two plugin
  sessions on one ring, with `vizi_hostsim.chain.Chain` as the host.

## Run against the emulator

From the repository root, after `uv sync` (and `uv pip install atomicshm`):

```
.venv/bin/python examples/stage_chain/python/hostsim.py
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/rust
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/go --other-dir examples/stage_chain/python
```

Without a `.venv` beside the manifest, the emulator substitutes
`VIZI_HOSTSIM_PYTHON` or its own interpreter for `.venv/bin/python` (an
emulator convenience; the host runs `entry.command` as written). The first
run ends:

```
ok    a missed deadline: the slot quarantined, not reused, then back: slot 0 held past 0.4 s, quarantined, slot 1 free meanwhile, slot 0 back once handed on

all passed
```

A run takes about ten seconds. `--plugin-dir` names session A's plugin and
`--other-dir` session B's (default: the same directory, as a second process);
`--only NAME` runs one scenario.

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python stage_chain.py --install
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
(`.venv/bin/python stage_chain.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.
