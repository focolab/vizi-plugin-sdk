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
   * {@code Ready.id} (re-exported as {@code Sdk.VERSION}). It is the SDK release's version in
   * Maven form: release {@code 1.0.0} is {@code 1.0.0}, and a pre-release such as {@code 1.1a1}
   * would be {@code 1.1.0-alpha.1}. It is the published artifact's version too: {@code
   * publishVersion} in {@code build.mill} must match it.
   */
  public static final String SDK_VERSION = "1.0.0";

  /**
   * The SDK release, in the form every SDK writes to a manifest's {@code sdk_version} and the
   * Python SDK's {@code __version__} uses: {@code 1.0.0} for {@link #SDK_VERSION} {@code 1.0.0}.
   */
  public static final String SDK_RELEASE = releaseForm(SDK_VERSION);

  /**
   * The oldest vizi release that speaks protocol {@code 1.0}: the {@code min_vizi} a declared
   * manifest carries when its author sets none.
   */
  public static final String DEFAULT_MIN_VIZI = "1.0.0";

  /**
   * {@code 1.0.0} → {@code 1.0.0}; {@code 1.2.3} → {@code 1.2.3}; {@code 1.1.0-alpha.1} → {@code
   * 1.1a1}. Only a pre-release drops a zero patch, as PEP 440 writes it.
   */
  static String releaseForm(String maven) {
    int dash = maven.indexOf('-');
    String base = dash < 0 ? maven : maven.substring(0, dash);
    String pre = dash < 0 ? "" : maven.substring(dash + 1);
    if (!pre.isEmpty() && base.endsWith(".0") && base.indexOf('.') != base.lastIndexOf('.')) {
      base = base.substring(0, base.length() - 2);
    }
    pre = pre.replaceFirst("^alpha\\.", "a").replaceFirst("^beta\\.", "b").replaceFirst("^rc\\.", "rc");
    return base + pre;
  }

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
