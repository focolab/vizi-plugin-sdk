# Readers

A reader teaches vizi to open a file format. Declaring one adds your
extensions to the File → Open filter; opening a matching file launches your
plugin and streams volumes out of it.

## SDK surface

```python
import numpy as np
from vizi_plugin_sdk import ReaderInfo, reader, serve

@reader("ramp")                      # must match the manifest reader id
class RampReader:
    def open(self, uri: str) -> ReaderInfo:
        return ReaderInfo(shape_tczyx=(10, 1, 16, 64, 64), dtype="u16")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        ...  # one (Z, Y, X) array of the declared dtype

if __name__ == "__main__":
    serve(plugin_id="org.example.ramp_reader")
```

- **Answer the open promptly.** A reader's open and metadata requests may be
  given as little as 30 s by a host that does not show them; a host that
  shows the request and lets the person cancel it may wait as long as the
  person tolerates. So `open()` reads the header for the shape and metadata
  and returns, and the heavy work goes in the reads, which are supervised.
- `open(uri)` — `uri` is the path the user picked. Open/inspect the file,
  return the full dataset geometry: `shape_tczyx` (5-tuple) and `dtype`
  (`"u8"` | `"u16"` | `"f32"`) — and **whatever calibration you know**:
  `spacing_zyx=(z, y, x)` in `spacing_unit` (`"um"`) and `time_increment` in
  `time_unit` (`"s"`). `None`, or an empty unit, means *unknown*, which is an
  honest and common answer; an axis you do not know can be `None` inside the tuple. Never
  invent a `1.0`. A unit the host does not recognize is unknown too: the
  file's own calibration stands if it has one, otherwise the host reports the
  axis as unknown. Nothing is assumed to be µm or s. A reader is the one end that can know the voxel pitch —
  without it a six-to-one Z stack displays as isotropic — and these fields
  win over a metadata document that disagrees. `axis_inference` (default
  `"plugin"`) is one word for the host's log saying how the axes were
  decided — `"ome"`, `"fallback"`, your format's name; the host records it
  on the dataset and branches on nothing.
- `read_volume(t, c)` — return **one volume** as a contiguous `(Z, Y, X)`
  numpy array whose dtype matches the declaration. Called repeatedly as the
  user scrubs. The SDK deposits it C-order, X fastest, little-endian,
  unpadded — the only conforming layout at `1.0`
  (`spec/shared_memory_protocol.md` §2). **The SDK converts byte order and
  nothing else:** a `>u2` array of the declared `uint16` is written
  little-endian for you, but a volume whose dtype or shape differs from what
  `open()` declared is refused as a failed read naming both — never
  delivered as a silently wrong array. The declarable dtypes are `uint8`,
  `uint16` and `float32`.
- `read_plane(t, z, c)` — optional. If absent, **the SDK** derives the plane
  by slicing `read_volume` — the host has no fallback of its own and sends
  every 2D view as a `ReadPlane`, so a reader that neither implements it nor
  sits on an SDK that derives it shows a blank 2D view
  (`spec/reader_writer_contract.md` §1.4). Implement it when a single plane
  is much cheaper than a volume (e.g. chunked formats).
- `metadata()` — optional. Return `(form, text)` with `form` one of
  `"ome-xml"`, `"ome-ngff"`, or a bare string for an untagged document. A
  reader without one answers the host's request with the empty form, which
  means *no document*, not an empty one.
- Several readers in one plugin are fine. The host names which reader it is
  opening, and `serve()` routes each open to it. Pass `serve(reader_id=...)`
  only to serve *one* of the registered readers and refuse the rest.
- `close()` — optional, and called on every deliberate close, on the next
  file's `OpenReader` (which **replaces** the open reader), and at shutdown
  — after the in-flight read that may still be using the file, before the
  `ShutdownReport`, and again as a backstop when the stream ends without a
  `Shutdown`. Do not *depend* on `CloseReader` itself arriving — a reader
  that is merely dropped (a crash, a finalizer) is retired without it, and
  a **replaced** reader gets neither a `CloseReader` nor a `RetireSegment`
  (`spec/reader_writer_contract.md` §1.2, §1.8) — but the SDK's close on the
  next open means the file is released regardless, and its close at
  shutdown means a `@on_shutdown` hook is not needed for it.
