package vizi.plugin.javasdk.control;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.OverlayChunk;
import vizi.plugin.v1.ViziPlugin.OverlayLabelTable;
import vizi.plugin.v1.ViziPlugin.OverlayUpdate;
import vizi.plugin.v1.ViziPlugin.PanelPatch;
import vizi.plugin.v1.ViziPlugin.PatchOp;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.SegmentShortfall;

/**
 * Everything the plugin sends, in two structures (control_plane_protocol.md §5.4, §7):
 *
 * <ul>
 *   <li><b>one ordered queue</b> for replies, one-shot pushes and plugin requests, never
 *       dropped, sent in the order admitted;
 *   <li><b>a keyed coalescing map</b> for the coalescing pushes, one pending item per key:
 *       {@code (panel, widget)}, {@code (overlay)}, {@code (segment, region)} for a shortfall,
 *       {@code (overlay, level, t)} for a chunk, {@code (overlay)} for a label table and {@code
 *       (drag)} for a shape proposal. A new item
 *       replaces the pending one for its key; it never evicts another key.
 * </ul>
 *
 * The ordered queue drains first. A superseded push that named a loan hands the loan back at
 * replacement time (§5.6), through {@link Loans}; a push whose loan is declared for more than one
 * use is never merged away, since another message still names it.
 *
 * <p>A widget with a pending {@code set_options} is held whole: its ops are neither superseded
 * nor reordered until the batch drains. Past 256 held ops per widget the push is refused to its
 * caller (§5.4's sender rule), never faulted.
 *
 * <p>A list's rows are state its other ops depend on (§5.4). A {@code set_rows} naming
 * only keys the pending one has supersedes it in place; one that brings back a key the pending one
 * dropped is kept beside it, and the list is held whole from there, so the row starts fresh. A
 * superseding {@code set_value} goes after everything pending for its widget, so after the rows.
 * In the patch, each widget's ops keep their order, and the widgets with a pending {@code
 * set_rows} go first: a cell names a row the rows must already hold.
 */
public final class Outbound {

  /** Where messages go: the gRPC call. */
  public interface Channel {
    boolean isReady();

    void send(PluginMsg msg);
  }

  /** The loans a message names, and how to hand one back when the message is superseded unsent. */
  /**
   * What a push names in shared memory: {@code reclaim} hands the loans back if the push is
   * superseded or dropped unsent; {@code sent} runs once the message naming them has gone to the
   * channel. Exactly one of the two runs, and the claims that hold a retiring segment (§9.1) end in
   * whichever does.
   */
  public record Loans(List<Runnable> reclaim, List<Runnable> sent, boolean multiUse) {
    public static final Loans NONE = new Loans(List.of(), List.of(), false);

    public Loans(List<Runnable> reclaim, boolean multiUse) {
      this(reclaim, List.of(), multiUse);
    }

    public void reclaimAll() {
      reclaim.forEach(Runnable::run);
    }

    void sentAll() {
      sent.forEach(Runnable::run);
    }
  }

  /** A push refused to its caller: the queue could not admit it. */
  public static final class RefusedException extends RuntimeException {
    private static final long serialVersionUID = 1L;

    public RefusedException(String message) {
      super(message);
    }
  }

  public static final int HELD_WHOLE_CAP = 256;

  private sealed interface Key {}

  /** A panel op's key (§5.4): (panel, widget), and the row for a list's cell. */
  private record WidgetKey(String panel, String widget, String row) implements Key {}

  private record OverlayKey(String overlay) implements Key {}

  private record ShortfallKey(long segment, int region) implements Key {}

  private record ChunkKey(String overlay, int level, long t) implements Key {}

  private record TableKey(String overlay) implements Key {}

  /** A panel's Follow offer (§8.15): the latest label wins, {@code ""} withdrawing. */
  private record FollowOfferKey(String panel) implements Key {}

  /** A running Follow's state (§8.15): whole states, the latest wins. */
  private record FollowStateKey(long follow) implements Key {}

  /** A shape proposal (§8.8): the latest per drag. */
  private record ShapeKey(long drag) implements Key {}

  /** A piece of work's progress (§8.16): the latest report per request or task. */
  private record WorkKey(boolean task, String id) implements Key {}

  /** A widget's pending ops. */
  private static final class WidgetOps {
    final List<PatchOp> ops = new ArrayList<>();
    final List<Loans> loans = new ArrayList<>();
    boolean holdWhole;
  }

  private static final class Pending {
    @Nullable PluginMsg msg;
    Loans loans = Loans.NONE;
    @Nullable WidgetOps widget;
  }

