import java.math.BigDecimal;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.ChunkedOverlay;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Volume;

/**
 * The arithmetic of the movie-seg plugin (../SPEC.md §2, §3, §5), with no protocol in it: the
 * band rule, the footprint's union, the tight box, the two tables and the threshold's spelling.
 */
public final class Bands {
  private Bands() {}

  public static final double BAND_WIDTH = 200.0;
  public static final double DEFAULT_THRESHOLD = 400.0;
  public static final int BANDS = 3;

  public static final Map<Integer, String> BAND_NAMES = names(1, "dim", 2, "mid", 3, "bright");
  static final int[][] BAND_COLORS = {{60, 120, 255, 255}, {255, 200, 0, 255}, {255, 60, 60, 255}};
  public static final String FOOTPRINT_NAME = "ever";
  static final int[] FOOTPRINT_COLOR = {200, 200, 200, 255};

  private static Map<Integer, String> names(Object... kv) {
    Map<Integer, String> m = new LinkedHashMap<>();
    for (int i = 0; i < kv.length; i += 2) m.put((Integer) kv[i], (String) kv[i + 1]);
    return java.util.Collections.unmodifiableMap(m);
  }

  /** SPEC §2: 0 below {@code t} or NaN, else {@code min(K, 1 + floor((v − t) / W))}. */
  public static int label(double v, double t) {
    if (!(v >= t)) return 0; // NaN compares false
    double band = 1 + Math.floor((v - t) / BAND_WIDTH);
    return band >= BANDS ? BANDS : (int) band;
  }

  /** Every voxel of {@code v} (any dtype) banded, as a u8 volume of the same shape. */
  public static Volume bandLabels(Volume v, double t) {
    Volume out = Volume.zeros(v.z(), v.y(), v.x(), DType.U8);
    for (long i = 0, n = v.samples(); i < n; i++) {
      int l = label(v.get(i), t);
      if (l != 0) out.setU8(i, l);
    }
    return out;
  }

  /** SPEC §2's footprint: 1 wherever any of {@code frames} (one shape) is at or above {@code t}. */
  public static Volume footprintLabels(List<Volume> frames, double t) {
    if (frames.isEmpty()) throw new IllegalArgumentException("no frames");
    Volume first = frames.getFirst();
    Volume out = Volume.zeros(first.z(), first.y(), first.x(), DType.U8);
    for (Volume f : frames) {
      if (f.z() != first.z() || f.y() != first.y() || f.x() != first.x()) {
        throw new IllegalArgumentException("frames of different shapes: " + first + " and " + f);
      }
      for (long i = 0, n = f.samples(); i < n; i++) if (f.get(i) >= t) out.setU8(i, 1);
    }
    return out;
  }

  /** The smallest box holding every nonzero label, at its origin; {@code null} when none. */
  public static ChunkedOverlay.@Nullable Chunk bboxTight(Volume labels) {
    int lz = Integer.MAX_VALUE, ly = Integer.MAX_VALUE, lx = Integer.MAX_VALUE, hz = -1, hy = -1, hx = -1;
    for (int k = 0; k < labels.z(); k++) {
      for (int j = 0; j < labels.y(); j++) {
        for (int i = 0; i < labels.x(); i++) {
          if (labels.u8(labels.index(k, j, i)) == 0) continue;
          lz = Math.min(lz, k); ly = Math.min(ly, j); lx = Math.min(lx, i);
          hz = Math.max(hz, k); hy = Math.max(hy, j); hx = Math.max(hx, i);
        }
      }
    }
    if (hz < 0) return null;
    int dz = hz - lz + 1, dy = hy - ly + 1, dx = hx - lx + 1;
    Volume tight = Volume.zeros(dz, dy, dx, DType.U8);
    for (int k = 0; k < dz; k++) {
      for (int j = 0; j < dy; j++) {
        for (int i = 0; i < dx; i++) {
          tight.setU8(tight.index(k, j, i), labels.u8(labels.index(lz + k, ly + j, lx + i)));
        }
      }
    }
    return ChunkedOverlay.Chunk.of(tight, lz, ly, lx);
  }

  /** The chunk for a label volume: the tight box, or the empty chunk. */
  public static ChunkedOverlay.Chunk chunk(Volume labels) {
    ChunkedOverlay.Chunk c = bboxTight(labels);
    return c != null ? c : ChunkedOverlay.Chunk.empty();
  }

  static byte[] lut(int[]... rows) {
    byte[] lut = new byte[256 * 4];
    for (int r = 0; r < rows.length; r++) {
      for (int c = 0; c < 4; c++) lut[(r + 1) * 4 + c] = (byte) rows[r][c];
    }
    return lut;
  }

  /** SPEC §3's band LUT: a hidden band keeps its color with alpha 0 (the host draws from the LUT). */
  static byte[] bandLut(Set<Integer> hidden) {
    int[][] rows = new int[BANDS][];
    for (int b = 1; b <= BANDS; b++) {
      rows[b - 1] = BAND_COLORS[b - 1].clone();
      if (hidden.contains(b)) rows[b - 1][3] = 0;
    }
    return lut(rows);
  }

  /**
   * SPEC §3's bands table: every band named and its visibility stated (for the compile), a hidden
   * band's row at alpha 0 (for the screen), drawn above the footprint that encloses it.
   */
  public static ChunkedOverlay.LabelTable bandTable(Set<Integer> hidden) {
    ChunkedOverlay.LabelTable t = ChunkedOverlay.LabelTable.of(bandLut(hidden)).opacity(0.6).drawPriority(1);
    for (var e : BAND_NAMES.entrySet()) t.name(e.getKey(), e.getValue()).visible(e.getKey(), !hidden.contains(e.getKey()));
    return t;
  }

  /** SPEC §3's footprint table: one name, no visibility. */
  public static ChunkedOverlay.LabelTable footprintTable() {
    return ChunkedOverlay.LabelTable.of(lut(FOOTPRINT_COLOR)).name(1, FOOTPRINT_NAME).opacity(0.3);
  }

  /** SPEC §5: an integral value without a decimal point, else the shortest round-tripping decimal. */
  public static String formatThreshold(double v) {
    if (v == Math.rint(v) && !Double.isInfinite(v)) return new BigDecimal(v).toBigInteger().toString();
    return new BigDecimal(Double.toString(v)).toPlainString();
  }

  public static String thresholdReply(double v, int wanted) {
    return "threshold " + formatThreshold(v) + "; " + wanted + " wanted volume(s) to serve again";
  }

  public static String bandReply(int band, boolean visible) {
    return "band " + band + " (" + BAND_NAMES.get(band) + ") " + (visible ? "visible" : "hidden");
  }
}
