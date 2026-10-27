# Debugging

## Where your output goes

vizi continuously drains your plugin's **stdout and stderr**. Redacted output
is copied to the host log and the latest combined 64 KiB tail is retained in
memory with any unexpected failure. This avoids both a verbose child blocking
on a full pipe and a traceback disappearing when the process is reaped.
Host-log records preserve line boundaries; a line longer than 8 KiB is visibly
shortened with ` …[truncated]`. The rotating per-plugin log file retains the
complete redacted output.

**Which two builds spoke** is the first thing in that log: at launch the host
writes a `handshake` record carrying both ends' identity strings — its own
`Hello from vizi <version>` and your plugin's `Ready; I am <id> <version>;
vizi-plugin-sdk <version>`. **A bare `serve()` gets all of it free**: the
id and the version come from `vizi-plugin.json` in the working directory —
where the host always launches you — and the SDK names its own version
because it is what carries the conversation. A plugin launched somewhere
without a manifest says `Ready; I am plugin; …`, which is valid and tells
you nothing; `serve(plugin_id=…)` names it then.
`serve(version=...)` is for a manifest that lives elsewhere; `serve(build=...)`
is for what only your build step knows — a release hash, a CI build number —
and it is the only way a revision gets into the string. The SDK deliberately
never guesses one from the filesystem: the nearest `.git` may be another
project's, and `HEAD` is not what an edited working tree is running.
Your code can read the host's as `ctx.host.host_identity`
once the session is up, and its own as `ctx.host.plugin_identity`. Neither end
parses the other's string; a malformed one (wrong prefix, empty, over 2048
bytes, or carrying a control character) fails the session at the handshake
with a diagnostic naming both.

The Plugin Manager (Tools → Plugin Manager…) shows live status and retains the
latest crash/failure status after process cleanup. Choose **Diagnostics…** to
view or copy the launch stage, exit code/Unix signal, transport error, detail,
and captured output. The record clears when the next launch attempt starts;
an expected shutdown does not create a crash record.

Exact values from secret-looking environment variables (tokens, passwords,
credentials, private/API keys) are removed before output reaches either the
host log or diagnostics. Do not treat this as a license to print secrets:
redaction is a safety net, not a general log-scrubber.

## The plugin can't run standalone (by design)

`serve()` reads transport endpoints from environment variables only the host
sets, so running your script by hand fails when those variables are absent.
That error is **expected** outside the host and its exact name varies by
platform. What you *can* do standalone is
catch import-time problems — from your plugin's venv:

```bash
.venv/bin/python -c "import my_plugin"
```

If that prints a traceback, fix it before involving vizi at all. Reaching the
missing-host-environment failure when running the script directly means the
plugin side got through imports, decorators, and registration.

## The plugin is listed as *not loadable*, or never appears

No launch was attempted, so there is no Diagnostics record and no plugin
output to read: the host rejected the plugin when it **scanned** the plugins
directory (`~/.vizi/plugins` on every OS, or `VIZI_PLUGINS_DIR`). Plugin
Manager lists it as **not loadable, with the reason**, and the host's log
carries the same diagnostic naming the manifest and the field. A directory
with no `vizi-plugin.json` at its top is not a plugin and is not listed at
all. The usual causes:

- **`engines.vizi` is not the host's major**, or asks for a newer minor than
  the host speaks (`spec/manifest_schema.md` §2.1).
- the manifest does not parse, or fails a schema rule — the warning quotes
  the rule;
- the directory is a broken symlink, or has no `vizi-plugin.json` at its top.

After fixing the manifest, **Rescan** in the Plugin Manager; editing it does
not trigger a scan on its own.

## Common failures, by error string

**Diagnostics say `stage: startup` / exited before readiness** — the process
died before publishing its control endpoint. Almost always one of:

- `entry.command` points at the wrong interpreter (typo'd venv path);
- the venv lacks the SDK or its deps — the SDK then says:
  `the vizi plugin SDK needs its plugin-side dependencies; install grpcio
  and protobuf in the plugin environment (pip install grpcio protobuf)`;
- your script raised at import time. Run the standalone import check above.

**Diagnostics say the plugin did not bind its endpoint within `<N>s`** — the
process is alive but never started serving: typically `serve()` was never
reached (for example, a wrong `__name__` guard) or the process is stuck in
module-level work.

**`open failed: …`** / **`read failed: …`** (viewer load error) — your
reader raised; the message wraps your exception. A shape/dtype mismatch
surfaces here too: the returned array must be `(Z, Y, X)` and exactly the
dtype `open()` declared (`u8`/`u16`/`f32` — anything else fails the dtype
mapping).

**`no command <command id>`** — the manifest declares a command whose id
doesn't match any `@command(...)` in your code; the command reports this as
its failure (`CommandDone{ok=false}`).

**`command "<command id>" is not one this plugin declares`** — the host
invoked a command your manifest does not declare, answered `Error{NOT_FOUND}`.
Usually a manifest out of step with the code that wrote it: regenerate it.

**`read failed: reader arena is full (<N> bytes free, <N> wanted)`** — a
returned volume does not fit the reader's `G` segment. Usually the shape is
inconsistent with what `open()` declared; the SDK also tells the host by how
much it was short.

**`NoRegion: no segment with purpose 'O' subject '<id>' is installed`** (or
`'P'`) — a push named an overlay or panel the host granted nothing for. In
practice the id is not in the manifest's `overlays`/`panels`, or the
manifest was edited without a Rescan. Undeclared ids are refused, not
borrowed from another contribution's arena.

**`NoRegion: the panel image arena is full (<N> bytes free, <N> wanted)`**
(or `the overlay arena …`) — the pushed bytes are loans and the host has not
released enough of the earlier ones yet. Push less often, push smaller, or
raise the overlay's `segment_mib`. The push failed cleanly; nothing was
overwritten.

**`overlay '<id>' names geometry in segment <N>; a live lane carries inline
geometry only`** and **`live update <N>B exceeds the <N>B payload capacity of
a <N>B slot`** — the drag-rate lane's two rules; see
[tools](contributions/tools.md). Build live items with `inline=True`, keep
them small, and persist the full result on release.

**The session fails at the handshake with `the plugin's identity string …`**
— `serve()` composed an identity the form check refuses (empty version, a
control character, over 2048 bytes). The message names the reason and shows
the string.

