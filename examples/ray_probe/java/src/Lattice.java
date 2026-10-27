import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import vizi.plugin.javasdk.PaneContext;

/**
 * The arithmetic of SPEC.md §"Levels" and §"The tool" 3: the two exact level maps of
 * control_plane_protocol.md §8.11, the crop as included centers, and the voxels a segment passes
 * through. Pure: nothing here knows about vizi. A coordinate is a voxel CENTRE in the index space
 * of its level, so cell {@code k} spans {@code [k − 0.5, k + 0.5]}.
 */
final class Lattice {
  private Lattice() {}

  /** A voxel index (z, y, x) at some level. */
  record Cell(int z, int y, int x) {
    int get(int axis) {
      return switch (axis) {
        case 0 -> z;
        case 1 -> y;
        default -> x;
      };
    }

    static Cell of(int[] v) {
      return new Cell(v[0], v[1], v[2]);
    }
  }

  /** Inclusive per-axis index ranges: {@code lo[a] ..= hi[a]}. */
  record Range(int[] lo, int[] hi) {
    boolean contains(Cell c) {
      for (int a = 0; a < 3; a++) if (c.get(a) < lo[a] || c.get(a) > hi[a]) return false;
      return true;
    }
  }

  /** A box of cells: {@code lo} inclusive, {@code hi} exclusive. */
  record Box(int[] lo, int[] hi) {
    long voxels() {
      long n = 1;
      for (int a = 0; a < 3; a++) n *= Math.max(0, hi[a] - lo[a]);
      return n;
    }
  }

  // ---- the two level maps (§8.11) ----

  // Both maps are the SDK's (PaneContext's static helpers); they are named here so the rest of
  // the plugin, and the tests against §8.11's worked numbers, go through one place.

  /** A level-L coordinate as a level-0 lattice coordinate: {@code (c + 0.5)·s − 0.5}. */
  static double level0Of(double c, double s) {
    return PaneContext.toLevel0(c, s);
  }

  /** A level-0 EDGE at level L: {@code (e + 0.5)/s − 0.5}. Affine, never proportional. */
  static double edgeAtLevel(double e, double s) {
    return PaneContext.edgeAtLevel(e, s);
  }

  /** The level's shape: vizi's pyramid discards an odd trailing sample, so {@code ⌊n/s⌋}, at least 1. */
  static int[] levelShape(int[] extent0, double[] s) {
    int[] out = new int[3];
    for (int a = 0; a < 3; a++) out[a] = Math.max(1, (int) Math.floor(extent0[a] / s[a]));
    return out;
  }

  /**
   * The crop — level-0 edges {@code (z0, z1, y0, y1, x0, x1)} — as included centers at a level,
   * clamped to the level's shape: converted to the level FIRST, then {@code ceil(lo) ..= floor(hi)}.
   * Null when an axis holds no center.
   */
  static Range cropAtLevel(double[] edges0, double[] s, int[] shape) {
    int[] lo = new int[3];
    int[] hi = new int[3];
    for (int a = 0; a < 3; a++) {
      double e0 = edgeAtLevel(edges0[2 * a], s[a]);
      double e1 = edgeAtLevel(edges0[2 * a + 1], s[a]);
      lo[a] = Math.max((int) Math.ceil(e0), 0);
      hi[a] = Math.min((int) Math.floor(e1), shape[a] - 1);
      if (lo[a] > hi[a]) return null;
    }
    return new Range(lo, hi);
  }

  // ---- the traversal ----

  /**
   * How close two crossing parameters (in units of the segment) may be and still be one event,
   * and how close a coordinate may sit to a cell boundary and still be on it.
   */
  static final double EPS = 1e-9;

  private static boolean onBoundary(double v) {
    double u = v + 0.5;
    return Math.abs(u - Math.rint(u)) <= EPS;
  }

