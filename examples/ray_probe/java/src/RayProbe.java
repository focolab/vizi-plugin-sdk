import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.checkbox;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.image;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;
import static vizi.plugin.javasdk.manifest.WidgetDecl.lineEdit;
import static vizi.plugin.javasdk.manifest.WidgetDecl.row;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.OptionalDouble;
import java.util.concurrent.atomic.AtomicBoolean;
import vizi.plugin.javasdk.Fmt;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.InputEvent;
import vizi.plugin.javasdk.Item;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Overlay;
import vizi.plugin.javasdk.PaneContext;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Position;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Tool;
import vizi.plugin.javasdk.ToolContext;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.manifest.ActionDecl;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.Hold;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PaneButton;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.javasdk.manifest.ToolPane;
import vizi.plugin.javasdk.manifest.TriggerDecl;
import vizi.plugin.v1.ViziPlugin.ContributeRois;
import vizi.plugin.v1.ViziPlugin.DataVolume;
import vizi.plugin.v1.ViziPlugin.Extent;
import vizi.plugin.v1.ViziPlugin.EdgeBox;
import vizi.plugin.v1.ViziPlugin.ReadDataVolume;
import vizi.plugin.v1.ViziPlugin.RoiContribution;
import vizi.plugin.v1.ViziPlugin.RoiStyle;
import vizi.plugin.v1.ViziPlugin.MouseButton;
import vizi.plugin.v1.ViziPlugin.ToolAction;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Trigger;
import vizi.plugin.v1.ViziPlugin.TriggerHold;

/**
 * The ray-probe example (../SPEC.md) on the Java SDK: a gesture tool that turns a click into the
 * brightest voxel along what the record names, refined at level 0; an aim line on the live lane;
 * a numbered pick list with markers and labels; a profile panel; undo and clear as actions fired
 * into an open gesture; the picks contributed as ellipsoid ROIs. The arithmetic is in {@link
 * Lattice}, {@link Picking} and {@link Plot}, and knows nothing about vizi.
 */
public final class RayProbe {
  RayProbe() {}

  static final String TOOL = "probe.point";
  static final String PICKS = "probe.picks";
  static final String AIM = "probe.aim";
  static final String PANEL = "probe";
  static final String PROFILE = "profile";

  static final String MSG_READY = "Ready";
  static final String MSG_ACTIVE = "Probe active — click in a view";
  static final String MSG_NOTHING = "Nothing under the pointer";
  static final String MSG_UNUSABLE = "The record's context could not be reconciled with the pane; nothing picked";
  static final String MSG_NO_CHANNEL = "No channel is visible";
  static final String MSG_BUSY = "Busy — a pick is still in flight";
  static final String MSG_TOO_LARGE = "Too large — the segment's box exceeds 2^26 voxels; zoom out";
  static final String MSG_CHANGED = "The dataset changed under the click; nothing picked";

  static final long READ_CAP = 1L << 26;
  static final int[] PICK_RGBA = {255, 204, 0, 255};
  static final int[] AIM_RGBA = {0, 200, 255, 255};
  static final String ROI_COLOR = "#ffcc00";
  static final Duration COMMAND_WAIT = Duration.ofSeconds(8);

  /** One pick: SPEC.md §"The UI state model". {@code position} is a level-0 lattice coordinate. */
  record Pick(int number, double[] position, double value, long t, OptionalDouble seconds, int c, int level, boolean refined) {

    /** §5 step 3's label: "Pick N: (z, y, x) = …, value …, c=…, t=T (S s)". */
    String label(double[] spacing, String unit) {
      StringBuilder b = new StringBuilder("Pick ").append(number).append(": (")
          .append(Fmt.g(position[0])).append(", ").append(Fmt.g(position[1])).append(", ").append(Fmt.g(position[2])).append(')');
      if (spacing != null) {
        b.append(" = (").append(Fmt.g(position[0] * spacing[0], 4)).append(", ").append(Fmt.g(position[1] * spacing[1], 4))
            .append(", ").append(Fmt.g(position[2] * spacing[2], 4)).append(") ").append(unit);
      }
      b.append(", value ").append(Fmt.g(value)).append(", c=").append(c).append(", t=").append(t);
      if (seconds.isPresent()) b.append(" (").append(Fmt.g(seconds.getAsDouble())).append(" s)");
      return b.toString();
    }
  }