- **The replacement is a handshake, and its order is open-then-close**
  (`spec/reader_writer_contract.md` §1.2). The host opens
  the next file *while the last is still on screen*, so the SDK constructs
  and `open()`s the next instance **first** and closes the previous one only
  on success — a refused open leaves the previous reader untouched, and
  `Error` means nothing changed. The one exception is a reload of the open
  reader's own URI, which closes first (a locked file cannot be opened
  twice) and whose failure leaves no reader. Handles are numbered `r0`,
  `r1`, … and never reused; the new `G` arrives with `supersedes` naming the
  old, which retires on the install; a request naming a replaced handle is
  `NOT_FOUND` and diagnosed (the host is out of step); a `CloseReader`
  naming a replaced handle is the idempotent `Closed` and leaves the open
  reader alone. **What this means for your class:** by the time a replaced
  instance's `close()` runs, its successor has already opened and may have
  written the panel — a `close()` that clears shared state must first check
  it is still the current reader (the format-bridge reference keeps the
  open instance in module state and clears the panel only when `close()`
  finds itself there).
- **A constructor with one parameter receives a `PluginContext`** —
  `def __init__(self, ctx)` — with `ctx.panel(id)`, `ctx.overlay(id)` and
  `ctx.host`, the same handles a command gets; a class with no constructor
  parameter is constructed bare. A format plugin's panel is updated from
  `open()` and a writer's from `finish()`, neither of which runs inside a
  command, so this is how they reach it. A reader serving a dataset must not
  ask `ctx.host` for that dataset (§1.9): the host refuses it rather than
  deadlock.
- Level 0 only. The host builds no pyramid over a plugin-served dataset and
  sends `level = 0`; the SDK answers any other level `OUT_OF_BOUNDS` before
  your class is asked, as it does an index past `shape_tczyx` and a request
  on a handle nothing is open under (`NOT_FOUND`). One volume is lent at a
  time; the host reads it and releases it before the next request.

Exceptions raised in `open`/`read_volume` are reported to the host as
protocol errors (`open failed: …` / `read failed: …`) — the viewer shows a
load failure; your process survives. **The kind is yours to choose**: raise
`Refusal(kind, message)` (exported next to `HostError`, its plugin-side
twin) with a `pb2.Error` kind — `INVALID_ARGUMENT` for a file that is not
your format, `UNSUPPORTED` for one you recognize and cannot serve,
`OUT_OF_BOUNDS` for a chunk that contradicts the declared geometry — and the
SDK forwards it. Anything else you raise is `IO_ERROR` ("the file failed
under the reader", §1.5), except a `FileNotFoundError`, which is `NOT_FOUND`
("`uri` cannot be opened"). The handlers run serialized on the SDK's reader
worker, in the order the host asked.

## Shear and deskew

vizi applies no shear or deskew to a dataset. A reader whose source is
sheared (light-sheet, oblique-plane or swept acquisitions) deskews the volume
itself and returns it on an orthogonal lattice; no metadata key asks vizi to
do it.

## How the host uses it

The volume bytes cross through a host-granted shared-memory segment (the
reader's `G` segment), not gRPC — returning a volume is roughly a memcpy into
a deposit the host reads and then releases. The host may cache; don't assume
a fixed read order or that every `(t, c)` is read exactly once. A volume whose
size disagrees with the declared shape and dtype is refused before any byte
is reinterpreted.

## Reference example

The [getting-started walkthrough](../getting-started.md) builds a
deterministic reader from scratch. A reader of a real container format — with calibration,
a metadata document, CRC verification, a plane path, and the panel updated
from `open()` — is [`examples/format_bridge/python/`](../../examples/format_bridge/python/)
in this repo, run against the emulator.
