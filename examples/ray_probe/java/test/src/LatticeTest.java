import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Random;
import java.util.Set;
import org.junit.jupiter.api.Test;

/**
 * The level maps, the crop, the traversal, the column and the block, offline, against expectations
 * written independently. The traversal's ties are
 * checked against a SLAB TEST over the segment's box, which is not a traversal.
 */
class LatticeTest {

  static Lattice.Cell c(int z, int y, int x) {
    return new Lattice.Cell(z, y, x);
  }

  // ---- the level maps: control_plane_protocol.md §8.11's worked numbers ----

  @Test
  void levelMapsOnTheWorkedNumbers() {
    // a crop of pixels 25 through 1519 is {24.5, 1519.5}; at level 1 (factor 2) its centers are 12 ..= 759
    assertEquals(12.0, Lattice.edgeAtLevel(24.5, 2), 0);
    assertEquals(759.5, Lattice.edgeAtLevel(1519.5, 2), 0);
    Lattice.Range r = Lattice.cropAtLevel(new double[] {24.5, 1519.5, 24.5, 1519.5, 24.5, 1519.5}, new double[] {2, 2, 2},
        new int[] {800, 800, 800});
    assertArrayEquals(new int[] {12, 12, 12}, r.lo());
    assertArrayEquals(new int[] {759, 759, 759}, r.hi());
    // the proportional map (e/s) would give 12.25 -> 13: the bug the offset term exists to stop
    assertEquals(13, (int) Math.ceil(24.5 / 2));
    // level-L center j is level-0 j·s + (s − 1)/2
    assertEquals(0.5, Lattice.level0Of(0, 2), 0);
    assertEquals(1.5, Lattice.level0Of(0, 4), 0);
    assertEquals(41.5, Lattice.level0Of(10, 4), 0);
    assertEquals(-0.5, Lattice.level0Of(-0.5, 4), 0); // an edge maps to an edge
    // the two maps are inverse on edges
    for (double e : new double[] {-0.5, 24.5, 63.5, 1519.5}) {
      for (double s : new double[] {1, 2, 3, 4, 5}) assertEquals(e, Lattice.level0Of(Lattice.edgeAtLevel(e, s), s), 1e-12);
    }
  }

  @Test
  void aFactorThatIsNotAPowerOfTwo() {
    // factor 3 (a host whose level 1 folds by 3): level-1 voxel 2 covers level-0 6, 7, 8, center 7
    assertEquals(7.0, Lattice.level0Of(2, 3), 0);
    assertEquals(2.0, Lattice.edgeAtLevel(7, 3), 0); // a level-0 center on the level-1 center
    // pixels 3 ..= 13 at factor 3: edges {2.5, 13.5} -> {2/3·... } = {0.5, 4.1667}: centers 1 ..= 4
    Lattice.Range r = Lattice.cropAtLevel(new double[] {2.5, 13.5, 2.5, 13.5, 2.5, 13.5}, new double[] {3, 3, 3}, new int[] {5, 5, 5});
    assertArrayEquals(new int[] {1, 1, 1}, r.lo());
    assertArrayEquals(new int[] {4, 4, 4}, r.hi());
    // a thin axis keeps the factor of its last fold (SPEC.md §"Levels"): n0 = 1, factor 1 at every level
    assertArrayEquals(new int[] {1, 12, 16}, Lattice.levelShape(new int[] {1, 48, 64}, new double[] {1, 4, 4}));
    // the odd trailing sample discarded: 31 at factor 2 is 15
    assertArrayEquals(new int[] {15, 15, 15}, Lattice.levelShape(new int[] {31, 31, 31}, new double[] {2, 2, 2}));
  }

