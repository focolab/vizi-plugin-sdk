package formatbridge;

import java.util.Map;
import java.util.OptionalDouble;

/**
 * Voxel size, as the exporter is told it (SPEC.md §7 and §"Units"): {@code OpenExport}'s fields,
 * per value, converted to the crate's canonical micrometers and seconds; else unknown. {@code 0.0}
 * is unknown and never an error. The source's document belongs to the SOURCE, and an exporter
 * writes no measurement it was not given, so nothing here reads one.
 */
public final class Ome {
  private Ome() {}

  public static final String MICRO_SIGN = "µ"; // what OME writes
  public static final String GREEK_MU = "μ"; // what real files contain; visually identical

  static final Map<String, Double> LENGTH_TO_UM = Map.of(
      "m", 1e6,
      "cm", 1e4,
      "mm", 1e3,
      MICRO_SIGN + "m", 1.0,
      GREEK_MU + "m", 1.0,
      "um", 1.0,
      "micron", 1.0,
      "nm", 1e-3,
      "Å", 1e-4,
      "angstrom", 1e-4);

  static final Map<String, Double> TIME_TO_S = Map.of("s", 1.0, "sec", 1.0, "ms", 1e-3, "min", 60.0, "h", 3600.0, "hr", 3600.0);

  /**
   * {@code value} in {@code unit}, or unknown for a value that is zero, negative or not finite, or a
   * unit not in the table; an empty unit is OME's default ({@code defaultUnit}).
   */
  static double convert(double value, String unit, Map<String, Double> table, String defaultUnit) {
    if (!Double.isFinite(value) || value <= 0.0) return 0.0;
    String u = unit.isBlank() ? defaultUnit : unit.strip();
    Double factor = table.get(u);
    return factor == null ? 0.0 : value * factor;
  }

  public static double toUm(double value, String unit) {
    return convert(value, unit, LENGTH_TO_UM, MICRO_SIGN + "m");
  }

  public static double toS(double value, String unit) {
    return convert(value, unit, TIME_TO_S, "s");
  }

  /** z, y, x in µm and t in s; 0.0 = unknown. */
  public record Spacing(double z, double y, double x, double t) {
    public double[] zyx() {
      return new double[] {z, y, x};
    }

    public double get(int i) {
      return switch (i) {
        case 0 -> z;
        case 1 -> y;
        case 2 -> x;
        default -> t;
      };
    }
  }

  /** The exporter's resolution. */
  public record Resolved(Spacing spacing) {
    public boolean anyKnown() {
      for (int i = 0; i < 4; i++) if (spacing.get(i) != 0) return true;
      return false;
    }

    public boolean allKnown() {
      for (int i = 0; i < 4; i++) if (spacing.get(i) == 0) return false;
      return true;
    }

    /** Some values known and some not: the one case SPEC.md §7 logs. */
    public boolean partial() {
      return anyKnown() && !allKnown();
    }

    /** Which values are unknown, as {@code "y, t"}. */
    public String unknown() {
      String[] axes = {"z", "y", "x", "t"};
      StringBuilder b = new StringBuilder();
      for (int i = 0; i < 4; i++) {
        if (spacing.get(i) != 0) continue;
        if (b.length() > 0) b.append(", ");
        b.append(axes[i]);
      }
      return b.toString();
    }
  }

  /** SPEC.md §7, per value: a known field, its unit converted (a unit not in the table is unknown); else unknown. */
  public static Resolved resolve(OptionalDouble z, OptionalDouble y, OptionalDouble x, String spacingUnit, OptionalDouble t,
      String timeUnit) {
    return new Resolved(new Spacing(
        z.isPresent() ? toUm(z.getAsDouble(), spacingUnit) : 0.0,
        y.isPresent() ? toUm(y.getAsDouble(), spacingUnit) : 0.0,
        x.isPresent() ? toUm(x.getAsDouble(), spacingUnit) : 0.0,
        t.isPresent() ? toS(t.getAsDouble(), timeUnit) : 0.0));
  }
}
