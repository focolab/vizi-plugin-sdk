package vizi.plugin.javasdk;

import java.time.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.List;
import java.util.Objects;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Consumer;
import org.jspecify.annotations.Nullable;

/**
 * Work that outlives a command (control_plane_protocol.md §8.3: a handler returns promptly and
 * long work continues on a plugin-owned thread). One job runs at a time, on a virtual thread;
 * others wait in a bounded queue or are refused, as the policy says.
 *
 * <ul>
 *   <li>{@link #start} answers the command at once and reports the outcome later, to the
 *       callback the plugin registered;
 *   <li>{@link #await} runs a job and waits for its result (within the caller's budget).
 * </ul>
 *
 * Every running job is a background task (control_plane_protocol.md §8.16): the person sees its
 * label, its {@link Job#progress} and its outcome, and can cancel it. Cancellation is cooperative:
 * {@link Job#checkpoint()} throws {@link Cancelled} once the job is cancelled (by the plugin or by
 * the person), and the job's thread is interrupted. Jobs are cancelled and joined inside the
 * shutdown window.
 */
public final class Jobs {

  /** A job's body: the returned text is its outcome's message. */
  @FunctionalInterface
  public interface Work {
    String run(Job job) throws Exception;
  }

  public enum Status {
    DONE,
    FAILED,
    CANCELLED
  }

  /** How a job ended. {@link #text()} is what a status label shows. */
  public record Outcome(String label, Status status, String message) {
    public String text() {
      return switch (status) {
        case DONE -> message.isEmpty() ? label + ": done" : message;
        case FAILED -> label + " failed: " + message;
        case CANCELLED -> label + ": cancelled";
      };
    }

    @Override
    public String toString() {
      return text();
    }
  }

  /** The running job's view of itself. */
  public final class Job {
    private final String label;
    private final Work work;
    private final AtomicBoolean cancelled = new AtomicBoolean();
    private final CompletableFuture<Outcome> done = new CompletableFuture<>();
    private volatile @Nullable Thread thread;
    private volatile @Nullable Task task;

    Job(String label, Work work) {
      this.label = label;
      this.work = work;
    }

    public String label() {
      return label;
    }

    public boolean cancelled() {
      return cancelled.get();
    }

    /** Throws {@link Cancelled} once the job is cancelled; call it between steps. */
    public void checkpoint() {
      if (cancelled.get()) throw new Cancelled(label + " was cancelled");
    }

    /** How far along, {@code fraction} in [0, 1], and what it is doing now: shown with its task. */
    public void progress(double fraction, String text) {
      Task t = task;
      if (t != null) t.progress(fraction, text);
    }

    /** What it is doing now, with no measure. */
    public void progress(String text) {
      Task t = task;
      if (t != null) t.progress(text);
    }

    /** The host client: a job is where host requests belong. */
    public Host host() {
      return rt.host();
    }

    public Overlay overlay(String id) {
      return rt.overlay(id);
    }

    public Panel panel(String id) {
      return rt.panel(id);
    }

    /** The plugin's settings folder, created on first use ({@link PluginContext#settingsDir}). */
    public java.util.Optional<java.nio.file.Path> settingsDir() throws java.io.IOException {
      return rt.settingsDir();
    }

    public void log(String text) {
      rt.log(text);
    }

    /** Cancels once; a second call changes nothing. */
    boolean cancel() {
      if (!cancelled.compareAndSet(false, true)) return false;
      Thread t = thread;
      if (t != null) t.interrupt();
      return true;
    }
  }

  private final Plugin.Runtime rt;
  private final Consumer<Outcome> onOutcome;
  private final Object gate = new Object();
  private final Deque<Job> queue = new ArrayDeque<>();
  private int queueLimit;
  private @Nullable Consumer<Boolean> onBusy;
  private @Nullable Job running;
  private boolean closed;

  Jobs(Plugin.Runtime rt, Consumer<Outcome> onOutcome) {
    this.rt = rt;
    this.onOutcome = onOutcome;
  }

  /** Queue up to {@code n} jobs behind the running one; 0 (the default) refuses while busy. */
  public Jobs queue(int n) {
    if (n < 0) throw new IllegalArgumentException("a queue of " + n);
    rt.checkOpen("configuring jobs");
    queueLimit = n;
    return this;
  }

  /**
   * Called with true when the first job starts and false when the last one ends (edges only),
   * under the jobs lock: it must not call back into {@code Jobs}.
   */
  public Jobs onBusy(Consumer<Boolean> edge) {
    rt.checkOpen("configuring jobs");
    onBusy = edge;
    return this;
  }

  public boolean busy() {
    synchronized (gate) {
      return running != null;
    }
  }

  /**
   * Starts {@code work} (or queues it); returns {@code label} for the command to show. Throws
   * {@link Refusal} when busy and the queue is full.
   */
  public String start(String label, Work work) throws Refusal {
    submit(label, work);
    return label;
  }

