package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Random;
import java.util.TreeMap;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import vizi.plugin.v1.ViziPlugin.Options;
import vizi.plugin.v1.ViziPlugin.OverlayChunk;
import vizi.plugin.v1.ViziPlugin.OverlayRemove;
import vizi.plugin.v1.ViziPlugin.OverlayUpdate;
import vizi.plugin.v1.ViziPlugin.PatchOp;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.SegmentShortfall;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * The outbound queues. Merges are tested by the state they produce — replaying merged and
 * unmerged sequences through a model of the host's dropdown — never by the order of ops
 * (control_plane_protocol.md §5.4, §13).
 */
class OutboundTest {

  /** A channel that holds everything until released, so pushes pile up and merge. */
  static final class Held implements Outbound.Channel {
    final AtomicBoolean ready = new AtomicBoolean(false);
    final List<PluginMsg> got = new ArrayList<>();

    @Override
    public boolean isReady() {
      return ready.get();
    }

    @Override
    public synchronized void send(PluginMsg msg) {
      got.add(msg);
    }

    synchronized List<PluginMsg> got() {
      return List.copyOf(got);
    }
  }

  static PatchOp value(String w, String v) {
    return PatchOp.newBuilder().setWidgetId(w).setSetValue(Value.newBuilder().setS(v)).build();
  }

  static PatchOp options(String w, String... opts) {
    return PatchOp.newBuilder().setWidgetId(w).setSetOptions(Options.newBuilder().addAllOptions(List.of(opts))).build();
  }

  static PatchOp enabled(String w, boolean e) {
    return PatchOp.newBuilder().setWidgetId(w).setSetEnabled(e).build();
  }

  /** The host's dropdown: set_options keeps the selection by text; set_value matches text. */
  static final class Dropdown {
    List<String> options = List.of();
    String selected = "";
    boolean enabled = true;

    void apply(PatchOp op) {
      switch (op.getOpCase()) {
        case SET_OPTIONS -> {
          options = op.getSetOptions().getOptionsList();
          if (!options.contains(selected)) selected = options.isEmpty() ? "" : options.get(0);
        }
        case SET_VALUE -> {
          if (options.contains(op.getSetValue().getS())) selected = op.getSetValue().getS();
        }
        case SET_ENABLED -> enabled = op.getSetEnabled();
        default -> {}
      }
    }

    @Override
    public String toString() {
      return options + "/" + selected + "/" + enabled;
    }
  }

  static Dropdown replay(List<PatchOp> ops, String start) {
    Dropdown d = new Dropdown();
    d.options = List.of("a", "b", "c");
    d.selected = start;
    for (PatchOp op : ops) d.apply(op);
    return d;
  }

  List<PatchOp> drained(Outbound out, Held ch) throws Exception {
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    List<PatchOp> ops = new ArrayList<>();
    for (PluginMsg m : ch.got()) if (m.hasPanelPatch()) ops.addAll(m.getPanelPatch().getOpsList());
    return ops;
  }

  @Test
  void aMergedSequenceLeavesEachWidgetWhereTheUnmergedOneWould() throws Exception {
    Random r = new Random(42);
    String[] vals = {"a", "b", "c", "d"};
    for (int trial = 0; trial < 300; trial++) {
      List<PatchOp> seq = new ArrayList<>();
      int n = 1 + r.nextInt(8);
      boolean optionsSent = false;
      for (int i = 0; i < n; i++) {
        switch (r.nextInt(3)) {
          // a value the widget's current (manifest) options hold, until a set_options makes it held whole:
          // a value the host refuses is a plugin bug, and per-property merging is exact only for valid traffic
          case 0 -> seq.add(value("w", optionsSent ? vals[r.nextInt(vals.length)] : vals[r.nextInt(3)]));
          case 1 -> {
            optionsSent = true;
            List<String> opts = new ArrayList<>(List.of(vals));
            java.util.Collections.shuffle(opts, r);
            seq.add(options("w", opts.subList(0, 1 + r.nextInt(opts.size())).toArray(new String[0])));
          }
          default -> seq.add(enabled("w", r.nextBoolean()));
        }
      }
      Held ch = new Held();
      Outbound out = new Outbound(ch, s -> {});
      for (PatchOp op : seq) out.patch("p", "w", op, Outbound.Loans.NONE);
      List<PatchOp> merged = drained(out, ch);
      out.close();
      assertEquals(replay(seq, "b").toString(), replay(merged, "b").toString(), "trial " + trial + ": " + seq);
    }
  }

  @Test
  void theTwoSequencesThatBrokeEarlierRules() throws Exception {
    List<List<PatchOp>> cases = List.of(
        List.of(value("w", "a"), options("w", "a", "b"), value("w", "b")),
        List.of(options("w", "a", "b"), value("w", "b"), options("w", "b", "a")),
        List.of(options("w", "a"), options("w", "a", "b")));
    for (List<PatchOp> seq : cases) {
      Held ch = new Held();
      Outbound out = new Outbound(ch, s -> {});
      for (PatchOp op : seq) out.patch("p", "w", op, Outbound.Loans.NONE);
      assertEquals(replay(seq, "b").toString(), replay(drained(out, ch), "b").toString(), seq.toString());
      out.close();
    }
  }

