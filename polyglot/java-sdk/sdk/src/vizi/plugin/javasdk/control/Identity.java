package vizi.plugin.javasdk.control;

import java.nio.charset.StandardCharsets;
import org.jspecify.annotations.Nullable;

/**
 * The handshake's identity strings (control_plane_protocol.md §3.1): the host's {@code Hello.id}
 * is {@code Hello from <version string>}, the plugin's {@code Ready.id} is {@code Ready; I am
 * <version string>}. Each whole id is non-empty after its prefix, at most 2048 bytes of UTF-8, and
 * free of control characters (Unicode Cc). The receiver checks the form and nothing else.
 */
public final class Identity {
  private Identity() {}

  /**
   * This SDK build's version, reported in {@code DoorbellFault.sdk_version} and in the default
   * {@code Ready.id} (re-exported as {@code Sdk.VERSION}). The {@code -r<n>} suffix is a build
   * serial that increases with every change to the SDK, so two builds that behave differently
   * never share a version. It is the published artifact's version too: {@code publishVersion} in
   * {@code build.mill} must match it.
   */
  public static final String SDK_VERSION = "0.1.0-r83";

  /** The protocol this SDK speaks. */
  public static final int PROTOCOL_MAJOR = 1;
  public static final int PROTOCOL_MINOR = 0;

  public static final String HELLO_PREFIX = "Hello from ";
  public static final String READY_PREFIX = "Ready; I am ";
  public static final int MAX_BYTES = 2048;

  /** Null when {@code id} is a well-formed {@code Hello.id}, else what is wrong with it. */
  public static @Nullable String checkHello(String id) {
    if (!id.startsWith(HELLO_PREFIX)) return "Hello.id does not start with \"" + HELLO_PREFIX + "\"";
    if (id.length() == HELLO_PREFIX.length()) return "Hello.id carries no version string";
    if (id.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) return "Hello.id is over " + MAX_BYTES + " bytes";
    if (hasControl(id)) return "Hello.id contains a control character";
    return null;
  }

  /** {@code Ready; I am <version>}, made well-formed: control characters become spaces, then fit. */
  public static String readyId(String version) {
    StringBuilder sb = new StringBuilder();
    version.codePoints().forEach(cp -> sb.appendCodePoint(Character.getType(cp) == Character.CONTROL ? ' ' : cp));
    String v = sb.toString().strip();
    if (v.isEmpty()) v = "an unnamed plugin";
    String id = READY_PREFIX + v;
    while (id.getBytes(StandardCharsets.UTF_8).length > MAX_BYTES) id = id.substring(0, id.offsetByCodePoints(0, id.codePointCount(0, id.length()) - 1));
    return id;
  }

  static boolean hasControl(String s) {
    return s.codePoints().anyMatch(cp -> Character.getType(cp) == Character.CONTROL);
  }

  /** For a diagnostic: never echo an oversized or hostile string whole. */
  public static String truncate(String s) {
    String safe = s.codePoints().map(cp -> Character.getType(cp) == Character.CONTROL ? '?' : cp)
        .collect(StringBuilder::new, StringBuilder::appendCodePoint, StringBuilder::append).toString();
    return safe.length() <= 160 ? "\"" + safe + "\"" : "\"" + safe.substring(0, 160) + "…\" (" + s.length() + " chars)";
  }
}
