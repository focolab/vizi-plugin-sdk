package vizi.plugin.javasdk.testkit;

import io.grpc.CallOptions;
import io.grpc.ClientCall;
import io.grpc.ManagedChannel;
import io.grpc.Metadata;
import io.grpc.Status;
import io.grpc.netty.shaded.io.grpc.netty.NettyChannelBuilder;
import io.grpc.netty.shaded.io.netty.channel.nio.NioEventLoopGroup;
import io.grpc.netty.shaded.io.netty.channel.socket.nio.NioDomainSocketChannel;
import java.net.UnixDomainSocketAddress;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import java.util.function.Predicate;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Application;
import vizi.plugin.javasdk.control.Environment;
import vizi.plugin.javasdk.control.Serving;
import vizi.plugin.javasdk.doorbell.LaunchFailure;
import vizi.plugin.javasdk.nativeio.OsException;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.v1.PluginGrpc;
import vizi.plugin.v1.ViziPlugin.Hello;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.PluginMsg;

/**
 * A host for in-process tests (unix): serves an {@link Application} exactly as a launched plugin
 * is served — a doorbell socketpair of the platform's type, a gRPC server on a unix socket — and
 * plays the host against it with a raw gRPC client and the doorbell's other end.
 *
 * <p>Everything the plugin sends is logged in order; tests wait on the log with {@link #expect},
 * never sleep for an event.
 */
public final class TestHost implements AutoCloseable {

  public final Path dir;
  public final int hostDoorbell;
  public final Serving serving;
  private final ManagedChannel channel;
  private final NioEventLoopGroup group;
  private @Nullable ClientCall<HostMsg, PluginMsg> call;
  private final CompletableFuture<Metadata> headers = new CompletableFuture<>();
  private final CompletableFuture<Status> closed = new CompletableFuture<>();
  private final List<PluginMsg> log = new ArrayList<>();
  /** The last few messages {@link #expect} took, for a timeout's message. */
  private final java.util.ArrayDeque<String> taken = new java.util.ArrayDeque<>();
  private long lastArrival = System.nanoTime();
  private volatile String step = "";
  private int cursor;
  private long nextId = 1;

  private TestHost(Path dir, int hostDoorbell, Serving serving, ManagedChannel channel, NioEventLoopGroup group) {
    this.dir = dir;
    this.hostDoorbell = hostDoorbell;
    this.serving = serving;
    this.channel = channel;
    this.group = group;
  }

  public static TestHost start(Application app) throws Exception {
    return start(app, Environment.of(java.util.Map.of()));
  }

  public static TestHost start(Application app, Environment env) throws Exception {
    if (!Platform.isUnix()) throw new UnsupportedOperationException("TestHost is unix only");
    Path dir = Files.createTempDirectory("vizi-host");
    int type = Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM;
    int[] sv;
    try {
      sv = Posix.socketpair(type);
    } catch (OsException e) {
      throw new LaunchFailure("socketpair", e);
    }
    Path uds = dir.resolve("p.sock");
    Serving serving = Serving.startForTest(app, sv[1], uds, env, null);
    NioEventLoopGroup group = new NioEventLoopGroup(1);
    ManagedChannel ch = NettyChannelBuilder.forAddress(UnixDomainSocketAddress.of(uds))
        .channelType(NioDomainSocketChannel.class)
        .eventLoopGroup(group)
        .overrideAuthority("localhost")
        .usePlaintext()
        .maxInboundMessageSize(64 * 1024 * 1024)
        .build();
    TestHost h = new TestHost(dir, sv[0], serving, ch, group);
    return h;
  }

  /** Opens the {@code Session} call; nothing is sent yet. */
  public TestHost open() {
    ClientCall<HostMsg, PluginMsg> c = channel.newCall(PluginGrpc.getSessionMethod(), CallOptions.DEFAULT);
    c.start(new ClientCall.Listener<>() {
      @Override
      public void onHeaders(Metadata h) {
        headers.complete(h);
      }

      @Override
      public void onMessage(PluginMsg m) {
        synchronized (log) {
          log.add(m);
          lastArrival = System.nanoTime();
          log.notifyAll();
        }
        c.request(1);
      }

      @Override
      public void onClose(Status status, Metadata trailers) {
        closed.complete(status);
        synchronized (log) {
          log.notifyAll();
        }
      }
    }, new Metadata());
    c.request(1);
    this.call = c;
    return this;
  }

  /** Response headers, as they arrived; completes before any message when headers are eager. */
  public CompletableFuture<Metadata> headers() {
    return headers;
  }

  public CompletableFuture<Status> closed() {
    return closed;
  }

  public void send(HostMsg m) {
    ClientCall<HostMsg, PluginMsg> c = call;
    if (c == null) throw new IllegalStateException("open() first");
    synchronized (this) {
      c.sendMessage(m);
    }
  }

  public void halfClose() {
    ClientCall<HostMsg, PluginMsg> c = call;
    if (c != null) c.halfClose();
  }

  /**
   * Names what the test is doing now. A timed-out {@link #expect} names it, so a failure says
   * which wait starved.
   */
  public TestHost step(String name) {
    step = name;
    return this;
  }

  /** A fresh host request id. */
  public synchronized String nextId() {
    return "r" + nextId++;
  }

