package vizi.plugin.javasdk.manifest;

import static org.junit.jupiter.api.Assertions.assertDoesNotThrow;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.OptionalInt;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** The model's builders and queries, the writer's canonical form, the diff, and strictness. */
class ManifestModelTest {
  @TempDir static Path dir;

  @BeforeAll
  static void icons() throws IOException {
    Path icons = Files.createDirectories(dir.resolve("icons"));
    Files.writeString(icons.resolve("tool.svg"), "<svg xmlns=\"http://www.w3.org/2000/svg\"/>");
    Files.write(icons.resolve("go.png"), ManifestFixturesTest.pngBytes());
  }

  private static final String SHA = "ab".repeat(32);

  /** A manifest using every contribution kind, every widget kind and every param type. */
  static Manifest everything() {
    return Manifest.builder("org.example.all", "All", "1.2.3")
        .entryCommand("java", "-jar", "plugin.jar")
        .reader(ReaderDecl.of("raw", "RAW", ".frames").withName("Raw frames"))
        .command(CommandDecl.of("run", "Run").withDescription("Run once").withKeybinding("Ctrl+R"))
        .command(CommandDecl.of("set", "Set").unlisted())
        .command(CommandDecl.of("fit", "Fit").withPane(PaneButton.of("icons/go.png").withTooltip("Fit")))
        .panel(
            PanelDecl.of(
                    "main",
                    "Controls",
                    WidgetDecl.column(
                        "root",
                        WidgetDecl.label("hint", "Adjust"),
                        WidgetDecl.row(
                            "r1",
                            WidgetDecl.slider("gain", "set").range(0, 10).step(0.5).value(2.5).continuous(true),
                            WidgetDecl.checkbox("on", "set").text("Enabled").value(true)),
                        WidgetDecl.dropdown("mode", "set", List.of("fast", "exact")).value("exact"),
                        WidgetDecl.lineEdit("note", "set").placeholder("a note").value("x"),
                        WidgetDecl.image("preview"),
                        WidgetDecl.progress("bar"),
                        WidgetDecl.button("go", "run", "Run")))
                .withMenuLabel("Example controls"))
        .exporter(ExporterDecl.of("csv", "CSV").withName("Stats"))
        .overlay(OverlayDecl.drawn("tracks", 64).withName("Tracks"))
        .overlay(OverlayDecl.drawn("dots"))
        .overlay(OverlayDecl.chunked("labels", OverlayDecl.TimeMode.STATIC))
        .overlay(OverlayDecl.chunked("seg", OverlayDecl.TimeMode.PER_T))
        .tool(ToolDecl.of("seeds").withViews(ToolDecl.View.THREE_D).withMenu(Menu.NONE))
        .tool(
            ToolDecl.of("polygon")
                .withName("Polygon")
                .withViews(ToolDecl.View.TWO_D)
                .withCancelOnCameraMove(false)
                .withPane(ToolPane.of("icons/tool.svg").withTooltip("Draw").withHold(Hold.MOMENTARY))
                .withTrigger(TriggerDecl.key("P"))
                .withTrigger(TriggerDecl.momentaryKey("Space"))
                .withTrigger(
                    TriggerDecl.mouse(
                        TriggerDecl.MouseButton.MIDDLE,
                        TriggerDecl.Modifier.SHIFT,
                        TriggerDecl.Modifier.CTRL))
                .withAction(ActionDecl.onKey("add_node", "N").withName("Add node"))
                .withAction(ActionDecl.onPane("close", PaneButton.of("icons/go.png"))))
        .tool(
            ToolDecl.of("mark")
                .withMode(ToolDecl.Mode.FIRE)
                .withTrigger(TriggerDecl.mouse(TriggerDecl.MouseButton.LEFT, TriggerDecl.Modifier.META)))
        .stage(
            StageDecl.of(
                    "blur",
                    StageParam.ofFloat("sigma", 2.0).withName("σ").withMin(0.1).withMax(50),
                    StageParam.ofInt("passes", 1).withMin(1),
                    StageParam.ofBool("clip", false),
                    StageParam.ofChoice("edge", "reflect", List.of("reflect", "constant")),
                    StageParam.ofString("label", "run 1").withMaxLength(64))
                .withName("Gaussian blur"))
        .stage(StageDecl.of("erode"))
        .asset(AssetDecl.manual("lut", "lut.bin", 1, SHA))
        .asset(
            AssetDecl.https("weights-v2", "weights.pt", 104857600, SHA.toUpperCase(), "https://x.org/w.pt")
                .withName("Weights"))
        .permissions(Permission.DATA_ACCESS, Permission.CONTRIBUTE_ROIS, Permission.DATA_ACCESS)
        .subscriptions(Subscription.EPOCHS)
        .build();
  }

  @Test
  void everyKindRoundTripsThroughTheWriter() throws ManifestException {
    Manifest m = everything();
    assertEquals(1, m.manifestVersion(), "tool triggers and a command pane need version 1");
    ParseResult r = ManifestParser.validate(m, dir);
    assertEquals(m, r.manifest());
    assertEquals(List.of(), r.warnings());
    String written = ManifestWriter.write(m);
    assertEquals(m, ManifestParser.parse(written, dir).manifest());
    assertEquals(written, ManifestWriter.write(ManifestParser.parse(written, dir).manifest()));
    assertTrue(written.endsWith("}\n"));
  }

