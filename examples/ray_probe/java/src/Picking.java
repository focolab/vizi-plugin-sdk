import java.util.ArrayList;
import java.util.List;
import vizi.plugin.javasdk.Volume;

/**
 * The pick rules of SPEC.md §"The tool" 3 and 4: the first maximum along the ordered set, the
 * block maximum with peak-ROI's tie rule, and the distances a profile is plotted against.
 */
final class Picking {
  private Picking() {}

  /** An index into the ordered set and its value. */
  record Best(int index, double value) {}

  /**
   * The greatest value, ties to the FIRST (nearest the camera, or lowest along a column); NaN
   * skipped, ±inf compared as numbers. Null when every value is NaN.
   */
  static Best firstMax(double[] values) {
    Best best = null;
    for (int i = 0; i < values.length; i++) {
      double v = values[i];
      if (Double.isNaN(v)) continue;
      if (best == null || v > best.value()) best = new Best(i, v);
    }
    return best;
  }

  /** A level-0 voxel and its value. */
  record Peak(int z, int y, int x, double value) {}

  /**
   * The greatest value of a block read at {@code origin}, ties to the smallest z, then y, then x
   * (raster order, first wins); NaN skipped. Null when the block is empty or all NaN.
   */
  static Peak blockMax(Volume block, int[] origin) {
    Peak best = null;
    for (int k = 0; k < block.z(); k++) {
      for (int j = 0; j < block.y(); j++) {
        for (int i = 0; i < block.x(); i++) {
          double v = block.get(block.index(k, j, i));
          if (Double.isNaN(v)) continue;
          if (best == null || v > best.value()) best = new Peak(origin[0] + k, origin[1] + j, origin[2] + i, v);
        }
      }
    }
    return best;
  }

  /**
   * Cumulative distance from the first cell's center along the ordered set: physical when {@code
   * spacing} is known ({@code √Σ(Δk·u_k)²} between consecutive centers), else in voxels of the
   * set's level ({@code spacing == null}).
   */
  static double[] distances(List<Lattice.Cell> cells, double[] spacing) {
    double[] out = new double[cells.size()];
    for (int n = 1; n < cells.size(); n++) {
      Lattice.Cell a = cells.get(n - 1);
      Lattice.Cell b = cells.get(n);
      double sum = 0;
      for (int k = 0; k < 3; k++) {
        double dk = (b.get(k) - a.get(k)) * (spacing == null ? 1.0 : spacing[k]);
        sum += dk * dk;
      }
      out[n] = out[n - 1] + Math.sqrt(sum);
    }
    return out;
  }

  /** Which of {@code n} samples to PLOT (never to search): every ⌈n/limit⌉-th, the last kept. */
  static List<Integer> sampleIndices(int n, int limit) {
    List<Integer> out = new ArrayList<>();
    if (n <= limit) {
      for (int i = 0; i < n; i++) out.add(i);
      return out;
    }
    int stride = (n + limit - 1) / limit;
    for (int i = 0; i < n; i += stride) out.add(i);
    if (out.get(out.size() - 1) != n - 1) out.add(n - 1);
    return out;
  }
}
