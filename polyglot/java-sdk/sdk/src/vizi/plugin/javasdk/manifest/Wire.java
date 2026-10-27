package vizi.plugin.javasdk.manifest;

import java.util.Arrays;
import java.util.Optional;
import java.util.stream.Collectors;

/** Lookups and message formatting shared by the parser and the writer. */
final class Wire {
  private Wire() {}

  static <E extends Enum<E> & WireName> Optional<E> lookup(Class<E> type, String wire) {
    for (E e : type.getEnumConstants()) {
      if (e.wire().equals(wire)) return Optional.of(e);
    }
    return Optional.empty();
  }

  /** The permitted spellings as the reference parser prints them: {@code ['a', 'b']}. */
  static <E extends Enum<E> & WireName> String choices(Class<E> type) {
    return Arrays.stream(type.getEnumConstants())
        .map(e -> quote(e.wire()))
        .collect(Collectors.joining(", ", "[", "]"));
  }

  static String quote(String s) {
    return "'" + s + "'";
  }

  /**
   * A number as the reference's diagnostics print it, Python's {@code :g} (six significant digits,
   * trailing zeros removed); the same rule as the root package's formatter, which this layer sits
   * below.
   */
  static String g(double v) {
    if (Double.isNaN(v)) return "nan";
    if (Double.isInfinite(v)) return v > 0 ? "inf" : "-inf";
    if (v == 0) return (1 / v < 0) ? "-0" : "0";
    java.math.BigDecimal d = new java.math.BigDecimal(v).round(new java.math.MathContext(6, java.math.RoundingMode.HALF_EVEN));
    int exp = d.precision() - d.scale() - 1;
    if (exp < -4 || exp >= 6) {
      String m = stripZeros(d.movePointLeft(exp).toPlainString());
      return m + "e" + (exp < 0 ? "-" : "+") + (Math.abs(exp) < 10 ? "0" : "") + Math.abs(exp);
    }
    return stripZeros(d.toPlainString());
  }

  private static String stripZeros(String s) {
    if (s.indexOf('.') < 0) return s;
    int end = s.length();
    while (s.charAt(end - 1) == '0') end--;
    if (s.charAt(end - 1) == '.') end--;
    return s.substring(0, end);
  }

  /** Characters as a person counts them (code points, not UTF-16 units). */
  static int length(String s) {
    return s.codePointCount(0, s.length());
  }
}
