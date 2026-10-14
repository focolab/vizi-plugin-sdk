package vizi.plugin.javasdk.control;

/**
 * Stops a request the host cancelled (control_plane_protocol.md §8.16): thrown from a handler, it
 * answers {@code Error{CANCELLED}}, which is not a fault. It is an {@link Error}, as {@code
 * ThreadDeath} was, so that a handler's {@code catch (Exception e)} cannot turn it into an
 * ordinary failure by accident. Authors throw the root API's {@code Cancelled}, a subclass.
 */
public abstract class CancelSignal extends Error {
  private static final long serialVersionUID = 1L;

  protected CancelSignal(String message) {
    super(message, null, false, false); // no stack trace: it is a signal, not a fault
  }
}
