package vizi.plugin.javasdk.control;

import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.CancelWork;
import vizi.plugin.v1.ViziPlugin.Progress;
import vizi.plugin.v1.ViziPlugin.WorkProgress;

/**
 * Long-running work (control_plane_protocol.md §8.16): the requests in flight, each
 * registered when it is DISPATCHED so a {@code CancelWork} that overtakes its handler's start is
 * kept, and the plugin's background tasks, at most {@value #MAX_OPEN_TASKS} open. Progress goes out
 * as a coalescing {@code WorkProgress} keyed by its subject; {@code title} and {@code text} are cut
 * to {@value #MAX_TEXT_BYTES} UTF-8 bytes at a character boundary first.
 */
public final class WorkRegistry {

  public static final int MAX_OPEN_TASKS = 64;
  public static final int MAX_TEXT_BYTES = 256;

  private static final ThreadLocal<@Nullable Request> CURRENT = new ThreadLocal<>();

  private final Outbound out;
  private final Map<String, Request> requests = new ConcurrentHashMap<>();
  private final Object taskLock = new Object();
  private final Map<String, Task> tasks = new LinkedHashMap<>();
  private long nextTask;
  private boolean ended;

  public WorkRegistry(Outbound out) {
    this.out = out;
  }

  /** A host request in flight: its subject is its own {@code id}. */
  public final class Request {
    private final String id;
    private volatile boolean cancelled;
    private final List<Runnable> onCancel = new java.util.concurrent.CopyOnWriteArrayList<>();

    Request(String id) {
      this.id = id;
    }

    public String id() {
      return id;
    }

    /** Whether the host sent {@code CancelWork} for this request. */
    public boolean cancelled() {
      return cancelled;
    }

    /** Reports progress: {@code fraction} in [0, 1], or null for none (shown as activity). */
    public void progress(@Nullable Double fraction, String text) {
      out.workProgress(WorkProgress.newBuilder().setRequestId(id).setProgress(measure(fraction)).setText(clip(text)).build());
    }

    /**
     * Runs {@code wake} when the host cancels this request (on the dispatch thread: it must not
     * block), or at once if it already has; it may run twice, so make it idempotent. A handler
     * blocked on something else registers one to be woken. Close the result to unregister.
     */
    public AutoCloseable whenCancelled(Runnable wake) {
      onCancel.add(wake);
      if (cancelled) wake.run();
      return () -> onCancel.remove(wake);
    }

    void cancel() {
      cancelled = true;
      for (Runnable r : onCancel) r.run();
    }
  }

  /** A background task: begun by its first report, ended by {@link #done}. */
  public final class Task {
    private final String id;
    private final String title;
    private final boolean cancellable;
    private final @Nullable Runnable onCancel;
    private volatile boolean cancelled;
    private boolean ended; // under taskLock

    Task(String id, String title, boolean cancellable, @Nullable Runnable onCancel) {
      this.id = id;
      this.title = clip(title);
      this.cancellable = cancellable;
      this.onCancel = onCancel;
    }

    public String id() {
      return id;
    }

    public String title() {
      return title;
    }

    public boolean cancelled() {
      return cancelled;
    }

    /** Whether {@link #done} has ended it (or the session did). */
    public boolean ended() {
      synchronized (taskLock) {
        return ended;
      }
    }

    /** Reports progress; after the task ended, nothing. */
    public void progress(@Nullable Double fraction, String text) {
      synchronized (taskLock) {
        if (ended) return;
        out.workProgress(report(fraction, text, false));
      }
    }

    /** Ends the task, {@code text} being its outcome; a second call changes nothing. */
    public void done(String text) {
      synchronized (taskLock) {
        if (ended) return;
        ended = true;
        tasks.remove(id);
        out.workProgress(report(null, text, true));
      }
    }

    private WorkProgress report(@Nullable Double fraction, String text, boolean done) {
      WorkProgress.Builder b = WorkProgress.newBuilder().setTaskId(id).setTitle(title).setText(clip(text))
          .setCancellable(cancellable).setDone(done);
      if (!done) b.setProgress(measure(fraction));
      return b.build();
    }

