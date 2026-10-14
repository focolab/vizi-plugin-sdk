package vizi.plugin.javasdk.control;

/**
 * The host cancelled the request this thread serves while it waited on something the SDK can wake
 * (a host request of its own, a job it awaits): thrown out of the wait, it answers {@code
 * Error{CANCELLED}} like any {@link CancelSignal} (§8.16). The root API turns it into its
 * {@code Cancelled}.
 */
public final class RequestCancelled extends CancelSignal {
  private static final long serialVersionUID = 1L;

  public RequestCancelled(String message) {
    super(message);
  }
}
