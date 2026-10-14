# Crate format bridge — plugin specification

A plugin that teaches vizi to open a container format, to write that same
format back out, and to use both halves of the shared-memory data plane.
It ships one reader, one exporter, one manifest asset, one runtime asset slot,
and a small panel whose only job is to make an invisible refusal visible.

This document specifies **behaviour**, not implementation. The
implementations beside it (`python/`, `go/`, `rust/`, `java/`) each follow
it on their language's SDK, and [`python/hostsim.py`](python/hostsim.py)
runs its scenarios against **any** of them. The protocol is `1.0`; where
this document and `docs/spec/` disagree, `docs/spec/` wins.

## What this example demonstrates

- **A plugin as a data source and a data sink.** `'G'` is the reader arena
  (plugin-written, host-read) and `'X'` the export segment (host-written,
  plugin-read): the two directions of the data plane. Both are `LOANED`
  arenas granted in two halves and released by a message.
- **A reader contribution**: `OpenReader`, `ReadVolume`, `ReadPlane`,
  `Metadata`, `CloseReader`, the replacement handshake when a second file
  is opened, and the calibration fields of `ReaderOpened`.
- **An exporter contribution**: `OpenExport`, `ExportChunk` addressed by
  source timepoint and channel, `FinishExport`, `AbortExport`, and the
  calibration, channel names, source document and provenance record an
  export is handed.
- **Both provisioning paths for out-of-band files**: a manifest asset and a
  runtime asset slot filled through `RequestAsset`.
- **A panel patched from code paths that are not command handlers**, a
  Tools-menu command with a keybinding, and commands that acknowledge
  before they prompt a person.
- **An artifact that can be checked offline.** The plugin produces files, so
  a crate written by one implementation must open in another, with no host
  running. The committed fixtures in [`fixtures/`](fixtures/) are the shared
  anchor, and [`interop/`](interop/) holds crates each implementation's
  writer produced.

Read [`shared_memory_protocol.md`](../../docs/spec/shared_memory_protocol.md)
§3 (the purpose table that names `G` and `X`, and which end produces each)
and §5–§7 (the `LOANED` discipline both use),
[`reader_writer_contract.md`](../../docs/spec/reader_writer_contract.md), and
[`control_plane_protocol.md`](../../docs/spec/control_plane_protocol.md) §5.6
and §8.12 for the loan rule and the other half of a grant. The SDK receives
the segment for you; [`doorbell_M_memory.md`](../../docs/spec/doorbell_M_memory.md)
is background on how it arrives.

### Protocol notes

The rules of the protocol this plugin leans on most, cited below as
*note N*.

1. **A plugin reader has no pyramid.** `reader_writer_contract.md` §1.6:
   the host builds no pyramid over a plugin-served dataset, sends
   `level = 0` only, and reserves the field; a reader answers any other
   level with `Error{OUT_OF_BOUNDS}` and never with level-0 data. §6 below
   is that rule. The SDKs apply it before the reader class is asked.

