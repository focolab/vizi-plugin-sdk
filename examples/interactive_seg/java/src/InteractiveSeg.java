import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.checkbox;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.dropdown;
import static vizi.plugin.javasdk.manifest.WidgetDecl.image;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;
import static vizi.plugin.javasdk.manifest.WidgetDecl.row;
import static vizi.plugin.javasdk.manifest.WidgetDecl.slider;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import vizi.plugin.javasdk.ArenaFullException;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.InputEvent;
import vizi.plugin.javasdk.Item;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Overlay;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Position;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Tool;
import vizi.plugin.javasdk.ToolContext;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.v1.ViziPlugin.DataVolume;
import vizi.plugin.v1.ViziPlugin.Extent;
import vizi.plugin.v1.ViziPlugin.MaskVoxels;
import vizi.plugin.v1.ViziPlugin.ReadDataVolume;
import vizi.plugin.v1.ViziPlugin.RoiRecord;
import vizi.plugin.v1.ViziPlugin.ToolActivationChanged;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * Interactive segmentation (../SPEC.md) on the Java SDK: a lasso tool whose trail rides the
 * {@code seg.trail} overlay's Pulsed lane while the user drags, a segmentation on a job (the
 * threshold, the anchor, the components: {@link Lasso}, {@link Grow}), the result pushed as a
 * label volume on {@code seg.labels}, a commit into native masks, and a mean-versus-t plot pushed
 * through the panel arena ({@link Plot}).
 */
public final class InteractiveSeg {

  static final String ID = "org.example.interactive_seg";
  static final String TOOL = "seg.lasso";
  static final String PANEL = "seg";

  static final String MSG_MIP = "Turn off Z-MIP — the tool needs to know which plane you are pointing at.";
  static final String MSG_LEVEL = "Zoom in to full resolution — the tool segments level-0 voxels.";
  static final String MSG_CHANNELS = "Show a single channel — the tool cannot tell which one you mean.";
  static final String MSG_ACTIVE = "Lasso active — drag a loop in a 2D view";
  static final String MSG_READY = "Ready";
  static final String MSG_BUSY = "Busy — a job is already running.";

  static final double TRAIL_SIZE_PX = 5.0;

  // ---- the plugin's state ----

  /** §9's accumulation, scoped to one gesture frame {@code (t, c, gen)}. */
  record Segmentation(long t, int c, long gen, Grow.LabelMap labels) {}

  /** A committed region, in dropdown order. */
  record Region(String name, String regionId) {}

  private final Object lock = new Object();
  private Segmentation seg; // guarded by lock
  private List<Region> regions = List.of(); // guarded by lock
  private int regionIndex; // guarded by lock

  private Overlay trail;
  private Overlay labelsOverlay;
  private Panel panel;
  private Panel.Label status;
  private Panel.Control commitButton;
  private Panel.Control measureRow;
  private Panel.Whole minSize;
  private Panel.Number thresholdScale;
  private Panel.Flag grow3d;
  private Panel.Choice mode;
  private Panel.Choice region;
  private Jobs jobs;

  public static void main(String[] args) {
    System.exit(PluginMain.run(new InteractiveSeg().plugin(), args));
  }

