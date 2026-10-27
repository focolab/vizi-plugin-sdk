package vizi.plugin.javasdk.control;

import io.grpc.Metadata;
import io.grpc.ServerCall;
import io.grpc.ServerCallHandler;
import io.grpc.ServerServiceDefinition;
import io.grpc.Status;
import io.grpc.netty.shaded.io.grpc.netty.NettyServerBuilder;
import io.grpc.netty.shaded.io.netty.channel.nio.NioEventLoopGroup;
import io.grpc.netty.shaded.io.netty.channel.socket.nio.NioServerDomainSocketChannel;
import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.UnixDomainSocketAddress;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.SecureRandom;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.function.Supplier;
import java.util.logging.Logger;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.LaunchFailure;
import vizi.plugin.v1.PluginGrpc;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.PluginMsg;

/**
 * The plugin's gRPC server (control_plane_protocol.md §2): one {@code Session} call, served by a
 * {@link Session}. Readiness is the endpoint's appearance and is never observable half-ready:
 *
 * <ul>
 *   <li><b>unix</b>: bind AND listen on a short staging name in the same directory, then
 *       hard-link the real path to it and delete the staging name. The JDK and Netty bind in two
 *       steps, and a path visible between them refuses connections. A link refuses
 *       an existing target where a rename would silently replace it, so a stale path is deleted
 *       first, deliberately.
 *   <li><b>Windows</b>: bind {@code 127.0.0.1:0}, then write the port to a temporary file and
 *       move it atomically over {@code VIZI_PLUGIN_PORT_FILE}.
 * </ul>
 *
 * The call handler sends response headers the moment the call opens (§3.1's SHOULD), from a raw
 * {@link ServerCallHandler}: the stub helpers defer them to the first message. It asks for every
 * message as the previous one is handled, so reading is never gated on anything.
 */
public final class Server implements AutoCloseable {

  /** The largest inbound message accepted: the host's 32 MiB decode bound, with room for framing. */
  public static final int MAX_INBOUND = 33 * 1024 * 1024;

  private final io.grpc.Server grpc;
  private final ExecutorService dispatch;
  private final NioEventLoopGroup boss;
  private final NioEventLoopGroup workers;
  private final String endpoint;

  private Server(io.grpc.Server grpc, ExecutorService dispatch, NioEventLoopGroup boss, NioEventLoopGroup workers, String endpoint) {
    this.grpc = grpc;
    this.dispatch = dispatch;
    this.boss = boss;
    this.workers = workers;
    this.endpoint = endpoint;
  }

  public String endpoint() {
    return endpoint;
  }

  /**
   * Netty warns that SO_KEEPALIVE is unknown on a domain socket; harmless, and it would be the
   * first line of every plugin's stderr. Only that record is dropped: every other
   * warning in the package still reaches stderr. A filter, unlike a level, is not
   * inherited, so it goes on the class loggers Netty logs through. Held here: java.util.logging
   * keeps loggers weakly, so a filter set on one nobody references is lost when it is collected.
   */
  private static final java.util.List<Logger> NETTY_BOOTSTRAP = java.util.stream.Stream.of("AbstractBootstrap", "ServerBootstrap", "Bootstrap")
      .map(c -> Logger.getLogger("io.grpc.netty.shaded.io.netty.bootstrap." + c)).toList();

  static {
    for (Logger l : NETTY_BOOTSTRAP) l.setFilter(Server::notKeepAliveNoise);
  }

  /** False only for Netty's "unknown channel option SO_KEEPALIVE" warning. */
  static boolean notKeepAliveNoise(java.util.logging.LogRecord r) {
    String m = r.getMessage();
    return m == null || !(m.contains("SO_KEEPALIVE") && m.contains("Unknown channel option"));
  }

  /** The gRPC call-metadata key carrying the Windows session token. */
  public static final String SESSION_TOKEN_KEY = "vizi-session-token";

  private static final Metadata.Key<String> SESSION_TOKEN = Metadata.Key.of(SESSION_TOKEN_KEY, Metadata.ASCII_STRING_MARSHALLER);

