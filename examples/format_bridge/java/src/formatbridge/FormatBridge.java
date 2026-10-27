package formatbridge;

import java.lang.foreign.ValueLayout;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.atomic.AtomicReference;
import vizi.plugin.javasdk.CommandContext;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Dataset;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Exporter;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.Environment;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.control.SessionEndedException;
import vizi.plugin.javasdk.manifest.AssetDecl;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.ExporterDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.v1.ViziPlugin.AssetResult;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * The crate format bridge (../SPEC.md) on the Java SDK: one reader, one exporter, a manifest
 * asset and a runtime slot, four commands and a panel. The container is {@link Crate} and {@link
 * CrateWriter} and knows nothing about vizi; this class is the plugin.
 */
public final class FormatBridge {

  static final String ID = "org.example.format_bridge";
  static final String PANEL = "crate";
  static final String CALIBRATION_ASSET = "calibration";
  /** Runtime slots are durable per-plugin state in the host's asset store: pinned by SPEC.md. */
  static final String OVERRIDE_SLOT = "calibration_override";
  static final String CHECK_FILE_SLOT = "crate_check_file";

  static final String NO_CRATE_OPEN = "No crate open";
  static final String UNKNOWN = "(unknown)";
  static final String NOT_PROVISIONED = "(not provisioned)";
  static final String NO_SPACING_FROM_HOST = "unknown — the host publishes no voxel size to a plugin without a tool";
  static final String DIRECTLY = "read directly — host data access is unavailable for a dataset this plugin serves";
  static final String VIA_HOST = "read via host data access";

  /** Jobs queued behind a running one: describes and pickers are serialized, not refused. */
  static final int JOB_QUEUE = 4;

  private final Environment env;
  private final AtomicReference<CrateDataset> open = new AtomicReference<>();
  private final Set<CrateExport> exports = ConcurrentHashMap.newKeySet();
  private volatile Path override;

  private Panel.Label head;
  private Panel.Label status;
  private Panel.Label vox;
  private Panel.Label cal;
  private Panel.Flag verify;

  FormatBridge(Environment env) {
    this.env = env;
  }

  public static void main(String[] args) {
    if (args.length > 0 && CrateTool.COMMANDS.contains(args[0])) System.exit(CrateTool.run(args, System.out, System.err));
    // VIZI_PLUGIN_ASSETS through the SDK's own parser (control_plane_protocol.md §2.1).
    Plugin p = new FormatBridge(Environment.of(System.getenv())).plugin();
    System.exit(PluginMain.run(p, args));
  }

  // ---- the declaration ----

