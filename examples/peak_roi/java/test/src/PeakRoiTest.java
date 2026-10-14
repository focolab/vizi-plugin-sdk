import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.HashSet;
import java.util.Set;
import org.junit.jupiter.api.Test;
import pois_math.v1.RoiOuterClass.ElementRole;
import pois_math.v1.RoiOuterClass.OutOfSpan;
import pois_math.v1.RoiOuterClass.Roi;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/** SPEC §3–§5 offline, from independently written expectations. */
class PeakRoiTest {

  static final double EPS = 1e-12;

  static Volume f32(int z, int y, int x, float... v) {
    return Volume.f32(z, y, x, java.util.Arrays.copyOf(v, z * y * x));
  }

  // ---- the kernel ----

  @Test
  void kernelForSigmaOneHasRadiusThreeAndTheHandComputedTaps() {
    double[] k = Analysis.taps(1.0);
    assertEquals(7, k.length);
    // exp(-i²/2) for i = 0..3: 1, 0.6065306597, 0.1353352832, 0.0111089965; sum 2.5066272
    double sum = 1 + 2 * (0.6065306597126334 + 0.1353352832366127 + 0.011108996538242306);
    assertEquals(1 / sum, k[3], EPS);
    assertEquals(0.6065306597126334 / sum, k[2], EPS);
    assertEquals(0.011108996538242306 / sum, k[0], EPS);
    assertEquals(k[0], k[6], 0.0);
    double total = 0;
    for (double d : k) total += d;
    assertEquals(1.0, total, EPS);
  }

  @Test
  void radiusIsTheCeilingOfThreeSigma() {
    assertEquals(2 * 2 + 1, Analysis.taps(0.5).length); // ceil(1.5) = 2
    assertEquals(2 * 6 + 1, Analysis.taps(2.0).length); // 6
    assertEquals(2 * 7 + 1, Analysis.taps(2.1).length); // ceil(6.3) = 7
    assertArrayEquals(new double[] {1.0}, Analysis.taps(0.0));
  }

  // ---- edges, accumulation order, the non-finite rule ----

  @Test
  void aConstantVolumeSurvivesTheBlurBecauseEdgesClamp() {
    float[] v = new float[3 * 4 * 5];
    java.util.Arrays.fill(v, 7f);
    Analysis.Grid g = Analysis.blur(Analysis.finite(f32(3, 4, 5, v)), 2, 2, 2);
    for (double d : g.v()) assertEquals(7.0, d, 1e-12);
  }

  @Test
  void anImpulseAtTheEdgeFoldsTheClippedTapsBackOntoTheEdge() {
    // x-line [1, 0, 0, 0, 0] with σ = 0.5: taps k(-2..2); out[0] = (k-2 + k-1 + k0)·1
    Analysis.Grid g = Analysis.blurAxis(Analysis.finite(f32(1, 1, 5, 1, 0, 0, 0, 0)), 0.5, 2);
    double e1 = Math.exp(-2), e2 = Math.exp(-8); // exp(-i²/(2·0.25)) = exp(-2i²)
    double s = 1 + 2 * e1 + 2 * e2;
    assertEquals((1 + e1 + e2) / s, g.v()[0], EPS);
    assertEquals((e1 + e2) / s, g.v()[1], EPS); // taps at offsets -1 (the impulse) and -2 (clamped to it)
    assertEquals(e2 / s, g.v()[2], EPS);
    assertEquals(0.0, g.v()[3], 0.0);
  }

  @Test
  void zeroSigmaIsNoBlurAlongThatAxis() {
    Analysis.Grid g = Analysis.blur(Analysis.finite(f32(2, 1, 1, 1, 5)), 0, 0, 0);
    assertArrayEquals(new double[] {1, 5}, g.v());
  }

