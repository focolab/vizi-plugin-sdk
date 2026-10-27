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
import vizi.plugin.v1.ViziPlugin.TablePatch;
import vizi.plugin.v1.ViziPlugin.TableRow;
import vizi.plugin.v1.ViziPlugin.TableRows;
import vizi.plugin.javasdk.manifest.WidgetDecl;

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
 *
 * <p>A table's rows are the third such state (§8.4). The outbox simulates each table from the
 * first {@code set_table} it admits, and checks every table op against that table, the declared
 * columns and every bound; an op the host would refuse is sent alone, merged with nothing. Only
 * valid, adjacent table ops merge: a {@code set_table} supersedes the table ops directly before
 * it, and a {@code patch_table} merges into the one directly before it once the table and its
 * columns are known, each only when no row that stood before the merged ops is removed by one and
 * present after them, so that the merge leaves any selection as the ops in order would. A
 * {@code set_value} is never moved: it replaces only a selection directly before it, and only when
 * the host will apply it. No merge crosses a pending {@code set_value}. A merge that would pass 1 MiB is
 * not made: what is pending goes ahead and the patch is queued behind it. A table's ops drain in a
 * patch of their own, so no merge of other widgets takes it past the bound.
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
    /** A table's: the run of table ops at the tail, since the last other op. */
    @Nullable TableRun run;
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
  /** How many columns a table declares, by (panel, widget): what a row's cells are checked against. */
  private volatile java.util.function.ToIntBiFunction<String, String> tableColumns = (panel, widget) -> 0;

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
      } else if (op.hasSetTable() || op.hasPatchTable()) {
        tableOp(panelId, w, op, loans);
      } else if (op.hasSetValue() && (tables.containsKey(List.of(panelId, op.getWidgetId()))
          || tableColumns.applyAsInt(panelId, op.getWidgetId()) > 0)) {
        // a table's selection, by its rows sent or by its declaration
        tableSelect(panelId, w, op, loans);
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
   * What one widget's pending ops may encode to when they carry a table op: under the 1 MiB a
   * {@code PanelPatch} carrying one may be (§8.4, §9), with room for the panel's id.
   */
  public static final int TABLE_PATCH_BUDGET = WidgetDecl.Table.MAX_PATCH_BYTES - 1024;

  /**
   * A table op's larger repeated field, its rows or its deleted keys each counted on its own, is at
   * most {@link WidgetDecl.Table#MAX_ROWS_PER_MESSAGE}: a merge that would pass it is queued behind.
   */
  public static final int TABLE_MAX_ENTRIES = WidgetDecl.Table.MAX_ROWS_PER_MESSAGE;

  /**
   * The rows the host will hold for one table widget, as the ops this outbox has admitted leave
   * them (an op the host will refuse is not applied). They are known only once a {@code set_table}
   * has been admitted this session.
   */
  private static final class TableSim {
    boolean known;
    final java.util.LinkedHashSet<String> keys = new java.util.LinkedHashSet<>();

    void forget() {
      known = false;
      keys.clear();
    }
  }

  /**
   * A table's merge record, for the table ops at the tail of a widget's pending ops (the run, since
   * the last other op): the rows that stood before the run and those of them the run has removed;
   * the same for the last pending op alone. "Removed" counts a key a patch deletes even when the
   * same patch upserts it again, since that is a fresh row. Merged ops must not undo such a
   * removal: a row that stood before them, removed and present again after them, loses any
   * selection in order and would keep it merged. Any row may be selected, so any such row blocks
   * the merge, whatever this end knows of the selection.
   */
  private static final class TableRun {
    /** Null where the rows are not known here (before this session's first statement): any row. */
    java.util.@Nullable Set<String> runBase;
    Removed runRemoved;
    java.util.@Nullable Set<String> tailBase;
    Removed tailRemoved;

    /** A run that an op starts: {@code before} the rows it lands on, {@code gone} those it removes. */
    TableRun(java.util.@Nullable Set<String> before, Removed gone) {
      runBase = before;
      runRemoved = gone;
      tailBase = before;
      tailRemoved = gone;
    }

    /** An op held as its own at the end of this run. */
    void extend(java.util.@Nullable Set<String> before, Removed gone) {
      runRemoved = runRemoved.union(gone.within(runBase));
      tailBase = before;
      tailRemoved = gone;
    }
  }

  /**
   * Keys removed: a finite set, or (a statement landing on rows not known here) every key but a
   * finite set.
   */
  private record Removed(boolean allBut, java.util.Set<String> keys) {
    static Removed of(java.util.Set<String> keys) {
      return new Removed(false, keys);
    }

    boolean contains(String k) {
      return allBut != keys.contains(k);
    }

    Removed union(Removed o) {
      java.util.Set<String> next;
      if (!allBut && !o.allBut) {
        next = new java.util.HashSet<>(keys);
        next.addAll(o.keys);
        return of(next);
      }
      if (allBut && o.allBut) {
        next = new java.util.HashSet<>(keys);
        next.retainAll(o.keys);
      } else {
        Removed but = allBut ? this : o;
        Removed fin = allBut ? o : this;
        next = new java.util.HashSet<>(but.keys);
        next.removeAll(fin.keys);
      }
      return new Removed(true, next);
    }

    /** Those of these keys among {@code base}; all of them when the base is not known. */
    Removed within(java.util.@Nullable Set<String> base) {
      if (base == null) return this;
      java.util.Set<String> next = new java.util.HashSet<>();
      if (allBut) {
        for (String k : base) if (!keys.contains(k)) next.add(k);
      } else {
        for (String k : keys) if (base.contains(k)) next.add(k);
      }
      return of(next);
    }

    /** Whether any of these keys is among {@code rows}. */
    boolean anyIn(java.util.Set<String> rows) {
      if (allBut) {
        for (String k : rows) if (!keys.contains(k)) return true;
        return false;
      }
      for (String k : keys) if (rows.contains(k)) return true;
      return false;
    }
  }

  /** The simulated tables, by (panel, widget). */
  private final Map<List<String>, TableSim> tables = new java.util.HashMap<>();

  /**
   * §8.4's rule for a table's two ops, under the lock. Every op is first checked against the
   * simulated table and every bound ({@link #tableRefusal}); one the host would refuse goes {@link
   * #alone}, merged with nothing. A valid op merges only with the table ops directly before it in
   * the widget's pending ops (the run: no {@code set_value} or other op between), so no merge
   * crosses a pending selection and a {@code set_table} never overtakes one. A merge is made only
   * when the declared column count and the rows are known (a {@code set_table} was admitted this
   * session), and only when it reaches the state the ops in order would for any selection: no row
   * that stood before the merged ops is removed by one and present after them ({@link TableRun}).
   *
   * <ul>
   *   <li>a {@code set_table} supersedes the run it restates;
   *   <li>a {@code patch_table} merges into the op before it (applied to a {@code set_table}, folded
   *       into a {@code patch_table}); otherwise it goes in order, unmerged.
   * </ul>
   *
   * A merge that would take the widget's pending ops past {@link #TABLE_PATCH_BUDGET} or {@link
   * #TABLE_MAX_ENTRIES} is not made: what is pending goes ahead, in order, on the ordered queue, and
   * the new op starts the widget's slot afresh. So do {@link #HELD_WHOLE_CAP} pending ops. Never a
   * fault.
   */
  private void tableOp(String panelId, WidgetOps w, PatchOp op, Loans loans) {
    int columns = tableColumns.applyAsInt(panelId, op.getWidgetId());
    TableSim sim = tables.computeIfAbsent(List.of(panelId, op.getWidgetId()), k -> new TableSim());
    String refused = tableRefusal(op, columns, sim);
    if (refused != null) {
      alone(panelId, w, op, loans, refused);
      return;
    }
    if (columns <= 0) {
      // the rows cannot be checked, so the host may refuse them: nothing is known, nothing merges
      sim.forget();
      append(panelId, w, op, loans, null);
      return;
    }
    // the rows the op lands on (none known before the first statement), and those it removes: a
    // key a patch deletes counts even when the patch upserts it again, a fresh row
    boolean wasKnown = sim.known;
    java.util.@Nullable Set<String> before = wasKnown ? new java.util.HashSet<>(sim.keys) : null;
    Removed gone;
    if (op.hasSetTable()) {
      java.util.Set<String> rows = new java.util.HashSet<>();
      for (TableRow r : op.getSetTable().getRowsList()) rows.add(r.getKey());
      if (before == null) {
        gone = new Removed(true, rows);
      } else {
        java.util.Set<String> g = new java.util.HashSet<>();
        for (String k : before) if (!rows.contains(k)) g.add(k);
        gone = Removed.of(g);
      }
    } else {
      gone = Removed.of(new java.util.HashSet<>(op.getPatchTable().getDeleteList())).within(before);
    }
    TableRun fresh = new TableRun(before, gone);
    int last = w.ops.size() - 1;
    TableRun run = last >= 0 && isTable(w.ops.get(last)) ? w.run : null;
    apply(sim, op);
    if (run != null && op.hasSetTable()) {
      Removed removed = run.runRemoved.union(gone.within(run.runBase));
      if (!removed.anyIn(sim.keys)) {
        // whole rows restate the run before them, and take its place
        while (!w.ops.isEmpty() && isTable(w.ops.get(w.ops.size() - 1))) {
          int i = w.ops.size() - 1;
          w.loans.get(i).reclaimAll();
          w.ops.remove(i);
          w.loans.remove(i);
          superseded++;
        }
        run.runRemoved = removed;
        run.tailRemoved = removed;
        run.tailBase = run.runBase;
        append(panelId, w, op, loans, fresh);
        return;
      }
    } else if (run != null && wasKnown) {
      Removed removed = run.tailRemoved.union(gone.within(run.tailBase));
      if (!removed.anyIn(sim.keys)) {
        PatchOp pending = w.ops.get(last);
        PatchOp merged = pending.hasSetTable() ? tableApplied(pending, op) : tableFolded(pending, op);
        if (fits(w, last, merged)) {
          w.ops.set(last, merged);
          w.loans.set(last, combined(w.loans.get(last), loans));
          superseded++;
          run.tailRemoved = removed;
          run.runRemoved = run.runRemoved.union(gone.within(run.runBase));
          return;
        }
        // queued behind what is pending: nothing is superseded, and the op starts a run of its own
        flushAhead(panelId, w);
        w.run = fresh;
        append(panelId, w, op, loans, fresh);
        return;
      }
    }
    // held as its own op: at the end of the run, or starting one
    if (run != null) run.extend(before, gone);
    else w.run = fresh;
    append(panelId, w, op, loans, fresh);
  }

  /**
   * {@code op} added to the widget's pending ops. When it would take them past one message, or past
   * {@link #HELD_WHOLE_CAP}, what is pending goes ahead first and the op starts a run of its own,
   * {@code fresh}.
   */
  private void append(String panelId, WidgetOps w, PatchOp op, Loans loans, @Nullable TableRun fresh) {
    if (!w.ops.isEmpty() && (!fits(w, -1, op) || w.ops.size() >= HELD_WHOLE_CAP)) {
      flushAhead(panelId, w);
      w.run = fresh;
    }
    if (fresh == null) w.run = null;
    w.ops.add(op);
    w.loans.add(loans);
  }

  private static boolean isTable(PatchOp op) {
    return op.hasSetTable() || op.hasPatchTable();
  }

  /**
   * A table's {@code set_value}, under the lock: never moved relative to the table ops around it. It
   * replaces the selection directly before it only when the host will apply it (one key the table
   * holds, or none); a refused selection leaves the one before it standing, so it is kept beside.
   */
  private void tableSelect(String panelId, WidgetOps w, PatchOp op, Loans loans) {
    TableSim sim = tables.computeIfAbsent(List.of(panelId, op.getWidgetId()), k -> new TableSim());
    var v = op.getSetValue();
    List<String> keys = List.of();
    boolean shaped = true; // a key, or a list of keys: anything else is refused
    if (v.hasS()) keys = v.getS().isEmpty() ? List.of() : List.of(v.getS());
    else if (v.hasStrings()) keys = List.copyOf(new java.util.LinkedHashSet<>(v.getStrings().getValuesList()));
    else shaped = false;
    boolean holds = shaped && sim.known && sim.keys.containsAll(keys);
    int last = w.ops.size() - 1;
    if (holds && keys.size() <= 1 && last >= 0 && w.ops.get(last).hasSetValue() && !w.loans.get(last).multiUse()) {
      w.loans.get(last).reclaimAll();
      w.ops.set(last, op);
      w.loans.set(last, loans);
      superseded++;
    } else {
      if (!w.ops.isEmpty() && w.ops.size() >= HELD_WHOLE_CAP) flushAhead(panelId, w);
      w.ops.add(op);
      w.loans.add(loans);
    }
  }

  /** The simulated rows after a valid op: a statement replaces; a patch deletes, then upserts in place or appends. */
  private static void apply(TableSim sim, PatchOp op) {
    if (op.hasSetTable()) {
      sim.known = true;
      sim.keys.clear();
      for (TableRow r : op.getSetTable().getRowsList()) sim.keys.add(r.getKey());
    } else if (sim.known) {
      sim.keys.removeAll(op.getPatchTable().getDeleteList());
      for (TableRow r : op.getPatchTable().getUpsertList()) sim.keys.add(r.getKey());
    }
  }

  /**
   * How many columns each table declares, by (panel, widget); 0 when not known. Rows are checked
   * against it, and with no declared count no patches are merged.
   */
  public void tableColumns(java.util.function.ToIntBiFunction<String, String> columns) {
    this.tableColumns = java.util.Objects.requireNonNull(columns, "columns");
  }

  /**
   * Why the host would refuse a table op (§8.4, §9), or null: a bad key (empty or past 128
   * characters), a key twice in the op, more cells than the declared columns (or than 32), more
   * than 1 MiB or 32,768 entries in a repeated field, or more than 100,000 rows after it. The row
   * count is judged against the simulated table, so only once a {@code set_table} is known.
   */
  private static @Nullable String tableRefusal(PatchOp op, int columns, TableSim sim) {
    String id = op.getWidgetId();
    List<TableRow> rows = op.hasSetTable() ? op.getSetTable().getRowsList() : op.getPatchTable().getUpsertList();
    int cap = columns > 0 ? Math.min(columns, WidgetDecl.Table.MAX_COLUMNS) : WidgetDecl.Table.MAX_COLUMNS;
    java.util.Set<String> keys = new java.util.HashSet<>();
    for (TableRow r : rows) {
      String k = r.getKey();
      int chars = k.codePointCount(0, k.length());
      if (chars == 0 || chars > WidgetDecl.Table.MAX_KEY) return "table " + id + " row key '" + k + "' is not 1 to " + WidgetDecl.Table.MAX_KEY + " characters";
      if (!keys.add(k)) return "table " + id + " row key '" + k + "' appears twice in one op";
      if (r.getCellsCount() > cap) return "row '" + k + "' of table " + id + " has " + r.getCellsCount() + " cells, and the table " + cap + " columns";
    }
    int deletes = op.hasPatchTable() ? op.getPatchTable().getDeleteCount() : 0;
    if (Math.max(rows.size(), deletes) > TABLE_MAX_ENTRIES) {
      return "a table op of table " + id + " carries " + Math.max(rows.size(), deletes) + " entries in one repeated field, past " + TABLE_MAX_ENTRIES;
    }
    if (op.getSerializedSize() + 8L > TABLE_PATCH_BUDGET) return "a table op of table " + id + " encodes past one message (1 MiB)";
    long after;
    if (op.hasSetTable()) {
      after = rows.size();
    } else if (sim.known) {
      java.util.Set<String> gone = new java.util.HashSet<>();
      for (String k : op.getPatchTable().getDeleteList()) if (sim.keys.contains(k)) gone.add(k);
      long added = 0;
      for (TableRow r : rows) if (!sim.keys.contains(r.getKey()) || gone.contains(r.getKey())) added++;
      after = sim.keys.size() - gone.size() + added;
    } else {
      return null; // the base is not known here; the panel that sends it judges the row count
    }
    if (after > WidgetDecl.Table.MAX_ROWS) return "table " + id + " would hold " + after + " rows, past its " + WidgetDecl.Table.MAX_ROWS;
    return null;
  }

  /**
   * A table op the host will refuse, sent ALONE and in order: what is pending for the widget goes
   * ahead, and the op follows in a patch of its own that nothing later merges into. Merged or
   * superseding, it would take the valid ops with it; applied in order, only it is refused.
   */
  private void alone(String panelId, WidgetOps w, PatchOp op, Loans loans, String why) {
    diagnostics.accept("a table op the host will refuse is sent on its own, merged with nothing: " + why);
    if (!w.ops.isEmpty()) flushAhead(panelId, w);
    PluginMsg msg = PluginMsg.newBuilder().setPanelPatch(PanelPatch.newBuilder().setPanelId(panelId).addOps(op)).build();
    ordered.add(msg);
    if (loans != Loans.NONE) orderedLoans.put(msg, loans);
  }


  /** Whether {@code w}'s pending ops, with {@code op} in place of the one at {@code replacing} (or added), stay in bounds. */
  private static boolean fits(WidgetOps w, int replacing, PatchOp op) {
    long bytes = op.getSerializedSize() + 8L;
    for (int i = 0; i < w.ops.size(); i++) if (i != replacing) bytes += w.ops.get(i).getSerializedSize() + 8L;
    int rows = op.hasSetTable() ? op.getSetTable().getRowsCount() : op.getPatchTable().getUpsertCount();
    int deletes = op.hasPatchTable() ? op.getPatchTable().getDeleteCount() : 0;
    return bytes <= TABLE_PATCH_BUDGET && Math.max(rows, deletes) <= TABLE_MAX_ENTRIES; // the larger repeated field
  }

  /** {@code w}'s pending ops go ahead as one patch on the ordered queue, which drains first. */
  private void flushAhead(String panelId, WidgetOps w) {
    PluginMsg msg = PluginMsg.newBuilder().setPanelPatch(PanelPatch.newBuilder().setPanelId(panelId).addAllOps(w.ops)).build();
    Loans all = Loans.NONE;
    for (Loans l : w.loans) all = combined(all, l);
    ordered.add(msg);
    if (all != Loans.NONE) orderedLoans.put(msg, all);
    w.ops.clear();
    w.loans.clear();
  }

  private static Loans combined(Loans a, Loans b) {
    if (b == Loans.NONE || (b.reclaim().isEmpty() && b.sent().isEmpty())) return a;
    if (a == Loans.NONE || (a.reclaim().isEmpty() && a.sent().isEmpty())) return b;
    List<Runnable> reclaim = new ArrayList<>(a.reclaim());
    reclaim.addAll(b.reclaim());
    List<Runnable> sent = new ArrayList<>(a.sent());
    sent.addAll(b.sent());
    return new Loans(List.copyOf(reclaim), List.copyOf(sent), a.multiUse() || b.multiUse());
  }

  /**
   * A pending {@code set_table} with a later {@code patch_table} applied to it: the rows the host
   * would hold after both, as one statement. Deletes first, then each upsert in place or appended.
   */
  static PatchOp tableApplied(PatchOp whole, PatchOp patch) {
    java.util.Set<String> gone = new java.util.HashSet<>(patch.getPatchTable().getDeleteList());
    List<TableRow> rows = new ArrayList<>();
    Map<String, Integer> at = new java.util.HashMap<>();
    for (TableRow r : whole.getSetTable().getRowsList()) {
      if (gone.contains(r.getKey())) continue;
      at.put(r.getKey(), rows.size());
      rows.add(r);
    }
    for (TableRow r : patch.getPatchTable().getUpsertList()) {
      Integer i = at.get(r.getKey());
      if (i == null) {
        at.put(r.getKey(), rows.size());
        rows.add(r);
      } else {
        rows.set(i, r);
      }
    }
    return whole.toBuilder().setSetTable(TableRows.newBuilder().addAllRows(rows)).build();
  }

  /**
   * Two {@code patch_table}s as one, reaching the state the two in order would: the deletes of
   * both, then the upserts in order. A key the later one deletes leaves the earlier one's upserts;
   * one it upserts after a delete stays deleted AND upserted, so the host still appends it fresh.
   */
  static PatchOp tableFolded(PatchOp earlier, PatchOp later) {
    java.util.LinkedHashSet<String> deletes = new java.util.LinkedHashSet<>(earlier.getPatchTable().getDeleteList());
    LinkedHashMap<String, TableRow> upserts = new LinkedHashMap<>();
    for (TableRow r : earlier.getPatchTable().getUpsertList()) upserts.put(r.getKey(), r);
    for (String k : later.getPatchTable().getDeleteList()) {
      upserts.remove(k);
      deletes.add(k);
    }
    for (TableRow r : later.getPatchTable().getUpsertList()) upserts.put(r.getKey(), r); // in place when pending, else last
    return earlier.toBuilder()
        .setPatchTable(TablePatch.newBuilder().addAllDelete(deletes).addAllUpsert(upserts.values()))
        .build();
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
      // one patch per panel: every pending widget of the same panel rides along, except that a
      // table's ops travel in a patch of their own, which their slot keeps under 1 MiB (§8.4)
      List<List<PatchOp>> groups = new ArrayList<>();
      groups.add(p.widget.ops);
      List<Loans> loans = new ArrayList<>(p.widget.loans);
      boolean alone = carriesTable(p.widget.ops);
      Iterator<Map.Entry<Key, Pending>> more = coalescing.entrySet().iterator();
      while (!alone && more.hasNext()) {
        Map.Entry<Key, Pending> m = more.next();
        if (m.getKey() instanceof WidgetKey w2 && w2.panel().equals(wk.panel()) && m.getValue().widget != null
            && !carriesTable(m.getValue().widget.ops)) {
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

  private static boolean carriesTable(List<PatchOp> ops) {
    for (PatchOp o : ops) if (o.hasSetTable() || o.hasPatchTable()) return true;
    return false;
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
