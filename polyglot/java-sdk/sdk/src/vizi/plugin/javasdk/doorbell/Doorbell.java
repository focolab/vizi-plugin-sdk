package vizi.plugin.javasdk.doorbell;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.locks.ReentrantLock;
import java.util.concurrent.locks.ReentrantReadWriteLock;
import vizi.plugin.javasdk.nativeio.Errno;
import vizi.plugin.javasdk.nativeio.OsException;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.javasdk.nativeio.Winsock;

/**
 * The plugin end of the doorbell (doorbell_protocol.md, §11a's list of what binds a plugin).
 *
 * <h2>Threads and order</h2>
 *
 * A platform thread (never a virtual one: it blocks in {@code poll} through FFM) is the
 * listener. It reads until the socket would block and processes each frame under one lock, the
 * same lock every lifecycle input takes. Each step mutates state and appends its outcomes to one
 * queue as a single serialized step; the lock is released; the listener is woken; and the queue
 * is drained, and the {@link Listener} called, outside the lock, before every frame
 * (doorbell_protocol.md §8's four steps). So emission order is production order, a frame-caused
 * cancellation follows its frame, and a terminal's cancellations precede it.
 *
 * <h2>Callbacks</h2>
 *
 * The listener calls {@link Listener} inline. <b>A callback should return within 5 ms</b>; a slow
 * one stops the socket draining, so the host's {@code DRAG}s are dropped at the source, and an
 * {@code M} queued behind it has its acknowledgement delayed by it. A callback that throws is
 * logged and the transport continues.
 *
 * <h2>Terminals</h2>
 *
 * The first terminal latches: both directions close to traffic, one terminal outcome is
 * published to the terminal sink (which must not block), the socket is released, and the
 * listener drains what is queued and exits. A stop after a fault emits nothing. The hand-up is
 * bounded by construction rather than by a timer: the sink is a non-blocking hand-off, and the
 * release follows it at once on the latching thread, because no thread holds the socket's lock
 * across a consumer callback. A consumer blocked in a callback delays only its own later
 * outcomes, never the release.
 */
public final class Doorbell {

  /** The consumer. Called on the listener thread, outside the transport's lock. */
  public interface Listener {
    void onFrame(Outcome.Frame frame);

    void onCancellation(Outcome.Cancellation cancellation);

    /** Exactly once, last: an {@link Outcome.Fault} or an {@link Outcome.Teardown}. */
    void onTerminal(Outcome terminal);
  }

  /** What became of a {@code D} send. None of these is ever retried. */
  public enum SendResult {
    SENT,
    /** Refused by backpressure: dropped and counted, as {@code D}'s policy is (§11a). */
    DROPPED,
    /** An all-zero mask, which a sender suppresses (D §1). */
    SUPPRESSED,
    /** Before the handshake completed: no record may precede it (§3.1a). */
    NOT_READY,
    /** After the terminal latch. */
    CLOSED
  }

  /** A lifecycle input's answer. */
  public record LifecycleResult(boolean ok, String detail) {}

  private final Wire wire;
  private final Listener listener;
  private final java.util.function.Consumer<Outcome> terminalSink;
  private final Stats stats = new Stats();
  private final Receiver receiver;

  private final ReentrantLock state = new ReentrantLock();
  private final ArrayDeque<Outcome> pending = new ArrayDeque<>();
  private boolean latched;
  private boolean listenerExited;

  private final ReentrantLock writer = new ReentrantLock();
  private long nextSendId = 1;
  private volatile boolean sendsEnabled;

  private final ReentrantReadWriteLock io = new ReentrantReadWriteLock();
  private boolean released;

  private final Thread thread;
  private volatile boolean trace;
  /** Listener-thread only: the terminal has been handed to the consumer. */
  private boolean terminalDelivered;

  private Doorbell(Wire wire, Listener listener, java.util.function.Consumer<Outcome> terminalSink) {
    this.wire = wire;
    this.listener = listener;
    this.terminalSink = terminalSink;
    this.receiver = new Receiver(wire.transportClass(), stats);
    this.thread = new Thread(this::run, "vizi-doorbell");
    this.thread.setDaemon(true);
  }

  // ---- setup ----

  /**
   * Adopts the doorbell the host passed through the environment and starts the listener. Call
   * this before signalling readiness (binding gRPC or publishing the port file): session-setup
   * grants follow readiness at once, and macOS queues only 32 records.
   */
  public static Doorbell fromEnvironment(
      Map<String, String> env, Listener listener, java.util.function.Consumer<Outcome> terminalSink)
      throws LaunchFailure {
    return start(adoptFromEnvironment(env), listener, terminalSink);
  }