  @Test
  void nanAndMinusInfBecomeTheMinimumFiniteAndPlusInfStands() {
    Analysis.Grid g = Analysis.finite(f32(1, 1, 5, Float.NaN, 3, Float.NEGATIVE_INFINITY, 2, Float.POSITIVE_INFINITY));
    assertArrayEquals(new double[] {2, 3, 2, 2, Double.POSITIVE_INFINITY}, g.v());
    Volume inf = f32(1, 1, 5, Float.NaN, 3, Float.NEGATIVE_INFINITY, 2, Float.POSITIVE_INFINITY);
    assertArrayEquals(new int[] {0, 0, 4}, Analysis.peak(inf, 0, 0, 0));
    // blurred, +Inf spreads to every voxel within the radius, and the tie goes to the lowest index
    assertArrayEquals(new int[] {0, 0, 2}, Analysis.peak(inf, 1, 1, 1));
    assertThrows(IllegalArgumentException.class, () -> Analysis.finite(f32(1, 1, 2, Float.NaN, Float.NEGATIVE_INFINITY)));
    // a NaN next to the true peak neither wins nor lifts it
    assertArrayEquals(new int[] {0, 0, 1}, Analysis.peak(f32(1, 1, 4, 0, 10, Float.NaN, 0), 0, 0, 0));
  }

  // ---- the peak ----

  @Test
  void tiesGoToTheLowestRasterIndex() {
    assertArrayEquals(new int[] {0, 1, 0}, Analysis.argmax(new Analysis.Grid(2, 2, 2, new double[] {0, 0, 5, 5, 5, 0, 0, 0})));
    assertArrayEquals(new int[] {0, 0, 0}, Analysis.argmax(new Analysis.Grid(1, 1, 3, new double[] {1, 1, 1})));
  }

  @Test
  void aSyntheticGaussianBlobPeaksAtItsCentre() {
    int z = 9, y = 21, x = 23;
    int[] centre = {3, 12, 7};
    short[] s = new short[z * y * x];
    for (int k = 0; k < z; k++) {
      for (int j = 0; j < y; j++) {
        for (int i = 0; i < x; i++) {
          double d = Math.pow(k - centre[0], 2) / 4 + Math.pow(j - centre[1], 2) / 18 + Math.pow(i - centre[2], 2) / 18;
          s[(k * y + j) * x + i] = (short) (600 * Math.exp(-d) + 80);
        }
      }
    }
    assertArrayEquals(centre, Analysis.peak(Volume.u16(z, y, x, s), 4, 8, 8));
    // an off-centre bright voxel far from the blob does not win once blurred
    s[(8 * y + 20) * x + 22] = (short) 700;
    assertArrayEquals(centre, Analysis.peak(Volume.u16(z, y, x, s), 4, 8, 8));
    assertArrayEquals(new int[] {8, 20, 22}, Analysis.peak(Volume.u16(z, y, x, s), 0, 0, 0));
  }

  @Test
  void everyDtypeReadsLittleEndianAndUnsigned() {
    assertArrayEquals(new int[] {0, 0, 1}, Analysis.peak(Volume.u8(1, 1, 3, new byte[] {10, (byte) 200, 100}), 0, 0, 0));
    assertArrayEquals(new int[] {0, 0, 2}, Analysis.peak(Volume.u16(1, 1, 3, new short[] {10, 20, (short) 65000}), 0, 0, 0));
    assertArrayEquals(new int[] {0, 0, 0}, Analysis.peak(Volume.f64(1, 1, 2, new double[] {3, -1}), 0, 0, 0));
  }

  // ---- the ellipsoid, against positions written out by hand ----

