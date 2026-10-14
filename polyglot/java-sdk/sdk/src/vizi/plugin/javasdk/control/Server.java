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
import java.util.logging.Logger;
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

  /** The {@code Session} service, handing each call to {@code session}; a second call is refused. */
  static ServerServiceDefinition service(Session session) {
    AtomicBoolean taken = new AtomicBoolean();
    ServerCallHandler<HostMsg, PluginMsg> handler = (call, headers) -> {
      if (!taken.compareAndSet(false, true)) {
        call.close(Status.RESOURCE_EXHAUSTED.withDescription("a plugin process serves one session"), new Metadata());
        return new ServerCall.Listener<>() {};
      }
      call.sendHeaders(new Metadata());
      call.request(1);
      return session.attach(call);
    };
    return ServerServiceDefinition.builder(PluginGrpc.SERVICE_NAME)
        .addMethod(PluginGrpc.getSessionMethod(), handler)
        .build();
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