  static String countText(int n) {
    return n == 0 ? "No picks" : n == 1 ? "1 pick" : n + " picks";
  }

  /** A refusal on the worker: its message is the status. */
  static final class Refused extends Exception {
    private static final long serialVersionUID = 1L;

    Refused(String message) {
      super(message);
    }
  }

  // ---- the plugin's model and its handles ----

  private final Object lock = new Object();
  private final List<Pick> picks = new ArrayList<>();
  /** A pick is between its release and its publication (refusal 4, §"Budgets"). */
  private final AtomicBoolean inFlight = new AtomicBoolean();

  private Overlay picksOverlay;
  private Overlay aimOverlay;
  private Panel probePanel;
  private Panel profilePanel;
  private Panel.Number radius;
  private Panel.Flag refine;
  private Jobs jobs;

  public static void main(String[] args) {
    System.exit(PluginMain.run(new RayProbe().plugin(), args));
  }

  Plugin plugin() {
    var b = Plugin.builder("org.example.ray_probe", "Ray probe", "0.1.0")
        .manifestVersion(1)
        .engine("1.0")
        .entry("./ray-probe")
        .permissions(Permission.DATA_ACCESS, Permission.CONTRIBUTE_ROIS);

    b.tool(ToolDecl.of(TOOL)
        .withName("Probe")
        .withViews(ToolDecl.View.TWO_D, ToolDecl.View.THREE_D)
        .withMode(ToolDecl.Mode.GESTURE)
        .withCancelOnCameraMove(false)
        .withTrigger(TriggerDecl.key("Shift+P")) // vizi binds P (with Space) to Play/Pause
        .withTrigger(TriggerDecl.mouse(TriggerDecl.MouseButton.MIDDLE))
        .withAction(ActionDecl.onKey("undo", "U").withName("Undo pick")
            .withPane(PaneButton.of("icons/undo.svg").withTooltip("Undo the last pick")))
        .withAction(ActionDecl.onKey("clear", "X").withName("Clear picks")
            .withPane(PaneButton.of("icons/clear.svg").withTooltip("Clear every pick")))
        .withPane(ToolPane.of("icons/probe.svg").withTooltip("Probe: the brightest voxel under the pointer").withHold(Hold.TOGGLE)),
        new Probe());

    picksOverlay = b.overlay(OverlayDecl.drawn(PICKS, 4).withName("Picks"));
    aimOverlay = b.overlay(OverlayDecl.drawn(AIM, 4).withName("Aim"));

    b.command(CommandDecl.of("probe.set_radius", "Set ROI radius").unlisted());
    b.command(CommandDecl.of("probe.set_refine", "Refine at level 0").unlisted());
    b.command(CommandDecl.of("probe.undo", "Undo pick").unlisted(), ctx -> {
      jobs.await("undo", job -> undo(), COMMAND_WAIT);
      return "Undid the last pick";
    });
    b.command(CommandDecl.of("probe.clear", "Clear picks").unlisted(), ctx -> {
      jobs.await("clear", job -> clear(), COMMAND_WAIT);
      return "Cleared the picks";
    });
    b.command(CommandDecl.of("probe.contribute", "Contribute picks as ROIs"),
        ctx -> jobs.await("contribute", job -> contribute(job.host()), COMMAND_WAIT));

    probePanel = b.panel(PanelDecl.of(PANEL, "Ray probe", column("root",
        label("status", MSG_READY),
        row("rradius", label("lradius", "ROI radius (voxels)"), lineEdit("radius", "probe.set_radius").value("3")),
        checkbox("refine", "probe.set_refine").text("Refine at level 0").value(true),
        row("rbuttons",
            button("undo", "probe.undo", "Undo"),
            button("clear", "probe.clear", "Clear"),
            button("contribute", "probe.contribute", "Contribute")),
        label("count", "No picks"))));
    profilePanel = b.panel(PanelDecl.of(PROFILE, "Ray profile", column("root",
        image("plot"),
        label("pick", "No pick yet"))));

    // §"The UI state model": radius is a positive finite number of level-0 voxels; the SDK
    // answers the command, refuses anything else and pushes the stored value back.
    radius = probePanel.number("radius", v -> v > 0 ? null : "the ROI radius must be a positive number of voxels");
    refine = probePanel.flag("refine");

    // The panels' models: the SDK mirrors every set and re-pushes it on each PanelOpened.
    probePanel.label("status").set(MSG_READY);
    probePanel.label("count").set(countText(0));
    profilePanel.label("pick").set("No pick yet");

    jobs = b.jobs(o -> {
      if (o.status() == Jobs.Status.FAILED) say(o.text());
    }).queue(64);

    return b.build();
  }

