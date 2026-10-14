package vizi.plugin.javasdk;

import java.util.function.BooleanSupplier;
import pois_math.v1.RoiOuterClass.DofMode;
import pois_math.v1.RoiOuterClass.Roi;
import pois_math.v1.RoiOuterClass.Vec3;

/**
 * A person editing the shape of a region whose type this plugin shapes (§8.8): pois_math's {@code
 * ConstrainMoveRequest} with the host's ids. {@link #roi()} is the region's REST form as it stood
 * when the drag began, and every coordinate is its own (voxel-index ZYX, before pose, placement
 * frame and parent); {@link #p1()} has already been projected onto the handle's declared DoF.
 */
public final class ShapeEdit {
  private final vizi.plugin.v1.ViziPlugin.ShapeEdit wire;
  private final boolean commit;
  private final BooleanSupplier stale;

  ShapeEdit(vizi.plugin.v1.ViziPlugin.ShapeEdit wire, boolean commit, BooleanSupplier stale) {
    this.wire = wire;
    this.commit = commit;
    this.stale = stale;
  }

  /** True for a {@code shape_commit}, whose answer is kept; false for a {@code shape_drag}, whose answer is a proposal. */
  public boolean commit() {
    return commit;
  }

  /** The host's region id, as {@code RoiRecord} and {@code RegionsChanged} name it. */
  public String regionId() {
    return wire.getRegionId();
  }

  /** This plugin's id for the region; {@code ""} when it did not contribute it (a person's copy keeps its type). */
  public String contribId() {
    return wire.getContribId();
  }

  /** One of this plugin's region types. */
  public String typeName() {
    return wire.getTypeName();
  }

  /** The gesture, pointer down to release; host-assigned, increasing, never reused in a session. */
  public long dragId() {
    return wire.getDragId();
  }

  /** 1, 2, 3, … within a drag; 0 on a commit. */
  public long seq() {
    return Integer.toUnsignedLong(wire.getSeq());
  }

  public Roi roi() {
    return wire.getMove().getRoi();
  }

  /** The grabbed handle: an index into {@code roi().getVerticesList()}. */
  public int handle() {
    return wire.getMove().getHandle();
  }

  public DofMode mode() {
    return wire.getMove().getMode();
  }

  /** The handle's position when the drag began. */
  public Vec3 p0() {
    return wire.getMove().getP0();
  }

  /** The handle's drag axis. */
  public Vec3 axis() {
    return wire.getMove().getAxis();
  }

  /** Where the person has put the handle. */
  public Vec3 p1() {
    return wire.getMove().getP1();
  }

  /** The vertices' rest positions, {@code {z, y, x}} per vertex: a starting point for the answer. */
  public double[][] positions() {
    Roi r = roi();
    double[][] out = new double[r.getVerticesCount()][];
    for (int i = 0; i < out.length; i++) {
      Vec3 v = r.getVertices(i).getPosition();
      out[i] = new double[] {v.getZ(), v.getY(), v.getX()};
    }
    return out;
  }

  /**
   * Whether the answer is no longer wanted: a drag's once newer input has arrived or the drag was
   * committed or ended; a commit's once its edit has ended. A slow shaper may stop early.
   */
  public boolean stale() {
    return stale.getAsBoolean();
  }

  /** Throws {@link Cancelled} once {@link #stale()}. */
  public void checkStale() {
    if (stale()) throw new Cancelled("superseded");
  }

  /** The message as it arrived. */
  public vizi.plugin.v1.ViziPlugin.ShapeEdit wire() {
    return wire;
  }
}
