import java.util.ArrayList;
import java.util.List;
import vizi.plugin.javasdk.Volume;

/**
 * The threshold, the components, the anchor and the choice (SPEC.md §6–§8), and the
 * accumulation into a label map (§9). Arrays in, arrays out: the window {@code W} has already
 * been read, as a {@code (Z, h, w)} {@link Volume}; {@code selected} is the lasso's {@code (h, w)}
 * columns in window coordinates.
 */
final class Grow {
  private Grow() {}

  static final int MAX_COMPONENT = 8_000_000;
  static final int MAX_LABELS = 255;
  static final int LABEL_ALPHA = 180;
  static final int[][] PALETTE = {
    {230, 25, 75}, {60, 180, 75}, {67, 99, 216}, {245, 130, 49},
    {145, 30, 180}, {66, 212, 244}, {240, 50, 230}, {191, 239, 69},
  };

  /** An outcome for the status line that changes nothing. */
  static final class Refused extends RuntimeException {
    private static final long serialVersionUID = 1L;

    Refused(String message) {
      super(message);
    }
  }

  static final String MSG_LOOP = "Drag a closed loop around a region.";
  static final String MSG_NOTHING = "Nothing measurable under the loop.";

  /**
   * §6: {@code scale × median} of the selected columns' maxima, in f64. NaN and −∞ never
   * contribute to a maximum; a column with nothing contributing is dropped; +∞ contributes. So
   * the result is finite or +∞.
   */
  static double threshold(Volume w, boolean[] selected, double scale) {
    int depth = w.z(), plane = w.y() * w.x();
    double[] maxima = new double[plane];
    int n = 0;
    boolean any = false;
    for (int p = 0; p < plane; p++) {
      if (!selected[p]) continue;
      any = true;
      double m = Double.NEGATIVE_INFINITY;
      boolean seen = false;
      for (int z = 0; z < depth; z++) {
        double v = w.get((long) z * plane + p);
        if (Double.isNaN(v) || v == Double.NEGATIVE_INFINITY) continue;
        if (!seen || v > m) m = v;
        seen = true;
      }
      if (seen) maxima[n++] = m;
    }
    if (!any) throw new Refused(MSG_LOOP);
    if (n == 0) throw new Refused(MSG_NOTHING);
    java.util.Arrays.sort(maxima, 0, n);
    double median = (n & 1) == 1 ? maxima[n / 2] : (maxima[n / 2 - 1] + maxima[n / 2]) / 2.0;
    return scale * median;
  }

  /**
   * Face-connected components of {@code fg}, shaped {@code (d, h, w)} (6-connected; with {@code d
   * = 1}, 4-connected in the plane). Ids {@code 1..K} in raster order of each component's first
   * voxel, so a lower id is the component holding the lower raster-order voxel; 0 is background.
   * {@code sizes[id]} is filled in.
   */
  static int[] components(boolean[] fg, int d, int h, int w, List<Integer> sizes) {
    int[] labels = new int[fg.length];
    int[] queue = new int[fg.length];
    int plane = h * w;
    sizes.clear();
    sizes.add(0);
    int next = 0;
    for (int s = 0; s < fg.length; s++) {
      if (!fg[s] || labels[s] != 0) continue;
      int id = ++next;
      int head = 0, tail = 0, count = 0;
      labels[s] = id;
      queue[tail++] = s;
      while (head < tail) {
        int i = queue[head++];
        count++;
        int z = i / plane, r = i % plane, y = r / w, x = r % w;
        if (x > 0) tail = visit(fg, labels, queue, tail, i - 1, id);
        if (x < w - 1) tail = visit(fg, labels, queue, tail, i + 1, id);
        if (y > 0) tail = visit(fg, labels, queue, tail, i - w, id);
        if (y < h - 1) tail = visit(fg, labels, queue, tail, i + w, id);
        if (z > 0) tail = visit(fg, labels, queue, tail, i - plane, id);
        if (z < d - 1) tail = visit(fg, labels, queue, tail, i + plane, id);
      }
      sizes.add(count);
    }
    return labels;
  }

  private static int visit(boolean[] fg, int[] labels, int[] queue, int tail, int j, int id) {
    if (fg[j] && labels[j] == 0) {
      labels[j] = id;
      queue[tail++] = j;
    }
    return tail;
  }