    void cancel() {
      cancelled = true;
      Runnable r = onCancel;
      if (r != null) r.run();
    }
  }

  // ---- requests ----

  /** On the dispatch thread, before the request is handed to a worker. */
  public Request register(String requestId) {
    Request r = new Request(requestId);
    requests.put(requestId, r);
    return r;
  }

  /** The request answered: a later cancel naming it is a no-op. */
  public void finished(Request r) {
    requests.remove(r.id, r);
  }

  /** The request the current thread is serving, while its handler runs. */
  public static @Nullable Request current() {
    return CURRENT.get();
  }

  /** Runs {@code body} as the handler of {@code r}, so {@link #current} names it. */
  public static <T> T serving(Request r, Body<T> body) throws Exception {
    Request before = CURRENT.get();
    CURRENT.set(r);
    try {
      return body.run();
    } finally {
      CURRENT.set(before);
    }
  }

  @FunctionalInterface
  public interface Body<T> {
    T run() throws Exception;
  }

  // ---- cancel ----

  /**
   * {@code CancelWork}, on the dispatch thread: true when it named open work. Finished, unknown or
   * unnamed work is a no-op (§8.16).
   */
  public boolean cancel(CancelWork c) {
    switch (c.getSubjectCase()) {
      case REQUEST_ID -> {
        Request r = requests.get(c.getRequestId());
        if (r == null) return false;
        r.cancel();
        return true;
      }
      case TASK_ID -> {
        Task t;
        synchronized (taskLock) {
          t = tasks.get(c.getTaskId());
        }
        if (t == null) return false;
        t.cancel();
        return true;
      }
      default -> {
        return false;
      }
    }
  }

  // ---- tasks ----

  /**
   * Begins a task, reported at once with no measure. Refused past {@value #MAX_OPEN_TASKS} open, and
   * once the session has ended.
   *
   * @param onCancel runs when the host cancels it (on the dispatch thread: it must not block)
   */
  public Task startTask(String title, boolean cancellable, @Nullable Runnable onCancel) {
    synchronized (taskLock) {
      if (ended) throw new IllegalStateException("the session has ended; no task can start");
      if (tasks.size() >= MAX_OPEN_TASKS) {
        throw new IllegalStateException(MAX_OPEN_TASKS + " background tasks are already open (§9); end one before starting another");
      }
      Task t = new Task("t" + ++nextTask, title, cancellable, onCancel);
      tasks.put(t.id, t);
      out.workProgress(t.report(null, "", false));
      return t;
    }
  }

  public List<Task> openTasks() {
    synchronized (taskLock) {
      return List.copyOf(tasks.values());
    }
  }

  /** At the session's end: every open task is canceled and ended with {@code text}; no more start. */
  public void endAll(String text) {
    List<Task> open;
    synchronized (taskLock) {
      ended = true;
      open = new ArrayList<>(tasks.values());
    }
    for (Task t : open) {
      t.cancel();
      t.done(text);
    }
  }

  // ---- bounds ----

  static Progress measure(@Nullable Double fraction) {
    return fraction == null ? Progress.newBuilder().setIndeterminate(true).build() : Progress.newBuilder().setValue(fraction).build();
  }

  /** {@code s} cut to {@value #MAX_TEXT_BYTES} UTF-8 bytes, at a character boundary. */
  public static String clip(String s) {
    if (s.length() * 3 <= MAX_TEXT_BYTES || s.getBytes(StandardCharsets.UTF_8).length <= MAX_TEXT_BYTES) return s;
    int bytes = 0;
    int i = 0;
    while (i < s.length()) {
      int cp = s.codePointAt(i);
      int n = cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
      if (bytes + n > MAX_TEXT_BYTES) break;
      bytes += n;
      i += Character.charCount(cp);
    }
    return s.substring(0, i);
  }
}