  @Test
  void cropClampsToTheShapeAndCanBeEmpty() {
    Lattice.Range r = Lattice.cropAtLevel(new double[] {-0.5, 31.5, -0.5, 47.5, -0.5, 63.5}, new double[] {4, 4, 4}, new int[] {8, 12, 16});
    assertArrayEquals(new int[] {0, 0, 0}, r.lo());
    assertArrayEquals(new int[] {7, 11, 15}, r.hi());
    // an integral edge at the level is a hit: {4.5, 5.5} is {0.75, 1.0} at factor 4, center 1
    assertEquals(1, Lattice.cropAtLevel(new double[] {4.5, 5.5, -0.5, 47.5, -0.5, 63.5}, new double[] {4, 4, 4}, new int[] {8, 12, 16}).lo()[0]);
    assertEquals(1, Lattice.cropAtLevel(new double[] {4.5, 5.5, -0.5, 47.5, -0.5, 63.5}, new double[] {4, 4, 4}, new int[] {8, 12, 16}).hi()[0]);
    // a crop narrower than one level-2 voxel, between two centers: {6.5, 8.5} is {1.25, 1.75}, no center
    assertNull(Lattice.cropAtLevel(new double[] {6.5, 8.5, -0.5, 47.5, -0.5, 63.5}, new double[] {4, 4, 4}, new int[] {8, 12, 16}));
  }

  // ---- the traversal ----

  @Test
  void axisAligned() {
    List<Lattice.Cell> got = Lattice.traverse(new double[] {18, 22, -0.3}, new double[] {18, 22, 5.2});
    List<Lattice.Cell> want = new ArrayList<>();
    for (int x = 0; x <= 5; x++) want.add(c(18, 22, x));
    assertEquals(want, got);
    List<Lattice.Cell> back = Lattice.traverse(new double[] {18, 22, 5.2}, new double[] {18, 22, -0.3});
    assertEquals(want.reversed(), back);
  }

  @Test
  void oneCell() {
    assertEquals(List.of(c(3, 4, 5)), Lattice.traverse(new double[] {2.6, 4.1, 5.4}, new double[] {3.3, 3.8, 4.7}));
    assertEquals(List.of(c(3, 4, 5)), Lattice.traverse(new double[] {3, 4, 5}, new double[] {3, 4, 5}));
  }

  @Test
  void edgeCrossingIsFourCells() {
    // (0,0,0) -> (1,1,0) crosses the edge at (0.5, 0.5): (0,1,0) and (1,0,0) touch it too
    List<Lattice.Cell> got = Lattice.traverse(new double[] {0, 0, 0}, new double[] {1, 1, 0});
    assertEquals(Set.of(c(0, 0, 0), c(0, 1, 0), c(1, 0, 0), c(1, 1, 0)), new HashSet<>(got));
    assertEquals(4, got.size());
    assertEquals(c(0, 0, 0), got.get(0));
    assertEquals(c(1, 1, 0), got.get(3));
    List<Lattice.Cell> back = Lattice.traverse(new double[] {1, 1, 0}, new double[] {0, 0, 0});
    assertEquals(new HashSet<>(got), new HashSet<>(back));
    assertEquals(c(1, 1, 0), back.get(0));
  }

  @Test
  void cornerCrossingIsEightCells() {
    List<Lattice.Cell> got = Lattice.traverse(new double[] {0, 0, 0}, new double[] {1, 1, 1});
    assertEquals(8, got.size());
    assertEquals(c(0, 0, 0), got.get(0));
    assertEquals(c(1, 1, 1), got.get(7));
    assertSlab(new double[] {0, 0, 0}, new double[] {1, 1, 1});
    assertSlab(new double[] {1, 1, 1}, new double[] {0, 0, 0});
  }

