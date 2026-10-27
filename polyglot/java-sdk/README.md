# vizi plugin SDK for Java

A plugin-side SDK for vizi's plugin protocol `1.0`, in Java 25: the doorbell (v2 records over
a socketpair, FFM), shared memory (grants, Loaned arenas, Pulsed lanes, deposits, staged
slots), the gRPC control plane, the manifest, and an author API that makes the protocol's rules
hard to break. Protocol `1.0` hosts are vizi v1.0.0 and later.

**The wire protocol and the Python SDK carry the 1.0 stability promise; this Java API may still
change in a 1.x minor release.** The protocol it speaks is `1.0`, and a plugin built on it keeps working with later `1.x` hosts.

- Artifact: `org.focolab:vizi-sdk-java:1.0.0`. Package root: `vizi.plugin.javasdk`.
- Build: mill 1.1.3 (`build.mill`); protoc and the grpc-java plugin are fetched and run at
  build time, so the wire code is never committed.
- Every JVM running it needs `--enable-native-access=ALL-UNNAMED` and
  `--sun-misc-unsafe-memory-access=allow`. Test forks, the samples' launchers and `--install`
  all pass both.

## Build and test

```sh
mill sdk.compile
mill sdk.test                      # unit tests, the host emulator test, the bind probe
VIZI_V2_CORPUS=corpus.json mill sdk.test.testOnly vizi.plugin.javasdk.doorbell.CrossCheckTest
mill sdk.publishM2Local            # for jbang and the ports
```

`sdk.test` needs `VIZI_SDK_REPO` (the repository root), which `build.mill` sets for test forks.

## A plugin

```java
///usr/bin/env jbang "$0" "$@" ; exit $?
//JAVA 25+
//DEPS org.focolab:vizi-sdk-java:1.0.0

import java.util.List;
import vizi.plugin.javasdk.*;
import vizi.plugin.javasdk.manifest.*;

public class Probe {
  public static void main(String[] args) {
    var b = Plugin.builder("org.example.probe", "Probe", "0.1.0").entry("./run");
    Overlay marker = b.overlay(OverlayDecl.drawn("marker"));
    b.tool(ToolDecl.of("probe"), new Tool() {
      public void press(ToolContext ctx, InputEvent.Press ev) {
        ev.info().voxel().ifPresent(v -> marker.publishLive(ev.info(), List.of(Item.markers("m", v.z(), v.y(), v.x()))));
      }
    });
    b.command(CommandDecl.of("probe.clear", "Clear"), ctx -> { marker.clear(); return ""; });
    System.exit(PluginMain.run(b.build(), args));
  }
}
```

A plugin that is slow to start (a cold JVM, a heavy first start) can say how long it expects with
`.startup(Duration.ofSeconds(90))` on the builder, written as `entry.startup_s`. It is advisory: vizi
honors it within 10–300 s and gives 60 s without it, and nothing in the plugin may depend on it.

`PluginMain` gives every plugin these options:

- `--manifest[=DIR]` writes `vizi-plugin.json` from the code, and the pane icons it names, into
  `DIR` (default: the working directory).
- `--install[=DIR]` installs into `~/.vizi/plugins/<id>/` (or `$VIZI_PLUGINS_DIR`, or `DIR`). It
  copies the jar as `plugin-<16 hex digits of its sha256>.jar`, then the icons and any
  `Class-Path` jars, and writes the manifest last, so a failed install leaves the old one
  working; the jars it replaced are deleted where the system allows. The `.vizi-java-sdk` marker
  names the SDK and the plugin: a non-empty directory without it, or whose marker names another
  plugin, needs `--force`, and the installer updates its own install without it. It holds
  `.vizi-install-lock` while it writes; one an interrupted install left must be removed by hand.
  It takes a launcher:
  - `--launcher=path`, the default, pins this JVM by its absolute path.
  - `--launcher=jbang` writes `jbang --java 25+ …`, which pins the JDK by version. It names jbang
    bare when jbang is on `PATH`, and by its `~/.jbang/bin` path otherwise. It warms jbang up
    before writing the manifest.
- `--help` prints the options.

What `--install` and `--manifest` print:

- **Success** is one line on stdout, `INSTALLED: <id> <version> into <dir>`, followed by the launch line vizi will
  use.
- **A failure** starts with a headline on stderr, `INSTALL FAILED: <id> <version> was not installed into <dir>` (or
  `MANIFEST NOT WRITTEN: …`, or `FAILED: … the command line was not understood`). Then come, each on its own line:
  - `Why:`, the cause with the operation and the full path;
  - what was left behind;
  - `To fix:`, with the exact flags or commands.
