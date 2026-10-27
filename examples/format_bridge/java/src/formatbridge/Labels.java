package formatbridge;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;
import java.util.Locale;

/** The panel's words. */
final class Labels {
  private Labels() {}

  /**
   * The {@code vox} line, anisotropy visible at a glance: {@code 2.000 × 0.325 × 0.325 µm (z × y ×
   * x), Δt 1.5 s}; {@code ?} per unknown component, {@code (unknown)} when nothing is known.
   */
  static String spacing(double[] zyx, double dt) {
    if (zyx[0] == 0 && zyx[1] == 0 && zyx[2] == 0 && dt == 0) return "(unknown)";
    return f3(zyx[0]) + " × " + f3(zyx[1]) + " × " + f3(zyx[2]) + " µm (z × y × x), Δt " + (dt == 0 ? "?" : g(dt)) + " s";
  }

  private static String f3(double v) {
    return v == 0 ? "?" : String.format(Locale.ROOT, "%.3f", v);
  }

  /** A number the short way: six significant digits, no trailing zeros (as C's {@code %g}). */
  static String g(double v) {
    if (Double.isNaN(v)) return "nan";
    if (Double.isInfinite(v)) return v > 0 ? "inf" : "-inf";
    if (v == 0) return (1 / v < 0) ? "-0" : "0";
    BigDecimal d = new BigDecimal(v).round(new MathContext(6, RoundingMode.HALF_EVEN));
    int exp = d.precision() - d.scale() - 1;
    if (exp < -4 || exp >= 6) {
      BigDecimal mant = d.movePointLeft(exp).stripTrailingZeros();
      String m = mant.toPlainString();
      return m + "e" + (exp < 0 ? "-" : "+") + String.format(Locale.ROOT, "%02d", Math.abs(exp));
    }
    String s = d.stripTrailingZeros().toPlainString();
    return s;
  }
}
