# Crate format bridge, in Java

The crate format bridge of [`../SPEC.md`](../SPEC.md), built on the Java SDK
(`polyglot/java-sdk/`, `org.focolab:vizi-sdk-java:1.0.0`): one reader, one exporter, a manifest
asset and a runtime slot, four commands and a panel, plus the host-free comparator and writer.

| File | What it is |
|---|---|
| `src/formatbridge/Crate.java` | the container: header, metadata, provenance and names sections, index, footer, block and plane reads, whole-file check |
| `src/formatbridge/CrateWriter.java` | the writer: blocks in arrival order, index, footer; abort deletes |
| `src/formatbridge/Calibration.java` | `vizi-crate-cal.bin` and `raw - offset[c]` |
| `src/formatbridge/Ome.java` | unit conversion, and the exporter's rule: `OpenExport`'s fields or unknown |
| `src/formatbridge/Compare.java` | the semantic comparator (tiers) |
| `src/formatbridge/CrateTool.java` | `compare`, `recreate`, `check` on the command line |
| `src/formatbridge/FormatBridge.java` | the plugin on the SDK |
| `test/src/formatbridge/CrateTest.java` | the offline checks |

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble` publishes the SDK from this checkout to `~/.m2`
(`polyglot/java-sdk/mill sdk.publishM2Local`), builds `build/plugin.jar` (the plugin and every
dependency in one jar) with `polyglot/java-sdk/mill assembly`, and writes the JDK's home to
`.java-home`. mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap. The
dependencies are pinned by exact version in `build.mill` (mill has no lockfile): the SDK at
`1.0.0` and JUnit Jupiter `5.13.4`.

Offline tests (the format against the committed fixtures, voxel size, the writer, the interop
crates, the comparator's tiers and exit codes, and that `vizi-plugin.json` is the manifest the
code declares):

```sh
../../../polyglot/java-sdk/mill --no-daemon test
```

## The launch line and the manifest

`vizi-plugin.json` is written from the code by `./format-bridge --manifest`. It is `SPEC.md`'s
Manifest section, with

```json
"entry": { "command": ["./format-bridge"] }
```

`./format-bridge` **never builds and `exec`s** `"$(cat .java-home)/bin/java"
--enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow -jar build/plugin.jar`,
so the process vizi starts is the JVM that serves, holding the inherited doorbell descriptor. A
fresh checkout needs `./assemble` once.

## Install

The manifest is declared in the code (`Plugin.builder(...)`), so the plugin installs itself with
the Java SDK's `--install`. **You need [jbang](https://www.jbang.dev/download/)**, a one-line
install. It supplies the JDK 25 the plugin runs on, and downloads one if none is installed.
The build fetches its own JDK through the committed `mill` bootstrap. Only on Linux with glibc
older than 2.39, or on Windows on ARM, does the bootstrap fall back to a JVM build of mill, which
needs some Java on `PATH` to start.

From the repository root, in bash or zsh:

```
cd polyglot/java-sdk
./mill --no-daemon sdk.publishM2Local
cd ../../examples/format_bridge/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd:

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\format_bridge\java
..\..\..\polyglot\java-sdk\mill.bat --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

- **The first two lines** publish the SDK from this checkout to `~/.m2`. Run them once, and again
  after the SDK changes.
- **The fourth line** builds `out/assembly.dest/out.jar`: the plugin and every dependency, in one
  jar.
- **The last line** runs that jar under jbang's JDK 25+ with the two flags the SDK needs, and
  installs into `$VIZI_PLUGINS_DIR`, or else `~/.vizi/plugins` (`%USERPROFILE%\.vizi\plugins`
  on Windows).
  - It writes `org.example.format_bridge/plugin-<hash>.jar` and `org.example.format_bridge/vizi-plugin.json`, and checks that the manifest reads
    back as vizi reads it.
  - If vizi is already running, use **Tools → Plugin Manager… → Rescan**.

**What `--install` offers:**

- **`--launcher=jbang`** (the line above; recommended). The launch line is
  `jbang --java 25+ -R=… plugin-<hash>.jar`, which pins the JDK **by version**: any JDK 25 or later that
  jbang finds or has fetched. A JDK update or removal does not break it.
  - **How it names jbang:** by its bare name when jbang is on `PATH` at install time. Otherwise
    by the absolute path of jbang's own install location, `~/.jbang/bin`.
  - **If vizi cannot start `jbang`,** vizi was launched without jbang on its `PATH` (a desktop
    launcher can do that). Add jbang to that `PATH`, or install with `--launcher=path`.
  - **Runtime options:** each is written as `-R=--flag`. jbang 0.139 does not take the
    `-R--flag` form (it reads the `--` as an option of its own); the `=` form works on 0.137 and
    0.139.
  - **A warm-up:** before writing the manifest, the install runs the installed jar once through
    exactly that launch line (`--help`). Any JDK download happens then, not at vizi's first
    launch. If the warm-up fails, the manifest is not written, and the error says why.