  /**
   * §7's diagnostic: the {@code (z, y, x)} of the maximum of {@code W} over all z and the selected
   * columns (NaN never wins), ties to the lowest raster-order index.
   */
  static int[] diagnostic(Volume w, boolean[] selected) {
    int plane = w.y() * w.x();
    long best = -1;
    double bestV = Double.NEGATIVE_INFINITY;
    for (long i = 0; i < w.samples(); i++) {
      if (!selected[(int) (i % plane)]) continue;
      double v = w.get(i);
      if (Double.isNaN(v)) v = Double.NEGATIVE_INFINITY;
      if (best < 0 || v > bestV) {
        best = i;
        bestV = v;
      }
    }
    int z = (int) (best / plane), r = (int) (best % plane);
    return new int[] {z, r / w.x(), r % w.x()};
  }

  /** The chosen component: a mask over the window, its voxel count, and whether it was clipped. */
  record Grown(boolean[] component, int size, double threshold, boolean clipped) {}

  /**
   * §7–§8. {@code volumeFace[axis][side]} says whether that face of the window is also a face of
   * the volume (so touching it is not a clip).
   */
  static Grown grow(Volume w, boolean[] selected, int zG, double t, boolean grow3d, long minSize, boolean[][] volumeFace) {
    int d = w.z(), h = w.y(), wd = w.x(), plane = h * wd;
    List<Integer> sizes = new ArrayList<>();
    int[] labels;
    if (grow3d) {
      boolean[] fg = new boolean[(int) w.samples()];
      for (int i = 0; i < fg.length; i++) fg[i] = w.get(i) >= t; // NaN compares false
      labels = components(fg, d, h, wd, sizes);
    } else {
      boolean[] fg = new boolean[plane];
      for (int p = 0; p < plane; p++) fg[p] = w.get((long) zG * plane + p) >= t;
      int[] pl = components(fg, 1, h, wd, sizes);
      labels = new int[(int) w.samples()];
      System.arraycopy(pl, 0, labels, zG * plane, plane);
    }
    // §7: candidates hold a voxel (z_g, y, x) with (y, x) selected; §8: the largest, ties to the lowest id
    int best = 0;
    for (int p = 0; p < plane; p++) {
      int id = labels[zG * plane + p];
      if (!selected[p] || id == 0) continue;
      if (best == 0 || sizes.get(id) > sizes.get(best) || (sizes.get(id).equals(sizes.get(best)) && id < best)) best = id;
    }
    if (best == 0) {
      int[] star = diagnostic(w, selected);
      throw new Refused("Nothing above the threshold on plane " + zG + ". The brightest voxel you enclosed is at z="
          + star[0] + " — scroll there, or lower Threshold ×.");
    }
    int size = sizes.get(best);
    if (size < minSize) throw new Refused("No component of at least " + minSize + " voxels.");
    if (size > MAX_COMPONENT) throw new Refused("Selection too permissive — try a tighter loop or a higher Threshold ×.");
    boolean[] comp = new boolean[labels.length];
    boolean clipped = false;
    for (int i = 0; i < labels.length; i++) {
      if (labels[i] != best) continue;
      comp[i] = true;
      int z = i / plane, r = i % plane, y = r / wd, x = r % wd;
      if ((z == 0 && !volumeFace[0][0]) || (z == d - 1 && !volumeFace[0][1])
          || (y == 0 && !volumeFace[1][0]) || (y == h - 1 && !volumeFace[1][1])
          || (x == 0 && !volumeFace[2][0]) || (x == wd - 1 && !volumeFace[2][1])) {
        clipped = true;
      }
    }
    return new Grown(comp, size, t, clipped);
  }

  /** §9's (256, 4) RGBA LUT: row 0 transparent, row k the palette entry (k − 1) % 8 at alpha 180. */
  static byte[] lut() {
    byte[] out = new byte[256 * 4];
    for (int k = 1; k < 256; k++) {
      int[] c = PALETTE[(k - 1) % PALETTE.length];
      out[4 * k] = (byte) c[0];
      out[4 * k + 1] = (byte) c[1];
      out[4 * k + 2] = (byte) c[2];
      out[4 * k + 3] = (byte) LABEL_ALPHA;
    }
    return out;
  }