  /** Sends {@code Hello} (major 1) and waits for {@code Ready}. */
  public PluginMsg hello() throws InterruptedException {
    send(HostMsg.newBuilder()
        .setId("Hello from vizi-java-testkit 1.0")
        .setHello(Hello.newBuilder().setProtocolMajor(1).setProtocolMinor(0))
        .build());
    return expect(PluginMsg::hasReady, Duration.ofSeconds(10));
  }

  /**
   * Waits for the next message (after those already taken) matching {@code p}. Messages that do
   * not match stay in the log for {@link #seen}; the cursor moves past the match.
   */
  public PluginMsg expect(Predicate<PluginMsg> p, Duration timeout) throws InterruptedException {
    long deadline = System.nanoTime() + timeout.toNanos();
    synchronized (log) {
      while (true) {
        for (int i = cursor; i < log.size(); i++) {
          if (p.test(log.get(i))) {
            PluginMsg m = log.get(i);
            log.remove(i);
            if (taken.size() == 6) taken.removeFirst();
            taken.addLast(summary(List.of(m)));
            return m;
          }
        }
        long now = System.nanoTime();
        long left = deadline - now;
        if (left <= 0 || closed.isDone() && cursor >= log.size()) {
          throw new AssertionError("timed out waiting" + (step.isEmpty() ? "" : " at step '" + step + "'") + " for "
              + timeout.toMillis() + " ms; saw " + summary(log) + "; last taken " + taken + "; the last message arrived "
              + (now - lastArrival) / 1_000_000 + " ms ago"
              + (closed.isDone() ? " (stream closed: " + closed.getNow(null) + ")" : "") + threads());
        }
        log.wait(Math.max(1, Math.min(left / 1_000_000, 100)));
      }
    }
  }

  /** Waits for a reply to host request {@code id}. */
  public PluginMsg reply(String id, Duration timeout) throws InterruptedException {
    return expect(m -> m.getId().equals(id), timeout);
  }

  /** Everything received and not yet taken by {@link #expect}. */
  public List<PluginMsg> seen() {
    synchronized (log) {
      return List.copyOf(log);
    }
  }

  /** Waits a bounded "nothing happens" window, then returns what arrived in it. */
  public List<PluginMsg> quiet(Duration window) throws InterruptedException {
    Thread.sleep(window.toMillis());
    return seen();
  }

  /** The SDK's and gRPC's threads, where each one is: a starved wait says who was stuck. */
  static String threads() {
    StringBuilder sb = new StringBuilder("\nthreads:");
    for (var e : Thread.getAllStackTraces().entrySet()) {
      String n = e.getKey().getName();
      if (!(n.startsWith("vizi-") || n.startsWith("grpc-") || n.contains("nioEventLoop") || n.contains("EventLoop"))) continue;
      sb.append("\n  ").append(n).append(" (").append(e.getKey().getState()).append(')');
      StackTraceElement[] st = e.getValue();
      for (int i = 0; i < Math.min(st.length, 8); i++) sb.append("\n    at ").append(st[i]);
    }
    return sb.toString();
  }

  static String summary(List<PluginMsg> l) {
    StringBuilder sb = new StringBuilder("[");
    for (PluginMsg m : l) sb.append(m.getMsgCase()).append(m.getId().isEmpty() ? "" : "(" + m.getId() + ")").append(' ');
    return sb.append(']').toString();
  }

  // ---- the doorbell's host end ----

  public void sendRecord(byte[] rec, int... fds) {
    Posix.Sent s = Posix.sendmsg(hostDoorbell, rec, fds);
    if (s.n() != rec.length) throw new IllegalStateException("doorbell send failed: " + s);
  }

  /** The next record the plugin sent on the doorbell, or null on timeout. */
  public Posix.@Nullable Received receiveRecord(Duration timeout) {
    short[] rev = new short[1];
    int r = Posix.poll(new int[] {hostDoorbell}, rev, (int) timeout.toMillis());
    if (r <= 0) return null;
    return Posix.recvmsg(hostDoorbell, 120);
  }

  /** Grants {@code h} in two halves: the {@code M} on the doorbell and the install on the stream. */
  public void grant(HostSegment h, int perms, InstallSegment install, boolean capabilityFirst) {
    if (capabilityFirst) {
      h.sendGrant(hostDoorbell, 1, perms);
      send(HostMsg.newBuilder().setInstallSegment(install).build());
    } else {
      send(HostMsg.newBuilder().setInstallSegment(install).build());
      h.sendGrant(hostDoorbell, 1, perms);
    }
  }

  @Override
  public void close() {
    ClientCall<HostMsg, PluginMsg> c = call;
    if (c != null && !closed.isDone()) {
      try {
        c.cancel("test over", null);
      } catch (RuntimeException ignored) {
        // already gone
      }
    }
    channel.shutdownNow();
    try {
      channel.awaitTermination(1, TimeUnit.SECONDS);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
    }
    group.shutdownGracefully(0, 0, TimeUnit.MILLISECONDS);
    serving.close();
    Posix.close(hostDoorbell);
    try (var s = Files.list(dir)) {
      s.forEach(p -> p.toFile().delete());
      Files.deleteIfExists(dir);
    } catch (java.io.IOException ignored) {
      // best effort
    }
  }
}