  Plugin plugin() {
    var b = Plugin.builder(ID, "Interactive Segmentation", "0.1.0")
        .entry("./interactive-seg")
        .permissions(Permission.DATA_ACCESS, Permission.CONTRIBUTE_ROIS);
    b.tool(ToolDecl.of(TOOL).withName("Lasso segment").withViews(ToolDecl.View.TWO_D), new LassoTool());
    trail = b.overlay(OverlayDecl.drawn("seg.trail").withName("Lasso trail"));
    labelsOverlay = b.overlay(OverlayDecl.drawn("seg.labels", 64).withName("Segmentation"));

    b.command(CommandDecl.of("seg.set_min_size", "Set minimum size").unlisted());
    b.command(CommandDecl.of("seg.set_threshold", "Set threshold scale").unlisted());
    b.command(CommandDecl.of("seg.set_3d", "Set 3D growth").unlisted());
    b.command(CommandDecl.of("seg.set_mode", "Set accumulate mode").unlisted());
    b.command(CommandDecl.of("seg.set_region", "Set measured region").unlisted());
    b.command(CommandDecl.of("seg.pick", "Activate lasso").unlisted(), this::pick);
    b.command(CommandDecl.of("seg.commit", "Commit to annotations").unlisted(), ctx -> commit());
    b.command(CommandDecl.of("seg.measure", "Measure over time").unlisted(), ctx -> measure());
    b.command(CommandDecl.of("seg.reset", "Clear segmentation").unlisted(), ctx -> reset());

    panel = b.panel(PanelDecl.of(PANEL, "Interactive Segmentation", column("root",
        button("pick", "seg.pick", "Select region…"),
        row("rsize", label("lsize", "Minimum size (voxels)"),
            slider("size", "seg.set_min_size").range(1, 5000).step(10).value(50)),
        row("rthresh", label("lthresh", "Threshold ×"),
            slider("thresh", "seg.set_threshold").range(0.5, 2.0).step(0.05).value(1.0).continuous(true)),
        checkbox("grow3d", "seg.set_3d").text("Grow through Z").value(true),
        dropdown("mode", "seg.set_mode", List.of("Replace", "Add")).value("Replace"),
        button("commit", "seg.commit", "Commit to annotations"),
        row("rmeasure",
            dropdown("region", "seg.set_region", List.of("(none)")).value("(none)"),
            button("measure", "seg.measure", "Measure")),
        image("plot"),
        button("reset", "seg.reset", "Clear"),
        label("status", "Ready"))));

    // The typed handles own their commands, keep the model, and re-push it on PanelOpened.
    status = panel.label("status");
    commitButton = panel.control("commit");
    measureRow = panel.control("rmeasure");
    minSize = panel.integer("size", v -> v < 1 ? "minimum size must be at least 1, not " + v : null);
    thresholdScale = panel.number("thresh", v -> v <= 0 ? "threshold scale must be positive, not " + v : null);
    grow3d = panel.flag("grow3d");
    mode = panel.choice("mode");
    region = panel.choice("region");
    region.onChange(this::regionChosen);
    // The mirror starts as SPEC.md's UI state model says; the SDK re-pushes it on every open.
    status.set(MSG_READY);
    commitButton.enabled(false);
    measureRow.visible(false);

    jobs = b.jobs(outcome -> {
      if (outcome.status() == Jobs.Status.FAILED) say("Failed: " + outcome.message());
    });
    return b.build();
  }

  void say(String text) {
    status.set(text);
  }

  // ---- the tool (§1, §2) ----

  /** What the first usable record fixed for the whole gesture (§1). */
  static final class Gesture {
    final long interaction;
    final InputEvent.Info press; // its pane pair is the one the live lane is stamped with
    final int[] shape; // (Z, Y, X), or null when the pane is not known
    Integer zG;
    long t;
    int level;
    long gen;
    List<Integer> visible; // null: bit 15 set with no pane to resolve it (note 8)
    boolean heard;
    String refusal;
    boolean liveFailed;
    final Lasso.Path path = new Lasso.Path();

    Gesture(InputEvent.Info press, int[] shape) {
      this.interaction = press.interaction();
      this.press = press;
      this.shape = shape;
    }
  }

  static List<Integer> visibleChannels(InputEvent.Info ev) {
    if ((ev.channels() & 0x8000) != 0 && ev.pane().isEmpty()) return null;
    return ev.visibleChannels();
  }

  /** §1: what one record contributes to the gesture. */
  static void take(InputEvent.Info ev, Gesture g) {
    if (!ev.usable()) return;
    if (!g.heard) {
      g.heard = true;
      g.t = ev.t();
      g.level = ev.level();
      g.gen = ev.gen();
      g.visible = visibleChannels(ev);
    }
    if (ev.position() instanceof Position.Column) {
      g.refusal = MSG_MIP; // §3: the record is the authority on a projection
    } else if (ev.position() instanceof Position.Voxel v) {
      if (g.zG == null) g.zG = (int) Math.rint(v.z());
      if (!ev.miss()) g.path.add(v.y(), v.x());
    }
  }

