# Panels

A panel is a side dock of real host-built widgets, declared as a JSON tree in
the manifest — **plugins propose, host disposes**. Your process never runs
UI code; it receives command invocations from the widgets and pushes state
patches back.

## Declaring

The manifest's `panels` entry carries the widget tree — kinds, per-kind
fields, and validation limits are in the
[manifest reference](../manifest.md#panels). Every interactive widget binds
one of your declared commands; opening the panel is a Tools-menu entry the
host adds for you.

Set the optional `menu_label` on a panel to control that Tools-menu entry
verbatim. It must be a non-empty string of at most 128 characters. When
omitted, the host keeps the default `Show <title>` label.

## Pushing state — `PanelHandle`

Inside a [command handler](commands.md), `ctx.panel(panel_id)` returns the
handle:

```python
panel = ctx.panel("hello")
panel.set_value("status", "Done")          # label text / slider pos / ...
panel.set_enabled("go", False)
panel.set_visible("advanced_row", True)
panel.set_options("mode", ["fast", "exact"])
panel.push_image("plot", png_bytes)        # bytes of a PNG, any encoder
panel.set_progress("bar", 0.5)             # a progress widget, below
```

Patches are thread-safe, ride the push channel, and apply last-write-wins
per widget, merged per property, so a `set_options` followed by a
`set_value` on the same dropdown lands in order.

`push_image` moves the PNG bytes through shared memory — the panel's `P`
segment, granted for this panel at session start — and the patch carries
only a reference. **The bytes are a loan**: they stay lent until the host
sends `ReleaseLoan`, and nothing reuses them underneath a host that may still
be reading. An arena with no room for the next image **fails the push** with
`NoRegion` (naming what was free and what was wanted) rather than blocking
or overwriting, and tells the host by how much it was short. Pushing a plot
every command invocation is fine; pushing one per mouse-move is what
[tools](tools.md) and the live overlay lane are for.

## Showing progress — `set_progress`

A `progress` widget is a progress bar the host draws and the user cannot
touch ([`spec/manifest_schema.md`](../spec/manifest_schema.md) §3.3.1):

```json
{ "kind": "progress", "id": "bar" }
```

It binds no command, so moving it never comes back to you as an invocation —
unlike a slider pressed into the same service, which echoes every step you
set. Move it with `set_progress`, not `set_value`:

```python
panel.set_progress("bar", 0.25)                    # a fraction in [0, 1]; the host clamps it
panel.set_progress("bar", indeterminate=True)      # length not known in advance
panel.set_progress("bar", 1.0)                     # done
```

`indeterminate=True` wins over `value` — the value is ignored while the bar
is indeterminate — so pass `indeterminate=False` (the default) with a value
to go back to a determinate bar. A value outside `[0, 1]` is clamped, and a
NaN shows an empty bar. The manifest may give
the widget an initial `value` (0 by default, within `[0, 1]`) and
`indeterminate` (`false` by default). A `set_progress` is a patch like the
others: thread-safe, so a [background worker](commands.md#long-work-belongs-on-a-background-thread)
reports its progress directly.

## Reacting to the panel opening — `@panel_opened`

The host rebuilds a panel from the manifest's declared initial values on
every open and does not persist earlier patches. A plugin that holds state
re-pushes it:

```python
from vizi_plugin_sdk import CommandContext, panel_opened

@panel_opened("analysis")
def on_open(ctx: CommandContext) -> None:
    panel = ctx.panel("analysis")
    panel.set_value("threshold", str(_STATE["threshold"]))
    panel.set_value("status", _STATE["status"])
```

The handler runs on the command worker with empty `ctx.args`, on every open
including a re-shown dock. Unlike a command it has no reply to ride, so
report through the panel.

## Lifecycle notes

- Opening a panel launches the plugin (lazily) and holds a lease — the
  process stays alive while the dock is open.
- Two panels are two `P` segments: each panel's images come from its own
  arena, keyed by the panel id, and a push from a panel the host granted
  nothing for raises `NoRegion` rather than borrowing another's.

## Reference examples

- The minimal loop — button → command → label patch — is inlined in full on
  the [commands page](commands.md).
- [`examples/peak_roi/python/`](../../examples/peak_roi/python/) (this repo)
  — a panel with a mirrored widget model, `@panel_opened`, and a background
  worker reporting through the status label.
