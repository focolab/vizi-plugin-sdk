# vizi-plugin-sdk

The SDK for building plugins for vizi, a 4D scientific-imaging viewer. A plugin
runs in its own process, with its own environment, and never imports vizi's
code. The SDK hides the transport between the two (a gRPC control plane, a
shared-memory data plane and a doorbell socket): you declare a class, decorate
it, and call `serve()`.

Plugins built on this SDK speak plugin protocol `1.0`. **vizi v0.9.0 and later
are protocol `1.0` hosts.**

## Install

```
pip install git+https://github.com/focolab/vizi-plugin-sdk.git
```

The package is `vizi_plugin_sdk` and needs Python 3.11 or later. It brings
`grpcio`, `protobuf`, `numpy` and `atomicshm`, a small native wheel (Linux,
macOS, Windows) for the process-shared atomics a live overlay needs. The name
`vizi-plugin-sdk` on PyPI currently holds a placeholder, not the SDK; install
from this repository until a release is published there.

## A first plugin

```python
import numpy as np
from vizi_plugin_sdk import ReaderInfo, reader, serve

@reader("synth")
class SynthReader:
    def open(self, uri: str) -> ReaderInfo:
        return ReaderInfo(shape_tczyx=(2, 1, 4, 8, 8), dtype="u16")

    def read_volume(self, t: int, c: int) -> np.ndarray:
        return np.zeros((4, 8, 8), dtype=np.uint16)

if __name__ == "__main__":
    serve()
```

A plugin is a directory with a `vizi-plugin.json` manifest and an entry
command. vizi discovers it and launches it as a separate process.
[Getting started](docs/getting-started.md) builds this reader step by step, from
an empty folder to a file opening in vizi.

## What a plugin can contribute

| Contribution | What it adds | Guide |
|---|---|---|
| Reader | a file format vizi can open | [readers](docs/contributions/readers.md) |
| Exporter | a file format vizi can write | [exporters](docs/contributions/exporters.md) |
| Command | a menu entry or key binding that runs your code | [commands](docs/contributions/commands.md) |
| Panel | a dock panel built from declared widgets | [panels](docs/contributions/panels.md) |
| Tool | pointer input in the 2D and 3D views | [tools](docs/contributions/tools.md) |
| Overlay | geometry or label volumes drawn over the data | [overlays](docs/contributions/overlays.md) |
| Processing stage | a filter that edits a volume in a chain | [stages](docs/contributions/stages.md) |

## Documentation

- [Developer guide](docs/README.md): concepts, the manifest, every
  contribution kind, reproducible environments and debugging.
- [Specification](docs/spec/README.md): the normative protocol documents. Where
  this README or the guide disagrees with a specification document, the
  document wins.
- [`proto/`](proto/): the protocol's IDL, for regenerating stubs or targeting
  another language.

## Examples

Each example under [`examples/`](examples/) is a complete plugin with a driver
that runs it against the host emulator, so it can be tried without vizi
installed.

| Example | Shows |
|---|---|
| [`format_bridge`](examples/format_bridge/) | a reader and an exporter |
| [`peak_roi`](examples/peak_roi/) | a panel, a background worker, contributed ROIs |
| [`voxel_probe`](examples/voxel_probe/) | a tool with live overlay feedback |
| [`interactive_seg`](examples/interactive_seg/) | a lasso tool, a label volume, masks |
| [`ray_probe`](examples/ray_probe/) | 3D input: resolving a point along a ray |
| [`movie_seg`](examples/movie_seg/) | a chunked overlay over a time series |
| [`region_quant`](examples/region_quant/) | measuring regions, lists in a panel, long-running work |
| [`tetra_shape`](examples/tetra_shape/) | a region type the plugin shapes |
| [`stage_chain`](examples/stage_chain/) | processing stages |

[`examples/vizi_hostsim/`](examples/vizi_hostsim/) is the host emulator
itself: it plays vizi's side of the protocol over real sockets and shared
memory, for tests and for development without the application.

## Other languages

A plugin is a process, not a Python import, so any language that can speak the
protocol can host one. [`polyglot/`](polyglot/) holds SDKs for
[Go](polyglot/go-sdk/), [Rust](polyglot/rust-sdk/) and
[Java](polyglot/java-sdk/), each written from the specification, and most
examples are implemented in all four languages.

## Versions

The SDK has its own version (`vizi_plugin_sdk.__version__`) and speaks a
protocol version (`1.0`); the handshake carries both. Compatibility is decided
by the protocol's major version: a `1.x` host serves a `1.0` plugin, and a
`2.0` host does not. A manifest's `engines.vizi` field states the protocol
version a plugin needs, and vizi lists a plugin it cannot serve as not loadable
instead of running it.

## Repository layout

- `vizi_plugin_sdk/`: the Python SDK. `tests/` is its test suite.
- `proto/`: the protocol IDL. The generated Python stubs are committed under
  `vizi_plugin_sdk/_proto/`.
- `docs/`: the developer guide, and the specification under `docs/spec/`.
- `examples/`: the example plugins and the host emulator.
- `polyglot/`: the Go, Rust and Java SDKs.
- `scripts/`: environment check, stub generation and cross-check tooling.

[`DEVELOPING.md`](DEVELOPING.md) covers building, testing and regenerating
stubs.

## Contributing

Issues and pull requests are welcome on this repository. The Python SDK, its
tests, the IDL and the specification (`vizi_plugin_sdk/`, `tests/`, `proto/`,
`docs/spec/`) are published here from vizi's own source tree, so a change to
any of them is best raised as an issue: it is made there and arrives with the
next publish. The guide, the examples, the emulator and the Go, Rust and Java
SDKs are maintained here directly.

## License

Apache-2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).
