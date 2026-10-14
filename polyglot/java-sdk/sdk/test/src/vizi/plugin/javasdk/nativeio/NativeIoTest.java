package vizi.plugin.javasdk.nativeio;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.IOException;
import java.io.InputStream;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.Test;

/** Exercises every POSIX downcall at least once, on the platform the test runs on. */
class NativeIoTest {

  private static void unixOnly() {
    assumeTrue(Platform.isUnix(), "POSIX downcalls");
  }

  @Test
  void seqpacketPassesOneDescriptorCloseOnExec() throws Exception {
    unixOnly();
    int type = Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM;
    int[] sv = Posix.socketpair(type);
    int shared = Posix.createSharedObject("nativeio-test", 4096);
    try {
      assertEquals(type, Posix.getsockoptInt(sv[1], Posix.SOL_SOCKET, Posix.SO_TYPE));
      assertTrue(Posix.isCloexec(sv[0]));
      Posix.setNonBlocking(sv[1]);
      assertTrue(Posix.isNonBlocking(sv[1]));

      Posix.Sent s = Posix.sendmsg(sv[0], new byte[112], shared);
      assertEquals(112, s.n());
      Posix.Received r = Posix.recvmsg(sv[1], 120);
      assertEquals(112, r.n());
      assertFalse(r.ctrunc());
      assertEquals(1, r.fds().length);
      assertTrue(Posix.isCloexec(r.fds()[0]), "a received descriptor is never inheritable (§7.6)");
      assertEquals(4096, Posix.objectSize(r.fds()[0]));
      assertEquals(0, Posix.close(r.fds()[0]));
    } finally {
      Posix.close(shared);
      Posix.close(sv[0]);
      Posix.close(sv[1]);
    }
  }

  @Test
  void twoDescriptorsAreCountedAndAManyDescriptorMessageTruncates() throws Exception {
    unixOnly();
    int type = Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM;
    int[] sv = Posix.socketpair(type);
    int[] pair = Posix.socketpair(Posix.SOCK_DGRAM);
    try {
      assertEquals(112, Posix.sendmsg(sv[0], new byte[112], pair[0], pair[1]).n());
      Posix.Received two = Posix.recvmsg(sv[1], 120);
      assertEquals(2, two.fds().length);
      assertFalse(two.ctrunc());
      for (int f : two.fds()) Posix.close(f);

      // the SDK's buffer is the kernel's SCM_MAX_FD, so a sender cannot overrun it: overrun a small one
      int[] many = new int[6];
      for (int i = 0; i < many.length; i++) many[i] = pair[i % 2];
      assertEquals(112, Posix.sendmsg(sv[0], new byte[112], many).n());
      Posix.Received trunc = Posix.recvmsg(sv[1], 120, 2);
      assertTrue(trunc.ctrunc(), "MSG_CTRUNC read through the platform constant");
      for (int f : trunc.fds()) Posix.close(f);
    } finally {
      Posix.close(sv[0]);
      Posix.close(sv[1]);
      Posix.close(pair[0]);
      Posix.close(pair[1]);
    }
  }

  @Test
  void anOversizedDatagramFillsTheBufferAndAZeroByteOneIsZero() throws Exception {
    unixOnly();
    int type = Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM;
    int[] sv = Posix.socketpair(type);
    try {
      assertEquals(200, Posix.sendmsg(sv[0], new byte[200]).n());
      assertEquals(120, Posix.recvmsg(sv[1], 120).n(), "never split, never coalesced");
      assertEquals(112, Posix.sendmsg(sv[0], new byte[112]).n());
      assertEquals(112, Posix.sendmsg(sv[0], new byte[112]).n());
      assertEquals(112, Posix.recvmsg(sv[1], 224).n(), "two queued records are two reads");
      assertEquals(112, Posix.recvmsg(sv[1], 224).n());
    } finally {
      Posix.close(sv[0]);
      Posix.close(sv[1]);
    }
  }

  @Test
  void errnosAreTypedWouldBlockAndPeerGone() throws Exception {
    unixOnly();
    int type = Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM;
    int[] sv = Posix.socketpair(type);
    Posix.setNonBlocking(sv[1]);
    Posix.Received empty = Posix.recvmsg(sv[1], 120);
    assertEquals(-1, empty.n());
    assertTrue(Errno.isWouldBlock(empty.errno()), Errno.describe(empty.errno()));
    Posix.close(sv[0]);
    Posix.Sent s = Posix.sendmsg(sv[1], new byte[112]);
    assertEquals(-1, s.n());
    assertTrue(Errno.isPeerGone(s.errno()), Errno.describe(s.errno()));
    if (Platform.OS == Platform.LINUX) {
      assertEquals(0, Posix.recvmsg(sv[1], 120).n(), "SEQPACKET peer close reads as 0 bytes");
    }
    Posix.close(sv[1]);
    OsException bad = assertThrows(OsException.class, () -> Posix.fcntl(sv[1], Posix.F_GETFD, 0));
    assertEquals(Errno.EBADF, bad.code());
    assertEquals("fcntl", bad.call());
  }

  @Test
  void pollSeesReadabilityAndTimesOut() throws Exception {
    unixOnly();
    int[] sv = Posix.socketpair(Posix.SOCK_STREAM);
    try {
      short[] rev = new short[1];
      assertEquals(0, Posix.poll(new int[] {sv[1]}, rev, 10));
      assertEquals(1, Posix.send(sv[0], new byte[] {7}, 0, 1, Posix.MSG_DONTWAIT | Posix.MSG_NOSIGNAL).n());
      assertEquals(1, Posix.poll(new int[] {sv[1]}, rev, 1000));
      assertTrue((rev[0] & Posix.POLLIN) != 0);
      byte[] got = new byte[4];
      assertEquals(1, Posix.recv(sv[1], got, 0, 4, 0).n());
      assertEquals(7, got[0]);
    } finally {
      Posix.close(sv[0]);
      Posix.close(sv[1]);
    }
  }

