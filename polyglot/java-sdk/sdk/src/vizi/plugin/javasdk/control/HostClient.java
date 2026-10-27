package vizi.plugin.javasdk.control;

import java.time.Duration;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import java.util.concurrent.atomic.AtomicLong;
import java.util.function.Predicate;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.PluginMsg;

/**
 * Plugin-initiated requests (control_plane_protocol.md §5.3, §4): each gets a fresh id
 * ({@code q<n>}; host requests use {@code r<n>}, and a reply is matched by this table, never by
 * the prefix), is sent on the ordered queue, and blocks its caller until the host's one-way
 * reply echoes the id, the request's timeout expires, or the session ends. A pending request's id
 * belongs to its reply alone: each request takes one reply kind ({@link #REPLIES}) or {@code
 * host_error}, and a known reply of any other kind under its id faults the session.
 *
 * <p><b>The waiter lookup and the delivery are one critical section against the timeout's pop</b>:
 * a reply is either delivered to its waiter or, if the waiter has given up, reported as abandoned
 * so the dispatcher releases what it names. There is no third case in which a reply is handed to
 * a queue nobody will read.
 */
public final class HostClient {

  public static final Duration DEFAULT_TIMEOUT = Duration.ofSeconds(30);

  /** Sends a request message (the outbound queue, after the bounds check). */
  public interface Sender {
    void send(PluginMsg msg);
  }

  /**
   * The one reply kind each plugin request takes, besides {@code host_error}
   * (control_plane_protocol.md §8, the proto's {@code PluginMsg} comments). A request missing here
   * takes any reply.
   */
  static final Map<PluginMsg.MsgCase, HostMsg.MsgCase> REPLIES = replies();

  private static Map<PluginMsg.MsgCase, HostMsg.MsgCase> replies() {
    Map<PluginMsg.MsgCase, HostMsg.MsgCase> m = new java.util.EnumMap<>(PluginMsg.MsgCase.class);
    m.put(PluginMsg.MsgCase.GET_VIEW_STATE, HostMsg.MsgCase.VIEW_STATE);
    m.put(PluginMsg.MsgCase.READ_DATA_VOLUME, HostMsg.MsgCase.DATA_VOLUME);
    m.put(PluginMsg.MsgCase.CONTRIBUTE_ROIS, HostMsg.MsgCase.ROIS_CONTRIBUTED);
    m.put(PluginMsg.MsgCase.GET_ROIS, HostMsg.MsgCase.ROIS);
    m.put(PluginMsg.MsgCase.EXTEND_ROI_POSES, HostMsg.MsgCase.ROI_POSES_EXTENDED);
    m.put(PluginMsg.MsgCase.COMPILE_LABEL_VOLUME, HostMsg.MsgCase.LABEL_VOLUME_COMPILED);
    m.put(PluginMsg.MsgCase.READ_MASK_VOXELS, HostMsg.MsgCase.MASK_VOXELS);
    m.put(PluginMsg.MsgCase.ACTIVATE_TOOL, HostMsg.MsgCase.TOOL_ACTIVATION_CHANGED);
    m.put(PluginMsg.MsgCase.REQUEST_ASSET, HostMsg.MsgCase.ASSET_RESULT);
    m.put(PluginMsg.MsgCase.REQUEST_STATUS_QUERY, HostMsg.MsgCase.REQUEST_STATUS);
    m.put(PluginMsg.MsgCase.READ_ROI_INCLUSION, HostMsg.MsgCase.ROI_INCLUSION);
    m.put(PluginMsg.MsgCase.REQUEST_SAVE_PATH, HostMsg.MsgCase.SAVE_PATH_RESULT);
    m.put(PluginMsg.MsgCase.GET_COLORMAP, HostMsg.MsgCase.COLORMAP_TABLE);
    m.put(PluginMsg.MsgCase.VIEW_REQUEST, HostMsg.MsgCase.VIEW_REQUEST_RESULT);
    return java.util.Collections.unmodifiableMap(m);
  }

  /**
   * A known reply of another kind under the id of a pending request: a protocol error that faults
   * the session, since a pending request's id belongs to its reply alone. The waiter never
   * receives the reply; it fails when the session ends.
   */
  public static final class WrongReplyKind extends RuntimeException {
    private final String detail;

    WrongReplyKind(String detail) {
      super(detail);
      this.detail = detail;
    }

    /** The id, the request kind and the reply kind, as the session fault names them. */
    public String detail() {
      return detail;
    }
  }

  private record Waiter(CompletableFuture<HostMsg> future, PluginMsg.MsgCase asked) {}