  static Wire adoptFromEnvironment(Map<String, String> env) throws LaunchFailure {
    if (Platform.OS == Platform.WINDOWS) {
      String addr = env.get("VIZI_PLUGIN_DOORBELL_ADDR");
      String token = env.get("VIZI_PLUGIN_TOKEN");
      if (addr == null || addr.isEmpty()) throw new LaunchFailure("VIZI_PLUGIN_DOORBELL_ADDR is not set; the plugin must be launched by vizi");
      if (token == null || token.isEmpty()) throw new LaunchFailure("VIZI_PLUGIN_TOKEN is not set; the plugin must be launched by vizi");
      return connectWindows(addr, token);
    }
    String fdText = env.get("VIZI_PLUGIN_DOORBELL_FD");
    if (fdText == null || fdText.isEmpty()) {
      throw new LaunchFailure("VIZI_PLUGIN_DOORBELL_FD is not set; the plugin must be launched by vizi (running without a doorbell is not legal)");
    }
    int fd;
    try {
      fd = Integer.parseInt(fdText.trim());
    } catch (NumberFormatException e) {
      throw new LaunchFailure("VIZI_PLUGIN_DOORBELL_FD is not a descriptor number: " + fdText);
    }
    return adoptUnix(fd, true);
  }

  /**
   * Adopts an inherited, connected unix socket with its own type, which must be the platform's
   * ({@code SOCK_SEQPACKET} on Linux, {@code SOCK_DGRAM} on macOS) unless {@code
   * enforcePlatformType} is false (tests). Sets close-on-exec and non-blocking once, here; on
   * macOS also {@code SO_NOSIGPIPE}, and raises {@code SO_RCVBUF} to 64 KiB.
   */
  static Wire adoptUnix(int fd, boolean enforcePlatformType) throws LaunchFailure {
    int type;
    try {
      type = Posix.getsockoptInt(fd, Posix.SOL_SOCKET, Posix.SO_TYPE);
    } catch (OsException e) {
      if (e.code() == Errno.EBADF || e.code() == Errno.ENOTSOCK) {
        throw new LaunchFailure(
            "VIZI_PLUGIN_DOORBELL_FD=" + fd + " is " + (e.code() == Errno.EBADF ? "not an open descriptor" : "not a socket")
                + " (" + Errno.describe(e.code()) + "). The launcher probably closed descriptors or did not pass them: "
                + "a launch script must exec the plugin (manifest_schema.md §2.2)", e);
      }
      throw new LaunchFailure("cannot read SO_TYPE of the doorbell: " + e.getMessage(), e);
    }
    TransportClass cls;
    if (type == Posix.SOCK_SEQPACKET) cls = TransportClass.SEQPACKET;
    else if (type == Posix.SOCK_DGRAM) cls = TransportClass.DGRAM;
    else if (type == Posix.SOCK_STREAM && !enforcePlatformType) cls = TransportClass.STREAM;
    else throw new LaunchFailure("the doorbell socket has type " + type + ", not a doorbell type");
    if (enforcePlatformType) {
      TransportClass want = Platform.OS == Platform.DARWIN ? TransportClass.DGRAM : TransportClass.SEQPACKET;
      if (cls != want) {
        throw new LaunchFailure("the doorbell socket is " + cls.wireName() + " but this platform's doorbell is "
            + want.wireName() + " (doorbell_protocol.md §3)");
      }
    }
    try {
      Posix.setCloexec(fd);
      Posix.setNonBlocking(fd);
      if (Platform.OS == Platform.DARWIN && cls == TransportClass.DGRAM) {
        Posix.setsockoptInt(fd, Posix.SOL_SOCKET, Posix.SO_NOSIGPIPE, 1);
        try {
          Posix.setsockoptInt(fd, Posix.SOL_SOCKET, Posix.SO_RCVBUF, 64 * 1024);
        } catch (OsException ignored) {
          // A MAY (§11): the host already raised it.
        }
      }
      return cls == TransportClass.STREAM ? new Wire.UnixStream(fd) : new Wire.UnixMessage(fd, cls);
    } catch (OsException e) {
      throw new LaunchFailure("doorbell setup failed: " + e.getMessage(), e);
    }
  }

