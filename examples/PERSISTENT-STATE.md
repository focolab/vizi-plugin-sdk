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

The paths below are those of a Linux install. The host publishes none of them,
so treat them as observed rather than contracted, and expect them to differ on
macOS and Windows.

## Where it lives

### Keyed by plugin id

The directory name is the lowercase hex **SHA-256 of the plugin id string**:

```
sha256("org.example.format_bridge")  = f612ab809e21f6bde71a4866d68ea0956664b28e71d0d1e4d5d17739065aad34
sha256("org.example.interactive_seg") = cc971520574489c6e76a7cf2a77b34b7806a358239fcf0dd02b99aef5967df76
sha256("org.example.peak_roi")        = 22f539ee506ba1545ee7e0dbc7471813823dcd735bcc28b8e032c510f4973338
```

| Path | Holds |
|---|---|
| `~/.local/share/vizi/plugin-assets/<key>/<asset-id>/` (`<key>` is the first 16 hex of the sha256) | Manifest assets, verified against the declared size and SHA-256, plus a `.<filename>.verified.json` receipt |
| `~/.local/share/vizi/plugin-assets/<key>/.runtime-assets/` | One directory and one JSON record per `RequestAsset` slot — the user's imported file, kept forever |
| `~/.local/share/vizi/plugin-logs/<sha256>/plugin.log` | Host diagnostics and captured plugin stderr |

### Keyed by plugin id *and* panel id

`~/.config/vizi/vizi.conf`, `[mainwindow] state` — a `QMainWindow::saveState()`
blob that includes each plugin dock under the object name
`plugin_panel_<plugin_id>_<panel_id>`, alongside the host's own docks:

```
controls_doc   display_doc   view_2d_doc   view_3d_doc   metadata_doc
annotations_doc
plugin_panel_org.example.interactive_seg_seg
plugin_panel_org.example.format_bridge_crate
```

**Dock position and visibility therefore persist across restarts and across
plugin replacement.** A dock that ends up in an odd state stays in that state
until the layout is re-saved or cleared.

### Not plugin-keyed, but history-dependent anyway

Also in `vizi.conf`: `recentfiles`, `last_open_dir`, and a `[files]` section
carrying per-dataset display state keyed by a dataset hash (channel colormaps,
`vmin`/`vmax`, crop, playback). Plus `~/.cache/vizi/pyramids/` and
`~/.cache/vizi/tiff_index/`. These confuse plugin debugging because they make
"open the same file again" behave differently the second time, which reads as a
plugin bug and is not one.

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
keyed by id again, and so shared. If you are comparing implementations and see behaviour
that is hard to reproduce, depends on which file you open, or changes after a
restart, **suspect the shared state before suspecting the plugin.** Reset
between swaps and re-test.

## Resetting it

Back up first; `vizi.conf` holds a lot of unrelated preferences.

```bash
cp ~/.config/vizi/vizi.conf ~/.config/vizi/vizi.conf.bak

ID=org.example.format_bridge
H=$(printf %s "$ID" | sha256sum | cut -d' ' -f1)
K=$(printf %s "$H" | cut -c1-16)

# assets: manifest-provisioned and runtime slots both (keyed by the first 16 hex)
rm -rf ~/.local/share/vizi/plugin-assets/$K

# host diagnostics for this plugin
rm -rf ~/.local/share/vizi/plugin-logs/$H

# the saved dock layout (affects the host's own docks too, so it is the
# bluntest of these; vizi rebuilds it with defaults on next start)
python3 - <<'EOF'
import pathlib, re
p = pathlib.Path.home() / ".config/vizi/vizi.conf"
p.write_text(re.sub(r"^state=@ByteArray\(.*\)\s*$", "", p.read_text(errors="replace"), flags=re.M))
EOF
```

Removing the asset store means re-provisioning any manifest asset through Plugin
Manager, and any runtime slot re-prompts on next use — which is usually what you
want when testing, and never what you want on a user's machine.

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