  void say(String text) {
    probePanel.label("status").set(text);
  }

  // ---- the tool ----

  final class Probe implements Tool {
    /** The open gesture's interaction while an aim is up, else 0. Listener thread only. */
    private long aiming;

    private boolean liveFailed;

    @Override
    public void activated(ToolContext ctx) {
      // SPEC §1: the honoured triggers and action keys, from the binding the host installed
      String keys = keysOf(ctx.binding());
      say(keys.isEmpty() ? MSG_ACTIVE : MSG_ACTIVE + " — " + keys);
    }

    @Override
    public void deactivated(ToolContext ctx) {
      say(MSG_READY);
    }

    @Override
    public void press(ToolContext ctx, InputEvent.Press ev) {
      liveFailed = false;
      aim(ev.info());
    }

    @Override
    public void drag(ToolContext ctx, InputEvent.Drag ev) {
      aim(ev.info());
    }

    /** §2: the record's segment (or column) in level-0 coordinates on the live lane. */
    private void aim(InputEvent.Info info) {
      if (!info.usable() || info.pane().isEmpty()) return;
      double[] s;
      try {
        s = info.pane().get().scale(info.level());
      } catch (IndexOutOfBoundsException e) {
        return;
      }
      double[] pts = segmentLevel0(info, s);
      if (pts == null) return;
      Overlay.Live how;
      String why;
      try {
        how = aimOverlay.publishLive(info, List.of(Item.segments("aim", pts).voxelFrame().color(AIM_RGBA[0], AIM_RGBA[1], AIM_RGBA[2], AIM_RGBA[3])
            .thickness(2).inline()));
        why = how == Overlay.Live.REFUSED ? "the lane refused it" : null;
      } catch (RuntimeException e) {
        how = Overlay.Live.REFUSED;
        why = e.getMessage();
      }
      if (how != Overlay.Live.REFUSED) {
        aiming = info.interaction();
      } else if (!liveFailed) {
        liveFailed = true;
        say("Aim unavailable (" + why + "); the pick still runs on release");
      }
    }

    private void clearAim() {
      if (aiming == 0) return;
      aiming = 0;
      aimOverlay.clear();
    }

    @Override
    public void cancel(ToolContext ctx, InputEvent.Cancel ev) {
      clearAim();
    }

    /** §3: the refusals in order, then the pick on a worker. */
    @Override
    public void release(ToolContext ctx, InputEvent.Release ev) {
      clearAim();
      InputEvent.Info info = ev.info();
      if (!info.usable()) {
        say(MSG_UNUSABLE);
        return;
      }
      if (info.miss() || !(info.position() instanceof Position.Ray || info.position() instanceof Position.Voxel
          || info.position() instanceof Position.Column)) {
        say(MSG_NOTHING);
        return;
      }
      List<Integer> visible = info.pane().isPresent() ? info.visibleChannels() : List.of();
      if (visible.isEmpty()) {
        say(MSG_NO_CHANNEL);
        return;
      }
      if (!inFlight.compareAndSet(false, true)) {
        say(MSG_BUSY);
        return;
      }
      int channel = visible.stream().min(Integer::compare).orElseThrow();
      boolean refineNow = refine.get();
      PaneContext pane = info.pane().get();
      try {
        jobs.start("pick", job -> {
          try {
            pick(job.host(), info, pane, channel, refineNow);
          } finally {
            inFlight.set(false);
          }
          return "";
        });
      } catch (Refusal r) {
        inFlight.set(false);
        say(MSG_BUSY);
      }
    }

    /** §6: an action inside an open gesture touches the gesture not at all. */
    @Override
    public void action(ToolContext ctx, InputEvent.Action ev) {
      Jobs.Work w = switch (ev.actionId()) {
        case "undo" -> job -> undo();
        case "clear" -> job -> clear();
        default -> null; // an index this manifest does not declare: a newer host; ignored
      };
      if (w == null) return;
      try {
        jobs.start(ev.actionId(), w);
      } catch (Refusal r) {
        say("Could not " + ev.actionId() + ": " + r.getMessage());
      }
    }
  }

