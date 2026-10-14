# Tetra shape, in Java

The tetra_shape plugin of [`../SPEC.md`](../SPEC.md), built on the Java SDK (`polyglot/java-sdk/`,
`org.focolab:vizi-sdk-java:0.1.0-r83`). One irregular tetrahedron is contributed four ways:

- `free` and `scale` are declared regions the host enacts from their data alone;
- `volume` is asked, and the plugin's shaper keeps its volume (SPEC §3);
- `unshaped` is a region type declared with no shaper, so the SDK answers its commits `REJECTED` by itself.

The files:

- `src/TetraShape.java` is the plugin: the manifest in code (`region_types` included), the one command, the shaper,
  and the `ShapeEditEnded` hook.
- `src/Tetra.java` is the geometry: the four regions, the 12 scale bindings (through the SDK's `Rois.scaleBindings`),
  and the volume rule.

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble`:

1. publishes the SDK from this checkout to `~/.m2` (`polyglot/java-sdk/mill sdk.publishM2Local`);
2. builds `build/plugin.jar` (the plugin and every dependency in one jar) with `polyglot/java-sdk/mill assembly`;
3. writes the JDK's home to `.java-home`.

mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap. `./assemble` and the launcher
`./tetra-shape` are POSIX shell. On Windows, use the build lines under *Install*.

The offline tests cover:

- the 12 bindings, entry by entry against `M = (v_j − c)·u_iᵀ / r_i` computed independently, and exact under a
  scaling;
- the four regions' handles, `dof_mask`, `min_extent` and bindings;
- the volume rule's three cases: a corner pulled out, a corner pushed in within the bound, and a corner past the
  1/16 limit (and through the face), with a non-corner refused;
- a result being one finite position per vertex;
- the manifest on disk against the declared one.

Run them with:

```sh
../../../polyglot/java-sdk/mill --no-daemon test
```

## Run against the emulator

From `../python`:

```sh
uv run --project ../../.. python hostsim.py --plugin-dir ../java
```

It runs the 14 acceptance checks against `./tetra-shape`, which runs the jar `./assemble` built. The launcher never
builds, and it `exec`s the JVM.

## Install

The manifest is declared in the code (`Plugin.builder(...)`), so the plugin installs itself with the Java SDK's
`--install`. **You need [jbang](https://www.jbang.dev/download/)**, a one-line install, which supplies the JDK 25 the
plugin runs on (and downloads one if none is installed).

From the repository root, in bash or zsh (Linux, macOS):

```
cd polyglot/java-sdk
./mill --no-daemon sdk.publishM2Local
cd ../../examples/tetra_shape/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd (Windows):

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\tetra_shape\java
..\..\..\polyglot\java-sdk\mill.bat --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

- The first two lines publish the SDK from this checkout to `~/.m2`. Run them once, and again after the SDK changes.
- The fourth line builds `out/assembly.dest/out.jar`.
- The last line installs into `$VIZI_PLUGINS_DIR`, or else `~/.vizi/plugins` (`%USERPROFILE%\.vizi\plugins` on
  Windows), as `org.example.tetra_shape/` with a content-named `plugin-<hash>.jar` and the manifest. It warms the
  launch line up once first, so any JDK download happens then, not at vizi's first launch.
- `--launcher=path` (the default) names the `java` that ran `--install` instead of jbang. The rest of the
  installer's options are the Java SDK's, described in `examples/region_quant/java/README.md`.

## The launch line

`vizi-plugin.json` is written from the code by `./tetra-shape --manifest`. It is SPEC §1's manifest with the
relative launch line `./tetra-shape`. At launch the plugin warns if the file and the code disagree anywhere but
`entry`.
