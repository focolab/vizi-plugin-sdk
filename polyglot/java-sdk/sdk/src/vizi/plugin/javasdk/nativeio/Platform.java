package vizi.plugin.javasdk.nativeio;

import java.nio.ByteOrder;
import java.util.Locale;

/**
 * The operating system this process runs on, decided once at class initialization.
 *
 * <p>Every per-OS layout and constant in this package is selected from {@link #OS}. The protocol
 * is little-endian throughout, and so is every platform the SDK supports; that is asserted here
 * so a big-endian JVM fails at start rather than misreading a record.
 */
public enum Platform {
  LINUX,
  DARWIN,
  WINDOWS;

  public static final Platform OS = detect();

  public static boolean isUnix() {
    return OS != WINDOWS;
  }

  private static Platform detect() {
    if (ByteOrder.nativeOrder() != ByteOrder.LITTLE_ENDIAN) {
      throw new ExceptionInInitializerError("the vizi protocol needs a little-endian platform");
    }
    String name = System.getProperty("os.name", "").toLowerCase(Locale.ROOT);
    if (name.startsWith("linux")) return LINUX;
    if (name.startsWith("mac") || name.startsWith("darwin")) return DARWIN;
    if (name.startsWith("windows")) return WINDOWS;
    throw new ExceptionInInitializerError("unsupported operating system: " + name);
  }
}
