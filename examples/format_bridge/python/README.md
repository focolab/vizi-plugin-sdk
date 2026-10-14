# Crate format bridge (Python)

The crate format bridge from [`../SPEC.md`](../SPEC.md), written on
`vizi_plugin_sdk`: one reader, one exporter, a manifest asset and a runtime
slot, four commands and a panel. Its manifest declares protocol **1.0**; it
runs on the Unix launch path.

## Run the self-test and the emulator

```bash
cd examples/format_bridge/python
uv venv --python 3.11
uv pip install --editable ../../..
.venv/bin/python selftest.py
.venv/bin/python hostsim.py
```

`uv` is not required — `python3.11 -m venv .venv && .venv/bin/pip install
../../..` gets the same result; the whole dependency set is the SDK.

- **`selftest.py`** — everything that needs no host. The format against the
  committed fixtures (the CRC check value, `sample.crate`'s index table
  from `SPEC.md`'s worked example, every voxel against the formula,
  calibration with and without the table, `badcrc`'s one bad block and five
  good ones, `unfinalized`, reserved bytes, zero-length and non-UTF-8
  metadata, a zero axis, a corrupt index); the provenance and names
  sections and the `DERIVED` flag; voxel size (the header wins over the
  document, unit conversions and both spellings of micro, the exporter's
  fields-only rule, malformed header values); the writer against itself and against
  [`../interop/python.crate`](../interop/python.crate) (byte-stable,
  scrambled block order, an incomplete finish and an abort leaving nothing);
  the exporter's big-endian and source-index-rank paths, and what it writes
  of an `ExportDescriptor`'s document, names and record; the comparator's
  tiers and exit codes, in-process and as a subprocess; the manifest.
- **`hostsim.py`** — the protocol, over a real process. It launches the
  plugin as the Broker would and plays host in
  `docs/spec/reader_writer_contract.md`'s order: `OpenReader`, **then** the
  `G` grant sized from the reply and acknowledged before the first read;
  reads, a plane, the document byte for byte, the four bounds refusals;
  `badcrc` with the verify checkbox on and off; the spacing fixtures; the
  calibrated fixture through the manifest asset; a `G` arena too small for
  one volume (the read fails with a `SegmentShortfall`); `CloseReader`,
  idempotent, the file released (checked in `/proc` on Linux), the handle
  `NOT_FOUND` after; the replacing second open, no `CloseReader`;
  `crate.describe` through host data access and through the plugin's own
  reader when the host answers with the self-serve guard's `UNSUPPORTED`;
  the busy guard; `X` granted **before** `OpenExport` with the `M` half
  late, chunks addressed by **source** `t` and channel (one per chunk, two
  in one, one `inline`), the export reopened in the plugin's own reader,
  calibration from the fields and never from the source's document, the
  document, names (a placeholder unwritten) and record kept, one timepoint
  too many, a finish with a hole, an abort; both pickers (chosen, declined,
  malformed); `PanelOpened`; no `ContractViolation`; teardown with the
  shutdown hook closing the file. Then a second, short session with no
  `VIZI_PLUGIN_ASSETS`: the calibrated fixture refuses naming Plugin
  Manager, the plain one serves.

  **It drives any implementation of SPEC.md.** `hostsim.py --plugin-dir
  ../<language>` reads that directory's `vizi-plugin.json`, launches its
  command there (as written; only a missing `.venv/` interpreter is
  substituted), and runs the same scenarios — the behaviour is the
  specification's, not this implementation's. Unix only. It needs an
  interpreter carrying the Python SDK; nothing from another language.

- **`crate_tool.py`** — host-free tools over the format, and the
  comparator `SPEC.md` describes:

  ```bash
  .venv/bin/python crate_tool.py compare A.crate B.crate [--calibration ../fixtures/vizi-crate-cal.bin]
  .venv/bin/python crate_tool.py recreate IN.crate OUT.crate [--order ascending|reverse|scrambled]
  .venv/bin/python crate_tool.py check FILE.crate
  ```

  `compare` names the tier — `byte-equal`, `semantically equal`,
  `voxel-equal`, `different` — with notes, and exits 0 once both crates
  opened, 2 when either would not, 1 on usage. `recreate` is the writer
  without a host; `../interop/python.crate` is its output on
  `../fixtures/sample.crate`.

## Layout

| | |
|---|---|
| `format_bridge.py` | the plugin: `@reader("crate")`, `@exporter("crate")`, the four commands, `@panel_opened`, `@on_shutdown`, the panel model, the two calibration paths |
| `crate/format.py` | the container: header, index, footer, CRC, calibration table, `Crate` (open, read a block or a plane, close), `CrateWriter`, `check_file` |
| `crate/ome.py` | unit conversion and the exporter's calibration rule |
| `crate/compare.py` | the semantic comparator |
| `vizi-plugin.json` | the manifest, `SPEC.md`'s verbatim plus the entry command |

Nothing under `crate/` imports the SDK; `format_bridge.py` is where the
format meets the protocol.

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python format_bridge.py --install
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
(`.venv/bin/python format_bridge.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.

**Provision the calibration asset**: Plugin Manager → *Crate Format Bridge*
→ **Import…** → `examples/format_bridge/fixtures/vizi-crate-cal.bin`. Without
it `sample-calibrated.crate` refuses to open, on purpose, with a message
naming Plugin Manager; every other fixture opens regardless.

## In vizi

**File → Open** a `.crate` — any fixture under `../fixtures/` — and the
panel (**Tools → Crate bridge…**) shows the file, its voxel size as
`2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s` or `(unknown)`, and which
calibration is live. **Describe active dataset** reports min, max and mean
and *which path read them*: `read via host data access` for a natively-read
dataset, `read directly — host data access is unavailable for a dataset this
plugin serves` for a crate. **Export Data → Crate volume container** writes
a crate with the calibration the host handed `OpenExport` (zeros, and a
line saying so, when it handed none). **Check a crate file…** in
the Tools menu picks a file and reports every block's CRC to the panel.
