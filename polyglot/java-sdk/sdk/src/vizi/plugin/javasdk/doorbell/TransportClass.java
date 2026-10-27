package vizi.plugin.javasdk.doorbell;

/**
 * The doorbell's transport class. The platform decides it (doorbell_protocol.md §3): Linux
 * {@code SOCK_SEQPACKET}, macOS {@code SOCK_DGRAM}, Windows a loopback TCP stream. Framing, the
 * descriptor-count table and what a 0-byte read means are all keyed on it.
 */
public enum TransportClass {
  SEQPACKET,
  DGRAM,
  STREAM;

  public boolean messages() {
    return this != STREAM;
  }

  /** What §12 calls it in a diagnostic. */
  public String wireName() {
    return switch (this) {
      case SEQPACKET -> "unix_seqpacket";
      case DGRAM -> "unix_dgram";
      case STREAM -> "tcp_stream";
    };
  }
}
