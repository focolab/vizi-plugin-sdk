package vizi.plugin.javasdk;

import vizi.plugin.javasdk.control.Identity;

/** Facts about this SDK build. */
public final class Sdk {
  private Sdk() {}

  /** The SDK's version string, reported in {@code DoorbellFault.sdk_version}. */
  public static final String VERSION = Identity.SDK_VERSION;

  /** The SDK release ({@code 1.0.0}), which a plugin's manifest states as {@code sdk_version}. */
  public static final String RELEASE = Identity.SDK_RELEASE;

  /** The protocol this SDK speaks. */
  public static final int PROTOCOL_MAJOR = Identity.PROTOCOL_MAJOR;
  public static final int PROTOCOL_MINOR = Identity.PROTOCOL_MINOR;
}
