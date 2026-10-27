# SDKs in other languages

The vizi plugin protocol implemented in Go, Rust and Java, beside the Python
SDK in [`../vizi_plugin_sdk/`](../vizi_plugin_sdk/). Each was written from the
specification in [`../docs/spec/`](../docs/spec/) and the IDL in
[`../proto/`](../proto/), independently of the Python SDK and of each other.

| SDK | Where | Platforms |
|---|---|---|
| Python | [`../vizi_plugin_sdk/`](../vizi_plugin_sdk/) | Linux, macOS, Windows |
| Go | [go-sdk](go-sdk/) | Linux, macOS, Windows |
| Rust | [rust-sdk](rust-sdk/) | Linux, macOS, Windows |
| Java | [java-sdk](java-sdk/) | Linux, macOS, Windows |

Each SDK's README covers installing it, declaring a plugin and the API. The
example plugins under [`../examples/`](../examples/) are implemented in each
language.

The four doorbell receivers are cross-checked against each other on a shared
corpus of records: [CROSSCHECK.md](CROSSCHECK.md) describes the corpus and how
to run it. The cross-check grades the plugin side of the doorbell only; it
does not cover the control plane, the memory registry, readers or exporters.
