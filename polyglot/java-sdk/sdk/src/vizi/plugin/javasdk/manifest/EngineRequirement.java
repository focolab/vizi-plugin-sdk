package vizi.plugin.javasdk.manifest;

import java.math.BigInteger;
import java.util.Objects;
import java.util.regex.Pattern;

/**
 * {@code engines.vizi}: the minimum protocol version the plugin needs (§2.1).
 *
 * <p>The text is kept as written ({@code "^1.0"}, {@code "1"}, {@code "1.x"} all mean 1.0) so the
 * writer reproduces it; {@link #major()} and {@link #minor()} are what it means. Each is an
 * unsigned 32-bit number, so both are {@code long}.
 *
 * @param text the string as it appears in the manifest
 * @param major the required protocol major
 * @param minor the minimum protocol minor
 */
public record EngineRequirement(String text, long major, long minor) {
  /** The protocol this SDK speaks. */
  public static final int PROTOCOL_MAJOR = 1;

  public static final int PROTOCOL_MINOR = 0;

  /** The largest component: a protocol version's numbers are unsigned 32-bit. */
  public static final long MAX_COMPONENT = 0xFFFF_FFFFL;

  /** One component: ASCII digits only. */
  private static final Pattern DIGITS = Pattern.compile("[0-9]+");

  public EngineRequirement {
    Objects.requireNonNull(text, "text");
    if (major < 0 || minor < 0) throw new IllegalArgumentException("negative version component");
    if (major > MAX_COMPONENT || minor > MAX_COMPONENT) {
      throw new IllegalArgumentException("version component above " + MAX_COMPONENT);
    }
  }

  /** The requirement {@code "1.0"}, which every manifest this SDK writes declares by default. */
  public static EngineRequirement current() {
    return new EngineRequirement(PROTOCOL_MAJOR + "." + PROTOCOL_MINOR, PROTOCOL_MAJOR, PROTOCOL_MINOR);
  }

  /**
   * Parses {@code "major.minor"}, {@code "major"} or {@code "major.x"}, with at most one leading
   * {@code >=}, {@code ^} or {@code ~} (§2.1). The rest is read exactly: ASCII digits, at most one
   * {@code .}, no whitespace anywhere (nothing is trimmed), no sign, no underscore and no other
   * Unicode digits. Leading zeros are allowed ({@code "01.0"} is 1.0). A component above
   * 4294967295 is not a version, never wrapped or clamped. A third component and {@code *} are
   * refused.
   *
   * @throws IllegalArgumentException with the §2.1 diagnostic, if it is not a version
   */
  public static EngineRequirement parse(String text) {
    String s = text;
    if (s.startsWith(">=")) s = s.substring(2);
    else if (s.startsWith("^") || s.startsWith("~")) s = s.substring(1);
    String[] parts = s.split("\\.", -1);
    if (parts.length > 2) throw notAVersion(text);
    if (negative(parts[0]) || (parts.length == 2 && negative(parts[1]))) {
      throw new IllegalArgumentException("engines.vizi " + Wire.quote(text) + " has a negative component");
    }
    long major = component(text, parts[0], false);
    long minor = parts.length == 2 ? component(text, parts[1], true) : 0;
    return new EngineRequirement(text, major, minor);
  }

  /**
   * A minus sign before ASCII digits that are not all zero: refused with the §8 fixture's
   * "negative component" wording. {@code -0} is refused as not a version, like any other sign.
   */
  private static boolean negative(String part) {
    if (part.length() < 2 || part.charAt(0) != '-') return false;
    String digits = part.substring(1);
    return DIGITS.matcher(digits).matches() && !digits.chars().allMatch(c -> c == '0');
  }

  private static long component(String text, String part, boolean xAllowed) {
    if (xAllowed && part.equals("x")) return 0;
    if (!DIGITS.matcher(part).matches()) throw notAVersion(text);
    BigInteger v = new BigInteger(part);
    if (v.compareTo(BigInteger.valueOf(MAX_COMPONENT)) > 0) throw notAVersion(text);
    return v.longValue();
  }

  private static IllegalArgumentException notAVersion(String text) {
    return new IllegalArgumentException(
        "engines.vizi " + Wire.quote(text) + " is not a version (expected \"major.minor\")");
  }

  /**
   * The §2.1 gate ({@code control_plane_protocol.md} §3.2): the majors are equal and the host's
   * minor is at least the declared minor.
   */
  public boolean admits(long hostMajor, long hostMinor) {
    return major == hostMajor && hostMinor >= minor;
  }

  /** Whether a host speaking this SDK's protocol runs the plugin. */
  public boolean admitsCurrentProtocol() {
    return admits(PROTOCOL_MAJOR, PROTOCOL_MINOR);
  }
}