  Plugin plugin() {
    var b = Plugin.builder(ID, "Crate Format Bridge", "0.1.0")
        .entry("./format-bridge")
        .permissions(Permission.DATA_ACCESS)
        .asset(AssetDecl.manual(CALIBRATION_ASSET, "vizi-crate-cal.bin", 32,
            "20845d7cc64ec17ab899a993c1138352828823e55ebc1959f257a28e528671d3").withName("Crate sensor calibration v1"))
        .reader(ReaderDecl.of("crate", ".crate").withName("Crate volume container"), this::openReader)
        .exporter(ExporterDecl.of("crate", ".crate").withName("Crate volume container"), this::openExport);
    Panel panel = b.panel(PanelDecl.of(PANEL, "Crate", WidgetDecl.column("root",
            WidgetDecl.label("head", NO_CRATE_OPEN),
            WidgetDecl.label("status", "Ready"),
            WidgetDecl.row("rvox", WidgetDecl.label("lvox", "Voxel size"), WidgetDecl.label("vox", UNKNOWN)),
            WidgetDecl.row("rcal", WidgetDecl.label("lcal", "Calibration"), WidgetDecl.label("cal", NOT_PROVISIONED)),
            new WidgetDecl.Checkbox("verify", "crate.set_verify", Optional.of("Verify CRC-32 on volume reads"), Optional.of(true)),
            WidgetDecl.button("describe", "crate.describe", "Describe active dataset"),
            WidgetDecl.button("pickcal", "crate.pick_calibration", "Use a different calibration…")))
        .withMenuLabel("Crate bridge…"));
    head = panel.label("head");
    status = panel.label("status");
    vox = panel.label("vox");
    cal = panel.label("cal");
    // The checkbox's command is answered by the SDK's typed handle; this only reports the change.
    verify = panel.flag("verify");
    verify.onChange(on -> status.set("CRC-32 verification " + (on ? "on" : "off") + " for volume reads (plane reads are never verified)"));
    b.command(CommandDecl.of("crate.set_verify", "Verify CRC-32 on volume reads").unlisted());
    b.command(CommandDecl.of("crate.describe", "Describe active dataset").unlisted(), this::describe);
    b.command(CommandDecl.of("crate.pick_calibration", "Use a different calibration").unlisted(), this::pickCalibration);
    b.command(CommandDecl.of("crate.check_file", "Check a crate file…").withKeybinding("Ctrl+Shift+K"), this::checkFile);
    b.jobs(o -> {
      if (o.status() == Jobs.Status.FAILED) status.set(o.text());
    }).queue(JOB_QUEUE);
    // The SDK re-pushes the panel's mirror on every open; this refreshes the one label whose truth
    // can change without an event (the next open's calibration source).
    b.onPanelOpened(PANEL, ctx -> cal.set(calibration().label()));
    // SPEC.md §5: the file is released at shutdown. This SDK does not close an open reader or
    // abort an open export at the session's end, so the plugin does it here.
    b.onShutdown((reason, detail) -> {
      CrateDataset d = open.getAndSet(null);
      if (d != null) d.crate.close();
      for (CrateExport x : exports) x.abort();
    });
    Plugin p = b.build();
    // Seed the mirror with the manifest's own defaults, so the first PanelOpened re-pushes them all.
    head.set(NO_CRATE_OPEN);
    status.set("Ready");
    vox.set(UNKNOWN);
    cal.set(calibration().label());
    return p;
  }

  // ---- the calibration table, two provisioning paths ----

  /** The live table, the {@code cal} label naming its source, and why when there is none. */
  record CalResolution(Calibration table, String label, String detail) {}

  /** Override slot if populated, then the manifest asset, then nothing; a malformed table is absent. */
  CalResolution calibration() {
    List<String> notes = new ArrayList<>();
    Path ov = override;
    if (ov != null) {
      try {
        return new CalResolution(Calibration.load(ov), "override: " + ov.getFileName(), "");
      } catch (Refusal e) {
        notes.add("override " + ov.getFileName() + " ignored: " + e.getMessage());
      }
    }
    Optional<Path> asset = env.asset(CALIBRATION_ASSET);
    if (asset.isEmpty()) {
      notes.add(env.assetsProblem().isEmpty()
          ? "the launch environment's VIZI_PLUGIN_ASSETS publishes no '" + CALIBRATION_ASSET + "' asset"
          : env.assetsProblem());
    } else if (!Files.isRegularFile(asset.get())) {
      notes.add("the '" + CALIBRATION_ASSET + "' asset is declared, but its file " + asset.get() + " is not there");
    } else {
      try {
        return new CalResolution(Calibration.load(asset.get()), "manifest asset", String.join("; ", notes));
      } catch (Refusal e) {
        notes.add("manifest asset ignored: " + e.getMessage());
      }
    }
    return new CalResolution(null, NOT_PROVISIONED, String.join("; ", notes));
  }

  // ---- the reader ----

