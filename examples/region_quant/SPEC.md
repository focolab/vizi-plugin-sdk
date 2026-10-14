# Region quant

A panel that lists every region the person has drawn, each with its **mean
intensity and voxel count on one channel at the frame on screen**, kept current
as the person scrubs, draws, redraws and deletes. Clicking a row selects that
region in vizi. **Brightest frame** jumps to the frame where the selected
region is brightest. **Export CSV…** writes the table where the person chooses.
**Follow: Tour N regions** in the panel's first row, when the person presses it,
frames each region in turn. Each row also shows a colour swatch from a vizi
colormap.

The example is implemented in Python, Go, Rust and Java, each on its
language's protocol `1.0` SDK. This document defines the behaviour they share,
and [`python/hostsim.py`](python/hostsim.py) checks it against any of them.
Where this document and `docs/spec/` disagree, `docs/spec/` wins.

## What this example demonstrates

> **State pushed, not polled.** `ViewChanged` and `RegionsChanged` say what
> changed; the plugin re-reads only the regions that changed
> (`GetRois{region_ids}`) and re-measures only what a change invalidates.
> A `reset` voids everything held.
>
> **The host measures the region, the plugin measures the voxels.** A region's
> footprint comes from `ReadRoiInclusion`, never from rasterising its geometry
> here, so the numbers match what vizi draws.
>
> **A list of runtime rows.** One row per region, keyed by region id, each with
> three labels and an image cell. The whole list is re-stated when it changes.
>
> **The person drives the view, through the plugin.** A row click and a button
> are `moves_view` surfaces. Every cause they mint is answered, with a move or a
> decline, and never left to expire.
>
> **The person starts a tour, and can take it back.** The plugin offers a
> Follow; the person starts it; the plugin streams where the view should be;
> the person's hand on the selection ends it, and Resume picks up where it left
> off.

### Host behaviour to know

1. **The panel's image segment exists only once the panel opens** (§8.4). A
   swatch pushed before then has nowhere to go yet, so every SDK holds it and
   sends it when the segment is installed. Push swatches with the rest of each
   row, at any time. A relaunched session whose dock is hidden receives no
   `PanelOpened`, and its swatches still arrive.
2. **A stale cell op is dropped by the SDK** (§8.4). When a region is deleted,
   a coalescer can send the newer `set_rows` ahead of an older, still-pending
   cell op for the removed row, and the host would refuse that op. Every SDK
   drops such ops, so the driver tolerates no refusal.
3. **The switch starts off, as in vizi.** A person turns it on before a row
   click moves anything or Follow can be pressed, and driver check 6 checks the
   refusal first.
4. **The emulator's regions are boxes** with two corner vertices, and its
   rasteriser is its own: every voxel whose centre lies within the corners,
   inclusive. This plugin never needs to know, because it asks the host.

## 1. Manifest

`manifest_version` 1, id **`org.example.region_quant`**, name `Region Quant`,
version `0.1.0`, `engines.vizi` `1.0`, `permissions` `["data_access"]`,
`subscriptions` `["view", "regions"]`, and exactly these contributions:

```json
"commands": [
  { "id": "quant.channel",   "title": "Measure a channel",         "menu": null },
  { "id": "quant.pick",      "title": "Select a region",           "menu": null },
  { "id": "quant.brightest", "title": "Go to the brightest frame", "menu": null },
  { "id": "quant.export",    "title": "Export region means",       "menu": null }
],
"panels": [ {
  "id": "quant", "title": "Region Quant", "view_targets": ["t", "select_region"],
  "follow_targets": ["frame_region"],
  "root": { "kind": "column", "id": "root", "children": [
    { "kind": "channel_picker", "id": "channel", "command": "quant.channel", "value": 0 },
    { "kind": "list", "id": "regions", "command": "quant.pick", "selectable": true, "moves_view": true,
      "max_rows": 1024, "visible_rows": 8,
      "row": [
        { "kind": "label", "id": "name",   "text": "" },
        { "kind": "label", "id": "voxels", "text": "" },
        { "kind": "label", "id": "mean",   "text": "" },
        { "kind": "image", "id": "swatch" } ] },
    { "kind": "button", "id": "brightest", "text": "Brightest frame", "command": "quant.brightest", "moves_view": true },
    { "kind": "button", "id": "export", "text": "Export CSV…", "command": "quant.export" },
    { "kind": "label", "id": "tracing", "text": "" },
    { "kind": "label", "id": "status", "text": "Ready" } ] } } ]
```

## 2. State, from the pushes

