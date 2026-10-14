# Movie seg — plugin specification

A dense segmentation of every frame of a movie, served as a **chunked
overlay**: one label volume at a time, at the pyramid level and timepoint the
host asks for. The plugin cuts each frame into three intensity bands above a
threshold and hands the host the tight box of labels; a second, `static`
overlay serves one volume per level saying where the foreground ever was; a
threshold change re-serves whatever the host is looking at; a band can be
hidden. That is the whole plugin.

This document specifies **behaviour**, not implementation. Each
implementation beside it (`python/`, `go/`, `rust/`, `java/`) is written on
its language's SDK under `polyglot/`, and
[`python/hostsim.py`](python/hostsim.py) runs this document's scenarios
against **any** of them. Where this document and `docs/spec/` disagree,
`docs/spec/` wins. The protocol is `1.0`.

## What this example demonstrates

With a chunked overlay **the host names the state it wants**, in a
coalescing host→plugin push with no queue at either end, and the plugin
speaks of one volume at a time under a key the host chose. Four things
follow, and each implementation does them through its SDK:

> **Serve a want set, not a call.** `ChunksWanted` is a *set* restated whole
> whenever it changes. The plugin serves each want once while its membership
> stands, in the host's order, off the dispatch path; forgets one that
> leaves; serves again one that returns. Nothing in the protocol calls the
> plugin per chunk: the plugin runs its own server against a set that moves
> under it.
>
> **Defer, don't fail.** Every other push on a fixed arena fails and counts
> when the arena is full (§5.6). A chunk is solicited, the host will release
> the one before it, so a chunk the arena cannot hold **waits for the next
> `ReleaseLoan` and is retried with the same answer** — no shortfall, no
> drop.
>
> **Speak on the wanted lattice.** `OverlayLabelVolume` is level-0 only; a
> chunk is on **level L's** lattice with `position_zyx` in level-L indices,
> legal because the host chose the level. Nothing converts.
>
> **Follow the dataset.** The overlay's arena is not the manifest's
> `segment_mib`; the host sizes it from the open dataset and **re-grants it
> superseding the old** when the dataset changes.

Two smaller things ride along: an `optional` scalar on a plugin message
(`t` absent is a key of its own, never `t = 0`), and a manifest form
(`chunked`, `time`) that refuses `segment_mib`.

The contract this plugin lives on is
[`control_plane_protocol.md`](../../docs/spec/control_plane_protocol.md)
§5.7, §5.4's key paragraph, §8.5's chunked-overlay paragraph and §8.12's
host-sized grant; `shared_memory_protocol.md` §9 (retirement, and §9.5 for
`supersedes`); and `manifest_schema.md` §3.5.

### Protocol notes

1. **The want set is what the host wants to *hold*, chunks it already holds
   included** (§5.7 rule 2). A plugin serves each want **once per continuous
   membership**: restating the same set serves nothing; a key that leaves
   and returns is served again; an empty set forgets everything; a want is
   served again when the plugin's own results change, and the host accepts
   it because the key is still in the set.
2. **The bookkeeping follows every `ChunksWanted` as it arrives**, not the
   worker's next look at the latest set. The host sends once per change, so
   the plugin sees every set — including the empty one a dataset change
   sends before the new dataset's wants — and a receiver that only ever
   consults the newest set mistakes *left and returned* for *never left*.
   The emulator spaces its ordinary sends one pump tick apart, as the host
   does, and acceptance 15 sends two sets **back to back** to check the
   rule directly.
3. **A superseded segment owes `SegmentReleased`.** `shared_memory_protocol.md`
   §9: `supersedes` *"behaves identically in every respect"* to
   `RetireSegment`: `GrantAck` for the new segment, then `SegmentReleased`
   for the old, and a `RetireSegment` for the old one afterwards is answered
   again. The emulator keeps a fallback — an explicit `RetireSegment` and a
   `supersede:` line in its diagnostics — so an implementation that omits
   the release fails acceptance 12 with a clear message rather than a
   timeout.
4. **An invalidate discards a deferred answer.** A chunk computed before the
   plugin's results changed and still waiting for room is stale; it is
   recomputed, not published. Not driven — the emulator cannot reach the
   interleaving deterministically — but required (§4).
