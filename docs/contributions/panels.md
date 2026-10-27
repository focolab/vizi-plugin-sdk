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

## Tables

A `table` shows rows of typed cells under columns you declare. The rows are
**data, not widgets**: the host draws only the rows the person can see. That
is why a table holds up to **100,000 rows**, where a `list` stops at 1024.
Use a table for results you would otherwise put in a spreadsheet, such as one
row per region with its measurements. Use a `list` when each row needs its own
controls.

```json
{ "kind": "table", "id": "stats", "selectable": true, "multiple": true,
  "command": "stats.pick", "sortable": true, "visible_rows": 12,
  "columns": [ { "id": "name", "header": "Region" },
               { "id": "area", "header": "Area", "type": "number", "precision": 2, "unit": "µm²" },
               { "id": "kept", "header": "Kept", "type": "bool" } ] }
```

Or in code, with the `widgets` constructors:

```python
from vizi_plugin_sdk import widgets

widgets.table("stats", [
    widgets.table_column("name", "Region"),
    widgets.table_column("area", "Area", type="number", precision=2, unit="µm²"),
    widgets.table_column("kept", "Kept", type="bool"),
], command=pick, selectable=True, multiple=True, sortable=True, visible_rows=12)
```

The declaration follows these rules:

- A table has 1 to 32 columns, each with a distinct `id`.
- A column's `type` is `"string"` (the default), `"number"` or `"bool"`.
- Only a number column takes `precision` (0 to 15 decimals shown, default 3)
  and `unit` (shown in its header). Numbers are right-aligned.
- `multiple` needs `selectable`. A selectable table binds `command`, and only
  a selectable table binds one.

The table is read-only: the person reads rows, sorts them and selects them,
and never edits a cell.

### Filling it — `set_table` and `patch_table`

`set_table` states the rows whole, in order, and replaces whatever the table
showed. Each row is a key and its cells. You can give the cells in column
order or by column id. A cell is a `str`, a number, a `bool`, or `None` for a
blank. A missing trailing cell is also blank.

```python
panel = ctx.panel("results")
panel.set_table("stats", [
    ("r1", ["Nucleus", 41.25, True]),            # positional
    ("r2", {"name": "Membrane", "area": 7.5}),   # by column id; "kept" is blank
])
```

`patch_table` changes rows by key without re-sending the rest. Every key in
`delete` goes first. Then each row in `upsert` replaces the row with its key
where it stands, or is appended at the end if the table does not hold that
key. A key that is deleted and upserted in the same call is therefore
appended fresh.

```python
panel.patch_table("stats", delete=["r2"], upsert=[("r1", ["Nucleus", 43.0, True]), ("r3", ["Golgi", 2.0, False])])
```

Keys are non-empty, distinct and at most 128 characters.

### The caps

| Bound | What happens past it |
|---|---|
| 100,000 rows | `set_table` raises `ValueError` before sending. The host refuses, whole, a patch that would take the table past the bound |
| 32 columns | the manifest is rejected; a row with more cells than the table has columns is refused |
| 1 MiB per message | the SDK splits for you: a large `set_table` goes as one statement followed by appending patches, and the host applies them in order |
| 65,536 entries in one repeated field (§9) | the SDK also splits by count, at most 32,768 rows and, separately, at most 32,768 deleted keys per message, so many short rows still fit |

You never see the 1 MiB split, and a `patch_table` sent while a table op is
still queued merges into it until the merged op would pass the bound. The
host does the same with table ops sent while the panel is closed: it holds
and merges them, and shows all of them when the panel opens. You never
re-send. An op
the host does refuse is not applied at all. It comes back as a
`panel_patch_refused` [violation](../debugging.md) whose detail begins
`INVALID_ARGUMENT:` and names the bound.

A cell of a kind the host does not show, such as a list, is shown blank. This
is not a refusal: the rest of the row still shows.

### Selection

With `selectable`, a change the person makes to the selection fires the
table's command with `args["rows"]`, the selected keys. It is always a list,
even with one key, and it is empty when the person clears the selection.
Without `multiple`, the person can select only one row.

```python
@command("stats.pick")
def pick(ctx: CommandContext) -> str:
    keys = ctx.args["rows"]            # e.g. ("r1", "r3")
    ctx.panel("results").set_value("status", f"{len(keys)} selected")
    return "ok"
```

`set_value` sets the selection from your side. It takes one key, `""` for
none, or a list of keys for a `multiple` table. A key the table does not
hold is refused. **`set_value` never echoes**: like every patch, it does not
fire the command, so a command call always means the person did something.
When you replace the rows, the keys that are still there keep their
selection, and you are not told.

### Sorting

With `sortable`, a click on a header sorts the rows on the host's side, with
no round trip. A number column sorts numerically, so 9 comes before 10, and
blank cells go last, whichever direction the sort runs. Your own order, the one `patch_table` appends to, is
unchanged underneath, so a sort never changes which key a patch replaces.

