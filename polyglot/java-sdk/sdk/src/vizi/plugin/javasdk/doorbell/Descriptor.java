package vizi.plugin.javasdk.doorbell;

import vizi.plugin.javasdk.nativeio.Posix;

/**
 * A descriptor handed up with an {@code M} record: claimable, and closed by the transport if and
 * only if it is still unclaimed when the consumer callback returns, including by raising
 * (doorbell_protocol.md §8). Exactly one party ever closes it.
 */
public final class Descriptor {
  private int fd;
  private boolean claimed;
  private boolean closed;

  Descriptor(int fd) {
    this.fd = fd;
  }

  /**
   * Wraps a descriptor the caller owns as a claimable one, as if a transport had handed it up
   * (tests and emulators that feed the memory layer without a socket).
   */
  public static Descriptor adopt(int fd) {
    return new Descriptor(fd);
  }

  /** Takes ownership. Throws if already claimed or already closed by the transport. */
  public synchronized int claim() {
    if (claimed || closed) throw new IllegalStateException("descriptor already " + (claimed ? "claimed" : "closed"));
    claimed = true;
    return fd;
  }

  public synchronized boolean claimed() {
    return claimed;
  }

  /** The transport's side: close unless claimed. */
  synchronized void closeIfUnclaimed() {
    if (!claimed && !closed) {
      closed = true;
      Posix.close(fd);
      fd = -1;
    }
  }

  @Override
  public synchronized String toString() {
    return "fd " + fd + (claimed ? " (claimed)" : closed ? " (closed)" : "");
  }
}
