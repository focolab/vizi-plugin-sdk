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
}
