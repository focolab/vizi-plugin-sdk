import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import pois_math.v1.RoiOuterClass.LinearBinding;
import pois_math.v1.RoiOuterClass.Roi;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/** The offline checks: the geometry, the bindings, the volume rule and the manifest. */
class TetraTest {
  static final double EPS = 1e-9;

  static double[][] rest() {
    double[][] p = new double[5][];
    for (int i = 0; i < 4; i++) p[i] = Tetra.CORNERS[i].clone();
    p[4] = Tetra.CENTRE.clone();
    return p;
  }

  @Test
  void theCentreIsTheCentroid() {
    assertArrayEquals(new double[] {16, 19.5, 17}, Tetra.CENTRE, EPS);
  }

  /** §2: the 12 bindings against M = (v_j − c)·u_iᵀ / r_i, in their order, computed here independently. */
  @Test
  void theScaleBindings() {
    Roi scale = Tetra.region(Tetra.REGIONS.get(1));
    List<LinearBinding> bs = scale.getBindingsList();
    assertEquals(12, bs.size());
    int k = 0;
    double[] c = Tetra.CENTRE;
    for (int i = 0; i < 4; i++) {
      double[] d = Tetra.sub(Tetra.CORNERS[i], c);
      double r = Math.sqrt(Tetra.dot(d, d));
      for (int j = 0; j < 4; j++) {
        if (j == i) continue;
        LinearBinding b = bs.get(k++);
        assertEquals(i, b.getTarget());
        assertEquals(j, b.getMoved());
        double[] vj = Tetra.sub(Tetra.CORNERS[j], c);
        for (int row = 0; row < 3; row++) {
          for (int col = 0; col < 3; col++) assertEquals(vj[row] * (d[col] / r) / r, b.getM(3 * row + col), EPS, i + "," + j);
        }
      }
    }
    // and they scale exactly: δ = t·u_i moves j by (t / r_i)(v_j − c)
    double t = 2.5;
    for (LinearBinding b : bs) {
      double[] d = Tetra.sub(Tetra.CORNERS[b.getTarget()], c);
      double r = Math.sqrt(Tetra.dot(d, d));
      double[] delta = {t * d[0] / r, t * d[1] / r, t * d[2] / r};
      double[] vj = Tetra.sub(Tetra.CORNERS[b.getMoved()], c);
      for (int a = 0; a < 3; a++) {
        double m = b.getM(3 * a) * delta[0] + b.getM(3 * a + 1) * delta[1] + b.getM(3 * a + 2) * delta[2];
        assertEquals(t / r * vj[a], m, EPS);
      }
    }
  }

  @Test
  void theRegionsAreSpecSection2() {
    for (Tetra.Region r : Tetra.REGIONS) {
      Roi roi = Tetra.region(r);
      assertEquals(4, roi.getRoot());
      assertEquals(5, roi.getVerticesCount());
      assertFalse(roi.getVertices(4).hasHandle());
      assertFalse(roi.getVertices(4).getEnclosing());
      for (int i = 0; i < 4; i++) {
        var h = roi.getVertices(i).getHandle();
        assertEquals(List.of(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0), h.getFrameList());
        boolean primary = r.typeName().equals("tetra.scale") || r.typeName().equals("tetra.volume");
        assertEquals(primary ? 0 : 7, h.getDofMask());
        assertEquals(r.typeName().equals("tetra.scale"), h.hasMinExtent());
      }
      assertEquals(r.typeName().equals("tetra.scale") ? 12 : 0, roi.getBindingsCount());
    }
  }