  private final Object lock = new Object();
  private final ArrayDeque<PluginMsg> ordered = new ArrayDeque<>();
  /** The loans of an ordered message that names some (a multi-use push sent in order rather than merged). */
  private final java.util.IdentityHashMap<PluginMsg, Loans> orderedLoans = new java.util.IdentityHashMap<>();
  /** The loans of the message takeCoalesced just built; read by the writer under the lock. */
  private List<Loans> takenLoans = List.of();
  private final LinkedHashMap<Key, Pending> coalescing = new LinkedHashMap<>();
  private final Channel channel;
  private final Consumer<String> diagnostics;
  private final Thread writer;
  private boolean closed;
  private boolean sealed;
  private long sent;
  private long superseded;

  public Outbound(Channel channel, Consumer<String> diagnostics) {
    this.channel = channel;
    this.diagnostics = diagnostics;
    this.writer = new Thread(this::run, "vizi-outbound");
    this.writer.setDaemon(true);
    this.writer.start();
  }

  // ---- admission ----

  /** A reply, a one-shot push or a plugin request: ordered, never dropped. */
  public void send(PluginMsg msg) {
    synchronized (lock) {
      if (sealed) {
        diagnostics.accept("dropped after the stream closed: " + msg.getMsgCase());
        return;
      }
      ordered.add(msg);
      lock.notifyAll();
    }
  }

  /** A panel patch: one pending entry per (panel, widget), merged per property. */
  public void patch(String panelId, String widgetId, PatchOp op, Loans loans) {
    synchronized (lock) {
      if (sealed) {
        loans.reclaimAll();
        return;
      }
      WidgetKey k = new WidgetKey(panelId, widgetId, op.getRowKey());
      Pending p = coalescing.computeIfAbsent(k, x -> new Pending());
      if (p.widget == null) p.widget = new WidgetOps();
      WidgetOps w = p.widget;
      if (op.hasSetOptions()) w.holdWhole = true;
      if (w.holdWhole) {
        if (w.ops.size() >= HELD_WHOLE_CAP) {
          throw new RefusedException("widget " + widgetId + " has " + HELD_WHOLE_CAP
              + " ops held behind a set_options; the patch was not sent");
        }
        w.ops.add(op);
        w.loans.add(loans);
      } else if (op.hasSetRows()) {
        int same = -1;
        for (int i = 0; i < w.ops.size(); i++) if (w.ops.get(i).hasSetRows()) same = i;
        if (same < 0) {
          w.ops.add(op);
          w.loans.add(loans);
        } else if (w.ops.get(same).getSetRows().getKeysList().containsAll(op.getSetRows().getKeysList())) {
          w.ops.set(same, op); // only keys the pending rows have: supersedes them where they stood
          superseded++;
        } else {
          // a key the pending rows dropped comes back: merged, the removal would vanish and the
          // row would not start fresh; kept beside, and the list held whole from here
          w.holdWhole = true;
          w.ops.add(op);
          w.loans.add(loans);
        }
      } else {
        int same = -1;
        for (int i = 0; i < w.ops.size(); i++) if (w.ops.get(i).getOpCase() == op.getOpCase()) same = i;
        if (same >= 0 && !w.loans.get(same).multiUse()) {
          w.loans.get(same).reclaimAll();
          w.ops.remove(same);
          w.loans.remove(same);
          superseded++;
        }
        w.ops.add(op);
        w.loans.add(loans);
      }
      lock.notifyAll();
    }
  }

  /**
   * A list's rows changed: pending cell ops for the rows it dropped are superseded (the host
   * drops those rows, and would refuse an op naming one), their loans handed back.
   */
  public void dropRows(String panelId, java.util.Set<String> cellWidgets, java.util.Set<String> keptRows) {
    synchronized (lock) {
      Iterator<Map.Entry<Key, Pending>> it = coalescing.entrySet().iterator();
      while (it.hasNext()) {
        Map.Entry<Key, Pending> e = it.next();
        if (e.getKey() instanceof WidgetKey k && k.panel().equals(panelId) && !k.row().isEmpty()
            && cellWidgets.contains(k.widget()) && !keptRows.contains(k.row()) && e.getValue().widget != null) {
          for (Loans l : e.getValue().widget.loans) l.reclaimAll();
          it.remove();
          superseded++;
        }
      }
    }
  }

  /** A full-replacement overlay update, one pending per overlay. */
  public void overlayUpdate(OverlayUpdate u, Loans loans) {
    replaceable(new OverlayKey(u.getOverlayId()), PluginMsg.newBuilder().setOverlayUpdate(u).build(), loans);
  }

  /**
   * An overlay's removal: one-shot, and it supersedes every pending coalescing push for that
   * overlay, whose loans come back at once.
   */
  public void overlayRemove(PluginMsg remove) {
    String id = remove.getOverlayRemove().getOverlayId();
    synchronized (lock) {
      Iterator<Map.Entry<Key, Pending>> it = coalescing.entrySet().iterator();
      while (it.hasNext()) {
        Map.Entry<Key, Pending> e = it.next();
        boolean mine = switch (e.getKey()) {
          case OverlayKey o -> o.overlay().equals(id);
          case ChunkKey c -> c.overlay().equals(id);
          case TableKey t -> t.overlay().equals(id);
          default -> false;
        };
        if (mine && !e.getValue().loans.multiUse()) {
          e.getValue().loans.reclaimAll();
          it.remove();
          superseded++;
        }
      }
    }
    send(remove);
  }

