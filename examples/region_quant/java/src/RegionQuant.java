import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.channelPicker;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.image;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;

import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.ArrayDeque;
import java.util.OptionalInt;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.locks.Condition;
import java.util.concurrent.locks.ReentrantLock;
import vizi.plugin.javasdk.CommandContext;
import vizi.plugin.javasdk.Follow;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Subscriptions;
import vizi.plugin.javasdk.ViewCause;
import vizi.plugin.javasdk.ViewMove;
import vizi.plugin.javasdk.ViewResult;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.control.SessionEndedException;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ViewTarget;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.ReadDataVolume;
import vizi.plugin.v1.ViziPlugin.ReadRoiInclusion;
import vizi.plugin.v1.ViziPlugin.RoiRecord;

/**
 * Region quant (../SPEC.md): every region's mean on one channel at the frame on screen, kept
 * current from the host's {@code view} and {@code regions} pushes, one list row per region with a
 * colormap swatch; a row click selects the region and "Brightest frame" moves {@code t}, both
 * through set-view; "Export CSV…" writes the table where the person chooses; a Follow, offered as
 * "Tour N regions", frames each row in turn once the person presses it (§7).
 *
 * <p>Six threads touch the state: the view worker, the regions worker, the command worker, the
 * export job, the tour's thread and the trace worker (§2). Every read of the state and every
 * re-statement of the list holds {@link #LOCK}; the trace worker reads the host without it.
 * "Brightest frame" reads nothing: it answers from the traces the worker keeps ahead of time. The
 * push handlers hold the lock across their reads, so the two commands that answer a cause wait for
 * it at most {@link #CAUSE_WAIT}, then decline (§2).
 */
public final class RegionQuant {

  static final String PANEL = "quant", LIST = "regions", COLORMAP = "viridis";
  static final int SWATCH = 8;

  private static final ReentrantLock LOCK = new ReentrantLock();
  /** How long a command answering a cause waits for the rows (§2): the cause must be answered in its life. */
  static final Duration CAUSE_WAIT = Duration.ofMillis(900);
  static final String NO_DATASET = "No dataset is open";
  // all guarded by LOCK
  private static long t = -1; // the frame on screen; -1 until the first ViewChanged or ViewState
  private static long channel = 0;
  private static long generation = -1; // the open dataset's open_generation; -1 before the first push
  private static int channels = -1; // the open dataset's channel count
  private static DType dtype = DType.DTYPE_UNSPECIFIED;
  private static String selected = "";
  private static final Map<String, Quant.Row> rows = new HashMap<>();
  private static byte[] table; // viridis, 256 RGBA entries
  private static String offered = ""; // the Follow offer's label now, "" for none
  private static Quant.OrderKey tourLast; // the last region a tour showed, or null

  static final Duration TOUR_DWELL = Duration.ofMillis(500);

  // ---- the traces (§2): each row's {count, sum} at every frame on c, kept ahead of time ----

  /** A row's trace on channel {@code c}: complete when it holds all {@code total} frames. */
  record Trace(long c, int total, List<long[]> frames) {
    boolean complete() {
      return frames.size() == total;
    }
  }

  private static final Condition TRACE_CHANGED = LOCK.newCondition();
  // all guarded by LOCK
  private static final ArrayDeque<String> traceWait = new ArrayDeque<>(); // rows waiting, in invalidation order
  private static final Map<String, Long> traceEpoch = new HashMap<>(); // bumped whenever a row's trace is invalidated
  private static final Map<String, Trace> traces = new HashMap<>();
  private static int framesTotal = -1; // T, as last read from GetViewState
  private static Host tracerHost;
  private static Thread tracer;
  private static long retryMs = 100; // a failed trace's next delay: doubles to 5 s, reset by a success

  static Panel panel;
  static Panel.Rows list;
  static Panel.Channels picker;
  static Panel.Label status;
  static Panel.Label tracing;

  public static void main(String[] args) {
    System.exit(PluginMain.run(build(), args));
  }