- The exit code is 1, or 2 for a usage mistake.
- There is no stack trace. `VIZI_SDK_TRACE=1` adds one for an unexpected error.
- It refuses to install from class directories (`mill run`), and from a jar that doesn't carry its libraries. A
  single-file jbang plugin run as `jbang Plugin.java --install` is the second case: export it first with `jbang export
  portable --force -O build/plugin.jar Plugin.java`, then run `java … -jar build/plugin.jar --install`.

Plugin code finds its settings folder with `ctx.settingsDir()` (and `job.settingsDir()`): the
host's `VIZI_PLUGIN_SETTINGS_DIR`, created on first use, or empty when the host gave none.

With no argument, the plugin serves.

The ports' READMEs install with `jbang --java 25+ -R=… out.jar --install --launcher=jbang`, so jbang
is the only prerequisite. `example/voxel_probe` is the single-file jbang sample, checked against
`examples/voxel_probe/python/hostsim.py`; the exercise ports are under `examples/*/java/`, each
with its README.

## What the author API guarantees

- Declarations and handlers are registered together; the manifest is assembled from code and
  validated by the same validator the `manifest_schema.md` §8 fixtures test.
- Handlers answer by returning; `Refusal(kind, msg)` chooses the wire error; the SDK answers
  every host request exactly once.
- Threads are the SDK's: a host request from the doorbell listener or the dispatch thread throws
  at once, naming the fix; `ToolContext` has no host client.
- Pickers (`Host.requestAsset`, `requestSavePath`) work from a command, which then waits while the
  person browses. That command holds the session's one request slot meanwhile, so a plugin that
  also serves reads or other commands should ask from a job (`ctx.jobs().start(...)`, the
  asynchronous form) and let the command return at once. The SDK says so once per session.
- No loan reaches an author: pushes copy into the arena and return the space on `ReleaseLoan`;
  deposits are copied out and released before a call returns; a full arena throws
  `ArenaFullException` and sends `SegmentShortfall` (a chunk is deferred instead).