  /** A chunk of a chunked overlay; {@code t} absent is its own key, never t = 0. */
  public void chunk(OverlayChunk c, Loans loans) {
    long t = c.hasT() ? Integer.toUnsignedLong(c.getT()) : -1;
    replaceable(new ChunkKey(c.getOverlayId(), c.getLevel(), t), PluginMsg.newBuilder().setOverlayChunk(c).build(), loans);
  }

  public void labelTable(OverlayLabelTable t, Loans loans) {
    replaceable(new TableKey(t.getOverlayId()), PluginMsg.newBuilder().setOverlayLabelTable(t).build(), loans);
  }

  /** A panel's Follow offer: coalescing, keyed by the panel ({@code label ""} withdraws). */
  public void offerFollow(vizi.plugin.v1.ViziPlugin.OfferFollow o) {
    replaceable(new FollowOfferKey(o.getPanelId()), PluginMsg.newBuilder().setOfferFollow(o).build(), Loans.NONE);
  }

  /** Progress on a request or a background task: coalescing, keyed by its subject (§8.16). */
  public void workProgress(vizi.plugin.v1.ViziPlugin.WorkProgress w) {
    boolean task = w.getSubjectCase() == vizi.plugin.v1.ViziPlugin.WorkProgress.SubjectCase.TASK_ID;
    replaceable(new WorkKey(task, task ? w.getTaskId() : w.getRequestId()), PluginMsg.newBuilder().setWorkProgress(w).build(), Loans.NONE);
  }

  /** A request was answered: its pending progress is dropped, since the host would drop it too. */
  public void dropRequestProgress(String requestId) {
    synchronized (lock) {
      if (coalescing.remove(new WorkKey(false, requestId)) != null) superseded++;
    }
  }

  /** A {@code shape_proposal}: coalescing, keyed by its drag (§8.8). */
  public void shapeProposal(PluginMsg proposal, long dragId) {
    replaceable(new ShapeKey(dragId), proposal, Loans.NONE);
  }

  /** The drag's commit or end arrived: a proposal for it still pending is withdrawn unsent. */
  public void dropShapeProposal(long dragId) {
    synchronized (lock) {
      if (coalescing.remove(new ShapeKey(dragId)) != null) superseded++;
    }
  }

  /** A Follow's whole state: coalescing, keyed by the Follow. */
  public void followState(vizi.plugin.v1.ViziPlugin.FollowState s) {
    replaceable(new FollowStateKey(s.getFollowId()), PluginMsg.newBuilder().setFollowState(s).build(), Loans.NONE);
  }

  /** The plugin's own end of a Follow: one-shot, and it supersedes that Follow's pending state. */
  public void endFollow(long followId) {
    synchronized (lock) {
      if (coalescing.remove(new FollowStateKey(followId)) != null) superseded++;
    }
    send(PluginMsg.newBuilder().setEndFollow(vizi.plugin.v1.ViziPlugin.EndFollow.newBuilder().setFollowId(followId)).build());
  }

  /**
   * A shortfall, merged in place: the largest {@code wanted_bytes} with its own {@code
   * largest_free_bytes}, and {@code dropped} summed.
   */
  public void shortfall(SegmentShortfall s) {
    synchronized (lock) {
      if (sealed) return;
      ShortfallKey k = new ShortfallKey(s.getSegmentId(), s.getRegion());
      Pending p = coalescing.computeIfAbsent(k, x -> new Pending());
      SegmentShortfall merged = s;
      if (p.msg != null) {
        SegmentShortfall held = p.msg.getSegmentShortfall();
        SegmentShortfall bigger = Long.compareUnsigned(s.getWantedBytes(), held.getWantedBytes()) > 0 ? s : held;
        merged = bigger.toBuilder().setDropped(held.getDropped() + s.getDropped()).build();
      }
      p.msg = PluginMsg.newBuilder().setSegmentShortfall(merged).build();
      lock.notifyAll();
    }
  }

  /** Messages the writer has taken from the queues and not yet handed to the channel. */
  private int inFlight;

  private void replaceable(Key k, PluginMsg msg, Loans loans) {
    synchronized (lock) {
      if (sealed) {
        loans.reclaimAll();
        return;
      }
      Pending p = coalescing.get(k);
      if (p != null && p.msg != null) {
        if (p.loans.multiUse()) {
          // another message still names these bytes: send the held one in order rather than merge
          ordered.add(p.msg);
          orderedLoans.put(p.msg, p.loans);
        } else {
          p.loans.reclaimAll();
          superseded++;
        }
      }
      Pending n = new Pending();
      n.msg = msg;
      n.loans = loans;
      coalescing.remove(k);
      coalescing.put(k, n);
      lock.notifyAll();
    }
  }