  static Plugin build() {
    var b = Plugin.builder("org.example.region_quant", "Region Quant", "0.1.0")
        .entry("./region-quant")
        .manifestVersion(1)
        .permissions(Permission.DATA_ACCESS);
    b.command(CommandDecl.of("quant.channel", "Measure a channel").unlisted());
    b.command(CommandDecl.of("quant.pick", "Select a region").unlisted());
    b.command(CommandDecl.of("quant.brightest", "Go to the brightest frame").unlisted(), RegionQuant::brightest);
    b.command(CommandDecl.of("quant.export", "Export region means").unlisted(), RegionQuant::export);
    panel = b.panel(PanelDecl.of(PANEL, "Region Quant", column("root",
            channelPicker("channel", "quant.channel").value(0),
            WidgetDecl.list(LIST, label("name", ""), label("voxels", ""), label("mean", ""), image("swatch"))
                .selectable("quant.pick").movesView(true).maxRows(Quant.MAX_ROWS).visibleRows(8),
            button("brightest", "quant.brightest", "Brightest frame").movesView(true),
            button("export", "quant.export", "Export CSV…"),
            label("tracing", ""),
            label("status", "Ready")))
        .withViewTargets(ViewTarget.T, ViewTarget.SELECT_REGION)
        .withFollowTargets(ViewTarget.FRAME_REGION));
    b.follow(PANEL, RegionQuant::tour);
    list = panel.rows(LIST);
    status = panel.label("status");
    tracing = panel.label("tracing");
    list.onCause(RegionQuant::pick);
    list.onNoCause(() -> status.set(NO_DATASET)); // cause 0: nothing can move the view (§5)
    picker = panel.channels("channel");
    picker.onChange(RegionQuant::chooseChannel);
    b.onViewChanged(RegionQuant::viewChanged);
    b.onRegionsChanged(RegionQuant::regionsChanged);
    b.onShutdown((reason, detail) -> stopTracer()); // the SDK knows nothing of the trace worker: stop it here
    b.jobs(o -> {
      if (o.status() != Jobs.Status.DONE) status.set(o.text());
    });
    return b.build();
  }

  // ---- measuring (§2) ----

  /** {count, exact sum} of {@code c} at {@code t} over the voxels the host says the region covers. */
  static long[] measure(Host host, String regionId, long t, long c) throws Exception {
    Host.Inclusion inc = host.readRoiInclusion(ReadRoiInclusion.newBuilder().setRegionId(regionId).setT((int) t).build());
    if (inc.mask().isEmpty() || inc.voxelCount() == 0) return new long[] {0, 0};
    Volume mask = inc.mask().get();
    List<Integer> o = inc.reply().getOriginZyxList();
    var read = ReadDataVolume.newBuilder().setT((int) t).setC((int) c)
        .addRoiMinZyx(o.get(0)).addRoiMinZyx(o.get(1)).addRoiMinZyx(o.get(2))
        .addRoiMaxZyx(o.get(0) + mask.z()).addRoiMaxZyx(o.get(1) + mask.y()).addRoiMaxZyx(o.get(2) + mask.x());
    Volume v = host.readVolume(read.build()).volume();
    if (v.z() != mask.z() || v.y() != mask.y() || v.x() != mask.x()) {
      throw new IllegalStateException("the box read for " + regionId + " is " + v.z() + "x" + v.y() + "x" + v.x()
          + ", its mask " + mask.z() + "x" + mask.y() + "x" + mask.x());
    }
    long count = 0, sum = 0;
    for (long i = 0; i < mask.samples(); i++) {
      if (mask.u8(i) == 0) continue;
      count++;
      sum += (long) v.get(i);
    }
    return new long[] {count, sum};
  }

  private static long currentT(Host host) throws Exception {
    if (t < 0) t = Integer.toUnsignedLong(host.viewState().getT());
    return t;
  }

  /** Whether the open dataset is integer data (§2): only then is anything measured or traced. */
  private static boolean measurable() {
    return generation > 0 && (dtype == DType.DTYPE_U8 || dtype == DType.DTYPE_U16);
  }

  private static String floatStatus() {
    return "Region Quant measures integer data; this dataset is " + dtype.name().substring("DTYPE_".length()).toLowerCase(java.util.Locale.ROOT);
  }

