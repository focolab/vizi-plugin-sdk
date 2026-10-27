package vizi.plugin.javasdk.doorbell;

/**
 * Setup could not produce a doorbell: a missing environment variable, a socket that is not its
 * platform's type, a descriptor that is not open, a token write that failed. This is a launch
 * failure, never a {@code DoorbellFault}: there is no channel yet to fault, and the plugin exits
 * with this message on stderr, which the host captures (doorbell_protocol.md §3.1).
 */
public final class LaunchFailure extends Exception {
  private static final long serialVersionUID = 1L;

  public LaunchFailure(String message) {
    super(message);
  }

  public LaunchFailure(String message, Throwable cause) {
    super(message, cause);
  }
}