5. **A dataset change empties the set first, and names nothing until the
   new arena is installed** (§5.7 rule 2). Nothing on a `ChunkWant` says
   which dataset it is for, so that empty set is what separates the old
   keys from the new; the driver sends it (§6), and closing the dataset
   sends it too. A cache a plugin keeps is keyed by `open_generation` as
   well as the threshold, level and t.
6. **A static source has to ask how many timepoints there are.** No
   arrangement reaches a session without a tool (`EpochChange`, `TimeMapping`
   are §8.7's), and a `ChunkWant` says nothing about the dataset, so the
   footprint's union over t needs `GetViewState` — `data_access`, which the
   reads need anyway.
7. **How much of the frame to read is the plugin's choice.** The Python
   implementation reads the whole level; an ROI around the previous chunk's
   box is a legitimate optimisation. The driver asserts that a read of
   `(t, level)` happened for each chunk and that every timepoint was read
   for a footprint, not what extent was read.
8. **A chunked overlay never compiles and an `OverlayUpdate` to one is
   refused** (site 30); `OverlayRemove` on one clears its chunks and table.
   This plugin sends none of the three.
9. **The table and the first chunk have no fixed order.** Both are coalescing
   pushes under different keys; the host draws chunks with a default LUT
   until the table arrives. §3 says *no later than the first chunk* in the
   sense of the plugin's own sends; the driver waits for both and checks the
   state, never the order.

## 1. Manifest

`manifest_version: 1`, `engines.vizi = "1.0"`, `data_access`; two chunked
overlays, five commands and one panel, exactly these ids:

```json
{
  "manifest_version": 1,
  "id": "org.example.movie_seg",
  "name": "Movie seg",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["<your launcher>"] },
  "permissions": ["data_access"],
  "contributions": {
    "overlays": [
      { "id": "bands", "name": "Intensity bands", "chunked": true, "time": "per_t" },
      { "id": "footprint", "name": "Footprint", "chunked": true, "time": "static" }
    ],
    "commands": [
      { "id": "movie_seg.threshold",    "title": "Set threshold",        "menu": null },
      { "id": "movie_seg.band_visible", "title": "Show or hide a band",  "menu": null },
      { "id": "movie_seg.band_1",       "title": "Show band 1 (dim)",    "menu": null },
      { "id": "movie_seg.band_2",       "title": "Show band 2 (mid)",    "menu": null },
      { "id": "movie_seg.band_3",       "title": "Show band 3 (bright)", "menu": null }
    ],
    "panels": [
      {
        "id": "movie_seg",
        "title": "Movie seg",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "row", "id": "rt", "children": [
              { "kind": "label", "id": "lt", "text": "Threshold" },
              { "kind": "line_edit", "id": "threshold", "command": "movie_seg.threshold", "value": "400" }
            ] },
            { "kind": "checkbox", "id": "band_1", "command": "movie_seg.band_1", "text": "Band 1 (dim)", "value": true },
            { "kind": "checkbox", "id": "band_2", "command": "movie_seg.band_2", "text": "Band 2 (mid)", "value": true },
            { "kind": "checkbox", "id": "band_3", "command": "movie_seg.band_3", "text": "Band 3 (bright)", "value": true }
          ]
        }
      }
    ]
  }
}
```

Neither overlay declares `segment_mib` — `manifest_schema.md` §3.5 refuses
it on a chunked overlay — and `time` appears only beside `chunked`. There is
no tool; the panel (§5a) is how a person starts the plugin in vizi. The host grants each overlay one `O` segment with
`subject` = its id: the ordinary overlay table (a Pulsed lane, unused here,
then the plugin-produced LOANED arena), the arena **sized by the host from
the open dataset** (one level-0 u16 volume plus one level-1 u16 volume,
each padded to 64 bytes; `Segment.chunk_arena_bytes` in the emulator is the
formula) and granted once a dataset is open — the driver grants it right
after the handshake, since its dataset is open from the start.

## 2. The segmentation

**Parameters.** A threshold `T` (default `400`, in the dataset's own
intensity units), a band width `W = 200`, and `K = 3` bands. `T` is the one
thing a command changes; `W` and `K` are constants.

**The band rule.** For a voxel of value `v`:

```
label(v) = 0                                   if v < T, or v is NaN
         = min(K, 1 + floor((v − T) / W))      otherwise
```

so band 1 is `[T, T + W)`, band 2 is `[T + W, T + 2W)`, band 3 is
`[T + 2W, ∞)`; every edge belongs to the upper band; `−∞` is background and
`+∞` is band `K`. Labels are `uint8`. The comparison is in floating point
whatever the dataset's dtype.

**Which voxels.** Channel `0` of the frame at timepoint `t`, **at the wanted
level `L`**: the plugin reads `(t, c = 0, level = L)` through the data plane
(`ReadDataVolume`), whole or in part (note 7), and labels what it read
on that lattice. Nothing is converted between levels: a chunk at level `L`
is on level `L`'s lattice, its `position_zyx` in level-`L` indices, and its
box inside level `L`'s shape (`ViewState.levels[L].shape_zyx`).

**Bbox-tight.** The chunk is the smallest box containing every nonzero
label — `position_zyx` its origin, `labels` its contents, zeros inside the
box kept. A frame with no foreground is an **empty chunk**: `OverlayChunk`
with `labels` absent and no `position_zyx`, which is a state (nothing here),
not a removal, and reserves nothing.

**The footprint** (`static`). One volume per level, `t` absent on the want
and on the chunk: label `1` wherever the voxel is at or above `T` in **any**
timepoint of the dataset, `0` elsewhere — the union over every `t` of the
foreground mask at level `L`, then bbox-tight as above. The number of
timepoints is `ViewState.shape_tczyx[0]`; every one is read at the wanted
level. A dataset with no timepoints has an empty footprint: an empty chunk,
not a failed want (not driven).

**`t`.** A per_t chunk carries `t` **present** — `t = 0` is present with
value 0. A static chunk carries no `t`. The two are different keys on the
wire (§5.4), and an implementation whose optional-field handling conflates
absent with zero fails acceptance 7 or the `t = 0` check.

## 3. The label tables

Once per overlay per session, **no later than the overlay's first chunk**
(note 9), the plugin sends `OverlayLabelTable` for it, inline:

| overlay | `colormap` (256 × 4 u8, RGBA) | `label_names` | `label_visible` | `style` |
|---|---|---|---|---|
| `bands` | row 0 `(0, 0, 0, 0)`; row 1 `(60, 120, 255, 255)`; row 2 `(255, 200, 0, 255)`; row 3 `(255, 60, 60, 255)`, **a hidden band's row with alpha 0**; rows 4–255 `(0, 0, 0, 0)` | `{1: "dim", 2: "mid", 3: "bright"}` | **every** band, `true` unless hidden (§5) | `opacity = 0.6`, `hidden = false`, `draw_priority = 1` |
| `footprint` | row 0 clear; row 1 `(200, 200, 200, 255)`; the rest clear | `{1: "ever"}` | empty | `opacity = 0.3`, `hidden = false`, `draw_priority = 0` |

The `bands` table is sent **again** after every visibility change (§5) —
coalescing per overlay, latest wins. A threshold change does not re-send a
table: nothing in it changed.

**Hiding a band is its row's alpha.** The host draws a label volume's voxels
straight from the LUT; `label_visible` governs only the compile (whether a
label becomes a visible annotation region, `control_plane_protocol.md` §8.8).
So a hidden band keeps its colour with alpha 0 — row 2 hidden is
`(255, 200, 0, 0)` — and `label_visible` says `false` for it as well.

**`bands` draws above `footprint`.** The footprint encloses every band voxel,
and in 3D each label volume is drawn over the ones before it in
`draw_priority` order, so an enclosing overlay hides what it encloses unless it
ranks lower. Opacity does not rescue it: 0.3 per voxel, through a thick region,
is opaque.

## 4. Serving the wants

**The set.** The host's `ChunksWanted{overlay_id, wants[]}` is the whole set
for that overlay in the host's priority order, restated whenever it changes,
empty when nothing is wanted; at most 8 entries. The plugin keeps the latest
per overlay and applies each one to its bookkeeping as it arrives (note
2).

**Once per membership, in order, off the dispatch path.** The plugin serves
the wants of a set that have not been served since they entered it, in the
host's order, one at a time per overlay, on a thread of its own — never on
the thread that receives host messages, since a chunk costs a read. A want
already served is not served again while it stays in the set; one that
leaves is forgotten, its unpublished answer with it; one that returns is
served again. A source that fails (a read refused, an exception) is
diagnosed and the next want served.

**The publish.** Labels are written into the overlay's arena and named by a
segment-backed `GeomBuffer` (inline is accepted under 8 MiB; the Python
implementation always reserves), `dtype = DTYPE_U8`, `shape = (Z, Y, X)` of the box,
`position_zyx` its origin; the loan is the host's to release.

**Deferred, not failed.** When the arena cannot hold the chunk the plugin
**keeps the computed answer and waits** — for the host's next `ReleaseLoan`
into that arena, or for the want set to change — then retries the same
answer. It sends no `SegmentShortfall` and counts no refusal (§5.6's
fail-and-count is for unsolicited pushes). Before publishing a deferred
answer the plugin checks the want still stands; a deferred want that left
the set is dropped unpublished. **An invalidate (§5) discards a deferred
answer** (note 4): the retry recomputes.

**Never a chunk the host did not name.** The plugin publishes only for a key
in the overlay's current set. A chunk that arrives after its want left is
the host's ordinary case (dropped and counted), not a defect — but a plugin
that checks before it sends never produces one, and the driver counts them.

## 5. Commands

Each is refused — `CommandDone.ok = false`, a non-empty `message` — on a
bad argument, and a refused command changes nothing and sends nothing.

**`movie_seg.threshold`** — `value`: a finite number `≥ 0` (a bool is not a
number), **or a string that is one**, surrounding whitespace ignored — the
panel's box sends its text. Sets `T`, shows it in the panel's `threshold`
box (§5a), then **re-serves every want currently in either
overlay's set** with the new labels (the SDK's *invalidate*, or its
equivalent: the served-set of each overlay is cleared and the server runs
again). Reply: `threshold <T>; <n> wanted volume(s) to serve again`, where
`<T>` is the value as parsed — integral values without a decimal point
(`400`, not `400.0`), others in their shortest round-tripping decimal form —
and `<n>` is the number of wants across both overlays at that moment. The
driver asserts the `threshold <T>;` prefix and the `<n> wanted` substring.