  @Test
  void theEllipsoidHasFourControlsInOrderAndCompilesToTheEighteenPoints() {
    Roi r = Rois.ellipsoid(3.5, 12.5, 7.5, 4, 8, 2);
    assertEquals("ellipsoid", r.getTypeName());
    assertEquals(0, r.getRoot());
    assertEquals(4, r.getVerticesCount());
    double[][] controls = {{3.5, 12.5, 7.5}, {7.5, 12.5, 7.5}, {3.5, 20.5, 7.5}, {3.5, 12.5, 9.5}};
    for (int i = 0; i < 4; i++) {
      var p = r.getVertices(i).getPosition();
      assertArrayEquals(controls[i], new double[] {p.getZ(), p.getY(), p.getX()}, 0.0);
    }
    assertEquals(java.util.List.of(0, 1, 2, 3), r.getCompile().getControlsList());
    double[][] want = {
      {7.5, 12.5, 7.5}, {-0.5, 12.5, 7.5}, // ±Z tips
      {3.5, 20.5, 7.5}, {3.5, 4.5, 7.5}, // ±Y
      {3.5, 12.5, 9.5}, {3.5, 12.5, 5.5}, // ±X
      // ZY ring corners (+,+) (-,+) (-,-) (+,-)
      {7.5, 20.5, 7.5}, {-0.5, 20.5, 7.5}, {-0.5, 4.5, 7.5}, {7.5, 4.5, 7.5},
      // ZX
      {7.5, 12.5, 9.5}, {-0.5, 12.5, 9.5}, {-0.5, 12.5, 5.5}, {7.5, 12.5, 5.5},
      // YX
      {3.5, 20.5, 9.5}, {3.5, 4.5, 9.5}, {3.5, 4.5, 5.5}, {3.5, 20.5, 5.5},
    };
    // evaluated here, from the triplets, not by the SDK's own evaluator (whose answer must agree)
    double[][] got = new double[r.getCompile().getNumDisplay()][3];
    for (var t : r.getCompile().getTripletsList()) {
      var p = r.getVertices(r.getCompile().getControls(t.getControl())).getPosition();
      got[t.getDisplayVertex()][0] += t.getCoeff() * p.getZ();
      got[t.getDisplayVertex()][1] += t.getCoeff() * p.getY();
      got[t.getDisplayVertex()][2] += t.getCoeff() * p.getX();
    }
    assertEquals(18, got.length);
    assertArrayEquals(got, Rois.compileDisplay(r));
    // the order is the SDK's; the SET must be exactly these eighteen, and the six tips first
    Set<String> w = new HashSet<>(), g = new HashSet<>();
    for (double[] p : want) w.add(p[0] + "," + p[1] + "," + p[2]);
    for (double[] p : got) g.add(p[0] + "," + p[1] + "," + p[2]);
    assertEquals(w, g);
    for (int i = 0; i < 6; i++) assertArrayEquals(want[i], got[i], 0.0);

    // twelve quarter arcs, weight √2/2, each control the ring corner between its two tips
    assertEquals(12, r.getEdgesCount());
    for (var e : r.getEdgesList()) {
      assertEquals(Math.sqrt(0.5), e.getWeight(), 1e-15);
      assertEquals(ElementRole.ELEMENT_DRAWN, e.getRole());
      double[] a = got[e.getSource()], b = got[e.getTarget()], c = got[e.getControl()];
      assertTrue(e.getSource() < 6 && e.getTarget() < 6 && e.getControl() >= 6 && e.getControl() < 18);
      double[] centre = controls[0];
      for (int k = 0; k < 3; k++) assertEquals(a[k] + b[k] - centre[k], c[k], 1e-12, "corner = tipA + tipB − centre");
    }
    assertFalse(r.hasTStart());
  }

  @Test
  void theTrackedEllipsoidCarriesADensePoseFromFrameZero() {
    Roi r = Rois.ellipsoid(1.5, 2.5, 3.5, 1, 1, 1, new double[][] {{0, 0, 0}, {0, -2, 3}, {1, -4, 6}});
    assertEquals(0, r.getTStart());
    assertEquals(2, r.getTEnd());
    assertEquals(OutOfSpan.HOLD, r.getOutOfSpan());
    assertEquals(java.util.List.of(0.0, 0.0, 0.0, 0.0, -2.0, 3.0, 1.0, -4.0, 6.0), r.getPoseTranslationList());
  }

  // ---- the manifest ----

  @Test
  void theManifestIsSpecsAndTheCommittedFileMatchesTheCode() throws Exception {
    Manifest m = PeakRoi.plugin().manifest();
    assertEquals("org.example.peak_roi", m.id());
    String json = ManifestWriter.write(m);
    for (String needle : new String[] {"\"vizi\": \"1.0\"", "\"data_access\"", "\"contribute_rois\"", "\"menu\": null", "\"./peak-roi\""}) {
      assertTrue(json.contains(needle), needle + " in " + json);
    }
    assertEquals(5, json.split("\"menu\": null", -1).length - 1);
    Path dir = Path.of(System.getProperty("user.dir"));
    while (dir != null && !Files.exists(dir.resolve("vizi-plugin.json"))) dir = dir.getParent();
    if (dir != null && Files.exists(dir.resolve("peak-roi"))) {
      Manifest disk = ManifestParser.read(dir).manifest();
      assertEquals(java.util.List.of(), vizi.plugin.javasdk.manifest.ManifestDiff.differences(m, disk));
    }
  }
}