  @Test
  void queriesTheRuntimeNeeds() {
    Manifest m = everything();
    Contributions c = m.contributions();
    assertEquals(List.of("raw"), c.readerIds());
    assertEquals(List.of(".raw", ".frames"), c.reader("raw").orElseThrow().extensions());
    assertTrue(c.reader("raw").orElseThrow().claims("/data/A.FRAMES"));
    assertEquals(List.of("run", "set", "fit"), c.commandIds());
    assertEquals(Menu.NONE, c.command("set").orElseThrow().menu());

    PanelDecl p = c.panel("main").orElseThrow();
    assertEquals(10, p.widgets().size());
    assertEquals(10, c.widgetCount());
    assertEquals(
        List.of("gain:set", "on:set", "mode:set", "note:set", "go:run"),
        p.interactiveWidgets().stream().map(w -> w.id() + ":" + w.command()).toList());
    assertEquals(WidgetDecl.Kind.PROGRESS, p.widget("bar").orElseThrow().kind());
    assertFalse(WidgetDecl.Kind.PROGRESS.interactive());

    assertEquals(List.of("labels", "seg"), c.chunkedOverlays().stream().map(OverlayDecl::id).toList());
    assertEquals(Optional.of(OverlayDecl.TimeMode.STATIC), c.overlay("labels").orElseThrow().timeMode());
    assertEquals(OptionalInt.of(32), c.overlay("dots").orElseThrow().segmentMib());
    assertEquals(OptionalInt.empty(), c.overlay("seg").orElseThrow().segmentMib());

    ToolDecl poly = c.tool("polygon").orElseThrow();
    assertEquals(OptionalInt.of(2), poly.actionIndex("close"));
    assertEquals("add_node", poly.action(1).orElseThrow().id());
    assertEquals(Optional.empty(), poly.action(0));
    assertEquals(Optional.empty(), poly.action(3));
    assertEquals(Hold.MOMENTARY, poly.triggers().get(1).hold());
    assertEquals(Hold.MOMENTARY, poly.triggers().get(2).hold(), "a mouse trigger is momentary");
    assertEquals(ToolDecl.DEFAULT_VIEWS, c.tool("mark").orElseThrow().views());

    StageParam sigma = c.stage("blur").orElseThrow().param("sigma").orElseThrow();
    assertEquals(StageParam.Type.FLOAT, sigma.type());
    StageParam.FloatParam f = (StageParam.FloatParam) sigma;
    assertEquals(OptionalDouble.of(50), f.max());
    assertTrue(f.admits(2.0));
    assertFalse(f.admits(Double.NaN));
    StageParam.StringParam label =
        (StageParam.StringParam) c.stage("blur").orElseThrow().param("label").orElseThrow();
    assertTrue(label.admits("é".repeat(32)));
    assertFalse(label.admits("é".repeat(33)), "64 UTF-8 bytes, not 64 characters");

    assertEquals(List.of(Permission.DATA_ACCESS, Permission.CONTRIBUTE_ROIS), m.permissions());
    assertTrue(m.has(Permission.CONTRIBUTE_ROIS));
    assertTrue(m.subscribes(Subscription.EPOCHS));
    assertEquals(List.of("lut", "weights-v2"), m.assetIds());
    assertEquals(SHA, m.asset("weights-v2").orElseThrow().sha256(), "stored lower-case");
  }

  @Test
  void theWrittenFormIsCanonical() throws ManifestException {
    Manifest m =
        Manifest.builder("org.example.min", "Min", "0.1")
            .entryCommand("python3", "plugin.py")
            .overlay(OverlayDecl.drawn("o"))
            .stage(StageDecl.of("s", StageParam.ofFloat("k", 2)))
            .build();
    String expected =
        """
        {
          "manifest_version": 1,
          "id": "org.example.min",
          "name": "Min",
          "version": "0.1",
          "engines": {
            "vizi": "1.0"
          },
          "entry": {
            "command": [
              "python3",
              "plugin.py"
            ]
          },
          "contributions": {
            "overlays": [
              {
                "id": "o"
              }
            ],
            "stages": [
              {
                "id": "s",
                "params": [
                  {
                    "key": "k",
                    "type": "float",
                    "value": 2.0
                  }
                ]
              }
            ]
          }
        }
        """;
    assertEquals(expected, ManifestWriter.write(m));
    assertEquals(m, ManifestParser.parseDetached(expected).manifest());
  }

  @Test
  void defaultsSpelledOutReadAsTheirAbsence() throws ManifestException {
    String explicit =
        """
        { "manifest_version": 1, "id": "x", "name": "X", "version": "1",
          "engines": { "vizi": "1.0" }, "entry": { "command": ["j"] },
          "contributions": {
            "readers": [ { "id": "r", "name": "r", "extensions": [".x"] } ],
            "commands": [ { "id": "c", "title": "C", "description": "", "menu": "tools", "default_keybinding": null } ],
            "overlays": [ { "id": "o", "segment_mib": 32, "chunked": false } ],
            "tools": [ { "id": "t", "views": ["3d", "2d"], "mode": "gesture", "cancel_on_camera_move": true,
                         "triggers": [], "actions": [] } ] } }
        """;
    String bare =
        """
        { "manifest_version": 1, "id": "x", "name": "X", "version": "1",
          "engines": { "vizi": "1.0" }, "entry": { "command": ["j"] },
          "contributions": {
            "readers": [ { "id": "r", "extensions": ["X"] } ],
            "commands": [ { "id": "c", "title": "C" } ],
            "overlays": [ { "id": "o" } ],
            "tools": [ { "id": "t" } ] } }
        """;
    Manifest a = ManifestParser.parseDetached(explicit).manifest();
    Manifest b = ManifestParser.parseDetached(bare).manifest();
    assertEquals(a, b);
    assertEquals(List.of(), ManifestDiff.differences(a, b));
  }

