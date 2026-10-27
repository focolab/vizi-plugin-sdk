# Peak ROI (Python)

The peak-finder ROI plugin from [`../SPEC.md`](../SPEC.md), written against
`vizi_plugin_sdk`. Its manifest declares protocol **1.0** (vizi v1.0.0 and
later), and the gate is the major: a `1.1` host serves it, a `0.x` or `2.0`
host does not. It runs on the Unix launch path (Linux `SOCK_SEQPACKET` and
macOS datagram doorbells).

## Build and run against the emulator

From the repository root:

```bash
cd examples/peak_roi/python
uv venv --python 3.11
uv pip install --editable ../../..
.venv/bin/python ../../../scripts/check-env.py
.venv/bin/python selftest.py
.venv/bin/python hostsim.py
```

`uv` is not required — `python3.11 -m venv .venv && .venv/bin/pip install
../../..` gets the same result. A shipping
plugin needs the hashed per-platform locks in
[`docs/environments.md`](../../../docs/environments.md); this example
deliberately does not, because its whole dependency set is the SDK.

- **`selftest.py`** — the parts that are pure computation: the kernel, edge
  clamping and tie-break from SPEC.md §3–§4 against independently written
  expectations, a synthetic blob with a known answer, the ellipsoid
  arrangement (4 controls, 18 display positions evaluated through the compile
  operator, 12 quarter arcs with their controls at the ring corners), the
  manifest, and the wire round-trip of the message the plugin actually sends.
- **`hostsim.py`** — the parts that are a protocol. It launches the plugin
  the way the Broker does (the manifest's `entry.command` in the plugin's
  directory, gRPC over a Unix domain socket, an inherited `SOCK_SEQPACKET`
  doorbell, a segment granted over `SCM_RIGHTS`) and plays host: the 1.0
  handshake; **both orders of the two-halves memory grant** —
  `M` before `InstallSegment` for one segment, and for another an install that
  rides the `DataVolume` reply so the descriptor is held unread until its `M`
  lands; the `GrantAck` carrying the proof read back out of the mapping; a
  `PanelOpened` on the control plane; widget-argument
  conventions; an unparseable extent; both timepoint modes; deposit reads at
  advancing offsets with a `ReleaseDeposit` naming the region the plugin
  resolved itself; the busy guard; a check that the plugin reported no
  `ContractViolation` about the emulator's own traffic; and the teardown —
  `Shutdown` with a reason and a deadline, the `ShutdownReport` that rides
  ahead of the close, and a clean end of stream inside the window.

  **It drives any implementation of SPEC.md.** `hostsim.py --plugin-dir
  ../<language>` reads that directory's `vizi-plugin.json`, launches its
  command there (as written; only a missing `.venv/` interpreter is
  substituted), and runs the same scenarios — the behavior is the
  specification's, not this implementation's. A plugin that fails the
  handshake ends the run there, as a host would. Unix only.

  Its **doorbell record encoder is its own**, written from
  `docs/spec/doorbell_protocol.md` and checked at startup against the
  cross-check corpus. Importing the SDK's encoder would
  make the encoder under test grade itself.

  Its segment carries purpose **`V`** — `shared_memory_protocol.md` §3's
  host-written volume data a plugin asked for. §3 lists vizi's five purposes
  normatively; a receiver's recognized set is the set it implements, and this
  SDK implements all five.

## Install

The manifest is declared in the code (`plugin(...)`, the decorators and
`panel(...)`), so the plugin installs itself. From this directory, in any shell
(PowerShell, cmd, bash, zsh); `uv` finds the repository's interpreter on every
platform:

```
uv sync --project ../../..
uv run --project ../../.. python peak_roi.py --install
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
(`.venv/bin/python peak_roi.py`), which the drivers use. The plugin warns at launch if
the file and the code ever disagree. CPython `exec`s nothing and re-spawns nothing, so
the inherited doorbell descriptor reaches the process that serves the plugin.

Two things in the declared manifest are load-bearing:

- **`menu=None` on every command.** Without it each command gets its own
  Tools-menu entry, and four of the five are broken by construction: a bare
  menu invocation sends no arguments, so `peak.set_z` from the menu can only
  fail. The panel's own "Show Peak ROI" entry is the front door.
- **A label beside each extent field.** `line_edit` takes a `placeholder`, but
  it disappears as soon as the field has a value, and these ship with
  defaults. Three bare boxes reading `4`, `8`, `8` say nothing about which
  axis is which.

## What using it looks like

Open **Tools → Show Peak ROI**, type a per-axis extent in voxels, pick a
timepoint mode, press **Find peak**. The plugin reads the active volume at
level 0, blurs it with a Gaussian whose σ is half the extent, finds the
brightest voxel, and contributes an axis-aligned ellipsoidal ROI centered on
that voxel's center with semi-axes equal to the extents.

Extents are **voxels, not microns**: the protocol exposes a physical voxel
spacing only to sessions that declare a tool (`EpochChange`'s `Geometry`),
and `ViewState` has none, so on anisotropic data entering `(2, 10, 10)` for a
roughly isotropic physical blob is a visible choice rather than a silent
wrong guess.

The status label is where the run reports. The host shows a
successful command's `message` too, so `peak.run` tells the status bar the
job was accepted; the outcome still arrives after that reply and can only
reach a user who has the panel open.