  /**
   * Runs {@code work} as a job and waits for it; its message on success, else throws. Inside a
   * handler serving a host request, a cancel of that request cancels the job and wakes the wait
   * with {@link Cancelled} at once (§8.16).
   */
  public String await(String label, Work work, Duration timeout) throws Exception {
    Job j = submit(label, work);
    Outcome o;
    java.util.concurrent.CompletableFuture<Outcome> waited = j.done;
    vizi.plugin.javasdk.control.WorkRegistry.Request serving = vizi.plugin.javasdk.control.WorkRegistry.current();
    AutoCloseable wake = () -> {};
    if (serving != null) {
      java.util.concurrent.CompletableFuture<Outcome> either = new java.util.concurrent.CompletableFuture<>();
      j.done.whenComplete((v, t) -> either.complete(v));
      wake = serving.whenCancelled(() -> either.completeExceptionally(new Cancelled(label + " was cancelled with its request")));
      waited = either;
    }
    try {
      o = waited.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
    } catch (TimeoutException e) {
      j.cancel();
      throw new TimeoutException(label + " did not finish within " + timeout.toMillis() + " ms; it was cancelled");
    } catch (ExecutionException e) {
      if (e.getCause() instanceof Cancelled c) {
        j.cancel(); // the request it served was cancelled: so is the job, and the wait ends now
        throw new Cancelled(c.getMessage() == null ? "cancelled" : c.getMessage());
      }
      throw new IllegalStateException(e.getCause());
    } finally {
      wake.close();
    }
    return switch (o.status()) {
      case DONE -> o.message();
      case FAILED -> throw new RuntimeException(o.message());
      case CANCELLED -> throw new Cancelled(label + " was cancelled");
    };
  }

  /** Cancels the running job and every queued one. */
  public void cancelAll() {
    List<Job> dropped;
    Job r;
    synchronized (gate) {
      dropped = new ArrayList<>(queue);
      queue.clear();
      r = running;
    }
    for (Job j : dropped) {
      j.cancel();
      finish(j, new Outcome(j.label, Status.CANCELLED, ""), false);
    }
    if (r != null) r.cancel();
  }

  /** Inside the shutdown window: refuse new jobs, cancel, and wait for the running one. */
  void shutdown(Duration within) {
    Job r;
    synchronized (gate) {
      closed = true;
      r = running;
    }
    cancelAll();
    if (r != null) {
      try {
        r.done.get(within.toMillis(), TimeUnit.MILLISECONDS);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
      } catch (ExecutionException | TimeoutException e) {
        rt.log("job " + r.label + " did not stop within the shutdown window");
      }
    }
  }

  private Job submit(String label, Work work) throws Refusal {
    Objects.requireNonNull(label, "label");
    Objects.requireNonNull(work, "work");
    Job j = new Job(label, work);
    boolean startNow;
    synchronized (gate) {
      if (closed) throw Refusal.unsupported("the plugin is shutting down; " + label + " was not started");
      if (running == null) {
        running = j;
        startNow = true;
        edge(true); // under the gate, so edges are never reported out of order
      } else if (queue.size() < queueLimit) {
        queue.add(j);
        startNow = false;
      } else {
        Job r = running;
        throw Refusal.unsupported("busy: " + (r == null ? "a job" : r.label) + " is still running");
      }
    }
    if (startNow) launch(j);
    return j;
  }

  private void launch(Job j) {
    // shown to the person as a background task, which their Cancel cancels
    j.task = rt.startTaskQuietly(j.label, j::cancel);
    Thread.ofVirtual().name("vizi-job-" + j.label).start(() -> {
      j.thread = Thread.currentThread();
      Outcome o;
      try {
        if (j.cancelled()) throw new Cancelled(j.label + " was cancelled");
        String msg = j.work.run(j);
        o = j.cancelled() ? new Outcome(j.label, Status.CANCELLED, "") : new Outcome(j.label, Status.DONE, Objects.requireNonNullElse(msg, ""));
      } catch (Cancelled c) {
        o = new Outcome(j.label, Status.CANCELLED, "");
      } catch (InterruptedException e) {
        o = j.cancelled() ? new Outcome(j.label, Status.CANCELLED, "") : new Outcome(j.label, Status.FAILED, "interrupted");
      } catch (Throwable t) {
        o = j.cancelled() ? new Outcome(j.label, Status.CANCELLED, "")
            : new Outcome(j.label, Status.FAILED, t.getMessage() == null ? t.getClass().getSimpleName() : t.getMessage());
      }
      j.thread = null;
      Thread.interrupted();
      finish(j, o, true);
    });
  }

  private void finish(Job j, Outcome o, boolean wasRunning) {
    Task t = j.task;
    if (t != null) t.done(o.text());
    Job next = null;
    if (wasRunning) {
      // free the runner BEFORE reporting: a command the outcome prompts must find it idle
      synchronized (gate) {
        next = queue.poll();
        running = next;
        if (next == null) edge(false);
      }
    }
    try {
      onOutcome.accept(o);
    } catch (RuntimeException e) {
      rt.log("the job outcome callback failed: " + e);
    }
    j.done.complete(o);
    if (next != null) launch(next);
  }

  private void edge(boolean busy) {
    Consumer<Boolean> e = onBusy;
    if (e == null) return;
    try {
      e.accept(busy);
    } catch (RuntimeException ex) {
      rt.log("the busy callback failed: " + ex);
    }
  }
}