  /**
   * Every cell whose CLOSED cube the CLOSED segment {@code p0 → p1} meets, in order of the
   * parameter at which each is first met (a supercover Amanatides–Woo). An endpoint on boundaries
   * belongs to every cell around it; a simultaneous crossing of several boundaries yields every
   * cell reached by stepping any subset of the tied axes; a segment lying in a boundary plane
   * belongs to the cells on both sides. The crossing parameter is recomputed from the cell at each
   * step, never accumulated.
   */
  static List<Cell> traverse(double[] p0, double[] p1) {
    double[] d = new double[3];
    int[] cell = new int[3];
    int[] step = new int[3];
    List<Integer> behind = new ArrayList<>(); // boundary axes the segment leaves: the cell behind touches p0 too
    List<Integer> both = new ArrayList<>(); // boundary axes the segment never leaves: both sides, all along
    for (int a = 0; a < 3; a++) {
      d[a] = p1[a] - p0[a];
      boolean on = onBoundary(p0[a]);
      cell[a] = on ? (int) Math.rint(p0[a] + 0.5) : (int) Math.floor(p0[a] + 0.5); // on: the upper cell
      step[a] = d[a] > 0 ? 1 : d[a] < 0 ? -1 : 0;
      if (on && step[a] < 0) cell[a] -= 1; // heading down from a boundary: the lower cell is entered
      if (on) (step[a] == 0 ? both : behind).add(a);
    }
    Set<Cell> seen = new HashSet<>();
    List<Cell> out = new ArrayList<>();
    // the cells around p0 behind it, then the start cell
    for (int mask = (1 << behind.size()) - 1; mask > 0; mask--) {
      int[] c = cell.clone();
      for (int i = 0; i < behind.size(); i++) if ((mask & (1 << i)) != 0) c[behind.get(i)] -= step[behind.get(i)];
      emit(Cell.of(c), seen, out);
    }
    emit(Cell.of(cell), seen, out);
    double[] cross = new double[3];
    while (true) {
      double t = Double.POSITIVE_INFINITY;
      for (int a = 0; a < 3; a++) {
        cross[a] = step[a] == 0 ? Double.POSITIVE_INFINITY : ((cell[a] + 0.5 * step[a]) - p0[a]) / d[a];
        t = Math.min(t, cross[a]);
      }
      if (!(t <= 1.0 + EPS)) break;
      List<Integer> tied = new ArrayList<>();
      for (int a = 0; a < 3; a++) if (step[a] != 0 && cross[a] <= t + EPS) tied.add(a);
      // every proper non-empty subset of the tied axes (the cells around the edge or corner), then all
      int full = (1 << tied.size()) - 1;
      for (int bits = 1; bits <= full; bits++) {
        for (int mask = 1; mask < full; mask++) {
          if (Integer.bitCount(mask) != bits) continue;
          int[] c = cell.clone();
          for (int i = 0; i < tied.size(); i++) if ((mask & (1 << i)) != 0) c[tied.get(i)] += step[tied.get(i)];
          emit(Cell.of(c), seen, out);
        }
      }
      for (int a : tied) cell[a] += step[a];
      emit(Cell.of(cell), seen, out);
    }
    if (both.isEmpty()) return out;
    Set<Cell> seen2 = new HashSet<>();
    List<Cell> expanded = new ArrayList<>();
    for (Cell c : out) {
      for (int mask = (1 << both.size()) - 1; mask >= 0; mask--) {
        int[] v = {c.z(), c.y(), c.x()};
        for (int i = 0; i < both.size(); i++) if ((mask & (1 << i)) != 0) v[both.get(i)] -= 1;
        emit(Cell.of(v), seen2, expanded);
      }
    }
    return expanded;
  }

  private static void emit(Cell c, Set<Cell> seen, List<Cell> out) {
    if (seen.add(c)) out.add(c);
  }

  /** The voxel containing a point: {@code ⌊v + 0.5⌋} per axis. */
  static Cell voxel(double z, double y, double x) {
    return new Cell((int) Math.floor(z + 0.5), (int) Math.floor(y + 0.5), (int) Math.floor(x + 0.5));
  }

  /**
   * A projection column: the two fixed coordinates rounded to their voxel, the free axis over
   * {@code [lo, hi)} ascending ({@code ceil(lo) .. ceil(hi) − 1}). {@code fixed} holds a coordinate
   * per axis; the free axis's entry is ignored.
   */
  static List<Cell> column(int free, double[] fixed, double lo, double hi) {
    List<Cell> out = new ArrayList<>();
    int first = (int) Math.ceil(lo);
    int last = (int) Math.ceil(hi) - 1;
    for (int k = first; k <= last; k++) {
      int[] c = new int[3];
      for (int a = 0; a < 3; a++) c[a] = a == free ? k : (int) Math.floor(fixed[a] + 0.5);
      out.add(Cell.of(c));
    }
    return out;
  }

  /** The cells inside {@code r}, order kept. */
  static List<Cell> clip(List<Cell> cells, Range r) {
    List<Cell> out = new ArrayList<>();
    for (Cell c : cells) if (r.contains(c)) out.add(c);
    return out;
  }

  /** The bounding box of a non-empty cell list. */
  static Box aabb(List<Cell> cells) {
    int[] lo = {Integer.MAX_VALUE, Integer.MAX_VALUE, Integer.MAX_VALUE};
    int[] hi = {Integer.MIN_VALUE, Integer.MIN_VALUE, Integer.MIN_VALUE};
    for (Cell c : cells) {
      for (int a = 0; a < 3; a++) {
        lo[a] = Math.min(lo[a], c.get(a));
        hi[a] = Math.max(hi[a], c.get(a) + 1);
      }
    }
    return new Box(lo, hi);
  }

  /**
   * The level-0 block a level-L cell covers, {@code [j·s, j·s + s)} per axis with {@code s} the
   * sampling factor, clamped to the level-0 extent at the high face.
   */
  static Box blockOf(Cell cell, double[] s, int[] extent0) {
    int[] lo = new int[3];
    int[] hi = new int[3];
    for (int a = 0; a < 3; a++) {
      // the integers k with j·s <= k < j·s + s (for an integral factor, j·s .. j·s + s − 1)
      lo[a] = (int) Math.ceil(cell.get(a) * s[a]);
      hi[a] = Math.min((int) Math.ceil(cell.get(a) * s[a] + s[a]), extent0[a]);
    }
    return new Box(lo, hi);
  }
}