**A command silently does nothing** — handler exceptions come back as
`ok=False` with `str(exc)` and are logged, not popped up. Check the host log;
if the process exited, open its retained Plugin Manager diagnostics.

**`HostError` with kind `UNSUPPORTED` naming a missing permission** — add
`"data_access"` (or `"contribute_rois"`) to the manifest permissions and
Rescan.

**`HostError` with kind `UNSUPPORTED` when the active dataset is served by
this plugin's own reader** — the self-read guard, not a timeout: the round
trip would re-enter your blocked reader, so the host refuses immediately.
Read the source your plugin already owns directly. Built-in-reader and
cross-plugin datasets remain readable.

**`AssetUnavailableError`** — the asset id is undeclared or its verified file
is missing. Check the manifest id, then use Plugin Manager's **Download** or
**Import…** action. For a user-specific file that cannot be declared with an
author-known size and digest, use `ctx.host.request_asset()` instead.

**The shutdown hook never ran** — the plugin's log carries a
`ShutdownReport` ahead of the stream close: `completed`, `raised` (with the
exception's text), `overran` (the host's deadline passed first — the hook
budget is `notice.grace_seconds`, 5 s unless the host says otherwise), or
`no_hook`. A hook queued behind nothing else and still overrunning is doing
too much; it may flush and persist, not compute.

## Trust, environment, and devices

Plugins run unconfined under the installing user's trust on Linux, Windows,
and macOS. They inherit the user's filesystem, network, and device access.
The separate process protects the host from an ordinary plugin crash; it is
not a security boundary, so install only plugins you trust.

A plugin's process inherits vizi's environment minus `LD_PRELOAD`,
`LD_LIBRARY_PATH`, `LD_AUDIT`, `DYLD_LIBRARY_PATH`,
`DYLD_INSERT_LIBRARIES`, `DYLD_FRAMEWORK_PATH`, `PYTHONPATH`,
`PYTHONHOME`, `PYTHONSTARTUP`, `PYTHONUSERBASE`, `VIRTUAL_ENV`,
`CONDA_PREFIX`, `CONDA_DEFAULT_ENV`, every `UV_*` except
`UV_PYTHON_INSTALL_MIRROR`, and every `PIP_*`. `SSL_CERT_FILE` is removed as
well when it names no file. vizi sets its own `UV_*` and `PIP_*` cache and
configuration variables, and puts its `uv` first on `PATH`
([environments](environments.md)). Ordinary settings such as `PATH` and
`CUDA_HOME` pass through. **A plugin that finds its dependencies through a
removed variable, an active conda environment included, works in your shell
and fails under vizi**; put them in the plugin's venv instead. The emulator in
`examples/vizi_hostsim/` removes the same variables, so it fails there first.
There is no `"gpu"` permission: the string is accepted with a warning and
changes nothing. A GPU profile must still verify that its framework build, runtime,
and the user's driver are mutually compatible.

## Developing in a checkout

- `VIZI_PLUGINS_DIR=/path/to/my/plugins` points the host's scan somewhere
  other than `~/.vizi/plugins` — keep work-in-progress plugins out of your
  real plugin set.
- A checkout symlinked into the plugin directory is supported. The host
  resolves the source before launch and rejects broken symlinks at discovery.
- After editing plugin code, restart the plugin (Plugin Manager, or just
  close/reopen what was using it — the process stops with its last lease).
  After editing the **manifest**, Rescan.
- Run the repository's bootstrap and doctor before viewer testing; the
  [environment convention](environments.md) makes dependency and device
  failures distinguishable from transport/launch failures.
