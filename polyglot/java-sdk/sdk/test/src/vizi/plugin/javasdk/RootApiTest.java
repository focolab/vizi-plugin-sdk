package vizi.plugin.javasdk;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.time.Duration;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.LinkedBlockingQueue;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;
import pois_math.v1.RoiOuterClass.Roi;
import vizi.plugin.javasdk.control.HandlerFailure;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StageShape;
import vizi.plugin.v1.ViziPlugin.Value;

class RootApiTest {

  // ---- %g ----

  @Test
  void fmtMatchesPythonPercentG() {
    // expected values computed with Python's '%g' % v
    Object[][] cases = {
      {0.5, "0.5"}, {1e-5, "1e-05"}, {123456.0, "123456"}, {1234567.0, "1.23457e+06"}, {2.5, "2.5"}, {100.0, "100"},
      {0.0001, "0.0001"}, {1e16, "1e+16"}, {-3.14159265, "-3.14159"}, {2.675, "2.675"}, {0.1 + 0.2, "0.3"},
      {0.25, "0.25"}, {1.234e-5, "1.234e-05"}, {999999.5, "1e+06"}, {9999995.0, "1e+07"}, {1e100, "1e+100"},
      {5e-324, "4.94066e-324"}, {0.0, "0"}, {Double.NaN, "nan"}, {Double.POSITIVE_INFINITY, "inf"}
    };
    for (Object[] c : cases) assertEquals(c[1], Fmt.g((double) c[0]), "g(" + c[0] + ")");
  }

  // ---- install ----

  @Test
  void installIdsArePortable() {
    assertNull(PluginMain.idProblem("org.example.voxel_probe"));
    for (String bad : List.of("", "../x", "a/b", "con", "Con.x", "com1.plugin", "x.", ".x", "é", "a b")) {
      assertTrue(PluginMain.idProblem(bad) != null, bad);
    }
  }

  // ---- subscriptions and pickers ----

  static vizi.plugin.v1.ViziPlugin.RegionChange rc(String id, long rev, boolean deleted) {
    return vizi.plugin.v1.ViziPlugin.RegionChange.newBuilder().setRegionId(id).setRevision(rev).setDeleted(deleted)
        .setOrigin(deleted ? "" : "user").build();
  }

  @Test
  void regionPushesMergeByRegionAndAResetVoidsWhatCameBefore() {
    var a = vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(7).setReset(true)
        .addChanges(rc("r1", 1, false)).addChanges(rc("r2", 1, false)).build();
    var b = vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(7).addChanges(rc("r1", 2, false)).build();
    var c = vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(7).addChanges(rc("r2", 0, true))
        .addChanges(rc("r3", 4, false)).build();
    Subscriptions.RegionsChange m = Subscriptions.fold(List.of(a, b, c));
    assertTrue(m.reset());
    assertEquals(List.of(new Subscriptions.RegionChange("r1", 2, "user", false), new Subscriptions.RegionChange("r2", 0, "", true),
        new Subscriptions.RegionChange("r3", 4, "user", false)), m.changes());
    var d = vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(9).setReset(true).addChanges(rc("q", 1, false)).build();
    Subscriptions.RegionsChange after = Subscriptions.fold(List.of(a, b, d));
    assertEquals(9, after.openGeneration());
    assertEquals(List.of(new Subscriptions.RegionChange("q", 1, "user", false)), after.changes(), "a reset voids everything before it");
  }

  /** A ViewChanged carries the applied filter, absent for the file's own voxels. */
  @Test
  void aViewChangeCarriesTheAppliedFilter() {
    var none = Subscriptions.ViewChange.of(vizi.plugin.v1.ViziPlugin.ViewChanged.newBuilder().setOpenGeneration(1).build());
    assertEquals(Optional.empty(), none.filter());
    var f = vizi.plugin.v1.ViziPlugin.AppliedFilter.newBuilder().setPluginId("org.example.stab").build();
    var with = Subscriptions.ViewChange.of(vizi.plugin.v1.ViziPlugin.ViewChanged.newBuilder().setOpenGeneration(1).setFilter(f).build());
    assertEquals(Optional.of(f), with.filter());
  }

  @Test
  void aSubscriptionNeedsDataAccessAndIsDeclaredByItsHandler() {
    var b = Plugin.builder("org.example.t", "T", "1").onViewChanged((ctx, v) -> {});
    assertThrows(IllegalStateException.class, b::build);
    Plugin p = Plugin.builder("org.example.t", "T", "1").permissions(vizi.plugin.javasdk.manifest.Permission.DATA_ACCESS)
        .onViewChanged((ctx, v) -> {}).onRegionsChanged((ctx, r) -> {}).build();
    assertEquals(List.of(vizi.plugin.javasdk.manifest.Subscription.VIEW, vizi.plugin.javasdk.manifest.Subscription.REGIONS),
        p.manifest().subscriptions());
  }