  /** §3, case 1: a corner pulled out along its ray; the face draws in, the volume stays. */
  @Test
  void aCornerPulledOutKeepsTheVolume() {
    double[][] p = rest();
    double[] p1 = Tetra.add(p[4], scaled(Tetra.sub(p[3], p[4]), 1.5));
    double[][] out = ((Tetra.Positions) Tetra.keepVolume(p, 3, p1)).positions();
    assertEquals(Tetra.volume(p), Tetra.volume(out), 1e-9 * Tetra.volume(p));
    assertArrayEquals(p1, out[3], EPS);
    assertArrayEquals(p[4], out[4], EPS);
    double[] g = Tetra.centroid(new double[][] {p[0], p[1], p[2]});
    double k = Tetra.dot(Tetra.sub(out[0], g), Tetra.sub(p[0], g)) / Tetra.dot(Tetra.sub(p[0], g), Tetra.sub(p[0], g));
    assertTrue(k < 1, "the face draws in");
    for (int j = 0; j < 3; j++) assertArrayEquals(Tetra.add(g, scaled(Tetra.sub(p[j], g), k)), out[j], 1e-9);
  }

  /** §3, case 2: pushed in, within the 1/16 bound; the face spreads out. */
  @Test
  void aCornerPushedInWithinBoundsKeepsTheVolume() {
    double[][] p = rest();
    double[] p1 = Tetra.add(p[4], scaled(Tetra.sub(p[0], p[4]), 0.6));
    var a = Tetra.keepVolume(p, 0, p1);
    double[][] out = assertInstanceOf(Tetra.Positions.class, a).positions();
    assertEquals(Tetra.volume(p), Tetra.volume(out), 1e-9 * Tetra.volume(p));
  }

  /** §3, case 3: past the 1/16 limit, or through the face: REJECTED with a message; and a non-corner. */
  @Test
  void pastTheLimitIsRejected() {
    double[][] p = rest();
    int h = 1;
    double[] g = Tetra.centroid(new double[][] {p[0], p[2], p[3]});
    double[] n = Tetra.cross(Tetra.sub(p[2], p[0]), Tetra.sub(p[3], p[0]));
    double s0 = Tetra.dot(n, Tetra.sub(p[h], g)) / Math.sqrt(Tetra.dot(n, n));
    double[] unit = scaled(n, 1 / Math.sqrt(Tetra.dot(n, n)));
    double[] at32 = Tetra.add(g, scaled(unit, s0 / 32)); // 1/32 of the height: past 1/16
    assertEquals(new Tetra.Rejected(Tetra.TOO_CLOSE), Tetra.keepVolume(p, h, at32));
    double[] through = Tetra.add(g, scaled(unit, -s0)); // the other side of the face
    assertInstanceOf(Tetra.Rejected.class, Tetra.keepVolume(p, h, through));
    double[] at15 = Tetra.add(g, scaled(unit, s0 / 15)); // just inside the bound
    assertInstanceOf(Tetra.Positions.class, Tetra.keepVolume(p, h, at15));
    assertEquals(new Tetra.Rejected("vertex 4 is not a corner"), Tetra.keepVolume(p, 4, p[4]));
  }

  /** A shaper result is one finite position per vertex. */
  @Test
  void aResultIsOneFinitePositionPerVertex() {
    double[][] p = rest();
    double[][] out = ((Tetra.Positions) Tetra.keepVolume(p, 2, Tetra.add(p[4], scaled(Tetra.sub(p[2], p[4]), 1.2)))).positions();
    assertEquals(5, out.length);
    for (double[] q : out) {
      assertEquals(3, q.length);
      for (double v : q) assertTrue(Double.isFinite(v));
    }
  }

  @Test
  void theManifestOnDiskIsTheDeclaredOne() throws Exception {
    String disk = Files.readString(Path.of(System.getenv().getOrDefault("MILL_WORKSPACE_ROOT", "."), "vizi-plugin.json"));
    assertEquals(ManifestParser.parseDetached(disk).manifest(), TetraShape.build().manifest());
    assertEquals(disk, ManifestWriter.write(TetraShape.build().manifest()));
  }

  static double[] scaled(double[] v, double f) {
    return new double[] {v[0] * f, v[1] * f, v[2] * f};
  }
}
