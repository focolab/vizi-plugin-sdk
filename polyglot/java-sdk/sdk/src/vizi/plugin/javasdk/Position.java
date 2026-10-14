package vizi.plugin.javasdk;

/**
 * Where an input event points (doorbell_E_input.md §3), in voxel index space of the event's
 * level, integer {@code i} being the CENTRE of voxel {@code i}. {@link None} means the host had no
 * coordinate: a gutter, a frame, an empty cell — never origin, never a guess.
 */
public sealed interface Position {

  /** No coordinate at all. */
  record None() implements Position {}

  /** A point in a 2D slice (or montage tile). {@code Math.rint} gives the containing voxel. */
  record Voxel(double z, double y, double x) implements Position {}

  /**
   * A column under a 2D projection: the pixel's two coordinates and the projected axis's range,
   * {@code min} inclusive, {@code max} exclusive. {@code axis} names the projected axis.
   */
  record Column(Axis axis, double a, double b, double min, double max) implements Position {
    /** For a Z projection, {@code a} is y and {@code b} is x. */
    public enum Axis {
      Z,
      Y,
      X
    }
  }

  /**
   * A 3D ray: the eye, a point past the clip box ({@code far}), and where the box is entered (or
   * the closest approach, on a MISS) as a fraction along it. {@code picked} is the host's chosen
   * voxel on the ray, when it made one ({@code RAY_AND_PICKED}): the front of the structure the
   * host found under the cursor. {@code backFrac} says how far that structure runs BEHIND its
   * front, as a fraction of the ray that is left there: the back point is {@code front + backFrac
   * × (far − front)}, with {@code front} the picked voxel's centre projected onto the ray.
   *
   * <p>{@code 0.0} is a structure of no depth: a real pick whose back is its front. NaN is the
   * other thing entirely: nothing along the ray stood out, the host took a likely voxel, and there
   * is no structure to have a back ({@link #pointPick()}). A host that predates the field sends
   * zeros, which read as the first.
   *
   * <p>Any 3D record may come with or without a pick, whatever its phase and whatever the tool
   * declared ({@code wants_pick}): whether the host picks is its policy, off the wire (vizi picks
   * on a PRESS or FIRE today). Without a pick, resolve the anchor yourself.
   *
   * @param backFrac in {@code [0, 1]} for a picked structure, {@code 0.0} one of no depth; NaN
   *     when nothing stood out ({@link #pointPick()}) and when there is no pick
   */
  record Ray(double[] camera, double[] far, double entryFrac, double roll, boolean rollRefY,
      java.util.Optional<Voxel> picked, double backFrac) implements Position {

    /**
     * Nothing stood out along the ray: {@code picked} is a likely voxel with no structure around
     * it. Not the same as a zero-depth pick (0.0). False with no pick.
     */
    public boolean pointPick() {
      return picked.isPresent() && Double.isNaN(backFrac);
    }

    /**
     * Where the picked voxel's centre projects onto {@code camera → far}, as a fraction of it,
     * clamped to the segment; empty with no pick.
     */
    public java.util.OptionalDouble frontFrac() {
      if (picked.isEmpty()) return java.util.OptionalDouble.empty();
      Voxel p = picked.get();
      double dz = far[0] - camera[0], dy = far[1] - camera[1], dx = far[2] - camera[2];
      double dd = dz * dz + dy * dy + dx * dx;
      if (dd <= 0.0) return java.util.OptionalDouble.of(0.0);
      double t = ((p.z() - camera[0]) * dz + (p.y() - camera[1]) * dy + (p.x() - camera[2]) * dx) / dd;
      return java.util.OptionalDouble.of(Math.min(1.0, Math.max(0.0, t)));
    }

    /** The picked front ON the ray: {@code picked}'s centre projected onto it; empty with no pick. */
    public java.util.Optional<double[]> front() {
      java.util.OptionalDouble t = frontFrac();
      return t.isPresent() ? java.util.Optional.of(at(t.getAsDouble())) : java.util.Optional.empty();
    }

    /**
     * Where the picked structure ends, on the ray: {@code front + backFrac × (far − front)}. Empty
     * with no pick or a point pick; the front itself for a structure of no depth.
     */
    public java.util.Optional<double[]> back() {
      if (picked.isEmpty() || Double.isNaN(backFrac)) return java.util.Optional.empty();
      double t = frontFrac().getAsDouble();
      return java.util.Optional.of(at(t + backFrac * (1.0 - t)));
    }

    /** {@code camera + f × (far − camera)}. */
    public double[] at(double f) {
      return new double[] {
        camera[0] + f * (far[0] - camera[0]), camera[1] + f * (far[1] - camera[1]), camera[2] + f * (far[2] - camera[2])
      };
    }

    public double[] entry() {
      return at(entryFrac);
    }
  }

  /** A position kind this SDK predates: treated as no position. */
  record Unknown(int kind) implements Position {}
}