  // ---- the writer ----

  private List<Loans> nextLoans = List.of(); // the writer thread's own

  private void run() {
    while (true) {
      PluginMsg next;
      synchronized (lock) {
        while (!closed && ((ordered.isEmpty() && coalescing.isEmpty()) || !channel.isReady())) {
          try {
            lock.wait(channel.isReady() ? 0 : 50);
          } catch (InterruptedException e) {
            return;
          }
        }
        if (closed && (ordered.isEmpty() && coalescing.isEmpty() || !channel.isReady())) {
          lock.notifyAll();
          return;
        }
        takenLoans = List.of();
        if (ordered.isEmpty()) {
          next = takeCoalesced();
        } else {
          next = ordered.poll();
          Loans l = orderedLoans.remove(next);
          if (l != null) takenLoans = List.of(l);
        }
        nextLoans = takenLoans;
        if (next != null) inFlight++; // taken but not yet handed over: drain() waits for it too
        lock.notifyAll();
      }
      if (next == null) continue;
      try {
        channel.send(next);
        synchronized (lock) {
          sent++;
        }
      } catch (RuntimeException e) {
        diagnostics.accept("sending " + next.getMsgCase() + " failed: " + e);
      } finally {
        // sent (or gone for good): the claims its loans held on their segments end now (§9.1)
        for (Loans l : nextLoans) l.sentAll();
        synchronized (lock) {
          inFlight--;
          lock.notifyAll();
        }
      }
    }
  }

  private @Nullable PluginMsg takeCoalesced() {
    Iterator<Map.Entry<Key, Pending>> it = coalescing.entrySet().iterator();
    if (!it.hasNext()) return null;
    Map.Entry<Key, Pending> e = it.next();
    it.remove();
    Pending p = e.getValue();
    if (e.getKey() instanceof WidgetKey wk && p.widget != null) {
      // one patch per panel: every pending widget of the same panel rides along
      List<List<PatchOp>> groups = new ArrayList<>();
      groups.add(p.widget.ops);
      List<Loans> loans = new ArrayList<>(p.widget.loans);
      Iterator<Map.Entry<Key, Pending>> more = coalescing.entrySet().iterator();
      while (more.hasNext()) {
        Map.Entry<Key, Pending> m = more.next();
        if (m.getKey() instanceof WidgetKey w2 && w2.panel().equals(wk.panel()) && m.getValue().widget != null) {
          groups.add(m.getValue().widget.ops);
          loans.addAll(m.getValue().widget.loans);
          more.remove();
        }
      }
      takenLoans = loans;
      // a list's ops before any cell op: a cell names a row set_rows must already hold. The
      // sort is by widget and stable, so each widget's own ops keep their order: a list's selection
      // stays after the rows it was sent after
      groups.sort(java.util.Comparator.comparingInt(g -> g.stream().anyMatch(PatchOp::hasSetRows) ? 0 : 1));
      List<PatchOp> ops = new ArrayList<>();
      for (List<PatchOp> g : groups) ops.addAll(g);
      return PluginMsg.newBuilder().setPanelPatch(PanelPatch.newBuilder().setPanelId(wk.panel()).addAllOps(ops)).build();
    }
    takenLoans = List.of(p.loans);
    return p.msg;
  }

  /** Wakes the writer after the channel became ready. */
  public void onReady() {
    synchronized (lock) {
      lock.notifyAll();
    }
  }

  /** Waits until everything admitted so far has been handed to the channel, or the timeout. */
  public boolean drain(long timeoutMs) throws InterruptedException {
    long deadline = System.currentTimeMillis() + timeoutMs;
    synchronized (lock) {
      while (!ordered.isEmpty() || !coalescing.isEmpty() || inFlight > 0) {
        long left = deadline - System.currentTimeMillis();
        if (left <= 0) return false;
        lock.wait(Math.min(left, 50));
      }
      return true;
    }
  }

  /** Refuses further admissions (the stream is closing). */
  public void seal() {
    synchronized (lock) {
      sealed = true;
    }
  }

  /** Stops the writer; what is still queued is abandoned. */
  public void close() {
    synchronized (lock) {
      closed = true;
      sealed = true;
      lock.notifyAll();
    }
  }

  public long sent() {
    synchronized (lock) {
      return sent;
    }
  }

  public long superseded() {
    synchronized (lock) {
      return superseded;
    }
  }

  public int queued() {
    synchronized (lock) {
      return ordered.size() + coalescing.size();
    }
  }
}