  /**
   * What engages the tool and what fires what, from the ToolBinding: the assigned triggers (a
   * subset of those requested) and the actions whose key the host kept ({@code ""}: none).
   */
  static String keysOf(ToolBinding b) {
    List<String> engages = new ArrayList<>();
    for (Trigger t : b.getTriggersList()) {
      if (!t.getKey().isEmpty()) {
        engages.add(t.getKey() + (t.getHold() == TriggerHold.TRIGGER_HOLD_MOMENTARY ? " holds" : " toggles"));
      } else if (t.getButton() != MouseButton.MOUSE_BUTTON_UNSPECIFIED) {
        StringBuilder m = new StringBuilder();
        String[] names = {"Shift", "Ctrl", "Alt", "Meta"};
        for (int i = 0; i < 4; i++) if ((t.getMods() & (1 << i)) != 0) m.append(names[i]).append('+');
        String button = switch (t.getButton()) {
          case MOUSE_BUTTON_LEFT -> "left";
          case MOUSE_BUTTON_RIGHT -> "right";
          case MOUSE_BUTTON_MIDDLE -> "middle";
          default -> "?";
        };
        engages.add(m + button + " button holds");
      }
    }
    List<String> fires = new ArrayList<>();
    for (ToolAction a : b.getActionsList()) {
      if (a.getKey().isEmpty()) continue;
      String verb = switch (a.getActionId()) {
        case "undo" -> "undoes";
        case "clear" -> "clears";
        default -> a.getActionId();
      };
      fires.add(a.getKey() + " " + verb);
    }
    List<String> parts = new ArrayList<>();
    if (!engages.isEmpty()) parts.add(String.join(", ", engages));
    if (!fires.isEmpty()) parts.add(String.join(", ", fires));
    return String.join("; ", parts);
  }

  /**
   * The aim's two points in level-0 lattice coordinates, flat (z, y, x, z, y, x): a ray's entry
   * and far point, or a column's first and last included centre. Null when there is no aim.
   */
  static double[] segmentLevel0(InputEvent.Info info, double[] s) {
    if (info.miss()) return null;
    double[] a;
    double[] b;
    switch (info.position()) {
      case Position.Ray r -> {
        a = r.entry();
        b = r.far().clone();
      }
      case Position.Column col -> {
        int free = freeAxis(col);
        a = fixedOf(col, free);
        b = a.clone();
        a[free] = col.min();
        b[free] = col.max() - 1;
      }
      default -> {
        return null;
      }
    }
    double[] out = new double[6];
    for (int k = 0; k < 3; k++) {
      out[k] = Lattice.level0Of(a[k], s[k]);
      out[3 + k] = Lattice.level0Of(b[k], s[k]);
    }
    return out;
  }

  static int freeAxis(Position.Column col) {
    return switch (col.axis()) {
      case Z -> 0;
      case Y -> 1;
      case X -> 2;
    };
  }

  /** A column's two fixed coordinates by axis (the free axis's slot is 0): Z→(y, x), Y→(z, x), X→(z, y). */
  static double[] fixedOf(Position.Column col, int free) {
    double[] v = new double[3];
    int n = 0;
    for (int k = 0; k < 3; k++) {
      if (k == free) continue;
      v[k] = n++ == 0 ? col.a() : col.b();
    }
    return v;
  }

  // ---- the pick (§3, §4), on the worker ----

  /** The ordered set S at the record's level, before clipping. */
  static List<Lattice.Cell> cellsOf(Position p) throws Refused {
    return switch (p) {
      case Position.Voxel v -> List.of(Lattice.voxel(v.z(), v.y(), v.x()));
      case Position.Column col -> {
        int free = freeAxis(col);
        yield Lattice.column(free, fixedOf(col, free), col.min(), col.max());
      }
      case Position.Ray r -> Lattice.traverse(r.entry(), r.far()); // the host's `picked` is not compared
      default -> throw new Refused(MSG_NOTHING);
    };
  }

  static int[] extent0(PaneContext pane) {
    Extent e = pane.geometry().getExtent();
    return new int[] {e.getZN() - e.getZ0(), e.getYN() - e.getY0(), e.getXN() - e.getX0()};
  }

