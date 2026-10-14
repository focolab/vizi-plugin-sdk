# Movie seg, in Java

The movie-seg plugin of [`../SPEC.md`](../SPEC.md), built on the Java SDK
(`polyglot/java-sdk/`, `org.focolab:vizi-sdk-java:0.1.0-r83`): two chunked overlays (`bands`, per t;
`footprint`, static), five commands (`movie_seg.threshold`, `movie_seg.band_visible`,
`movie_seg.band_1`–`band_3`) and the panel `movie_seg` (a threshold box and a checkbox per band).
`src/MovieSeg.java` is the plugin (the two chunk sources, the tables, the commands, the panel);
`src/Bands.java` is the arithmetic (band rule, footprint union, tight box, tables, replies).

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble` publishes the SDK from this checkout to `~/.m2`
(`polyglot/java-sdk/mill sdk.publishM2Local`), builds `build/plugin.jar` (the plugin and every
dependency in one jar) with `polyglot/java-sdk/mill assembly`, and writes the JDK's home to
`.java-home`. mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap.

Offline tests (the band rule at its edges, the tight box, the union,
the tables, the replies, the manifest and its §3.5 refusals):

```sh
../../../polyglot/java-sdk/mill --no-daemon test
```

## Install into vizi

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
cd ../../examples/movie_seg/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd:

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\movie_seg\java
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
  - It writes `org.example.movie_seg/plugin-<hash>.jar` and `org.example.movie_seg/vizi-plugin.json`, and checks that the manifest reads
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
    `-R--flag` form (it reads the `--` as an option of its own); the `=` form works on 0.137
    and 0.139.
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
- **Where.** `--install=DIR` installs into another plugins directory, as `DIR/org.example.movie_seg`.
- **Into a directory it did not write.** The installer marks the directories it writes with a
  `.vizi-java-sdk` file naming the SDK and the plugin. It refuses to write into an existing,
  non-empty `org.example.movie_seg` directory without that mark, or whose mark names another plugin;
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

`vizi-plugin.json` here is the same manifest with the relative launch line `./movie-seg`. That is
a POSIX development launcher for the drivers, and it runs the jar `./assemble` builds (the POSIX
shortcut for the build lines above). At launch the plugin warns if the file and
the code disagree anywhere but `entry`, whose launch line is meant to differ once installed.

## In vizi

Open a movie, then **Tools → Show Movie seg**. Opening the panel is what starts the plugin: vizi
launches a plugin only when something of it is used, and an overlay is not such a thing. The host
then names the volumes the panes show, and the overlays fill in as they are served. Type a
threshold in the box (intensity units, default 400) and press Enter to re-serve every shown
volume; untick a band to hide it. The box and the checkboxes follow the plugin's state, whichever
command changed it. Closing the panel may end the session, and the overlays with it.

## The launch line

`vizi-plugin.json` is written from the code by `./movie-seg --manifest`:

```json
{
  "manifest_version": 1,
  "id": "org.example.movie_seg",
  "name": "Movie seg",
  "version": "0.1.0",
  "engines": { "vizi": "1.0" },
  "entry": { "command": ["./movie-seg"] },
  "contributions": {
    "commands": [
      { "id": "movie_seg.threshold", "title": "Set threshold", "menu": null },
      { "id": "movie_seg.band_visible", "title": "Show or hide a band", "menu": null },
      { "id": "movie_seg.band_1", "title": "Show band 1 (dim)", "menu": null },
      { "id": "movie_seg.band_2", "title": "Show band 2 (mid)", "menu": null },
      { "id": "movie_seg.band_3", "title": "Show band 3 (bright)", "menu": null }
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
    ],
    "overlays": [
      { "id": "bands", "name": "Intensity bands", "chunked": true },
      { "id": "footprint", "name": "Footprint", "chunked": true, "time": "static" }
    ]
  },
  "permissions": ["data_access"]
}
```

(The SDK's writer pretty-prints it one value per line and leaves out `"time": "per_t"`, the
default; `MovieSegTest.specSectionOneIsTheSameManifest` checks that SPEC §1 verbatim parses to
the same manifest.)

`./movie-seg` **never builds and `exec`s** `"$(cat .java-home)/bin/java"
--enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow -jar
build/plugin.jar`, so the process vizi starts is the JVM that serves, holding the inherited
doorbell. A fresh checkout needs `./assemble` once.

## Run against the emulator

From the repository root:

```sh
.venv/bin/python examples/movie_seg/python/hostsim.py --plugin-dir examples/movie_seg/java
```