  /**
   * Windows (§3.2): split {@code 127.0.0.1:<port>} on the last colon, connect, write the 32-hex
   * token as the stream's first bytes (a loop is allowed; the token is not a record), set
   * {@code TCP_NODELAY}, then set non-blocking. The whole phase is bounded locally: any failure or
   * timeout is a launch failure.
   */
  static Wire connectWindows(String addr, String token) throws LaunchFailure {
    int colon = addr.lastIndexOf(':');
    if (colon < 0) throw new LaunchFailure("VIZI_PLUGIN_DOORBELL_ADDR has no port: " + addr);
    int port;
    try {
      port = Integer.parseInt(addr.substring(colon + 1));
    } catch (NumberFormatException e) {
      throw new LaunchFailure("VIZI_PLUGIN_DOORBELL_ADDR has a bad port: " + addr);
    }
    if (token.length() != 32 || !token.chars().allMatch(c -> Character.digit(c, 16) >= 0)) {
      throw new LaunchFailure("VIZI_PLUGIN_TOKEN is not 32 hex characters");
    }
    long[] holder = {Winsock.INVALID_SOCKET};
    Thread watchdog = Thread.ofVirtual().unstarted(() -> {
      try {
        Thread.sleep(5000);
        long s = holder[0];
        if (s != Winsock.INVALID_SOCKET) Winsock.closesocket(s);
      } catch (InterruptedException ignored) {
        // finished in time
      }
    });
    try {
      long s = Winsock.connectLoopback(port);
      holder[0] = s;
      watchdog.start();
      byte[] t = token.getBytes(StandardCharsets.US_ASCII);
      int off = 0;
      int[] err = new int[1];
      while (off < t.length) {
        int n = Winsock.send(s, t, off, t.length - off, err);
        if (n < 0) {
          if (Errno.isInterrupted(err[0])) continue;
          throw new LaunchFailure("writing the doorbell token failed: " + Errno.describe(err[0]));
        }
        off += n;
      }
      watchdog.interrupt();
      holder[0] = Winsock.INVALID_SOCKET;
      Winsock.setNoDelay(s);
      Winsock.setNonBlocking(s);
      return new Wire.WinsockStream(s);
    } catch (OsException e) {
      throw new LaunchFailure("connecting the doorbell to " + addr + " failed: " + e.getMessage(), e);
    } finally {
      watchdog.interrupt();
    }
  }

  /** Starts the listener on an adopted wire. */
  static Doorbell start(Wire wire, Listener listener, java.util.function.Consumer<Outcome> terminalSink) {
    Doorbell d = new Doorbell(wire, listener, terminalSink);
    d.trace = System.getenv("VIZI_SDK_TRACE") != null;
    d.thread.start();
    return d;
  }

  /** Test and host-emulator entry: adopt any unix socket of a doorbell type (stream = stand-in). */
  public static Doorbell adoptForTest(int fd, Listener listener, java.util.function.Consumer<Outcome> sink)
      throws LaunchFailure {
    return start(adoptUnix(fd, false), listener, sink);
  }

  public TransportClass transportClass() {
    return wire.transportClass();
  }

  public Stats stats() {
    return stats;
  }

  /** The listener thread, so a caller can refuse work that must not run on it. */
  public boolean onListenerThread() {
    return Thread.currentThread().equals(thread);
  }

  // ---- lifecycle inputs: synchronous, serialized, answered ----

  public LifecycleResult toolActivated(long handle) {
    return lifecycle(m -> m.toolActivated(handle));
  }

  public LifecycleResult toolRetired(long handle) {
    return lifecycle(m -> m.toolRetired(handle));
  }

  public LifecycleResult epochAcknowledged(int viewId, long epoch) {
    return lifecycle(m -> m.epochAcknowledged(viewId, epoch));
  }

  public LifecycleResult cancelInteraction(long interaction) {
    return lifecycle(m -> m.cancelInteraction(interaction));
  }

  public LifecycleResult cancelInteractions(int viewId) {
    return lifecycle(m -> m.cancelInteractions(viewId));
  }

  /**
   * Runs {@code action} on the listener thread after every outcome queued so far. Returns false
   * (and runs nothing) once the listener has exited.
   */
  public boolean deferInOrder(Runnable action) {
    state.lock();
    try {
      if (listenerExited || terminalDeliveredOrLatched()) return false;
      pending.add(new Outcome.Deferred(action));
    } finally {
      state.unlock();
    }
    wire.wake();
    return true;
  }

  private boolean terminalDeliveredOrLatched() {
    return latched;
  }

  private LifecycleResult lifecycle(java.util.function.Function<InputMachine, InputMachine.Lifecycle> f) {
    InputMachine.Lifecycle r;
    state.lock();
    try {
      if (latched || listenerExited) return new LifecycleResult(false, "the doorbell has closed");
      r = f.apply(receiver.input());
      pending.addAll(r.cancellations());
    } finally {
      state.unlock();
    }
    if (!r.cancellations().isEmpty()) wire.wake();
    return new LifecycleResult(r.ok(), r.detail());
  }

