package vizi.plugin.javasdk.control;

/** The session ended while a plugin request was waiting for its reply. */
public final class SessionEndedException extends IllegalStateException {
  private static final long serialVersionUID = 1L;

  public SessionEndedException(String message) {
    super(message);
  }
}