  final class LassoTool implements Tool {
    private Gesture gesture; // guarded by this

    @Override
    public void activated(ToolContext ctx) {
      say(MSG_ACTIVE);
    }

    @Override
    public void deactivated(ToolContext ctx) {
      say(MSG_READY);
    }

    @Override
    public synchronized void press(ToolContext ctx, InputEvent.Press ev) {
      InputEvent.Info i = ev.info();
      int[] shape = i.pane().map(p -> {
        Extent e = p.geometry().getExtent();
        return new int[] {e.getZN(), e.getYN(), e.getXN()};
      }).orElse(null);
      gesture = new Gesture(i, shape);
      take(i, gesture);
      publish(gesture);
    }

    @Override
    public synchronized void drag(ToolContext ctx, InputEvent.Drag ev) {
      Gesture g = gesture;
      if (g == null || g.interaction != ev.interaction()) return;
      take(ev.info(), g);
      publish(g);
    }

    @Override
    public synchronized void release(ToolContext ctx, InputEvent.Release ev) {
      Gesture g = gesture;
      gesture = null;
      if (g == null || g.interaction != ev.interaction()) return;
      take(ev.info(), g);
      trail.clear(); // from the handler, before the handoff
      if (g.refusal != null) {
        say(g.refusal);
        return;
      }
      if (!g.heard || g.zG == null) return; // no usable record reached it: silence (Acceptance 16)
      // §3's refusals are keyed on the record and need no host: answer them here
      if (g.level != 0) {
        say(MSG_LEVEL);
        return;
      }
      if (g.visible == null || g.visible.size() != 1) {
        say(MSG_CHANNELS);
        return;
      }
      Settings s = new Settings(minSize.get(), thresholdScale.get(), grow3d.get(), mode.get().orElse("Replace"));
      try {
        jobs.start("segmentation", job -> {
          segmentJob(job.host(), g, s);
          return "";
        });
      } catch (Refusal busy) {
        say(MSG_BUSY);
      }
    }

    @Override
    public synchronized void cancel(ToolContext ctx, InputEvent.Cancel ev) {
      if (gesture != null && gesture.interaction == ev.interaction()) gesture = null;
      trail.clear();
    }

    /** §2: the trail as markers at z_g on the live lane, decimated, stamped with the press's pane. */
    private void publish(Gesture g) {
      if (g.refusal != null || g.level != 0 || g.zG == null || g.path.size() == 0) return;
      int[] picks = Lasso.decimate(g.path.size(), Lasso.LIVE_MAX_POINTS);
      double[] zyx = new double[3 * picks.length];
      for (int k = 0; k < picks.length; k++) {
        double[] p = g.path.get(picks[k]);
        zyx[3 * k] = g.zG;
        zyx[3 * k + 1] = p[0];
        zyx[3 * k + 2] = p[1];
      }
      Item markers = Item.markers("trail", zyx).voxelFrame().color(255, 255, 255, 220).sizePx(TRAIL_SIZE_PX).inline();
      String failure;
      try {
        Overlay.Live r = trail.publishLive(g.press, List.of(markers));
        if (r != Overlay.Live.REFUSED) return; // COALESCED: the newest committed value stands
        failure = "the trail's lane is not granted or is quarantined";
      } catch (RuntimeException e) {
        failure = e.getMessage();
      }
      if (!g.liveFailed) {
        g.liveFailed = true;
        say("Live preview unavailable (" + failure + "); the segmentation will still run on release.");
      }
    }
  }

  record Settings(long minSize, double scale, boolean grow3d, String mode) {}

  // ---- the segmentation job (§3–§9) ----