  // ---- sending: D only, one call, never retried ----

  /** Opens the send gate. Call once the handshake has completed (Ready sent). */
  public void enableSends() {
    sendsEnabled = true;
  }

  public SendResult sendHint(Notification hint) {
    if (hint.segmentId() == 0) throw new IllegalArgumentException("segment_id 0 names no segment");
    if (hint.emptyMask()) return SendResult.SUPPRESSED;
    if (!sendsEnabled) {
      stats.hintsRefused.increment();
      return SendResult.NOT_READY;
    }
    Outcome terminal = null;
    SendResult result;
    writer.lock();
    try {
      state.lock();
      try {
        if (latched) {
          stats.hintsRefused.increment();
          return SendResult.CLOSED;
        }
      } finally {
        state.unlock();
      }
      long id = nextSendId;
      nextSendId = (nextSendId + 1) & 0xffffffffL;
      byte[] rec = hint.encode(id);
      while (true) {
        Posix.Sent s;
        io.readLock().lock();
        try {
          if (released) {
            stats.hintsRefused.increment();
            return SendResult.CLOSED;
          }
          s = wire.send(rec);
        } finally {
          io.readLock().unlock();
        }
        if (s.n() == Records.SIZE) {
          stats.hintsSent.increment();
          result = SendResult.SENT;
          break;
        }
        TransportClass tc = wire.transportClass();
        if (s.n() >= 0) {
          terminal = new Outcome.Fault(DoorbellFault.shortWrite(s.n(), 0, tc, "sent " + s.n() + " of 112 bytes"));
          result = SendResult.CLOSED;
          break;
        }
        int e = s.errno();
        if (Errno.isInterrupted(e)) continue;
        if (Errno.isBackpressure(e)) {
          stats.hintsDropped.increment();
          result = SendResult.DROPPED;
          break;
        }
        if (Errno.isPeerGone(e)) {
          terminal = new Outcome.Teardown("peer gone on send: " + Errno.describe(e));
        } else if (Errno.isLocallyClosed(e)) {
          terminal = new Outcome.Teardown("locally shut down (" + Errno.describe(e) + ")");
        } else {
          terminal = new Outcome.Fault(DoorbellFault.ambiguousSend(e, tc, "send failed: " + Errno.describe(e)));
        }
        result = SendResult.CLOSED;
        break;
      }
    } finally {
      writer.unlock();
    }
    if (terminal != null) latch(terminal);
    return result;
  }

  // ---- stopping ----

  /**
   * A thread-safe cancellation request: emits one teardown (preceded by a cancellation per open
   * interaction) unless a terminal already happened, releases the socket and wakes the listener.
   * Returns as soon as that is recorded; it is not a join.
   */
  public void stop() {
    latch(new Outcome.Teardown("local stop"));
  }

  /** Waits for the listener to exit, which is bounded only by the consumer's callbacks. */
  public boolean join(Duration timeout) throws InterruptedException {
    thread.join(timeout);
    return !thread.isAlive();
  }

  /**
   * Latches the first terminal: queues one cancellation per open interaction, then the terminal;
   * publishes the terminal to the sink; releases the socket; wakes the listener.
   */
  private boolean latch(Outcome terminal) {
    state.lock();
    try {
      if (latched) return false;
      latched = true;
      pending.addAll(receiver.input().cancelAll());
      pending.add(terminal);
    } finally {
      state.unlock();
    }
    try {
      terminalSink.accept(terminal);
    } catch (RuntimeException e) {
      log("terminal sink raised: " + e);
    }
    release();
    wire.wake();
    return true;
  }

  private void release() {
    io.writeLock().lock();
    try {
      if (released) return;
      released = true;
      wire.closeSocket();
    } finally {
      io.writeLock().unlock();
    }
  }

  // ---- the listener ----

  private void run() {
    try {
      while (true) {
        if (drain() || terminalDelivered) return;
        boolean closed;
        state.lock();
        try {
          closed = latched;
        } finally {
          state.unlock();
        }
        if (closed) continue; // drain the rest, then exit on the terminal
        int r = wire.await(-1);
        if (r < 0) {
          int e = -r;
          if (Errno.isInterrupted(e)) continue;
          latch(new Outcome.Fault(DoorbellFault.recvError(e, receiver.ordinal() + 1, wire.transportClass(),
              "poll failed: " + Errno.describe(e))));
          continue;
        }
        if ((r & Wire.READABLE) != 0) readUntilWouldBlock();
      }
    } catch (Throwable t) {
      log("doorbell listener died: " + t);
      latch(new Outcome.Teardown("listener failed: " + t));
      drain();
    } finally {
      state.lock();
      try {
        listenerExited = true;
      } finally {
        state.unlock();
      }
      release();
      wire.closeWake();
    }
  }