  Dataset openReader(String uri, Path fileStateDir) throws Refusal {
    Crate cr = Crate.open(Path.of(uri));
    CalResolution r;
    try {
      r = calibration(); // resolved before any read, never during one
      if (cr.header().calibrated() && r.table() == null) {
        throw new Refusal(ErrorKind.IO_ERROR, cr.name() + " stores calibrated counts and no calibration table is available — provision"
            + " vizi-crate-cal.bin for this plugin in Plugin Manager (" + r.detail() + ")");
      }
    } catch (Refusal | RuntimeException e) {
      cr.close();
      throw e;
    }
    List<String> names = cr.channelNames();
    CrateDataset d = new CrateDataset(cr, r.table(), names == null ? List.of() : names);
    open.set(d); // before the SDK closes the reader this one replaces, so that close says nothing
    Crate.Header h = cr.header();
    String notes = "";
    if (!h.malformed().isEmpty()) notes = "; malformed header " + String.join(", ", h.malformed()) + " treated as unknown";
    if (!cr.metadataUtf8()) notes += "; metadata is not valid UTF-8 and is reported as no document";
    if (names == null) notes += "; " + cr.namesProblem();
    head.set(cr.name() + ": " + h.shape() + " " + h.dtypeName() + (h.calibrated() ? " (calibrated)" : ""));
    vox.set(cr.spacingLabel());
    cal.set(r.label());
    status.set("Opened " + cr.name() + notes);
    return d;
  }

  final class CrateDataset implements Dataset {
    final Crate crate;
    final Calibration table;
    final List<String> names;

    CrateDataset(Crate crate, Calibration table, List<String> names) {
      this.crate = crate;
      this.table = table;
      this.names = names;
    }

    @Override
    public Info info() {
      Crate.Header h = crate.header();
      boolean spaced = h.spacingZ() != 0 || h.spacingY() != 0 || h.spacingX() != 0;
      return new Info(h.t(), h.c(), h.z(), h.y(), h.x(), h.dtype(), "crate",
          known(h.spacingZ()), known(h.spacingY()), known(h.spacingX()), spaced ? "um" : "",
          known(h.timeIncrement()), h.timeIncrement() != 0 ? "s" : "", names); // the names win over any document's (§8.2)
    }

    @Override
    public Volume volume(int t, int c) throws Refusal {
      return crate.readBlock(t, c, verify.get(), table);
    }

    /** One seek and a short read, never verified: the CRC covers the whole block (SPEC.md §3). */
    @Override
    public Volume plane(int t, int z, int c) throws Refusal {
      return crate.readPlane(t, z, c, table);
    }

    /** The crate's own document; a DERIVED crate's is its source's, and none is served (SPEC.md §4). */
    @Override
    public Metadata metadata() {
      String[] doc = crate.metadata();
      return new Metadata(doc[0], doc[1]);
    }

    /**
     * On CloseReader, on the next OpenReader (after the successor opened), at shutdown. Only the
     * reader that is still the open one says "no crate open" (SPEC.md note 8).
     */
    @Override
    public void close() {
      crate.close();
      if (open.compareAndSet(this, null)) {
        head.set(NO_CRATE_OPEN);
        vox.set(UNKNOWN);
        status.set("Closed " + crate.name());
      }
    }
  }

  private static OptionalDouble known(double v) {
    return v != 0 ? OptionalDouble.of(v) : OptionalDouble.empty();
  }

  // ---- the exporter ----

  Exporter.Export openExport(Exporter.Request req) throws Refusal {
    DType dt = req.dtype();
    if (Crate.codeOf(dt) == 0) throw Refusal.unsupported("dtype " + dt + " has no crate encoding (u8, u16 and f32 do)");
    // What the crate came from: the SOURCE's document, kept verbatim as provenance and
    // never served as this crate's (DERIVED), and the host's record.
    String form = req.sourceMetadata().form();
    String text = form.isEmpty() ? "" : req.sourceMetadata().text(); // an empty form is no document, whatever the text
    // a record that is not a JSON object is none, as the SDK parses it; one that is goes in as it arrived
    byte[] provenance = req.provenance().isPresent() ? req.provenanceJson().getBytes(StandardCharsets.UTF_8) : new byte[0];
    // What it is: the fields' calibration alone (never the source's document), and only declared names
    Ome.Resolved res = Ome.resolve(req.spacingZ(), req.spacingY(), req.spacingX(), req.spacingUnit(), req.timeIncrement(),
        req.timeUnit());
    List<String> names = new ArrayList<>();
    for (int k = 0; k < Math.min(req.c(), req.channels().size()); k++) {
      var ch = req.channels().get(k);
      names.add(ch.declared() ? ch.name() : ""); // a host's placeholder would read back as the file's
    }
    CrateWriter w = new CrateWriter(Path.of(req.locator()), new int[] {req.t(), req.c(), req.z(), req.y(), req.x()}, dt, true,
        form.getBytes(StandardCharsets.UTF_8), text.getBytes(StandardCharsets.UTF_8), provenance, names, res.spacing().zyx(),
        res.spacing().t());
    w.begin();
    // SPEC §7: a healthy export is quiet, and one with nothing known says so on the panel; only a
    // partial calibration is worth a log line for the round trip.
    if (res.partial()) System.err.println("[" + ID + "] export " + w.name() + ": voxel size unknown for " + res.unknown());
    CrateExport x = new CrateExport(w, !req.littleEndian(), res);
    exports.add(x);
    status.set("Exporting " + w.name() + "…");
    return x;
  }