  /**
   * The {@code Session} service, handing the call that proves it is the host to {@code session};
   * a second session is refused.
   *
   * <p>When the session has a token (Windows), a {@code 1.0} host sends it in {@code
   * Hello.session_token}: a call without the metadata key is checked against its first message, a
   * {@code Hello}, before it reaches the session. A host MAY also send the token as call metadata;
   * when that key is present it is checked before the call is accepted, and a wrong one is
   * refused. Both compares are constant-time. A call with a missing or wrong token is closed {@code UNAUTHENTICATED} and never
   * ends the session: the server keeps accepting calls until the host's arrives.
   */
  static ServerServiceDefinition service(Session session) {
    AtomicBoolean taken = new AtomicBoolean();
    String expected = session.token();
    ServerCallHandler<HostMsg, PluginMsg> handler = (call, headers) -> {
      Iterable<String> offered = headers.getAll(SESSION_TOKEN);
      if (expected != null && offered != null) {
        java.util.Iterator<String> it = offered.iterator();
        String first = it.next();
        if (it.hasNext() || !tokensEqual(first, expected)) return refuse(call);
      } else if (expected != null) {
        return new HelloGate(call, expected, () -> accept(call, taken, session));
      }
      ServerCall.Listener<HostMsg> accepted = accept(call, taken, session);
      if (accepted == null) return new ServerCall.Listener<>() {};
      call.sendHeaders(new Metadata());
      call.request(1);
      return accepted;
    };
    return ServerServiceDefinition.builder(PluginGrpc.SERVICE_NAME)
        .addMethod(PluginGrpc.getSessionMethod(), handler)
        .build();
  }

  /** Claims the session for an authenticated call, or closes it and returns null if it is taken. */
  private static ServerCall.@Nullable Listener<HostMsg> accept(
      ServerCall<HostMsg, PluginMsg> call, AtomicBoolean taken, Session session) {
    if (!taken.compareAndSet(false, true)) {
      call.close(Status.RESOURCE_EXHAUSTED.withDescription("a plugin process serves one session"), new Metadata());
      return null;
    }
    return session.attach(call);
  }

  private static ServerCall.Listener<HostMsg> refuse(ServerCall<HostMsg, PluginMsg> call) {
    call.close(Status.UNAUTHENTICATED.withDescription("session token missing or mismatched"), new Metadata());
    return new ServerCall.Listener<>() {};
  }

  /** Equality whose running time does not depend on where the tokens differ. */
  static boolean tokensEqual(String offered, String expected) {
    return java.security.MessageDigest.isEqual(
        offered.getBytes(StandardCharsets.US_ASCII), expected.getBytes(StandardCharsets.US_ASCII));
  }

  /**
   * A call without the metadata token: it reaches the session only once its {@code Hello} carries
   * the token. Until then nothing it does — a wrong token, another first message, a close or a
   * cancel — touches the session.
   */
  private static final class HelloGate extends ServerCall.Listener<HostMsg> {
    private final ServerCall<HostMsg, PluginMsg> call;
    private final String expected;
    private final Supplier<ServerCall.@Nullable Listener<HostMsg>> accept;
    private ServerCall.@Nullable Listener<HostMsg> session;
    private boolean closed;

    HelloGate(ServerCall<HostMsg, PluginMsg> call, String expected,
        Supplier<ServerCall.@Nullable Listener<HostMsg>> accept) {
      this.call = call;
      this.expected = expected;
      this.accept = accept;
      call.sendHeaders(new Metadata());
      call.request(1);
    }

    @Override
    public void onMessage(HostMsg m) {
      ServerCall.Listener<HostMsg> s = session;
      if (s != null) {
        s.onMessage(m);
        return;
      }
      if (closed) return;
      if (m.getMsgCase() != HostMsg.MsgCase.HELLO || !tokensEqual(m.getHello().getSessionToken(), expected)) {
        closed = true;
        refuse(call);
        return;
      }
      s = accept.get();
      if (s == null) {
        closed = true;
        return;
      }
      session = s;
      s.onMessage(m);
    }

    @Override
    public void onHalfClose() {
      ServerCall.Listener<HostMsg> s = session;
      if (s != null) s.onHalfClose();
    }

    @Override
    public void onCancel() {
      ServerCall.Listener<HostMsg> s = session;
      if (s != null) s.onCancel();
    }

    @Override
    public void onComplete() {
      ServerCall.Listener<HostMsg> s = session;
      if (s != null) s.onComplete();
    }

    @Override
    public void onReady() {
      ServerCall.Listener<HostMsg> s = session;
      if (s != null) s.onReady();
    }
  }