  /**
   * §2, a dataset opens (the first push, or a new {@code open_generation}): keep its frame count,
   * channel count and dtype, put {@code c} back to 0 if it has no such channel, and say
   * whether it can be measured. True when a dataset opened: every row is measured anew.
   * Holds LOCK.
   */
  private static boolean opened(Host host, long gen) throws Exception {
    if (gen == generation) return false;
    var v = host.viewState();
    boolean wasMeasurable = generation < 0 || measurable();
    generation = gen;
    if (t < 0) t = Integer.toUnsignedLong(v.getT());
    boolean shaped = v.getShapeTczyxCount() == 5;
    framesTotal = shaped ? v.getShapeTczyx(0) : 0;
    channels = shaped ? v.getShapeTczyx(1) : 0;
    dtype = v.getDtype();
    if (gen > 0 && channel >= channels) {
      channel = 0;
      picker.set(List.of(0));
    }
    if (gen > 0 && !measurable()) status.set(floatStatus());
    else if (gen > 0 && !wasMeasurable) status.set("Ready");
    return true;
  }

  /**
   * Measures these rows (§2). A region the host does not have ({@code NOT_FOUND}) loses its row and
   * its trace; any other failure keeps the row's numbers and is said once the rest is committed.
   * On data that is not integer data nothing is read: every row keeps only its name.
   */
  private static void remeasure(Host host, List<String> ids) throws Exception {
    if (!measurable()) {
      for (String id : ids) {
        Quant.Row r = rows.get(id);
        if (r != null) rows.put(id, Quant.Row.unmeasured(r.name()));
      }
      return;
    }
    long at = currentT(host);
    String failed = null;
    for (String id : ids) {
      Quant.Row r = rows.get(id);
      if (r == null) continue;
      try {
        long[] m = measure(host, id, at, channel);
        rows.put(id, new Quant.Row(r.name(), m[0], m[1]));
      } catch (HostException e) {
        if (e.kind() == vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND) {
          rows.remove(id);
          forgetTrace(id);
        } else {
          failed = e.getMessage();
        }
      } catch (TimeoutException e) {
        failed = e.getMessage();
      }
    }
    if (failed != null) status.set("Measuring failed: " + failed);
  }

  /** §3: the whole list, keys first, then each row's cells, then the selection. Holds LOCK. */
  private static void publish(Host host) throws Exception {
    if (table == null) table = host.colormap(COLORMAP);
    List<String> keys = Quant.ordered(rows);
    if (keys.size() > Quant.MAX_ROWS) {
      status.set(Quant.showing(keys.size())); // §3: the first 1024, in order
      keys = keys.subList(0, Quant.MAX_ROWS);
    }
    list.set(keys);
    for (String k : keys) {
      Quant.Row r = rows.get(k);
      int e = 4 * Quant.swatchIndex(r.sum(), r.voxels());
      // the swatch goes with the row: until the panel's segment installs, the SDK holds it
      list.row(k).set("name", r.display(k)).set("voxels", r.voxelsText()).set("mean", Quant.meanText(r.sum(), r.voxels()))
          .image("swatch", Quant.png(SWATCH, table[e] & 0xff, table[e + 1] & 0xff, table[e + 2] & 0xff));
    }
    list.select(keys.contains(selected) ? selected : "");
    // §7: the offer follows the number of rows (kept for an unopened panel); none withdraws it
    String label = Quant.tourLabel(keys.size());
    if (!label.equals(offered)) {
      if (label.isEmpty()) panel.withdrawFollow();
      else panel.offerFollow(label);
      offered = label;
    }
  }

  // ---- the subscriptions (§2) ----

  static void viewChanged(CommandContext ctx, Subscriptions.ViewChange v) throws Exception {
    LOCK.lock();
    try {
      hostClient = ctx.host();
      boolean opened = opened(ctx.host(), v.openGeneration());
      boolean moved = t != v.t();
      t = v.t();
      selected = v.selectedRegionId();
      if (opened || moved) remeasure(ctx.host(), new ArrayList<>(rows.keySet()));
      if (opened) invalidate(ctx.host(), Quant.ordered(rows)); // new data: every trace starts over
      publish(ctx.host());
    } finally {
      LOCK.unlock();
    }
  }