The plugin holds: the frame `t`, the channel `c` (initially `0`), vizi's
selected region, and one row per region, each with its name, voxel count and
exact sum.

- **`ViewChanged`**: take `t` and `selected_region_id`. If `t` changed,
  re-measure every row. Then re-state the list (§3).
- **`RegionsChanged`**: on `reset`, drop every row. Drop each `deleted` region's
  row. For every other changed region, read it with
  **`GetRois{region_ids: the changed ids}`** (its `name`), create or replace
  its row, and measure it. Then re-state the list.
- Until the first `ViewChanged` arrives, take `t` from `GetViewState`.
- **A dataset opens** (the first push, or a new `open_generation`): read
  `GetViewState`, and keep its frame count `T` (`shape_tczyx[0]`), its channel
  count and its `dtype`. If `c` is not below the channel count, set `c` to `0`
  and the `channel` picker's value to `0`.
- **Integer data only.** This plugin's arithmetic is exact integer
  arithmetic. On a dataset whose `dtype` is not an integer type (`f32`), measure
  nothing and trace nothing: every row keeps its name, its `voxels` and `mean`
  are empty, its swatch is entry 0; set `status` to **`"Region Quant measures
  integer data; this dataset is f32"`** and keep it until an integer dataset
  opens, which sets it back to `"Ready"`. Brightest declines with the same
  status, and an export writes the rows with `voxels` and `mean` empty.
- **A measure that fails `NOT_FOUND`** means the region is gone (deleted, or
  another dataset opened): drop its row and its trace, and commit the rest of
  the change. Any other failure leaves that row's numbers as they were,
  commits the rest, and sets `status` to **`"Measuring failed: <message>"`**.

**The trace** of a row is its `(count, sum)` at every frame `0 ≤ t < T` on `c`
(`T` is `shape_tczyx[0]` from `GetViewState`, read when the trace starts). It
is what "Brightest frame" answers from (§5), so it is computed **ahead of
time, on a worker of its own**, and never inside a command or a push handler:
- A row's trace is started when the row is created or replaced (a
  `RegionsChanged` entry), and every row's is restarted when `c` changes. A
  trace that is invalidated while in progress is abandoned, and restarted from
  frame 0. A deleted row's trace is dropped.
- **A region can go while its trace runs** (the person deletes it, or another
  dataset opens): a read then fails (`NOT_FOUND`, "unknown region"). Abandon
  that trace; the push that says so drops the row. The worker itself must
  never die of it: the driver deletes a region whose trace may be running.
- Traces are computed one row at a time, frame by frame. While one is running,
  the **`tracing`** label reads **`"Measuring <display name>: <k> of <T>
  frames"`**, with `k` the frames done, updated as each completes. When none
  is running or waiting, it reads `""`.
- A trace is complete when it has all `T` frames on the current `c`.
- **A trace read that fails** with anything but `NOT_FOUND` (a timeout,
  `IO_ERROR`, `INTERNAL`) is retried: the row waits again and its trace
  restarts from frame 0, after a delay that starts at 100 ms and doubles up to
  5 s. A successful trace resets the delay.

**Measuring** a region at `(t, c)`: `ReadRoiInclusion{region, t}` gives a mask
and its origin; a region absent at `t`, or covering no voxel, has count 0 and
sum 0. Otherwise read `ReadDataVolume{t, c}`, clipped to the mask's box or
whole, and take the **count** of the mask's voxels and the **exact integer
sum** of the channel's values over them (64-bit unsigned is enough).

**The model is held briefly.** A command that answers a cause (`quant.pick`,
`quant.brightest`) waits **at most 900 ms** for the rows while a push handler
re-measures, then declines and changes nothing else: the
cause must be answered within its life. Measure outside the lock where your
language allows it, or answer from a snapshot.

## 3. The list

- **At most 1024 rows** (`max_rows`; with four cells a row, the list's
  4096-cell ceiling). Past 1024 regions, list the first 1024 in order and set
  `status` to **`"Showing 1024 of <n> regions"`**.
- **Rows are ordered by display name, then by region id**, both compared by
  code point. The display name is the region's `name`, or its id when the name
  is empty. **Each row's key is its region id.**
- **Re-state the whole list** whenever it changes: `set_rows` with the keys in
  order, then each row's cells, then the list's selection. The cells:
  - `name`: the display name;
  - `voxels`: the count, in decimal;
  - `mean`: the mean in thousandths, **rounded half up in integers**,
    `thousandths = (2000·sum + count) div (2·count)`, printed as
    `"<thousandths div 1000>.<thousandths mod 1000, three digits>"`
    (`"531.250"`). Empty when the count is 0;
  - `swatch`: an 8 × 8 RGB (or RGBA) PNG of one colour, the entry
    **`min(255, (sum div count) >> 8)`** of the colormap **`viridis`** read with
    `GetColormap` (entry 0 when the count is 0), alpha dropped. The PNG's bytes
    are yours; the driver decodes them and checks the pixels. Sent with the
    rest of the row, at any time.