**`movie_seg.band_visible`** — `band`: an integer in `{1, 2, 3}` (a bool is
not an integer); `visible`: a bool. Records the band as hidden or shown and,
if the `bands` table has been sent this session, sends it again with
the band's row alpha and `label_visible` reflecting every band's current
state (§3), and sets the
band's checkbox `band_<b>` to `visible` (§5a). Reply:
`band <b> (<name>) visible` or `band <b> (<name>) hidden` — asserted whole.

**`movie_seg.band_1`, `band_2`, `band_3`** — `value`: a bool. Exactly
`band_visible` for that band, with the same reply: the checkbox form. A
widget event carries only `{"value": …}`, nothing naming the widget, so each
checkbox needs a command of its own.

## 5a. The panel

**Why it exists.** vizi launches a plugin lazily, when a person first uses
something it contributes: a Tools-menu command, a panel, a tool, a reader. An
overlay is not such a thing, and this plugin's commands are `menu: null`, so
without a panel nothing in vizi can start it. Opening the panel starts the
session and keeps it alive while the panel is open; once a dataset is open,
the host names its wants and the overlays draw on their own.

**What it holds:** a `line_edit` `threshold`, which fires
`movie_seg.threshold` with its text, and three `checkbox`es `band_1`–`band_3`,
which fire `movie_seg.band_1`–`band_3` with their state. The declared values
are the defaults: `"400"`, and every band shown.

