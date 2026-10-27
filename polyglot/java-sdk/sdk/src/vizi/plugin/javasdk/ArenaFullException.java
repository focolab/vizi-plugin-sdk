package vizi.plugin.javasdk;

/**
 * A push that needed more of its arena than was free. The push was not sent, nothing was
 * overwritten, and a {@code SegmentShortfall} naming the amount went to the host, which may
 * re-grant a larger arena. Loans come back as the host releases them, so a later push may fit.
 */
public final class ArenaFullException extends RuntimeException {
  private static final long serialVersionUID = 1L;
  private final long wanted;
  private final long largestFree;

  public ArenaFullException(String message, long wanted, long largestFree) {
    super(message);
    this.wanted = wanted;
    this.largestFree = largestFree;
  }

  /** The single allocation that failed, in bytes. */
  public long wanted() {
    return wanted;
  }

  /** The longest free run at the time. */
  public long largestFree() {
    return largestFree;
  }
}