- **The list's selection** (`set_value` on `regions`) is vizi's selected region
  when it has a row, `""` otherwise.

## 4. The channel

`quant.channel` fires with `{"value": <channel index>}`: take it as `c`,
re-measure every row, restart every trace, and re-state the list. **Choosing
the channel already in use changes nothing**: no re-measure, no trace
restarted. A new dataset may reset `c` (§2).

## 5. Set-view: every cause answered

- **`quant.pick`** (a click on a row: `moves_view`, `{"row": <key>}` with a
  cause): if the key has a row, **move the view with `select_region: <key>`**,
  and set `status` to `"Selected <display name>"` when applied, or to
  `"The view did not move: <REASON>"` (the refusal's name, such as
  `NOT_ENABLED`). If the key has no row, **decline**.
- **`quant.brightest`** (`moves_view`, with a cause) **answers at once and
  reads nothing**. Scanning every frame here cannot fit the cause's life on a
  real dataset, which is why the trace exists.
  - With no selected region that has a row: **decline**, and set `status` to
    `"Select a region first"`.
  - While the selected row's trace is incomplete: **decline**, and set `status`
    to **`"Measuring… <k> of <T> frames"`**, with `k` the frames its trace has
    so far (`0` if it has not started).
  - Otherwise, from the trace: the best frame has the highest mean (compare
    `sum_a · count_b > sum_b · count_a`, exact), the earliest on a tie, and
    frames where it covers no voxel are skipped. Move the view with
    `t: <best>`, and set `status` to `"Brightest frame for <display name>:
    t=<best>"` when applied, or the "did not move" text. If no frame has a
    voxel, decline and set `status` to `"<display name> covers no voxel"`.
- **Cause 0** (vizi sends it when no dataset is open): a `moves_view`
  command with cause 0 cannot move the view. Send no view request, and set
  `status` to **`"No dataset is open"`**.
- `<T>` in the texts above is the dataset's frame count from §2, never a
  placeholder: before the first trace starts, `"Measuring… 0 of <T> frames"`.
- **Every cause you receive is answered once**, within the cause's life
  (answer within 1 s; a cause is `STALE` after 2 s). Read the answer from the
  reply, never from a later `ViewChanged`.

## 6. Export

`quant.export` (not `moves_view`): **acknowledge at once**, and on a worker ask
**`RequestSavePath{suggested_name: "regions.csv", extension: "csv", title:
"Export region means"}`**. The person may take minutes, and the command
window is 10 s.
- **Cancelled**: `status` `"Export cancelled"`.
- **Chosen**: write the file as UTF-8 with `\n` line endings and exactly:

  ```
  region_id,name,t,channel,voxels,mean
  <key>,<name>,<t>,<c>,<count>,<mean>
  ```

  one line per row in list order, `<name>` the region's `name` (not the display
  name), `<mean>` as the `mean` cell (empty for a count of 0), and a field holding
  a comma, a double quote or a line break quoted as RFC 4180 does. Then set
  `status` to `"Exported <number of rows> regions to <path>"`, with the path
  the host returned.
- **Refused** (`HostError`), or **the write fails** (the file cannot be
  written): `status` `"Export failed: <message>"`. Never swallow it.

## 7. Follow: the tour

- **The offer** (`OfferFollow`, §8.15) follows the list: whenever the number of
  rows changes, offer **`"Tour <n> regions"`** (`"Tour 1 region"` for one), and
  withdraw the offer when there are none. Offer from the start: an offer sent
  before the panel opens is kept for it.
