package vizi.plugin.javasdk.control;

/**
 * A blocking host request made from a thread that must never block on one: the doorbell
 * listener (it would stall input and grants) or the dispatch thread (it would deadlock, since it
 * carries the reply). The message names the fix.
 */
public final class WrongThreadException extends IllegalStateException {
  private static final long serialVersionUID = 1L;

  public WrongThreadException(String message) {
    super(message);
  }
}
