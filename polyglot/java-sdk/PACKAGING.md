# Packaging for the catalog

How a plugin on the Java SDK ships through vizi's plugin catalog, so that a person installs it
with one click and needs nothing on their machine but vizi. `catalog/` beside this page holds
the reference scripts: `scripts/bootstrap.py`, `scripts/doctor.py`, `locks/java-runtime.json`,
`tools/lock_runtime.py` and `tools/make_release.py`. Copy them into the plugin's repository.

## What vizi does, and what that leaves to the plugin

vizi's catalog install ([releasing](../../docs/releasing.md)):
1. downloads the release zip and checks its size and SHA-256 against the catalog entry;
2. unpacks it into the plugins folder;
3. installs the CPython minor version that `.python-version` names;
4. runs `scripts/bootstrap.py` on that Python, bare, in the plugin folder.

There is no other hook, and one zip serves every platform. A Java plugin therefore ships a
bootstrap that fetches a Java runtime for the platform it runs on, and `entry.command` launches
that runtime. Python is only the bootstrap's language: the plugin itself never runs on it.

jbang has no part in this. It is a developer convenience (`--install --launcher=jbang`), and
the catalog path does not depend on it or on any Java the person may have installed.

## The runtime: Eclipse Temurin JRE 25

The reference lock pins **Eclipse Temurin's JRE 25** (Eclipse Adoptium), the LTS feature
release the SDK requires. Temurin is chosen because:
- it is TCK-certified OpenJDK under the GPL v2 with the Classpath Exception, which allows
  redistribution;
- it publishes a JRE (not only a JDK) for all three catalog platforms;
- every archive has a stable, versioned GitHub release URL and a published SHA-256;
- Adoptium's API (`api.adoptium.net`) lists them, so the lock is generated, not hand-copied.

A JRE is enough: the plugin is compiled before release, and nothing compiles on the person's
machine. Azul Zulu and BellSoft Liberica meet the same bar and differ only in the lock's URLs.

| platform | archive | download | unpacked |
|---|---|---|---|
| `linux-x86_64` | `OpenJDK25U-jre_x64_linux_hotspot_25.0.5_8.tar.gz` | 61.7 MB | 192 MB |
| `macos-arm64` | `OpenJDK25U-jre_aarch64_mac_hotspot_25.0.5_8.tar.gz` | 56.3 MB | 190 MB |
| `windows-x86_64` | `OpenJDK25U-jre_x64_windows_hotspot_25.0.5_8.zip` | 58.6 MB | 188 MB |

`locks/java-runtime.json` pins each platform's archive by URL, size, SHA-256 and archive type.
`python3 tools/lock_runtime.py` writes it from Adoptium's API, or `--release jdk-25.0.5+8` for a
named build. It checks each pin against the `.sha256.txt` file published beside the archive.
Regenerating the lock is a dependency update, committed like any other; the bootstrap never
changes it.

A runtime fetched from its publisher's URL, pinned this way, is a dependency
[releasing](../../docs/releasing.md) clause 4 allows. To serve it from the plugin's own release
assets instead, attach the three archives to the plugin's release and point the lock's URLs at
them; the bootstrap is the same. So is a smaller runtime made with `jlink` from only the modules
the plugin uses: build it per platform in CI, attach it, and lock it.

## The bootstrap

`scripts/bootstrap.py` uses the standard library only and runs on CPython 3.11–3.13. vizi runs
it bare. It:
1. **Picks the lock entry** for this machine (`linux-x86_64`, `macos-arm64` or
   `windows-x86_64`). A platform with no entry is a failure that names the supported ones.
2. **Says what it downloads**: the runtime, its version, its size, where from and its license.
   Progress follows in tenths. vizi shows each line in the install's activity.
3. **Streams the archive into the plugin folder**, holding it to the pinned size as it arrives
   and checking its SHA-256 before anything is unpacked.
4. **Unpacks inside the plugin folder only.** It refuses absolute paths, `..`, links that leave
   the archive and special files, and uses tar's `data` filter where Python has it. The
   runtime's home (the folder holding `bin/java`, which is `Contents/Home` in the macOS
   archive) becomes `runtime/`, so the launch path is the same on every platform.
5. **Records** the platform, the pin and a digest of the unpacked files in
   `runtime/.vizi-runtime.json`.
6. **Checks the jars** against `lib/SHA256SUMS`, and **starts the runtime** with the SDK's flags
   (`java … -version`).

It is idempotent: a second run with the same lock downloads nothing and changes nothing. A
`runtime/` from another lock entry, or one with no record, is replaced, and the bootstrap says
so. Its scratch space (`.runtime-setup/`) is inside the plugin folder and is removed on
success and on failure. It writes nothing outside the plugin folder. On failure it exits
non-zero, and its last line is the cause.

A standalone CPython can start with an empty certificate store. The bootstrap then loads the
system's bundle (`/etc/ssl/cert.pem` on macOS, the distribution's bundle on Linux); Windows
uses its own store.

## The launch line

`entry.command` is one list for all three platforms:

```json
["runtime/bin/java", "--enable-native-access=ALL-UNNAMED", "--sun-misc-unsafe-memory-access=allow",
 "-jar", "lib/plugin.jar"]
```

vizi starts it in the plugin folder, so the relative path resolves there. On Windows it finds
`runtime\bin\java.exe` and starts it without a console window. Declare it in the code, so
`--manifest` writes it:

```java
static String[] launch() {
  var line = new ArrayList<String>(List.of("runtime/bin/java"));
  line.addAll(PluginMain.JVM_FLAGS);
  line.addAll(List.of("-jar", "lib/plugin.jar"));
  return line.toArray(String[]::new);
}
// Plugin.builder(ID, NAME, VERSION).entry(launch())
```

