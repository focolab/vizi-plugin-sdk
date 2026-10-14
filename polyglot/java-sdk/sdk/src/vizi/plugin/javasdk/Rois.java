package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import pois_math.v1.RoiOuterClass.AutoSurface;
import pois_math.v1.RoiOuterClass.AxisSource;
import pois_math.v1.RoiOuterClass.DofMode;
import pois_math.v1.RoiOuterClass.ElementRole;
import pois_math.v1.RoiOuterClass.HandleSpec;
import pois_math.v1.RoiOuterClass.LinearBinding;
import pois_math.v1.RoiOuterClass.RoiOrBuilder;
import pois_math.v1.RoiOuterClass.OutOfSpan;
import pois_math.v1.RoiOuterClass.Roi;
import pois_math.v1.RoiOuterClass.Vec3;

/**
 * Builders for native regions ({@code pois_math.v1.Roi}) whose arrangement the host reads BY
 * POSITION: a structurally different but geometrically identical region is misread rather than
 * refused, so these are the arrangements to use.
 */
public final class Rois {
  private Rois() {}

  /** Quarter arc of a conic as a rational quadratic Bézier: exact under any semi-axes. */
  public static final double ARC_WEIGHT = Math.sqrt(2.0) / 2.0;

  // the display vertices as signatures (α, β, γ) of c + α·a0 + β·a1 + γ·a2:
  // six tips (+Z −Z +Y −Y +X −X), then the four corners of each ring (ZY, ZX, YX)
  private static final int[][] RINGS = {{0, 1}, {0, 2}, {1, 2}};
  private static final int[][] RING_CORNERS = {{1, 1}, {-1, 1}, {-1, -1}, {1, -1}};
  private static final List<int[]> DISPLAY = display();
  private static final Map<String, Integer> INDEX = index();

  private static int[] sig(int axisI, int signI, int axisJ, int signJ) {
    int[] s = new int[3];
    s[axisI] = signI;
    if (axisJ >= 0) s[axisJ] = signJ;
    return s;
  }

  private static List<int[]> display() {
    List<int[]> out = new ArrayList<>();
    for (int axis = 0; axis < 3; axis++) {
      out.add(sig(axis, 1, -1, 0));
      out.add(sig(axis, -1, -1, 0));
    }
    for (int[] ring : RINGS) {
      for (int[] c : RING_CORNERS) out.add(sig(ring[0], c[0], ring[1], c[1]));
    }
    return List.copyOf(out);
  }

  private static String key(int[] s) {
    return s[0] + "," + s[1] + "," + s[2];
  }

  private static Map<String, Integer> index() {
    Map<String, Integer> m = new HashMap<>();
    for (int i = 0; i < DISPLAY.size(); i++) m.put(key(DISPLAY.get(i)), i);
    return Map.copyOf(m);
  }

  private static int at(int[] s) {
    Integer i = INDEX.get(key(s));
    if (i == null) throw new IllegalStateException("no display vertex " + key(s));
    return i;
  }

  /** A timeless axis-aligned ellipsoid centred on {@code (z, y, x)} with semi-axes {@code (a, b, c)}. */
  public static Roi ellipsoid(double z, double y, double x, double a, double b, double c) {
    return ellipsoidBuilder(z, y, x, a, b, c).build();
  }

  /**
   * An ellipsoid carried by a dense whole-region pose from frame 0: {@code poseTranslation[t]} is
   * the ZYX offset at frame {@code t}; outside the domain the region holds its end pose.
   */
  public static Roi ellipsoid(double z, double y, double x, double a, double b, double c, double[][] poseTranslation) {
    if (poseTranslation.length == 0) throw new IllegalArgumentException("pose_translation is one ZYX row per frame");
    Roi.Builder r = ellipsoidBuilder(z, y, x, a, b, c);
    r.setTStart(0).setTEnd(poseTranslation.length - 1).setOutOfSpan(OutOfSpan.HOLD);
    for (double[] row : poseTranslation) {
      if (row.length != 3) throw new IllegalArgumentException("pose_translation is one ZYX row per frame");
      r.addPoseTranslation(row[0]).addPoseTranslation(row[1]).addPoseTranslation(row[2]);
    }
    return r.build();
  }

