package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import pois_math.v1.RoiOuterClass.Resolution;
import pois_math.v1.RoiOuterClass.Vec3;
import vizi.plugin.v1.ViziPlugin.ShapeResult;

/**
 * A {@link Shaper}'s answer (§8.8): a position for EVERY vertex of the region, in order and in the
 * edit's coordinates (voxel-index ZYX, before pose, frame and parent); or none, rejected. The
 * region's topology and everything else in its {@code Roi} are unchanged.
 */
public final class Shape {
  private final Resolution resolution;
  private final List<Vec3> positions;
  private final String message;

  private Shape(Resolution resolution, List<Vec3> positions, String message) {
    this.resolution = resolution;
    this.positions = List.copyOf(positions);
    this.message = Objects.requireNonNull(message, "message");
  }

  /** The shape: one position per vertex. */
  public static Shape ok(List<Vec3> positions) {
    return new Shape(Resolution.OK, positions, "");
  }

  /** The shape, as {@code {z, y, x}} rows, one per vertex. */
  public static Shape ok(double[][] zyx) {
    return ok(vec3s(zyx));
  }

  /** The shape, with the grabbed handle somewhere other than where the person put it (informational). */
  public static Shape clamped(List<Vec3> positions) {
    return new Shape(Resolution.CLAMPED, positions, "");
  }

  public static Shape clamped(double[][] zyx) {
    return clamped(vec3s(zyx));
  }

  /**
   * No shape for this position, and why, for the person: a drag keeps the last shape on screen, a
   * commit reverts the edit.
   */
  public static Shape rejected(String message) {
    return new Shape(Resolution.REJECTED, List.of(), message);
  }

  public Resolution resolution() {
    return resolution;
  }

  public List<Vec3> positions() {
    return positions;
  }

  public String message() {
    return message;
  }

  /** The wire form, before the SDK fills in {@code drag_id} and {@code seq}. */
  ShapeResult wire() {
    return ShapeResult.newBuilder().setResolution(resolution).addAllPositions(positions).setMessage(message).build();
  }

  private static List<Vec3> vec3s(double[][] zyx) {
    List<Vec3> out = new ArrayList<>(zyx.length);
    for (double[] p : zyx) {
      if (p.length != 3) throw new IllegalArgumentException("a position is one {z, y, x} row");
      out.add(Vec3.newBuilder().setZ(p[0]).setY(p[1]).setX(p[2]).build());
    }
    return out;
  }
}