To test a table, run your plugin against the
[host emulator](../../examples/vizi_hostsim/README.md#panels).
`select_table_rows(panel, table, keys)` makes a selection as the person
would, and `table_sorted(panel, table, column)` reads the order after a
header click.

## Plots

**Tables are data; a plot is a PNG, a click and a size.** vizi has no plot
or chart widget. Render a plot as a PNG and push it to an `image` widget. A
click on the image reaches the plugin with its position in the image's own
pixels, which is how a plot answers a click.

The host also tells you how large each `image` widget is drawn: when the
panel is first shown, and again whenever its size or the screen's pixel
ratio changes. Several changes in a row may reach you as the newest alone.
Register a handler per panel:

```python
from vizi_plugin_sdk import WidgetSize, widget_geometry

@widget_geometry("results")
def resized(ctx, size: WidgetSize) -> None:
    w, h = size.pixels                 # round(width_px * dpr), round(height_px * dpr)
    ctx.panel("results").push_image(size.widget_id, render_plot(w, h))
```

An image of exactly `size.pixels` is shown one image pixel per screen pixel,
and any other size is scaled to fit. A plugin without a geometry handler
pushes whatever size it likes, and its image is scaled. Clicks are reported in the
pushed image's own pixels either way. The reported size is the space the
widget is given, never the image's size, so redrawing at it cannot make the
panel grow. An image cell in a `list` is not reported.

### Matching vizi's theme — the appearance push

A plot drawn in fixed colors looks pasted on in one of vizi's themes. The host
tells the plugin its theme and the colors it draws panels with: once as the
session starts, before any panel opens, and again whenever the person switches
between dark and light. `ctx.appearance` holds the newest (`None` before the
first), so a handler that draws can read it without a callback of its own;
`@appearance` is told each change, which is the moment to redraw.

An `Appearance` has `theme` (`"dark"` or `"light"`, and `dark` as a bool) and
six colors, each an `(r, g, b, a)` tuple of 0–255: `window_bg` (behind a
panel), `panel_bg` (inside a field, a list, a table), `text`, `text_disabled`,
`accent` (a selection's highlight) and `border` (a frame, a grid line).
`hex("text")` gives one as `"#rrggbb"`, and `hex_colors` all of them by name.

```python
import io

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vizi_plugin_sdk import Appearance, WidgetSize, appearance, panel_opened, widget_geometry

_PIXELS = {"plot": (640, 480)}          # the last size the host reported
_OPENED = set()                         # panels opened at least once
_LIGHT = {"window_bg": "#efefef", "panel_bg": "#ffffff", "text": "#000000",
          "text_disabled": "#bebebe", "accent": "#308cc6", "border": "#b8b8b8"}

def render_plot(look: "Appearance | None", w: int, h: int) -> bytes:
    c = look.hex_colors if look is not None else _LIGHT
    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100, facecolor=c["window_bg"])
    ax = fig.add_subplot(facecolor=c["panel_bg"])
    ax.plot(_STATE["xs"], _STATE["ys"], color=c["accent"])     # your data
    ax.grid(color=c["border"], linewidth=0.5)
    ax.tick_params(colors=c["text"])
    ax.set_xlabel("t", color=c["text"])
    ax.set_ylabel("mean", color=c["text"])
    for spine in ax.spines.values():
        spine.set_color(c["border"])
    out = io.BytesIO()
    fig.savefig(out, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out.getvalue()

def redraw(ctx) -> None:
    w, h = _PIXELS["plot"]
    ctx.panel("results").push_image("plot", render_plot(ctx.appearance, w, h))

@panel_opened("results")
def opened(ctx) -> None:
    _OPENED.add("results")
    redraw(ctx)                         # ctx.appearance is already set

@widget_geometry("results")
def resized(ctx, size: WidgetSize) -> None:
    _PIXELS[size.widget_id] = size.pixels
    redraw(ctx)

@appearance
def themed(ctx, look: Appearance) -> None:
    if "results" in _OPENED:            # the first push comes before any panel opens
        redraw(ctx)
```

The appearance is about the session, not a panel: there is one handler per
plugin, and it is told before any panel opens, which is why `themed` draws
only into a panel that has opened; `opened` reads `ctx.appearance` for the
first picture. Several switches in a row may
reach it as the newest alone. Nothing vizi draws depends on the plugin reading
the appearance; a plugin that ignores it keeps its own colors.

To test it, run the plugin against the
[host emulator](../../examples/vizi_hostsim/README.md): it sends the
appearance as the plugin becomes ready, and `set_theme("light")` switches the
theme, sending a new appearance only when it differs.

A plugin that needs a richer interactive view runs its own window.

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
