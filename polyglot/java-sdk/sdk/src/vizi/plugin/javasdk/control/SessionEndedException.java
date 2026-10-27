package vizi.plugin.javasdk.control;

import vizi.plugin.v1.ViziPlugin.Error;

/**
 * The session ended while a plugin request was waiting for its reply, or before one was made: when
 * {@code Shutdown} arrives (before the hook runs, since the host answers no request in its window,
 * §3.3), when the host's stream ends, or at a session fault. Every request still pending fails at
 * once with it, and a later one fails without being sent.
 *
 * <p>A {@link HostException} of kind {@code KIND_UNSPECIFIED}, the kind of a timeout, so a {@code
 * catch (HostException e)} sees it; a plugin tells it from other host errors by this type, not by
 * text. Its message is "the session ended: &lt;why&gt;" for a pending request and "the session has
 * ended: &lt;why&gt;" for a later one.
 */
public final class SessionEndedException extends HostException {
  private static final long serialVersionUID = 1L;

  public SessionEndedException(String message) {
    super(Error.Kind.KIND_UNSPECIFIED, message);
  }
}