- **`--launcher=path`** (the default when `--launcher` is left off). The launch line names, by
  absolute path, the `java` that ran `--install`, with the two flags.
  - Under jbang, as above, that is the JDK jbang chose.
  - With a JDK of your own, run the jar with it instead:
    `java --enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow -jar
    out/assembly.dest/out.jar --install`.
  - It needs no jbang at launch, and survives `PATH` changes, but not that JDK being moved or
    removed by an update: then run `--install` again.
- **Where.** `--install=DIR` installs into another plugins directory, as `DIR/org.example.format_bridge`.
- **Into a directory it did not write.** The installer marks the directories it writes with a
  `.vizi-java-sdk` file naming the SDK and the plugin. It refuses to write into an existing,
  non-empty `org.example.format_bridge` directory without that mark, or whose mark names another plugin;
  `--force` writes into it anyway. It updates its own install without `--force`. While it writes
  it holds `.vizi-install-lock` in the directory; if an interrupted install left one behind, remove
  it and run `--install` again. Each file is written beside its target and renamed over it, the
  manifest last, so a failed install leaves the old one working.
- **A copy, not in place.** The jar is copied into the plugin directory under a name taken from
  its content, `plugin-<16 hex digits of its sha256>.jar`, never over the jar a running plugin
  uses; the jars it replaces are deleted once the new manifest is written, where the system
  allows (Windows keeps one that is running). This checkout can move or go without breaking the
  installed plugin. Rebuilding here changes nothing installed
  until you run `--install` again.
- **Only the manifest.** `--manifest` (or `--manifest=DIR`) writes the manifest, and any pane
  icons it names, into the working directory (or `DIR`) without installing anything.

**Provisioning the calibration asset.** It is declared `manual`, so vizi keeps it, not the
plugin directory. `sample-calibrated.crate` stores raw counts, and will not open until the table
is provisioned: in vizi, **Plugin Manager → Crate Format Bridge → Import…**, then choose
`examples/format_bridge/fixtures/vizi-crate-cal.bin` (32 bytes, SHA-256 `20845d7c…`). Without it
the open is refused with a message naming Plugin Manager; that is the specified behavior, not a
reader bug. **Use a different calibration…** on the panel picks a per-user override, which wins
over the manifest asset once set.

The host-free crate tools (`compare`, `recreate`, `check`, below) are read before the SDK's
options, so the same jar runs them: put the tool's name and its arguments where `--install` is.

`vizi-plugin.json` here is the same manifest with the relative launch line `./format-bridge`. That is
a POSIX development launcher for the drivers, and it runs the jar `./assemble` builds (the POSIX
shortcut for the build lines above). At launch the plugin warns if the file and
the code disagree anywhere but `entry`, whose launch line is meant to differ once installed.

To run the driver against an installed copy, from the repository root:

```sh
.venv/bin/python examples/format_bridge/python/hostsim.py --plugin-dir SCRATCH/org.example.format_bridge
```

## The host-free tools

**What an exported crate says it came from.** Every crate the exporter writes
is `DERIVED`: `OpenExport.source_metadata`, the source's document, goes into the metadata section
verbatim, as provenance, and this plugin's reader serves no document for it, because that
document describes the source's sizes, channels and pixel sizes, not the crate's. The host's
provenance record goes into its own section as it arrived (none when the SDK finds it is not a
JSON object). A channel's name is written only when `OpenExport.channels` marks it `FILE` or
`USER`, and a placeholder is written as `""`. The reader declares the names as
`ReaderOpened.channel_names`. The voxel size comes from `OpenExport`'s fields alone: nothing reads
the source's document for it.

```sh
./format-bridge compare A.crate B.crate [--calibration TABLE]
./format-bridge recreate IN.crate OUT.crate [--order ascending|reverse|scrambled] [--calibration TABLE]
./format-bridge check FILE.crate
```

`compare` prints the tier the two crates match at (`byte-equal`, `semantically equal`,
`voxel-equal` or `different`, then notes; channel names count toward `semantically equal`, and
`DERIVED` and the provenance record are only noted) and exits **0** once both opened and a verdict was
printed, **2** when either could not be opened or decoded (a calibrated crate with no
`--calibration` cannot be), **1** for a usage error. `check` exits 0 clean, 3 with CRC failures,
2 unopenable.

**The interop crate.** `../interop/java.crate` is written by

```sh
./format-bridge recreate ../fixtures/sample.crate java.crate
```

and is byte-identical to `../interop/python.crate`.

## Run against the emulator

After `./assemble`, from the repository root:

```sh
.venv/bin/python examples/format_bridge/python/hostsim.py --plugin-dir examples/format_bridge/java
```
