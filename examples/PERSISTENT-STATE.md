# What the host remembers about a plugin between runs

A plugin is not the only thing that carries its state. The host keeps several
stores of its own, **keyed by the plugin's `id` and nothing else**, and none of
them is reset by rebuilding the plugin, renaming its directory, replacing its
implementation, or upgrading it to a new version.

That is usually invisible and occasionally the reason something makes no sense:
the symptoms look, at every step, like a defect in the plugin.

It matters in two situations:

- **Updating a plugin.** A new version inherits the old version's stored state.
  Rename a runtime asset slot, change a panel id, drop a widget — the host still
  holds what the previous version left behind, and the two do not reconcile.
- **Running several implementations of the same plugin**, as the examples
  under this directory are. Within each example every implementation declares
  the **same** `id` — so installing one in place of another inherits every
  byte of the other's history.

## What the host keeps

Keyed by the plugin's `id`:

- **Verified manifest assets**, each checked against its declared size and
  SHA-256.
- **Runtime asset slots**: one per `RequestAsset` slot name, holding the file
  the person imported, kept until the plugin is removed.
- **Host diagnostics** and the plugin's captured output.
- **The person's choice to let the plugin move the view.**

Keyed by the plugin's `id` *and* a panel's id:

- **Dock position and visibility.** They persist across restarts and across
  replacing the plugin, so a dock left in an odd state stays in it.

Not keyed by plugin, but affecting what a plugin sees:

- **Per-dataset display state** (colormaps, contrast, crop, playback), the
  recent-files list and the host's own caches. They make "open the same file
  again" behave differently the second time, which reads as a plugin bug and
  is not one.

**Where the host stores these is not part of the contract** and differs
between platforms and versions. A plugin reaches only what the environment
and the protocol name: `VIZI_PLUGIN_ASSETS`, `VIZI_PLUGIN_SETTINGS_DIR` and a
dataset's `file_state_dir`.

## What this means when you update a plugin

The trap in each case is that the *old* state is still valid as far as the host
is concerned; nothing tells it a new version has different ideas.

- **Renaming a runtime asset slot orphans the old one permanently, and creates a
  second durable import.** A store used by two implementations of the same
  example, each with its own slot name, holds both

  ```
  slot "check_file"        -> sample.crate       (2162 bytes)
  slot "crate_check_file"  -> unfinalized.crate  (1986 bytes)
  ```

  Neither is reachable from the other's code, both survive uninstallation, and
  the user has no obvious way to see that a stale one exists. **Choose slot
  names once.**

- **Changing a panel id changes the dock's object name**, so the saved layout
  entry is orphaned and the new dock lands wherever the host defaults to — while
  the old entry sits in the layout indefinitely.

- **Changing a widget id** is silent in both directions: patches to the removed
  id go nowhere, and the new id has no stored state to inherit.

- **Changing an asset's `filename`, `size` or `sha256`** means the host verifies
  the *new* declaration, but a verified copy of the old one can remain in the
  store.

None of this breaks a first install. All of it can break an upgrade, and the
symptoms are history-dependent, which makes them expensive to diagnose.

## What this means when you swap implementations

Every implementation of one example declares the same `id`, because they are
implementations of the *same plugin*. The consequence is that they are
indistinguishable to every store above.

So a symptom can follow you from one implementation to the next, and swapping
back will not clear it.

**Two implementations cannot be installed side by side under one id**
(`manifest_schema.md` §1). An id is one plugin: of two
directories declaring the same id, the first in directory-name order loads
and the second is listed in Plugin Manager as not loadable, naming the first.
To compare implementations, install one at a time — which is also why the
state above follows you from one to the next.

**The one exception is the plugin's own settings folder** (`settings/`
beside the manifest, `VIZI_PLUGIN_SETTINGS_DIR`). It
lives in the implementation's directory, not in a host store, so two
implementations of one example run from different folders do NOT share
settings, unlike everything above. **But `--install` puts every implementation
of one example in the same `<plugins>/<id>/` directory**, and a reinstall keeps
`settings/`, so installed implementations DO share it. Per-file state
(`file_state_dir`, `plugins/<id>/` in a dataset's companion directory) is
keyed by id again, and so shared. If you are comparing implementations and see behavior
that is hard to reproduce, depends on which file you open, or changes after a
restart, **suspect the shared state before suspecting the plugin.** Reset
between swaps and re-test.

## Resetting it

Removing a plugin in Plugin Manager deletes its folder; reinstalling starts
its settings over. Re-provision a manifest asset from its row in Plugin
Manager. A runtime slot re-prompts when the plugin passes
`force_reprompt=True`. Dock layout and per-dataset display state are reset
through vizi itself, not by the plugin.

## If you are writing a plugin rather than testing one

Three habits follow from all of this:

1. **Treat `id`, panel ids, widget ids and asset slot names as permanent.**
   They are the keys to state you do not own and cannot enumerate. Renaming one
   is not a refactor; it is an orphaning.
2. **Do not assume a slot is empty on first run.** `RequestAsset` has no way to
   ask whether a slot is populated without risking a file dialog, so a plugin
   cannot cleanly distinguish "never set" from "set by a previous version".
   Do not build a design that depends on that distinction.
3. **Keep your own state in the two folders the host gives you**
   (`control_plane_protocol.md` §2.3): settings in `settings_dir()` (named by
   the host, created by the SDK on first use, removed with the plugin's
   folder), and state about
   one dataset in its `file_state_dir` (named by the host, created by you on
   first write, moved and deleted with the file). Unlike the stores above,
   you own these and can enumerate them.