  @Test
  void endpointsOnBoundaries() {
    // a face touched at the end: the end lies on x = 2.5, so cell x = 3 is met too
    List<Lattice.Cell> got = Lattice.traverse(new double[] {0, 0, 0}, new double[] {0, 0, 2.5});
    assertEquals(List.of(c(0, 0, 0), c(0, 0, 1), c(0, 0, 2), c(0, 0, 3)), got);
    // a start on a corner belongs to all eight cells around it
    got = Lattice.traverse(new double[] {0.5, 0.5, 0.5}, new double[] {0.9, 0.8, 0.7});
    assertEquals(8, new HashSet<>(got).size());
    assertEquals(c(1, 1, 1), got.get(got.size() - 1));
    // a segment lying in a boundary plane walks both sides
    got = Lattice.traverse(new double[] {0.5, 0, 0}, new double[] {0.5, 0, 2});
    assertEquals(Set.of(c(0, 0, 0), c(1, 0, 0), c(0, 0, 1), c(1, 0, 1), c(0, 0, 2), c(1, 0, 2)), new HashSet<>(got));
    for (double[][] seg : new double[][][] {
        {{0, 0, 0}, {0, 0, 2.5}}, {{0.5, 0.5, 0.5}, {0.9, 0.8, 0.7}}, {{0.5, 0, 0}, {0.5, 0, 2}}, {{0.5, -0.5, 0}, {0.5, -0.5, 3}},
        {{2.5, 1.5, 0.5}, {-1.5, -0.5, 0.5}}}) {
      assertSlab(seg[0], seg[1]);
      assertSlab(seg[1], seg[0]);
    }
  }

  /**
   * Segments with endpoints on a half-voxel grid are almost all ties — faces, edges, corners,
   * planes — and each one is checked against the slab test, both directions.
   */
  @Test
  void tieRichSegmentsAgainstTheSlabTest() {
    Random rnd = new Random(20260924);
    for (int n = 0; n < 4000; n++) {
      double[] a = new double[3];
      double[] b = new double[3];
      for (int k = 0; k < 3; k++) {
        a[k] = (rnd.nextInt(13) - 6) * 0.5;
        b[k] = (rnd.nextInt(13) - 6) * 0.5;
      }
      assertSlab(a, b);
    }
    // and ties exact in real arithmetic but not in floating point: tenths (not representable),
    // moving the same whole distance on every axis, so equal fractional parts cross together
    for (int n = 0; n < 4000; n++) {
      double[] a = new double[3];
      double[] b = new double[3];
      int m = rnd.nextInt(9) + 1;
      for (int k = 0; k < 3; k++) {
        a[k] = (rnd.nextInt(61) - 30) / 10.0;
        b[k] = a[k] + (rnd.nextInt(3) - 1) * m;
      }
      assertSlab(a, b);
    }
  }

  @Test
  void obliqueAgainstDenseSamplingAndSlab() {
    Random rnd = new Random(7);
    for (int n = 0; n < 300; n++) {
      double[] a = new double[3];
      double[] b = new double[3];
      for (int k = 0; k < 3; k++) {
        a[k] = rnd.nextDouble() * 40 - 5;
        b[k] = rnd.nextDouble() * 40 - 5;
      }
      List<Lattice.Cell> got = Lattice.traverse(a, b);
      Set<Lattice.Cell> dense = new HashSet<>();
      for (int i = 0; i <= 20000; i++) {
        double u = i / 20000.0;
        dense.add(Lattice.voxel(a[0] + u * (b[0] - a[0]), a[1] + u * (b[1] - a[1]), a[2] + u * (b[2] - a[2])));
      }
      assertTrue(new HashSet<>(got).containsAll(dense), "dense sampling found a cell the traversal missed");
      assertSlab(a, b);
    }
  }

  @Test
  void clippedByTheCropAtALevel() {
    // at level 2 (factor 4) the crop of level-0 pixels 8 ..= 23 on x is {7.5, 23.5} -> {1.625, 5.625}: centers 2 ..= 5
    double[] s = {4, 4, 4};
    Lattice.Range r = Lattice.cropAtLevel(new double[] {-0.5, 31.5, -0.5, 47.5, 7.5, 23.5}, s, new int[] {8, 12, 16});
    List<Lattice.Cell> cells = Lattice.clip(Lattice.traverse(new double[] {4, 5, -0.5}, new double[] {4, 5, 15.5}), r);
    List<Lattice.Cell> want = new ArrayList<>();
    for (int x = 2; x <= 5; x++) want.add(c(4, 5, x));
    assertEquals(want, cells);
    Lattice.Box box = Lattice.aabb(cells);
    assertArrayEquals(new int[] {4, 5, 2}, box.lo());
    assertArrayEquals(new int[] {5, 6, 6}, box.hi());
  }

  // ---- the column and the block ----