  void segmentJob(Host host, Gesture g, Settings s) throws Exception {
    try {
      segment(host, g, s);
    } catch (Grow.Refused r) {
      say(r.getMessage());
    } catch (HostException e) {
      say("Failed: " + e.getMessage());
    } catch (ArenaFullException e) {
      say("The segmentation does not fit the overlay's arena (" + e.wanted() + " bytes wanted, " + e.largestFree()
          + " free); commit or clear first.");
    }
  }

  private void segment(Host host, Gesture g, Settings s) throws Exception {
    int c = g.visible.get(0);
    int zG = g.zG;
    int[] shape = g.shape;
    if (shape == null) {
      ViewState v = host.viewState();
      shape = new int[] {v.getShapeTczyx(2), v.getShapeTczyx(3), v.getShapeTczyx(4)};
    }
    int depth = shape[0], height = shape[1], width = shape[2];
    if (zG < 0 || zG >= depth) throw new Grow.Refused(Grow.MSG_LOOP);

    // §4
    boolean[] selected = Lasso.selectedColumns(g.path.ys(), g.path.xs(), height, width);
    if (Lasso.bbox2(selected, height, width) == null) throw new Grow.Refused(Grow.MSG_LOOP);

    // §5: one sub-box read at level 0
    Lasso.Window w = Lasso.searchWindow(selected, depth, height, width);
    say("Reading " + w.height() + "×" + w.width() + " columns through " + depth + " planes…");
    Host.Deposit<DataVolume> read = host.readVolume(ReadDataVolume.newBuilder()
        .setT((int) g.t).setC(c).setLevel(0)
        .addRoiMinZyx(w.lo()[0]).addRoiMinZyx(w.lo()[1]).addRoiMinZyx(w.lo()[2])
        .addRoiMaxZyx(w.hi()[0]).addRoiMaxZyx(w.hi()[1]).addRoiMaxZyx(w.hi()[2])
        .build());
    if ((read.reply().getOpenGeneration() & 0xFFFFFFFFL) != (g.gen & 0xFFFFFFFFL)) {
      throw new Grow.Refused("The dataset changed under the gesture — draw again.");
    }
    Volume win = read.volume();
    if (win.z() != w.depth() || win.y() != w.height() || win.x() != w.width()) {
      throw new Grow.Refused("The host returned a " + win + " for a " + w.depth() + "×" + w.height() + "×" + w.width() + " window.");
    }
    boolean[] selW = Lasso.crop(selected, width, w);

    // §6–§8
    double t = Grow.threshold(win, selW, s.scale());
    boolean[][] faces = {
      {w.lo()[0] == 0, w.hi()[0] == depth}, {w.lo()[1] == 0, w.hi()[1] == height}, {w.lo()[2] == 0, w.hi()[2] == width},
    };
    Grow.Grown grown = Grow.grow(win, selW, zG, t, s.grow3d(), s.minSize(), faces);
    Grow.Placed placed = Grow.tight(grown.component(), w.depth(), w.height(), w.width(), w.lo());

    // §9: accumulate, scoped to (t, c, gen)
    Segmentation current;
    int label;
    String note = "";
    synchronized (lock) {
      Segmentation old = seg;
      boolean sameFrame = old != null && old.t() == g.t && old.c() == c && old.gen() == g.gen;
      if (old != null && s.mode().equals("Add") && !sameFrame) note = " (new frame — starting a fresh accumulation)";
      current = s.mode().equals("Add") && sameFrame ? old : new Segmentation(g.t, c, g.gen, new Grow.LabelMap());
      label = current.labels().add(placed);
      seg = current;
    }
    // One use: the push alone (§"Committing", route 2) — the commit reserves again.
    labelsOverlay.setItems(List.of(labelItem(current)));
    commitButton.enabled(true);
    say(String.format(Locale.ROOT, "Label %d: %d voxels at threshold %s%s%s", label, grown.size(), Plot.num(t),
        grown.clipped() ? "; clipped by the search window" : "", note));
  }

