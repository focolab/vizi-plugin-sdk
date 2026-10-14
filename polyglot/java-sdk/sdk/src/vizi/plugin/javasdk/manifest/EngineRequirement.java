package vizi.plugin.javasdk.manifest;

import java.math.BigInteger;
import java.util.Objects;

/**
 * {@code engines.vizi}: the minimum protocol version the plugin needs (§2.1).
 *
 * <p>The text is kept as written ({@code "^1.0"}, {@code "1"}, {@code "1.x"} all mean 1.0) so the
 * writer reproduces it; {@link #major()} and {@link #minor()} are what it means.
 *
 * @param text the string as it appears in the manifest
 * @param major the required protocol major
 * @param minor the minimum protocol minor
 */
public record EngineRequirement(String text, int major, int minor) {
  /** The protocol this SDK speaks. */
  public static final int PROTOCOL_MAJOR = 1;

  public static final int PROTOCOL_MINOR = 0;

  public EngineRequirement {
    Objects.requireNonNull(text, "text");
    if (major < 0 || minor < 0) throw new IllegalArgumentException("negative version component");
  }

  /** The requirement {@code "1.0"}, which every manifest this SDK writes declares by default. */
  public static EngineRequirement current() {
    return new EngineRequirement(PROTOCOL_MAJOR + "." + PROTOCOL_MINOR, PROTOCOL_MAJOR, PROTOCOL_MINOR);
  }

  /**
   * Parses {@code "major.minor"}, {@code "major"} or {@code "major.x"}, with an optional leading
   * {@code ^}, {@code ~} or {@code >=} (§2.1). A third component and {@code *} are refused.
   *
   * @throws IllegalArgumentException with the §2.1 diagnostic, if it is not a version
   */
  public static EngineRequirement parse(String text) {
    String s = text.strip();
    if (s.startsWith(">=")) s = s.substring(2);
    else if (s.startsWith("^") || s.startsWith("~")) s = s.substring(1);
    s = s.strip();
    String[] parts = s.split("\\.", -1);
    if (parts.length > 2) throw notAVersion(text);
    BigInteger major = component(text, parts[0], false);
    BigInteger minor = parts.length == 2 ? component(text, parts[1], true) : BigInteger.ZERO;
    if (major.signum() < 0 || minor.signum() < 0) {
      throw new IllegalArgumentException(
          "engines.vizi " + Wire.quote(text) + " has a negative component");
    }
    if (major.bitLength() > 31 || minor.bitLength() > 31) throw notAVersion(text);
    return new EngineRequirement(text, major.intValue(), minor.intValue());
  }

  private static BigInteger component(String text, String part, boolean xAllowed) {
    if (xAllowed && part.equals("x")) return BigInteger.ZERO;
    if (!part.matches("-?[0-9]+")) throw notAVersion(text);
    return new BigInteger(part);
  }

  private static IllegalArgumentException notAVersion(String text) {
    return new IllegalArgumentException(
        "engines.vizi " + Wire.quote(text) + " is not a version (expected \"major.minor\")");
  }

  /**
   * The §2.1 gate ({@code control_plane_protocol.md} §3.2): the majors are equal and the host's
   * minor is at least the declared minor.
   */
  public boolean admits(int hostMajor, int hostMinor) {
    return major == hostMajor && hostMinor >= minor;
  }

  /** Whether a host speaking this SDK's protocol runs the plugin. */
  public boolean admitsCurrentProtocol() {
    return admits(PROTOCOL_MAJOR, PROTOCOL_MINOR);
  }
}
