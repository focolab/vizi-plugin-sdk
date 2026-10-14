# Region quant, in Java

The region-quant plugin of [`../SPEC.md`](../SPEC.md), built on the Java SDK
(`polyglot/java-sdk/`, `org.focolab:vizi-sdk-java:0.1.0-r83`). It keeps one list row per region,
each with its mean on one channel at the frame on screen and a `viridis` swatch. The rows are
kept current from the `view` and `regions` pushes. A row click and "Brightest frame" move the view
through set-view, and "Export CSV…" writes the table through the host's save dialog. A Follow,
offered as "Tour N regions", frames each row in turn once the person presses it (SPEC §7).
"Brightest frame" answers at once from each row's trace, which a worker thread keeps ahead of
time and shows in the `tracing` label (SPEC §2, §5).

- `src/RegionQuant.java` is the plugin: the declarations, the state under one lock, measuring,
  re-stating the list, and the four commands.
- `src/Quant.java` is the arithmetic and formatting: the mean text, the swatch index, the order
  by code point, the best frame, the CSV, and the PNG.

## Build

From this directory, with a JDK 25 or later on `PATH` (or in `JAVA_HOME`):

```sh
./assemble
```

`./assemble` does three things:

1. publishes the SDK from this checkout to `~/.m2`
   (`polyglot/java-sdk/mill sdk.publishM2Local`);
2. builds `build/plugin.jar` (the plugin and every dependency in one jar) with
   `polyglot/java-sdk/mill assembly`;
3. writes the JDK's home to `.java-home`.

mill 1.1.3 is fetched by the committed `polyglot/java-sdk/mill` bootstrap.

The offline tests cover:

- the mean text: a tie, a count of 0, a sum past 64 bits × 2000;
- the swatch index and its clamp;
- the order: an empty name, equal names, and code point versus UTF-16 order;
- the CSV quoting, a leading space unquoted, and a row not measured (f32) written with empty numbers;
- the best frame: a tie, and frames with no voxel;
- the tour's label, and where a resumed tour picks up;
- the PNG, decoded back;
- the manifest on disk against the declared one.

Run them with:

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
cd ../../examples/region_quant/java
../../../polyglot/java-sdk/mill --no-daemon assembly
jbang --java 25+ -R=--enable-native-access=ALL-UNNAMED -R=--sun-misc-unsafe-memory-access=allow out/assembly.dest/out.jar --install --launcher=jbang
```

In PowerShell or cmd:

```
cd polyglot\java-sdk
.\mill.bat --no-daemon sdk.publishM2Local
cd ..\..\examples\region_quant\java
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
  - It writes `org.example.region_quant/plugin-<hash>.jar` and `org.example.region_quant/vizi-plugin.json`, and checks that the manifest reads
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
  - **Runtime options:** each is written as
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
- **Where.** `--install=DIR` installs into another plugins directory, as `DIR/org.example.region_quant`.
- **Into a directory it did not write.** The installer marks the directories it writes with a
  `.vizi-java-sdk` file naming the SDK and the plugin. It refuses to write into an existing,
  non-empty `org.example.region_quant` directory without that mark, or whose mark names another plugin;
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

`vizi-plugin.json` here is the same manifest with the relative launch line `./region-quant`. That is
a POSIX development launcher for the drivers, and it runs the jar `./assemble` builds (the POSIX
shortcut for the build lines above). At launch the plugin warns if the file and
the code disagree anywhere but `entry`, whose launch line is meant to differ once installed.

The driver runs against an installed copy too: `uv run python
examples/region_quant/python/hostsim.py --plugin-dir <plugins dir>/org.example.region_quant`, from the repository root.

## The launch line

`vizi-plugin.json` is written from the code by `./region-quant --manifest`. It is SPEC.md §1's
manifest, except that `visible_rows: 8` is left out because it is the default:

```json
{
  "manifest_version": 1,
  "id": "org.example.region_quant",
  "name": "Region Quant",
  "version": "0.1.0",
  "engines": {
    "vizi": "1.0"
  },
  "entry": {
    "command": [
      "./region-quant"
    ]
  },
  "contributions": {
    "commands": [
      {
        "id": "quant.channel",
        "title": "Measure a channel",
        "menu": null
      },
      {
        "id": "quant.pick",
        "title": "Select a region",
        "menu": null
      },
      {
        "id": "quant.brightest",
        "title": "Go to the brightest frame",
        "menu": null
      },
      {
        "id": "quant.export",
        "title": "Export region means",
        "menu": null
      }
    ],
    "panels": [
      {
        "id": "quant",
        "title": "Region Quant",
        "view_targets": [
          "t",
          "select_region"
        ],
        "follow_targets": [
          "frame_region"
        ],
        "root": {
          "kind": "column",
          "id": "root",
          "children": [
            {
              "kind": "channel_picker",
              "id": "channel",
              "command": "quant.channel",
              "value": 0
            },
            {
              "kind": "list",
              "id": "regions",
              "row": [
                {
                  "kind": "label",
                  "id": "name",
                  "text": ""
                },
                {
                  "kind": "label",
                  "id": "voxels",
                  "text": ""
                },
                {
                  "kind": "label",
                  "id": "mean",
                  "text": ""
                },
                {
                  "kind": "image",
                  "id": "swatch"
                }
              ],
              "max_rows": 1024,
              "selectable": true,
              "command": "quant.pick",
              "moves_view": true
            },
            {
              "kind": "button",
              "id": "brightest",
              "command": "quant.brightest",
              "text": "Brightest frame",
              "moves_view": true
            },
            {
              "kind": "button",
              "id": "export",
              "command": "quant.export",
              "text": "Export CSV…"
            },
            {
              "kind": "label",
              "id": "tracing",
              "text": ""
            },
            {
              "kind": "label",
              "id": "status",
              "text": "Ready"
            }
          ]
        }
      }
    ]
  },
  "permissions": [
    "data_access"
  ],
  "subscriptions": [
    "view",
    "regions"
  ]
}
```

`./region-quant` never builds. It `exec`s `java … -jar build/plugin.jar`, so the process the host
started is the JVM that serves.

## Run against the emulator

From the repository root:

```sh
.venv/bin/python examples/region_quant/python/hostsim.py --plugin-dir examples/region_quant/java
```

It runs the checks of [`../SPEC.md`](../SPEC.md) and ends `host emulation passed`.

The shutdown check (a trace read parked mid-trace, an idle plugin, and the host vanishing) runs
beside it. Run it several times, since its failures are timing-dependent:

```
uv run python examples/region_quant/python/shutdown_check.py --plugin-dir examples/region_quant/java
```