  @Test
  void pickersAreAnsweredByTheSdk() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root",
        WidgetDecl.channelPicker("chs", "set_chs").multiple(true).value(0, 2),
        WidgetDecl.channelPicker("ch", "set_ch"),
        WidgetDecl.colorPicker("col", "set_col").value("#FF8800"),
        WidgetDecl.roiPicker("roi", "set_roi"))));
    for (String c : List.of("set_chs", "set_ch", "set_col", "set_roi")) b.command(CommandDecl.of(c, c));
    Panel.Channels chs = p.channels("chs");
    Panel.Channels ch = p.channels("ch");
    Panel.Picked col = p.picked("col");
    Panel.Picked roi = p.picked("roi");
    Plugin.Runtime rt = b.build().runtime();
    assertEquals(List.of(0, 2), chs.get());
    rt.invokeCommand("set_chs", Map.of("value", Values.ofInts(List.of(1, 3, 4))));
    assertEquals(List.of(1, 3, 4), chs.get());
    rt.invokeCommand("set_ch", Map.of("value", Values.of(5L)));
    assertEquals(Optional.of(5), ch.first());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("set_ch", Map.of("value", Values.ofInts(List.of(1, 2)))));
    assertEquals(Optional.of("#FF8800"), col.get());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("set_col", Map.of("value", Values.of("orange"))));
    rt.invokeCommand("set_roi", Map.of("value", Values.of("r17"), "name", Values.of("Nucleus")));
    assertEquals(Optional.of("r17"), roi.get());
    assertEquals("Nucleus", roi.name());
    assertEquals(Optional.of(List.of(1L, 2L)), Values.toJava(Values.ofInts(List.of(1, 2))));
  }

  // ---- lists ----

  /** Cause 0 (no dataset open, §8.15): a moves_view handle's onNoCause runs, and there is no cause to answer. */
  @Test
  void aCauselessInteractionRunsOnNoCause() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root",
        WidgetDecl.list("cells", label("name")).selectable("pick").movesView(true), WidgetDecl.list("plain", label("n")).selectable("plainpick")))
        .withViewTargets(vizi.plugin.javasdk.manifest.ViewTarget.SELECT_REGION));
    b.command(CommandDecl.of("pick", "Pick"));
    b.command(CommandDecl.of("plainpick", "Pick"));
    Panel.Rows rows = p.rows("cells");
    List<String> said = new ArrayList<>();
    rows.onCause(c -> said.add("cause"));
    rows.onNoCause(() -> said.add("no cause " + rows.selected().orElse("")));
    assertThrows(IllegalStateException.class, () -> p.rows("plain").onNoCause(() -> {}), "not moves_view");
    Plugin.Runtime rt = b.build().runtime();
    rows.set(List.of("a"));
    assertEquals("", rt.invokeCommand("pick", Map.of("row", Values.of("a")), 0));
    assertEquals(List.of("no cause a"), said, "the value is taken, and onNoCause says why nothing moved");
  }

  /** §8.5: a polyline sends "", a loop "loop", segments "segments" with an even point count. */
  @Test
  void theThreeLineTopologies() {
    byte[] rgba = {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16};
    var seg = Overlay.materialize(Item.segments("s", 0, 0, 0, 1, 1, 1, 2, 2, 2, 3, 3, 3).vertexColors(rgba), new Loans(null), null, true)
        .getLineSet();
    assertEquals("segments", seg.getTopology());
    assertEquals(List.of(4, 3), seg.getPositionsZyx().getShapeList());
    assertEquals(List.of(4, 4), seg.getColorsRgba().getShapeList(), "a color per vertex");
    assertEquals("", Overlay.materialize(Item.line("l", 0, 0, 0, 1, 1, 1, 2, 2, 2), new Loans(null), null, true).getLineSet().getTopology(),
        "a polyline is the default topology, sent as the empty string");
    assertEquals("loop", Overlay.materialize(Item.loop("o", 0, 0, 0, 1, 1, 1, 2, 2, 2), new Loans(null), null, true).getLineSet().getTopology());
    assertThrows(IllegalArgumentException.class, () -> Item.segments("s", 0, 0, 0, 1, 1, 1, 2, 2, 2), "an odd point count");
  }

  @Test
  void aListsRowsAndCells() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root",
        WidgetDecl.list("cells", label("name"), WidgetDecl.checkbox("on", "set_on")).maxRows(3).selectable("pick"))));
    b.command(CommandDecl.of("set_on", "On"), ctx -> ctx.row().orElse("?") + " " + ctx.flag("value").orElse(false));
    b.command(CommandDecl.of("pick", "Pick"));
    Panel.Rows rows = p.rows("cells");
    List<String> picks = new ArrayList<>();
    rows.onSelect(picks::add);
    assertThrows(IllegalArgumentException.class, () -> p.label("name"), "a cell has no whole-panel handle");
    Plugin plugin = b.build();
    assertEquals(1 + 1 + 2 * 3, plugin.manifest().contributions().panels().get(0).widgetCount());
    assertThrows(IllegalArgumentException.class, () -> rows.set(List.of("a", "b", "c", "d")), "more than max_rows");
    assertThrows(IllegalArgumentException.class, () -> rows.set(List.of("a", "a")));
    assertThrows(IllegalArgumentException.class, () -> rows.set(List.of("")));
    rows.set(List.of("a", "b"));
    rows.row("a").set("name", "Alpha").enabled("on", false);
    assertThrows(IllegalArgumentException.class, () -> rows.row("z"));
    assertThrows(IllegalArgumentException.class, () -> rows.row("a").set("cells", "x"));
    Plugin.Runtime rt = plugin.runtime();
    assertEquals("b true", rt.invokeCommand("set_on", Map.of("value", Values.of(true), "row", Values.of("b"))));
    assertEquals("", rt.invokeCommand("pick", Map.of("row", Values.of("b"))));
    assertEquals(Optional.of("b"), rows.selected());
    assertEquals(List.of("b"), picks);
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("pick", Map.of("row", Values.of("nope"))));
    rows.set(List.of("a"));
    assertEquals(Optional.empty(), rows.selected(), "a selection whose row left is cleared");
  }

  // ---- tables and widget geometry ----

  static WidgetDecl.Table statsTable() {
    return WidgetDecl.table("stats", WidgetDecl.TableColumn.string("name"), WidgetDecl.TableColumn.number("area").unit("µm²"),
        WidgetDecl.TableColumn.bool("kept"));
  }

  @Test
  void aTablesRowsSelectionAndRefusals() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", statsTable().selectable("pick").multiple(true),
        WidgetDecl.table("single", WidgetDecl.TableColumn.string("n")).selectable("one"))));
    b.command(CommandDecl.of("pick", "Pick"));
    b.command(CommandDecl.of("one", "One"));
    Panel.Table stats = p.table("stats");
    Panel.Table single = p.table("single");
    assertTrue(stats == p.table("stats"), "one handle per table");
    List<List<String>> picks = new ArrayList<>();
    stats.onSelect(picks::add);
    assertThrows(IllegalArgumentException.class, () -> p.table("root"), "not a table");
    Plugin.Runtime rt = b.build().runtime();

    // positional, by column id, and blank cells; a by-id row naming no such column is refused
    stats.set(TableRow.of("a", "Alpha", 1.5, true), TableRow.byColumn("b", Map.of("area", 2)), TableRow.of("c", "Gamma", null));
    assertEquals(List.of("a", "b", "c"), stats.keys());
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.byColumn("x", Map.of("volume", 1))));
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("x", "a", 1, true, "extra")), "more cells than columns");
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("x", List.of(1)), TableRow.of("x", "y")), "keys distinct");
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("")));
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("k".repeat(129))));
    stats.set(TableRow.of("😀".repeat(128))); // 128 characters, 256 UTF-16 units
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("x", new Object())));
    assertThrows(IllegalArgumentException.class, () -> stats.set(TableRow.of("x", Values.ofInts(List.of(1)))));
    assertEquals(List.of("😀".repeat(128)), stats.keys(), "a refused statement changes nothing");

    p.setTable("stats", List.of(TableRow.of("a", "A"), TableRow.of("b", "B"), TableRow.of("c", "C")));
    // the person selects two rows; the handle hears them and tracks them
    assertEquals("", rt.invokeCommand("pick", Map.of("rows", Values.ofStrings(List.of("a", "c")))));
    assertEquals(List.of(List.of("a", "c")), picks);
    assertEquals(List.of("a", "c"), stats.selected());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("pick", Map.of("rows", Values.ofStrings(List.of("zz")))));
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("pick", Map.of("rows", Values.of("a"))), "a list of keys, always");
    assertEquals(List.of("a", "c"), stats.selected(), "a refused selection changes nothing");

    // deletes go first, then upserts in place or appended; a key a patch deletes leaves the
    // selection even when the patch upserts it again: it is a fresh row
    stats.patch(List.of(TableRow.of("b", "B2"), TableRow.of("c", "C2"), TableRow.of("d", "D")), List.of("c"));
    assertEquals(List.of("a", "b", "c", "d"), stats.keys(), "c deleted, then upserted in order: appended fresh");
    assertEquals(List.of("a"), stats.selected(), "c was deleted and upserted in one patch, a fresh row: it left the selection");
    stats.select("a", "c");
    stats.delete("c");
    stats.upsert(TableRow.of("c", "C2"));
    assertEquals(List.of("a"), stats.selected(), "deleted by one patch and upserted by the next, c left the selection the same way");
    stats.patch(List.of(TableRow.of("a", "A2")), List.of("a"));
    assertEquals(List.of("b", "d", "c", "a"), stats.keys(), "a deleted and upserted moves to the end");
    assertEquals(List.of(), stats.selected(), "and starts unselected");
    stats.upsert(TableRow.of("a", "A"));
    assertEquals(List.of("b", "d", "c", "a"), stats.keys(), "an upsert of a held key is in place");
    stats.select("a");
    stats.upsert(TableRow.of("a", "A4"));
    assertEquals(List.of("a"), stats.selected(), "a row replaced in place keeps its selection");
    stats.patch(List.of(TableRow.of("a", "A3")), List.of("a"));
    assertEquals(List.of(), stats.selected(), "a row deleted and upserted in one patch is fresh, not selected");
    stats.select("a", "c");
    stats.delete("a");
    assertEquals(List.of("c"), stats.selected());
    stats.select("b", "d");
    assertEquals(List.of("b", "d"), stats.selected());
    assertThrows(IllegalArgumentException.class, () -> stats.select("nope"));

    // a single-selection table: one key, as Value.s on the wire
    single.set(TableRow.of("x", "X"), TableRow.of("y", "Y"));
    assertThrows(IllegalArgumentException.class, () -> single.select("x", "y"));
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("one", Map.of("rows", Values.ofStrings(List.of("x", "y")))));
    assertEquals("", rt.invokeCommand("one", Map.of("rows", Values.ofStrings(List.of("y")))));
    assertEquals(List.of("y"), single.selected());
    single.select();
    assertEquals(List.of(), single.selected());
  }

  /** A cleared selection is {@code {"rows": []}} as {@code Value.strings} with no entries; a {@code Value.ints}, even empty, is refused. */
  @Test
  void aClearedTableSelectionIsEmptyStringsAndIntsAreRefused() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", statsTable().selectable("pick").multiple(true),
        WidgetDecl.table("single", WidgetDecl.TableColumn.string("n")).selectable("one"),
        WidgetDecl.table("loose", WidgetDecl.TableColumn.string("n")).selectable("free").multiple(true))));
    List<Optional<List<String>>> heard = new ArrayList<>();
    b.command(CommandDecl.of("pick", "Pick"));
    b.command(CommandDecl.of("one", "One"));
    // a table with no handle: its command's handler reads the keys itself
    b.command(CommandDecl.of("free", "Free"), ctx -> {
      heard.add(ctx.strings("rows"));
      return "";
    });
    Panel.Table stats = p.table("stats");
    Panel.Table single = p.table("single");
    List<List<String>> picks = new ArrayList<>();
    stats.onSelect(picks::add);
    Plugin.Runtime rt = b.build().runtime();
    stats.set(TableRow.of("a", "A"), TableRow.of("b", "B"));
    single.set(TableRow.of("x", "X"));

    assertEquals("", rt.invokeCommand("pick", Map.of("rows", Values.ofStrings(List.of("a", "b")))));
    assertEquals(List.of("a", "b"), stats.selected());
    Value emptyStrings = Values.ofStrings(List.of());
    assertEquals("", rt.invokeCommand("pick", Map.of("rows", emptyStrings)));
    assertEquals(List.of(), stats.selected(), "the selection is cleared");
    assertEquals(List.of(List.of("a", "b"), List.of()), picks, "the handle hears the empty selection");

    single.select("x");
    assertEquals("", rt.invokeCommand("one", Map.of("rows", emptyStrings)));
    assertEquals(List.of(), single.selected(), "a single-selection table clears the same way");

    // Value.ints is never a list of keys, empty or not
    stats.select("a");
    for (Value ints : List.of(Values.ofInts(List.of()), Values.ofInts(List.of(0)))) {
      assertThrows(HandlerFailure.class, () -> rt.invokeCommand("pick", Map.of("rows", ints)), ints.toString());
      assertEquals(List.of("a"), stats.selected(), "a refused selection changes nothing");
    }
    assertEquals(List.of(List.of("a", "b"), List.of()), picks, "nothing heard for a refused selection");

    assertEquals("", rt.invokeCommand("free", Map.of("rows", emptyStrings)));
    assertEquals("", rt.invokeCommand("free", Map.of("rows", Values.ofInts(List.of()))));
    assertEquals(List.of(Optional.of(List.of()), Optional.empty()), heard,
        "ctx.strings: empty strings is the empty list, empty ints is none");
  }

  @Test
  void aTablePastItsBoundIsRefusedWhole() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", WidgetDecl.table("t", WidgetDecl.TableColumn.number("v"))));
    b.build();
    List<TableRow> rows = new ArrayList<>();
    for (int i = 0; i < WidgetDecl.Table.MAX_ROWS; i++) rows.add(TableRow.of("r" + i, i));
    p.setTable("t", rows);
    assertEquals(WidgetDecl.Table.MAX_ROWS, p.table("t").keys().size());
    assertThrows(IllegalArgumentException.class, () -> p.patchTable("t", List.of(TableRow.of("one more", 1)), List.of()));
    p.patchTable("t", List.of(TableRow.of("one more", 1)), List.of("r0")); // one out, one in
    p.patchTable("t", List.of(TableRow.of("r1", 2)), List.of("r1")); // deleted and appended: still 100,000
    assertEquals(WidgetDecl.Table.MAX_ROWS, p.table("t").keys().size());
    rows.add(TableRow.of("r-extra", 0));
    assertThrows(IllegalArgumentException.class, () -> p.setTable("t", rows));
    assertThrows(IllegalArgumentException.class, () -> p.setTable("t", List.of(TableRow.of("big", "x".repeat(1 << 20)))),
        "a row past one message");
  }

  /**
   * A table larger than one message goes as a set_table and appending patches, each at most 1 MiB
   * AND at most §9's 65,536 entries per repeated field (the host refuses a patch past either whole).
   */
  @Test
  void aLargeTableIsSplitIntoMessagesThatReplayToTheWhole() {
    List<vizi.plugin.v1.ViziPlugin.TableRow> rows = new ArrayList<>();
    for (int i = 0; i < 100_000; i++) {
      rows.add(vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey("row-" + i)
          .addCells(Values.of("region number " + i)).addCells(Values.of(i * 0.5)).build());
    }
    long budget = WidgetDecl.Table.MAX_PATCH_BYTES - 1024 - 1 - 1;
    var ops = Panel.tableOps("t", budget, rows, null);
    assertTrue(ops.size() > 1, "split: " + ops.size());
    assertTrue(ops.get(0).hasSetTable());
    assertEquals(rows, replayed(ops), "the host holds the whole table, in order");

    // 100,000 key-only rows with short keys fit in 1 MiB in two messages by bytes alone (one of
    // over 65,536 rows, which the host refuses whole): the row cap splits them, 32,768 a message
    List<vizi.plugin.v1.ViziPlugin.TableRow> keyOnly = new ArrayList<>();
    for (int i = 0; i < 100_000; i++) keyOnly.add(vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey(Integer.toString(i, 36)).build());
    var split = Panel.tableOps("t", budget, keyOnly, null);
    for (var op : split) {
      int n = op.hasSetTable() ? op.getSetTable().getRowsCount() : op.getPatchTable().getUpsertCount();
      assertTrue(n <= WidgetDecl.Table.MAX_ROWS_PER_MESSAGE, "at most 32,768 rows a message: " + n);
    }
    assertEquals(32_768, WidgetDecl.Table.MAX_ROWS_PER_MESSAGE);
    assertEquals(4, split.size(), "3 × 32,768 + 1,696");
    assertEquals(32_768, split.get(0).getSetTable().getRowsCount());
    assertEquals(keyOnly, replayed(split));

    // 40,000 deletes and a few upserts: the delete list is split by the same cap on its own,
    // deletes first, the last run of them sharing its message with the upserts
    List<String> deletes = new ArrayList<>();
    for (int i = 0; i < 40_000; i++) deletes.add("d" + i);
    var patch = Panel.tableOps("t", budget, rows.subList(0, 10), deletes);
    assertEquals(2, patch.size());
    assertEquals(32_768, patch.get(0).getPatchTable().getDeleteCount());
    assertEquals(0, patch.get(0).getPatchTable().getUpsertCount());
    assertEquals(40_000 - 32_768, patch.get(1).getPatchTable().getDeleteCount());
    assertEquals(10, patch.get(1).getPatchTable().getUpsertCount());
    // counted each on its own, not added: 32,768 deletes and 32,768 upserts fit one message
    List<vizi.plugin.v1.ViziPlugin.TableRow> full = keyOnly.subList(0, 32_768);
    var both = Panel.tableOps("t", budget, full, deletes.subList(0, 32_768));
    assertEquals(1, both.size(), "the larger field is 32,768: one message");
    assertEquals(32_768, both.get(0).getPatchTable().getDeleteCount());
    assertEquals(32_768, both.get(0).getPatchTable().getUpsertCount());
    var over = Panel.tableOps("t", budget, keyOnly.subList(0, 32_769), deletes.subList(0, 3));
    assertEquals(List.of(32_768, 1), over.stream().map(o -> o.getPatchTable().getUpsertCount()).toList());
    assertEquals(List.of(3, 0), over.stream().map(o -> o.getPatchTable().getDeleteCount()).toList());
    assertEquals(List.of(), Panel.tableOps("t", budget, List.of(), List.of()), "an empty patch sends nothing");
    assertEquals(1, Panel.tableOps("t", budget, List.of(), null).size(), "an empty table is a statement");
  }

  /** What the host holds after {@code ops}, each checked against one message's bounds. */
  private static List<vizi.plugin.v1.ViziPlugin.TableRow> replayed(List<vizi.plugin.v1.ViziPlugin.PatchOp> ops) {
    java.util.LinkedHashMap<String, vizi.plugin.v1.ViziPlugin.TableRow> host = new java.util.LinkedHashMap<>();
    for (var op : ops) {
      var patch = vizi.plugin.v1.ViziPlugin.PanelPatch.newBuilder().setPanelId("p").addOps(op).build();
      assertTrue(patch.getSerializedSize() <= WidgetDecl.Table.MAX_PATCH_BYTES, "one message: " + patch.getSerializedSize());
      assertNull(vizi.plugin.javasdk.control.Bounds.check(patch), "within §9's bounds");
      if (op.hasSetTable()) {
        host.clear();
        for (var r : op.getSetTable().getRowsList()) host.put(r.getKey(), r);
      } else {
        for (String k : op.getPatchTable().getDeleteList()) host.remove(k);
        for (var r : op.getPatchTable().getUpsertList()) host.put(r.getKey(), r);
      }
    }
    return List.copyOf(host.values());
  }

  @Test
  void widgetGeometryReachesItsHandlerAsTheNewest() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", WidgetDecl.image("plot"), WidgetDecl.image("thumb"), label("l"))));
    List<String> heard = new ArrayList<>();
    b.onWidgetGeometry("p", (ctx, s) -> heard.add("panel " + s.widgetId() + " " + s.pixels()));
    b.onWidgetGeometry("p", "plot", (ctx, s) -> heard.add("plot " + s.widthPx() + "x" + s.heightPx() + "@" + s.dpr() + " " + s.pixels()));
    assertThrows(IllegalArgumentException.class, () -> b.onWidgetGeometry("p", (ctx, s) -> {}), "one per panel");
    Plugin.Runtime rt = b.build().runtime();

    var g = vizi.plugin.v1.ViziPlugin.WidgetGeometry.newBuilder().setPanelId("p").setWidgetId("plot").setWidthPx(201).setHeightPx(100).setDpr(1.5);
    assertTrue(rt.widgetGeometry(g.build()));
    assertFalse(rt.widgetGeometry(g.setWidthPx(300).build()), "one already due reads the newest");
    rt.widgetGeometryHook("p", "plot");
    assertEquals(List.of("plot 300x100@1.5 Pixels[width=450, height=150]"), heard, "the newest ran, once");
    assertEquals(Optional.of(450), p.image("plot").size().map(s -> s.pixels().width()));
    assertTrue(rt.widgetGeometry(g.setWidgetId("thumb").setWidthPx(5).setHeightPx(3).setDpr(0).build()), "due again once it ran");
    rt.widgetGeometryHook("p", "thumb");
    assertEquals("panel thumb Pixels[width=5, height=3]", heard.get(1), "dpr 0 reads as 1; the panel's handler hears the rest");
    assertEquals(new WidgetSize.Pixels(2, 8), new WidgetSize("p", "w", 1, 3, 2.5).pixels(), "half to even: 2.5 → 2, 7.5 → 8");

    var bad = Plugin.builder("org.example.t", "T", "1");
    bad.panel(PanelDecl.of("p", "P", column("root", WidgetDecl.image("plot"), label("l"))));
    bad.onWidgetGeometry("p", "l", (ctx, s) -> {});
    assertThrows(IllegalStateException.class, bad::build, "only an image's size is reported");
    var none = Plugin.builder("org.example.t", "T", "1");
    none.panel(PanelDecl.of("p", "P", label("l")));
    none.onWidgetGeometry("q", (ctx, s) -> {});
    assertThrows(IllegalStateException.class, none::build, "an undeclared panel");
  }

  @Test
  void theAppearanceReachesItsHandlerAsTheNewest() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    List<String> heard = new ArrayList<>();
    List<Optional<Appearance>> read = new ArrayList<>();
    b.onAppearance((ctx, a) -> {
      heard.add(a.theme() + " " + a.dark() + " " + a.accent() + " " + a.hex("text"));
      read.add(ctx.appearance());
    });
    assertThrows(IllegalStateException.class, () -> b.onAppearance((ctx, a) -> {}), "one per plugin");
    Plugin.Runtime rt = b.build().runtime();
    assertEquals(Optional.empty(), rt.appearance(), "empty before the first");

    var m = vizi.plugin.v1.ViziPlugin.HostAppearance.newBuilder().setTheme("light").setWindowBg(0xF0F0F0FF).setPanelBg(0xFFFFFFFF)
        .setText(0x000000FF).setTextDisabled(0x7F7F7F80).setAccent(0x3399FFFF).setBorder(0xA0A0A0FF);
    assertTrue(rt.appearance(m.build()));
    assertFalse(rt.appearance(m.setTheme("dark").setAccent(0xFF8000C0).setText(0xE0E0E0FF).build()), "one already due reads the newest");
    rt.appearanceHook();
    assertEquals(List.of("dark true Rgba[r=255, g=128, b=0, a=192] #e0e0e0"), heard, "the newest ran, once");
    assertEquals(rt.appearance(), read.get(0));
    assertTrue(rt.appearance(m.build()), "due again once it ran");

    Appearance a = rt.appearance().orElseThrow();
    assertEquals(new Appearance.Rgba(127, 127, 127, 128), a.textDisabled());
    assertEquals(a.textDisabled(), a.color("text_disabled"));
    assertEquals(Appearance.COLORS, List.copyOf(a.hexColors().keySet()), "in the message's order");
    assertEquals("#f0f0f0", a.hexColors().get("window_bg"));
    assertThrows(IllegalArgumentException.class, () -> a.hex("highlight"));

    var none = Plugin.builder("org.example.t", "T", "1").build().runtime();
    assertFalse(none.appearance(m.build()), "no handler: kept, nothing queued");
    assertEquals("dark", none.appearance().orElseThrow().theme());
  }

  @Test
  void stringsAreAValueKindBothWays() {
    Value v = Values.ofStrings(List.of("a", "b"));
    assertEquals(Optional.of(List.of("a", "b")), Values.toJava(v));
    var ctx = new CommandContext(Plugin.builder("org.example.t", "T", "1").build().runtime(), "c", Map.of("rows", v));
    assertEquals(Optional.of(List.of("a", "b")), ctx.strings("rows"));
    assertEquals(Optional.of(List.of("a", "b")), ctx.value("rows"));
    assertEquals(Optional.empty(), ctx.strings("none"));
  }

  /**
   * A click naming a row just removed is refused while a worker re-states the
   * rows under its own lock, as region_quant's does. The refusal
   * must not take the panel's lock inside the list's, the reverse of every cell op.
   */
  @Test
  void aClickOnARemovedRowDuringRowChurnNeverDeadlocks() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", WidgetDecl.list("cells", label("name")).selectable("pick"))));
    b.command(CommandDecl.of("pick", "Pick"));
    Panel.Rows rows = p.rows("cells");
    Plugin.Runtime rt = b.build().runtime();
    Object lock = new Object();
    var stop = new java.util.concurrent.atomic.AtomicBoolean();
    var refused = new java.util.concurrent.atomic.AtomicLong();
    Thread worker = Thread.ofPlatform().daemon().name("churn").start(() -> {
      for (int i = 0; !stop.get(); i++) {
        synchronized (lock) {
          List<String> keys = i % 2 == 0 ? List.of("a", "gone") : List.of("a");
          rows.set(keys);
          for (String k : keys) rows.row(k).set("name", k + i);
        }
      }
    });
    Thread clicker = Thread.ofPlatform().daemon().name("clicks").start(() -> {
      while (!stop.get()) {
        try {
          rt.invokeCommand("pick", Map.of("row", Values.of("never")));
        } catch (HandlerFailure e) {
          refused.incrementAndGet();
        } catch (Exception e) {
          throw new AssertionError(e);
        }
      }
    });
    Thread.sleep(1500);
    stop.set(true);
    worker.join(2000);
    clicker.join(2000);
    long[] dead = java.lang.management.ManagementFactory.getThreadMXBean().findDeadlockedThreads();
    assertFalse(worker.isAlive() || clicker.isAlive(), "deadlocked: " + (dead == null ? "no monitor cycle" : dead.length + " threads"));
    assertTrue(refused.get() > 100, "the refusal ran: " + refused.get());
  }

  // ---- ROI builder ----

  @Test
  void ellipsoidArrangement() {
    Roi r = Rois.ellipsoid(10, 20, 30, 2, 3, 4);
    assertEquals("ellipsoid", r.getTypeName());
    assertEquals(4, r.getVerticesCount());
    assertEquals(12, r.getEdgesCount());
    assertEquals(18, r.getCompile().getNumDisplay());
    double[][] d = Rois.compileDisplay(r);
    double[][] tips = {{12, 20, 30}, {8, 20, 30}, {10, 23, 30}, {10, 17, 30}, {10, 20, 34}, {10, 20, 26}};
    for (int i = 0; i < 6; i++) assertArrayEquals(tips[i], d[i], 1e-12, "tip " + i);
    assertEquals(12, d[6][0], 1e-12); // first ZY ring corner: +Z +Y
    assertEquals(23, d[6][1], 1e-12);
    for (var e : r.getEdgesList()) assertEquals(Rois.ARC_WEIGHT, e.getWeight());
    Roi posed = Rois.ellipsoid(1, 1, 1, 1, 1, 1, new double[][] {{0, 0, 0}, {1, 2, 3}});
    assertEquals(0, posed.getTStart());
    assertEquals(1, posed.getTEnd());
    assertEquals(6, posed.getPoseTranslationCount());
  }

  // ---- the builder ----

  private static PanelDecl panel() {
    return PanelDecl.of("p", "P", column("root",
        label("status", "hi"),
        WidgetDecl.slider("gain", "set_gain").range(0, 10).step(0.5).continuous(true),
        WidgetDecl.slider("count", "set_count").range(0, 10),
        WidgetDecl.lineEdit("note", "set_note"),
        WidgetDecl.dropdown("mode", "set_mode", List.of("fast", "exact")).value("exact"),
        button("go", "run", "Go")));
  }

  /** §3.3.1: a panel declared in code carries a rich label through build's validation. */
  @Test
  void aRichLabelDeclaredInCode() {
    var b = Plugin.builder("org.example.t", "T", "1");
    b.panel(PanelDecl.of("p", "P", column("root", label("plain", "<b>as written</b>"), label("rich", "<b>bold</b>").rich())));
    var root = (WidgetDecl.Column) b.build().manifest().contributions().panels().get(0).root();
    assertEquals(WidgetDecl.TextFormat.PLAIN, ((WidgetDecl.Label) root.children().get(0)).textFormat());
    assertEquals(WidgetDecl.TextFormat.RICH, ((WidgetDecl.Label) root.children().get(1)).textFormat());
  }

  /** A handler answering a checkbox's command itself sets the box by id; only a checkbox takes a bool. */
  @Test
  void aCheckboxIsSetByIdWithABool() {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root",
        WidgetDecl.checkbox("on", "set_on").text("On").value(true),
        WidgetDecl.lineEdit("note", "set_note"))));
    b.command(CommandDecl.of("set_on", "On"), ctx -> "");
    b.command(CommandDecl.of("set_note", "Note"), ctx -> "");
    b.build();
    p.setValue("on", false);
    p.setValue("note", "3");
    assertThrows(IllegalArgumentException.class, () -> p.setValue("note", true));
    assertThrows(IllegalArgumentException.class, () -> p.setValue("nope", true));
  }

  @Test
  void builderSealsAndValidates() {
    var b = Plugin.builder("org.example.t", "T", "1").entry("./run");
    b.command(CommandDecl.of("run", "Run"), ctx -> "ran");
    Plugin p = b.build();
    assertEquals("org.example.t", p.manifest().id());
    assertThrows(IllegalStateException.class, () -> b.command(CommandDecl.of("x", "X"), ctx -> ""));
    assertThrows(IllegalStateException.class, b::build);
  }

  @Test
  void aCommandWithNoOwnerIsRefused() {
    var b = Plugin.builder("org.example.t", "T", "1");
    b.command(CommandDecl.of("orphan", "O"));
    assertThrows(IllegalStateException.class, b::build);
  }

  @Test
  void aChunkedOverlayNeedsItsSource() {
    var b = Plugin.builder("org.example.t", "T", "1");
    assertThrows(IllegalArgumentException.class, () -> b.overlay(OverlayDecl.chunked("c", OverlayDecl.TimeMode.PER_T)));
    b.chunkedOverlay(OverlayDecl.chunked("c", OverlayDecl.TimeMode.PER_T), (ctx, w) -> ChunkedOverlay.Chunk.empty());
    assertThrows(IllegalArgumentException.class, () -> b.overlay(OverlayDecl.drawn("c")));
  }

  @Test
  void aWidgetCommandHasOneOwner() {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(panel());
    b.command(CommandDecl.of("set_gain", "g"), ctx -> "");
    for (String c : List.of("set_count", "set_note", "set_mode", "run")) b.command(CommandDecl.of(c, c), ctx -> "");
    p.number("gain");
    assertThrows(IllegalStateException.class, b::build); // a handler AND a widget handle
  }

  @Test
  void widgetsAreAnsweredByTheSdk() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    Panel p = b.panel(panel());
    for (String c : List.of("set_gain", "set_count", "set_note", "set_mode")) b.command(CommandDecl.of(c, c));
    b.command(CommandDecl.of("run", "Run"), ctx -> "ran");
    List<Double> gains = Collections.synchronizedList(new ArrayList<>());
    Panel.Number gain = p.number("gain", v -> v > 8 ? "too loud" : null);
    gain.onChange(gains::add).reportTo(p.label("status"));
    Panel.Whole count = p.integer("count");
    Panel.Number note = p.number("note");
    Panel.Choice mode = p.choice("mode");
    Plugin plugin = b.build();
    Plugin.Runtime rt = plugin.runtime();

    assertEquals("", rt.invokeCommand("set_gain", Map.of("value", Value.newBuilder().setF(2.5).build())));
    assertEquals(2.5, gain.get());
    assertEquals(List.of(2.5), gains);
    HandlerFailure refused = assertThrows(HandlerFailure.class,
        () -> rt.invokeCommand("set_gain", Map.of("value", Value.newBuilder().setF(9).build())));
    assertEquals("too loud", refused.getMessage());
    assertEquals(2.5, gain.get(), "a refused value leaves the old one");

    rt.invokeCommand("set_count", Map.of("value", Value.newBuilder().setF(3.0).build()));
    assertEquals(3, count.get());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("set_count", Map.of("value", Value.newBuilder().setF(3.5).build())));

    rt.invokeCommand("set_note", Map.of("value", Value.newBuilder().setS(" 1.25 ").build()));
    assertEquals(1.25, note.get());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("set_note", Map.of("value", Value.newBuilder().setS("abc").build())));

    assertEquals(Optional.of("exact"), mode.get());
    rt.invokeCommand("set_mode", Map.of("index", Value.newBuilder().setI(0).build()));
    assertEquals(Optional.of("fast"), mode.get());
    assertThrows(HandlerFailure.class, () -> rt.invokeCommand("set_mode", Map.of("value", Value.newBuilder().setS("slow").build())));
    mode.options(List.of("exact", "slow"));
    assertEquals(Optional.empty(), mode.get(), "a selection not in the new list is cleared");

    assertEquals("ran", rt.invokeCommand("run", Map.of()));
    HandlerFailure nf = assertThrows(HandlerFailure.class, () -> rt.invokeCommand("nope", Map.of()));
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, nf.kind());
  }

  @Test
  void refusalsChooseTheKind() {
    var b = Plugin.builder("org.example.t", "T", "1");
    b.command(CommandDecl.of("c", "C"), ctx -> {
      throw Refusal.outOfBounds("far");
    });
    Plugin.Runtime rt = b.build().runtime();
    HandlerFailure f = assertThrows(HandlerFailure.class, () -> rt.invokeCommand("c", Map.of()));
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.OUT_OF_BOUNDS, f.kind());
    assertEquals("far", f.getMessage());
  }

  // ---- jobs ----

  @Test
  void jobsRefuseQueueAndReport() throws Exception {
    var b = Plugin.builder("org.example.t", "T", "1");
    LinkedBlockingQueue<Jobs.Outcome> outcomes = new LinkedBlockingQueue<>();
    List<Boolean> edges = Collections.synchronizedList(new ArrayList<>());
    Jobs jobs = b.jobs(outcomes::add).queue(1).onBusy(edges::add);
    b.build();
    CountDownLatch release = new CountDownLatch(1);
    assertEquals("first", jobs.start("first", j -> {
      release.await();
      return "done first";
    }));
    jobs.start("second", j -> {
      throw new IllegalStateException("broke");
    });
    Refusal busy = assertThrows(Refusal.class, () -> jobs.start("third", j -> ""));
    assertTrue(busy.getMessage().contains("first"), busy.getMessage());
    release.countDown();
    Jobs.Outcome a = outcomes.poll(5, TimeUnit.SECONDS);
    Jobs.Outcome c = outcomes.poll(5, TimeUnit.SECONDS);
    assertEquals(new Jobs.Outcome("first", Jobs.Status.DONE, "done first"), a);
    assertEquals(Jobs.Status.FAILED, c.status());
    assertEquals("second failed: broke", c.text());
    waitFor(() -> edges.size() == 2);
    assertEquals(List.of(true, false), edges);

    jobs.start("spin", j -> {
      while (true) {
        j.checkpoint();
        Thread.sleep(5);
      }
    });
    jobs.cancelAll();
    assertEquals(Jobs.Status.CANCELLED, outcomes.poll(5, TimeUnit.SECONDS).status());
    assertEquals("x", jobs.await("quick", j -> "x", Duration.ofSeconds(5)));
  }

  private static void waitFor(java.util.function.BooleanSupplier c) throws InterruptedException {
    long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
    while (!c.getAsBoolean() && System.nanoTime() < end) Thread.sleep(5);
    assertTrue(c.getAsBoolean());
  }

  // ---- events ----

  @Test
  void positionsDecode() {
    InputRecord.Builder r = InputRecord.builder();
    assertInstanceOf(Position.None.class, Events.position(r.none().build()));
    float[] pos = new float[InputRecord.POSITION_VALUES];
    pos[0] = 1;
    pos[1] = 2;
    pos[2] = 3;
    pos[3] = 4;
    InputRecord col = new InputRecord(1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, InputRecord.KIND_COLUMN_ZX, 0, 0, pos);
    Position.Column c = assertInstanceOf(Position.Column.class, Events.position(col));
    assertEquals(Position.Column.Axis.Y, c.axis());
    assertEquals(3, c.min());
    InputRecord odd = new InputRecord(1, 0, 1, 0, 1, 0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 42, 0, 0, pos);
    assertEquals(new Position.Unknown(42), Events.position(odd));
  }

  @Test
  void aPickCarriesItsBackOrSaysItIsAPoint() {
    float[] camera = {0, 0, 0}, far = {10, 20, 40}, at = {5, 10, 20};
    Position.Ray deep = assertInstanceOf(Position.Ray.class, Events.position(InputRecord.builder()
        .rayAndPicked(camera, far, 0.25f, 0f, at, 0.75f).build()));
    assertEquals(Optional.of(new Position.Voxel(5, 10, 20)), deep.picked());
    assertEquals(0.75, deep.backFrac());
    assertFalse(deep.pointPick());
    // The back is measured from the picked FRONT (half way along the ray): three quarters of the
    // half that is left behind it.
    assertEquals(0.5, deep.frontFrac().orElseThrow());
    assertArrayEquals(new double[] {5, 10, 20}, deep.front().orElseThrow());
    assertArrayEquals(new double[] {8.75, 17.5, 35}, deep.back().orElseThrow(), "front + back_frac × (far − front)");

    Position.Ray point = assertInstanceOf(Position.Ray.class, Events.position(InputRecord.builder()
        .rayAndPicked(camera, far, 0.25f, 0f, at, Float.NaN).build()));
    assertTrue(point.pointPick());
    assertTrue(point.back().isEmpty(), "nothing stood out: no back");
    assertTrue(point.picked().isPresent());
    assertArrayEquals(new double[] {5, 10, 20}, point.front().orElseThrow(), "the front is the voxel's, whatever stood out");

    Position.Ray bare = assertInstanceOf(Position.Ray.class, Events.position(InputRecord.builder()
        .ray(camera, far, 0.25f, 0f).build()));
    assertTrue(bare.picked().isEmpty());
    assertFalse(bare.pointPick(), "no pick is not a point pick");
    assertTrue(bare.back().isEmpty());
    assertTrue(bare.front().isEmpty());
    assertTrue(bare.frontFrac().isEmpty());
  }

  // ---- back_frac is measured from the picked front ----

  private static Position.Ray pick(double[] camera, double[] far, double[] picked, double backFrac) {
    return new Position.Ray(camera, far, 0.0, 0.0, false,
        Optional.of(new Position.Voxel(picked[0], picked[1], picked[2])), backFrac);
  }

  /** 0.0 is NOT a point: a real pick of no depth, and what a sender that predates the field leaves. */
  @Test
  void aPreFieldSendersZerosReadAsAZeroDepthPickAtTheFront() {
    double[] camera = {0, 0, 0}, far = {0, 0, 10}, at = {0, 0, 4};
    Position.Ray zeros = pick(camera, far, at, 0.0);
    assertFalse(zeros.pointPick());
    assertArrayEquals(at, zeros.front().orElseThrow());
    assertArrayEquals(at, zeros.back().orElseThrow(), "the back is the front, not the camera");
    Position.Ray nothing = pick(camera, far, at, Double.NaN);
    assertTrue(nothing.pointPick());
    assertTrue(nothing.back().isEmpty());
    // and off the wire: four zero bytes where the field is
    Position.Ray wire = assertInstanceOf(Position.Ray.class, Events.position(InputRecord.builder()
        .rayAndPicked(new float[] {0, 0, 0}, new float[] {0, 0, 10}, 0f, 0f, new float[] {0, 0, 4}, 0f).build()));
    assertFalse(wire.pointPick());
    assertArrayEquals(at, wire.back().orElseThrow());
  }

  /**
   * The two ends of the definition agree: the numbers of vizi's own
   * {@code test_the_sdk_puts_the_back_where_the_host_measured_it}, where the host encodes a front
   * at t = 0.2 and a back at t = 0.5 as (0.5 − 0.2) / (1 − 0.2).
   */
  @Test
  void theBackIsWhereTheHostMeasuredIt() {
    double[] camera = {1, 2, 3}, far = {9, 6, 43}, picked = {2.6, 2.8, 11}, back = {5, 4, 23};
    Position.Ray pos = pick(camera, far, picked, (0.5 - 0.2) / (1.0 - 0.2));
    assertEquals(0.2, pos.frontFrac().orElseThrow(), 1e-12);
    assertArrayEquals(picked, pos.front().orElseThrow(), 1e-9);
    assertArrayEquals(back, pos.back().orElseThrow(), 1e-9);
    // the formula it replaces put this back at camera + 0.375 × (far − camera)
    assertArrayEquals(new double[] {4, 3.5, 18}, pos.at(pos.backFrac()), 1e-9);
  }

  @Test
  void theFrontIsProjectedOntoTheRayAndClampedToIt() {
    double[] camera = {0, 0, 0}, far = {0, 0, 10};
    // a voxel center off the ray lands on it
    Position.Ray off = pick(camera, far, new double[] {0, 3, 2}, 0.5);
    assertEquals(0.2, off.frontFrac().orElseThrow(), 1e-12);
    assertArrayEquals(new double[] {0, 0, 2}, off.front().orElseThrow(), 1e-12);
    assertArrayEquals(new double[] {0, 0, 6}, off.back().orElseThrow(), 1e-12, "4 of the 8 that are left");
    // to the far end
    assertArrayEquals(far, pick(camera, far, new double[] {0, 0, 2}, 1.0).back().orElseThrow(), 1e-12);
    // a voxel behind the camera, or past far, is held to the segment; at far no ray is left
    Position.Ray behind = pick(camera, far, new double[] {0, 0, -3}, 0.5);
    assertEquals(0.0, behind.frontFrac().orElseThrow());
    assertArrayEquals(new double[] {0, 0, 5}, behind.back().orElseThrow(), 1e-12);
    Position.Ray past = pick(camera, far, new double[] {0, 0, 12}, 0.5);
    assertEquals(1.0, past.frontFrac().orElseThrow());
    assertArrayEquals(far, past.back().orElseThrow(), 1e-12);
    // camera == far has no direction: the front fraction is 0, and every point is the camera
    // (the receiver refuses such a record, step 5; the accessor still answers)
    double[] one = {1, 1, 1};
    Position.Ray flat = pick(one, one, new double[] {0, 0, 0}, 0.5);
    assertEquals(0.0, flat.frontFrac().orElseThrow());
    assertArrayEquals(one, flat.front().orElseThrow());
    assertArrayEquals(one, flat.back().orElseThrow());
  }

  /** The version is the SDK release's in Maven form ({@code 1.0.0} is {@code 1.0.0}; a pre-release {@code 1.1a1} would be {@code 1.1.0-alpha.1}); build.mill publishes the same. */
  @Test
  void theVersionIsTheSdkReleaseInMavenForm() throws Exception {
    assertTrue(Sdk.VERSION.matches("\\d+\\.\\d+\\.\\d+(-(alpha|beta|rc)\\.\\d+)?"), Sdk.VERSION);
    String build = java.nio.file.Files.readString(java.nio.file.Path.of(System.getenv("VIZI_SDK_REPO"), "polyglot", "java-sdk", "build.mill"));
    assertTrue(build.contains("def publishVersion = \"" + Sdk.VERSION + "\""), "build.mill publishes " + Sdk.VERSION);
  }

  /** A tool takes either record whichever it declared, and reads both one way. */
  @Test
  void aToolReadsTheRayTheSameWayWithOrWithoutAPick() {
    float[] camera = {0, 0, 0}, far = {10, 0, 0};
    InputEvent.Info bare = Events.info(InputRecord.builder().phase(InputRecord.FIRE).interaction(1)
        .ray(camera, far, 0.25f, 1.5f).build(), true, Optional.empty());
    InputEvent.Info picked = Events.info(InputRecord.builder().phase(InputRecord.FIRE).interaction(2)
        .rayAndPicked(camera, far, 0.25f, 1.5f, new float[] {5, 0, 0}, 0.5f).build(), true, Optional.empty());
    InputEvent.Info voxel = Events.info(InputRecord.builder().phase(InputRecord.FIRE).interaction(3)
        .voxel(1, 2, 3).build(), true, Optional.empty());

    Position.Ray a = bare.ray().orElseThrow(), b = picked.ray().orElseThrow();
    assertArrayEquals(a.camera(), b.camera());
    assertArrayEquals(a.far(), b.far());
    assertEquals(a.entryFrac(), b.entryFrac());
    assertEquals(a.roll(), b.roll());
    assertArrayEquals(a.entry(), b.entry());
    assertTrue(bare.pick().isEmpty(), "a bare ray has no pick");
    assertEquals(Optional.of(new Position.Voxel(5, 0, 0)), picked.pick().orElseThrow().picked());
    assertArrayEquals(new double[] {7.5, 0, 0}, picked.pick().orElseThrow().back().orElseThrow());
    assertTrue(voxel.ray().isEmpty());
    assertTrue(voxel.pick().isEmpty());
  }

  // ---- the stage slot ----

  private static StageArm arm(int zin, int yin, int xin, vizi.plugin.v1.ViziPlugin.DType in, vizi.plugin.v1.ViziPlugin.DType out) {
    return StageArm.newBuilder().setStageId("s").setRevision(1).setPipeline(1).setPosition(1)
        .setInput(StageShape.newBuilder().addChannels(0).addChannels(1).setZ(zin).setY(yin).setX(xin).setDtype(in))
        .setOutput(StageShape.newBuilder().setZ(zin).setY(yin).setX(xin).setDtype(out))
        .putNewChannels("extra", 7).build();
  }

  /** A slot of two u16 planes (2, 4, 4), full extent, identity pose. */
  private static MemorySegment slot(Arena a, int planeCap) {
    long hb = StageSlot.headerBytes(planeCap);
    long capacity = 3L * 2 * 4 * 4 * 4; // room for three f32 planes
    MemorySegment m = a.allocate(hb + capacity, 64);
    m.set(StageSlot.U32, StageSlot.DTYPE, vizi.plugin.v1.ViziPlugin.DType.DTYPE_U16_VALUE);
    m.set(StageSlot.U32, StageSlot.PLANES, 2);
    m.set(StageSlot.U32, StageSlot.PLANE_CAP, planeCap);
    m.set(StageSlot.U32, StageSlot.Z_ALLOC, 2);
    m.set(StageSlot.U32, StageSlot.Y_ALLOC, 4);
    m.set(StageSlot.U32, StageSlot.X_ALLOC, 4);
    m.set(StageSlot.U64, StageSlot.CAPACITY, capacity);
    int[] e = {0, 2, 0, 4, 0, 4};
    for (int i = 0; i < 6; i++) m.set(StageSlot.U32, StageSlot.EXTENT + 4L * i, e[i]);
    for (int i = 0; i < 3; i++) m.set(StageSlot.F64, StageSlot.POSE_R + 8L * (4 * i), 1.0);
    m.set(StageSlot.U16, StageSlot.CHANNELS, (short) 0);
    m.set(StageSlot.U16, StageSlot.CHANNELS + 2, (short) 1);
    for (int v = 0; v < 32; v++) m.set(StageSlot.U16, hb + 2L * v, (short) (v * 1000)); // plane 0
    for (int v = 0; v < 32; v++) m.set(StageSlot.U16, hb + 64 + 2L * v, (short) v); // plane 1
    return m;
  }

  @Test
  void slotHeadersAreValidated() {
    try (Arena a = Arena.ofConfined()) {
      var u16 = vizi.plugin.v1.ViziPlugin.DType.DTYPE_U16;
      StageArm arm = arm(2, 4, 4, u16, u16);
      MemorySegment m = slot(a, 3);
      assertNull(StageSlot.problem(m, m.byteSize(), arm));
      assertTrue(StageSlot.problem(m, m.byteSize() - 1, arm).contains("does not fit"));
      m.set(StageSlot.U32, 12, 1);
      assertTrue(StageSlot.problem(m, m.byteSize(), arm).contains("reserved"));
      m.set(StageSlot.U32, 12, 0);
      m.set(StageSlot.U16, StageSlot.CHANNELS + 2, (short) 0);
      assertTrue(StageSlot.problem(m, m.byteSize(), arm).contains("two planes"));
      m.set(StageSlot.U16, StageSlot.CHANNELS + 2, (short) 1);
      assertTrue(StageSlot.problem(m, m.byteSize(), arm(2, 4, 3, u16, u16)).contains("exceeds the armed input"));
      m.set(StageSlot.F64, StageSlot.ORIGIN, Double.NaN);
      assertTrue(StageSlot.problem(m, m.byteSize(), arm).contains("finite"));
    }
  }

  @Test
  void slotEditing() {
    try (Arena a = Arena.ofConfined()) {
      var u16 = vizi.plugin.v1.ViziPlugin.DType.DTYPE_U16;
      StageSlot s = new StageSlot(slot(a, 3), arm(2, 4, 4, u16, vizi.plugin.v1.ViziPlugin.DType.DTYPE_F32));
      assertEquals(List.of(0, 1), s.channels());
      assertEquals(5, s.plane(1).u16(5));
      Volume extra = s.addChannel("extra");
      assertEquals(List.of(0, 1, 7), s.channels());
      extra.setU16(0, 9);
      assertThrows(IllegalArgumentException.class, () -> s.addChannel("other"));
      assertTrue(s.planBreach() != null, "still u16: the plan says f32");
      s.setOrigin(10, 20, 30);
      s.relayout(DType.F32, 2, 2, 2, 0, 1, 1);
      assertEquals(DType.F32, s.dtype());
      assertArrayEquals(new int[] {0, 2, 0, 2, 0, 2}, s.extent());
      assertArrayEquals(new double[] {10, 21, 31}, s.origin());
      // new (0, 0, 0) is old (0, 1, 1): index 5
      assertEquals(5000f, s.plane(0).f32(0));
      assertEquals(5f, s.plane(1).f32(0));
      assertEquals(0f, s.plane(7).f32(0), "old (0,1,1) of the added plane");
      assertNull(s.planBreach());
      s.composePose(new double[] {1, 0, 0, 0, 1, 0, 0, 0, 1}, new double[] {1, 2, 3});
      s.composePose(new double[] {1, 0, 0, 0, 1, 0, 0, 0, 1}, new double[] {1, 1, 1});
      assertArrayEquals(new double[] {2, 3, 4}, s.poseT());
      s.close();
      assertThrows(IllegalStateException.class, s::channels);
    }
  }

  @Test
  void refusalReasonsAreBounded() {
    assertThrows(IllegalArgumentException.class, () -> new Stage.Refuse(0, "x"));
    assertEquals(1, new Stage.Refuse(Stage.Refuse.CAUSAL, "x").reason());
    assertFalse(new Stage.Refuse(3, "x").getMessage().isEmpty());
  }

  @Test
  void aViewMoveIsCheckedLocally() {
    var r = vizi.plugin.v1.ViziPlugin.ViewRequest.newBuilder().setCause(3);
    ViewMove.to().t(4).selectRegion("r").lookAt(new double[] {1, 2, 3}, 0, null).into(r);
    assertEquals(4, r.getT());
    assertEquals("r", r.getSelectRegion());
    assertEquals(List.of(1.0, 2.0, 3.0), r.getLookAt().getTargetZyxList());
    assertEquals(0, r.getLookAt().getDirectionZyxCount());
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().into(vizi.plugin.v1.ViziPlugin.ViewRequest.newBuilder()));
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().t(-1));
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().selectRegion(""));
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().orbitPoint(new double[] {1, 2}));
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().lookAt(new double[] {1, 2, 3}, 1, new double[] {0, 0, 0}));
    assertThrows(IllegalArgumentException.class, () -> ViewMove.to().lookAt(new double[] {1, Double.NaN, 3}, 1, null));
  }

  @Test
  void aCauseListenerNeedsAMovesViewWidget() {
    var b = Plugin.builder("org.test.cv", "CV", "0.1");
    Panel p = b.panel(vizi.plugin.javasdk.manifest.PanelDecl.of("p", "P", vizi.plugin.javasdk.manifest.WidgetDecl.slider("s", "s")));
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("s", "S"));
    assertThrows(IllegalStateException.class, () -> p.number("s").onCause(c -> c.decline()));
  }

  @Test
  void theJbangLauncher() throws Exception {
    assertEquals(List.of("--java", "25+", "-R=--enable-native-access=ALL-UNNAMED", "-R=--sun-misc-unsafe-memory-access=allow", "plugin-0123456789abcdef.jar"),
        PluginMain.jbangArgs("plugin-0123456789abcdef.jar"));
    // on PATH: the bare name is what the launch line writes
    java.nio.file.Path dir = java.nio.file.Files.createTempDirectory("jbang-path");
    String name = System.getProperty("os.name").toLowerCase(java.util.Locale.ROOT).startsWith("windows") ? "jbang.cmd" : "jbang";
    java.nio.file.Path fake = java.nio.file.Files.writeString(dir.resolve(name), "#!/bin/sh\n");
    fake.toFile().setExecutable(true);
    var found = PluginMain.findJbang(Map.of("PATH", dir.toString())).orElseThrow();
    assertEquals(name, found.written());
    assertEquals(fake.toAbsolutePath(), found.run());
    // not on PATH: jbang's own location, by absolute path, or nothing
    var off = PluginMain.findJbang(Map.of("PATH", ""));
    off.ifPresent(j -> assertEquals(PluginMain.jbangHome().toString(), j.written()));
  }

  @Test
  void theLauncherGoesWithInstall() {
    var b = Plugin.builder("org.test.l", "L", "0.1");
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    Plugin p = b.build();
    var sink = new java.io.PrintStream(new java.io.ByteArrayOutputStream());
    assertEquals(2, PluginMain.run(p, new String[] {"--launcher=jbang"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(2, PluginMain.run(p, new String[] {"--manifest", "--launcher=jbang"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(2, PluginMain.run(p, new String[] {"--install", "--launcher=docker"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(2, PluginMain.run(p, new String[] {"--install", "--launcher=path", "--launcher=jbang"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(0, PluginMain.run(p, new String[] {"--help"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(2, PluginMain.run(p, new String[] {"--manifest", "--force"}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(2, PluginMain.run(p, new String[] {"--install-force"}, Map.of(), sink, sink, RootApiTest.class), "--install-force is not an option");
  }

  /**
   * {@code --install}: the jar under a content name, the
   * manifest last, the marker's contents checked, an install lock, --force.
   */
  @Test
  void installNamesTheJarByContentAndChecksTheMarker(@org.junit.jupiter.api.io.TempDir java.nio.file.Path tmp) throws Exception {
    var b = Plugin.builder("org.test.inst", "Inst", "0.1");
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    Plugin p = b.build();
    var sink = new java.io.PrintStream(new java.io.ByteArrayOutputStream());
    java.nio.file.Path plugins = tmp.resolve("plugins");
    java.nio.file.Path dir = plugins.resolve("org.test.inst");
    java.nio.file.Path jar1 = jar(tmp.resolve("a.jar"), "one");
    java.nio.file.Path jar2 = jar(tmp.resolve("b.jar"), "two");
    String name1 = PluginMain.jarName(java.nio.file.Files.readAllBytes(jar1));
    String name2 = PluginMain.jarName(java.nio.file.Files.readAllBytes(jar2));
    assertTrue(name1.matches("plugin-[0-9a-f]{16}\\.jar"), name1);

    assertEquals(0, PluginMain.install(p, jar1, plugins, false, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    var m = vizi.plugin.javasdk.manifest.ManifestParser.read(dir).manifest();
    assertEquals(name1, m.entryCommand().getLast());
    assertTrue(PluginMain.markerNames(java.nio.file.Files.readString(dir.resolve(PluginMain.MARKER)), "org.test.inst"));
    assertFalse(java.nio.file.Files.exists(dir.resolve(PluginMain.LOCK)), "the lock is released");

    // over its own install: no --force; the replaced jar goes
    java.nio.file.Files.write(dir.resolve("plugin.jar"), new byte[] {9}); // an older installer's jar
    assertEquals(0, PluginMain.install(p, jar2, plugins, false, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(name2, vizi.plugin.javasdk.manifest.ManifestParser.read(dir).manifest().entryCommand().getLast());
    assertFalse(java.nio.file.Files.exists(dir.resolve(name1)));
    assertFalse(java.nio.file.Files.exists(dir.resolve("plugin.jar")));

    // a lock held: refused, and nothing changes
    java.nio.file.Files.createFile(dir.resolve(PluginMain.LOCK));
    assertEquals(1, PluginMain.install(p, jar1, plugins, false, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(name2, vizi.plugin.javasdk.manifest.ManifestParser.read(dir).manifest().entryCommand().getLast());
    java.nio.file.Files.delete(dir.resolve(PluginMain.LOCK));

    // a marker naming another plugin, or none in a non-empty directory: --force only
    java.nio.file.Files.writeString(dir.resolve(PluginMain.MARKER), PluginMain.markerText("org.test.other"));
    assertEquals(1, PluginMain.install(p, jar1, plugins, false, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    assertEquals(0, PluginMain.install(p, jar1, plugins, true, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    java.nio.file.Path foreign = plugins.resolve("org.test.inst2");
    java.nio.file.Files.createDirectories(foreign);
    java.nio.file.Files.writeString(foreign.resolve("notes.txt"), "mine");
    var b2 = Plugin.builder("org.test.inst2", "Inst2", "0.1");
    b2.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    assertEquals(1, PluginMain.install(b2.build(), jar1, plugins, false, PluginMain.Launcher.PATH, Map.of(), sink, sink, RootApiTest.class));
    assertFalse(java.nio.file.Files.exists(foreign.resolve(name1)), "nothing written into a directory that is not ours");
  }

  private static java.nio.file.Path jar(java.nio.file.Path to, String content) throws java.io.IOException {
    var mf = new java.util.jar.Manifest();
    mf.getMainAttributes().put(java.util.jar.Attributes.Name.MANIFEST_VERSION, "1.0");
    try (var j = new java.util.jar.JarOutputStream(java.nio.file.Files.newOutputStream(to), mf)) {
      j.putNextEntry(new java.util.jar.JarEntry("content.txt"));
      j.write(content.getBytes(java.nio.charset.StandardCharsets.UTF_8));
    }
    return to;
  }

  @Test
  void manifestNamesADirectory(@org.junit.jupiter.api.io.TempDir java.nio.file.Path tmp) throws Exception {
    var b = Plugin.builder("org.test.man", "Man", "0.1");
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    Plugin p = b.build();
    var sink = new java.io.PrintStream(new java.io.ByteArrayOutputStream());
    java.nio.file.Path out = tmp.resolve("pkg");
    assertEquals(0, PluginMain.run(p, new String[] {"--manifest=" + out}, Map.of(), sink, sink, RootApiTest.class));
    assertEquals("org.test.man", vizi.plugin.javasdk.manifest.ManifestParser.read(out).manifest().id());
    java.nio.file.Path file = java.nio.file.Files.writeString(tmp.resolve("f"), "x");
    assertEquals(1, PluginMain.run(p, new String[] {"--manifest=" + file}, Map.of(), sink, sink, RootApiTest.class));
  }

  @Test
  void theDriftCheckIgnoresTheLaunchLineOnly() {
    var b = Plugin.builder("org.test.d", "D", "0.1").entry("./run");
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    var m = b.build().manifest();
    var installed = m.withEntryCommand(List.of("jbang", "--java", "25+", "plugin-0123456789abcdef.jar"));
    assertEquals(List.of(), PluginMain.drift(m, installed), "an installed launch line is not drift");
    var renamed = new vizi.plugin.javasdk.manifest.Manifest(m.id(), "D!", m.version(), m.engine(),
        m.minVizi(), m.sdkVersion(), m.entryCommand(), m.startupS(), m.contributions(), m.assets(), m.permissions(), m.subscriptions());
    assertEquals(1, PluginMain.drift(m, renamed).size(), "any other difference is");
    var slower = new vizi.plugin.javasdk.manifest.Manifest(m.id(), m.name(), m.version(), m.engine(),
        m.minVizi(), m.sdkVersion(), m.entryCommand(), java.util.OptionalDouble.of(90), m.contributions(), m.assets(), m.permissions(), m.subscriptions());
    assertEquals(1, PluginMain.drift(m, slower).size(), "entry.startup_s is not the launch line");
  }

  // ---- entry.startup_s ----

  /** A declared startup time reaches the manifest {@code --manifest} writes, and an install keeps it. */
  @Test
  void aDeclaredStartupTimeReachesTheWrittenManifest(@org.junit.jupiter.api.io.TempDir java.nio.file.Path tmp)
      throws Exception {
    var b = Plugin.builder("org.test.slow", "Slow", "0.1").entry("./run").startup(Duration.ofSeconds(90));
    b.command(vizi.plugin.javasdk.manifest.CommandDecl.of("c", "C"), ctx -> "");
    Plugin p = b.build();
    assertEquals(java.util.OptionalDouble.of(90), p.manifest().startupS());
    var sink = new java.io.PrintStream(new java.io.ByteArrayOutputStream());
    java.nio.file.Path out = tmp.resolve("pkg");
    assertEquals(0, PluginMain.run(p, new String[] {"--manifest=" + out}, Map.of(), sink, sink, RootApiTest.class));
    String text = java.nio.file.Files.readString(out.resolve(vizi.plugin.javasdk.manifest.ManifestParser.FILE_NAME));
    assertTrue(text.contains("\"startup_s\": 90\n"), text);
    var read = vizi.plugin.javasdk.manifest.ManifestParser.read(out).manifest();
    assertEquals(p.manifest(), read);
    assertEquals(java.util.OptionalDouble.of(90), read.withEntryCommand(List.of("java", "-jar", "x.jar")).startupS());
  }

  @Test
  void aStartupTimeThatIsNotPositiveIsRefusedAtBuild() {
    for (Duration d : List.of(Duration.ZERO, Duration.ofSeconds(-5))) {
      var b = Plugin.builder("org.test.slow", "Slow", "0.1").entry("./run").startup(d);
      var e = assertThrows(RuntimeException.class, b::build, d.toString());
      assertTrue(String.valueOf(e.getMessage()).contains("entry.startup_s must be a positive number of seconds"),
          String.valueOf(e.getMessage()));
    }
  }
}