  /** Binds on a unix socket at {@code path}, atomically visible. */
  public static Server bindUnix(Path path, Session session) throws LaunchFailure {
    Path dir = path.toAbsolutePath().getParent();
    if (dir == null) throw new LaunchFailure("VIZI_PLUGIN_UDS has no directory: " + path);
    Path staging = dir.resolve(".v" + shortToken());
    ExecutorService dispatch = dispatchExecutor();
    NioEventLoopGroup boss = new NioEventLoopGroup(1);
    NioEventLoopGroup workers = new NioEventLoopGroup(2);
    io.grpc.Server s;
    try {
      Files.deleteIfExists(staging);
      s = NettyServerBuilder.forAddress(UnixDomainSocketAddress.of(staging))
          .channelType(NioServerDomainSocketChannel.class)
          .bossEventLoopGroup(boss)
          .workerEventLoopGroup(workers)
          .executor(dispatch)
          .maxInboundMessageSize(MAX_INBOUND)
          .addService(service(session))
          .build()
          .start();
    } catch (IOException | RuntimeException e) {
      shutdownGroups(dispatch, boss, workers);
      throw new LaunchFailure("binding the gRPC server on " + staging + " failed: " + e, e);
    }
    try {
      if (Files.exists(path, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
        Files.delete(path); // a stale socket from an earlier run: deleted deliberately
      }
      Files.createLink(path, staging);
      Files.delete(staging);
    } catch (IOException | RuntimeException e) {
      s.shutdownNow();
      shutdownGroups(dispatch, boss, workers);
      try {
        Files.deleteIfExists(staging);
      } catch (IOException ignored) {
        // best effort
      }
      throw new LaunchFailure("publishing the gRPC socket at " + path + " failed: " + e, e);
    }
    return new Server(s, dispatch, boss, workers, path.toString());
  }

  /** Windows: loopback TCP on an ephemeral port, published through the port file. */
  public static Server bindLoopback(Path portFile, Session session) throws LaunchFailure {
    ExecutorService dispatch = dispatchExecutor();
    NioEventLoopGroup boss = new NioEventLoopGroup(1);
    NioEventLoopGroup workers = new NioEventLoopGroup(2);
    io.grpc.Server s;
    try {
      s = NettyServerBuilder.forAddress(new InetSocketAddress("127.0.0.1", 0))
          .bossEventLoopGroup(boss)
          .workerEventLoopGroup(workers)
          .channelType(io.grpc.netty.shaded.io.netty.channel.socket.nio.NioServerSocketChannel.class)
          .executor(dispatch)
          .maxInboundMessageSize(MAX_INBOUND)
          .addService(service(session))
          .build()
          .start();
    } catch (IOException | RuntimeException e) {
      shutdownGroups(dispatch, boss, workers);
      throw new LaunchFailure("binding the gRPC server on 127.0.0.1 failed: " + e, e);
    }
    int port = s.getPort();
    try {
      Path dir = portFile.toAbsolutePath().getParent();
      Path tmp = Files.createTempFile(dir == null ? Path.of(".") : dir, ".vizi-port", ".tmp");
      Files.writeString(tmp, Integer.toString(port), StandardCharsets.US_ASCII);
      Files.move(tmp, portFile, StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
    } catch (IOException | RuntimeException e) {
      s.shutdownNow();
      shutdownGroups(dispatch, boss, workers);
      throw new LaunchFailure("publishing the port file " + portFile + " failed: " + e, e);
    }
    return new Server(s, dispatch, boss, workers, "127.0.0.1:" + port);
  }

  /** The dispatch thread: one, named, so a request made from it can be refused by identity. */
  private static ExecutorService dispatchExecutor() {
    return Executors.newSingleThreadExecutor(r -> {
      Thread t = new Thread(r, Session.DISPATCH_THREAD);
      t.setDaemon(true);
      return t;
    });
  }

  private static String shortToken() {
    byte[] b = new byte[4];
    new SecureRandom().nextBytes(b);
    return java.util.HexFormat.of().formatHex(b);
  }

  private static void shutdownGroups(ExecutorService dispatch, NioEventLoopGroup boss, NioEventLoopGroup workers) {
    dispatch.shutdownNow();
    boss.shutdownGracefully(0, 0, TimeUnit.MILLISECONDS);
    workers.shutdownGracefully(0, 0, TimeUnit.MILLISECONDS);
  }

  @Override
  public void close() {
    grpc.shutdown();
    try {
      if (!grpc.awaitTermination(1, TimeUnit.SECONDS)) grpc.shutdownNow();
    } catch (InterruptedException e) {
      grpc.shutdownNow();
      Thread.currentThread().interrupt();
    }
    shutdownGroups(dispatch, boss, workers);
  }
}
