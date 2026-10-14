# Exporters

An exporter adds an output format to vizi's Export Data dialog. The host
pumps the selected volumes to your process; you write the artifact.

## SDK surface

```python
import os
from vizi_plugin_sdk import ExportDescriptor, exporter, serve

@exporter("stats_csv")               # must match the manifest exporter id
class StatsCsvExporter:
    def begin(self, desc: ExportDescriptor) -> None:
        self._path = desc.locator
        self._f = open(self._path, "w", encoding="utf-8")

    def write(self, t: int, c: int, volume_zyx) -> None:
        ...  # one delivered (Z, Y, X) volume

    def finish(self) -> None:
        self._f.close()

    def abort(self) -> None:
        self._f.close()
        os.unlink(self._path)  # delete the partial artifact

if __name__ == "__main__":
    serve(plugin_id="org.example.stats_csv")
```

- `begin(desc)` — `ExportDescriptor` carries the whole-artifact geometry the
  host declared: `locator` (the output path — your process is granted write
  access to its directory), `shape_tczyx`, `dtype` (a numpy dtype),
  `little_endian`, and **the calibration the host knows**: `spacing_zyx` /
  `spacing_unit` and `time_increment` / `time_unit`, `None` where it does
  not. Write a measurement only when you were given one: an artifact that
  claims 1 µm for an uncalibrated source is a false claim the next reader
  will believe.

  It also says **what the artifact is** and **what it came from**
  (`spec/control_plane_protocol.md` §8.6):
  - `channels` — the artifact's channels in its C order, each a
    `ChannelName` (`index` in the source, `name`, `source`). Write a name
    into the artifact only when its `source` is `"FILE"` or `"USER"`; a
    `"PLACEHOLDER"` ("Channel 2") would read back as a name the file gave.
  - `source_metadata_form` (`"ome-xml"` | `"ome-ngff"` | `""`) and
    `source_metadata_text` — the **source's** document, as its reader gave
    it. Its sizes, channel list and pixel sizes describe the source, and an
    export can be a subset, a crop, a projection or a filter's output, so
    **never write it as the artifact's own description**. Embed it, if at
    all, as provenance beside `provenance`.
  - `provenance` — vizi's record of the export (source and fingerprint,
    selection, crop, filter, host) as a parsed `dict`, `None` when the host
    sent none. Embed it where your format keeps extra metadata, as vizi does
    under `vizi:provenance`.
- `write(t, c, volume_zyx)` — called once per delivered volume, in chunk
  order, serialized on the SDK's export worker. `t` and `c` are **source**
  indices; the artifact index of each is its rank among the distinct values
  the export names, which the host sends in artifact order
  (`spec/reader_writer_contract.md` §2.3) — keep the two ranks yourself. The
  array is yours: the SDK
  has already copied the deposit out of the host's segment before it replies
  `ChunkWritten` — which matters, because **the reply is the handoff**: the
  host reuses the arena the moment it arrives, without waiting for the
  release (`spec/reader_writer_contract.md` §2.3). The `X` segment is
  granted before `OpenExport` and the first chunk may arrive before both
  halves of the grant have landed; the SDK holds it until they do.
- `finish()` — finalize the artifact.
- `abort()` — the user cancelled: clean up the partial file. The host never
  finalizes an abort, and the SDK always answers it, so a slow `abort` cannot
  wedge the session. The SDK also calls it for every export still open at
  shutdown, best effort: the host is not going to finish them.
- A constructor with one parameter receives a `PluginContext` (`panel(id)`,
  `overlay(id)`, `host`), exactly as a [reader's](readers.md) does — an
  export's outcome lands in `finish()`, outside any command, and this is how
  it reaches the panel. A class with no constructor parameter is constructed
  bare.

Exceptions in any of these surface in the host's export flow (`begin
failed: …` / `write failed: …` / `finish failed: …`) and abort the export.
The kind is yours through `Refusal(kind, message)`, as for a reader:
`UNSUPPORTED` for a dtype your format cannot hold, `OUT_OF_BOUNDS` for a
chunk that contradicts the declared geometry; anything else is `IO_ERROR`,
and a `HostError` you let through from `ctx.host` keeps the host's kind. A
request naming a handle that `finish` or `abort` has already consumed is
answered `NOT_FOUND` before your class is asked.

## How the host uses it

Voxels arrive through the export shared-memory segment (`X`: host-written,
plugin-read — the reader data plane reversed). The host's timeouts are 30 s
to open, 120 s per chunk, 60 s to finish and 10 s to abort; each is a
dead-session backstop, not a budget, and an expiry marks the session suspect.
Your `name` labels the format-selector entry; `extension` drives the save
dialog's filename filter.

## Reference example

An exporter that writes a container format —
ranking the source `t` and channel each chunk names into artifact indices,
writing the calibration it was given and never one it was not, refusing to
finalize an incomplete artifact — is [`examples/format_bridge/python/`](../../examples/format_bridge/python/)
in this repo, run against the emulator.
