package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import io.grpc.CallOptions;
import io.grpc.ClientCall;
import io.grpc.ManagedChannel;
import io.grpc.Metadata;
import io.grpc.Status;
import io.grpc.netty.shaded.io.grpc.netty.NettyChannelBuilder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeUnit;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.v1.PluginGrpc;
import vizi.plugin.v1.ViziPlugin.Hello;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.PluginMsg;

/**
 * The Windows session token over loopback TCP, the Windows transport: a call with no token or a
 * wrong one is refused UNAUTHENTICATED and the session stays open for the host.
 */
class SessionTokenTest {

  static final String TOKEN = "0123456789abcdef0123456789abcdef";
  static final String WRONG = "fedcba9876543210fedcba9876543210";

  static final class Quiet implements Application {
    @Override
    public Manifest manifest() {
      return Manifest.builder("org.test.token", "Token", "0.1").entryCommand("./run").build();
    }

    @Override
    public String versionString() {
      return "org.test.token 0.1";
    }
  }

  Session session;
  Server server;
  ManagedChannel channel;

  @BeforeEach
  void start() throws Exception {
    Path dir = Files.createTempDirectory("vizi-token");
    session = new Session(new Quiet(), Environment.of(Map.of()), "test", TOKEN);
    server = Server.bindLoopback(dir.resolve("port"), session);
    int port = Integer.parseInt(Files.readString(dir.resolve("port")).trim());
    channel = NettyChannelBuilder.forAddress("127.0.0.1", port).usePlaintext().overrideAuthority("localhost").build();
  }

  @AfterEach
  void stop() throws Exception {
    channel.shutdownNow().awaitTermination(5, TimeUnit.SECONDS);
    server.close();
  }

  /** The first message of a call that sends Hello, or the status it closed with. */
  private Object attempt(@Nullable String metadataToken, String helloToken) throws Exception {
    CompletableFuture<Object> first = new CompletableFuture<>();
    ClientCall<HostMsg, PluginMsg> call = channel.newCall(PluginGrpc.getSessionMethod(), CallOptions.DEFAULT);
    Metadata headers = new Metadata();
    if (metadataToken != null) {
      headers.put(Metadata.Key.of(Server.SESSION_TOKEN_KEY, Metadata.ASCII_STRING_MARSHALLER), metadataToken);
    }
    call.start(new ClientCall.Listener<>() {
      @Override
      public void onMessage(PluginMsg m) {
        first.complete(m);
      }

      @Override
      public void onClose(Status status, Metadata trailers) {
        first.complete(status);
      }
    }, headers);
    call.request(1);
    call.sendMessage(HostMsg.newBuilder()
        .setId("Hello from token fixture")
        .setHello(Hello.newBuilder().setProtocolMajor(1).setSessionToken(helloToken))
        .build());
    return first.get(5, TimeUnit.SECONDS);
  }

  private static void assertRefused(Object result) {
    assertTrue(result instanceof Status s && s.getCode() == Status.Code.UNAUTHENTICATED, String.valueOf(result));
  }

  private void assertWaiting() {
    assertFalse(session.ended().isDone(), "a refused call ended the session");
  }

  @Test
  void metadataTokenRefusesBadCallsWithoutEndingTheSession() throws Exception {
    assertRefused(attempt(null, ""));
    assertWaiting();
    // a wrong metadata token is refused even when Hello carries the right one
    assertRefused(attempt(WRONG, TOKEN));
    assertWaiting();
    Object ready = attempt(TOKEN, "");
    assertTrue(ready instanceof PluginMsg m && m.hasReady(), String.valueOf(ready));
    Object second = attempt(TOKEN, "");
    assertTrue(second instanceof Status s && s.getCode() == Status.Code.RESOURCE_EXHAUSTED, String.valueOf(second));
  }

  @Test
  void helloTokenIsStillAcceptedWithoutMetadata() throws Exception {
    assertRefused(attempt(null, WRONG));
    assertWaiting();
    assertRefused(attempt("", TOKEN));
    assertWaiting();
    Object ready = attempt(null, TOKEN);
    assertTrue(ready instanceof PluginMsg m && m.hasReady(), String.valueOf(ready));
  }

  @Test
  void tokenEqualityCoversLengthAndContent() {
    assertTrue(Server.tokensEqual(TOKEN, TOKEN));
    assertFalse(Server.tokensEqual(WRONG, TOKEN));
    assertFalse(Server.tokensEqual(TOKEN.substring(1), TOKEN));
    assertEquals(32, TOKEN.length());
  }
}
