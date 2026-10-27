package vizi.plugin.javasdk;

import vizi.plugin.javasdk.control.CancelSignal;

/**
 * Stops canceled work (control_plane_protocol.md §8.16). Thrown from a handler serving a host
 * request (a command, a reader's open or read, an exporter's chunk), it answers {@code
 * Error{CANCELLED}}, which is not a fault: nothing is logged against the plugin. Thrown from a
 * {@link Jobs} job, the job ends {@code CANCELLED}.
 *
 * <p>It is an {@link Error}, not an exception, so a {@code catch (Exception e)} between the check
 * and the handler's return cannot turn it into an ordinary failure by accident. Throw it yourself
 * after cleaning up, or let {@link CurrentWork#checkCancelled()}, {@link
 * CommandContext#checkCancelled()}, {@link Task#checkCancelled()} or {@link
 * Jobs.Job#checkpoint()} throw it.
 */
public final class Cancelled extends CancelSignal {
  private static final long serialVersionUID = 1L;

  /** Answers {@code CANCELLED} with the message {@code "canceled"}. */
  public Cancelled() {
    this("canceled");
  }

  /** Answers {@code CANCELLED} with {@code message}, say how far it got ("stopped at step 3"). */
  public Cancelled(String message) {
    super(message);
  }
}