  private void readUntilWouldBlock() {
    while (true) {
      if (drain()) return;
      Posix.Received rv;
      io.readLock().lock();
      try {
        if (released) return;
        rv = wire.receive();
      } finally {
        io.readLock().unlock();
      }
      if (rv.n() < 0) {
        int e = rv.errno();
        if (Errno.isInterrupted(e)) continue;
        if (Errno.isWouldBlock(e)) return;
        if (Errno.isLocallyClosed(e)) {
          latch(new Outcome.Teardown("locally shut down (" + Errno.describe(e) + ")"));
        } else if (Errno.isPeerGone(e)) {
          latch(new Outcome.Teardown("peer gone: " + Errno.describe(e)));
        } else {
          latch(new Outcome.Fault(DoorbellFault.recvError(e, receiver.ordinal() + 1, wire.transportClass(),
              "receive failed: " + Errno.describe(e))));
        }
        return;
      }
      Outcome terminal = null;
      state.lock();
      try {
        if (latched) {
          Receiver.closeAll(rv.fds());
          return;
        }
        Receiver.Result res;
        if (wire.transportClass() == TransportClass.STREAM) {
          if (rv.n() == 0) {
            byte[] partial = receiver.partialWindow();
            terminal = new Outcome.Teardown(partial.length == 0
                ? "peer closed the stream"
                : "peer closed the stream mid-record; buffered " + partial.length + " bytes: " + Records.hex(partial));
            res = null;
          } else {
            res = receiver.onStreamBytes(rv.data(), (int) rv.n());
          }
        } else {
          res = receiver.onDatagram(rv.data(), rv.n(), rv.fds(), rv.ctrunc());
          if (res.teardown()) terminal = new Outcome.Teardown("peer closed the doorbell");
        }
        if (res != null) {
          pending.addAll(res.outcomes());
          if (res.terminal() != null) terminal = new Outcome.Fault(res.terminal());
        }
      } finally {
        state.unlock();
      }
      if (terminal != null) {
        latch(terminal);
        return;
      }
    }
  }

  /** Delivers everything queued, in order, outside the lock. Returns true after the terminal. */
  private boolean drain() {
    while (true) {
      List<Outcome> batch;
      state.lock();
      try {
        if (pending.isEmpty()) return false;
        batch = new ArrayList<>(pending);
        pending.clear();
      } finally {
        state.unlock();
      }
      for (Outcome o : batch) {
        if (deliver(o)) return true;
      }
    }
  }

  private boolean deliver(Outcome o) {
    if (trace) log("outcome " + o);
    if (o instanceof Outcome.Fault || o instanceof Outcome.Teardown) terminalDelivered = true;
    try {
      switch (o) {
        case Outcome.Frame f -> {
          try {
            listener.onFrame(f);
          } finally {
            if (f.disposition() instanceof Outcome.Decoded(Outcome.GrantBody g)) {
              g.descriptor().ifPresent(Descriptor::closeIfUnclaimed);
            }
          }
        }
        case Outcome.Cancellation c -> listener.onCancellation(c);
        case Outcome.Deferred d -> d.action().run();
        case Outcome.Fault f -> {
          listener.onTerminal(f);
          return true;
        }
        case Outcome.Teardown t -> {
          listener.onTerminal(t);
          return true;
        }
      }
    } catch (Throwable e) {
      // an Error from a handler (an AssertionError, say) is reported and the listener goes on; only
      // a VM error it cannot outlive ends it, through run()'s teardown
      log("doorbell consumer raised on " + o + ": " + e);
      if (fatal(e)) throw e;
      return o instanceof Outcome.Fault || o instanceof Outcome.Teardown;
    }
    return false;
  }

  /** A VM error no thread should carry on past; a stack overflow has unwound by the time it is caught. */
  public static boolean fatal(Throwable t) {
    return t instanceof VirtualMachineError && !(t instanceof StackOverflowError);
  }

  /** For a diagnostic: the socket and the counters. */
  public String describe() {
    return wire.describe() + " " + stats;
  }

  private static void log(String s) {
    System.err.println("[vizi-sdk doorbell] " + s);
  }
}
