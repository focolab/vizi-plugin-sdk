import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;

/**
 * The lasso (SPEC.md §2, §4, §5): the decimated live preview, the even-odd selection of voxel
 * columns, and the search window. Pure arithmetic: nothing here touches the protocol.
 */
final class Lasso {
  private Lasso() {}

  /** §2: at most this many points on the live lane. */
  static final int LIVE_MAX_POINTS = 512;

  /**
   * §2: the preview's indices into a path of {@code n} points: uniform index sampling, {@code k =
   * min(n, kMax)}, {@code rint(i * (n - 1) / (k - 1))} (half to even), deduplicated; {@code k = 1}
   * is the first point alone.
   */
  static int[] decimate(int n, int kMax) {
    if (n <= 0) return new int[0];
    int k = Math.min(n, kMax);
    if (k == 1) return new int[] {0};
    int[] out = new int[k];
    int m = 0;
    for (int i = 0; i < k; i++) {
      int j = (int) Math.rint((double) i * (n - 1) / (k - 1));
      if (m == 0 || out[m - 1] != j) out[m++] = j;
    }
    return Arrays.copyOf(out, m);
  }

  /**
   * §4: the columns {@code (y, x)} of a {@code (height, width)} plane whose centre {@code (y + 0.5,
   * x + 0.5)} is inside the closed polygon {@code ys, xs} by the even-odd rule, as a row-major
   * mask. The crossing-number test SPEC.md pins, in f64, evaluated a row at a time: for one row
   * the test toggles once per straddling edge whose crossing lies right of {@code px}, so a column
   * is inside exactly when an odd number of that row's crossings exceed it.
   */
  static boolean[] selectedColumns(double[] ys, double[] xs, int height, int width) {
    boolean[] mask = new boolean[height * width];
    int n = ys.length;
    if (n < 3 || height == 0 || width == 0) return mask;
    double[] cross = new double[n];
    for (int y = 0; y < height; y++) {
      double py = y + 0.5;
      int c = 0;
      for (int j = 0; j < n; j++) {
        double ay = ys[j], ax = xs[j];
        double by = ys[(j + 1) % n], bx = xs[(j + 1) % n];
        if ((ay > py) != (by > py)) cross[c++] = ax + (py - ay) * (bx - ax) / (by - ay);
      }
      if (c == 0) continue;
      Arrays.sort(cross, 0, c);
      // walk the columns left to right; `above` counts crossings strictly greater than px
      int k = 0; // crossings <= px
      for (int x = 0; x < width; x++) {
        double px = x + 0.5;
        while (k < c && cross[k] <= px) k++;
        if (((c - k) & 1) == 1) mask[y * width + x] = true;
      }
    }
    return mask;
  }

  /** The exclusive bounding box {@code {y0, x0, y1, x1}} of a row-major mask, or null if empty. */
  static int[] bbox2(boolean[] mask, int height, int width) {
    int y0 = height, x0 = width, y1 = -1, x1 = -1;
    for (int y = 0; y < height; y++) {
      for (int x = 0; x < width; x++) {
        if (!mask[y * width + x]) continue;
        y0 = Math.min(y0, y);
        y1 = Math.max(y1, y);
        x0 = Math.min(x0, x);
        x1 = Math.max(x1, x);
      }
    }
    return y1 < 0 ? null : new int[] {y0, x0, y1 + 1, x1 + 1};
  }

  /** A search window: {@code [lo, hi)} in ZYX. */
  record Window(int[] lo, int[] hi) {
    int depth() {
      return hi[0] - lo[0];
    }

    int height() {
      return hi[1] - lo[1];
    }

    int width() {
      return hi[2] - lo[2];
    }
  }

  /**
   * §5: the selection's bounding box expanded by {@code max(8, rint(0.25 * extent))} on each side,
   * clipped to the volume, all of Z.
   */
  static Window searchWindow(boolean[] selected, int depth, int height, int width) {
    int[] b = bbox2(selected, height, width);
    if (b == null) throw new IllegalArgumentException("no selection");
    int my = Math.max(8, (int) Math.rint(0.25 * (b[2] - b[0])));
    int mx = Math.max(8, (int) Math.rint(0.25 * (b[3] - b[1])));
    return new Window(
        new int[] {0, Math.max(0, b[0] - my), Math.max(0, b[1] - mx)},
        new int[] {depth, Math.min(height, b[2] + my), Math.min(width, b[3] + mx)});
  }

  /** The window's part of a full-plane selection, row-major {@code (h, w)}. */
  static boolean[] crop(boolean[] selected, int width, Window w) {
    boolean[] out = new boolean[w.height() * w.width()];
    for (int y = 0; y < w.height(); y++) {
      System.arraycopy(selected, (w.lo()[1] + y) * width + w.lo()[2], out, y * w.width(), w.width());
    }
    return out;
  }

  /** A path of (y, x) points, growable; the ys and xs of the polygon. */
  static final class Path {
    private final List<double[]> points = new ArrayList<>();

    void add(double y, double x) {
      points.add(new double[] {y, x});
    }

    int size() {
      return points.size();
    }

    double[] get(int i) {
      return points.get(i);
    }

    double[] ys() {
      double[] out = new double[points.size()];
      for (int i = 0; i < out.length; i++) out[i] = points.get(i)[0];
      return out;
    }

    double[] xs() {
      double[] out = new double[points.size()];
      for (int i = 0; i < out.length; i++) out[i] = points.get(i)[1];
      return out;
    }
  }
}