  final class CrateExport implements Exporter.Export {
    private final CrateWriter w;
    private final boolean swap;
    private final Ome.Resolved res;

    CrateExport(CrateWriter w, boolean swap, Ome.Resolved res) {
      this.w = w;
      this.swap = swap;
      this.res = res;
    }

    /**
     * The SDK ranks source {@code t} and channels (reader_writer_contract.md §2.3); a rank past
     * the declared geometry is a host bug surfaced, and nothing of the chunk is written.
     */
    @Override
    public void chunk(Exporter.Chunk ch) throws Refusal {
      Crate.Header h = w.header();
      if (ch.tRank() >= h.t()) {
        throw Refusal.outOfBounds("source timepoint " + ch.t() + " would be the " + ordinal(ch.tRank() + 1)
            + " distinct timepoint of an export declared with " + h.t());
      }
      for (int k = 0; k < ch.channels().size(); k++) {
        if (ch.cRanks().get(k) >= h.c()) {
          throw Refusal.outOfBounds("source channel " + ch.channels().get(k) + " would be the " + ordinal(ch.cRanks().get(k) + 1)
              + " distinct channel of an export declared with " + h.c());
        }
      }
      for (int k = 0; k < ch.channels().size(); k++) {
        byte[] bytes = ch.volume(k).data().toArray(ValueLayout.JAVA_BYTE); // consumed before the reply
        if (swap) bytes = CrateWriter.swapped(bytes, h.itemsize());
        if (w.writeBlock(ch.tRank(), ch.cRanks().get(k), bytes)) {
          System.err.println("[" + ID + "] export " + w.name() + ": (t=" + ch.tRank() + ", c=" + ch.cRanks().get(k)
              + ") arrived twice; the last write wins");
        }
      }
    }

    @Override
    public void finish() throws Refusal {
      exports.remove(this);
      try {
        w.finish();
      } catch (Refusal e) {
        status.set("Export failed: " + e.getMessage());
        throw e;
      }
      Crate.Header h = w.header();
      if (!res.anyKnown()) {
        status.set("Exported " + w.name() + " (" + blocks(h.blocks()) + ") without voxel size — the host supplied no spacing.");
      } else {
        status.set("Exported " + w.name() + " (" + blocks(h.blocks()) + "); voxel size "
            + Labels.spacing(h.spacingZyx(), h.timeIncrement()));
      }
    }

    @Override
    public void abort() {
      if (!exports.remove(this)) return;
      w.abort();
      status.set("Export of " + w.name() + " canceled; the partial file was removed.");
    }
  }

  static String blocks(int n) {
    return n + (n == 1 ? " block" : " blocks");
  }

  static String ordinal(int n) {
    String suffix = (n % 100 >= 10 && n % 100 <= 20) ? "th" : switch (n % 10) {
      case 1 -> "st";
      case 2 -> "nd";
      case 3 -> "rd";
      default -> "th";
    };
    return n + suffix;
  }

  // ---- commands ----

  /**
   * Basic statistics of the active dataset, and which path had to read them: host data access,
   * or this plugin's own reader when the host refuses (UNSUPPORTED) because this process serves it.
   */
  String describe(CommandContext ctx) throws Refusal {
    ctx.jobs().start("Describe", this::describeWork);
    return "Describing the active dataset; the result is on the Crate panel.";
  }

