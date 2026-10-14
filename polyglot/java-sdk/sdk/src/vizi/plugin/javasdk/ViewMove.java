package vizi.plugin.javasdk;

import java.util.Objects;
import java.util.OptionalInt;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.LookAt;
import vizi.plugin.v1.ViziPlugin.OrbitPoint;
import vizi.plugin.v1.ViziPlugin.ViewRequest;

/**
 * What one set-view request moves (control_plane_protocol.md §8.15), applied whole or not at all.
 * Start from {@link #to()} and name the targets:
 *
 * <pre>{@code ViewMove.to().t(430).selectRegion("r7")}</pre>
 *
 * Each target must be in the panel's or tool's {@code view_targets}, or the host refuses the whole
 * request {@code NOT_DECLARED}. Immutable; the checks here are the ones that need no host.
 */
public final class ViewMove {

  private final OptionalInt t;
  private final OptionalInt z;
  private final @Nullable String selectRegion;
  private final @Nullable String frameRegion;
  private final @Nullable LookAt lookAt;
  private final @Nullable OrbitPoint orbitPoint;

  private ViewMove(OptionalInt t, OptionalInt z, @Nullable String selectRegion, @Nullable String frameRegion,
      @Nullable LookAt lookAt, @Nullable OrbitPoint orbitPoint) {
    this.t = t;
    this.z = z;
    this.selectRegion = selectRegion;
    this.frameRegion = frameRegion;
    this.lookAt = lookAt;
    this.orbitPoint = orbitPoint;
  }

  /** A move with no target yet. It must gain one before it is sent: to move nothing, decline. */
  public static ViewMove to() {
    return new ViewMove(OptionalInt.empty(), OptionalInt.empty(), null, null, null, null);
  }

  /** Set the frame. Like the person stepping, it pauses playback. */
  public ViewMove t(int t) {
    if (t < 0) throw new IllegalArgumentException("t is a frame index, not " + t);
    return new ViewMove(OptionalInt.of(t), z, selectRegion, frameRegion, lookAt, orbitPoint);
  }

  /** Set the plane. Like the person stepping, it pauses playback. */
  public ViewMove z(int z) {
    if (z < 0) throw new IllegalArgumentException("z is a plane index, not " + z);
    return new ViewMove(t, OptionalInt.of(z), selectRegion, frameRegion, lookAt, orbitPoint);
  }

  /** Select a region, as a single click on it does (one carrying a camera view is not flown to). */
  public ViewMove selectRegion(String regionId) {
    return new ViewMove(t, z, regionId(regionId), frameRegion, lookAt, orbitPoint);
  }

  /** Frame a region, as choosing it in the Regions list does. */
  public ViewMove frameRegion(String regionId) {
    return new ViewMove(t, z, selectRegion, regionId(regionId), lookAt, orbitPoint);
  }

  /**
   * Aim the camera at a level-0 voxel, voxel-centred (§8.11). {@code fitExtent} is how many voxels
   * to fit in view (0 keeps the distance); {@code directionZyx} is the viewing direction, or null
   * to keep it. The target becomes the orbit point unless {@link #orbitPoint} is also set.
   */
  public ViewMove lookAt(double[] targetZyx, double fitExtent, double @Nullable [] directionZyx) {
    var b = LookAt.newBuilder();
    for (double d : zyx("look_at target", targetZyx)) b.addTargetZyx(d);
    if (!Double.isFinite(fitExtent) || fitExtent < 0) {
      throw new IllegalArgumentException("look_at fit_extent must be finite and ≥ 0, not " + fitExtent);
    }
    b.setFitExtent(fitExtent);
    if (directionZyx != null) {
      double[] d = zyx("look_at direction", directionZyx);
      if (d[0] == 0 && d[1] == 0 && d[2] == 0) throw new IllegalArgumentException("look_at direction is the zero vector");
      for (double x : d) b.addDirectionZyx(x);
    }
    return new ViewMove(t, z, selectRegion, frameRegion, b.build(), orbitPoint);
  }

  /** Set the one orbit point (every camera mode has it), voxel-centred. */
  public ViewMove orbitPoint(double[] zyx) {
    var b = OrbitPoint.newBuilder();
    for (double d : zyx("orbit_point", zyx)) b.addZyx(d);
    return new ViewMove(t, z, selectRegion, frameRegion, lookAt, b.build());
  }

  /** Whether any target is named. */
  public boolean isEmpty() {
    return t.isEmpty() && z.isEmpty() && selectRegion == null && frameRegion == null && lookAt == null && orbitPoint == null;
  }

  /** Writes the targets into {@code r}; an empty move is refused, since that is a decline. */
  void into(ViewRequest.Builder r) {
    if (isEmpty()) {
      throw new IllegalArgumentException("a move names at least one target; to answer a cause and move nothing, decline it");
    }
    t.ifPresent(r::setT);
    z.ifPresent(r::setZ);
    if (selectRegion != null) r.setSelectRegion(selectRegion);
    if (frameRegion != null) r.setFrameRegion(frameRegion);
    if (lookAt != null) r.setLookAt(lookAt);
    if (orbitPoint != null) r.setOrbitPoint(orbitPoint);
  }

  /** The targets this move names, in {@link vizi.plugin.javasdk.manifest.ViewTarget} order. */
  public java.util.Set<vizi.plugin.javasdk.manifest.ViewTarget> targets() {
    var out = java.util.EnumSet.noneOf(vizi.plugin.javasdk.manifest.ViewTarget.class);
    if (t.isPresent()) out.add(vizi.plugin.javasdk.manifest.ViewTarget.T);
    if (z.isPresent()) out.add(vizi.plugin.javasdk.manifest.ViewTarget.Z);
    if (selectRegion != null) out.add(vizi.plugin.javasdk.manifest.ViewTarget.SELECT_REGION);
    if (frameRegion != null) out.add(vizi.plugin.javasdk.manifest.ViewTarget.FRAME_REGION);
    if (lookAt != null) out.add(vizi.plugin.javasdk.manifest.ViewTarget.LOOK_AT);
    if (orbitPoint != null) out.add(vizi.plugin.javasdk.manifest.ViewTarget.ORBIT_POINT);
    return out;
  }

  /** Writes the targets into a Follow's state; an empty move is refused, as for a request. */
  void into(vizi.plugin.v1.ViziPlugin.FollowState.Builder f) {
    if (isEmpty()) throw new IllegalArgumentException("a Follow state names at least one target");
    t.ifPresent(f::setT);
    z.ifPresent(f::setZ);
    if (selectRegion != null) f.setSelectRegion(selectRegion);
    if (frameRegion != null) f.setFrameRegion(frameRegion);
    if (lookAt != null) f.setLookAt(lookAt);
    if (orbitPoint != null) f.setOrbitPoint(orbitPoint);
  }

  @Override
  public String toString() {
    var r = ViewRequest.newBuilder();
    if (!isEmpty()) into(r);
    return "ViewMove{" + r.build().toString().replace('\n', ' ').trim() + "}";
  }

  private static String regionId(String id) {
    Objects.requireNonNull(id, "regionId");
    if (id.isEmpty()) throw new IllegalArgumentException("a region id is non-empty");
    return id;
  }

  private static double[] zyx(String what, double[] v) {
    Objects.requireNonNull(v, what);
    if (v.length != 3) throw new IllegalArgumentException(what + " is 3 values (z, y, x), not " + v.length);
    for (double d : v) if (!Double.isFinite(d)) throw new IllegalArgumentException(what + " has a non-finite value " + d);
    return v.clone();
  }
}
