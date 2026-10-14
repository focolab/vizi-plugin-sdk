# Stage chain, in Java

The four processing stages of [`../SPEC.md`](../SPEC.md), built on the Java SDK
(`polyglot/java-sdk/`, `org.focolab:vizi-sdk-java:0.1.0-r83`): `gain`, `diff`, `widen` and `drift`,
in `src/StageChain.java`.

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble` publishes the SDK from this checkout to `~/.m2`
(`polyglot/java-sdk/mill sdk.publishM2Local`), builds `build/plugin.jar` (the plugin and every
dependency in one jar) with `polyglot/java-sdk/mill assembly`, and writes the JDK's home to
`.java-home`. mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap.

Offline tests (SPEC §3's arithmetic):

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
cd ../../examples/stage_chain/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd:

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\stage_chain\java
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
  - It writes `org.example.stage_chain/plugin-<hash>.jar` and `org.example.stage_chain/vizi-plugin.json`, and checks that the manifest reads
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
  - **Runtime options** are each written as `-R=--flag`. jbang 0.139 does not take the `-R--flag`
    form (it reads the `--` as an option of its own); the `=` form works on 0.137 and 0.139.
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
- **Where.** `--install=DIR` installs into another plugins directory, as `DIR/org.example.stage_chain`.
- **Into a directory it did not write.** The installer marks the directories it writes with a
  `.vizi-java-sdk` file naming the SDK and the plugin. It refuses to write into an existing,
  non-empty `org.example.stage_chain` directory without that mark, or whose mark names another plugin;
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

`vizi-plugin.json` here is the same manifest with the relative launch line `./stage-chain`. That is
a POSIX development launcher for the drivers, and it runs the jar `./assemble` builds (the POSIX
shortcut for the build lines above). At launch the plugin warns if the file and
the code disagree anywhere but `entry`, whose launch line is meant to differ once installed.

To check an installed copy, run the driver against it from the repository root, here chained to
itself: `uv run python examples/stage_chain/python/hostsim.py --plugin-dir SCRATCH/org.example.stage_chain --other-dir
SCRATCH/org.example.stage_chain`.

## The launch line

`vizi-plugin.json` is written from the code by `./stage-chain --manifest`; it is SPEC §1's
manifest, with

```json
"entry": { "command": ["./stage-chain"] }
```

`./stage-chain` **never builds and `exec`s** `"$(cat .java-home)/bin/java"
--enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow -jar
build/plugin.jar`, so the process vizi starts is the JVM that serves, holding the inherited
doorbell. A fresh checkout needs `./assemble` once; after that a launch is `java -jar` on an
assembled jar, well inside the host's 10 s window.

## Run against the emulator

From the repository root, after `./assemble`:

```sh
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/java
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/java --other-dir examples/stage_chain/python
.venv/bin/python examples/stage_chain/python/hostsim.py --plugin-dir examples/stage_chain/python --other-dir examples/stage_chain/java
```

`--plugin-dir` launches session A and `--other-dir` session B (default: the same directory, as a
second process). The same works both ways with `go` and `rust`, each built as its own README says.
A run that passes ends `all passed`.