  static void regionsChanged(CommandContext ctx, Subscriptions.RegionsChange r) throws Exception {
    LOCK.lock();
    try {
      hostClient = ctx.host();
      if (opened(ctx.host(), r.openGeneration()) && !r.reset()) {
        remeasure(ctx.host(), new ArrayList<>(rows.keySet()));
        invalidate(ctx.host(), Quant.ordered(rows));
      }
      if (r.reset()) {
        for (String id : rows.keySet()) forgetTrace(id);
        rows.clear();
      }
      List<String> changed = new ArrayList<>();
      for (var c : r.changes()) {
        if (c.deleted()) {
          rows.remove(c.regionId());
          forgetTrace(c.regionId());
        } else {
          changed.add(c.regionId());
        }
      }
      if (!changed.isEmpty()) {
        List<String> found = new ArrayList<>();
        for (RoiRecord rec : ctx.host().rois(changed).getRoisList()) {
          rows.put(rec.getRegionId(), new Quant.Row(rec.getName(), 0, 0));
          found.add(rec.getRegionId());
        }
        remeasure(ctx.host(), found);
        found.removeIf(id -> !rows.containsKey(id)); // gone while being measured (NOT_FOUND)
        invalidate(ctx.host(), found); // a new or replaced row: its trace starts over
      }
      publish(ctx.host());
    } finally {
      LOCK.unlock();
    }
  }

  // ---- the commands (§4–§6) ----

  /** §4: the picker's handle has taken the value; the channel already in use changes nothing. */
  static void chooseChannel(List<Integer> chosen) {
    long c = chosen.isEmpty() ? 0 : chosen.get(0);
    LOCK.lock();
    try {
      if (c == channel) return;
      channel = c;
      if (hostClient == null) return; // no push yet: there is nothing to measure
      remeasure(hostClient, new ArrayList<>(rows.keySet()));
      invalidate(hostClient, Quant.ordered(rows)); // every row's trace restarts on the new channel
      publish(hostClient);
    } catch (RuntimeException e) {
      throw e;
    } catch (Exception e) {
      throw new IllegalStateException("re-measuring on channel " + c + ": " + e.getMessage(), e);
    } finally {
      LOCK.unlock();
    }
  }

  /** The host client, taken from the push handlers' context for the picker's listener, which has none. Guarded by LOCK. */
  private static Host hostClient;

  private static String didNotMove(ViewResult r) {
    return "The view did not move: " + (r.refused() ? r.reason().name().replace("VIEW_REFUSAL_", "") : r.outcome().name().replace("VIEW_OUTCOME_", ""));
  }

  /** A row click (§5): the list handle has taken the row (an unknown key is refused there, and the SDK declines its cause). */
  static void pick(ViewCause cause) throws Exception {
    String key = cause.row().orElse("");
    String name;
    if (!LOCK.tryLock(CAUSE_WAIT.toMillis(), TimeUnit.MILLISECONDS)) {
      cause.decline(); // a push handler is re-measuring: decline within the cause's life, change nothing else
      return;
    }
    try {
      Quant.Row row = rows.get(key);
      name = row == null ? null : row.display(key);
    } finally {
      LOCK.unlock();
    }
    if (name == null) {
      cause.decline();
      return;
    }
    ViewResult r = cause.move(ViewMove.to().selectRegion(key));
    status.set(r.applied() ? "Selected " + name : didNotMove(r));
  }

  /**
   * §5: answers at once and reads nothing. A scan of every frame here would not fit the cause's
   * life on a real dataset, so it answers from the selected row's trace, and declines while that
   * trace is incomplete.
   */
  static String brightest(CommandContext ctx) throws Exception {
    if (ctx.cause().isEmpty()) {
      status.set(NO_DATASET); // cause 0 (§5): no dataset is open, so no view request at all
      return "";
    }
    String sel, name;
    boolean known, integer;
    String floats;
    Trace tr;
    long[][] m;
    int total;
    if (!LOCK.tryLock(CAUSE_WAIT.toMillis(), TimeUnit.MILLISECONDS)) {
      ctx.declineView(); // a push handler is re-measuring: decline within the cause's life, change nothing else
      return "declined";
    }
    try {
      sel = selected;
      known = rows.containsKey(sel);
      name = known ? rows.get(sel).display(sel) : sel;
      integer = measurable();
      floats = floatStatus();
      tr = known ? traceOf(sel) : null;
      m = tr == null ? new long[0][] : tr.frames().toArray(new long[0][]);
      total = tr != null ? tr.total() : framesTotal;
    } finally {
      LOCK.unlock();
    }
    if (!integer) {
      ctx.declineView();
      status.set(floats); // §2: nothing is traced on data that is not integer data
      return "declined";
    }
    if (!known) {
      ctx.declineView();
      status.set("Select a region first");
      return "declined";
    }
    if (tr == null || m.length < tr.total()) {
      ctx.declineView();
      status.set("Measuring… " + m.length + " of " + total + " frames");
      return "declined";
    }
    OptionalInt best = Quant.bestFrame(m);
    if (best.isEmpty()) {
      ctx.declineView();
      status.set(name + " covers no voxel");
      return "declined";
    }
    ViewResult r = ctx.moveView(ViewMove.to().t(best.getAsInt()));
    status.set(r.applied() ? "Brightest frame for " + name + ": t=" + best.getAsInt() : didNotMove(r));
    return r.toString();
  }