  static double[] cropEdges0(PaneContext pane) {
    EdgeBox c = pane.geometry().getCrop();
    return new double[] {c.getZ0(), c.getZ1(), c.getY0(), c.getY1(), c.getX0(), c.getX1()};
  }

  private static Host.Deposit<DataVolume> read(Host host, long t, int c, int level, Lattice.Box box)
      throws HostException, java.util.concurrent.TimeoutException, InterruptedException {
    ReadDataVolume.Builder r = ReadDataVolume.newBuilder().setT((int) t).setC(c).setLevel(level);
    for (int a = 0; a < 3; a++) {
      r.addRoiMinZyx(box.lo()[a]);
      r.addRoiMaxZyx(box.hi()[a]);
    }
    return host.readVolume(r.build());
  }

  private static void checkGeneration(DataVolume reply, InputEvent.Info info) throws Refused {
    if ((reply.getOpenGeneration() & 0xFFFFFFFFL) != (info.gen() & 0xFFFFFFFFL)) throw new Refused(MSG_CHANGED);
  }

  private static void checkShape(Volume v, Lattice.Box box) throws Refused {
    if (v.z() != box.hi()[0] - box.lo()[0] || v.y() != box.hi()[1] - box.lo()[1] || v.x() != box.hi()[2] - box.lo()[2]) {
      throw new Refused("The host returned " + v + " for a read of " + box.voxels() + " voxels; nothing picked");
    }
  }

  void pick(Host host, InputEvent.Info info, PaneContext pane, int channel, boolean refineOn) {
    try {
      int level = info.level();
      double[] s = pane.scale(level);
      int[] extent = extent0(pane);
      int[] shape = Lattice.levelShape(extent, s);
      Lattice.Range crop = Lattice.cropAtLevel(cropEdges0(pane), s, shape);
      if (crop == null) throw new Refused(MSG_NOTHING);
      List<Lattice.Cell> cells = Lattice.clip(cellsOf(info.position()), crop);
      if (cells.isEmpty()) throw new Refused(MSG_NOTHING);
      Lattice.Box box = Lattice.aabb(cells);
      if (box.voxels() > READ_CAP) throw new Refused(MSG_TOO_LARGE);

      var coarseRead = read(host, info.t(), channel, level, box);
      checkGeneration(coarseRead.reply(), info);
      Volume vol = coarseRead.volume();
      checkShape(vol, box);
      double[] values = new double[cells.size()];
      for (int n = 0; n < cells.size(); n++) {
        Lattice.Cell c = cells.get(n);
        values[n] = vol.get(vol.index(c.z() - box.lo()[0], c.y() - box.lo()[1], c.x() - box.lo()[2]));
      }
      Picking.Best coarse = Picking.firstMax(values);
      if (coarse == null) throw new Refused(MSG_NOTHING);
      Lattice.Cell cell = cells.get(coarse.index());

      double[] position;
      double value;
      boolean refined = false;
      if (level > 0 && refineOn) {
        Lattice.Box block = Lattice.blockOf(cell, s, extent);
        var fine = read(host, info.t(), channel, 0, block);
        checkGeneration(fine.reply(), info);
        checkShape(fine.volume(), block);
        Picking.Peak peak = Picking.blockMax(fine.volume(), block.lo());
        if (peak == null) throw new Refused(MSG_NOTHING);
        position = new double[] {peak.z(), peak.y(), peak.x()};
        value = peak.value();
        refined = true;
      } else {
        position = new double[] {Lattice.level0Of(cell.z(), s[0]), Lattice.level0Of(cell.y(), s[1]), Lattice.level0Of(cell.x(), s[2])};
        value = coarse.value();
      }
      publish(pane, info, position, value, channel, level, refined, cells, values, coarse.index());
    } catch (Refused r) {
      say(r.getMessage());
    } catch (HostException e) {
      say("Read failed: " + e.getMessage());
    } catch (java.util.concurrent.TimeoutException e) {
      say("Read failed: the host did not answer (" + e.getMessage() + ")");
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      say("The pick was cancelled");
    }
  }

