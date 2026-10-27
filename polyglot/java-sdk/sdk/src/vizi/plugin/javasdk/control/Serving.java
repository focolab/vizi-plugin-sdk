package vizi.plugin.javasdk.control;

import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.Doorbell;
import vizi.plugin.javasdk.doorbell.LaunchFailure;
import vizi.plugin.javasdk.nativeio.Platform;

/**
 * Brings a session up in the order the protocol requires (control_plane_protocol.md §2.1,
 * doorbell_protocol.md §3.2): adopt the doorbell and start its listener, then bind the gRPC
 * endpoint — readiness — so that no session-setup grant can arrive before something is draining.
 */
public final class Serving implements AutoCloseable {

  private final Session session;
  private final Doorbell doorbell;
  private final Server server;

  private Serving(Session session, Doorbell doorbell, Server server) {
    this.session = session;
    this.doorbell = doorbell;
    this.server = server;
  }

  /** From the launch environment. Every setup failure is a {@link LaunchFailure}. */
  public static Serving start(Application app, Map<String, String> env, String manifestSource) throws LaunchFailure {
    String token = Platform.OS == Platform.WINDOWS ? env.get("VIZI_PLUGIN_TOKEN") : null;
    Session session = new Session(app, Environment.of(env), manifestSource, token);
    Doorbell d = Doorbell.fromEnvironment(env, session.doorbellListener(), session::onDoorbellTerminal);
    session.attachDoorbell(d);
    Server s;
    try {
      if (Platform.OS == Platform.WINDOWS) {
        String portFile = env.get("VIZI_PLUGIN_PORT_FILE");
        if (portFile == null || portFile.isBlank()) throw new LaunchFailure("VIZI_PLUGIN_PORT_FILE is not set; the plugin must be launched by vizi");
        s = Server.bindLoopback(Path.of(portFile), session);
      } else {
        String uds = env.get("VIZI_PLUGIN_UDS");
        if (uds == null || uds.isBlank()) throw new LaunchFailure("VIZI_PLUGIN_UDS is not set; the plugin must be launched by vizi");
        s = Server.bindUnix(Path.of(uds), session);
      }
    } catch (LaunchFailure e) {
      d.stop();
      throw e;
    }
    return new Serving(session, d, s);
  }

  /** In-process, over a doorbell descriptor the caller made (tests and emulators). */
  public static Serving startForTest(Application app, int doorbellFd, Path uds, Environment env, @Nullable String token)
      throws LaunchFailure {
    Session session = new Session(app, env, "test", token);
    Doorbell d = Doorbell.adoptForTest(doorbellFd, session.doorbellListener(), session::onDoorbellTerminal);
    session.attachDoorbell(d);
    Server s;
    try {
      s = Server.bindUnix(uds, session);
    } catch (LaunchFailure e) {
      d.stop();
      throw e;
    }
    return new Serving(session, d, s);
  }

  public Session session() {
    return session;
  }

  public Doorbell doorbell() {
    return doorbell;
  }

  public String endpoint() {
    return server.endpoint();
  }

  /** Completes when the session ends, however it ended. */
  public CompletableFuture<String> ended() {
    return session.ended();
  }

  @Override
  public void close() {
    session.endSession("closed");
    doorbell.stop();
    server.close();
  }
}