  /**
   * The ellipsoid as a builder: four controls (the centre, then the centre plus each semi-axis,
   * Z, Y, X — the one order the host accepts), a linear compile operator onto eighteen display
   * vertices, and twelve quarter-arc edges, drawn but not enclosing (inclusion is analytic).
   */
  public static Roi.Builder ellipsoidBuilder(double z, double y, double x, double a, double b, double c) {
    for (double v : new double[] {z, y, x, a, b, c}) if (!Double.isFinite(v)) throw new IllegalArgumentException("a non-finite ellipsoid parameter");
    Roi.Builder r = Roi.newBuilder().setTypeName("ellipsoid").setRoot(0);
    double[][] offsets = {{0, 0, 0}, {a, 0, 0}, {0, b, 0}, {0, 0, c}};
    for (double[] o : offsets) {
      r.addVerticesBuilder().setPosition(Vec3.newBuilder().setZ(z + o[0]).setY(y + o[1]).setX(x + o[2]));
    }
    var compile = r.getCompileBuilder().setNumDisplay(DISPLAY.size()).addControls(0).addControls(1).addControls(2).addControls(3);
    for (int row = 0; row < DISPLAY.size(); row++) {
      int[] s = DISPLAY.get(row);
      double[] coeff = {1 - s[0] - s[1] - s[2], s[0], s[1], s[2]};
      for (int col = 0; col < 4; col++) {
        if (coeff[col] != 0) compile.addTripletsBuilder().setDisplayVertex(row).setControl(col).setCoeff(coeff[col]);
      }
    }
    for (int[] ring : RINGS) {
      int i = ring[0], j = ring[1];
      int[] tips = {at(sig(i, 1, -1, 0)), at(sig(j, 1, -1, 0)), at(sig(i, -1, -1, 0)), at(sig(j, -1, -1, 0))};
      int[] corners = new int[4];
      for (int k = 0; k < 4; k++) corners[k] = at(sig(i, RING_CORNERS[k][0], j, RING_CORNERS[k][1]));
      for (int step = 0; step < 4; step++) {
        r.addEdgesBuilder().setSource(tips[step]).setTarget(tips[(step + 1) % 4]).setControl(corners[step]).setWeight(ARC_WEIGHT)
            .setRole(ElementRole.ELEMENT_DRAWN);
      }
    }
    return r;
  }

  /**
   * A contribution: {@code roi} under the plugin-local {@code contribId} (re-sending the id
   * replaces the plugin's region), shown as {@code name} ({@code ""} for a host-generated one) in
   * {@code color} ({@code "#RRGGBB"}, or {@code ""} for the host's next default).
   */
  public static vizi.plugin.v1.ViziPlugin.RoiContribution contribution(String contribId, Roi roi, String name, String color) {
    var c = vizi.plugin.v1.ViziPlugin.RoiContribution.newBuilder().setContribId(contribId).setRoi(roi).setName(name);
    if (!color.isEmpty()) c.setStyle(vizi.plugin.v1.ViziPlugin.RoiStyle.newBuilder().setColor(color));
    return c.build();
  }

  // ---- generic regions (control_plane_protocol.md §8.8) ----

  private static final double[] IDENTITY = {1, 0, 0, 0, 1, 0, 0, 0, 1};

  /**
   * A point cloud about a centre, the generic region that needs no mesh: vertex 0 is {@code centre}
   * and the {@code root} (the centre of a {@code STAR} surface, not a point of it), and vertices 1…n
   * are {@code points} ({@code {z, y, x}} each), every one {@code enclosing}. It declares no edge,
   * facet or patch, so the host derives the surface by {@code surface} (§8.8's rule 2) and draws its
   * triangles' sides. The region is {@code ENCLOSED}, the default. Add handles and bindings to the
   * builder; a {@code type_name} the plugin declares in {@code region_types} makes it asked.
   */
  public static Roi.Builder pointCloud(String typeName, double[] centre, double[][] points, AutoSurface surface) {
    Roi.Builder r = Roi.newBuilder().setTypeName(typeName).setRoot(0).setAutoSurface(surface);
    r.addVerticesBuilder().setPosition(vec3(centre));
    for (double[] p : points) r.addVerticesBuilder().setPosition(vec3(p)).setEnclosing(true);
    return r;
  }