  /** §9's item: bbox-tight u8 labels, the LUT, an integer origin, {@code t}, names keyed by id. */
  static Item labelItem(Segmentation s) {
    // only jobs touch a label map, and one job runs at a time
    Grow.Rendered r = s.labels().render();
    int n = s.labels().size();
    Item item = Item.labels("labels", Volume.u8(r.shape()[0], r.shape()[1], r.shape()[2], r.labels()),
        r.origin()[0], r.origin()[1], r.origin()[2], Grow.lut()).at((int) s.t());
    for (int k = 1; k <= n; k++) item.name(k, "Region " + k);
    return item;
  }

  // ---- commands ----

  private String pick(vizi.plugin.javasdk.CommandContext ctx) throws Exception {
    ViewState v = ctx.host().viewState();
    if (v.getMip()) {
      say(MSG_MIP);
      throw Refusal.invalid(MSG_MIP);
    }
    ToolActivationChanged a;
    try {
      a = ctx.host().activateTool(TOOL, true);
    } catch (HostException e) {
      String m = "Could not activate the lasso: " + e.getMessage();
      say(m);
      throw Refusal.invalid(m);
    }
    if (!a.getActive()) {
      String m = "The host declined to activate the lasso.";
      say(m);
      throw Refusal.invalid(m);
    }
    return "Lasso active";
  }

  private String startJob(String label, Jobs.Work work) throws Refusal {
    try {
      return jobs.start(label, work);
    } catch (Refusal busy) {
      say(MSG_BUSY);
      throw Refusal.unsupported(MSG_BUSY);
    }
  }

  private String commit() throws Refusal {
    Segmentation s;
    synchronized (lock) {
      s = seg;
    }
    if (s == null || s.labels().size() == 0) {
      String m = "Nothing to commit — segment something first.";
      say(m);
      throw Refusal.invalid(m);
    }
    startJob("commit", job -> {
      try {
        commitJob(job.host(), s);
      } catch (HostException e) {
        say("Commit failed: " + e.getMessage());
      } catch (ArenaFullException e) {
        say("The commit does not fit the overlay's arena (" + e.wanted() + " bytes wanted, " + e.largestFree() + " free).");
      }
      return "";
    });
    return "Commit started; the outcome is on the Interactive Segmentation panel.";
  }

  private void commitJob(Host host, Segmentation s) throws Exception {
    // §"Committing", route 2: the SDK lends the compile's geometry afresh (Host.compileLabelVolume)
    int count = host.compileLabelVolume(labelsOverlay, labelItem(s), true).getRegionCount();
    labelsOverlay.clear(); // the host draws the masks now
    List<Region> found = ours(host);
    synchronized (lock) {
      regions = found;
      regionIndex = 0;
    }
    showRegions(found);
    measureRow.visible(!found.isEmpty());
    say("Committed " + count + " region" + (count == 1 ? "" : "s") + " as native masks");
  }

  /** This plugin's regions, by label id, their names made unique for the dropdown. */
  private static List<Region> ours(Host host) throws Exception {
    List<RoiRecord> mine = new ArrayList<>();
    for (RoiRecord r : host.rois().getRoisList()) if (r.getOrigin().equals("plugin:" + ID)) mine.add(r);
    mine.sort((a, b) -> Long.compare(key(a), key(b)));
    List<Region> out = new ArrayList<>();
    List<String> seen = new ArrayList<>();
    for (RoiRecord r : mine) {
      String name = r.getName().isEmpty() ? "Region " + r.getContribId() : r.getName();
      String unique = name;
      for (int k = 2; seen.contains(unique); k++) unique = name + " (" + k + ")";
      seen.add(unique);
      out.add(new Region(unique, r.getRegionId()));
    }
    return out;
  }

  private static long key(RoiRecord r) {
    try {
      return Long.parseLong(r.getContribId());
    } catch (NumberFormatException e) {
      return Long.MAX_VALUE;
    }
  }

