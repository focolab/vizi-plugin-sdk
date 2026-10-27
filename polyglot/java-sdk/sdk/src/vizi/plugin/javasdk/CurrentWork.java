package vizi.plugin.javasdk;

import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.WorkRegistry;

/**
 * The host request the current thread is serving, for any handler: a reader's open or read, an
 * exporter's chunk, a command (control_plane_protocol.md §8.16, reader_writer_contract.md §1.5).
 * Report progress as often as is convenient (the host shows the latest), and check {@link
 * #cancelled()} between steps: a canceled request stops by throwing {@link Cancelled}, or answers
 * with whatever it had. Outside a handler every method does nothing and nothing is canceled.
 */
public final class CurrentWork {
  private CurrentWork() {}

  /** How far along, {@code fraction} in [0, 1], and what it is doing now (one line). */
  public static void progress(double fraction, String text) {
    WorkRegistry.Request r = WorkRegistry.current();
    if (r != null) r.progress(fraction, text);
  }

  /** What it is doing now, with no measure of how far along (the host shows activity). */
  public static void progress(String text) {
    WorkRegistry.Request r = WorkRegistry.current();
    if (r != null) r.progress(null, text);
  }

  /** Whether the host asked this request to stop. */
  public static boolean cancelled() {
    WorkRegistry.Request r = WorkRegistry.current();
    return r != null && r.cancelled();
  }

  /** Throws {@link Cancelled} once the host asked this request to stop. */
  public static void checkCancelled() {
    if (cancelled()) throw new Cancelled();
  }

  /** The request's id, while a handler runs. */
  public static @Nullable String requestId() {
    WorkRegistry.Request r = WorkRegistry.current();
    return r == null ? null : r.id();
  }
}
