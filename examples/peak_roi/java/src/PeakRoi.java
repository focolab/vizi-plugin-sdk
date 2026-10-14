import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.TimeoutException;
import pois_math.v1.RoiOuterClass.Roi;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.v1.ViziPlugin.ContributeRois;
import vizi.plugin.v1.ViziPlugin.RoiContribution;
import vizi.plugin.v1.ViziPlugin.RoiStyle;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * The peak-finder ROI plugin (../SPEC.md): blur the active volume, find the brightest voxel, and
 * contribute an ellipsoid there. The panel's widgets are typed handles, so the SDK answers the
 * four {@code set_*} commands itself; {@code peak.run} validates, then hands the analysis to a
 * job and reports on the {@code status} label.
 */
public final class PeakRoi {
  private PeakRoi() {}

  static final String PANEL = "peak";
  static final String CONTRIB_ID = "peak";
  static final String ROI_NAME = "Peak";
  static final String ROI_COLOR = "#ff4081";
  static final String CURRENT = "Current timepoint";
  static final String ALL = "All timepoints";

  public static void main(String[] args) {
    System.exit(PluginMain.run(plugin(), args));
  }

  /** The panel of SPEC.md's manifest, widget for widget. */
  static PanelDecl panelDecl() {
    return PanelDecl.of(PANEL, "Peak ROI", WidgetDecl.column("root",
        extentRow("z", "Z", "4"),
        extentRow("y", "Y", "8"),
        extentRow("x", "X", "8"),
        new WidgetDecl.Dropdown("mode", "peak.set_mode", List.of(CURRENT, ALL), Optional.of(CURRENT)),
        WidgetDecl.button("go", "peak.run", "Find peak"),
        WidgetDecl.label("status", "Ready")));
  }

  private static WidgetDecl extentRow(String axis, String upper, String value) {
    return WidgetDecl.row("r" + axis,
        WidgetDecl.label("l" + axis, upper + " extent (voxels)"),
        new WidgetDecl.LineEdit("e" + axis, "peak.set_" + axis, Optional.empty(), Optional.of(value)));
  }

  /** SPEC: an extent is a finite, strictly positive number of voxels (finiteness is the SDK's check). */
  static Panel.Check<Double> positive(String upper) {
    return v -> v > 0 ? null : upper + " extent " + vizi.plugin.javasdk.Fmt.g(v) + " is not a positive number of voxels";
  }

  public static Plugin plugin() {
    var b = Plugin.builder("org.example.peak_roi", "Peak ROI", "0.1.0")
        .entry("./peak-roi")
        .permissions(Permission.DATA_ACCESS, Permission.CONTRIBUTE_ROIS);
    for (String a : new String[] {"Z", "Y", "X"}) {
      b.command(CommandDecl.of("peak.set_" + a.toLowerCase(java.util.Locale.ROOT), "Set " + a + " extent").unlisted());
    }
    b.command(CommandDecl.of("peak.set_mode", "Set timepoint mode").unlisted());

    Panel panel = b.panel(panelDecl());
    Panel.Label status = panel.label("status");
    status.set("Ready"); // into the mirror, so every PanelOpened re-pushes the status too
    Panel.Number ez = extent(panel, status, "z", "Z");
    Panel.Number ey = extent(panel, status, "y", "Y");
    Panel.Number ex = extent(panel, status, "x", "X");
    Panel.Choice mode = panel.choice("mode");
    mode.reportTo(status);
    mode.onChange(m -> status.set("Mode: " + m.toLowerCase(java.util.Locale.ROOT) + "."));

    Jobs jobs = b.jobs(o -> status.set(o.text()));
    b.command(CommandDecl.of("peak.run", "Find peak").unlisted(), ctx -> {
      double[] extents = {ez.get(), ey.get(), ex.get()};
      boolean all = mode.get().orElse(CURRENT).equals(ALL);
      if (jobs.busy()) {
        status.set("Busy: a run is already in progress.");
        throw Refusal.unsupported("a peak run is already in progress");
      }
      ViewState view;
      try {
        view = ctx.host().viewState();
      } catch (HostException | TimeoutException e) {
        status.set("No dataset is open (" + e.getMessage() + ").");
        throw Refusal.unsupported("could not read the view state: " + e.getMessage());
      }
      List<Integer> shape = view.getShapeTczyxList();
      if (shape.size() != 5 || shape.contains(0)) {
        status.set("No dataset is open.");
        throw Refusal.unsupported("no dataset is open");
      }
      int c = Host.shownChannel(view); // SPEC §1: visible_channels[0], or 0; never ViewState.c
      int t = view.getT();
      int frames = shape.get(0);
      try {
        jobs.start("Peak run", job -> run(job, status, extents, all, t, c, frames));
      } catch (Refusal r) {
        status.set("Busy: a run is already in progress.");
        throw r;
      }
      return "Peak run started; progress is on the Peak ROI panel.";
    });
    return b.build();
  }

  private static Panel.Number extent(Panel panel, Panel.Label status, String axis, String upper) {
    Panel.Number n = panel.number("e" + axis, positive(upper));
    n.reportTo(status);
    n.onChange(v -> status.set(upper + " extent set to " + vizi.plugin.javasdk.Fmt.g(v) + "."));
    return n;
  }

  /** The job: read, blur, find, contribute; its returned text is the final status. */
  static String run(Jobs.Job job, Panel.Label status, double[] e, boolean all, int t, int c, int frames) throws Exception {
    Host host = job.host();
    Roi roi;
    String summary;
    if (all) {
      List<int[]> peaks = new ArrayList<>();
      for (int f = 0; f < frames; f++) {
        job.checkpoint();
        status.set("Reading frame " + (f + 1) + "/" + frames + "…");
        peaks.add(peakAt(host, f, c, e));
      }
      int[] first = peaks.get(0);
      double[][] pose = new double[frames][3];
      for (int f = 0; f < frames; f++) {
        for (int a = 0; a < 3; a++) pose[f][a] = peaks.get(f)[a] - first[a];
      }
      roi = Rois.ellipsoid(first[0] + 0.5, first[1] + 0.5, first[2] + 0.5, e[0], e[1], e[2], pose);
      summary = "peak " + show(first) + " at t=0, tracked over " + frames + " frames (channel " + c + ")";
    } else {
      status.set("Reading t=" + t + ", c=" + c + "…");
      int[] p = peakAt(host, t, c, e);
      roi = Rois.ellipsoid(p[0] + 0.5, p[1] + 0.5, p[2] + 0.5, e[0], e[1], e[2]);
      summary = "peak " + show(p) + " at t=" + t + " (channel " + c + ")";
    }
    job.checkpoint();
    status.set("Contributing ROI…");
    host.contributeRois(ContributeRois.newBuilder()
        .addRois(RoiContribution.newBuilder().setContribId(CONTRIB_ID).setRoi(roi).setName(ROI_NAME)
            .setStyle(RoiStyle.newBuilder().setColor(ROI_COLOR)))
        .build());
    return "Done: " + summary + ".";
  }

  /** One level-0 read (never ViewState.level: ROI positions are level-0 indices). */
  static int[] peakAt(Host host, int t, int c, double[] e) throws Exception {
    Volume v = host.readVolume(t, c, 0).volume();
    return Analysis.peak(v, e[0], e[1], e[2]);
  }

  static String show(int[] p) {
    return "(" + p[0] + ", " + p[1] + ", " + p[2] + ")";
  }
}