  @Test
  void theVersionIsOneUnlessPinned() {
    Manifest plain =
        Manifest.builder("x", "X", "1").entryCommand("j").tool(ToolDecl.of("t")).build();
    assertEquals(1, plain.manifestVersion(), "0 is a development artifact; a plugin says 1");
    Manifest pinned =
        Manifest.builder("x", "X", "1").entryCommand("j").manifestVersion(0).build();
    assertEquals(0, pinned.manifestVersion(), "a pinned 0 is still written as 0");
    Manifest fire =
        Manifest.builder("x", "X", "1")
            .entryCommand("j")
            .tool(ToolDecl.of("t").withMode(ToolDecl.Mode.FIRE))
            .build();
    assertEquals(1, fire.manifestVersion());
  }

  @Test
  void aBuiltManifestIsValidatedLikeAFile() {
    Manifest unbound =
        Manifest.builder("x", "X", "1")
            .entryCommand("j")
            .panel(PanelDecl.of("p", "P", WidgetDecl.button("b", "nothing", "B")))
            .build();
    ManifestException e =
        assertThrows(ManifestException.class, () -> ManifestParser.validate(unbound, null));
    assertTrue(e.getMessage().contains("button widget 'b' must bind a declared command"), e.getMessage());

    Manifest noCommand = Manifest.builder("x", "X", "1").build();
    e = assertThrows(ManifestException.class, () -> ManifestParser.validate(noCommand, null));
    assertTrue(e.getMessage().contains("entry.command must be a non-empty list of strings"));

    Manifest v0WithPane =
        Manifest.builder("x", "X", "1")
            .entryCommand("j")
            .manifestVersion(0)
            .command(CommandDecl.of("c", "C").withPane(PaneButton.of("icons/go.png")))
            .build();
    e = assertThrows(ManifestException.class, () -> ManifestParser.validate(v0WithPane, dir));
    assertTrue(e.getMessage().contains("pane needs manifest_version 1"), e.getMessage());
  }