  private final Object lock = new Object();
  private final Map<String, Waiter> waiters = new HashMap<>();
  private final AtomicLong next = new AtomicLong();
  private final Sender sender;
  private final Predicate<Thread> forbidden;
  private final java.util.function.Consumer<HostMsg> abandoned;
  private @Nullable String ended;
  private long unmatched;

  /**
   * @param forbidden threads that must never block on a host request (the doorbell listener and
   *     the dispatch thread); a request from one throws {@link WrongThreadException}
   */
  public HostClient(Sender sender, Predicate<Thread> forbidden) {
    this(sender, forbidden, m -> {});
  }

  /**
   * @param abandoned disposes of a reply delivered to a caller that was interrupted before it took
   *     it: releases what the reply names, as for a reply that matched no waiter
   */
  public HostClient(Sender sender, Predicate<Thread> forbidden, java.util.function.Consumer<HostMsg> abandoned) {
    this.sender = sender;
    this.forbidden = forbidden;
    this.abandoned = abandoned;
  }

  /**
   * Sends {@code request} and waits for its reply. A {@code host_error} reply throws {@link
   * HostException} with the host's kind, and so does a reply of a kind this build does not know
   * (kind {@code PROTOCOL}); the timeout throws {@link TimeoutException}.
   */
  public HostMsg call(PluginMsg.Builder request, Duration timeout)
      throws HostException, TimeoutException, InterruptedException {
    Thread me = Thread.currentThread();
    if (forbidden.test(me)) {
      throw new WrongThreadException("a host request (" + request.getMsgCase()
          + ") cannot be made from " + me.getName() + ": it would stall the doorbell or deadlock the dispatch"
          + " thread that carries its reply. Hand the work to a job (plugin.jobs().start(...)) and make the"
          + " request there");
    }
    String id = "q" + next.incrementAndGet();
    CompletableFuture<HostMsg> f = new CompletableFuture<>();
    synchronized (lock) {
      if (ended != null) throw new SessionEndedException("the session has ended: " + ended);
      waiters.put(id, new Waiter(f, request.getMsgCase()));
    }
    try {
      sender.send(request.setId(id).build());
    } catch (RuntimeException e) {
      synchronized (lock) {
        waiters.remove(id);
      }
      throw e;
    }
    HostMsg reply;
    // a host cancel of the request this thread serves wakes the wait (§8.16): a command waiting
    // on a picker answers CANCELLED at once rather than when the person closes the dialog
    WorkRegistry.Request serving = WorkRegistry.current();
    AutoCloseable wake = serving == null ? () -> {} : serving.whenCancelled(() -> f.completeExceptionally(new RequestCancelled(
        "canceled while waiting on the host's reply to " + request.getMsgCase())));
    try {
      reply = f.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
    } catch (TimeoutException e) {
      synchronized (lock) {
        if (waiters.remove(id) != null) {
          throw new TimeoutException("no reply to " + request.getMsgCase() + " (" + id + ") within " + timeout.toMillis()
              + " ms; the host's state is unknown, not dead (control_plane_protocol.md §9)");
        }
      }
      reply = delivered(f); // delivered in the same instant: it is ours after all
      if (reply == null) {
        // or the session ended in the same instant: that is the failure, not the timeout
        if (f.isCompletedExceptionally() && f.exceptionNow() instanceof SessionEndedException s) throw s;
        throw new TimeoutException("no reply to " + id);
      }
    } catch (InterruptedException e) {
      HostMsg late = null;
      synchronized (lock) {
        if (waiters.remove(id) == null) late = delivered(f); // delivered as we gave up: nobody will take it
      }
      if (late != null) abandoned.accept(late);
      throw e;
    } catch (ExecutionException e) {
      Throwable c = e.getCause();
      if (c instanceof SessionEndedException s) throw s;
      if (c instanceof RequestCancelled rc) {
        HostMsg late = null;
        synchronized (lock) {
          // its reply, when it comes, matches no waiter and is disposed of as one that timed out
          if (waiters.remove(id) == null) late = delivered(f);
        }
        if (late != null) abandoned.accept(late);
        throw new RequestCancelled(rc.getMessage() == null ? "canceled" : rc.getMessage());
      }
      throw new IllegalStateException(c);
    } finally {
      try {
        wake.close();
      } catch (Exception ignored) {
        // unregistering cannot fail
      }
    }
    return checked(reply, request.getMsgCase());
  }

  /**
   * The reply {@code f} holds, or null when it holds none: not yet done, or failed -- by the
   * session's end ({@link #endSession}) or a cancel. {@code getNow} on a failed future throws its
   * failure wrapped in a {@code CompletionException}, which escaped a waiter interrupted at the
   * moment the session ended (the chunked overlay's worker, stopped as the host's stream ended).
   */
  private static @Nullable HostMsg delivered(CompletableFuture<HostMsg> f) {
    return f.isDone() && !f.isCompletedExceptionally() ? f.getNow(null) : null;
  }