- Readers: `builder.reader(ReaderDecl.of(id, ".nwb"), (uri, dir) -> dataset)` opens every file as
  its one image. When one file can hold several images (an NWB file's image series, an OME-Zarr's
  numbered series, an OME-TIFF's images), register a `SeriesReader`, `(uri, dir, series) -> …`.
  With `series` empty, return the `Dataset`, or a `SeriesChoice` of `SeriesEntry(id, label)`s
  (shape and dtype when the listing knows them cheaply). The host shows the list to the person
  and opens again with the chosen id. With `series` present, open that one, and refuse an id you
  don't have with `Refusal.notFound`. The id is yours and opaque to the host: an HDF5 path, an
  index, a member name, even `""`. A choice of one entry is opened at once, not sent. A choice
  given an `openingWith(opener)` is held until the next open, so what you opened to list the
  file opens the choice; its `onClose` runs if it is retired unused (another open, ten idle
  minutes, the session's end). A list too long for one message is sent as a prefix marked
  `truncated`.
- A reader's suffixes may be compound (`.ome.tif`), and a name matches its longest. The host
  never reads a file to pick its reader: it chooses from declarations, and asks the person on a
  tie. `ReaderDecl.of("hdf5", ".hdf5").withFallback(".h5", ".nwb")` declares
  `fallback_extensions`, for a generic reader beside specific ones: it is offered for `.nwb` and
  can be made the default there, but is never picked for it unasked over a reader that declares
  `.nwb` in `extensions`.
- Channel names carry their source. `PaneContext.channels()` (a tool's geometry) and
  `Host.channels(viewState)` (every channel of the open dataset, empty with none open) are
  `ChannelName(index, name, source)`s. A name is never empty: a channel nothing named is shown as
  `"Channel 3"` with source `PLACEHOLDER`. Label a trace with any name, but never write a
  placeholder into a file as the channel's name, where it would read back as the file's; write a
  name only when `declared()` (`FILE` or `USER`). A source this SDK doesn't know is kept, and
  `sourceLabel()` shows its number. `Host.datasetSeries(viewState)` is empty for a file of one
  image and present, `""` included, when a series is open.
- Reads (`host.readVolume(t, c)`) return the source, unless the person shares a filter's output
  with plugins (*Plugins See Filtered Data*). Then they return that output, and
  `Host.filterOf(deposit.reply())` names the filter. `host.readRawVolume(t, c)` (or
  `ReadDataVolume.raw`) asks for the file's own voxels whatever is shared, for a plugin that
  must measure the source. Its reply names no filter, and `ViewState.filter` still says what the
  views show, for a plugin that records it.
- `Host.pitch(viewState)` reads the voxel pitch axis by axis. **Check `z()`, `y()` and `x()` for
  every axis you use before using its pitch: one known axis says nothing about the rest.** Each is
  empty exactly where that axis is not known, and positive and finite wherever it is present. A
  file that records X and Y but not Z gives Z empty: scale what is known and leave the rest in
  voxels. `allKnown()` means all three; `anyKnown()` means at least one, and does not license
  using every axis. A tool's `PaneContext.spacing()` reads `Geometry` instead, which
  stays finite and calls such a file `DISPLAY_FALLBACK`.
- A tool's 3D input arrives as a `Position.Ray`: `camera`, `far` and `entryFrac`. It carries the
  host's pick when the host made one: `picked()` is the front voxel of the structure it found,
  `front()` is that voxel on the ray, and `back()` is where the structure ends:
  `front + backFrac × (far − front)`. A `backFrac` of `0.0` is a real pick of no depth, its back at
  its front. NaN is different: nothing stood out, `pointPick()` is true and there is no back. Any
  3D record may come with or without a pick, whatever its phase: vizi picks on a
  press and a fire, but that is its policy, not the wire's. `info.ray()` is the ray either way and `info.pick()` is
  present only when a pick came. Without a pick, find the anchor yourself.
- `ToolDecl.withWantsPick(false)` (`wants_pick`, `manifest_version` 1) asks the host not to compute
  its pick for this tool's 3D presses. It is a request about what the host computes, not a
  guarantee about what arrives: a host that predates the field still sends a pick, and a host that
  picks still sends a bare ray when it found nothing. Handle both whichever you declared.
- A reader names its channels with `Info.channelNames("GCaMP", "", "tdTomato")`: by index, `""`
  for a channel it doesn't name, and the list may stop short. They win over the metadata
  document's names, and are `FILE` names from then on. More names than channels refuse the open
  (`INVALID_ARGUMENT`), and the reader open before it stays open.
- An exporter's `Exporter.Request` says what the artifact is and what it came from. What it is:
  the shape, the calibration (each value present only when the host knows it) and `channels()`,
  the artifact's channels in its C order, each with its source index and name. What it came
  from: `sourceMetadata()`, the SOURCE's own document, and the host's provenance record,
  `provenanceJson()` as it arrived and `provenance()` parsed (empty when there is none or it
  isn't a JSON object). The source's document describes the source, whose sizes, channels and
  pixel sizes a subset, crop or projection no longer has: embed it only as provenance, never as
  the artifact's description, and read no measurement from it. Write a channel's name only when
  it is `declared()`.
- Long work (§8.16). A command, a reader's open or read, and an exporter's chunk each hold the
  session's one request slot while they run, and the host shows them to the person, who can
  cancel. There is no deadline while they are shown, so report instead: `ctx.progress(0.4, "slice
  12 of 30")` in a command, `CurrentWork.progress(...)` in a reader or exporter (the host shows the
  latest report). Check `ctx.cancelled()` or `CurrentWork.cancelled()` between steps, and stop by
  throwing `Cancelled` ("stopped at step 3"), which answers `CANCELLED`. That is not a fault. Or
  answer with what you have. `Cancelled` is an `Error`, so a `catch (Exception e)` on the way out
  cannot swallow it. A request the host cancels before its handler starts is never run. A
  handler blocked on the host (a picker, a read) or on `jobs().await(...)` is woken by its
  cancel and answers `CANCELLED` at once; an awaited job is canceled with it.
- Work that should not hold the slot runs on a thread of the plugin's own, as a background task.
  A `Jobs` job is one: `job.progress(...)`, and the person's Cancel cancels it. Otherwise
  `ctx.startTask("Index the file")` returns a `Task` with `progress`, `cancelled()` and
  `done(outcome)`. It is `AutoCloseable`, and `task.run(t -> ...)` ends it with what the body
  returned or threw. At most 64 are open; the session's end ends the rest. Titles and texts are
  cut to 256 UTF-8 bytes.
- Panels keep a mirror. Every set is sent as it happens, open or not (the host keeps a closed
  panel's state), and the whole mirror is re-sent on every open, on a panel worker no
  command holds; the panel's `onPanelOpened` hook then runs on the command worker, never beside a
  command. Only an image waits: pushed while the panel has no `P` segment, it is held, and sent
  when `P` installs or at the next open, whichever comes first (vizi's relaunch of a crashed
  plugin grants `P` with no `PanelOpened`), and lent once for that open. Typed widget handles (including the host
  pickers: `channels`, `picked`) answer their own commands and push a refused value back. A
  `list`'s rows are `rows(id).set(keys)`, and its cells `rows(id).row(key).set(...)`.
- A `table` (§3.3.1) is declared `WidgetDecl.table(id, TableColumn.string("name"),
  TableColumn.number("area").precision(2).unit("µm²"), TableColumn.bool("kept"))`, with
  `.selectable(command).multiple(true).sortable(true).visibleRows(n)`. Its rows are data, up to
  100,000: `panel.setTable(id, rows)` states them whole and `panel.patchTable(id, upsert, delete)`
  changes them by key (deletes first, then each upsert in place or appended). The same is on the
  handle `panel.table(id)` (`set`, `patch`, `upsert`, `delete`, `keys`). A row is
  `TableRow.of(key, cells...)` in column order or `TableRow.byColumn(key, map)`, with cells a
  `String`, a number, a `Boolean` or `null` for blank. The SDK splits what is larger than one
  message into a `set_table` and appending `patch_table`s. Each message is at most 1 MiB and
  `Table.MAX_ROWS_PER_MESSAGE` (32,768) rows, and at most that many deleted keys, each counted
  on its own. A selectable table's handle answers
  its command (`{"rows": [keys]}`, `Value.strings`), `onSelect(keys -> …)` hears it, and
  `select(keys...)` sets it. A row that is deleted leaves the selection. A table is sent once, as
  it is set, and is never re-sent when the panel opens, since the host holds what it was sent.
- The outbox merges a table's `patch_table` into the `set_table` or `patch_table` pending for it
  while the widget's pending ops stay under 1 MiB and the row cap. Otherwise what is pending goes
  ahead and the patch queues behind it, never refused. A table's ops travel in a patch of their own.
- `builder.onWidgetGeometry(panel, (ctx, size) -> …)`, or `onWidgetGeometry(panel, image, …)` for
  one image, hears how large each `image` widget is drawn (`WidgetGeometry`, §8.4). The handler runs
  on the command worker, with the newest size when several arrive in a row. An image pushed at
  `size.pixels()` (`width_px × dpr`, rounded half to even) is shown 1:1. `panel.image(id).size()`
  is the last size heard. `ctx.strings(key)` and `Values.ofStrings` carry `Value.strings`.
- `builder.onAppearance((ctx, a) -> …)` hears the host's theme and panel colors (`HostAppearance`,
  §8.4): once after the session starts, before any panel opens, and again when the person switches
  theme. It runs on the command worker, with the newest when several arrive in a row. An
  `Appearance` has `theme()` (`"dark"` or `"light"`, and `dark()`), and `windowBg()`, `panelBg()`,
  `text()`, `textDisabled()`, `accent()` and `border()` as `Rgba(r, g, b, a)`, each 0–255;
  `hex("text")` gives one as `"#rrggbb"`, and `hexColors()` all six by wire name. Any handler reads
  the newest with `ctx.appearance()`, empty before the first, so a panel-open hook can draw in the
  theme without a handler of its own.
- A label shows its text as written, declared or set, so markup relayed from data is shown, not
  interpreted. `label(id, text).rich()` declares `text_format: "rich"`: Qt's rich-text subset,
  with links styled but never opened (§3.3.1).
- Set-view (§8.15): a `moves_view` widget's command carries a cause, answered with
  `ctx.moveView(ViewMove.to().t(430))` or `ctx.declineView()` (a typed handle's `onCause`). A
  cause left unanswered is declined by the SDK when the handler returns, and a move without a
  cause is refused before anything is sent. A tool answers a FIRE with `ctx.moveView(ev, move)`,
  which does not block. The host's answer is a `ViewResult`, never an exception. A cause carries
  its invocation (`commandId()`, `args()`, and `row()` for a list's click), and every
  auto-decline is logged, naming the command.
- Follow (§8.15): `panel.offerFollow(label)` offers it, and it is kept as panel state. When the
  person starts it, the driver registered with `builder.follow(panel, (ctx, f) -> …)` runs on a
  thread of its own. `f.move(...)` streams whole states and refuses undeclared targets. It
  returns false once the Follow ends, and so does `f.waitFor(...)`. `f.end()` finishes it, and
  returning or throwing from the driver ends it too.
- Region types a plugin shapes (§8.8, manifest §3.8). `builder.regionType(
  RegionTypeDecl.of("tetra.volume"), edit -> Shape.ok(positions))` declares a type (it needs
  `Permission.CONTRIBUTE_ROIS`); every region of it is asked. One `Shaper` serves the drag's
  proposals and the release's commit (`edit.commit()`), on a shape worker of its own that no
  command holds, one call at a time. Answer from the edit alone: it carries the whole region at
  rest, the handle, `p0` and `p1`. The SDK answers every commit (`REJECTED` with a message when
  there is no shaper, the shaper throws, or its result is not one finite position per vertex),
  stops proposing for a drag once its commit or end arrives, and hands a slow shaper the newest
  input rather than a queue; `edit.stale()` says when an answer is no longer wanted.
  `onShapeEnded` hears `COMMITTED`, `REVERTED` or `CANCELLED`. `Rois.pointCloud`,
  `freeHandle`, `radialHandle` and `scaleBindings` build generic regions that need no mesh.
- The `view` and `regions` subscriptions are handlers (`onViewChanged`, `onRegionsChanged`) on
  workers of their own: the newest view, and region pushes merged by region, never dropped.
- Stages: the SDK validates each slot header (§7), refuses missing inputs, holds the stage to its
  armed plan, and hands the turn on with the outcome and a `D`.
- Later `1.x` hosts. `host.protocol()` is the `ProtocolVersion` the host's `Hello` announced
  (empty before the handshake). The SDK answers with a `Ready` at the lower of that minor and
  its own, and `host.sessionProtocol()` is that negotiated version. A `Ready` below the
  manifest's `engines.vizi` (`Plugin.Builder.engine`, default `"1.0"`: the oldest host you
  tested) is refused at the handshake: *this plugin's manifest requires protocol 1.2 but its
  code speaks 1.0*. For anything newer, `host.supports(Features.SET_VIEW)` answers from the
  negotiated minor (the feature names are the same in every SDK); `host.require(feature)` is
  the call-site guard, throwing `FeatureUnavailableException` naming the feature, the minor it
  needs and the session's. A message of a kind this SDK does not know is never a reply: under
  the id of one of the plugin's requests in flight it faults the session (the request fails with
  `SessionEndedException`), and under any other id it is answered `UNSUPPORTED`. A `host_error` of a
  kind it does not know has `kind()` `UNRECOGNIZED` and keeps the host's number in `kindValue()`.
- On Windows every call must prove the session token before it becomes the session. A `1.0`
  host sends the token in `Hello.session_token`, and the SDK accepts a call whose first message
  is a `Hello` carrying the right one. A host MAY also send it as gRPC call metadata
  (`vizi-session-token`, `Server.SESSION_TOKEN_KEY`); when that key is present it is checked
  before the call is accepted, and a wrong one is refused. Both compares are constant-time. A call with a missing or wrong token is closed `UNAUTHENTICATED` and never
  ends the session; the plugin keeps accepting calls until the host's arrives.
- Permissions are not a sandbox. A plugin is trusted code that runs as the user; `data_access` is a
  capability declaration, not an isolation boundary, and vizi adds no gates that would imply one.
  Sandbox vizi and its plugins together if isolation is needed.

## Layout

```
sdk/src/vizi/plugin/javasdk/
  nativeio/   FFM: POSIX (Linux, Darwin layouts), Winsock, kernel32; the only downcalls
  doorbell/   records, CRC, M/E/D, the E interaction machine, faults, the listener
  memory/     segments, preamble, region tables, grants, arenas, Pulsed lanes, re-check
  manifest/   model, validator, writer, differences
  control/    gRPC server, handshake, dispatch, outbound queues, arrangements, shutdown
  (root)      Plugin, PluginMain, Overlay, ChunkedOverlay, Panel, Jobs, Host, Tool, Stage, ...
  testkit/    an in-process host (TestHost, HostSegment) for tests
```

The order is checked by `LayeringTest`: a package names only its own layer and those below.

## Not yet run anywhere

The Windows paths (loopback TCP, the port file, the token, `OpenFileMappingW`) compile, but no
Windows session has been served: CI's Windows job runs only the portable tests. The Darwin
layouts run in macOS CI.