2. **A plugin cannot use `data_access` on a dataset its own reader serves.**
   `docs/contributions/commands.md` states the rule — the host raises
   `UNSUPPORTED` immediately, because calling back into the blocked reader
   would deadlock. The consequence is architectural: a plugin that both
   *serves* a format and *analyses* the open dataset has two code paths to
   the same voxels, and which one is live depends on who opened the file.
   This example makes that visible on purpose — see
   [the panel](#cratedescribe-and-the-re-entrancy-guard).

3. **Reader and exporter entry points run while the host is waiting on
   them.** The SDK runs each class on its own worker, off the loop that
   routes host replies (`control_plane_protocol.md` §6). The host is blocked
   on the reply, and a request that needs *it* to act — a read of the
   dataset the reader is serving — is refused by the self-read guard
   (note 2). Resolve anything needed from the host *before* the reader is
   asked for bytes, or on a worker whose result the entry point does not
   wait for.

4. **A chunk's bytes stay valid until they are released.** `ExportChunk`
   names a deposit in the `X` segment's host-produced `LOANED` ring, and the
   bytes stay valid **until the plugin sends `ReleaseDeposit`** naming them
   (`control_plane_protocol.md` §5.6, §8.7) — not until it replies, and not
   forever. Copy or consume the chunk, release it, then reply
   `ChunkWritten`. A plugin that never releases leaks ring capacity until
   teardown and never corrupts anything.

5. **The `G` grant follows `ReaderOpened` and is acknowledged before the
   first read — it is not a session-setup grant**
   (`reader_writer_contract.md` §1.1). The arena is sized from the reply —
   one level-0 volume of `shape_tczyx × dtype` — so it cannot precede the
   open, and the host waits for the installed `GrantAck` before it sends a
   read. The grant has two halves on two channels — `M` on the doorbell,
   `InstallSegment` standalone on the stream — and `doorbell_protocol.md`
   §11a requires the listener draining before readiness so the `M` cannot
   be dropped. A read that arrives before both halves is answered with an
   `Error` naming the missing purpose; the kind is unspecified because
   nothing depends on it. **`X` is the other way round**: granted standalone
   *before* `OpenExport`, the host waiting for nothing, so a chunk naming a
   segment not yet installed is held until both halves land (§8.12) — and
   **the reply is the handoff**: the host reuses the arena the moment
   `ChunkWritten` arrives, so consume before replying
   (`reader_writer_contract.md` §2.3). And the rule the reader most needs:
   **a second `OpenReader` replaces the open reader, whether or not a
   `CloseReader` came first** (`reader_writer_contract.md` §1.2, §1.8 — the
   host sends one on a deliberate close, but a dropped reader is retired
   without it), so release the file on the next open and at `Shutdown`,
   never only on `CloseReader`.

6. **`menu_label` is a panel field**: non-empty, at most 128 characters,
   overriding the default `Show <title>` menu entry
   (`docs/spec/manifest_schema.md`). The manifest below uses it.

7. **A reader declares a voxel size, an exporter is told one, and the host
   consumes both.** `ReaderOpened` fields 6–11 and `OpenExport` fields 7–12
   carry `spacing_z/y/x`, `spacing_unit`, `time_increment`, `time_unit`,
   with `0` / `""` meaning *unknown* and never an invented `1.0`. The host
   asks `Metadata` **once per open**, parses the document through the same
   path as every OME file, and lays the declared fields over it **per
   axis** — a reader that declares only Z leaves X and Y to the document
   (`reader_writer_contract.md` §1.7); and it fills `OpenExport`'s fields
   from the open dataset's parsed calibration, per axis, leaving the rest
   unknown (`control_plane_protocol.md` §8.6). In the Python SDK:
   `ReaderInfo(spacing_zyx=, spacing_unit=, time_increment=, time_unit=)`
   and the same four on `ExportDescriptor`. So declare what the crate header
   knows, write what `OpenExport` gives, and keep the source's metadata
   document as provenance. **Unknown spacing is a normal state, not an
   error.**

8. **One reader is open per session, and a second `OpenReader` replaces
   it — as a handshake, open-then-close** (`reader_writer_contract.md`
   §1.2): the host opens the next file while the last is still on screen;
   the SDK opens the next reader FIRST and closes the previous only on
   success, so a refused open leaves it untouched; handles are never reused;
   the new `G` supersedes the old (no `CloseReader`, no `RetireSegment` for
   the replaced reader); a request naming a replaced handle is `NOT_FOUND`;
   a `CloseReader` naming one is the idempotent `Closed`. The driver runs
   that order (its `G` grants supersede; a refused open while a crate is
   open must leave it readable). **Consequence for the panel:** a replaced
   reader's `close()` runs AFTER its successor's `open()` has written
   `head`/`vox`, so a close that says "no crate open" must first check it
   is still the open reader.

## What a reader may declare

Everything the host learns about a dataset from a plugin reader is one
`ReaderOpened`: `handle`, `shape_tczyx`, `dtype`, `little_endian`,
`axis_inference`, `channel_names`, and the calibration — `spacing_z/y/x`,
`spacing_unit`, `time_increment`, `time_unit`, `0` / `""` for unknown. Plus
one `MetadataResult` when it asks, which the host does once per open: a
`form` string and an opaque `text`, parsed like any OME document, with the
declared fields laid over it per axis. There is no other channel.

**The unit when every value is unknown** is the plugin's to choose, and both
choices conform: the crate's units are fixed (§"Voxel size"), so a reader
may declare `spacing_unit = "um"`, `time_unit = "s"` always and let the zeros
carry *unknown*, or leave the unit empty with the values. The driver accepts
either.

**Shape and dtype.** Five dimensions in canonical TCZYX, three dtypes, and a
byte order.

**Resolution.** There is no `levels` field: a plugin-served dataset has no
pyramid, the host sends `level = 0` only, and §6 refuses anything else with
`OUT_OF_BOUNDS` (`reader_writer_contract.md` §1.6).

**Physical voxel spacing.** Real volumes are anisotropic. X and Y usually
share a sampling pitch; Z almost never matches it — a confocal stack at
0.325 µm laterally and 2 µm between planes is entirely ordinary, and a plugin
that assumes cubic voxels reports a Z extent six times wrong while looking
perfectly correct on screen. The same is true along `t`: a rate is
uninterpretable without knowing what an increment of 1 means. So the design
does three things:

1. **The crate format carries spacing as a first-class header field**, in fixed
   canonical units, because the plugin's own reader, panel and exporter need a
   number they can rely on without parsing anything.
2. **The reader declares it in `ReaderOpened`'s fields and also carries it
   in the metadata document**, because the fields are what the host reads
   first and the document is what every other consumer of the file reads.
3. **Nothing panics when it does not come back.** Unknown spacing is a normal
   state with a spelling (`0.0`), reported as unknown and carried on from.

That third point is the rule for this whole document. Absent spacing never
fails an open, never fails an export, and never produces a guess — least of all
the guess that voxels are cubic, which is the one that silently corrupts every
downstream measurement.

**The document.** The crate carries its metadata document verbatim and the
reader returns it verbatim. The exporter keeps the document `OpenExport`
hands it too, but as **provenance, never as the artifact's description**:
that document is the SOURCE's, and it describes the source's sizes, channels
and pixel sizes, which an export that subsets, crops or projects does not
have. So an exported crate is marked `DERIVED`, and its reader serves no
document for it.

**`axis_inference`** is `"crate"`.

## What the plugin does

**File → Open** on a `.crate` file loads it: the plugin opens the container,
reports its geometry, and streams volumes as the user scrubs.

**Export Data → Crate volume container** writes the open dataset back out as a
`.crate`, with the channel names the source declared, and the source's document
and the host's provenance record kept as provenance, cleaning up after itself
if the user cancels.

The panel carries a CRC-verification toggle, a **Describe active dataset**
button that reports the dataset's basic statistics *and which path it had to
read them through*, a control for replacing the shipped calibration table with
a user-chosen one, and a read-only line showing the **voxel size**, which says
`unknown` when it is.

## Manifest

```json
{
  "manifest_version": 1,
  "id": "org.example.format_bridge",
  "name": "Crate Format Bridge",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["<your launcher>", "<your entry point>"] },
  "permissions": ["data_access"],
  "assets": [
    {
      "id": "calibration",
      "name": "Crate sensor calibration v1",
      "filename": "vizi-crate-cal.bin",
      "size": 32,
      "sha256": "20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3",
      "source": { "kind": "manual" }
    }
  ],
  "contributions": {
    "readers": [
      { "id": "crate", "name": "Crate volume container", "extensions": [".crate"] }
    ],
    "exporters": [
      { "id": "crate", "name": "Crate volume container", "extension": ".crate" }
    ],
    "commands": [
      { "id": "crate.set_verify",       "title": "Verify CRC-32 on volume reads", "menu": null },
      { "id": "crate.describe",         "title": "Describe active dataset",     "menu": null },
      { "id": "crate.pick_calibration", "title": "Use a different calibration", "menu": null },
      { "id": "crate.check_file",       "title": "Check a crate file…",
        "menu": "tools", "default_keybinding": "Ctrl+Shift+K" }
    ],
    "panels": [
      {
        "id": "crate",
        "title": "Crate",
        "menu_label": "Crate bridge…",
        "root": {
          "kind": "column", "id": "root",
          "children": [
            { "kind": "label", "id": "head",   "text": "No crate open" },
            { "kind": "label", "id": "status", "text": "Ready" },
            { "kind": "row",   "id": "rvox", "children": [
              { "kind": "label", "id": "lvox", "text": "Voxel size" },
              { "kind": "label", "id": "vox",  "text": "(unknown)" }
            ]},
            { "kind": "row",   "id": "rcal", "children": [
              { "kind": "label", "id": "lcal", "text": "Calibration" },
              { "kind": "label", "id": "cal",  "text": "(not provisioned)" }
            ]},
            { "kind": "checkbox", "id": "verify", "command": "crate.set_verify",
              "text": "Verify CRC-32 on volume reads", "value": true },
            { "kind": "button", "id": "describe", "text": "Describe active dataset",
              "command": "crate.describe" },
            { "kind": "button", "id": "pickcal", "text": "Use a different calibration…",
              "command": "crate.pick_calibration" }
          ]
        }
      }
    ]
  }
}
```

`docs/manifest.md` is authoritative over this sketch if the two disagree.

Notes on choices that are load-bearing:

- **The reader id and the exporter id are both `crate`.** Ids must be unique
  *within* their kind, not across kinds.
- **`"menu": "tools"` on exactly one command, and `"menu": null` on the rest.**
  A widget-bound command must not be menu-invocable: a bare menu invocation
  carries no arguments. `crate.check_file` takes no arguments by design, and
  it is the example's use of the menu path and of `default_keybinding`. The
  host shows a `CommandDone.message` on success as well as failure
  (`control_plane_protocol.md` §8.3), so the acknowledgement
  `crate.check_file` returns before it prompts can say *"choose a crate to
  check"* in the status bar; what the check *found* arrives after that reply
  has gone and reaches only the panel — see §"`crate.check_file`".
- **`default_keybinding` loses every conflict**, silently, per
  `docs/manifest.md`.
- **`permissions` is `["data_access"]` alone.** The reader and the exporter
  need no permission at all — they are host-initiated. `data_access` is needed
  only for the panel's `crate.describe` and for `request_asset`.
- **No overlays, no tools, no ROI contribution.** This example stays on the
  format boundary.

## The `.crate` format

Pinned byte-for-byte: two implementations that disagree about this section
produce files that fail to open in each other.

All integers are **little-endian**. There is no alignment padding anywhere
except the explicit reserved fields below, which senders zero and receivers
ignore.

```
[ header      96 bytes                    ]
[ metadata    form_len + text_len bytes   ]
[ provenance  provenance_len bytes        ]
[ names       names_len bytes             ]
[ blocks      any order                   ]
[ index     24 * (T * C) bytes          ]
[ footer    32 bytes                    ]
```

### Header — 96 bytes at offset 0

| Offset | Size | Field | Type | Meaning |
|---|---|---|---|---|
| 0 | 8 | `magic` | bytes | ASCII `VIZCRATE`, no terminator |
| 8 | 2 | `format_version` | u16 | `1` |
| 10 | 2 | `dtype` | u16 | `1` = u8, `2` = u16, `3` = f32 |
| 12 | 4 | `flags` | u32 | bit 0 = `CALIBRATED`; bit 1 = `DERIVED`; all others reserved, zero on write, **ignored on read** like the reserved fields |
| 16 | 4 | `t_count` | u32 | timepoints |
| 20 | 4 | `c_count` | u32 | channels |
| 24 | 4 | `z` | u32 | |
| 28 | 4 | `y` | u32 | |
| 32 | 4 | `x` | u32 | |
| 36 | 2 | `metadata_form_len` | u16 | bytes of the form string |
| 38 | 4 | `metadata_text_len` | u32 | bytes of the document |
| 42 | 6 | reserved | | zero on write, ignored on read |
| 48 | 8 | `spacing_z` | f64 | micrometres; `0.0` = unknown |
| 56 | 8 | `spacing_y` | f64 | micrometres; `0.0` = unknown |
| 64 | 8 | `spacing_x` | f64 | micrometres; `0.0` = unknown |
| 72 | 8 | `time_increment` | f64 | seconds; `0.0` = unknown |
| 80 | 4 | `provenance_len` | u32 | bytes of the provenance record |
| 84 | 4 | `names_len` | u32 | bytes of the channel names |
| 88 | 8 | reserved | | zero on write, ignored on read |

The dtype codes deliberately match `DType`'s first three enumerators. `f64` has
no crate encoding: it is overlay geometry, not voxels.

The four doubles sit at offsets 48, 56, 64 and 72 — naturally 8-byte aligned,
so a header mapped as a struct needs no unaligned loads. The 6-byte reserved
field at 42 exists to buy that alignment and nothing else.

A header whose magic or `format_version` does not match is not a crate. Reject
it; do not attempt recovery. **A zero in any of `t_count`, `c_count`, `z`,
`y`, `x` is likewise not a crate** — refuse at the header (`INVALID_ARGUMENT`,
naming the axis), because a volume with no voxels is not a smaller volume.

### Voxel size

`spacing_z`, `spacing_y`, `spacing_x` are the physical extent of one voxel
along each axis in **micrometres**; `time_increment` is the interval between
timepoints in **seconds**. Fixed units, canonical in the file, no unit string to
disagree about — conversion happens once, when `OpenExport`'s fields are
written ([Units](#units)), never when a crate is read.

**`0.0` means unknown**, unambiguously: no real spacing is zero. Anisotropy is
the normal case, not the exception, and the format stores three independent
numbers because Z routinely differs from X and Y by a factor of five or more.

A value that is negative, infinite or NaN is **malformed metadata, not a
malformed file**. Treat it as unknown, note it, and keep reading voxels.

Micrometres and seconds are chosen because they are OME's own defaults, so the
common case needs no conversion at all.

### Metadata — immediately after the header

`metadata_form_len` bytes of UTF-8, then `metadata_text_len` bytes of UTF-8,
with no terminators and no padding between or after them. `form` uses
`MetadataResult`'s grammar: `"ome-xml"`, `"ome-ngff"`, or empty. Both lengths
may be zero, which means the crate carries no metadata document. **An empty
form with a non-empty text is also no document**: the wire cannot carry a
text without a form (`MetadataResult` with an empty form *is* "none"), so a
reader carries nothing to the host for it — and a writer never produces it,
because the exporter writes what `OpenExport` handed it, which is either a
form and a text or neither.

**`DERIVED` says whose document this is.** Clear, the section is this crate's
own description, and the reader serves it (§4). Set — every crate the exporter
writes — it is the document of the dataset the crate was exported *from*,
kept as provenance, and the reader serves no document at all: its sizes,
channels and pixel sizes are the source's, and an export can hold a subset, a
crop or a projection of it (`control_plane_protocol.md` §8.6). What
a derived crate *is* lives in its header and its names.

**Neither string is parsed, ever.** They are carried.

### Provenance — after the metadata

`provenance_len` bytes: the host's provenance record, `OpenExport.provenance`
(JSON, UTF-8), which vizi also embeds in its own exports. Write the
string as it arrived where your SDK hands it over. Where it hands over only
the parsed record, re-serialize that, so the section parses to the same JSON
value. A reader of the section compares parsed values, never bytes. Zero
means none, and so does a record the SDK could not parse. Carried, never
parsed and never served: the protocol has no field that takes it back to a
host. It is there so the file says what it was made from, and so a
comparator can show it.

### Channel names — after the provenance

`names_len` bytes of UTF-8: the channels' names by index, each separated from
the next by one `0x00` byte, with no terminator. Zero bytes means no names.
Otherwise there are one more names than there are `0x00` bytes, at most
`c_count` of them, and `""` means the crate does not name that channel. The
list may stop short of `c_count`. A writer drops trailing `""`s, so a crate
naming no channel writes `names_len = 0`.

**These are names the file declares, never a host's placeholder.** The
exporter writes a channel's name only when `OpenExport.channels` marks it
`FILE` or `USER`, and writes `""` for a `PLACEHOLDER` or an unspecified one.
Written into the artifact, a placeholder would read back as a name the file
gave (§8.6). A name that contains `0x00` cannot be written, and is written as
`""`.

The reader hands the names to the host as `ReaderOpened.channel_names`
(§1). A names section that is not valid UTF-8, or that holds more names than
`c_count`, is **malformed metadata, not a malformed file**: open the crate, serve
no names, and say so once in the panel's `status`.

The block region therefore begins at
`96 + metadata_form_len + metadata_text_len + provenance_len + names_len`.

### Blocks

One block per `(t, c)` pair: exactly `z * y * x * itemsize` bytes of raw
voxels, little-endian, in C order — `z` slowest, `x` fastest. No per-block
header, no per-block trailer, no padding.

`itemsize` is 1, 2 or 4 by dtype.

**Blocks may appear in any order on disk, and the committed fixtures do not use
canonical order.** The index is the only thing that says where a volume lives.
A reader that computes an offset by multiplying an index by a block size will
read the wrong volume every single time, which is why the fixture data is
constructed so that a wrong volume announces itself.

### Index — `24 * (t_count * c_count)` bytes

Entry `i` describes the block for `t = i / c_count`, `c = i % c_count`
(integer division). Entries appear in ascending `i`.

| Offset | Size | Field | Type |
|---|---|---|---|
| 0 | 8 | `offset` | u64 — absolute file offset of the block |
| 8 | 8 | `length` | u64 — block length in bytes |
| 16 | 4 | `crc32` | u32 — CRC-32 of the block's bytes |
| 20 | 4 | reserved | zero |

`length` must equal `z * y * x * itemsize` for every entry. A crate whose index
disagrees with its own header is malformed.

### Footer — the last 32 bytes of the file

| Offset | Size | Field | Type |
|---|---|---|---|
| 0 | 8 | `index_offset` | u64 |
| 8 | 8 | `index_entries` | u64 — must equal `t_count * c_count` |
| 16 | 4 | `index_crc32` | u32 — CRC-32 of the index bytes |
| 20 | 4 | reserved | zero |
| 24 | 8 | `magic` | bytes — ASCII `ETARCZIV` |

**The footer is what makes a crate finalized.** A file whose last 8 bytes are
not `ETARCZIV` is an unfinished crate — most likely the partial artifact of an
export that was aborted — and is not openable. §10 and
[`fixtures/unfinalized.crate`](fixtures/unfinalized.crate) are two ends of the
same fact.

### CRC-32

The CRC-32 used throughout is the standard IEEE 802.3 / zlib one: reflected
polynomial `0xEDB88320`, initial value `0xFFFFFFFF`, final XOR `0xFFFFFFFF`.
Its check value is fixed and easy to assert against:

```
crc32(b"123456789") == 0xCBF43926
```

Every ecosystem here ships this: `zlib.crc32`, `hash/crc32.ChecksumIEEE`,
`java.util.zip.CRC32`. Where it is not in the standard library, it is about
twenty lines and a 256-entry table. **Assert the check value in a unit test**;
a wrong CRC variant produces files that look fine until another
implementation opens them.

### Units

`OpenExport` states its spacing in `spacing_unit` and its time increment in
`time_unit`, and the crate stores micrometres and seconds. Convert by this
table:

| Length unit | × to µm | | Time unit | × to s |
|---|---|---|---|---|
| `m` | 1e6 | | `s`, `sec` | 1 |
| `cm` | 1e4 | | `ms` | 1e-3 |
| `mm` | 1e3 | | `min` | 60 |
| `µm`, `um`, `micron` | 1 | | `h`, `hr` | 3600 |
| `nm` | 1e-3 | | | |
| `Å`, `angstrom` | 1e-4 | | | |

**Accept both spellings of micro.** OME writes U+00B5 MICRO SIGN (`µ`), real
files in the wild contain U+03BC GREEK SMALL LETTER MU (`μ`), the two are
visually identical and compare unequal. A converter that handles only one of
them silently gets the right number with the wrong unit — which is to say the
wrong number — on a fraction of real files. An empty unit is `µm` or `s`
respectively (OME's defaults).

A value that is zero, negative or not finite, or that carries a unit not in the
table, yields **unknown** for that value alone. The others still count.
Unknown is `0.0`, and it is never an error.

### The calibration table

The manifest asset, `vizi-crate-cal.bin`. Present so a crate can store raw
sensor counts and still decode to comparable values.

| Offset | Size | Field | Type |
|---|---|---|---|
| 0 | 8 | `magic` | bytes — ASCII `VIZCAL01` |
| 8 | 4 | `entry_count` | u32 |
| 12 | 4 | reserved | zero |
| 16 | 8 × n | entries | `channel` u32, then `offset` i32 |

When and only when the header's `CALIBRATED` flag is set, the value a reader
reports is

```
reported = raw - offset[c]
```

with the subtraction performed in the dtype's own arithmetic, **saturating at
zero for the unsigned integer dtypes** and unclamped for `f32`. A channel absent
from the table has offset 0. The flag is a property of the stored bytes, so an
uncalibrated crate is decoded identically whether or not the table is available.

The committed
[`fixtures/sample.crate`](fixtures/sample.crate) and
[`fixtures/sample-calibrated.crate`](fixtures/sample-calibrated.crate) hold
**the same logical volumes** by construction — the second stores biased counts
and the flag. Decoding both and comparing is a complete test of the calibration
path, and it fails loudly if the asset is missing, misparsed, or applied to the
wrong channel.

### Worked example — `sample.crate`

2162 bytes, SHA-256
`5a60f7f496051d0ccccfe07a6fcbf8a12c543521b0b918a76f03333a17440c5f`.
`T, C, Z, Y, X = 3, 2, 4, 6, 5`, dtype u16, `flags = 0`, metadata form
`ome-xml` with a 443-byte document. No two axis lengths coincide, so a
transposing reader cannot accidentally produce a correctly shaped array.

Spacing is `(z, y, x) = (2.0, 0.325, 0.325)` µm with `time_increment = 1.5` s —
deliberately anisotropic, at a ratio real confocal data actually has. **The
header fields and the document's `PhysicalSize*` attributes agree by
construction**. `spacing-conflict.crate` is the fixture where they do not
agree, and where the reader must report the header's.

Voxel values are

```
value(t, c, z, y, x) = 10000*t + 1000*c + 100*z + 10*y + x
```

so every axis is independently legible in the decimal digits of any voxel you
read, and a swapped pair of axes tells you *which* pair. The first ten u16
values of the `(t=0, c=0)` block are `0, 1, 2, 3, 4, 10, 11, 12, 13, 14` — a row
of X, then the next row of Y.

The blocks are laid out in **reverse** canonical order, so the index reads:

| `i` | `(t, c)` | `offset` | `length` | `crc32` |
|---|---|---|---|---|
| 0 | (0, 0) | 1746 | 240 | `0x8d53b95b` |
| 1 | (0, 1) | 1506 | 240 | `0x1c3015ab` |
| 2 | (1, 0) | 1266 | 240 | `0xd56706b3` |
| 3 | (1, 1) | 1026 | 240 | `0x03eaaa2e` |
| 4 | (2, 0) | 786 | 240 | `0x5889bb44` |
| 5 | (2, 1) | 546 | 240 | `0x5aa02fdd` |

with the block region starting at 546, the index at 1986, and the footer at
2130. A parser that reproduces this table is right.

## The reader

### 1. `OpenReader`

`uri` is the path the user picked. Open the file, validate the header, read the
footer, load and verify the index, and reply `ReaderOpened` with
`shape_tczyx = (t_count, c_count, z, y, x)`, the mapped `dtype`,
`little_endian = true`, and `axis_inference = "crate"`.

Keep the file open. Assign a handle; every later request names it, and the
host echoes what it was given. Handles are the plugin's to allocate (the
Python SDK says `r0`) and need only be unique within the session.

**One reader is open per session at a time, and a second `OpenReader`
replaces it — open first, close second** (`reader_writer_contract.md`
§1.2; note 8). On `OpenReader` with a file already open: open the new
file; **only if that succeeds**, close the old one, drop its handle and answer
a new `ReaderOpened` with a new handle. A refused open answers an `Error` and
leaves the old file open and readable — the host keeps showing it, because
an `Error` means nothing changed. Do not refuse because a file is open, and do
not wait for a `CloseReader` that may never come: the host supersedes the
old `G` segment with a new one behind the open on the same ordered stream.
The SDKs run this order for you. A read naming the old handle afterwards is
`NOT_FOUND`.

Refuse, with an `Error` and a message a user can act on:

- not a crate (bad magic or `format_version`) — `INVALID_ARGUMENT`;
- not finalized (footer magic absent) —
  `IO_ERROR`, and say so in words: *"unfinished crate — an export of this file
  did not complete"*. This is a normal outcome, not a corruption;
- index CRC mismatch, or an index inconsistent with the header — `IO_ERROR`;
- `CALIBRATED` set and no calibration available — `IO_ERROR`, naming Plugin
  Manager. See [Calibration](#calibration-and-the-two-ways-a-file-reaches-a-plugin);
- a dtype code the format does not define — `UNSUPPORTED`.

**Voxel size is read from the header and from nowhere else.** The header wins
outright over the document, always, and the reader never parses the document to
find out whether they agree — `spacing-conflict.crate` is the fixture that
catches a reader which got clever. Report it to the panel's `vox` label,
formatted so anisotropy is visible at a glance:

```
2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s
```

with any component that is `0.0` rendered `?` and an all-unknown header
rendering `(unknown)`. Missing spacing is **not** a reason to refuse the open,
to warn, or to substitute a default — least of all `1.0`, which is a claim of
isotropy that will be wrong and will not look wrong.

Declare the crate's channel names as `ReaderOpened.channel_names` (the
[names section](#channel-names--after-the-provenance), `""` for a channel it
does not name; none at all when it names none). By the calibration fields'
rule, a name declared there wins over the document's for that channel.

Hand the metadata document to the host verbatim when it asks, unless the crate
is `DERIVED` (§4).

**Resolve the calibration table before you are asked for bytes, not during a
read** (note 3).

### 2. `ReadVolume`

Look the handle up, bounds-check `t` and `c`, take the index entry for
`t * c_count + c`, read `length` bytes from `offset`, verify the CRC if
verification is on, apply calibration if the flag is set, write the result into
the `'G'` segment, and reply with a `SegmentRef` describing what you wrote:
`shape_zyx = (z, y, x)`, the matching `dtype`, and the `segment_id` the grant
carried.

Out-of-range `t` or `c` is `OUT_OF_BOUNDS`. An unknown or closed handle is
`NOT_FOUND`. A CRC mismatch is `IO_ERROR` naming the `(t, c)` that failed —
never a silent return of the bytes you know are wrong.

The host may cache, may read out of order, and may read the same `(t, c)` more
than once. Do not assume otherwise.

**The deposit stays intact until the host says so.** The `G` region is a
plugin-produced `LOANED` arena (`shared_memory_protocol.md` §3, §7). Each
reply's `SegmentRef` is a reservation from it — a **loan** — and the bytes
stay lent until the host sends `ReleaseLoan` naming them
(`control_plane_protocol.md` §5.6). Never overwrite a range the host has not
released.

**And a deposit that does not fit fails the read.** The host budgets the
arena; reserve from it, and if the largest free run is smaller than the
volume, reply with an `Error` naming the shortfall and push a
`SegmentShortfall{segment_id, region, wanted_bytes, largest_free_bytes}` so
the host learns *by how much* (the Python SDK: `read failed: reader arena
is full (N bytes free, M wanted)`). Never block waiting for room, never reuse
lent bytes. Growing the arena is the host's move — it retires the segment
and re-grants a larger one (§8.12); a plugin's only move is to say the
number.

### 3. `ReadPlane`

Optional in general, and implemented here. A block is a contiguous C-order
volume, so plane `z` is `y * x * itemsize` bytes at
`offset + z * y * x * itemsize` — one seek and a short read, no need to touch
the rest of the block.

But the CRC covers the **whole block**. Verifying it means reading everything
the plane read was meant to avoid.

**`ReadPlane` reads only the plane and does not verify.** The panel's verify
checkbox governs `ReadVolume` only, and its label says so. A plane read
exists to be cheap, and a checksum that costs the whole block defeats it.

Reply with `shape_zyx = (1, y, x)` — a plane carries a leading Z of 1.

Bounds-check `z` as well as `t` and `c`; an out-of-range plane index is
`OUT_OF_BOUNDS`, exactly as in §2.

### 4. `Metadata`

Reply `MetadataResult{form, text}` with the crate's metadata section, byte for
byte. A crate with both lengths zero yields an empty `form`, which the contract
defines as "no metadata document". **So does a `DERIVED` crate, whatever its
section holds**: that document is its source's, kept as provenance, and served
as this file's it would tell the host the source's sizes and channels. An
unknown or closed handle is `NOT_FOUND`, as everywhere else a handle is named.
The host asks **once per open**, at open, and lays the `ReaderOpened`
calibration over whatever the document says, per axis
(`reader_writer_contract.md` §1.7); an `Error` reply is logged and treated as
the empty form, so a reader whose document fails is uncalibrated, not
unopenable.

**A metadata section that is not valid UTF-8** is malformed metadata, not a
malformed file — the same rule as a negative spacing: open the crate, serve
the voxels, answer `Metadata` with the empty form (a proto3 `string` cannot
carry the bytes, and substituting U+FFFD would make a corrupt document
compare equal to its own replacement-character form), say so once in the
panel's `status`, and treat the crate as carrying no document for the round
trip.

Do not synthesize, reformat, re-indent, or re-encode. The exporter keeps the
document it is handed with the same discipline, as provenance.

### 5. `CloseReader`, and handle lifetime

Reply `Closed{handle}`, drop the handle, and close the file. Subsequent reads
naming it are `NOT_FOUND`. Close **idempotently**: a `CloseReader` for a
handle nothing is open under is answered `Closed`, not an error, because the
host's close is best effort and reads nothing from the reply
(`reader_writer_contract.md` §1.8).

**Do not depend on `CloseReader` arriving.** The host sends it on every
deliberate close, but a reader that is merely dropped — a host crash, a
finaliser, a close that faulted before the announcement — is retired without
it. So the file is released on `CloseReader` when it comes, and on the next
`OpenReader` (§1) and at `Shutdown` when it does not. **The session's end
drops every handle opened on it**: the stream's lifetime *is* the session's.
So a clean shutdown, a stream error, and a host that simply disappears must
all release every open file.

### 6. Levels — the one resolution

The crate format stores one resolution, and so does every plugin-served
dataset: the host builds no pyramid over one and sends `level = 0` only
(`reader_writer_contract.md` §1.6; note 1).

**Any `ReadVolume` or `ReadPlane` whose `level` is not 0 gets an `Error` with
kind `OUT_OF_BOUNDS`** and the message *"crate datasets have one resolution
level"*. Not a silent fallback to level 0: a plugin that quietly serves
full-resolution data in answer to a request for a downsampled level has
returned an array of the wrong shape for the question asked, and the host has
no way to notice. The SDKs answer this before the reader class is asked.
[`python/hostsim.py`](python/hostsim.py) sends a non-zero level once, to see
the refusal.

## Calibration, and the two ways a file reaches a plugin

The protocol has two provisioning paths for out-of-band files, and this
plugin uses both so the difference is visible in one place.

**The manifest asset** is the published table: declared with an exact size and
SHA-256, verified by the host before the plugin ever sees it, provisioned by an
explicit user action in Plugin Manager, exposed to the plugin as a path. It is
immutable and it is the same for every user of this plugin version. The
manifest above declares
`vizi-crate-cal.bin`, 32 bytes, SHA-256 `20845d7c…`; the bytes are committed at
[`fixtures/vizi-crate-cal.bin`](fixtures/vizi-crate-cal.bin) and the user
provisions them with **Import…**.

**The runtime slot** is the per-user override: `crate.pick_calibration` calls
`RequestAsset{slot: "calibration_override", force_reprompt: true}`, the host
shows a native file picker, copies the selection into a persistent per-plugin
slot, and answers with a path plus the size and digest it recorded at import.
A clean cancellation sets `declined` and leaves `path` empty, which is a normal
outcome and must not be reported as an error.

**`crate.pick_calibration` must acknowledge before it prompts** — reply
`CommandDone{ok = true}`, then call `RequestAsset` from a worker and report to
the panel. The picker is human-latency and the command budget is ten seconds;
see §"Budgets, threads and failure". This applies whatever the size of anything
involved, and the calibration table is 32 bytes.

**Precedence: an override, once set, wins.** Resolve in this order at the point
a calibrated crate is opened — the override slot if it is populated, then the
manifest asset, then nothing. Report which one is live in the panel's `cal`
label: `"override: <basename>"`, `"manifest asset"`, or `"(not provisioned)"`.
A calibration you cannot see is a calibration you will misattribute a bug to.

Validate whatever you load — magic, `entry_count` against the file length — and
treat a malformed table as absent rather than as a crash. The host verified the
*digest* of the manifest asset; nobody verified that its contents parse, and
the override was never digest-checked against anything at all.

**How the asset path reaches the process.** It does not arrive on the
session stream: `control_plane_protocol.md` §2.1 says it is the launch
environment's `VIZI_PLUGIN_ASSETS`, a JSON object `{asset_id: absolute
path}` — a mapping, not a directory, and an asset absent from it is *not
declared*, which is a different condition from declared-but-missing. The
SDKs expose it (`asset_path(id)` in Python, raising `AssetUnavailableError`
with a message that says which condition).

**What `request_asset` does.** It is the protocol's only host-mediated file
picker, and it does not merely pick — it *imports*, permanently, into the
plugin's asset store. `crate.check_file` below uses it as a file picker and
gets a persistent import with it.

## The exporter

The reader deposit reversed: the host writes the selected voxels into the `'X'`
segment and sends a descriptor; the plugin reads them out and writes the file.

### 7. `OpenExport`

`locator` is the output path — the process is granted write access to its
directory. `shape_tczyx`, `dtype` and `little_endian` describe the whole
artifact and are declared exactly once. What the artifact **is** is those, the
calibration below, and `channels`; what it **came from** is
`source_metadata` and `provenance` (`control_plane_protocol.md` §8.6).

Create the file, write the header and the metadata, provenance and names
sections, and remember the geometry. Reply with a handle.

- **The source's document is kept verbatim, as provenance, and the crate is
  marked `DERIVED`.** `source_metadata`'s `form` and `text` go into the
  metadata section unchanged, and `flags` bit 1 is set, so the crate's reader
  never serves the document as this file's (§4). Never synthesize one; an empty
  form means the crate carries none.
- **`provenance` goes into the provenance section** (as it arrived, or
  re-serialized from what your SDK parsed). Empty means none.
- **`channels` names the artifact's channels, in its C order.** Write each
  one's `name` into the names section when its `source` is `FILE` or `USER`,
  and `""` otherwise. A host's `PLACEHOLDER` is never written as a name.
- **`dtype` outside `{u8, u16, f32}` fails `begin`** with a message naming the
  dtype. `f64` is overlay geometry and has no crate encoding.
- **`little_endian = false` means byte-swapping into the format's fixed
  little-endian layout.** In practice the host is little-endian; the path is
  unit-tested against hand-built bytes.
- **`CALIBRATED` is never set.** The exporter never writes calibrated crates:
  it is handed decoded values and has no offsets to re-apply. Exporting a calibrated
  crate and reading the result back therefore yields an *equal* file
  semantically but not a byte-identical one — see
  [the round trip](#the-round-trip).

**Voxel size, per axis, in this order.** `OpenExport` carries the
calibration the host knows — `spacing_z/y/x` in `spacing_unit`,
`time_increment` in `time_unit`, `0` / `""` for unknown (§8.6; note
7). For each of the four values: take the field if it is known, converting
the unit to the crate's canonical one by the table in [Units](#units) (a unit
not in the table is unknown); else unknown. Write the result into the crate
header. **Do not look in `source_metadata` for a value the fields leave
unknown**: it is the source's, and after a projection or a binning its pixel
sizes are not the artifact's. **Never write a measurement you were not given**
(`reader_writer_contract.md` §2.5): an artifact that claims 1 µm for an
uncalibrated source is a false claim the next reader will believe.

When every value comes out unknown, **write zeros and finish the export
normally.** Do not fail, do not warn modally, and do not synthesize a
spacing. Say it once in the panel:

> `"Exported without voxel size — the host supplied no spacing."`

**When some but not all values are unknown, log which.** An export whose
four values all came from the fields logs nothing: a healthy export is quiet.
The panel's "Exported" line names the file, the blocks and the voxel size.

### 8. `ExportChunk`

One chunk is at most one timepoint slab: `t`, a list of `channels` in buffer
order, and a payload of `channels.len()` volumes of `shape_zyx` concatenated in
that order.

**`t` and `channels` are SOURCE indices, not artifact indices**
(`reader_writer_contract.md` §2.3). An export of timepoints 40–49 of one
channel sends `t = 40 … 49`, and an export of channels 2 and 5 names them
`2` and `5`; `t < t_count` is not a check. The artifact's time index of a
chunk is **the rank of its `t` among the export's distinct `t`s**, which the
host sends ascending, one distinct `t` after another; the artifact's channel
index is likewise the rank of the source channel among the distinct channels
the export names, which the host sends in artifact order. So keep two ordered
lists of distinct source indices in arrival order — artifact index =
position in the list — and refuse a chunk that would make either list longer
than `OpenExport` declared. vizi sends one channel per chunk, one chunk at a
time, never `inline`; the emulator sends several channels in one chunk and
`inline` too, and [`python/hostsim.py`](python/hostsim.py) does both: its
two-channel chunk `channels = [1, 0]` lands at artifact `0, 1`.

**Bounds-check before you reinterpret.** `len` must equal
`channels.len() * z * y * x * itemsize`.

For each channel in order: append the volume's bytes to the file, record
`(offset, length, crc32)` against index slot `t_a * c_count + c_a`, where
`t_a` and `c_a` are the artifact indices above. Blocks land in arrival order,
which is why the format has an index at all.

Reply `ChunkWritten` with the bytes **consumed from the chunk** — that is,
`len` — not the bytes written to disk. The two are equal here and would not
be in a compressing format.

Copy or consume the whole payload, **release the deposit** with
`ReleaseDeposit{segment_id, region, offset, len}`, then reply (note 4).
The chunk may instead arrive `inline` — bytes on the message itself, below
the 8 MiB ceiling — in which case there is nothing to release.

If a `(t, c)` arrives twice, last write wins — append the new block and
overwrite the index slot, leaving the old bytes stranded in the file as dead
space.

A chunk whose `t` would be the `t_count + 1`-th distinct timepoint, or whose
channel would be the `c_count + 1`-th distinct channel, fails the chunk with
`OUT_OF_BOUNDS` naming the axis and leaves the export open: the host declared
the geometry at `OpenExport`, and a chunk that contradicts it is surfaced
rather than absorbed. The failed chunk ends the export, and
the host then sends `AbortExport` for it (`control_plane_protocol.md` §8.6:
however an export ends early), which removes the file. A chunk naming a handle no export is
open under is `NOT_FOUND`.

### 9. `FinishExport`

Write the index, write the footer, flush, close, reply `ExportDone`.

**Fail if the index is incomplete.** If any `(t, c)` slot never received a
block, the artifact cannot be finalized: fail with a message naming how many
slots are missing and one example. The alternative — writing a footer over an
index with holes — produces a file that opens and then fails mid-scrub, which
is strictly worse than not producing it.

Delete the partial file when finalization fails. The user asked for an
artifact; half of one is not a smaller version of that.

### 10. `AbortExport`

The export ended early: the user cancelled, or a chunk was cancelled, refused
or failed (`control_plane_protocol.md` §8.6 — the host sends `AbortExport`
however an export ends early). Close the file, **delete it**, and drop the handle. The
host never finalizes an abort, so what is on disk is yours to clean and nobody
else's.

**Reply `ExportDone`**: an abort is a request and holds the single in-flight
slot, and a plugin that answered nothing would wedge every later request
until the timeout (`control_plane_protocol.md` §8.6).

An abort that arrives for an unknown handle is not an error worth escalating;
the artifact is gone either way.

## The panel

### The UI state model

Each widget's invocation carries only that widget's own value, a button sends
nothing, there is no way to ask the host what another widget holds, and the
plugin owns the model, seeded from the manifest's declared defaults.

On panel open, re-push everything — the host rebuilds from the manifest on
every open and persists nothing. The event is `PanelOpened`, a control-plane
one-way (`@panel_opened` in the Python SDK).

**Most of this panel is updated from code paths that are not command
handlers.** A crate is opened by `OpenReader`, an export finishes in
`FinishExport`, and both must move `head`, `vox` and `status` — none of them
arriving as an `InvokeCommand`.

That is allowed. `PanelPatch` is an unsolicited push: the `PluginMsg` arm is
annotated `UNSOLICITED push` in `proto/vizi_plugin.proto`, and the message
comment says it is "pushed by the plugin at any time (its own thread)". Any
code path that can reach the outbound stream may patch a panel, and the host
applies patches last-write-wins on its own pump.

Two consequences:

- **A patch to a closed panel goes nowhere and is not an error.** A crate
  opened before the panel was ever shown leaves no trace, which is what
  the re-push-on-open rule above repairs. Keep the model in the
  plugin, not in the widgets.
- **The reader and exporter reach the panel through the SDK's context.** The
  Python SDK constructs a reader or exporter class that takes one
  constructor parameter with a `PluginContext` — `panel(id)`, `overlay(id)`,
  `host` — and the Python implementation patches from that; the other SDKs
  offer the same in their idioms.

Patching a panel is not a host *request* — nothing is waited on — so it does
not violate note 3. Pushing state from `OpenReader` is fine; blocking
there for a reply is not.

State to push: `head` names the open dataset or says none is open, `vox` the
voxel size, `cal` the live calibration source, `status` the last outcome.

### `crate.describe` and the re-entrancy guard

This command makes note 2 visible.

1. `GetViewState` for `t`, **the channel** (`visible_channels[0]`, or `0` when
   that set is empty; **not** `ViewState.c`, which is deprecated and always `0`:
   vizi has no active-channel control), the shape and `open_generation`.
2. `ReadDataVolume{t, c: that channel, level: 0}`, copy the deposit out,
   `ReleaseDeposit`.
3. Report min, max and mean to `status`, **and name the path**:
   `"read via host data access"`.

When the open dataset is a `.crate` **that this same plugin process is
serving**, step 2 does not return voxels. The host answers `host_error` with
kind `UNSUPPORTED`, immediately, because calling back into the blocked reader
would deadlock.

That is not a failure to report and give up on. The plugin is *the reader*: it
has the file open already. So fall back — read the same `(t, c)` through your
own reader path, directly, and report `"read directly — host data access is
unavailable for a dataset this plugin serves"`.

Both branches are reachable in one session: open `sample.crate` and the
message says *directly*; open anything the host reads natively and it says
*via host data access*.

**Also update `vox`, and here the two branches say different things.** For a
crate this plugin serves, the spacing is in the header and the label shows it.
For a natively-read dataset, this plugin has no way to obtain it: `ViewState`
publishes `t`, `z`, `c`, `visible_channels`, `level`, shape, dtype, crop,
`mip`, levels and dataset identity, and **no spacing**; the one message that
carries a spacing to a plugin, `EpochChange`'s `Geometry` (§8.11), is
arranged only for sessions that declare a tool (§8.7), and this plugin
declares none. So the label reads

> `"unknown — the host publishes no voxel size to a plugin without a tool"`

### `crate.check_file` — the menu path

Invoked from the Tools menu or its keybinding, with no arguments. It asks the
host for a file (`request_asset` on the slot **`crate_check_file`**,
`force_reprompt: true`), then validates that file as a crate without opening
it as a dataset: header, footer, index CRC, and every block CRC. The slot
name is pinned because a slot is durable per-plugin state in the host's asset
store (`../PERSISTENT-STATE.md`): two implementations spelling it differently
would leave two imports behind.

**Acknowledge before you prompt.** Reply `CommandDone{ok = true}` immediately,
then raise the picker and validate on a worker. See
§"Budgets, threads and failure": the ten-second budget is measuring the user's
trip through a file dialog, not the size of the crate. The reply leaves before
the user has chosen a file, so its `ok` cannot depend on what they chose.

- A clean cancellation does nothing and says nothing.
- **Every outcome — success, cancellation and failure alike — reaches the user
  through the panel's `status` only**, and is therefore invisible with the
  panel closed.
- Validate the whole file, count every failure, and report the first:
  *"3 of 6 blocks failed CRC; first: (t=1, c=0)"*.

## The round trip

Equality of two crates comes in **three tiers**:

- **Voxel equality** — same `dtype`, same `t_count`, `c_count`, `z`, `y`, `x`,
  and identical decoded voxels for every `(t, c)`.
- **Semantic equality** — voxel equality, plus identical metadata `form` and
  `text` byte for byte, identical header spacing and time increment, and the
  same channel names. `DERIVED` and the provenance record are not compared: a
  host's record differs from export to export (vizi's carries a timestamp).
  The comparator notes them.
- **Byte equality** — the files are identical. Deliberately *not* required of
  the round trip: block layout on disk is free and the fixtures exploit it.

Four properties, in increasing order of what they prove:

1. **Read then export preserves voxels.** Open `sample.crate`, export it to a
   new crate, and the two are voxel-equal.
2. **Read then export preserves the calibration, the channel names and the
   document.** Whether the result is *semantically* equal depends on what the
   host carries from a plugin reader through to a plugin exporter. The
   reader's `ReaderOpened` fields are consumed and laid over the document per
   axis, and `OpenExport`'s fields are filled from the open dataset's
   calibration (note 7), so spacing survives through the fields. The names
   the reader declares are `FILE` names from then on (§8.2), so they come
   back in `OpenExport.channels`. And the host hands the source's document to
   the exporter as `source_metadata`. **The emulator is not a measurement of
   the host** — `open_export(spacing_zyx=...)` is the emulator saying what
   it was told to know — so this property is checked only against a live
   vizi. When the fields come back zero the plugin reports it (§7) and never
   invents a spacing.
3. **Export is deterministic and byte-stable.** Export the same dataset twice
   under the same `OpenExport` and the two files are byte-identical. (A host
   whose provenance record carries a timestamp sends a different
   `OpenExport` each time. Then the files differ in the provenance section
   alone.) Re-import one and export again: the voxels, the spacing and the
   names carry forward. The document does not: a `DERIVED` crate serves none,
   so the second export's source has none to hand on.
4. **`sample.crate` and `sample-calibrated.crate` decode identically.** The
   calibration path is correct, or it is not.

Properties 1 and 2 need a host. Properties 3 and 4 do not, and neither does the
cross-implementation check that a crate written by one language's
implementation opens in another's. **Each implementation ships a semantic
comparator** — a subcommand or a test mode. It reports *which tier* two
crates match at, not a boolean: "voxel-equal, document differs, spacing
lost" is a sentence it can say. As a process: **exit 0 when both crates
opened and a verdict was printed, whatever the tier; exit 2 when either file
could not be opened as a crate; exit 1 for a usage error** — so a harness
that shells out to it can tell "they differ" from "I could not look".

## Budgets, threads and failure

- **The host allows 10 seconds for a command handler.** Validate, return
  `CommandDone{ok = true}` meaning *accepted*, and continue on a worker,
  reporting through the panel.
- **A command that waits for a human has already lost the budget.**
  `RequestAsset` opens a native file picker, and a user who navigates to a
  directory takes longer than ten seconds more often than not. So
  `crate.pick_calibration` and `crate.check_file` — the two commands that call
  it — **must** acknowledge before they prompt, not after: reply
  `CommandDone{ok = true}` first, then issue `RequestAsset` from a worker and
  report the outcome to the panel. The clock is measuring the user, not the
  work; the size of the crate is irrelevant.

  **"First" is about the budget, not the wire.** The rule is that the reply
  does not *wait for* the picker: it leaves when the handler returns, and
  nothing about the user's choice can reach it. It is **not** a requirement
  that `CommandDone` precede `RequestAsset` on the stream — the host
  correlates each by its own id, and a `RequestAsset` that arrives a few
  microseconds before its command's `CommandDone` is answered on its own
  merits. The Python implementation starts a worker thread and returns, and
  its worker's `RequestAsset` can overtake the reply; the driver asserts on
  neither order.

  **The outcome of a picker is never reported through `CommandDone`**:
  `ok = true` here means *the prompt was raised*, not *the file was
  accepted*. Everything after that — declined, malformed table, validation
  failures — reaches the user only through the panel.
- **A picker request is exempt from the ordinary request timeout.** Whatever
  deadline applies to `GetViewState` or `ReadDataVolume` — thirty seconds is a
  reasonable number for those — is far too short here: the thing being waited
  on is a person. Give `RequestAsset` its own, much longer backstop, sized so
  that no user choosing a file can trip it while a request orphaned by a dead
  host still eventually clears. The implementations here use **thirty
  minutes**. A plugin that abandons the request early discards the file the
  user actually chose when the answer finally arrives.
- **Reader and exporter entry points are not command handlers** and have no
  such budget, but they are on the critical path of a user waiting for a
  volume. Keep them to file I/O.
- **Never call the host from a reader or exporter entry point** (note
  3): the host is blocked on your reply.
- **Overlapping jobs are the plugin's call.** This document specifies no
  busy guard: `crate.describe` invoked while a previous describe (or a
  picker) is still on its worker may be refused (`CommandDone{ok = false}`
  and a status line) or serialized; the driver accepts either, and holds
  the first read open so the second invocation really does overlap. The
  Python implementation refuses.

- **One export at a time is not guaranteed.** The contract hands out handles,
  which implies more than one may be live. Key your state by handle rather than
  assuming a single current export, even if you never see two.
- A `host_error` reply is a normal outcome — no dataset, missing permission,
  the re-entrancy guard. Report it and keep the session alive.
- **Every error message names the file and the `(t, c)` it concerns.** These
  failures reach a user who is looking at a load error in a viewer, with no log
  open and no idea a plugin is involved.

## Fixtures

[`fixtures/`](fixtures/) is the interop anchor, regenerated by the pinned
recipe in [`fixtures/make_fixtures.py`](fixtures/make_fixtures.py) — pure
standard library, readable as a specification of last resort. If a regenerated
digest ever differs from the one recorded here, the format changed and this
document is wrong.

| File | Bytes | Purpose |
|---|---|---|
| `sample.crate` | 2162 | The reference crate. Reverse block order, `ome-xml` metadata, `flags = 0`, header spacing and document spacing agreeing. |
| `sample-calibrated.crate` | 2162 | The same volumes stored as biased counts with `CALIBRATED` set. |
| `unfinalized.crate` | 1986 | Header, metadata and blocks; no index, no footer. What an aborted export leaves. |
| `badcrc.crate` | 2162 | `sample.crate` with one bit flipped in block `i = 2`, `(t=1, c=0)`, CRC left describing the original. |
| `nospacing.crate` | 1996 | Zero spacing in the header and no `PhysicalSize` in the document. Everything must still work. |
| `spacing-conflict.crate` | 2180 | Header and document disagree, and the document states Z in nm and `t` in ms. |
| `vizi-crate-cal.bin` | 32 | The manifest asset: offsets 7 and 13 for channels 0 and 1. |

SHA-256 of each is printed by the generator and recorded in
[`fixtures/README.md`](fixtures/README.md).

Three of these repay particular care:

- **`badcrc.crate`** — with verification on, reading `(t=1, c=0)` must fail and
  **every other block must still read cleanly**. A reader that fails the whole
  file has made one bad block fatal; a reader that succeeds has not checked
  anything.
- **`nospacing.crate`** — must open, display and export exactly like any other
  crate, reporting `(unknown)` in the panel. It is the don't-panic path, and
  the failure it guards against is a plugin that treats missing spacing as an
  error, or worse, substitutes `1.0`.
- **`spacing-conflict.crate`** — the reader must report the **header's**
  `(2.0, 0.325, 0.325) µm` and `1.5 s`, not the document's
  `(1.0, 1.0, 1.0) µm` and `0.5 s` (which the document writes as `1000 nm` and
  `500 ms`, so a reader that parses it gives itself away twice).

## Acceptance

1. The plugin appears in Plugin Manager, stopped. `.crate` is in the File →
   Open filter, and **opening one launches the plugin**.
2. `fixtures/sample.crate` opens and displays. Scrubbing T and C shows the
   ramping synthetic values; the first voxel of `(t=2, c=1)` reads 21000. The
   panel's **Voxel size** reads `2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s`.
3. `fixtures/unfinalized.crate` fails to open with a message that says the
   export did not complete, and the plugin process survives.
4. `fixtures/badcrc.crate` opens; `(t=1, c=0)` fails to read with CRC
   verification on and the message names that timepoint and channel; other
   timepoints display normally; unchecking **Verify CRC-32 on volume reads** makes it
   display, wrongly, without complaint.
5. `fixtures/sample-calibrated.crate` displays identically to `sample.crate`
   once the calibration asset is provisioned, and refuses to open with a
   message naming Plugin Manager when it is not.
6. **Describe active dataset** reports *read directly* with a crate open, and
   *read via host data access* with a natively-read dataset open. With a
   natively-read dataset, **Voxel size** reads
   *"unknown — the host publishes no voxel size to a plugin without a tool"*.
7. `fixtures/nospacing.crate` opens, displays and exports normally, with
   **Voxel size** reading `(unknown)` and no warning anywhere.
8. `fixtures/spacing-conflict.crate` reports the **header's** spacing —
   `2.000 × 0.325 × 0.325 µm`, `Δt 1.5 s` — not the document's.
9. **Use a different calibration…** shows a picker; choosing a table updates
   the `cal` label and takes precedence; cancelling changes nothing.
10. **Check a crate file…** appears in the Tools menu, fires from its
    keybinding, acknowledges at once, and reports `badcrc.crate`'s failure —
    and `sample.crate`'s success — in the panel's `status`; **with the panel
    closed neither is visible** (§"`crate.check_file`").
11. Export Data offers **Crate volume container**; exporting the open dataset
    produces a file that opens again in the same plugin.
12. Cancelling an export mid-way leaves **no file behind**.
13. Exporting a dataset the plugin itself is reading is **voxel-equal** to the
    original (round-trip property 1). Whether it is also *semantically* equal —
    document, spacing and names intact — is property 2, which depends on the
    host. The exported crate is `DERIVED`: reopened, it serves no document,
    and it declares the names it was given as `FILE` or `USER`.
14. Opening a second crate while one is open **replaces** it: the first file
    is released (`lsof`-visible, or the equivalent), reads naming the old
    handle are `NOT_FOUND`, and the second serves normally — whether or not
    a `CloseReader` for the first ever arrived.
15. Reopening the panel shows the plugin's current state, not the manifest
    defaults.
16. **A chunk that contradicts its export is refused**
    (`control_plane_protocol.md` §8.6): `filtered_channels` non-empty when the
    export was opened with no `filter`, or naming a channel the chunk does not
    carry, is answered `Error{INVALID_ARGUMENT}` naming the contradiction, and
    nothing from it is written; the artifact does not survive the `AbortExport`
    the host sends next (answered `ExportDone`, or `NOT_FOUND` if the plugin already
    ended the export). A consistent filtered chunk exports normally. vizi
    never sends such a chunk; only the driver does. The SDK refuses it before
    the exporter sees it.