- **The tour** (the `quant` panel's Follow): take the rows in list order at the
  start. A fresh Follow starts at the first. A **resumed** one starts at the
  first row whose order key (display name, then id) is **after** that of the
  last region this plugin's tours showed. For each row that still exists:
  1. a state with **`frame_region: <key>`**;
  2. `status` **`"Touring <display name> (<i> of <n>)"`**, where `i` is the row's
     1-based position in the list now and `n` the number of rows now;
  3. a dwell of **0.5 s**, ending early if the Follow ends.

  Having shown every row, set `status` to **`"Tour finished"`** and end the
  Follow yourself. The host answers `PLUGIN_ENDED`, and a resumed Follow with
  nothing left to show says the same at once.
- **Ended by the host** (the person's hand, Stop, the switch, and so on): set
  `status` to **`"Tour stopped: <REASON>"`**, where `REASON` is the
  `FollowEndReason` name without its prefix (`USER_TOOK_CONTROL`), and send
  nothing more for that Follow.
- **While a tour runs**, a row click's `select_region` is refused
  `FOLLOW_ACTIVE`, because the tour drives the selection. The row click already
  handles that, with `"The view did not move: FOLLOW_ACTIVE"`.

## Driver checks

The driver runs these in order, on a `(5, 2, 4, 12, 12)` u16 dataset whose
values vary with `t`, `c` and position, with two regions drawn before the
plugin starts (`beta`, then `alpha`), and `t = 1`. Expected values are
computed by the driver from the dataset and the emulator's box rule.

1. **Start:** `view` and `regions` arranged. The first pushes give rows
   `alpha`, `beta` (name order), each measured at `t = 1` on channel 0, each
   with its swatch.
2. **The frame moves** to `t = 3`: every row re-measured.
3. **The channel picker** fires with 1: every row on channel 1.
4. **Regions come and go:** `gamma` added gets a row in order; `beta` redrawn
   (a new revision) is re-measured; `gamma` deleted loses its row.
5. **vizi's selection** of `alpha` is the list's selection.
6. **A row click** on `beta`, first with the person's switch off (as at every
   open): refused `NOT_ENABLED`, `status` `"The view did not move:
   NOT_ENABLED"`. Then with it on: a `ViewRequest{select_region: beta}` under
   that click's cause, `APPLIED`; `status` `"Selected beta"`; the list follows.
7. **Brightest frame** with `beta` selected, once its trace is complete:
   `ViewRequest{t: <best>}`, `APPLIED`; `status` names it; the rows are
   re-measured at the new `t`.
8. **Brightest frame while measuring:** with a slow host (every volume read
   takes 0.5 s, so a synchronous scan of the 5 frames would take 2.5 s and go
   `STALE`), the channel changes. Pressed at once, brightest declines within
   1.5 s with `"Measuring… <k> of 5 frames"`, `k < 5`. When `tracing` reads
   `""`, it answers `APPLIED` within 1.5 s with the best frame on the new
   channel. The channel then goes back to 1.
9. **Brightest frame with nothing selected:** a decline, `DECLINED`;
   `status` `"Select a region first"`.
10. **The offer:** `"Tour 2 regions"`, for the two rows then.
11. **The tour:** Follow pressed; `frame_region` states for `alpha`, then
    `beta`, in list order; `PLUGIN_ENDED`; `status` `"Tour finished"`.
12. **Taken back and resumed:** a second tour; while it runs, a row click is
    refused `FOLLOW_ACTIVE`; the person's selection ends it
    `USER_TOOK_CONTROL`, and `status` is `"Tour stopped: USER_TOOK_CONTROL"`.
    Resume (`resumed: true`) shows only the rows after the last one shown, then
    `PLUGIN_ENDED` and `"Tour finished"`.
13. **Every cause answered.**
14. **Export:** the save dialog, the file byte for byte, and its `status`;
    then a cancel, and `"Export cancelled"`.
15. **Many regions:** 68 more boxes are drawn, 70 in all. Every row is listed, in order, measured.
    Then the 68 are deleted.
16. **A second dataset:** a dataset with **one** channel opens (a new
    `open_generation`) while `c` is 1. `c` resets to 0, the `channel` picker's
    value is set to 0, and every row is measured on channel 0 of the new data.
17. **A float dataset:** an `f32` dataset opens. `status` reads
    `"Region Quant measures integer data; this dataset is f32"`, every row's
    `voxels` and `mean` are empty, and Brightest declines with the same status.
    Then the integer dataset opens again, and the rows are measured.
18. **No dataset open:** the dataset closes, and vizi sends a row click
    and Brightest with cause 0. Each sends no view request, and `status` reads
    `"No dataset is open"`. The dataset opens again, and the rows are measured.
19. **Nothing refused**, no violation, nothing unhandled.
20. **A relaunch, hidden dock** (a second session): vizi
    relaunches a crashed plugin, grants `P` again and, because the dock is
    hidden, sends **no** `PanelOpened`. The rows, measured at the frame on
    screen on channel 0, still reach the panel, **with their swatches**.
21. **A relaunch, visible dock** (a third session): as 20,
    but vizi sends `PanelOpened` right after the grant, without waiting for
    its `GrantAck`, so it can arrive before `P` is installed. The rows and
    swatches arrive all the same.