**It shows the plugin's state.** An accepted threshold sets `threshold` to
`<T>` as the reply formats it (`" 800 "` becomes `800`); an accepted band
change, by either form, sets that band's checkbox. The plugin patches whether
or not the panel is open: the host keeps a declared panel's state and shows it
at open (`control_plane_protocol.md` §8.4), so re-sending on `PanelOpened` is
optional. A refused command patches nothing.

## 6. Following the dataset

When the dataset changes the host (a) restates every chunked overlay's set
**empty**, (b) re-grants each overlay's arena at a new `segment_id` sized
for the new dataset, naming the old one in `InstallSegment.supersedes`, and
(c) once the new arena is installed at the plugin, names the new dataset's
volumes (§5.7 rule 2). The plugin's side is its SDK's: the old
segment enters *retiring* when the new one reaches *installed* and is
released with **`SegmentReleased`** (`shared_memory_protocol.md` §9; known
gap 3), and the new wants are served into the new arena from the new
dataset — every chunk reflecting the dataset open when it was served (known
gap 5).

## Acceptance

The driver runs these in this order against a six-frame `(6, 1, 8, 24, 32)`
u16 movie with a three-level pyramid — a Gaussian blob drifting in x — and
then, after a dataset change, a `(4, 1, 4, 8, 8)` u16 movie with two levels
whose every voxel is foreground, so that a level-0 chunk is the whole volume
and two of them fill the host-sized arena. Expectations are computed from the
emulator's dataset with the driver's own arithmetic (§2).

