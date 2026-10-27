package vizi.plugin.javasdk.control;

import java.util.Optional;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.v1.ViziPlugin.EdgeBox;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.LevelScale;

/**
 * doorbell_E_input.md §2's third terminal row, which only the supervisor can check: a record
 * whose context cannot be reconciled with the arranged state is delivered with {@code usable =
 * false} and reported as {@code unusable_context}. Exactly:
 *
 * <ul>
 *   <li>{@code level} must name an entry of the epoch's {@code Geometry.levels};
 *   <li>a non-MISS voxel-naming coordinate ({@code VOXEL}, {@code PICKED}, the pick of {@code
 *       RAY_AND_PICKED}) must lie inside the clip box converted to the record's level, per axis
 *       ({@code (e + 0.5) / scale − 0.5}); a column or a ray's eye and far point are exempt;
 *   <li>{@code t} must index the arranged time mapping, when one is arranged; silence about time
 *       means unknown, so an unmapped {@code t} is unchecked.
 * </ul>
 */
public final class Usability {
  private Usability() {}

  /** Null when usable, else why not. */
  public static @Nullable String problem(InputRecord ev, Optional<Arrangements.PaneState> pane) {
    if (pane.isEmpty()) return null; // the transport already decided the epoch
    Arrangements.PaneState p = pane.get();
    Geometry g = p.geometry();
    int level = ev.level();
    int levels = g.getLevelsCount();
    if (levels > 0 && level >= levels) {
      return "level " + level + " names no entry of the pane's " + levels + " levels";
    }
    if (levels == 0 && level != 0) return "level " + level + " with no level table arranged";
    if (p.timeMapping().isPresent()) {
      long t = ev.t();
      if (t >= p.timeMapping().get().length) {
        return "t " + t + " is past the arranged time mapping's " + p.timeMapping().get().length + " entries";
      }
    }
    if (ev.miss() || !g.hasCrop()) return null;
    int base;
    switch (ev.payloadKind()) {
      case InputRecord.KIND_VOXEL, InputRecord.KIND_PICKED -> base = 0;
      case InputRecord.KIND_RAY_AND_PICKED -> base = 8;
      default -> {
        return null;
      }
    }
    LevelScale s = levels > 0 ? g.getLevels(level) : LevelScale.newBuilder().setZ(1).setY(1).setX(1).build();
    EdgeBox c = g.getCrop();
    float z = ev.position(base);
    float y = ev.position(base + 1);
    float x = ev.position(base + 2);
    if (!inside(z, c.getZ0(), c.getZ1(), s.getZ()) || !inside(y, c.getY0(), c.getY1(), s.getY())
        || !inside(x, c.getX0(), c.getX1(), s.getX())) {
      return "the coordinate (" + z + ", " + y + ", " + x + ") at level " + level + " lies outside the clip box";
    }
    return null;
  }

  /** A level-0 edge {@code e} is exactly {@code (e + 0.5) / scale − 0.5} at a level of that scale. */
  public static double atLevel(double edge, double scale) {
    double sc = scale > 0 ? scale : 1;
    return (edge + 0.5) / sc - 0.5;
  }

  private static boolean inside(double v, double lo, double hi, double scale) {
    return v >= atLevel(lo, scale) && v <= atLevel(hi, scale);
  }
}
