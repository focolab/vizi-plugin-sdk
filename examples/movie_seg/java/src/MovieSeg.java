import static vizi.plugin.javasdk.manifest.WidgetDecl.checkbox;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;
import static vizi.plugin.javasdk.manifest.WidgetDecl.lineEdit;
import static vizi.plugin.javasdk.manifest.WidgetDecl.row;
import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import java.util.Set;
import java.util.regex.Pattern;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.ChunkedOverlay;
import vizi.plugin.javasdk.ChunkedOverlay.Chunk;
import vizi.plugin.javasdk.ChunkedOverlay.Want;
import vizi.plugin.javasdk.CommandContext;
import vizi.plugin.javasdk.Panel;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * Movie seg (../SPEC.md): every frame of a movie cut into three intensity bands above a threshold,
 * served as a chunked overlay ({@code bands}, per t), plus the {@code static} footprint of
 * everywhere the foreground ever was. The SDK owns the want set, the serving thread, the
 * deferral and the re-grant; this file owns the sources, the tables, the five commands and the
 * panel (§5a: a threshold box and a checkbox per band, which is how a person starts the plugin in
 * vizi, and which shows the plugin's state). {@link Bands} is the arithmetic.
 */
public final class MovieSeg {

  static final String BANDS = "bands";
  static final String FOOTPRINT = "footprint";
  /** SPEC §5a: the panel a person opens in vizi, which starts the session. */
  static final String PANEL = "movie_seg";
  static final String THRESHOLD_BOX = "threshold";
  /** SPEC §2: the channel segmented. */
  static final int CHANNEL = 0;

  private final Object lock = new Object();
  private double threshold = Bands.DEFAULT_THRESHOLD;
  private final Set<Integer> hidden = new HashSet<>();
  private final Set<String> tablesSent = new HashSet<>();
  private ChunkedOverlay bands;
  private ChunkedOverlay footprint;
  private Panel panel;

  public static void main(String[] args) {
    System.exit(PluginMain.run(new MovieSeg().plugin(), args));
  }

  Plugin plugin() {
    Plugin.Builder b = Plugin.builder("org.example.movie_seg", "Movie seg", "0.1.0")
        .entry("./movie-seg")
        .permissions(Permission.DATA_ACCESS);
    bands = b.chunkedOverlay(OverlayDecl.chunked(BANDS, OverlayDecl.TimeMode.PER_T).withName("Intensity bands"), this::serveBands);
    footprint = b.chunkedOverlay(OverlayDecl.chunked(FOOTPRINT, OverlayDecl.TimeMode.STATIC).withName("Footprint"), this::serveFootprint);
    b.command(CommandDecl.of("movie_seg.threshold", "Set threshold").unlisted(), this::setThreshold);
    b.command(CommandDecl.of("movie_seg.band_visible", "Show or hide a band").unlisted(), this::bandVisible);
    // A checkbox fires its command with {"value": bool} and nothing naming the widget, so each
    // band's checkbox has a command of its own.
    for (int i = 1; i <= 3; i++) {
      int band = i;
      b.command(CommandDecl.of("movie_seg.band_" + band, "Show band " + band + " (" + Bands.BAND_NAMES.get(band) + ")").unlisted(),
          ctx -> setBand(band, "value", ctx.args().get("value")));
    }
    panel = b.panel(PanelDecl.of(PANEL, "Movie seg", column("root",
        row("rt", label("lt", "Threshold"),
            lineEdit(THRESHOLD_BOX, "movie_seg.threshold").value(Bands.formatThreshold(Bands.DEFAULT_THRESHOLD))),
        checkbox("band_1", "movie_seg.band_1").text("Band 1 (dim)").value(true),
        checkbox("band_2", "movie_seg.band_2").text("Band 2 (mid)").value(true),
        checkbox("band_3", "movie_seg.band_3").text("Band 3 (bright)").value(true))));
    return b.build();
  }

  private double thresholdNow() {
    synchronized (lock) {
      return threshold;
    }
  }

  /** Sends an overlay's table (under the lock, so the newest visibility is the one sent last). */
  private void sendTable(ChunkedOverlay o) {
    synchronized (lock) {
      o.labelTable(o == bands ? Bands.bandTable(hidden) : Bands.footprintTable());
    }
  }

  /** SPEC §3: once per overlay per session, no later than its first chunk. */
  private void tableOnce(ChunkedOverlay o) {
    synchronized (lock) {
      if (tablesSent.add(o.id())) sendTable(o);
    }
  }

  /** One frame at the wanted level, banded, the tight box. */
  Chunk serveBands(ChunkedOverlay.Context ctx, Want want) throws Exception {
    tableOnce(bands);
    if (want.t().isEmpty()) throw new IllegalArgumentException("a per_t want with no t: " + want);
    double t = thresholdNow();
    Volume frame = ctx.host().readVolume(want.t().getAsInt(), CHANNEL, want.level()).volume();
    return Bands.chunk(Bands.bandLabels(frame, t));
  }

  /** The union over every timepoint at the wanted level; {@code t} absent on the want and the chunk. */
  Chunk serveFootprint(ChunkedOverlay.Context ctx, Want want) throws Exception {
    tableOnce(footprint);
    double t = thresholdNow();
    int frames = ctx.host().viewState().getShapeTczyx(0);
    if (frames == 0) return Chunk.empty();
    List<Volume> volumes = new ArrayList<>(frames);
    for (int i = 0; i < frames; i++) volumes.add(ctx.host().readVolume(i, CHANNEL, want.level()).volume());
    return Bands.chunk(Bands.footprintLabels(volumes, t));
  }

  /** A decimal number, as the panel's box might hold one: no hex, no {@code d}/{@code f} suffix. */
  private static final Pattern DECIMAL = Pattern.compile("[+-]?(\\d+\\.?\\d*|\\.\\d+)([eE][+-]?\\d+)?");

  /** A finite number ≥ 0, or the text of one (surrounding whitespace ignored). A bool is neither. */
  static OptionalDouble parseThreshold(@Nullable Value raw) {
    if (raw == null) return OptionalDouble.empty();
    double v;
    if (raw.hasF()) v = raw.getF();
    else if (raw.hasI()) v = raw.getI();
    else if (raw.hasS() && DECIMAL.matcher(raw.getS().strip()).matches()) v = Double.parseDouble(raw.getS().strip());
    else return OptionalDouble.empty();
    return Double.isFinite(v) && v >= 0 ? OptionalDouble.of(v) : OptionalDouble.empty();
  }

  /**
   * {@code value}: a finite number ≥ 0, or its text (the panel's box sends its text). Re-serves
   * every current want, and the box shows the threshold.
   */
  String setThreshold(CommandContext ctx) throws Refusal {
    OptionalDouble v = parseThreshold(ctx.args().get("value"));
    if (v.isEmpty()) throw Refusal.invalid("value must be a finite number >= 0, got " + describe(ctx.args().get("value")));
    synchronized (lock) {
      threshold = v.getAsDouble();
    }
    panel.setValue(THRESHOLD_BOX, Bands.formatThreshold(v.getAsDouble()));
    int wanted = 0;
    for (ChunkedOverlay o : List.of(bands, footprint)) {
      wanted += o.wants().size();
      o.invalidate();
    }
    return Bands.thresholdReply(v.getAsDouble(), wanted);
  }

  /** {@code band}: an integer in {1, 2, 3}; {@code visible}: a bool. The scripted form of the checkboxes. */
  String bandVisible(CommandContext ctx) throws Refusal {
    Map<String, Value> args = ctx.args();
    var band = ctx.integer("band");
    if (band.isEmpty() || !Bands.BAND_NAMES.containsKey((int) Math.max(Integer.MIN_VALUE, Math.min(Integer.MAX_VALUE, band.getAsLong())))) {
      throw Refusal.invalid("band must be one of " + Bands.BAND_NAMES.keySet() + ", got " + describe(args.get("band")));
    }
    return setBand((int) band.getAsLong(), "visible", args.get("visible"));
  }

  /**
   * Shows or hides one band: the bands table goes out again if it has gone out at all, and the
   * band's checkbox follows. {@code visible}, the argument named {@code arg}, must be a bool.
   */
  String setBand(int band, String arg, Value visible) throws Refusal {
    if (visible == null || !visible.hasB()) throw Refusal.invalid(arg + " must be a bool, got " + describe(visible));
    boolean on = visible.getB();
    synchronized (lock) {
      if (on) hidden.remove(band);
      else hidden.add(band);
      if (tablesSent.contains(BANDS)) sendTable(bands);
    }
    panel.setValue("band_" + band, on);
    return Bands.bandReply(band, on);
  }

  private static String describe(Value v) {
    if (v == null) return "nothing";
    return switch (v.getKindCase()) {
      case S -> "the string \"" + v.getS() + "\"";
      case F -> "the number " + v.getF();
      case I -> "the integer " + v.getI();
      case B -> "the bool " + v.getB();
      default -> "an empty value";
    };
  }
}
