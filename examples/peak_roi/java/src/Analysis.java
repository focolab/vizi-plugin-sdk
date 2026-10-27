import vizi.plugin.javasdk.Volume;

/**
 * SPEC.md §3 and §4: a separable Gaussian blur and the brightest voxel. No image-processing
 * library and nothing from the host: this runs under the unit tests with no session.
 */
public final class Analysis {
  private Analysis() {}

  /** A {@code (Z, Y, X)} array of doubles, C order. */
  public record Grid(int z, int y, int x, double[] v) {
    public Grid {
      if ((long) z * y * x != v.length) throw new IllegalArgumentException("a (" + z + ", " + y + ", " + x + ") grid of " + v.length);
    }

    public int index(int k, int j, int i) {
      return (k * y + j) * x + i;
    }
  }

  /**
   * The normalized 1-D kernel: radius {@code ceil(3σ)}, taps {@code exp(-i²/2σ²)} for {@code i ∈
   * [-r, r]}, summing to 1. {@code σ = 0} is the single-tap identity.
   */
  public static double[] taps(double sigma) {
    if (!(sigma > 0)) return new double[] {1.0};
    int r = (int) Math.ceil(3.0 * sigma);
    double[] k = new double[2 * r + 1];
    double sum = 0;
    for (int i = -r; i <= r; i++) {
      k[i + r] = Math.exp(-((double) i * i) / (2.0 * sigma * sigma));
      sum += k[i + r];
    }
    for (int i = 0; i < k.length; i++) k[i] /= sum;
    return k;
  }

  /**
   * The volume as f64, with every NaN and −Inf replaced by the minimum finite value (+Inf
   * stands). Throws when no voxel is finite.
   */
  public static Grid finite(Volume vol) {
    long n = vol.samples();
    if (n > Integer.MAX_VALUE - 8) {
      throw new IllegalArgumentException("a volume of " + n + " voxels is past what this plugin blurs in one array");
    }
    double[] v = new double[(int) n];
    double min = Double.POSITIVE_INFINITY;
    boolean bad = false;
    for (int i = 0; i < v.length; i++) {
      double s = vol.get(i);
      v[i] = s;
      if (Double.isFinite(s)) {
        if (s < min) min = s;
      } else if (s != Double.POSITIVE_INFINITY) {
        bad = true;
      }
    }
    if (min == Double.POSITIVE_INFINITY) throw new IllegalArgumentException("the volume has no finite voxel");
    if (bad) {
      for (int i = 0; i < v.length; i++) {
        if (Double.isNaN(v[i]) || v[i] == Double.NEGATIVE_INFINITY) v[i] = min;
      }
    }
    return new Grid(vol.z(), vol.y(), vol.x(), v);
  }

  /** Correlates {@code g} with the kernel along {@code axis} (0 = Z, 1 = Y, 2 = X), edges clamped. */
  public static Grid blurAxis(Grid g, double sigma, int axis) {
    double[] k = taps(sigma);
    if (k.length == 1) return g;
    int r = (k.length - 1) / 2;
    int[] dims = {g.z(), g.y(), g.x()};
    int len = dims[axis];
    int stride = axis == 0 ? g.y() * g.x() : axis == 1 ? g.x() : 1;
    double[] in = g.v();
    double[] out = new double[in.length];
    for (int k0 = 0; k0 < g.z(); k0++) {
      for (int j0 = 0; j0 < g.y(); j0++) {
        for (int i0 = 0; i0 < g.x(); i0++) {
          int at = g.index(k0, j0, i0);
          int pos = axis == 0 ? k0 : axis == 1 ? j0 : i0;
          int base = at - pos * stride;
          double acc = 0;
          for (int t = 0; t < k.length; t++) {
            int p = Math.min(len - 1, Math.max(0, pos + t - r));
            acc += k[t] * in[base + p * stride];
          }
          out[at] = acc;
        }
      }
    }
    return new Grid(g.z(), g.y(), g.x(), out);
  }

  /** Z, then Y, then X, each with its own σ; f64 throughout. */
  public static Grid blur(Grid g, double sz, double sy, double sx) {
    return blurAxis(blurAxis(blurAxis(g, sz, 0), sy, 1), sx, 2);
  }

  /** The maximum's {@code (z, y, x)}; a tie goes to the lowest index in raster order. */
  public static int[] argmax(Grid g) {
    double[] v = g.v();
    int best = 0;
    for (int i = 1; i < v.length; i++) {
      if (v[i] > v[best] || (Double.isNaN(v[i]) && !Double.isNaN(v[best]))) best = i;
    }
    int x = best % g.x();
    int y = (best / g.x()) % g.y();
    int z = best / (g.x() * g.y());
    return new int[] {z, y, x};
  }

  /** §3–§4 end to end: σ = extent / 2 per axis, then the peak's index. */
  public static int[] peak(Volume vol, double ez, double ey, double ex) {
    if (vol.samples() == 0) throw new IllegalArgumentException("the volume is empty");
    return argmax(blur(finite(vol), ez / 2, ey / 2, ex / 2));
  }
}