  @Test
  void socketOptionsAndDup() throws Exception {
    unixOnly();
    int[] sv = Posix.socketpair(Posix.SOCK_DGRAM);
    try {
      Posix.setsockoptInt(sv[0], Posix.SOL_SOCKET, Posix.SO_RCVBUF, 65536);
      assertTrue(Posix.getsockoptInt(sv[0], Posix.SOL_SOCKET, Posix.SO_RCVBUF) >= 65536);
      if (Platform.OS == Platform.LINUX) {
        OsException e =
            assertThrows(OsException.class, () -> Posix.setsockoptInt(sv[0], Posix.SOL_SOCKET, Posix.SO_NOSIGPIPE, 1));
        assertEquals(Errno.ENOPROTOOPT, e.code(), "Linux refuses SO_NOSIGPIPE (§6 rule 4)");
      } else {
        Posix.setsockoptInt(sv[0], Posix.SOL_SOCKET, Posix.SO_NOSIGPIPE, 1);
      }
      int d = Posix.dup(sv[0]);
      assertNotEquals(sv[0], d);
      assertEquals(0, Posix.close(d));
    } finally {
      Posix.close(sv[0]);
      Posix.close(sv[1]);
    }
  }

  @Test
  void twoMappingsOfOneObjectSeeEachOthersAtomics() throws Exception {
    unixOnly();
    int fd = Posix.createSharedObject("atomics", 4096);
    int fd2 = Posix.unlinkedTempFile(64);
    try {
      assertEquals(64, Posix.objectSize(fd2));
      MemorySegment a = Posix.mmap(fd, 4096, true);
      MemorySegment b = Posix.mmap(fd, 4096, true);
      assertNotEquals(a.address(), b.address());
      assertTrue(Atomics.compareAndSet(a, 8, 0, 0xFEED));
      Atomics.storeRelease(a, 16, 42);
      assertEquals(0xFEED, Atomics.loadAcquire(b, 8));
      assertEquals(42, Atomics.loadAcquire(b, 16));
      assertEquals(0xFEED, Atomics.compareAndExchange(b, 8, 0, 1), "a stale expectation fails");
      MemorySegment ro = Posix.mmap(fd, 4096, false).asReadOnly();
      assertEquals(42, ro.get(ValueLayout.JAVA_LONG, 16));
      assertThrows(IllegalArgumentException.class, () -> ro.set(ValueLayout.JAVA_LONG, 16, 1));
      assertEquals(0, Posix.munmap(a));
      assertEquals(0, Posix.munmap(b));
      assertEquals(0, Posix.munmap(ro));
    } finally {
      Posix.close(fd);
      Posix.close(fd2);
    }
  }

  @Test
  void crossProcessAtomicsAgainstPythonAtomicshm() throws Exception {
    unixOnly();
    String repo = System.getenv("VIZI_SDK_REPO");
    assumeTrue(repo != null, "VIZI_SDK_REPO");
    Path python = Path.of(repo, ".venv", "bin", "python");
    assumeTrue(Files.isExecutable(python), "the repository's .venv (with atomicshm)");
    Path script = Files.createTempFile("atomic_peer", ".py");
    try (InputStream in = NativeIoTest.class.getResourceAsStream("/atomic_peer.py")) {
      Files.write(script, requireNonNull(in).readAllBytes());
    }
    Posix.NamedFile f = Posix.namedTempFile(4096);
    int rounds = 2000;
    try {
      MemorySegment m = Posix.mmap(f.fd(), 4096, true);
      Process p =
          new ProcessBuilder(python.toString(), script.toString(), f.path(), Integer.toString(rounds))
              .redirectErrorStream(true)
              .start();
      Atomics.storeRelease(m, 8, 12345);
      assertTrue(Atomics.compareAndSet(m, 0, 0, 1));
      long deadline = System.nanoTime() + TimeUnit.SECONDS.toNanos(20);
      while (Atomics.loadAcquire(m, 0) != 2) {
        assertTrue(System.nanoTime() < deadline, "peer never handed back");
        Thread.onSpinWait();
      }
      assertEquals(777, Atomics.loadAcquire(m, 16));
      int done = 0;
      while (done < rounds) {
        long cur = Atomics.loadAcquire(m, 24);
        if (cur % 2 == 0) {
          assertEquals(cur, Atomics.compareAndExchange(m, 24, cur, cur + 1));
          done++;
        } else {
          assertTrue(System.nanoTime() < deadline, "ping-pong stalled at " + cur);
          Thread.onSpinWait();
        }
      }
      assertTrue(p.waitFor(20, TimeUnit.SECONDS));
      String out = new String(p.getInputStream().readAllBytes(), StandardCharsets.UTF_8);
      assertEquals(0, p.exitValue(), out);
      assertEquals(2L * rounds, Atomics.loadAcquire(m, 24));
      Posix.munmap(m);
    } finally {
      Posix.close(f.fd());
      Files.deleteIfExists(Path.of(f.path()));
      Files.deleteIfExists(script);
    }
  }

  private static <T> T requireNonNull(T t) throws IOException {
    if (t == null) throw new IOException("missing test resource");
    return t;
  }
}