  private String describeWork(Jobs.Job job) throws Exception {
    Host host = job.host();
    ViewState view = host.viewState();
    List<Integer> shape = view.getShapeTczyxList();
    if (shape.size() != 5 || shape.contains(0)) {
      status.set("No dataset is open.");
      return "";
    }
    int t = view.getT();
    int c = Host.shownChannel(view); // SPEC §"crate.describe" 1: visible_channels[0], or 0; never ViewState.c
    Volume v;
    String path;
    String voxel;
    try {
      v = host.readVolume(t, c, 0).volume();
      path = VIA_HOST;
      voxel = NO_SPACING_FROM_HOST;
    } catch (HostException e) {
      if (e.kind() != Error.Kind.UNSUPPORTED) throw e;
      CrateDataset d = open.get();
      if (d == null) {
        status.set("Host data access refused (" + e.getMessage() + ") and this plugin is not serving a crate.");
        return "";
      }
      v = d.crate.readBlock(t, c, verify.get(), d.table);
      path = DIRECTLY;
      voxel = d.crate.spacingLabel();
    }
    double min = Double.POSITIVE_INFINITY;
    double max = Double.NEGATIVE_INFINITY;
    double sum = 0;
    long n = v.samples();
    for (long i = 0; i < n; i++) {
      double s = v.get(i);
      min = Math.min(min, s);
      max = Math.max(max, s);
      sum += s;
    }
    vox.set(voxel);
    status.set("t=" + t + " c=" + c + ": min " + Labels.g(min) + ", max " + Labels.g(max) + ", mean "
        + String.format(Locale.ROOT, "%.3f", sum / n) + " (" + path + ")");
    return "";
  }

  /**
   * Acknowledge, THEN prompt: a command waiting on a dialog holds the session's one request slot
   * while the person browses, so the picker runs in a job and the command returns at once.
   */
  String pickCalibration(CommandContext ctx) throws Refusal {
    ctx.jobs().start("Calibration selection", this::pickCalibrationWork);
    return "Choose a calibration table; the outcome is on the Crate panel.";
  }

  private String pickCalibrationWork(Jobs.Job job) throws Exception {
    AssetResult r;
    try {
      r = job.host().requestAsset(OVERRIDE_SLOT, true); // the SDK's 30-minute picker backstop
    } catch (SessionEndedException e) {
      return ""; // the session ended with the picker open: nothing to report to
    } catch (HostException e) {
      status.set("Calibration selection failed: " + e.getMessage());
      return "";
    }
    if (r.getDeclined() || r.getPath().isEmpty()) {
      status.set("Calibration unchanged — no file was chosen.");
      return "";
    }
    Path p = Path.of(r.getPath());
    Calibration table;
    try {
      table = Calibration.load(p);
    } catch (Refusal e) {
      status.set(p.getFileName() + " is not a calibration table (" + e.getMessage() + "); the previous calibration stands.");
      return "";
    }
    override = p;
    cal.set("override: " + p.getFileName());
    status.set("Calibration override set to " + p.getFileName() + " (" + table.channels() + " channels); it applies to the next crate opened.");
    return "";
  }

  /** The menu path: no arguments, a host picker, whole-file validation, the outcome on the panel only. */
  String checkFile(CommandContext ctx) throws Refusal {
    ctx.jobs().start("Check", this::checkFileWork);
    return "Choose a crate to check; the result is on the Crate panel.";
  }

  private String checkFileWork(Jobs.Job job) throws Exception {
    AssetResult r;
    try {
      r = job.host().requestAsset(CHECK_FILE_SLOT, true);
    } catch (SessionEndedException e) {
      return ""; // the session ended with the picker open: nothing to report to
    } catch (HostException e) {
      status.set("Check failed: " + e.getMessage());
      return "";
    }
    if (r.getDeclined() || r.getPath().isEmpty()) return ""; // a clean cancellation says nothing
    Path p = Path.of(r.getPath());
    try {
      status.set(Crate.check(p).summary(String.valueOf(p.getFileName())));
    } catch (Refusal e) {
      status.set(p.getFileName() + ": " + e.getMessage());
    }
    return "";
  }
}
