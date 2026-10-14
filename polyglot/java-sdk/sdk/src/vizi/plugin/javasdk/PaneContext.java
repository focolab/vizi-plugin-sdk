package vizi.plugin.javasdk;

import java.util.List;
import java.util.OptionalDouble;
import vizi.plugin.javasdk.control.Arrangements;
import vizi.plugin.v1.ViziPlugin.EdgeBox;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.LevelScale;
import vizi.plugin.v1.ViziPlugin.SpacingProvenance;

/**
 * A pane's arranged interpretation (control_plane_protocol.md §8.10, §8.11): the geometry an
 * event under this pane's epoch is to be read against, with the level and crop maps.
 */
public final class PaneContext {

  private final Arrangements.PaneState state;

  PaneContext(Arrangements.PaneState state) {
    this.state = state;
  }

  public int viewId() {
    return state.viewId();
  }

  public long epoch() {
    return state.epoch();
  }

  /** The dataset open generation (64-bit; an {@code E} record carries its low 32 bits). */
  public long gen() {
    return state.gen();
  }

  /** The whole geometry, as the host sent it. */
  public Geometry geometry() {
    return state.geometry();
  }

  /**
   * The dataset's level-0 extent, half-open voxel indices: {@code {z0, zN, y0, yN, x0, xN}}, voxels
   * {@code z0 ≤ z < zN} and so on.
   */
  public long[] extent() {
    var e = state.geometry().getExtent();
    return new long[] {
      Integer.toUnsignedLong(e.getZ0()), Integer.toUnsignedLong(e.getZN()),
      Integer.toUnsignedLong(e.getY0()), Integer.toUnsignedLong(e.getYN()),
      Integer.toUnsignedLong(e.getX0()), Integer.toUnsignedLong(e.getXN())
    };
  }

  /** Visible channels at index ≥ 15 (the rest ride each event's mask). */
  public List<Integer> channelsHigh() {
    return state.channelsHigh();
  }

  /**
   * Every channel, with its name and whose it is (§8.11): label with any of them, but write a
   * {@link ChannelName#placeholder placeholder} into no artifact as the file's.
   */
  public List<ChannelName> channels() {
    return ChannelName.of(state.geometry().getChannelsList());
  }

  /** Levels declared, level 0 first. */
  public int levels() {
    return Math.max(1, state.geometry().getLevelsCount());
  }

  /** The SAMPLING factor from level 0 to {@code level}, per axis (z, y, x) — never a shape ratio. */
  public double[] scale(int level) {
    if (state.geometry().getLevelsCount() == 0) {
      if (level != 0) throw new IndexOutOfBoundsException("level " + level + " is not arranged");
      return new double[] {1, 1, 1};
    }
    LevelScale s = state.geometry().getLevels(level);
    return new double[] {s.getZ(), s.getY(), s.getX()};
  }

  /** A level-0 edge {@code e} at {@code level}: exactly {@code (e + 0.5) / scale − 0.5}, per axis. */
  public static double edgeAtLevel(double edge, double scale) {
    return (edge + 0.5) / scale - 0.5;
  }

  /** A level-{@code level} coordinate at level 0: the inverse map. */
  public static double toLevel0(double v, double scale) {
    return (v + 0.5) * scale - 0.5;
  }

  /**
   * The clip box at {@code level}, in edge coordinates: {@code {z0, z1, y0, y1, x0, x1}}. The
   * included voxel centres are {@code ceil(lo) ..= floor(hi)}, clamped to the level's shape.
   */
  public double[] cropAt(int level) {
    EdgeBox c = state.geometry().getCrop();
    double[] s = scale(level);
    return new double[] {
      edgeAtLevel(c.getZ0(), s[0]), edgeAtLevel(c.getZ1(), s[0]),
      edgeAtLevel(c.getY0(), s[1]), edgeAtLevel(c.getY1(), s[1]),
      edgeAtLevel(c.getX0(), s[2]), edgeAtLevel(c.getX1(), s[2])
    };
  }

  /** Whether a coordinate at {@code level} lies inside the clip box. */
  public boolean inCrop(int level, double z, double y, double x) {
    double[] c = cropAt(level);
    return z >= c[0] && z <= c[1] && y >= c[2] && y <= c[3] && x >= c[4] && x <= c[5];
  }

  /** Physical voxel pitch (z, y, x) — but check {@link #spacingKnown()} before believing it. */
  public double[] spacing() {
    Geometry g = state.geometry();
    return new double[] {g.getSpacingZ(), g.getSpacingY(), g.getSpacingX()};
  }

  public String spacingUnit() {
    return state.geometry().getSpacingUnit();
  }

  /** False for the display fallback: "we do not know how big a voxel is". Never read 1.0 as a measurement. */
  public boolean spacingKnown() {
    Geometry g = state.geometry();
    return g.getSpacingProvenance() != SpacingProvenance.SPACING_PROVENANCE_DISPLAY_FALLBACK
        && g.getSpacingProvenance() != SpacingProvenance.SPACING_PROVENANCE_UNSPECIFIED
        && !g.getSpacingUnit().isEmpty();
  }

  /** The physical time of index {@code t}, when a mapping is arranged; empty means unknown. */
  public OptionalDouble timeAt(long t) {
    if (state.timeMapping().isEmpty()) return OptionalDouble.empty();
    double[] m = state.timeMapping().get();
    return t >= 0 && t < m.length ? OptionalDouble.of(m[(int) t]) : OptionalDouble.empty();
  }

  public boolean hasTimeMapping() {
    return state.timeMapping().isPresent();
  }

  @Override
  public String toString() {
    return "pane " + viewId() + " epoch " + epoch() + " gen " + gen();
  }
}
