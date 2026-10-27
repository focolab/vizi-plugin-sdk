package vizi.plugin.javasdk;

import java.math.BigDecimal;
import java.math.MathContext;
import java.math.RoundingMode;

/** Number formatting that matches what the reference drivers expect to read. */
public final class Fmt {
  private Fmt() {}

  /** C's (and Python's) {@code %g}: six significant digits. */
  public static String g(double v) {
    return g(v, 6);
  }

  /**
   * C's {@code %.<precision>g}: {@code precision} significant digits, round half to even on the
   * decimal value, trailing zeros removed, exponent form when the exponent is below −4 or at least
   * the precision, the exponent signed and at least two digits.
   */
  public static String g(double v, int precision) {
    if (Double.isNaN(v)) return "nan";
    if (Double.isInfinite(v)) return v > 0 ? "inf" : "-inf";
    int p = Math.max(1, precision);
    if (v == 0) return (1 / v < 0) ? "-0" : "0";
    BigDecimal d = new BigDecimal(v).round(new MathContext(p, RoundingMode.HALF_EVEN));
    int exp = d.precision() - d.scale() - 1;
    String out;
    if (exp < -4 || exp >= p) {
      BigDecimal mant = d.movePointLeft(exp);
      String m = strip(mant.toPlainString());
      out = m + "e" + (exp < 0 ? "-" : "+") + (Math.abs(exp) < 10 ? "0" : "") + Math.abs(exp);
    } else {
      out = strip(d.toPlainString());
    }
    return out;
  }

  private static String strip(String s) {
    if (s.indexOf('.') < 0) return s;
    int end = s.length();
    while (s.charAt(end - 1) == '0') end--;
    if (s.charAt(end - 1) == '.') end--;
    return s.substring(0, end);
  }
}