  @Test
  void column() {
    // COLUMN_YX at (y, x) = (22.4, 39.6) over [0, 4): z = 0 .. 3 at (22, 40)
    assertEquals(List.of(c(0, 22, 40), c(1, 22, 40), c(2, 22, 40), c(3, 22, 40)),
        Lattice.column(0, new double[] {0, 22.4, 39.6}, 0, 4));
    // the half pixel of slack: [0.5, 3.5) holds centers 1, 2, 3
    assertEquals(List.of(c(22, 1, 40), c(22, 2, 40), c(22, 3, 40)), Lattice.column(1, new double[] {22, 0, 40}, 0.5, 3.5));
  }

  @Test
  void blockOfACoarseVoxel() {
    Lattice.Box b2 = Lattice.blockOf(c(4, 5, 10), new double[] {2, 2, 2}, new int[] {32, 48, 64});
    assertArrayEquals(new int[] {8, 10, 20}, b2.lo());
    assertArrayEquals(new int[] {10, 12, 22}, b2.hi());
    Lattice.Box b4 = Lattice.blockOf(c(4, 5, 10), new double[] {4, 4, 4}, new int[] {32, 48, 64});
    assertArrayEquals(new int[] {16, 20, 40}, b4.lo());
    assertArrayEquals(new int[] {20, 24, 44}, b4.hi());
    // clamped at the high face: extent 31 at factor 4, voxel 7 covers 28, 29, 30 only
    Lattice.Box edge = Lattice.blockOf(c(7, 0, 0), new double[] {4, 4, 4}, new int[] {31, 48, 64});
    assertArrayEquals(new int[] {28, 0, 0}, edge.lo());
    assertArrayEquals(new int[] {31, 4, 4}, edge.hi());
  }

  // ---- the slab test ----

  /** The parameter at which the closed segment first meets the closed cube of {@code cell}, or NaN. */
  static double slab(double[] a, double[] b, Lattice.Cell cell) {
    double lo = 0, hi = 1;
    for (int k = 0; k < 3; k++) {
      double c0 = cell.get(k) - 0.5, c1 = cell.get(k) + 0.5, d = b[k] - a[k];
      if (d == 0) {
        if (a[k] < c0 - 1e-12 || a[k] > c1 + 1e-12) return Double.NaN;
        continue;
      }
      double t0 = (c0 - a[k]) / d, t1 = (c1 - a[k]) / d;
      lo = Math.max(lo, Math.min(t0, t1));
      hi = Math.min(hi, Math.max(t0, t1));
    }
    return lo <= hi + 1e-12 ? lo : Double.NaN;
  }

  /**
   * The traversal equals the set of cells in the segment's box whose closed cube the slab test
   * says the closed segment meets, and lists them in non-decreasing order of first contact.
   */
  static void assertSlab(double[] a, double[] b) {
    Set<Lattice.Cell> want = new HashSet<>();
    int[] lo = new int[3];
    int[] hi = new int[3];
    for (int k = 0; k < 3; k++) {
      lo[k] = (int) Math.floor(Math.min(a[k], b[k])) - 1;
      hi[k] = (int) Math.ceil(Math.max(a[k], b[k])) + 1;
    }
    for (int z = lo[0]; z <= hi[0]; z++) {
      for (int y = lo[1]; y <= hi[1]; y++) {
        for (int x = lo[2]; x <= hi[2]; x++) if (!Double.isNaN(slab(a, b, c(z, y, x)))) want.add(c(z, y, x));
      }
    }
    List<Lattice.Cell> got = Lattice.traverse(a, b);
    String where = java.util.Arrays.toString(a) + " -> " + java.util.Arrays.toString(b);
    assertEquals(got.size(), new HashSet<>(got).size(), "a cell twice: " + where);
    assertEquals(want, new HashSet<>(got), where);
    double prev = -1;
    for (Lattice.Cell cell : got) {
      double t = slab(a, b, cell);
      assertTrue(t >= prev - 1e-9, "out of order at " + cell + ": " + where + " " + got);
      prev = Math.max(prev, t);
    }
  }
}
