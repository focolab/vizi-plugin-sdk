# Ray probe, in Java

[`../SPEC.md`](../SPEC.md)'s ray probe, built on the Java SDK (`polyglot/java-sdk/`,
`org.focolab:vizi-sdk-java:1.0.0`, package `vizi.plugin.javasdk`).

| file | what it is |
|---|---|
| `src/RayProbe.java` | the plugin: declarations, the tool (`press`/`drag`/`release`/`cancel`/`action`, `activated`/`deactivated`), the pick worker, the five commands, the model |
| `src/Lattice.java` | the arithmetic: the two level maps (the SDK's), the crop as included centers, the supercover traversal, the column, the clip, the box, the block |
| `src/Picking.java` | the first maximum along the set, the block maximum (z, then y, then x), distances, plot decimation |
| `src/Plot.java` | the profile raster and a dependency-free PNG encoder |
| `test/src/*.java` | the offline checks |
| `icons/` | `probe.svg`, `undo.svg`, `clear.svg` |

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble` publishes the SDK from this checkout to `~/.m2`
(`polyglot/java-sdk/mill sdk.publishM2Local`), builds `build/plugin.jar` (the plugin and every
dependency in one jar) with `polyglot/java-sdk/mill assembly`, and writes the JDK's home to
`.java-home`. mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap. The
artifact is `build/plugin.jar`; `build/`, `out/` and `.java-home` are ignored.

Offline tests (21: the level maps on §8.11's numbers and a factor of 3, the crop, the traversal
against dense sampling and against a slab test over 8000 tie-rich segments, the column, the
block at factors 2 and 4, both tie rules, the ellipsoid, the pinned words, the keys said from a `ToolBinding`, the PNG, the manifest
against the code and the host's reader, the icons as SVG):

```sh
../../../polyglot/java-sdk/mill --no-daemon test
```

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
cd ../../examples/ray_probe/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd:

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\ray_probe\java
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
  - It writes `org.example.ray_probe/plugin-<hash>.jar` and `org.example.ray_probe/vizi-plugin.json`, and checks that the manifest reads
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
  - **jbang versions tested:** 0.137.0 and 0.139.3, on Linux. Each runtime option is written as
    `-R=--flag`. jbang 0.139 does not take the `-R--flag` form (it reads the `--` as an option of
    its own), and the `=` form works on both.
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
- **Where.** `--install=DIR` installs into another plugins directory, as `DIR/org.example.ray_probe`.
- **Into a directory it did not write.** The installer marks the directories it writes with a
  `.vizi-java-sdk` file naming the SDK and the plugin. It refuses to write into an existing,
  non-empty `org.example.ray_probe` directory without that mark, or whose mark names another plugin;
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

**The pane icons are packaged in the jar** (`build.mill` adds `icons/` as resources), and
`--install` writes them to `org.example.ray_probe/icons/`, so the install works from any
directory.

`vizi-plugin.json` here is the same manifest with the relative launch line `./ray-probe`. That is
a POSIX development launcher for the drivers, and it runs the jar `./assemble` builds (the POSIX
shortcut for the build lines above). At launch the plugin warns if the file and
the code disagree anywhere but `entry`, whose launch line is meant to differ once installed.

The driver runs an installed copy too: `uv run python
examples/ray_probe/python/hostsim.py --plugin-dir DIR/org.example.ray_probe`, from the repository root.

## Install and the launch line

`vizi-plugin.json` is written from the code by `./ray-probe --manifest`. It is SPEC's manifest
(`views` written out; the writer leaves `mode: "gesture"` and the pane's `hold: "toggle"`,
both schema defaults, implicit), and

```json
"entry": { "command": ["./ray-probe"] }
```

`./ray-probe` **never builds and `exec`s** `"$(cat .java-home)/bin/java"
--enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow -jar
build/plugin.jar`, so the process vizi starts is the JVM that serves, holding the inherited
doorbell. To install into vizi, either point vizi at this directory after `./assemble`, or run
`./ray-probe --install` (the SDK's installer: `~/.vizi/plugins/org.example.ray_probe/`, a
launch line pinned to this JVM, the icons copied).

## Run against the emulator

From the repository root:

```sh
.venv/bin/python examples/ray_probe/python/hostsim.py --plugin-dir examples/ray_probe/java
```

The driver runs 22 checks. The arrangement check
reports the activation status `'Probe active — click in a view — Shift+P toggles, middle button holds;
U undoes, X clears'`, and a passing run ends:

```
ok    profile panel re-pushed on open: PNG and 'Pick 4: (18, 22, 42) = (36, 11, 21) um, value 2901, c=0, t=2 (2.5 s)' re-pushed
ok    no violations: no ContractViolation beyond the two undeclared-level records
ok    teardown: ShutdownReport COMPLETED, then a clean end of stream; 2 violation report(s) in all, none unexpected

host emulation passed
```