1. Three wants on `bands` — `(0, 2)`, `(2, 2)`, `(0, 3)` — are served **once
   each, in that order**.
2. Each chunk is `uint8`, segment-backed into the `bands` overlay's own
   arena, carries `t`, and equals the expected bbox-tight labels and origin
   on its level's lattice; a read of `(t, level)` preceded each.
3. The host released each loan once it applied the chunk; nothing is
   outstanding (a host-side check, but a plugin that reserves twice fails it).
4. The `bands` table arrived, once, as §3 pins it, at `draw_priority` 1; the
   footprint's (7) at 0.
5. The same set restated serves nothing.
6. The set moves to `(0, 3)`, `(2, 3)`, `(0, 4)`: `(0, 3)` is **not** served
   again; the other two are; the host evicts `(0, 2)` and `(2, 2)`.
7. The `footprint` overlay: wants `(0, –)`, `(1, –)` served with `t`
   **absent**, each the union over all six timepoints at that level, every
   timepoint read at that level; its table as §3 pins it.
8. `movie_seg.threshold 800` re-serves every current want on both overlays
   (three) with labels at 800; the reply as §5 pins it.
9. `movie_seg.threshold 5000` re-serves them **empty**: labels absent, no loan.
10. `band_visible 2 false` then `2 true`: the `bands` table re-sent each
    time, `label_visible` `{1: true, 2: false, 3: true}` then all true, row 2's
    alpha 0 then 255 and the other rows unchanged; replies as §5 pins them.
11. A negative threshold, a threshold that is not a number (`"high"`),
    band 7, a string `visible`: each refused, nothing served, no table sent.
12. The dataset changes: both arenas re-granted superseding the old; both
    old segments released **with `SegmentReleased`**; `threshold 400` replies
    `0 wanted`.
13. With the host holding its releases: wants `(0, 1)`, `(0, 2)`, `(0, 3)` —
    the first two arrive as whole-volume chunks; the third does **not**
    arrive within 0.8 s, and no `SegmentShortfall` and no `ContractViolation`
    is sent; after the host releases both, the third arrives with the
    expected labels.
14. With releases held: wants `(0, 0)`, `(0, 1)`, `(0, 2)` — two arrive, the
    third is deferred; the set is restated without `(0, 2)` and the host
    releases: `(0, 2)` is **never** published (the host drops nothing); the
    set is restated with `(0, 2)`: it arrives.
15. Two sets **back to back**, closer than a pump tick — `[]` then
    `(0, 0)`, `(0, 1)`, both already served and held: both are served
    again. (The served-set follows every set as it arrives, note 2.)
16. On the new dataset, `bands (1, 3)` and `footprint (1, –)`: whole
    level-1 volumes, inside level 1's shape.
17. The panel (§5a), after accounting. **While it is still closed**,
    `threshold " 800 "` (text) is accepted as `threshold 800;` and `band_2
    false` re-sends the table with band 2 hidden; **when it opens**, the box
    already shows `800` and `band_2` is already unchecked;
    `band_visible 2 true` checks it again; `threshold "high"` and
    `band_3 "no"` are refused; `threshold "400"` restores the default and
    the box shows `400`. The host refuses no patch.

Un-numbered, also driven: the handshake and the host-sized grants; `t = 0`
present on the wire; an empty set evicts everything and serves nothing;
accounting (nothing refused by the host, nothing dropped, no plugin-side
`ContractViolation`, no shortfall, no loan outstanding, nothing unhandled);
teardown inside the window with `ShutdownReport` `COMPLETED` or `NO_HOOK`.
