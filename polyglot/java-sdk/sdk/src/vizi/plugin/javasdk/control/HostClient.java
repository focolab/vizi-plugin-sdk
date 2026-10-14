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
 * ({@code q<n>}), is sent on the ordered queue, and blocks its caller until the host's one-way
 * reply echoes the id, the request's timeout expires, or the session ends.
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

  private final Object lock = new Object();
  private final Map<String, CompletableFuture<HostMsg>> waiters = new HashMap<>();
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
   * HostException} with the host's kind; the timeout throws {@link TimeoutException}.
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
      waiters.put(id, f);
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
        "cancelled while waiting on the host's reply to " + request.getMsgCase())));
    try {
      reply = f.get(timeout.toMillis(), TimeUnit.MILLISECONDS);
    } catch (TimeoutException e) {
      synchronized (lock) {
        if (waiters.remove(id) != null) {
          throw new TimeoutException("no reply to " + request.getMsgCase() + " (" + id + ") within " + timeout.toMillis()
              + " ms; the host's state is unknown, not dead (control_plane_protocol.md §9)");
        }
      }
      reply = f.getNow(null); // delivered in the same instant: it is ours after all
      if (reply == null) throw new TimeoutException("no reply to " + id);
    } catch (InterruptedException e) {
      HostMsg late = null;
      synchronized (lock) {
        if (waiters.remove(id) == null) late = f.getNow(null); // delivered as we gave up: nobody will take it
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
          if (waiters.remove(id) == null) late = f.getNow(null);
        }
        if (late != null) abandoned.accept(late);
        throw new RequestCancelled(rc.getMessage() == null ? "cancelled" : rc.getMessage());
      }
      throw new IllegalStateException(c);
    } finally {
      try {
        wake.close();
      } catch (Exception ignored) {
        // unregistering cannot fail
      }
    }
    if (reply.hasHostError()) {
      Error err = reply.getHostError();
      throw new HostException(err.getKind(), err.getMessage());
    }
    return reply;
  }

  /**
   * Sends {@code request} without waiting: the future completes with the reply, or exceptionally
   * with {@link HostException} (a {@code host_error}), {@link TimeoutException} or {@link
   * SessionEndedException}. Allowed on every thread, the doorbell listener included, since nothing
   * blocks. Only for a reply that names no segment: one that arrives after the timeout is
   * discarded, and nothing it named would be released.
   */
  public CompletableFuture<HostMsg> submit(PluginMsg.Builder request, Duration timeout) {
    String id = "q" + next.incrementAndGet();
    CompletableFuture<HostMsg> f = new CompletableFuture<>();
    synchronized (lock) {
      if (ended != null) return CompletableFuture.failedFuture(new SessionEndedException("the session has ended: " + ended));
      waiters.put(id, f);
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
    String what = request.getMsgCase().toString();
    f.orTimeout(timeout.toMillis(), TimeUnit.MILLISECONDS).whenComplete((reply, t) -> {
      if (t != null) {
        synchronized (lock) {
          waiters.remove(id);
        }
        out.completeExceptionally(t instanceof TimeoutException
            ? new TimeoutException("no reply to " + what + " (" + id + ") within " + timeout.toMillis() + " ms")
            : t);
      } else if (reply.hasHostError()) {
        out.completeExceptionally(new HostException(reply.getHostError().getKind(), reply.getHostError().getMessage()));
      } else {
        out.complete(reply);
      }
    });
    return out;
  }

  /**
   * Hands a reply to its waiter. Returns false when no waiter holds its id — it timed out, was
   * interrupted, or never existed — and the caller then disposes of what the reply names.
   */
  public boolean deliver(HostMsg reply) {
    synchronized (lock) {
      CompletableFuture<HostMsg> f = waiters.remove(reply.getId());
      if (f == null) {
        unmatched++;
        return false;
      }
      f.complete(reply);
      return true;
    }
  }

  /** Whether an id names a request still in flight (for an unknown kind, §11). */
  public boolean inFlight(String id) {
    synchronized (lock) {
      return waiters.containsKey(id);
    }
  }

  /** Fails every waiter: the session ended. */
  public void endSession(String reason) {
    List<CompletableFuture<HostMsg>> all;
    synchronized (lock) {
      ended = reason;
      all = List.copyOf(waiters.values());
      waiters.clear();
    }
    for (CompletableFuture<HostMsg> f : all) f.completeExceptionally(new SessionEndedException("the session ended: " + reason));
  }

  /** Replies that matched no waiter, so far. */
  public long unmatched() {
    synchronized (lock) {
      return unmatched;
    }
  }
}