  @Test
  void readersDeclareFallbackAndCompoundSuffixes() throws ManifestException {
    ReaderDecl nwb = ReaderDecl.of("nwb", "NWB").withFallback(".H5", "hdf5");
    ReaderDecl ome = ReaderDecl.of("ome", ".ome.tif");
    ReaderDecl h5 = new ReaderDecl("h5", "h5", List.of(), List.of(".h5"));
    Manifest m = Manifest.builder("x", "X", "1").entryCommand("j").reader(nwb).reader(ome).reader(h5).build();
    assertEquals(m, ManifestParser.validate(m, null).manifest());
    String written = ManifestWriter.write(m);
    assertTrue(written.contains("\"fallback_extensions\""), written);
    assertEquals(m, ManifestParser.parse(written, dir).manifest());
    assertEquals(List.of(".h5", ".hdf5"), nwb.fallbackExtensions());

    // longest declared suffix, case-insensitive, compound allowed, the name longer than the suffix
    assertEquals(Optional.of(".ome.tif"), ome.withFallback(".tif").match("/d/Cells.OME.TIF"));
    assertEquals(Optional.of(".tif"), ome.withFallback(".tif").match("/d/cells.tif"));
    assertEquals(Optional.of(".h5"), nwb.match("C:\\data\\run.H5"));
    assertEquals(Optional.empty(), ome.match("/d/.ome.tif"));
    assertEquals(Optional.empty(), ome.match("/d.ome.tif/x"));
    assertTrue(h5.claims("a.h5"));

    Manifest both = Manifest.builder("x", "X", "1").entryCommand("j").reader(ReaderDecl.of("r", ".h5").withFallback("H5", ".nwb", "x")).build();
    ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.validate(both, null));
    assertTrue(e.getMessage().contains("a suffix is in both extensions and fallback_extensions: ['.h5']"), e.getMessage());
    Manifest none = Manifest.builder("x", "X", "1").entryCommand("j").reader(new ReaderDecl("r", "r", List.of(), List.of())).build();
    e = assertThrows(ManifestException.class, () -> ManifestParser.validate(none, null));
    assertTrue(e.getMessage().contains("extensions must be non-empty"), e.getMessage());
  }

  @Test
  void iconFilesAreCheckedOnlyWithADirectory() throws ManifestException {
    Manifest m =
        Manifest.builder("x", "X", "1")
            .entryCommand("j")
            .command(CommandDecl.of("c", "C").withPane(PaneButton.of("icons/absent.svg")))
            .build();
    ManifestParser.validate(m, null);
    ManifestException e =
        assertThrows(ManifestException.class, () -> ManifestParser.validate(m, dir));
    assertTrue(e.getMessage().contains("not found in the plugin directory"), e.getMessage());
  }

  @Test
  void differencesArePathQualified() {
    Manifest declared = everything();
    Contributions c = declared.contributions();
    ToolDecl poly = c.tool("polygon").orElseThrow();
    ToolDecl swapped =
        new ToolDecl(
            poly.id(), poly.name(), poly.views(), poly.menu(), poly.mode(), poly.cancelOnCameraMove(),
            poly.triggers(), List.of(poly.actions().get(1), poly.actions().get(0)), poly.pane());
    List<CommandDecl> commands =
        List.of(CommandDecl.of("run", "Go"), c.commands().get(1), c.commands().get(2));
    Contributions changed =
        new Contributions(
            List.of(),
            commands,
            c.panels(),
            c.exporters(),
            c.overlays(),
            c.tools().stream().map(t -> t.id().equals("polygon") ? swapped : t).toList(),
            c.stages());
    Manifest onDisk =
        new Manifest(
            declared.manifestVersion(), declared.id(), declared.name(), "1.2.2", declared.engine(),
            declared.entryCommand(), declared.startupS(), changed, declared.assets(), declared.permissions(),
            declared.subscriptions());
    List<String> d = ManifestDiff.differences(declared, onDisk);
    String all = String.join("\n", d);
    assertTrue(all.contains("version: declared \"1.2.3\", on disk \"1.2.2\""), all);
    assertTrue(all.contains("contributions.readers['raw']: declared, absent on disk"), all);
    assertTrue(all.contains("contributions.commands['run'].title: declared \"Run\", on disk \"Go\""), all);
    assertTrue(all.contains("contributions.commands['run'].description: declared \"Run once\", absent on disk"), all);
    assertTrue(
        all.contains("contributions.tools['polygon'].actions: order differs: declared ['add_node', 'close'], on disk ['close', 'add_node']"),
        all);
    assertEquals(List.of(), ManifestDiff.differences(declared, everything()));
  }

  // ---- strictness: types are checked, not coerced ----

  private static final String HEAD =
      "{ \"manifest_version\": 0, \"id\": \"x\", \"name\": \"X\", \"version\": \"1\","
          + " \"engines\": { \"vizi\": \"1.0\" }, \"entry\": { \"command\": [\"j\"] }";

  private static String withContributions(String contributions) {
    return HEAD + ", \"contributions\": " + contributions + " }";
  }

  private static String slider(String fields) {
    return withContributions(
        "{ \"commands\": [ { \"id\": \"c\", \"title\": \"C\" } ], \"panels\": [ { \"id\": \"p\","
            + " \"title\": \"P\", \"root\": { \"kind\": \"slider\", \"id\": \"s\", \"command\": \"c\""
            + fields + " } } ] }");
  }

  private static String param(String p) {
    return withContributions("{ \"stages\": [ { \"id\": \"s\", \"params\": [ " + p + " ] } ] }");
  }

  private static void rejects(String json, String expected) {
    ManifestException e =
        assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(json));
    assertTrue(e.getMessage().contains(expected), () -> "wanted \"" + expected + "\": " + e.getMessage());
  }

  @Test
  void typesAreCheckedNotCoerced() throws ManifestException {
    rejects(HEAD.replace("\"manifest_version\": 0", "\"manifest_version\": 0.0") + " }", "field 'manifest_version' must be int");
    rejects(HEAD.replace("\"manifest_version\": 0", "\"manifest_version\": false") + " }", "field 'manifest_version' must be int");
    rejects(slider(", \"min\": true"), "slider 's' min must be numeric");
    rejects(slider(", \"max\": 1e400"), "slider 's' max must be a finite number");
    rejects(slider(", \"max\": " + "9".repeat(400)), "slider 's' max must be a finite number");
    rejects(param("{ \"key\": \"a\", \"type\": \"int\", \"value\": 1.0 }"), "numeric field 'value' must be an integer");
    rejects(param("{ \"key\": \"a\", \"type\": \"int\", \"value\": 1e2 }"), "numeric field 'value' must be an integer");
    rejects(param("{ \"key\": \"a\", \"type\": \"float\", \"value\": true }"), "numeric field 'value' must be a number");
    rejects(withContributions("{ \"overlays\": [ { \"id\": \"o\", \"segment_mib\": 64.0 } ] }"), "segment_mib must be an int in [1, 512]");
    rejects(
        withContributions(
            "{ \"commands\": [ { \"id\": \"c\", \"title\": \"C\" } ], \"panels\": [ { \"id\": \"p\", \"title\": \"P\","
                + " \"root\": { \"kind\": \"checkbox\", \"id\": \"b\", \"command\": \"c\", \"value\": \"yes\" } } ] }"),
        "checkbox 'b' value must be a boolean");
    rejects(withContributions("{ \"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"label\", \"id\": \"l\", \"min\": \"low\" } } ] }"),
        "label 'l' min must be numeric");
    // A float param takes an integer literal; the value is the same number.
    Manifest m = ManifestParser.parseDetached(param("{ \"key\": \"a\", \"type\": \"float\", \"value\": 3 }")).manifest();
    StageParam.FloatParam f = (StageParam.FloatParam) m.contributions().stages().get(0).params().get(0);
    assertEquals(3.0, f.value());
  }

  @Test
  void onlyStrictJsonIsRead() {
    rejects(slider(", \"max\": NaN"), "is not valid JSON");
    rejects(HEAD + " } trailing", "is not valid JSON");
    rejects(HEAD + ", } ", "is not valid JSON");
    rejects(HEAD.replace("\"name\"", "name") + " }", "is not valid JSON");
    rejects("﻿" + HEAD + " }", "is not valid JSON");
    rejects("[1, 2]", "the top level must be a JSON object");
  }

  @Test
  void aDuplicateKeyIsWarnedAndTheLastWins() throws ManifestException {
    ParseResult r = ManifestParser.parseDetached(HEAD + ", \"name\": \"Y\" }");
    assertEquals("Y", r.manifest().name());
    assertEquals(1, r.warnings().size());
    assertTrue(r.warnings().get(0).contains("duplicate key 'name'"), r.warnings().get(0));
  }

  @Test
  void unknownSubscriptionsAreWarnedAndIgnored() throws ManifestException {
    ParseResult r =
        ManifestParser.parseDetached(HEAD + ", \"subscriptions\": [\"epochs\", \"weather\"] }");
    assertEquals(List.of(Subscription.EPOCHS), r.manifest().subscriptions());
    assertTrue(r.warnings().get(0).contains("unknown subscription 'weather'"), r.warnings().toString());
  }

  @Test
  void engineRequirements() {
    assertEquals(new EngineRequirement("^1.0", 1, 0), EngineRequirement.parse("^1.0"));
    assertEquals(new EngineRequirement(">= 1.2", 1, 2), EngineRequirement.parse(">= 1.2"));
    assertEquals(new EngineRequirement("1.x", 1, 0), EngineRequirement.parse("1.x"));
    assertEquals(new EngineRequirement("1", 1, 0), EngineRequirement.parse("1"));
    assertTrue(EngineRequirement.parse("1.0").admitsCurrentProtocol());
    assertFalse(EngineRequirement.parse("1.1").admitsCurrentProtocol());
    assertFalse(EngineRequirement.parse("2.0").admitsCurrentProtocol());
    assertFalse(EngineRequirement.parse("0.9").admitsCurrentProtocol());
    for (String bad : List.of("", "1.", ".1", "x", "1.0.3", "1.*", "v1", "1.0-beta")) {
      IllegalArgumentException e =
          assertThrows(IllegalArgumentException.class, () -> EngineRequirement.parse(bad));
      assertTrue(e.getMessage().contains("is not a version"), bad);
    }
    assertThrows(IllegalArgumentException.class, () -> Manifest.builder("x", "X", "1").engine("1.*"));
  }

  @Test
  void readRefusesTextThatIsNotUtf8() throws IOException {
    Path d = Files.createDirectories(dir.resolve("latin1"));
    byte[] bytes = (HEAD.replace("\"X\"", "\"é\"") + " }").getBytes(StandardCharsets.ISO_8859_1);
    Files.write(d.resolve(ManifestParser.FILE_NAME), bytes);
    ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.read(d));
    assertTrue(e.getMessage().contains("is not UTF-8"), e.getMessage());
  }

  @Test
  void readParsesTheFileBesideTheDirectory() throws Exception {
    Path d = Files.createDirectories(dir.resolve("good"));
    Files.writeString(d.resolve(ManifestParser.FILE_NAME), ManifestWriter.write(everything()));
    Files.createDirectories(d.resolve("icons"));
    Files.copy(dir.resolve("icons/tool.svg"), d.resolve("icons/tool.svg"));
    Files.copy(dir.resolve("icons/go.png"), d.resolve("icons/go.png"));
    assertEquals(everything(), ManifestParser.read(d).manifest());
  }

  @Test
  void setViewDeclarationsRoundTrip() throws Exception {
    String json = withContributions("{ \"commands\": [ { \"id\": \"set\", \"title\": \"Set\" } ],"
        + " \"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"view_targets\": [\"frame_region\", \"t\"],"
        + " \"root\": { \"kind\": \"column\", \"id\": \"c\", \"children\": ["
        + " { \"kind\": \"image\", \"id\": \"trace\", \"command\": \"set\", \"moves_view\": true },"
        + " { \"kind\": \"list\", \"id\": \"l\", \"row\": [ { \"kind\": \"label\", \"id\": \"n\" } ],"
        + " \"selectable\": true, \"command\": \"set\", \"moves_view\": true },"
        + " { \"kind\": \"roi_picker\", \"id\": \"roi\", \"command\": \"set\", \"follows_selection\": true, \"filter\": \"mine\" } ] } } ],"
        + " \"tools\": [ { \"id\": \"jump\", \"view_targets\": [\"z\"] } ] }");
    Manifest m = ManifestParser.parseDetached(json).manifest();
    PanelDecl p = m.contributions().panels().get(0);
    assertEquals(List.of(ViewTarget.FRAME_REGION, ViewTarget.T), p.viewTargets());
    assertEquals(new WidgetDecl.Image("trace", "set", true), p.widget("trace").orElseThrow());
    assertTrue(p.widget("l").orElseThrow().movesView());
    var roi = (WidgetDecl.RoiPicker) p.widget("roi").orElseThrow();
    assertTrue(roi.followsSelection());
    assertEquals(WidgetDecl.RoiFilter.MINE, roi.filter());
    assertFalse(roi.movesView());
    assertEquals(List.of(ViewTarget.Z), m.contributions().tools().get(0).viewTargets());
    String written = ManifestWriter.write(m);
    assertEquals(m, ManifestParser.parseDetached(written).manifest());
    assertEquals(written, ManifestWriter.write(ManifestParser.parseDetached(written).manifest()));
  }

  @Test
  void setViewRejections() {
    String cmd = "\"commands\": [ { \"id\": \"set\", \"title\": \"Set\" } ], ";
    for (String[] c : new String[][] {
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"label\", \"id\": \"l\", \"moves_view\": true } } ]",
            "label 'l' cannot be moves_view"},
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"view_targets\": [\"t\"], \"root\": { \"kind\": \"image\", \"id\": \"i\", \"moves_view\": true } } ]",
            "image 'i' cannot be moves_view"},
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"view_targets\": [\"t\", \"t\"], \"root\": { \"kind\": \"label\", \"id\": \"l\" } } ]",
            "view_targets must be distinct names from"},
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"button\", \"id\": \"b\", \"command\": \"set\", \"moves_view\": 1 } } ]",
            "moves_view must be a boolean"},
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"color_picker\", \"id\": \"c\", \"command\": \"set\", \"filter\": \"all\" } } ]",
            "does not take follows_selection or filter"},
        {"\"tools\": [ { \"id\": \"jump\", \"view_targets\": [\"camera\"] } ]", "view_targets must be distinct names from"},
        {"\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"label\", \"id\": \"l\" } } ],"
            + " \"tools\": [ { \"id\": \"jump\", \"view_targets\": [\"t\"] } ]",
            "tool 'jump' declares view_targets but no panel does; the move-the-view row lives on the plugin's panel"},
    }) {
      ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(withContributions("{ " + cmd + c[0] + " }")));
      assertTrue(e.getMessage().contains(c[1]), e.getMessage());
    }
    assertThrows(IllegalArgumentException.class, () -> new WidgetDecl.Image("i", "", true));
    assertThrows(IllegalArgumentException.class, () -> WidgetDecl.list("l", WidgetDecl.label("n")).movesView(true));
  }
  /**
   * Selectable, multiple and follows_selection are refused on the wrong kind
   * only when true, as the host and the reference refuse them; filter by presence.
   */
  @Test
  void aBooleanKeyOnTheWrongKindIsRefusedOnlyWhenTrue() throws ManifestException {
    String cmd = "\"commands\": [ { \"id\": \"set\", \"title\": \"Set\" } ], ";
    for (String key : List.of("selectable", "multiple", "follows_selection")) {
      String root = "{ \"kind\": \"dropdown\", \"id\": \"d\", \"command\": \"set\", \"options\": [\"a\"], \"" + key + "\": ";
      ManifestParser.parseDetached(withContributions("{ " + cmd + "\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": "
          + root + "false } } ] }"));
      ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(withContributions(
          "{ " + cmd + "\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": " + root + "true } } ] }")));
      assertTrue(e.getMessage().contains("dropdown 'd' does not take " + key), e.getMessage());
    }
  }

  /** §3.5: overlays need a starter; a menu-less command counts only with a keybinding. */
  @Test
  void overlaysNeedSomethingThatStartsThePlugin() throws Exception {
    String ov = "\"overlays\": [ { \"id\": \"o\", \"chunked\": true } ]";
    // only what the host always offers counts; a keybinding or triggers alone do not
    for (String refused : new String[] {
        "{ " + ov + " }",
        "{ " + ov + ", \"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": null } ] }",
        "{ " + ov + ", \"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": null, \"default_keybinding\": \"Ctrl+Shift+O\" } ] }",
        "{ " + ov + ", \"tools\": [ { \"id\": \"t\", \"menu\": null } ] }"}) {
      ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(withContributions(refused)));
      assertTrue(e.getMessage().contains("overlays are drawn only while the plugin runs, and nothing declared here starts it;"
          + " declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)"), e.getMessage());
    }
    for (String starter : new String[] {
        "\"commands\": [ { \"id\": \"c\", \"title\": \"C\" } ]",
        "\"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": null, \"pane\": { \"icon\": \"i.svg\", \"tooltip\": \"Go\" } } ]",
        "\"panels\": [ { \"id\": \"p\", \"title\": \"P\", \"root\": { \"kind\": \"label\", \"id\": \"l\" } } ]",
        "\"tools\": [ { \"id\": \"t\" } ]"}) {
      // version 1: a pane button needs it
      ManifestParser.parseDetached(withContributions("{ " + ov + ", " + starter + " }").replace("\"manifest_version\": 0", "\"manifest_version\": 1"));
    }
    ManifestParser.parseDetached(withContributions("{ \"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": null } ] }"));
  }

  /** A list realises at most 4096 cells, in the parser and in code; a menu is typed first. */
  @Test
  void aListsCellsAreCappedAndAMenuIsTypedFirst() throws Exception {
    assertThrows(IllegalArgumentException.class, () -> WidgetDecl.list("l", WidgetDecl.label("a"), WidgetDecl.label("b"),
        WidgetDecl.label("c"), WidgetDecl.label("d"), WidgetDecl.label("e")).maxRows(1024));
    WidgetDecl.list("l", WidgetDecl.label("a"), WidgetDecl.label("b"), WidgetDecl.label("c"), WidgetDecl.label("d")).maxRows(1024);
    ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(withContributions(
        "{ \"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": [\"tools\"] } ] }")));
    assertTrue(e.getMessage().contains("not allowed (expected one of ['tools'])"), e.getMessage());
  }

  /** §3.3.1: a label's text_format; the value is checked before the kind. */
  @Test
  void aLabelsTextFormat() throws Exception {
    String panel = "{ \"commands\": [ { \"id\": \"c\", \"title\": \"C\", \"menu\": null } ], \"panels\": [ { \"id\": \"p\","
        + " \"title\": \"P\", \"root\": ";
    Manifest rich = ManifestParser.parseDetached(withContributions(panel
        + "{ \"kind\": \"label\", \"id\": \"a\", \"text\": \"<b>x</b>\", \"text_format\": \"rich\" } } ] }")).manifest();
    assertEquals(new WidgetDecl.Label("a", Optional.of("<b>x</b>"), WidgetDecl.TextFormat.RICH), rich.contributions().panels().get(0).root());
    assertTrue(ManifestWriter.write(rich).contains("\"text_format\": \"rich\""), "rich is written");
    Manifest plain = ManifestParser.parseDetached(withContributions(panel
        + "{ \"kind\": \"label\", \"id\": \"a\", \"text_format\": \"plain\" } } ] }")).manifest();
    assertEquals(WidgetDecl.TextFormat.PLAIN, ((WidgetDecl.Label) plain.contributions().panels().get(0).root()).textFormat());
    assertFalse(ManifestWriter.write(plain).contains("text_format"), "the default is not written out");
    for (String[] c : new String[][] {
        {"{ \"kind\": \"label\", \"id\": \"a\", \"text_format\": \"html\" }", "label 'a' text_format must be one of ['plain', 'rich']"},
        {"{ \"kind\": \"label\", \"id\": \"a\", \"text_format\": true }", "label 'a' text_format must be one of ['plain', 'rich']"},
        {"{ \"kind\": \"button\", \"id\": \"a\", \"command\": \"c\", \"text_format\": \"rich\" }",
            "button 'a' does not take text_format; only a label does"},
        {"{ \"kind\": \"button\", \"id\": \"a\", \"command\": \"c\", \"text_format\": \"html\" }",
            "button 'a' text_format must be one of ['plain', 'rich']"}}) {
      ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(withContributions(panel + c[0] + " } ] }")));
      assertTrue(e.getMessage().contains(c[1]), e.getMessage());
    }
  }

  /** Extensions checked whole before fallback_extensions; only ASCII whitespace is blank. */
  @Test
  void suffixChecksRunInOrderAndOnlyAsciiWhitespaceIsBlank() {
    String head = "{ \"manifest_version\": 1, \"id\": \"org.example.x\", \"name\": \"X\", \"version\": \"1\","
        + " \"engines\": { \"vizi\": \"1.0\" }, \"entry\": { \"command\": [\"x\"] }, \"contributions\": { \"readers\": [ ";
    java.util.function.Function<String, String> refused = reader -> assertThrows(ManifestException.class,
        () -> ManifestParser.parseDetached(head + reader + " ] } }")).getMessage();
    assertTrue(refused.apply("{ \"id\": \"r\", \"extensions\": [\".\"], \"fallback_extensions\": \".h5\" }")
        .contains("each extension must be a non-empty string"));
    for (String ws : java.util.List.of(" ", "\\t", "\\r", "\\n", "\\u000b", "\\f", ". \\t")) {
      String m = refused.apply("{ \"id\": \"r\", \"extensions\": [\".x\"], \"fallback_extensions\": [\"" + ws + "\"] }");
      assertTrue(m.contains("each fallback extension must be a non-empty string"), ws + ": " + m);
    }
    // a Unicode space (NBSP, U+2003) is not ASCII whitespace: a suffix like any other
    assertDoesNotThrow(() -> ManifestParser.parseDetached(head + "{ \"id\": \"r\", \"extensions\": [\".\\u00a0\", \".\\u2003\"] } ] } }"));
    // an exporter's extension takes the same rule
    String ex = head.replace("\"readers\"", "\"exporters\"");
    for (String bad : java.util.List.of(".", "  ", ". \\t")) {
      String m = assertThrows(ManifestException.class,
          () -> ManifestParser.parseDetached(ex + "{ \"id\": \"csv\", \"extension\": \"" + bad + "\" } ] } }")).getMessage();
      assertTrue(m.contains("extension must be non-empty"), bad + ": " + m);
    }
    assertDoesNotThrow(() -> ManifestParser.parseDetached(ex + "{ \"id\": \"csv\", \"extension\": \"\\u00a0\" } ] } }"));
  }

  // ---- entry.startup_s (§2.3) ----

  @Test
  void startupSecondsAreWrittenUnderEntryAndReadBack() throws ManifestException {
    for (var c : List.of(
        new Object[] {java.time.Duration.ofSeconds(120), "\"startup_s\": 120"},
        new Object[] {java.time.Duration.ofMillis(1500), "\"startup_s\": 1.5"})) {
      Manifest m =
          Manifest.builder("org.example.x", "X", "1").entryCommand("java", "-jar", "x.jar")
              .startup((java.time.Duration) c[0]).build();
      String json = ManifestWriter.write(m);
      assertTrue(json.contains((String) c[1]), json);
      assertTrue(json.indexOf("startup_s") > json.indexOf("\"command\""), json);
      assertEquals(m, ManifestParser.validate(m, null).manifest());
    }
    Manifest none = Manifest.builder("org.example.x", "X", "1").entryCommand("x").build();
    assertEquals(OptionalDouble.empty(), none.startupS());
    assertFalse(ManifestWriter.write(none).contains("startup_s"));
  }

  @Test
  void startupSecondsMustBePositiveNumbers() {
    for (String v : List.of("0", "-1", "0.0", "\"60\"", "true", "null", "[60]", "1e999")) {
      String json =
          "{\"manifest_version\": 1, \"id\": \"org.example.x\", \"name\": \"X\", \"version\": \"1\","
              + " \"engines\": {\"vizi\": \"1.0\"}, \"entry\": {\"command\": [\"x\"], \"startup_s\": " + v + "}}";
      var e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(json), v);
      assertTrue(e.getMessage().contains("entry.startup_s must be a positive number of seconds"), e.getMessage());
    }
  }

  // ---- a tool declares whether it wants the host's pick (§3.6) ----

  private static ToolDecl toolOf(String fields, int version) throws ManifestException {
    String json =
        "{\"manifest_version\": " + version + ", \"id\": \"org.example.x\", \"name\": \"X\", \"version\": \"1\","
            + " \"engines\": {\"vizi\": \"1.0\"}, \"entry\": {\"command\": [\"x\"]}, \"contributions\": {\"tools\": [ {\"id\": \"t\"" + fields + "} ]}}";
    return ManifestParser.parseDetached(json).manifest().contributions().tools().get(0);
  }

  @Test
  void aToolDeclaresWhetherItWantsTheHostsPick() throws ManifestException {
    // a v1 boolean, default true: every tool before the field existed wanted it
    assertTrue(toolOf("", 1).wantsPick());
    assertTrue(toolOf(", \"wants_pick\": true", 1).wantsPick());
    assertFalse(toolOf(", \"wants_pick\": false", 1).wantsPick());
    assertTrue(toolOf("", 0).wantsPick());
    for (String v : List.of("\"no\"", "0", "null", "[false]")) {
      var e = assertThrows(ManifestException.class, () -> toolOf(", \"wants_pick\": " + v, 1), v);
      assertTrue(e.getMessage().contains("wants_pick must be a boolean"), e.getMessage());
    }
    // under manifest_version 0 the field is refused, not dropped, at either value
    for (String v : List.of("false", "true")) {
      var e = assertThrows(ManifestException.class, () -> toolOf(", \"wants_pick\": " + v, 0), v);
      assertTrue(e.getMessage().contains("wants_pick needs manifest_version 1"), e.getMessage());
    }
  }

  @Test
  void wantsPickIsWrittenOnlyWhenDeclinedAndOnlyUnderVersionOne() throws ManifestException {
    assertTrue(ToolDecl.of("t").wantsPick());
    assertFalse(ToolDecl.of("t").needsVersion1());
    assertTrue(ToolDecl.of("t").withWantsPick(false).needsVersion1());
    // the constructors that predate the field want the pick
    assertTrue(new ToolDecl("t", "t", ToolDecl.DEFAULT_VIEWS, Menu.TOOLS, ToolDecl.Mode.GESTURE, true,
        List.of(), List.of(), Optional.empty()).wantsPick());
    assertTrue(new ToolDecl("t", "t", ToolDecl.DEFAULT_VIEWS, Menu.TOOLS, ToolDecl.Mode.GESTURE, true,
        List.of(), List.of(), Optional.empty(), List.of()).wantsPick());

    Manifest wants = Manifest.builder("org.example.x", "X", "1").entryCommand("x").tool(ToolDecl.of("t")).build();
    assertFalse(ManifestWriter.write(wants).contains("wants_pick"), "the default is not written");

    Manifest declines =
        Manifest.builder("org.example.x", "X", "1").entryCommand("x").tool(ToolDecl.of("t").withWantsPick(false)).build();
    assertEquals(1, declines.manifestVersion());
    String json = ManifestWriter.write(declines);
    assertTrue(json.contains("\"wants_pick\": false"), json);
    Manifest back = ManifestParser.validate(declines, null).manifest();
    assertEquals(declines, back);
    assertFalse(back.contributions().tools().get(0).wantsPick());
    assertEquals(List.of(), ManifestDiff.differences(declines, back));
    assertFalse(ManifestDiff.differences(wants, declines).isEmpty(), "declining the pick is a difference");

    // a manifest pinned to 0 cannot carry it
    Manifest v0 =
        Manifest.builder("org.example.x", "X", "1").entryCommand("x").manifestVersion(0)
            .tool(ToolDecl.of("t").withWantsPick(false)).build();
    var e = assertThrows(ManifestException.class, () -> ManifestParser.validate(v0, null));
    assertTrue(e.getMessage().contains("wants_pick needs manifest_version 1"), e.getMessage());
  }
}