`--install`, the developer's path, writes its own launch line for the JVM that runs it; a
release ships the `--manifest` output. A cold JVM with a large classpath starts in about a
second. Declare `startup(...)` only if the plugin's own first start takes longer than vizi's
default.

## The doctor

`scripts/doctor.py` is read-only, runs on any Python 3.11 or later, and ends with one
ready / not-ready line. It checks:
- **The runtime:** the lock entry for this platform, the bootstrap's record, the unpacked files'
  digest (a runtime changed after setup fails), and `runtime/release`'s Java version.
- **The jars:** every jar `lib/SHA256SUMS` names is present and matches.
- **The manifest:** `manifest_version` 1, `engines.vizi` 1.x, and `entry.command` launching
  `runtime/bin/java` with both of the SDK's flags and a jar that is in `lib/`.
- **The JVM:** it starts with the flags.
- **The plugin:** its own `--manifest`, written to a temporary folder, is the shipped
  `vizi-plugin.json`. That proves the classpath, the SDK and the declarations together, and
  prints the SDK version.

## The release zip

One folder named for the plugin id, holding:

```text
<plugin id>/
  vizi-plugin.json          the plugin's --manifest output
  .python-version           the bootstrap's Python, for example 3.11
  scripts/bootstrap.py
  scripts/doctor.py
  locks/java-runtime.json
  lib/plugin.jar            the plugin's classes; its manifest's Class-Path names the jars below
  lib/<dependency>.jar      every runtime dependency, exactly as resolved, one file each
  lib/SHA256SUMS
  LICENSE  NOTICE  licenses/  README.md
```

**Ship the dependency jars unmerged, not as one fat jar.** A fat jar keeps one file of each
name, so the dependencies' own `META-INF/LICENSE` and `META-INF/NOTICE` files overwrite one
another. In the Bio-Formats plugin, 22 of 78 dependency jars carry such files, and its fat jar
keeps one of each. Unmerged jars:
- keep every notice where its license put it;
- keep the upstream bytes, so a reviewer can check each jar against its repository;
- make an update's diff the jars that changed.

`plugin.jar`'s `Class-Path` lists them in resolution order, which is the order the build tests
with.

**Licensing.** The zip is a distribution of every jar in it, so it must carry what each
license asks for:
- **The plugin's own license**, compatible with all of its dependencies. A plugin on
  Bio-Formats (`ome:formats-gpl`, GPL-3.0) is itself distributed under GPL-3.0, and the
  Apache-2.0 SDK and dependencies combine with it.
- **`licenses/`:** each dependency's coordinates, license and source location, taken from its
  POM (following parent POMs). Apache-2.0 dependencies that ship a NOTICE file need it
  reproduced, and their jars keep theirs.
- **The GPL's corresponding source:** the plugin repository at the release's tag, and each GPL
  dependency's sources, named by coordinates (Maven Central or OME's repository serve
  `-sources.jar`). `README.md` says where they are.

The Java runtime is not in the zip. The bootstrap fetches it from its publisher, and it carries
its own `legal/` folder.

**Reproducible.** Build the tree with mill, then pack it with `tools/make_release.py`:

```scala
// build.mill, in the plugin's module
def libJars = Task { resolvedRunMvnDeps() }
def manifest = Task { super.manifest().add("Class-Path" -> libJars().map(_.path.last).mkString(" ")) }
def releaseTree = Task {
  val lib = Task.dest / "lib"
  os.makeDir.all(lib)
  val names = libJars().map(_.path.last)
  require(names.distinct.size == names.size, "two dependency jars share a file name: " + names.diff(names.distinct))
  os.copy(jar().path, lib / "plugin.jar")
  libJars().foreach(p => os.copy(p.path, lib / p.path.last))
  PathRef(Task.dest)
}
```

```sh
./mill --no-daemon releaseTree
python3 tools/make_release.py --tree out/releaseTree.dest
```

`make_release.py`:
- packs only committed files and the built `lib/`;
- refuses uncommitted changes;
- runs the plugin's `--manifest` (with `$JAVA_HOME/bin/java`) and requires it to equal the
  committed `vizi-plugin.json`;
- writes `lib/SHA256SUMS`.

mill stamps `plugin.jar` with the build time, so the script rewrites it with its entries in a
fixed order, the earliest timestamp a zip records, and no compression. Dependency jars are
copied byte for byte. The zip is written the same way, so two builds of one commit, on one JDK,
give the same bytes. The catalog pins that SHA-256.

## What a person sees

For the Bio-Formats plugin, cold, on Linux:

| | download | on disk |
|---|---|---|
| the release zip (79 jars) | 73 MB | 70 MB |
| Python for the bootstrap, shared between plugins | 19–36 MB | kept in vizi's data folder |
| the Java runtime | 56–62 MB | 188–192 MB |
| total | about 150–170 MB | about 260 MB beside the shared Python |

The bootstrap takes about 3 s on a fast connection: the download, a 1–2 s unpack and hash, and
a JVM start. At 50 Mbit/s the whole install is about half a minute; at 10 Mbit/s, about two and
a half minutes. An update reinstalls into a fresh folder, so it downloads the runtime again.

## Checks before a release

- `python3 scripts/bootstrap.py` twice on a fresh unpack of the zip, then
  `python3 scripts/doctor.py`, on each of `linux-x86_64`, `macos-arm64` and `windows-x86_64`.
- The plugin under a host emulator or vizi, launched by its manifest's `entry.command`.
- Two clean builds of the tagged commit give the same zip SHA-256.