  // ---- the trace worker (§2) ----

  /** Restarts these rows' traces, starting the worker on first use. Holds LOCK. */
  private static void invalidate(Host host, List<String> ids) {
    boolean trace = measurable(); // §2: nothing is traced on data that is not integer data
    for (String id : ids) {
      traceEpoch.merge(id, 1L, Long::sum);
      traces.remove(id);
      if (!trace) traceWait.remove(id);
      else if (!traceWait.contains(id)) traceWait.add(id);
    }
    if (tracer == null) {
      tracerHost = host;
      tracer = Thread.ofPlatform().daemon().name("region-quant-trace").start(RegionQuant::traceWorker);
    }
    TRACE_CHANGED.signalAll();
  }

  /** A deleted row's trace goes. Holds LOCK. */
  private static void forgetTrace(String id) {
    traceEpoch.merge(id, 1L, Long::sum);
    traces.remove(id);
    traceWait.remove(id);
  }

  /** The row's trace on the current channel, complete or not. Holds LOCK. */
  private static Trace traceOf(String id) {
    Trace tr = traces.get(id);
    return tr != null && tr.c() == channel ? tr : null;
  }

  /**
   * Never dies of a failed trace: a failed read is retried after a delay, and anything else
   * unexpected abandons that one trace, and the next one runs. It ends only with the session:
   * interrupted by the shutdown hook, or on the host request that finds the session gone. Neither
   * is a failure, so neither is logged.
   */
  private static void traceWorker() {
    while (true) {
      try {
        traceOne();
      } catch (InterruptedException | SessionEndedException e) {
        return;
      } catch (Exception e) {
        if (Thread.currentThread().isInterrupted()) return;
        System.err.println("region_quant: a trace failed and was abandoned: " + e);
      }
    }
  }

  /** Stops the trace worker (the shutdown window): whatever it was reading is abandoned. */
  private static void stopTracer() {
    Thread t;
    LOCK.lock();
    try {
      t = tracer;
      traceWait.clear();
    } finally {
      LOCK.unlock();
    }
    if (t != null) t.interrupt();
  }

  /**
   * A trace read failed with anything but {@code NOT_FOUND}: the row waits again, its trace
   * from frame 0, after a delay that starts at 100 ms and doubles up to 5 s.
   */
  private static void retryLater(String id, long epoch) throws InterruptedException {
    long delay;
    LOCK.lock();
    try {
      delay = retryMs;
      retryMs = Math.min(5000, retryMs * 2);
      if (traceEpoch.getOrDefault(id, 0L) == epoch) traces.remove(id);
    } finally {
      LOCK.unlock();
    }
    Thread.sleep(delay);
    LOCK.lock();
    try {
      // invalidated meanwhile, it is already waiting; deleted, it is not traced at all
      if (traceEpoch.getOrDefault(id, 0L) == epoch && rows.containsKey(id) && measurable() && !traceWait.contains(id)) {
        traceWait.add(id);
        TRACE_CHANGED.signalAll();
      }
    } finally {
      LOCK.unlock();
    }
  }

  private static boolean notFound(Exception e) {
    return e instanceof HostException h && h.kind() == vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND;
  }