  private void showRegions(List<Region> rs) {
    List<String> names = new ArrayList<>();
    for (Region r : rs) names.add(r.name());
    if (names.isEmpty()) names.add("(none)");
    region.options(names);
    region.set(names.get(0));
  }

  /** The dropdown's value, resolved to its index: names are unique because {@link #ours} made them so. */
  private void regionChosen(String name) {
    synchronized (lock) {
      for (int i = 0; i < regions.size(); i++) {
        if (regions.get(i).name().equals(name)) {
          regionIndex = i;
          return;
        }
      }
    }
  }

  private String measure() throws Refusal {
    List<Region> rs;
    int index;
    synchronized (lock) {
      rs = regions;
      index = regionIndex;
    }
    if (rs.isEmpty()) {
      String m = "Nothing to measure — commit first.";
      say(m);
      throw Refusal.invalid(m);
    }
    startJob("measure", job -> {
      try {
        measureJob(job, rs, index);
      } catch (Grow.Refused r) {
        say(r.getMessage());
      } catch (HostException e) {
        say("Measure failed: " + e.getMessage());
      } catch (ArenaFullException e) {
        say("The plot does not fit the panel's arena (" + e.wanted() + " bytes wanted).");
      }
      return "";
    });
    return "Measure started; the plot lands on the Interactive Segmentation panel.";
  }

  private void measureJob(Jobs.Job job, List<Region> rs, int index) throws Exception {
    Host host = job.host();
    Region r = index >= 0 && index < rs.size() ? rs.get(index) : null;
    boolean live = false;
    if (r != null) {
      for (RoiRecord rec : host.rois().getRoisList()) if (rec.getRegionId().equals(r.regionId())) live = true;
    }
    if (!live) {
      List<Region> found = ours(host);
      synchronized (lock) {
        regions = found;
        regionIndex = 0;
      }
      showRegions(found);
      throw new Grow.Refused("The selected region is gone — the list was refreshed; choose again.");
    }
    say("Reading mask " + r.name() + "…");
    Host.Deposit<MaskVoxels> m = host.readMaskVoxels(r.regionId());
    Volume mask = m.volume();
    int[] lo = {m.reply().getOriginZyx(0), m.reply().getOriginZyx(1), m.reply().getOriginZyx(2)};
    int[] hi = {lo[0] + mask.z(), lo[1] + mask.y(), lo[2] + mask.x()};
    // §"Measuring" step 3: c and T from a fresh snapshot (`c` is deprecated: the first visible channel)
    ViewState v = host.viewState();
    int frames = v.getShapeTczyx(0);
    @SuppressWarnings("deprecation")
    int c = Host.shownChannel(v); // visible_channels[0], or 0 when none is shown; never ViewState.c
    long members = 0;
    for (long i = 0; i < mask.samples(); i++) if (mask.u8(i) != 0) members++;
    double[] means = new double[frames];
    for (int t = 0; t < frames; t++) {
      job.checkpoint();
      say("Measuring " + r.name() + ": timepoint " + (t + 1) + "/" + frames + "…");
      Volume vol = host.readVolume(ReadDataVolume.newBuilder().setT(t).setC(c).setLevel(0)
          .addRoiMinZyx(lo[0]).addRoiMinZyx(lo[1]).addRoiMinZyx(lo[2])
          .addRoiMaxZyx(hi[0]).addRoiMaxZyx(hi[1]).addRoiMaxZyx(hi[2]).build()).volume();
      double sum = 0;
      for (long i = 0; i < mask.samples(); i++) if (mask.u8(i) != 0) sum += vol.get(i);
      means[t] = members == 0 ? Double.NaN : sum / members;
    }
    panel.image("plot").push(Plot.png(Plot.render(means, 240, 140)));
    say(r.name() + ": mean over " + frames + " timepoints, " + members + " voxels, channel " + c);
  }

  private String reset() {
    synchronized (lock) {
      seg = null;
    }
    labelsOverlay.clear();
    trail.clear();
    commitButton.enabled(false);
    say("Cleared");
    return "";
  }
}
