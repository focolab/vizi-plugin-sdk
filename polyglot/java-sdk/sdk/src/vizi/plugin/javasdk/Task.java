package vizi.plugin.javasdk;

import vizi.plugin.javasdk.control.WorkRegistry;

/**
 * A background task (control_plane_protocol.md §8.16): work the plugin runs alongside, blocking no
 * request, shown to the person with its progress and, when {@code cancellable}, a Cancel. Started
 * with {@link PluginContext#startTask}, which reports it at once; ended by {@link #done}, whose
 * text is its outcome. At most 64 are open per session. Report from any thread.
 *
 * <p>As a resource, {@code try (Task t = ctx.startTask("Index")) { ... t.done("indexed 40 files"); }}
 * ends it on the way out if the body did not; {@link #run} also ends it with what escaped.
 */
public final class Task implements AutoCloseable {

  private final WorkRegistry.Task t;

  Task(WorkRegistry.Task t) {
    this.t = t;
  }

  /** The id the host names it by ({@code t1}, {@code t2}, …). */
  public String id() {
    return t.id();
  }

  public String title() {
    return t.title();
  }

  /** How far along, {@code fraction} in [0, 1], and what it is doing now. */
  public void progress(double fraction, String text) {
    t.progress(fraction, text);
  }

  /** What it is doing now, with no measure. */
  public void progress(String text) {
    t.progress(null, text);
  }

  /** Whether the host asked it to stop. */
  public boolean cancelled() {
    return t.cancelled();
  }

  /** Throws {@link Cancelled} once the host asked it to stop. */
  public void checkCancelled() {
    if (t.cancelled()) throw new Cancelled(t.title() + " was canceled");
  }

  /** Ends it; {@code outcome} is what the person is shown. A second call changes nothing. */
  public void done(String outcome) {
    t.done(outcome);
  }

  public boolean ended() {
    return t.ended();
  }

  /**
   * Ends it, if it has not ended, as {@code "canceled"} when it was canceled and {@code "done"}
   * otherwise. Closing cannot tell whether the body finished its work after a cancel arrived, so it
   * says what the person asked for; work that finishes anyway says so with {@link #done} before it
   * closes, or runs under {@link #run}, which reports what the body returned.
   */
  @Override
  public void close() {
    t.done(t.cancelled() ? "canceled" : "done");
  }

  /** A task's body: the returned text is its outcome. */
  @FunctionalInterface
  public interface Body {
    String run(Task task) throws Exception;
  }

  /**
   * Runs {@code body} on the calling thread and ends the task with its outcome: the text it
   * returned, {@code "canceled"} for a {@link Cancelled}, or {@code "failed: <message>"} for what
   * else escaped, which is then rethrown.
   */
  public String run(Body body) throws Exception {
    try {
      String outcome = body.run(this);
      done(outcome == null ? "done" : outcome);
      return outcome == null ? "" : outcome;
    } catch (Cancelled c) {
      done("canceled");
      throw c;
    } catch (Exception | Error e) {
      done("failed: " + (e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage()));
      throw e;
    }
  }
}