  @Test
  void oneKeyNeverEvictsAnother() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.patch("p", "A", value("A", "keep"), Outbound.Loans.NONE);
    for (int i = 0; i < 1000; i++) out.patch("p", "B", value("B", "v" + i), Outbound.Loans.NONE);
    out.patch("p", "A", enabled("A", false), Outbound.Loans.NONE);
    List<PatchOp> ops = drained(out, ch);
    assertTrue(ops.contains(value("A", "keep")), "a burst on B did not evict A");
    assertTrue(ops.contains(enabled("A", false)), "set_enabled did not displace set_value");
    assertEquals(1, ops.stream().filter(o -> o.getWidgetId().equals("B")).count());
    assertEquals(value("B", "v999"), ops.stream().filter(o -> o.getWidgetId().equals("B")).findFirst().orElseThrow());
    out.close();
  }

  @Test
  void aSupersededPushReturnsItsLoansAndAMultiUseOneIsNotMergedAway() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    AtomicInteger reclaimed = new AtomicInteger();
    out.overlayUpdate(OverlayUpdate.newBuilder().setOverlayId("o").setRevision(1).build(),
        new Outbound.Loans(List.of(reclaimed::incrementAndGet), false));
    out.overlayUpdate(OverlayUpdate.newBuilder().setOverlayId("o").setRevision(2).build(),
        new Outbound.Loans(List.of(reclaimed::incrementAndGet), true));
    out.overlayUpdate(OverlayUpdate.newBuilder().setOverlayId("o").setRevision(3).build(), Outbound.Loans.NONE);
    assertEquals(1, reclaimed.get(), "revision 1's loan came back at replacement; revision 2's is still named elsewhere");
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    List<Long> revs = ch.got().stream().filter(PluginMsg::hasOverlayUpdate).map(m -> m.getOverlayUpdate().getRevision()).toList();
    assertEquals(List.of(2L, 3L), revs, "the multi-use push was sent in order, not merged");
    out.close();
  }

  @Test
  void aRemoveSupersedesThePendingPushesForItsOverlay() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    AtomicInteger reclaimed = new AtomicInteger();
    Outbound.Loans loan = new Outbound.Loans(List.of(reclaimed::incrementAndGet), false);
    out.overlayUpdate(OverlayUpdate.newBuilder().setOverlayId("o").setRevision(4).build(), loan);
    out.chunk(OverlayChunk.newBuilder().setOverlayId("o").setLevel(0).setT(0).build(), loan);
    out.chunk(OverlayChunk.newBuilder().setOverlayId("o").setLevel(0).build(), loan);
    out.overlayUpdate(OverlayUpdate.newBuilder().setOverlayId("other").setRevision(1).build(), Outbound.Loans.NONE);
    out.overlayRemove(PluginMsg.newBuilder().setOverlayRemove(OverlayRemove.newBuilder().setOverlayId("o").setRevision(5)).build());
    assertEquals(3, reclaimed.get());
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    List<PluginMsg> got = ch.got();
    assertEquals(2, got.size());
    assertTrue(got.get(0).hasOverlayRemove(), "the one-shot goes first");
    assertEquals("other", got.get(1).getOverlayUpdate().getOverlayId());
    out.close();
  }

  @Test
  void chunkKeysKeepAbsentTApartFromZero() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.chunk(OverlayChunk.newBuilder().setOverlayId("o").setLevel(0).setT(0).build(), Outbound.Loans.NONE);
    out.chunk(OverlayChunk.newBuilder().setOverlayId("o").setLevel(0).build(), Outbound.Loans.NONE);
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    assertEquals(2, ch.got().size());
    out.close();
  }

  @Test
  void shortfallsMergeByTheirOwnRule() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.shortfall(SegmentShortfall.newBuilder().setSegmentId(9).setRegion(1).setWantedBytes(100).setLargestFreeBytes(40).setDropped(1).build());
    out.shortfall(SegmentShortfall.newBuilder().setSegmentId(9).setRegion(1).setWantedBytes(500).setLargestFreeBytes(10).setDropped(2).build());
    out.shortfall(SegmentShortfall.newBuilder().setSegmentId(9).setRegion(1).setWantedBytes(200).setLargestFreeBytes(90).setDropped(4).build());
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    SegmentShortfall s = ch.got().get(0).getSegmentShortfall();
    assertEquals(500, s.getWantedBytes());
    assertEquals(10, s.getLargestFreeBytes(), "the free run travels with its own want");
    assertEquals(7, s.getDropped(), "dropped is summed");
    out.close();
  }

  @Test
  void heldWholeOpsPastTheCapAreRefusedToTheCaller() {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.patch("p", "w", options("w", "a"), Outbound.Loans.NONE);
    for (int i = 1; i < Outbound.HELD_WHOLE_CAP; i++) out.patch("p", "w", value("w", "a"), Outbound.Loans.NONE);
    assertThrows(Outbound.RefusedException.class, () -> out.patch("p", "w", value("w", "a"), Outbound.Loans.NONE));
    out.close();
  }

  @Test
  void theOrderedQueueNeverDropsAndDrainsFirst() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.patch("p", "w", value("w", "x"), Outbound.Loans.NONE);
    for (int i = 0; i < 2000; i++) out.send(PluginMsg.newBuilder().setId("m" + i).build());
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(5000));
    List<PluginMsg> got = ch.got();
    assertEquals(2001, got.size());
    for (int i = 0; i < 2000; i++) assertEquals("m" + i, got.get(i).getId());
    assertTrue(got.get(2000).hasPanelPatch());
    out.close();
  }

  @Test
  void followOffersCoalesceByPanelStatesByFollowAndAnEndSupersedesItsState() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    for (String l : List.of("Tour 1 region", "Tour 2 regions", "Tour 3 regions")) {
      out.offerFollow(vizi.plugin.v1.ViziPlugin.OfferFollow.newBuilder().setPanelId("p").setLabel(l).build());
    }
    out.offerFollow(vizi.plugin.v1.ViziPlugin.OfferFollow.newBuilder().setPanelId("q").setLabel("Q").build());
    for (int i = 1; i <= 5; i++) {
      out.followState(vizi.plugin.v1.ViziPlugin.FollowState.newBuilder().setFollowId(1).setSeq(i).setT(i).build());
      out.followState(vizi.plugin.v1.ViziPlugin.FollowState.newBuilder().setFollowId(2).setSeq(i).setT(i).build());
    }
    out.endFollow(2); // its pending state is superseded; follow 1's is not
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(5000));
    List<PluginMsg> got = ch.got();
    assertEquals(List.of("Tour 3 regions", "Q"), got.stream().filter(PluginMsg::hasOfferFollow).map(m -> m.getOfferFollow().getLabel()).toList());
    assertEquals(List.of(5L), got.stream().filter(PluginMsg::hasFollowState).map(m -> m.getFollowState().getSeq()).toList());
    assertEquals(1, got.stream().filter(PluginMsg::hasFollowState).findFirst().orElseThrow().getFollowState().getFollowId());
    assertEquals(1, got.stream().filter(m -> m.hasEndFollow() && m.getEndFollow().getFollowId() == 2).count());
    out.close();
  }

  @SuppressWarnings("unused")
  private static Map<String, Integer> tally(List<PluginMsg> l) {
    Map<String, Integer> m = new TreeMap<>();
    for (PluginMsg p : l) m.merge(p.getMsgCase().name(), 1, Integer::sum);
    return m;
  }

  // ---- lists ----

  static PatchOp cell(String w, String row, String v) {
    return PatchOp.newBuilder().setWidgetId(w).setRowKey(row).setSetValue(Value.newBuilder().setS(v)).build();
  }

  static PatchOp rows(String list, String... keys) {
    return PatchOp.newBuilder().setWidgetId(list).setSetRows(vizi.plugin.v1.ViziPlugin.RowKeys.newBuilder().addAllKeys(List.of(keys))).build();
  }

  @Test
  void twoRowsOfOneCellBothArriveAndTheRowsComeFirst() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.patch("p", "name", cell("name", "a", "A1"), Outbound.Loans.NONE);
    out.patch("p", "name", cell("name", "b", "B"), Outbound.Loans.NONE);
    out.patch("p", "name", cell("name", "a", "A2"), Outbound.Loans.NONE); // supersedes A1 only
    out.patch("p", "cells", rows("cells", "a", "b"), Outbound.Loans.NONE);
    List<PatchOp> ops = drained(out, ch);
    assertEquals(List.of(rows("cells", "a", "b"), cell("name", "a", "A2"), cell("name", "b", "B")), ops,
        "keyed by (panel, widget, row): both rows arrive, the rows before any cell");
    out.close();
  }

  @Test
  void aRowThatLeavesTakesItsPendingCellOpsWithIt() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    AtomicInteger reclaimed = new AtomicInteger();
    out.patch("p", "name", cell("name", "a", "A"), Outbound.Loans.NONE);
    out.patch("p", "spark", PatchOp.newBuilder().setWidgetId("spark").setRowKey("b").setSetEnabled(true).build(),
        new Outbound.Loans(List.of(reclaimed::incrementAndGet), false));
    out.dropRows("p", java.util.Set.of("name", "spark"), java.util.Set.of("a"));
    out.patch("p", "cells", rows("cells", "a"), Outbound.Loans.NONE);
    assertEquals(1, reclaimed.get(), "the dropped row's loan came back");
    assertEquals(List.of(rows("cells", "a"), cell("name", "a", "A")), drained(out, ch));
    out.close();
  }

  /**
   * A set_rows that brings back a key the pending one dropped is kept beside it; one naming
   * only keys the pending one has supersedes it; a list's selection goes after the rows pending with it.
   */
  @Test
  void aReturningRowIsKeptBesideAndASelectionFollowsItsRows() throws Exception {
    Held ch = new Held();
    Outbound out = new Outbound(ch, s -> {});
    out.patch("p", "cells", rows("cells", "a", "b"), Outbound.Loans.NONE);
    out.patch("p", "cells", rows("cells", "a"), Outbound.Loans.NONE); // a subset: supersedes
    assertEquals(List.of(rows("cells", "a")), drained(out, ch));
    out.close();

    ch = new Held();
    out = new Outbound(ch, s -> {});
    out.patch("p", "cells", rows("cells", "a"), Outbound.Loans.NONE);
    out.patch("p", "cells", rows("cells", "a", "b"), Outbound.Loans.NONE); // b comes back: kept beside
    out.patch("p", "name", cell("name", "b", "fresh"), Outbound.Loans.NONE);
    assertEquals(List.of(rows("cells", "a"), rows("cells", "a", "b"), cell("name", "b", "fresh")), drained(out, ch),
        "the removal reaches the host, so row b starts fresh");
    out.close();

    ch = new Held();
    out = new Outbound(ch, s -> {});
    out.patch("p", "cells", value("cells", "a"), Outbound.Loans.NONE);
    out.patch("p", "cells", rows("cells", "a", "c"), Outbound.Loans.NONE);
    out.patch("p", "cells", value("cells", "c"), Outbound.Loans.NONE); // names c: after the rows that hold it
    assertEquals(List.of(rows("cells", "a", "c"), value("cells", "c")), drained(out, ch));
    out.close();
  }

  // ---- tables (§8.4) ----

  static vizi.plugin.v1.ViziPlugin.TableRow trow(String key, String cell) {
    return vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey(key).addCells(Value.newBuilder().setS(cell)).build();
  }

  static PatchOp setTable(String w, vizi.plugin.v1.ViziPlugin.TableRow... rows) {
    return PatchOp.newBuilder().setWidgetId(w).setSetTable(vizi.plugin.v1.ViziPlugin.TableRows.newBuilder().addAllRows(List.of(rows))).build();
  }

  static PatchOp patchTable(String w, List<String> delete, vizi.plugin.v1.ViziPlugin.TableRow... upsert) {
    return PatchOp.newBuilder().setWidgetId(w)
        .setPatchTable(vizi.plugin.v1.ViziPlugin.TablePatch.newBuilder().addAllDelete(delete).addAllUpsert(List.of(upsert))).build();
  }

  /**
   * The host's table, the reference's rules ported as the oracle: a whole statement replaces; a
   * patch deletes, then upserts in place or appends; an op with a bad key, more cells than columns
   * or past 100,000 rows is refused whole and changes nothing. After every table op the selection
   * keeps the keys still there, less those a patch deletes: a key deleted and upserted again in one
   * patch is a fresh row and loses its selection, as across two patches. A
   * {@code set_value} is one key ({@code ""} for none) or a list of keys, duplicates collapsing,
   * refused for a key the table does not hold or for several keys on a table that selects one.
   */
  static final class HostTable {
    final int width;
    final boolean multiple;
    final List<String> keys = new ArrayList<>();
    final Map<String, vizi.plugin.v1.ViziPlugin.TableRow> rows = new java.util.HashMap<>();
    List<String> selection = List.of();

    HostTable(int width, boolean multiple) {
      this.width = width;
      this.multiple = multiple;
    }

    HostTable() {
      this(1, false);
    }

    private boolean badRows(List<vizi.plugin.v1.ViziPlugin.TableRow> rs) {
      for (var r : rs) {
        int chars = r.getKey().codePointCount(0, r.getKey().length());
        if (chars == 0 || chars > 128 || r.getCellsCount() > width) return true;
      }
      return false;
    }

    void apply(PatchOp op) {
      java.util.Set<String> fresh = java.util.Set.of();
      switch (op.getOpCase()) {
        case SET_TABLE -> {
          var rs = op.getSetTable().getRowsList();
          if (rs.size() > 100_000 || badRows(rs)) return;
          List<String> ks = rs.stream().map(r -> r.getKey()).toList();
          if (new java.util.HashSet<>(ks).size() != ks.size()) return;
          keys.clear();
          keys.addAll(ks);
          rows.clear();
          for (var r : rs) rows.put(r.getKey(), r);
        }
        case PATCH_TABLE -> {
          var up = op.getPatchTable().getUpsertList();
          if (badRows(up)) return;
          java.util.Set<String> gone = new java.util.HashSet<>(op.getPatchTable().getDeleteList());
          List<String> ks = new ArrayList<>(keys.stream().filter(k -> !gone.contains(k)).toList());
          Map<String, vizi.plugin.v1.ViziPlugin.TableRow> rs = new java.util.HashMap<>();
          for (String k : ks) rs.put(k, rows.get(k));
          for (var r : up) {
            if (!rs.containsKey(r.getKey())) ks.add(r.getKey());
            rs.put(r.getKey(), r);
          }
          if (ks.size() > 100_000) return;
          keys.clear();
          keys.addAll(ks);
          rows.clear();
          rows.putAll(rs);
          fresh = gone;
        }
        case SET_VALUE -> {
          var v = op.getSetValue();
          List<String> ks;
          if (v.hasS()) ks = v.getS().isEmpty() ? List.of() : List.of(v.getS());
          else if (v.hasStrings()) ks = List.copyOf(new java.util.LinkedHashSet<>(v.getStrings().getValuesList()));
          else return;
          if (ks.size() > 1 && !multiple) return;
          if (!rows.keySet().containsAll(ks)) return;
          selection = ks;
          return;
        }
        default -> {
          return;
        }
      }
      java.util.Set<String> deleted = fresh;
      selection = selection.stream().filter(k -> rows.containsKey(k) && !deleted.contains(k)).toList();
    }

    String state() {
      StringBuilder b = new StringBuilder();
      for (String k : keys) b.append(k).append('=').append(rows.get(k).getCellsList().stream().map(c -> c.getS()).toList()).append(' ');
      return b + "selected=" + new java.util.TreeSet<>(selection);
    }
  }

  static String replayTable(List<PatchOp> ops) {
    HostTable t = new HostTable();
    for (PatchOp op : ops) t.apply(op);
    return t.state();
  }


  static final vizi.plugin.v1.ViziPlugin.TableRow[] NO_ROWS = new vizi.plugin.v1.ViziPlugin.TableRow[0];

  /** An outbox whose table "t" declares {@code columns} columns (0: not known). */
  static Outbound tableOutbox(Held ch, int columns) {
    Outbound out = new Outbound(ch, s -> {});
    out.tableColumns((panel, widget) -> widget.equals("t") ? columns : 0);
    return out;
  }

  /** Every op the channel received, one list per PanelPatch. */
  static List<List<PatchOp>> patches(Held ch) {
    return ch.got().stream().map(m -> m.getPanelPatch().getOpsList()).toList();
  }

  /** Releases the channel and drains, then holds it again so later pushes pile up. */
  static void flush(Outbound out, Held ch) throws InterruptedException {
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(5000));
    ch.ready.set(false);
  }

  static vizi.plugin.v1.ViziPlugin.TableRow[] keyed(String prefix, int n) {
    vizi.plugin.v1.ViziPlugin.TableRow[] rows = new vizi.plugin.v1.ViziPlugin.TableRow[n];
    for (int i = 0; i < n; i++) rows[i] = vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey(prefix + i).build();
    return rows;
  }

  /**
   * Random runs of set_table, patch_table and set_value, with drains between, reach through the
   * outbox the rows AND the selection that applying them in order does, on the reference's rules
   * ({@link HostTable}). The host may already hold rows and a selection this outbox never saw; keys
   * are deleted and upserted again in one patch and across patches; some ops and selections are
   * refused; the table selects one row or several.
   */
  @Test
  void mergedTableOpsReachTheRowsAndSelectionTheUnmergedOnesWould() throws Exception {
    Random r = new Random(99);
    String[] keys = {"a", "b", "c", "d", "e", "f"};
    int merges = 0;
    for (int trial = 0; trial < 6000; trial++) {
      boolean multiple = r.nextBoolean();
      int width = 2;
      Held ch = new Held();
      Outbound out = tableOutbox(ch, width);
      // what the host held before this session, unknown here
      HostTable inOrder = new HostTable(width, multiple);
      HostTable merged = new HostTable(width, multiple);
      List<PatchOp> start = new ArrayList<>();
      if (r.nextBoolean()) {
        List<vizi.plugin.v1.ViziPlugin.TableRow> rs = new ArrayList<>();
        for (String k : keys) if (r.nextBoolean()) rs.add(trow(k, "old"));
        start.add(setTable("t", rs.toArray(NO_ROWS)));
        if (!rs.isEmpty()) start.add(strings("t", rs.get(r.nextInt(rs.size())).getKey(), rs.get(r.nextInt(rs.size())).getKey()));
      }
      for (PatchOp op : start) {
        inOrder.apply(op);
        merged.apply(op);
      }
      List<PatchOp> seq = new ArrayList<>();
      int sent = 0;
      int n = 1 + r.nextInt(14);
      for (int i = 0; i < n; i++) {
        PatchOp op;
        int pick = r.nextInt(10);
        if (pick < 2) {
          List<vizi.plugin.v1.ViziPlugin.TableRow> rs = new ArrayList<>();
          for (String k : keys) if (r.nextInt(3) > 0) rs.add(r.nextInt(30) == 0 ? wide(k) : trow(k, k + i));
          java.util.Collections.shuffle(rs, r);
          op = setTable("t", rs.toArray(NO_ROWS));
        } else if (pick < 5) {
          List<String> del = new ArrayList<>();
          List<vizi.plugin.v1.ViziPlugin.TableRow> up = new ArrayList<>();
          for (String k : keys) if (r.nextInt(3) == 0) del.add(k);
          for (String k : keys) if (r.nextInt(3) == 0) up.add(r.nextInt(30) == 0 ? wide(k) : trow(k, k + i));
          java.util.Collections.shuffle(up, r);
          op = patchTable("t", del, up.toArray(NO_ROWS));
        } else if (pick < 9) {
          int shape = r.nextInt(4);
          String k = keys[r.nextInt(keys.length)];
          if (shape == 0) op = value("t", "");
          else if (shape == 1) op = value("t", k);
          else if (shape == 2) op = strings("t", k);
          else op = strings("t", k, keys[r.nextInt(keys.length)]);
        } else {
          op = enabled("t", r.nextBoolean());
        }
        seq.add(op);
        inOrder.apply(op);
        out.patch("p", "t", op, Outbound.Loans.NONE);
        if (r.nextInt(6) == 0) flush(out, ch);
      }
      List<PatchOp> got = drained(out, ch);
      out.close();
      for (PatchOp op : got) merged.apply(op);
      if (got.size() < seq.size()) merges++;
      assertEquals(inOrder.state(), merged.state(), "trial " + trial + " (multiple=" + multiple + "): " + brief(start) + " then " + brief(seq) + " sent as " + brief(got));
    }
    assertTrue(merges > 1000, "the merges are exercised: " + merges);
  }

  /** Ops as a line each can be read from: T[rows] P[-deletes +upserts] S(key) S[keys] E. */
  static String brief(List<PatchOp> ops) {
    List<String> out = new ArrayList<>();
    for (PatchOp o : ops) {
      out.add(switch (o.getOpCase()) {
        case SET_TABLE -> "T" + o.getSetTable().getRowsList().stream().map(r -> r.getKey() + (r.getCellsCount() > 2 ? "!" : "")).toList();
        case PATCH_TABLE -> "P[-" + o.getPatchTable().getDeleteList() + " +"
            + o.getPatchTable().getUpsertList().stream().map(r -> r.getKey() + (r.getCellsCount() > 2 ? "!" : "")).toList() + "]";
        case SET_VALUE -> o.getSetValue().hasS() ? "S(" + o.getSetValue().getS() + ")" : "S" + o.getSetValue().getStrings().getValuesList();
        default -> "E";
      });
    }
    return out.toString();
  }

  static PatchOp strings(String w, String... keys) {
    return PatchOp.newBuilder().setWidgetId(w)
        .setSetValue(Value.newBuilder().setStrings(vizi.plugin.v1.ViziPlugin.StringList.newBuilder().addAllValues(List.of(keys)))).build();
  }

  /** A row with three cells: more than the table's two columns, so the host refuses its op. */
  static vizi.plugin.v1.ViziPlugin.TableRow wide(String key) {
    return vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey(key)
        .addCells(Value.newBuilder().setS("x")).addCells(Value.newBuilder().setS("y")).addCells(Value.newBuilder().setS("z")).build();
  }

  /**
   * A key deleted and upserted again in ONE patch is a fresh row: it loses its selection, exactly
   * as when the delete and the upsert travel in two patches. No merge undoes that removal: a row
   * that stood before the merged ops, removed by one and present after them, blocks the merge,
   * whatever is selected.
   */
  @Test
  void aKeyDeletedAndUpsertedAgainIsAFreshRowInOnePatchOrTwo() throws Exception {
    PatchOp base = setTable("t", trow("a", "1"), trow("b", "2"));
    PatchOp same = patchTable("t", List.of("a"), trow("a", "fresh"));
    PatchOp drop = patchTable("t", List.of("a"));
    PatchOp back = patchTable("t", List.of(), trow("a", "fresh"));
    for (boolean sentFirst : List.of(true, false)) {
      // within one patch: not selected after it, and the patch is not merged into the one before
      Held ch = new Held();
      Outbound out = tableOutbox(ch, 1);
      out.patch("p", "t", base, Outbound.Loans.NONE);
      if (sentFirst) flush(out, ch);
      out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
      out.patch("p", "t", patchTable("t", List.of(), trow("c", "3")), Outbound.Loans.NONE);
      out.patch("p", "t", same, Outbound.Loans.NONE);
      List<PatchOp> seq = List.of(base, value("t", "a"), patchTable("t", List.of(), trow("c", "3")), same);
      List<PatchOp> got = drained(out, ch);
      assertTrue(replayTable(seq).endsWith("selected=[]"), replayTable(seq));
      assertEquals(replayTable(seq), replayTable(got), "sent as " + got);
      assertEquals(seq, got, "a stood before the patches and is removed and present again: no merge");
      out.close();

      // with nothing selected the rule is the same: it does not depend on the selection
      ch = new Held();
      out = tableOutbox(ch, 1);
      out.patch("p", "t", base, Outbound.Loans.NONE);
      if (sentFirst) flush(out, ch);
      out.patch("p", "t", patchTable("t", List.of(), trow("c", "3")), Outbound.Loans.NONE);
      out.patch("p", "t", same, Outbound.Loans.NONE);
      got = drained(out, ch);
      assertEquals(List.of(base, patchTable("t", List.of(), trow("c", "3")), same), got);
      out.close();

      // across two patches: not selected, so the two are not folded into one that would keep it
      ch = new Held();
      out = tableOutbox(ch, 1);
      out.patch("p", "t", base, Outbound.Loans.NONE);
      if (sentFirst) flush(out, ch);
      out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
      out.patch("p", "t", drop, Outbound.Loans.NONE);
      out.patch("p", "t", back, Outbound.Loans.NONE);
      seq = List.of(base, value("t", "a"), drop, back);
      got = drained(out, ch);
      assertTrue(replayTable(seq).endsWith("selected=[]"), replayTable(seq));
      assertEquals(replayTable(seq), replayTable(got), "sent as " + got);
      assertEquals(seq, got);
      out.close();

      // and a set_table after them does not supersede the run that dropped it
      ch = new Held();
      out = tableOutbox(ch, 1);
      out.patch("p", "t", base, Outbound.Loans.NONE);
      if (sentFirst) flush(out, ch);
      out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
      out.patch("p", "t", drop, Outbound.Loans.NONE);
      out.patch("p", "t", base, Outbound.Loans.NONE);
      seq = List.of(base, value("t", "a"), drop, base);
      got = drained(out, ch);
      assertEquals(replayTable(seq), replayTable(got), "sent as " + got);
      out.close();
    }
  }

  /** A key that did not stand before the merged ops may be deleted and upserted again inside them: they merge. */
  @Test
  void aKeyNewToTheMergedOpsMayComeAndGo() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
    flush(out, ch);
    out.patch("p", "t", patchTable("t", List.of(), trow("c", "3")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of("c"), trow("c", "4")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of("c")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("c", "5")), Outbound.Loans.NONE);
    flush(out, ch);
    assertEquals(List.of(List.of(setTable("t", trow("a", "1"))), List.of(patchTable("t", List.of("c"), trow("c", "5")))), patches(ch));
    out.close();
  }

  /**
   * A burst of 1,000 small patches on one table, after a set_table and with no set_value sent, is
   * never refused: patches that may merge do, and those that may not fill the widget's slot, which
   * goes out whole when full while a new one starts. The host reaches the state the burst in order
   * does.
   */
  @Test
  void aBurstOfPatchesIsNeverRefused() throws Exception {
    for (boolean mergeable : List.of(true, false)) {
      Held ch = new Held();
      Outbound out = tableOutbox(ch, 1);
      List<PatchOp> seq = new ArrayList<>();
      seq.add(setTable("t", trow("a", "0")));
      out.patch("p", "t", seq.get(0), Outbound.Loans.NONE);
      flush(out, ch);
      for (int i = 0; i < 1000; i++) {
        // the unmergeable burst takes a row that stood before it out and back in each time
        PatchOp op = mergeable ? patchTable("t", List.of(), trow("k" + i, "v" + i)) : patchTable("t", List.of("a"), trow("a", "v" + i));
        seq.add(op);
        out.patch("p", "t", op, Outbound.Loans.NONE);
      }
      flush(out, ch);
      List<PatchOp> got = new ArrayList<>();
      for (List<PatchOp> p : patches(ch)) got.addAll(p);
      assertEquals(replayTable(seq), replayTable(got));
      if (mergeable) {
        assertEquals(2, got.size(), "the burst merges into one patch");
      } else {
        assertEquals(seq, got, "nothing merged, nothing lost");
        for (List<PatchOp> p : patches(ch)) assertTrue(p.size() <= Outbound.HELD_WHOLE_CAP, "a full slot went out whole: " + p.size());
        assertTrue(patches(ch).size() >= 1 + 1000 / Outbound.HELD_WHOLE_CAP, "the slot filled and went out: " + patches(ch).size());
      }
      out.close();
    }
  }

  /** Before this session's first set_table the host's rows are not known: patches go in order, unmerged. */
  @Test
  void noPatchMergesBeforeTheFirstSetTable() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", patchTable("t", List.of(), trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", "2")), Outbound.Loans.NONE);
    assertEquals(List.of(patchTable("t", List.of(), trow("a", "1")), patchTable("t", List.of(), trow("b", "2"))), drained(out, ch));
    assertEquals(0, out.superseded());
    out.close();
  }

  /** Once a set_table is sent, the table is known: adjacent valid patches merge, applied or folded. */
  @Test
  void patchesMergeOnceASetTableIsKnown() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
    flush(out, ch); // the set_table is sent
    out.patch("p", "t", patchTable("t", List.of("a"), trow("b", "2")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("c", "3")), Outbound.Loans.NONE);
    flush(out, ch);
    assertEquals(List.of(List.of(setTable("t", trow("a", "1"))), List.of(patchTable("t", List.of("a"), trow("b", "2"), trow("c", "3")))),
        patches(ch));
    out.close();

    // and a patch pending behind an unsent set_table is applied to it
    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "0")), Outbound.Loans.NONE);
    flush(out, ch);
    out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", "2")), Outbound.Loans.NONE);
    assertEquals(List.of(setTable("t", trow("a", "0")), setTable("t", trow("a", "1"), trow("b", "2"))), drained(out, ch));
    out.close();

    // but not when the rows before that set_table are not known: a key selected there that the
    // statement drops and the patch brings back is unselected in order, and would stay selected
    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", "2")), Outbound.Loans.NONE);
    assertEquals(List.of(setTable("t", trow("a", "1")), patchTable("t", List.of(), trow("b", "2"))), drained(out, ch));
    out.close();
  }

  /** With no declared column count the rows cannot be checked: nothing merges, everything goes in order. */
  @Test
  void noMergeWithoutDeclaredColumns() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 0);
    out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", "2")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("c", "3")), Outbound.Loans.NONE);
    assertEquals(List.of(setTable("t", trow("a", "1")), patchTable("t", List.of(), trow("b", "2")), patchTable("t", List.of(), trow("c", "3"))),
        drained(out, ch));
    out.close();
  }

  /**
   * An op the host would refuse is refused ALONE: what is pending goes ahead, it follows in a patch
   * of its own, and the valid ops after it still merge with each other. Nothing valid is lost.
   */
  @Test
  void aBadOpIsSentAloneWhileItsNeighboursStillMerge() throws Exception {
    PatchOp manyCells = PatchOp.newBuilder().setWidgetId("t").setPatchTable(vizi.plugin.v1.ViziPlugin.TablePatch.newBuilder()
        .addUpsert(vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey("c")
            .addCells(Value.newBuilder().setS("x")).addCells(Value.newBuilder().setS("y")))).build(); // 2 cells, 1 column
    for (PatchOp bad : List.of(manyCells,
        patchTable("t", List.of(), trow("", "empty key")),
        patchTable("t", List.of(), trow("k".repeat(129), "long key")),
        patchTable("t", List.of(), trow("d", "1"), trow("d", "2")), // a key twice in one op
        setTable("t", trow("b", "1"), trow("b", "2")))) {
      Held ch = new Held();
      Outbound out = tableOutbox(ch, 1);
      out.patch("p", "t", setTable("t", trow("a", "1")), Outbound.Loans.NONE);
      flush(out, ch);
      out.patch("p", "t", patchTable("t", List.of(), trow("p", "1")), Outbound.Loans.NONE);
      out.patch("p", "t", patchTable("t", List.of(), trow("q", "2")), Outbound.Loans.NONE); // merges with p
      out.patch("p", "t", bad, Outbound.Loans.NONE);
      out.patch("p", "t", patchTable("t", List.of(), trow("r", "3")), Outbound.Loans.NONE);
      out.patch("p", "t", patchTable("t", List.of(), trow("s", "4")), Outbound.Loans.NONE); // merges with r
      flush(out, ch);
      assertEquals(List.of(List.of(setTable("t", trow("a", "1"))), List.of(patchTable("t", List.of(), trow("p", "1"), trow("q", "2"))),
          List.of(bad), List.of(patchTable("t", List.of(), trow("r", "3"), trow("s", "4")))), patches(ch), bad.toString());
      out.close();
    }

    // the patch that would bring the 100,001st row: the patches beside it merge and land, it alone is refused
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    PatchOp whole = PatchOp.newBuilder().setWidgetId("t").setSetTable(vizi.plugin.v1.ViziPlugin.TableRows.newBuilder()
        .addAllRows(List.of(keyed("r", 30_000)))).build();
    out.patch("p", "t", whole, Outbound.Loans.NONE);
    flush(out, ch);
    for (String prefix : List.of("s", "u")) {
      out.patch("p", "t", patchTable("t", List.of(), keyed(prefix, 30_000)), Outbound.Loans.NONE);
      flush(out, ch);
    }
    // 90,000 held: 4,000 + 4,000 + 1,000 reach 99,000; the next 2,000 would pass 100,000; 1,000 more reach 100,000
    PatchOp a = patchTable("t", List.of(), keyed("a", 4_000));
    PatchOp b = patchTable("t", List.of(), keyed("b", 4_000));
    PatchOp c = patchTable("t", List.of(), keyed("c", 1_000));
    PatchOp crossing = patchTable("t", List.of(), keyed("x", 2_000));
    PatchOp d = patchTable("t", List.of(), keyed("d", 1_000));
    for (PatchOp op : List.of(a, b, c, crossing, d)) out.patch("p", "t", op, Outbound.Loans.NONE);
    flush(out, ch);
    List<List<PatchOp>> got = patches(ch).subList(3, patches(ch).size());
    assertEquals(3, got.size(), "the merged three, the crossing patch alone, then the last");
    assertEquals(9_000, got.get(0).get(0).getPatchTable().getUpsertCount());
    assertEquals(List.of(crossing), got.get(1));
    assertEquals(List.of(d), got.get(2));
    out.close();
  }

  /**
   * Order is kept around a selection: a set_table never overtakes a pending set_value, and no
   * patch merges across one; each goes after it, so the selection is applied to the rows it named.
   */
  @Test
  void noTableOpOvertakesOrMergesAcrossAPendingSelection() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1"), trow("b", "2")), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "b"), Outbound.Loans.NONE);
    out.patch("p", "t", setTable("t", trow("b", "3"), trow("c", "4")), Outbound.Loans.NONE);
    List<PatchOp> seq = List.of(setTable("t", trow("a", "1"), trow("b", "2")), value("t", "b"), setTable("t", trow("b", "3"), trow("c", "4")));
    List<PatchOp> got = drained(out, ch);
    assertEquals(seq, got);
    assertEquals(replayTable(seq), replayTable(got));
    out.close();

    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1"), trow("b", "2")), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of("a"), trow("c", "3")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("d", "4")), Outbound.Loans.NONE); // merges with the patch before it
    seq = List.of(setTable("t", trow("a", "1"), trow("b", "2")), value("t", "a"), patchTable("t", List.of("a"), trow("c", "3")),
        patchTable("t", List.of(), trow("d", "4")));
    got = drained(out, ch);
    assertEquals(List.of(setTable("t", trow("a", "1"), trow("b", "2")), value("t", "a"),
        patchTable("t", List.of("a"), trow("c", "3"), trow("d", "4"))), got);
    assertEquals(replayTable(seq), replayTable(got));
    out.close();

    // a selection after the rows it names stays after them
    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", patchTable("t", List.of("x"), trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
    out.patch("p", "t", setTable("t", trow("z", "9")), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "z"), Outbound.Loans.NONE); // after the rows that hold z; the first is never moved
    assertEquals(List.of(patchTable("t", List.of("x"), trow("a", "1")), value("t", "a"), setTable("t", trow("z", "9")), value("t", "z")),
        drained(out, ch));
    out.close();

    // a selection the host applies replaces the one directly before it; a refused one is kept beside
    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t", trow("a", "1"), trow("b", "2")), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "b"), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "nope"), Outbound.Loans.NONE);
    seq = List.of(setTable("t", trow("a", "1"), trow("b", "2")), value("t", "a"), value("t", "b"), value("t", "nope"));
    got = drained(out, ch);
    assertEquals(List.of(setTable("t", trow("a", "1"), trow("b", "2")), value("t", "b"), value("t", "nope")), got);
    assertEquals(replayTable(seq), replayTable(got));
    out.close();
  }

  /**
   * A selection on a declared table goes with the table's ops from the first, before any table op:
   * kept in order, not superseded as a value is. The host kept rows from before (a relaunched
   * panel), so k3 is selected, and k2, which it does not hold, is refused and leaves k3 standing.
   */
  @Test
  void aSelectionBeforeAnyTableOpIsKeptInOrder() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", value("t", "k3"), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "k2"), Outbound.Loans.NONE);
    List<PatchOp> seq = List.of(value("t", "k3"), value("t", "k2"));
    List<PatchOp> got = drained(out, ch);
    assertEquals(seq, got, "both kept, in order");
    assertEquals(0, out.superseded());
    HostTable inOrder = new HostTable();
    HostTable drainedTable = new HostTable();
    for (HostTable h : List.of(inOrder, drainedTable)) h.apply(setTable("t", trow("k1", "1"), trow("k3", "3")));
    seq.forEach(inOrder::apply);
    got.forEach(drainedTable::apply);
    assertEquals(List.of("k3"), drainedTable.selection, "k3 stays selected");
    assertEquals(inOrder.state(), drainedTable.state());
    out.close();
  }

  /** A set_table supersedes the table ops directly before it, which it restates. */
  @Test
  void aSetTableSupersedesTheAdjacentTableOps() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", patchTable("t", List.of(), trow("a", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", "1")), Outbound.Loans.NONE);
    out.patch("p", "t", setTable("t", trow("z", "9")), Outbound.Loans.NONE); // restates both
    assertEquals(List.of(setTable("t", trow("z", "9"))), drained(out, ch));
    assertEquals(2, out.superseded());
    out.close();
  }

  /**
   * A merge is bounded by the merged op's LARGER repeated field, rows or deleted keys each counted
   * on its own: 20,000 deletes and 20,000 upserts merge; 20,000 and 20,000 upserts do not.
   */
  @Test
  void aMergeIsBoundedByItsLargerRepeatedField() throws Exception {
    List<String> dels = new ArrayList<>();
    for (int i = 0; i < 20_000; i++) dels.add("d" + i);
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t"), Outbound.Loans.NONE);
    flush(out, ch);
    out.patch("p", "t", patchTable("t", dels), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), keyed("u", 20_000)), Outbound.Loans.NONE);
    flush(out, ch);
    assertEquals(2, patches(ch).size(), "the larger field is 20,000: merged");
    out.close();

    ch = new Held();
    out = tableOutbox(ch, 1);
    out.patch("p", "t", setTable("t"), Outbound.Loans.NONE);
    flush(out, ch);
    out.patch("p", "t", patchTable("t", List.of(), keyed("u", 20_000)), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), keyed("v", 20_000)), Outbound.Loans.NONE);
    flush(out, ch);
    List<List<PatchOp>> behind = patches(ch).subList(1, patches(ch).size());
    assertEquals(2, behind.size(), "40,000 rows would pass 32,768: queued behind, never refused");
    for (List<PatchOp> o : behind) assertTrue(o.get(0).getPatchTable().getUpsertCount() <= 32_768);
    out.close();
  }

  /**
   * A merge that would pass the 1 MiB bound is not made: what is pending goes ahead, in order, and
   * the patch is queued behind it. Never refused. A table's ops travel in a patch of their own.
   */
  @Test
  void aMergePastTheBoundIsQueuedBehindAndEveryPatchStaysUnderIt() throws Exception {
    Held ch = new Held();
    Outbound out = tableOutbox(ch, 1);
    String big = "x".repeat(600 * 1024);
    out.patch("p", "label", value("label", "hello"), Outbound.Loans.NONE);
    out.patch("p", "t", setTable("t", trow("a", big)), Outbound.Loans.NONE);
    out.patch("p", "t", value("t", "a"), Outbound.Loans.NONE);
    out.patch("p", "t", patchTable("t", List.of(), trow("b", big)), Outbound.Loans.NONE); // after the selection
    out.patch("p", "t", patchTable("t", List.of(), trow("c", "small")), Outbound.Loans.NONE); // merges with it
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    List<PluginMsg> got = ch.got();
    for (PluginMsg m : got) {
      assertTrue(m.getSerializedSize() <= (1 << 20), "every patch is at most 1 MiB: " + m.getSerializedSize());
      boolean table = m.getPanelPatch().getOpsList().stream().anyMatch(o -> o.hasSetTable() || o.hasPatchTable());
      if (table) {
        assertTrue(m.getPanelPatch().getOpsList().stream().allMatch(o -> o.getWidgetId().equals("t")), "a table's ops ride alone");
      }
    }
    assertEquals(3, got.size(), "the label's patch, the set_table and its selection, the merged patches: " + tally(got));
    List<PatchOp> ops = new ArrayList<>();
    for (PluginMsg m : got) for (PatchOp o : m.getPanelPatch().getOpsList()) if (o.getWidgetId().equals("t")) ops.add(o);
    assertEquals(List.of(setTable("t", trow("a", big)), value("t", "a"), patchTable("t", List.of(), trow("b", big), trow("c", "small"))), ops);
    out.close();
  }
}
