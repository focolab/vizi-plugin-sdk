package vizi.plugin.javasdk.control;

/**
 * A protocol version, {@code major.minor}, as a {@code Hello} or {@code Ready} announces it
 * (control_plane_protocol.md §3). Within one major a minor bump only adds, so a plugin may use a
 * message newer than 1.0 only when the host's minor says it exists.
 */
public record ProtocolVersion(int major, int minor) {

  /** Whether this version has everything {@code major.minor} has: the same major, and a minor at least as high. */
  public boolean includes(int major, int minor) {
    return this.major == major && this.minor >= minor;
  }

  @Override
  public String toString() {
    return major + "." + minor;
  }
}
