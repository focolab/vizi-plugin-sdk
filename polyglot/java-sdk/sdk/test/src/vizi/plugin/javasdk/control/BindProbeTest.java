package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.IOException;
import java.net.StandardProtocolFamily;
import java.net.UnixDomainSocketAddress;
import java.nio.channels.SocketChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Semaphore;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.nativeio.Platform;

/**
 * A unix socket path is visible before {@code listen()}, so a host connecting the
 * moment it appears is refused. Sixty servers bind, each watched from the moment its bind starts
 * by a thread that connects the instant the path exists; the staging bind plus hard link must
 * leave none refused.
 *
 * <p>Each watcher waits on a latch its bind counts down just before binding, then spins (yielding)
 * until the path exists or the bind returns: it never parks, so it cannot miss the window. Binds
 * run a few at a time, not sixty at once, so the watchers do not starve the test JVMs beside
 * them on a small runner.
 */
class BindProbeTest {

  static final class Quiet implements Application {
    @Override
    public Manifest manifest() {
      return Manifest.builder("org.test.bind", "Bind", "0.1").entryCommand("./run").build();
    }

    @Override
    public String versionString() {
      return "org.test.bind 0.1";
    }
  }

  @Test
  void sixtyBindsAreNeverRefused() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    int n = 60;
    int inFlight = Math.max(2, Runtime.getRuntime().availableProcessors() / 2);
    Path dir = Files.createTempDirectory("vizi-bind");
    Semaphore slots = new Semaphore(inFlight);
    AtomicInteger refused = new AtomicInteger();
    AtomicInteger connected = new AtomicInteger();
    ConcurrentLinkedQueue<Server> servers = new ConcurrentLinkedQueue<>();
    ConcurrentLinkedQueue<String> problems = new ConcurrentLinkedQueue<>();
    List<Thread> threads = new ArrayList<>();
    for (int i = 0; i < n; i++) {
      Path path = dir.resolve("p" + i + ".sock");
      CountDownLatch binding = new CountDownLatch(1);
      AtomicBoolean bound = new AtomicBoolean();
      slots.acquire();
      // the watcher first, so it is spinning before the bind can publish anything
      threads.add(Thread.ofPlatform().start(() -> {
        try {
          binding.await();
          // Spins (yielding, so a one-core runner still runs the bind) until the path exists or
          // the bind has returned without publishing one.
          while (!Files.exists(path) && !bound.get()) Thread.yield();
          try (SocketChannel c = SocketChannel.open(StandardProtocolFamily.UNIX)) {
            c.connect(UnixDomainSocketAddress.of(path));
            connected.incrementAndGet();
          } catch (IOException e) {
            refused.incrementAndGet();
            problems.add("connect " + path + ": " + e);
          }
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        } finally {
          slots.release();
        }
      }));
      threads.add(Thread.ofPlatform().start(() -> {
        try {
          binding.countDown();
          servers.add(Server.bindUnix(path, new Session(new Quiet(), Environment.of(Map.of()), "test", null)));
        } catch (Exception e) {
          problems.add("bind " + path + ": " + e);
        } finally {
          bound.set(true);
        }
      }));
    }
    for (Thread t : threads) t.join();
    for (Server s : servers) s.close();
    assertEquals(List.of(), List.copyOf(problems));
    assertEquals(0, refused.get());
    assertEquals(n, connected.get());
  }
}
