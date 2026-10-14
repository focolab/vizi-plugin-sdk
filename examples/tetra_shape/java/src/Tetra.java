import java.util.List;
import pois_math.v1.RoiOuterClass.AutoSurface;
import pois_math.v1.RoiOuterClass.HandleSpec;
import pois_math.v1.RoiOuterClass.LinearBinding;
import pois_math.v1.RoiOuterClass.Roi;
import pois_math.v1.RoiOuterClass.Vec3;
import vizi.plugin.javasdk.Rois;

/** SPEC §2 (the four tetrahedra) and §3 (the volume rule): geometry only, no protocol. */
final class Tetra {
  private Tetra() {}

  /** §2: the corners A, B, C, D (ZYX). */
  static final double[][] CORNERS = {{10, 10, 10}, {10, 30, 12}, {28, 18, 14}, {16, 20, 32}};

  /** §2: vertex 4, the corners' centroid, (16, 19.5, 17). */
  static final double[] CENTRE = centroid(CORNERS);

  static final int CENTRE_INDEX = 4;

  /** §2's table, in its order. */
  record Region(String contribId, String name, String typeName, double[] offset) {}

  static final List<Region> REGIONS = List.of(
      new Region("free", "Free corners", "tetra.free", new double[] {0, 0, 0}),
      new Region("scale", "Scale", "tetra.scale", new double[] {0, 0, 40}),
      new Region("volume", "Keeps its volume", "tetra.volume", new double[] {0, 0, 80}),
      new Region("unshaped", "No shaper", "tetra.unshaped", new double[] {0, 0, 120}));

  static double[] centroid(double[][] ps) {
    double[] c = new double[3];
    for (double[] p : ps) for (int a = 0; a < 3; a++) c[a] += p[a] / ps.length;
    return c;
  }

  /** §2: a corner's handle, by region type. */
  static HandleSpec handle(String typeName) {
    return switch (typeName) {
      case "tetra.free", "tetra.unshaped" -> Rois.freeHandle();
      case "tetra.scale" -> Rois.radialHandle(CENTRE_INDEX, 1.0);
      case "tetra.volume" -> Rois.radialHandle(CENTRE_INDEX);
      default -> throw new IllegalArgumentException(typeName);
    };
  }

  /** One region at rest: corners 0–3 (enclosing handles), the centre 4 (the root), and §2's extras. */
  static Roi region(Region r) {
    Roi.Builder b = Roi.newBuilder().setTypeName(r.typeName()).setRoot(CENTRE_INDEX);
    for (double[] p : CORNERS) b.addVerticesBuilder().setPosition(vec(add(p, r.offset()))).setEnclosing(true).setHandle(handle(r.typeName()));
    b.addVerticesBuilder().setPosition(vec(add(CENTRE, r.offset())));
    if (r.typeName().equals("tetra.scale")) {
      b.setAutoSurface(AutoSurface.AUTO_SURFACE_STAR);
      b.addAllBindings(scaleBindings(b));
    }
    return b.build();
  }

  /** §2: M = (v_j − c)·u_iᵀ / r_i, targets 0..3, each other corner ascending (the SDK's helper). */
  static List<LinearBinding> scaleBindings(pois_math.v1.RoiOuterClass.RoiOrBuilder roi) {
    return Rois.scaleBindings(roi, CENTRE_INDEX, 0, 1, 2, 3);
  }

  // ---- §3, the volume rule ----

  /** The shape §3 gives, or why there is none. */
  sealed interface Answer {}

  record Positions(double[][] positions) implements Answer {}

  record Rejected(String message) implements Answer {}

  static final String TOO_CLOSE = "the corner would come too close to the opposite face";

  /** §3: corner {@code h} of {@code p} (the five rest positions) moved to {@code p1}, the volume kept. */
  static Answer keepVolume(double[][] p, int h, double[] p1) {
    if (h < 0 || h > 3) return new Rejected("vertex " + h + " is not a corner");
    int[] face = new int[3];
    for (int j = 0, k = 0; j < 4; j++) if (j != h) face[k++] = j;
    double[] pa = p[face[0]], pb = p[face[1]], pd = p[face[2]];
    double[] g = centroid(new double[][] {pa, pb, pd});
    double[] n = cross(sub(pb, pa), sub(pd, pa));
    double len = Math.sqrt(dot(n, n));
    for (int a = 0; a < 3; a++) n[a] /= len;
    double s0 = dot(n, sub(p[h], g));
    double s1 = dot(n, sub(p1, g));
    if (s0 * s1 <= 0 || Math.abs(s1) < Math.abs(s0) / 16) return new Rejected(TOO_CLOSE);
    double k = Math.sqrt(Math.abs(s0) / Math.abs(s1));
    double[][] out = new double[p.length][];
    for (int i = 0; i < p.length; i++) out[i] = p[i].clone();
    out[h] = p1.clone();
    for (int j : face) for (int a = 0; a < 3; a++) out[j][a] = g[a] + k * (p[j][a] - g[a]);
    return new Positions(out);
  }

  /** |det(B − A, C − A, D − A)| / 6. */
  static double volume(double[][] p) {
    double[] u = sub(p[1], p[0]), v = sub(p[2], p[0]), w = sub(p[3], p[0]);
    return Math.abs(dot(u, cross(v, w))) / 6;
  }

  static double[] add(double[] a, double[] b) {
    return new double[] {a[0] + b[0], a[1] + b[1], a[2] + b[2]};
  }

  static double[] sub(double[] a, double[] b) {
    return new double[] {a[0] - b[0], a[1] - b[1], a[2] - b[2]};
  }

  static double dot(double[] a, double[] b) {
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
  }

  static double[] cross(double[] a, double[] b) {
    return new double[] {a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]};
  }

  static Vec3 vec(double[] p) {
    return Vec3.newBuilder().setZ(p[0]).setY(p[1]).setX(p[2]).build();
  }

  static double[] zyx(Vec3 v) {
    return new double[] {v.getZ(), v.getY(), v.getX()};
  }
}