  /** A bool array placed at an integer origin, ZYX. */
  record Placed(int[] origin, int[] shape, boolean[] mask) {}

  /** The bbox-tight part of a {@code (d, h, w)} mask, placed relative to {@code base}; null if empty. */
  static Placed tight(boolean[] mask, int d, int h, int w, int[] base) {
    int[] lo = {d, h, w}, hi = {-1, -1, -1};
    int plane = h * w;
    for (int i = 0; i < mask.length; i++) {
      if (!mask[i]) continue;
      int[] c = {i / plane, (i % plane) / w, i % w};
      for (int a = 0; a < 3; a++) {
        lo[a] = Math.min(lo[a], c[a]);
        hi[a] = Math.max(hi[a], c[a]);
      }
    }
    if (hi[0] < 0) return null;
    int[] shape = {hi[0] - lo[0] + 1, hi[1] - lo[1] + 1, hi[2] - lo[2] + 1};
    boolean[] out = new boolean[shape[0] * shape[1] * shape[2]];
    for (int z = 0; z < shape[0]; z++) {
      for (int y = 0; y < shape[1]; y++) {
        for (int x = 0; x < shape[2]; x++) {
          out[(z * shape[1] + y) * shape[2] + x] = mask[((lo[0] + z) * h + lo[1] + y) * w + lo[2] + x];
        }
      }
    }
    return new Placed(new int[] {base[0] + lo[0], base[1] + lo[1], base[2] + lo[2]}, shape, out);
  }

  /** The rendered label volume: bbox-tight u8 labels and their integer origin. */
  record Rendered(int[] origin, int[] shape, byte[] labels) {}

  /**
   * §9's accumulation: labels 1..255, each a component at an origin; the newer label wins voxels
   * an earlier one holds. The caller scopes it to one {@code (t, c, gen)}.
   */
  static final class LabelMap {
    private final List<Placed> entries = new ArrayList<>();

    int size() {
      return entries.size();
    }

    int nextId() {
      return entries.size() + 1;
    }

    int add(Placed p) {
      if (nextId() > MAX_LABELS) throw new Refused("At most " + MAX_LABELS + " labels — commit or clear first.");
      entries.add(p);
      return entries.size();
    }

    Rendered render() {
      if (entries.isEmpty()) throw new IllegalStateException("empty label map");
      int[] lo = {Integer.MAX_VALUE, Integer.MAX_VALUE, Integer.MAX_VALUE}, hi = {Integer.MIN_VALUE, Integer.MIN_VALUE, Integer.MIN_VALUE};
      for (Placed p : entries) {
        for (int a = 0; a < 3; a++) {
          lo[a] = Math.min(lo[a], p.origin()[a]);
          hi[a] = Math.max(hi[a], p.origin()[a] + p.shape()[a]);
        }
      }
      int d = hi[0] - lo[0], h = hi[1] - lo[1], w = hi[2] - lo[2];
      byte[] out = new byte[d * h * w];
      for (int k = 0; k < entries.size(); k++) {
        Placed p = entries.get(k);
        int[] s = p.shape();
        for (int z = 0; z < s[0]; z++) {
          for (int y = 0; y < s[1]; y++) {
            for (int x = 0; x < s[2]; x++) {
              if (!p.mask()[(z * s[1] + y) * s[2] + x]) continue;
              out[((p.origin()[0] - lo[0] + z) * h + p.origin()[1] - lo[1] + y) * w + p.origin()[2] - lo[2] + x] = (byte) (k + 1);
            }
          }
        }
      }
      // tight around the labelled voxels: a newer label may have covered an older one's edge
      boolean[] nz = new boolean[out.length];
      for (int i = 0; i < out.length; i++) nz[i] = out[i] != 0;
      Placed t = tight(nz, d, h, w, lo);
      int[] o = {t.origin()[0] - lo[0], t.origin()[1] - lo[1], t.origin()[2] - lo[2]};
      int[] s = t.shape();
      byte[] cut = new byte[s[0] * s[1] * s[2]];
      for (int z = 0; z < s[0]; z++) {
        for (int y = 0; y < s[1]; y++) {
          System.arraycopy(out, ((o[0] + z) * h + o[1] + y) * w + o[2], cut, (z * s[1] + y) * s[2], s[2]);
        }
      }
      return new Rendered(t.origin(), s, cut);
    }
  }
}