  /**
   * A {@code host_error} becomes {@link HostException} with the host's kind, its raw number kept;
   * a reply of a kind this build does not know (a later host's, §11: its oneof is unset here) is a
   * {@link HostException} of kind {@code PROTOCOL}, never a reply whose typed getter would yield a
   * default instance.
   */
  private static HostMsg checked(HostMsg reply, PluginMsg.MsgCase asked) throws HostException {
    if (reply.hasHostError()) {
      Error err = reply.getHostError();
      throw new HostException(err.getKindValue(), err.getMessage());
    }
    if (reply.getMsgCase() == HostMsg.MsgCase.MSG_NOT_SET) {
      throw new HostException(Error.Kind.PROTOCOL, "the reply to " + asked + " (" + reply.getId()
          + ") is of a kind this SDK does not know: the host speaks a later protocol minor than this build");
    }
    return reply;
  }

  /**
   * Sends {@code request} without waiting: the future completes with the reply, or exceptionally
   * with {@link HostException} (a {@code host_error}, or a reply of a kind this build does not
   * know), {@link TimeoutException} or {@link
   * SessionEndedException}. Allowed on every thread, the doorbell listener included, since nothing
   * blocks. Only for a reply that names no segment: one that arrives after the timeout is
   * discarded, and nothing it named would be released.
   */
  public CompletableFuture<HostMsg> submit(PluginMsg.Builder request, Duration timeout) {
    String id = "q" + next.incrementAndGet();
    CompletableFuture<HostMsg> f = new CompletableFuture<>();
    synchronized (lock) {
      if (ended != null) return CompletableFuture.failedFuture(new SessionEndedException("the session has ended: " + ended));
      waiters.put(id, new Waiter(f, request.getMsgCase()));
    }
    try {
      sender.send(request.setId(id).build());
    } catch (RuntimeException e) {
      synchronized (lock) {
        waiters.remove(id);
      }
      return CompletableFuture.failedFuture(e);
    }
    CompletableFuture<HostMsg> out = new CompletableFuture<>();
    PluginMsg.MsgCase asked = request.getMsgCase();
    String what = asked.toString();
    f.orTimeout(timeout.toMillis(), TimeUnit.MILLISECONDS).whenComplete((reply, t) -> {
      if (t != null) {
        synchronized (lock) {
          waiters.remove(id);
        }
        out.completeExceptionally(t instanceof TimeoutException
            ? new TimeoutException("no reply to " + what + " (" + id + ") within " + timeout.toMillis() + " ms")
            : t);
      } else {
        try {
          out.complete(checked(reply, asked));
        } catch (HostException e) {
          out.completeExceptionally(e);
        }
      }
    });
    return out;
  }

  /**
   * Hands a reply to its waiter. Returns false when no waiter holds its id — it timed out, was
   * interrupted, or never existed — and the caller then disposes of what the reply names.
   *
   * @throws WrongReplyKind for a known reply of another kind than the pending request takes
   *     ({@code host_error} answers any request): the waiter keeps its place, to fail with the
   *     session, and the caller disposes of what the reply names and faults the session
   */
  public boolean deliver(HostMsg reply) {
    synchronized (lock) {
      Waiter w = waiters.get(reply.getId());
      if (w == null) {
        unmatched++;
        return false;
      }
      HostMsg.MsgCase takes = REPLIES.get(w.asked());
      HostMsg.MsgCase kind = reply.getMsgCase();
      if (takes != null && kind != takes && kind != HostMsg.MsgCase.HOST_ERROR && kind != HostMsg.MsgCase.MSG_NOT_SET) {
        throw new WrongReplyKind("reply " + name(kind) + " under id \"" + reply.getId() + "\" of pending request "
            + name(w.asked()) + ", which takes " + name(takes) + " or host_error");
      }
      waiters.remove(reply.getId());
      w.future().complete(reply);
      return true;
    }
  }

  /** A oneof case as the proto names its arm. */
  private static String name(Enum<?> c) {
    return c.name().toLowerCase(java.util.Locale.ROOT);
  }

  /** Whether an id names a request still in flight. */
  public boolean inFlight(String id) {
    synchronized (lock) {
      return waiters.containsKey(id);
    }
  }

  /** Fails every waiter: the session ended. */
  public void endSession(String reason) {
    List<Waiter> all;
    synchronized (lock) {
      ended = reason;
      all = List.copyOf(waiters.values());
      waiters.clear();
    }
    for (Waiter w : all) w.future().completeExceptionally(new SessionEndedException("the session ended: " + reason));
  }

  /** Replies that matched no waiter, so far. */
  public long unmatched() {
    synchronized (lock) {
      return unmatched;
    }
  }
}