  /** §5, in its order: the list and the count, the picks overlay, the profile panel, the status. */
  private void publish(PaneContext pane, InputEvent.Info info, double[] position, double value, int channel, int level,
      boolean refined, List<Lattice.Cell> cells, double[] values, int coarseIndex) {
    double[] spacing = pane.spacingKnown() ? pane.spacing() : null;
    Pick p;
    synchronized (lock) {
      p = new Pick(picks.size() + 1, position, value, info.t(), pane.timeAt(info.t()), channel, level, refined);
      picks.add(p);
    }
    pushList();

    // the profile: the ordered set's values against distance from entry (spacing unit when known)
    double[] xs = Picking.distances(cells, spacing);
    List<Integer> shown = new ArrayList<>(Picking.sampleIndices(values.length, 1024));
    if (!shown.contains(coarseIndex)) {
      shown.add(coarseIndex);
      shown.sort(Integer::compare);
    }
    double[] v = new double[shown.size()];
    double[] x = new double[shown.size()];
    for (int i = 0; i < shown.size(); i++) {
      v[i] = values[shown.get(i)];
      x[i] = xs[shown.get(i)];
    }
    byte[] png = Plot.png(Plot.render(v, x, shown.indexOf(coarseIndex)));
    try {
      profilePanel.image("plot").push(png); // held by the SDK until the panel opens (§8.4)
    } catch (RuntimeException e) {
      // SPEC §5 step 3: the profile panel failing is not the pick failing; a log line, and on to step 4
      System.err.println("ray_probe: the profile plot for pick " + p.number() + " could not be pushed: " + e);
    }
    profilePanel.label("pick").set(p.label(spacing, pane.spacingUnit()));
    say("Pick " + p.number() + ": value " + Fmt.g(p.value()) + " at (" + Fmt.g(position[0]) + ", " + Fmt.g(position[1]) + ", "
        + Fmt.g(position[2]) + ")" + (refined ? ", refined at level 0" : level > 0 ? ", level " + level + " centre" : ""));
  }

  /** §5 step 2 (and undo, clear): the count, and one marker plus one text item per pick, or a clear. */
  private void pushList() {
    List<Pick> now;
    synchronized (lock) {
      now = List.copyOf(picks);
    }
    probePanel.label("count").set(countText(now.size()));
    if (now.isEmpty()) {
      picksOverlay.clear();
      return;
    }
    double[] flat = new double[now.size() * 3];
    for (int i = 0; i < now.size(); i++) System.arraycopy(now.get(i).position(), 0, flat, 3 * i, 3);
    List<Item> items = new ArrayList<>();
    items.add(Item.markers("picks", flat).inline().voxelFrame().markerShape("disc").sizePx(8)
        .color(PICK_RGBA[0], PICK_RGBA[1], PICK_RGBA[2], PICK_RGBA[3]));
    for (Pick p : now) {
      double[] q = p.position();
      items.add(Item.text("label-" + p.number(), Integer.toString(p.number()), q[0], q[1], q[2]).voxelFrame().textSize(0)
          .color(PICK_RGBA[0], PICK_RGBA[1], PICK_RGBA[2], PICK_RGBA[3]));
    }
    picksOverlay.setItems(items);
  }

  // ---- undo, clear, contribute (§6, §"Contributing"): run as jobs, so after a pick in flight ----

  String undo() {
    Pick gone;
    synchronized (lock) {
      gone = picks.isEmpty() ? null : picks.remove(picks.size() - 1);
    }
    pushList();
    say(gone == null ? "Nothing to undo" : "Undid pick " + gone.number());
    return "";
  }

  String clear() {
    synchronized (lock) {
      picks.clear();
    }
    pushList();
    say("Cleared the picks");
    return "";
  }

  String contribute(Host host) throws Exception {
    List<Pick> now;
    synchronized (lock) {
      now = List.copyOf(picks);
    }
    double r = radius.get();
    ContributeRois.Builder batch = ContributeRois.newBuilder().setReplaceAll(true);
    for (Pick p : now) {
      double[] q = p.position();
      batch.addRois(RoiContribution.newBuilder()
          .setContribId("pick-" + p.number())
          .setName("Pick " + p.number())
          .setStyle(RoiStyle.newBuilder().setColor(ROI_COLOR))
          .setRoi(Rois.ellipsoid(q[0], q[1], q[2], r, r, r)));
    }
    try {
      host.contributeRois(batch.build());
    } catch (HostException e) {
      say("Contribute failed: " + e.kind() + ": " + e.getMessage());
      throw e;
    }
    int n = now.size();
    String said = n == 0 ? "Cleared the contributed ROIs" : "Contributed " + n + (n == 1 ? " ROI" : " ROIs");
    say(said);
    return said;
  }
}