  /** Takes the next waiting row and traces it, frame by frame, reading the host without the lock. */
  private static void traceOne() throws Exception {
    String id, name;
    long epoch, c;
    LOCK.lock();
    try {
      while (traceWait.isEmpty()) {
        tracing.set("");
        TRACE_CHANGED.await();
      }
      id = traceWait.poll();
      if (!rows.containsKey(id) || !measurable()) return;
      epoch = traceEpoch.getOrDefault(id, 0L);
      c = channel;
      name = rows.get(id).display(id);
    } finally {
      LOCK.unlock();
    }
    int total;
    try {
      total = tracerHost.viewState().getShapeTczyx(0);
    } catch (HostException | TimeoutException e) {
      retryLater(id, epoch);
      return;
    }
    List<long[]> frames = new ArrayList<>();
    LOCK.lock();
    try {
      framesTotal = total;
      if (traceEpoch.getOrDefault(id, 0L) != epoch) return; // invalidated meanwhile: queued again
      traces.put(id, new Trace(c, total, frames));
      tracing.set("Measuring " + name + ": 0 of " + total + " frames");
    } finally {
      LOCK.unlock();
    }
    for (int f = 0; f < total; f++) {
      long[] got;
      try {
        got = measure(tracerHost, id, f, c);
      } catch (HostException | TimeoutException e) {
        if (notFound(e)) return; // the region went while its trace ran; the push that says so drops the row
        retryLater(id, epoch);
        return;
      }
      LOCK.lock();
      try {
        if (traceEpoch.getOrDefault(id, 0L) != epoch) return; // invalidated: it is queued again, from frame 0
        frames.add(got);
        if (frames.size() == total) retryMs = 100; // a trace that completes resets the delay
        tracing.set("Measuring " + name + ": " + frames.size() + " of " + total + " frames");
      } finally {
        LOCK.unlock();
      }
    }
  }

  // ---- the tour (§7) ----

  private static Quant.OrderKey orderKey(String id) {
    return new Quant.OrderKey(rows.get(id).display(id), id);
  }

  /** Frames each row in list order, dwelling on each; a resumed tour starts after the last region shown. */
  static void tour(CommandContext ctx, Follow f) throws Exception {
    List<String> keys;
    LOCK.lock();
    try {
      keys = new ArrayList<>(Quant.ordered(rows));
      Quant.OrderKey last = f.resumed() ? tourLast : null;
      if (last != null) keys.removeIf(k -> orderKey(k).compareTo(last) <= 0);
    } finally {
      LOCK.unlock();
    }
    for (String k : keys) {
      String name, where;
      Quant.OrderKey shown;
      LOCK.lock();
      try {
        if (!rows.containsKey(k)) continue; // deleted since the tour began
        List<String> now = Quant.ordered(rows);
        name = rows.get(k).display(k);
        where = "(" + (now.indexOf(k) + 1) + " of " + now.size() + ")";
        shown = orderKey(k);
      } finally {
        LOCK.unlock();
      }
      if (!f.move(ViewMove.to().frameRegion(k))) break;
      LOCK.lock();
      try {
        tourLast = shown;
      } finally {
        LOCK.unlock();
      }
      status.set("Touring " + name + " " + where);
      if (!f.waitFor(TOUR_DWELL)) break;
    }
    if (f.active()) status.set("Tour finished"); // returning ends the Follow: PLUGIN_ENDED
    else status.set("Tour stopped: " + f.reason().map(r -> r.name().replace("FOLLOW_END_", "")).orElse("the session ended"));
  }

  static String export(CommandContext ctx) throws Exception {
    return ctx.jobs().start("export", job -> {
      java.util.Optional<java.nio.file.Path> path;
      try {
        path = job.host().requestSavePath("regions.csv", "csv", "Export region means");
      } catch (HostException e) {
        status.set("Export failed: " + e.getMessage());
        return "";
      }
      if (path.isEmpty()) {
        status.set("Export cancelled");
        return "";
      }
      String text;
      int n;
      LOCK.lock();
      try {
        text = Quant.csv(rows, t, channel);
        n = rows.size();
      } finally {
        LOCK.unlock();
      }
      try {
        Files.writeString(path.get(), text, StandardCharsets.UTF_8);
      } catch (java.io.IOException | SecurityException e) {
        status.set("Export failed: " + e.getMessage()); // §6: never swallowed
        return "";
      }
      status.set("Exported " + n + " regions to " + path.get());
      return "";
    });
  }
}