  /** A handle the person drags freely ({@code ALL}, every axis of the identity frame). */
  public static HandleSpec freeHandle() {
    HandleSpec.Builder h = HandleSpec.newBuilder().setMode(DofMode.ALL).setDofMask(0b111);
    for (double v : IDENTITY) h.addFrame(v);
    return h.build();
  }

  /**
   * A handle dragged along the ray from vertex {@code anchor} ({@code PRIMARY}, {@code
   * AXIS_RADIAL}), which reorients as the region reshapes. No extent bound; {@code dof_mask} is
   * left 0, since a {@code PRIMARY} handle moves along its axis whatever the mask says.
   */
  public static HandleSpec radialHandle(int anchor) {
    HandleSpec.Builder h = HandleSpec.newBuilder().setMode(DofMode.PRIMARY)
        .setAxisSource(AxisSource.AXIS_RADIAL).setExtentAnchor(anchor);
    for (double v : IDENTITY) h.addFrame(v);
    return h.build();
  }

  /**
   * {@link #radialHandle(int)}, its distance from the anchor kept at least {@code minExtent} (> 0
   * keeps a star's ray from inverting; a drag past it is {@code CLAMPED}).
   */
  public static HandleSpec radialHandle(int anchor, double minExtent) {
    return radialHandle(anchor).toBuilder().setMinExtent(minExtent).build();
  }

  /**
   * The {@code LinearBinding}s that make a drag of any of {@code handles} along its ray from {@code
   * centre} a uniform scaling about it, exactly: each other handle {@code j} is bound to each handle
   * {@code i} with {@code M = (v_j − c)·u_iᵀ / r_i}, where {@code u_i} is the unit ray to {@code i}
   * and {@code r_i} its length. A drag moves {@code i} by {@code δ = (k − 1)·r_i·u_i} for a scale
   * {@code k}, and {@code M·δ = (k − 1)·(v_j − c)}. One level of propagation (§8.8) is all this
   * needs, and the table may be cyclic. {@code n·(n − 1)} bindings, in order of {@code i} then {@code j}.
   */
  public static List<LinearBinding> scaleBindings(RoiOrBuilder roi, int centre, int... handles) {
    double[] c = zyx(roi, centre);
    List<LinearBinding> out = new ArrayList<>();
    for (int i : handles) {
      double[] vi = zyx(roi, i);
      double[] u = {vi[0] - c[0], vi[1] - c[1], vi[2] - c[2]};
      double r = Math.sqrt(u[0] * u[0] + u[1] * u[1] + u[2] * u[2]);
      if (!(r > 0)) throw new IllegalArgumentException("handle " + i + " is at the centre: it has no ray to scale along");
      for (int j : handles) {
        if (j == i) continue;
        double[] vj = zyx(roi, j);
        LinearBinding.Builder b = LinearBinding.newBuilder().setMoved(j).setTarget(i);
        for (int a = 0; a < 3; a++) {
          for (int k = 0; k < 3; k++) b.addM((vj[a] - c[a]) * (u[k] / r) / r);
        }
        out.add(b.build());
      }
    }
    return out;
  }

  private static double[] zyx(RoiOrBuilder roi, int i) {
    if (i < 0 || i >= roi.getVerticesCount()) throw new IllegalArgumentException("no vertex " + i);
    Vec3 v = roi.getVertices(i).getPosition();
    return new double[] {v.getZ(), v.getY(), v.getX()};
  }

  private static Vec3 vec3(double[] p) {
    if (p.length != 3) throw new IllegalArgumentException("a position is one {z, y, x} row");
    for (double v : p) if (!Double.isFinite(v)) throw new IllegalArgumentException("a non-finite position");
    return Vec3.newBuilder().setZ(p[0]).setY(p[1]).setX(p[2]).build();
  }

  /** Evaluates a linear compile operator against its controls: the display positions, ZYX. */
  public static double[][] compileDisplay(Roi roi) {
    double[][] out = new double[roi.getCompile().getNumDisplay()][3];
    List<Integer> controls = roi.getCompile().getControlsList();
    for (var t : roi.getCompile().getTripletsList()) {
      Vec3 v = roi.getVertices(controls.get(t.getControl())).getPosition();
      double[] row = out[t.getDisplayVertex()];
      row[0] += t.getCoeff() * v.getZ();
      row[1] += t.getCoeff() * v.getY();
      row[2] += t.getCoeff() * v.getX();
    }
    return out;
  }
}
