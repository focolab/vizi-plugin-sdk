# vizi plugin developer guide

> **Protocol `1.0`.** This guide is written for plugin protocol `1.0` and the
> SDK in this repository. Compatibility is decided by the protocol **major**: a
> `1.x` host serves a `1.0` plugin, and a change that would break one is a
> `2.0`.
>
> **vizi v1.0.0 and later are protocol `1.0` hosts.** A plugin built on this
> guide runs against those, and against the host emulator in
> [`examples/vizi_hostsim/`](../examples/vizi_hostsim/README.md) with no vizi
> installed. The normative documents are in [`spec/`](spec/) and win over
> anything written here.

Plugins are **separate processes**: your code runs in its own interpreter and
venv, speaks a versioned protocol to the vizi host, and never imports vizi.
The SDK hides the transport; you declare what you contribute in a JSON
manifest and implement it with decorators.

Plugins run unconfined on all three supported platforms (Linux, Windows,
macOS), under the installing user's trust. The process boundary provides
crash isolation, not security containment.

## Contents

1. [Getting started](getting-started.md) — your first plugin, end to end
   (a file reader, working in the viewer in under 15 minutes).
2. [Manifest reference](manifest.md) — every `vizi-plugin.json` field, and
   [declaring it in code](manifest.md#declaring-the-manifest-in-code)
   (`plugin(...)`, `--manifest`, `--install`).
3. Contribution kinds — what a plugin can add, one page each:
   [readers](contributions/readers.md) ·
   [exporters](contributions/exporters.md) ·
   [commands](contributions/commands.md) ·
   [panels](contributions/panels.md) ·
   [overlays](contributions/overlays.md) ·
   [tools](contributions/tools.md) ·
   [stages](contributions/stages.md) (processing stages)
4. [Concepts](concepts.md) — the process model, the three channels, which
   thread runs what, coordinates, lifecycle, and version compatibility.
5. [Reproducible environments](environments.md) — Python version, local venv,
   CPU/GPU lock profiles, immutable assets, bootstrap/doctor, and CI.
6. [Debugging](debugging.md) — where output goes, common failures, and what
   the error strings mean.
7. [Releasing](releasing.md) — what a release must be for vizi to install it
   from the catalog.
8. [API index](reference.md) — every public name in `vizi_plugin_sdk`, with
   a one-line summary.

## What a plugin author should know first

A reader is `open` + `read_volume`, a command is a decorated function, and a
panel is a manifest tree patched from a handler. A few things underneath show
through:

- **`serve()` reads the manifest.** The handshake carries an identity
  string (`Ready; I am <id> <version>; vizi-plugin-sdk <version>`) that the
  host writes to your plugin's log, and the SDK composes it
  from `vizi-plugin.json` in the working directory — where the host always
  launches you — so a bare `serve()` is enough; `plugin_id=` overrides.
- **A shutdown hook runs.** `@on_shutdown` receives a `ShutdownNotice` with a
  reason and a deadline.
- **Tools receive `InputEvent`s**: the position's shape depends on the pane
  (`Voxel`, `Column`, `Ray`, `RayAndPicked`), a gesture has an interaction id,
  and `cancel` can arrive without a record. A tool can also **request its
  binding** in the manifest (`manifest_version: 1`).
- **Pushed bytes are loans.** A `push_image` or `set_items` reserves from an
  arena the host granted for that panel or overlay and the bytes stay lent
  until the host releases them; an arena with no room fails the push with
  `NoRegion` instead of silently overwriting what the host may still be
  reading. Two overlays are two arenas, two panels two arenas.
- **`set_items_live(items, pane=ev)`** stamps the pane and epoch a gesture's
  geometry was computed under, so a stale value is dropped rather than drawn
  in the wrong place.
- **A success message shows.** A command handler that returns a string has it
  shown to the user.

## Known limitations

Things a plugin cannot do with protocol 1.0, so you do not spend time looking
for them:

- **A reader opens images only.** A file of points, tracks or labels cannot be
  opened through File → Open as anything but an image. Read such a file from a
  command instead, and draw it as an [overlay](contributions/overlays.md) or
  contribute it as regions.
- **The file picker copies.** `ctx.host.request_asset()` copies the chosen
  file into the plugin's asset store; there is no picker that returns a path
  to a file left where it is, and no directory chooser.
- **How a 2D pane draws a point marker away from its own slice is not
  specified.** Lines are drawn on every slice, dashed off their own; for a
  marker, test the appearance you need against the host.
- **The emulator runs on Linux and macOS only.** On Windows, develop against
  vizi itself, or run the emulator under WSL.

The wire itself is documented: [`proto/vizi_plugin.proto`](../proto/vizi_plugin.proto)
is the control plane's IDL and [`spec/`](spec/) the three channels' contracts,
for anyone going below the SDK or writing one in another language
([`polyglot/`](../polyglot/) has Go, Rust and Java).
