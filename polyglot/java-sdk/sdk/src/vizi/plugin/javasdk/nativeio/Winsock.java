package vizi.plugin.javasdk.nativeio;

import static java.lang.foreign.ValueLayout.ADDRESS;
import static java.lang.foreign.ValueLayout.JAVA_BYTE;
import static java.lang.foreign.ValueLayout.JAVA_INT;
import static java.lang.foreign.ValueLayout.JAVA_LONG;
import static java.lang.foreign.ValueLayout.JAVA_SHORT;

import java.lang.foreign.Arena;
import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.MemoryLayout;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.StructLayout;
import java.lang.foreign.SymbolLookup;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.VarHandle;

/**
 * Winsock downcalls for the Windows doorbell: a loopback TCP stream (doorbell_protocol.md §3.2).
 *
 * <p>Winsock rather than NIO because the transport classifies every send and receive error by
 * code (§6 rules 6–7, §7 rule 5), and NIO reports them only as localized message text. A
 * {@code SOCKET} is a pointer-sized handle; it is carried as a {@code long}.
 *
 * <p><b>Not yet run anywhere</b>: this class compiles on every platform and is initialised only
 * on Windows, where no session has yet been served with it.
 */
public final class Winsock {
  private Winsock() {}

  public static final long INVALID_SOCKET = -1L;
  public static final int AF_INET = 2;
  public static final int SOCK_STREAM = 1;
  public static final int IPPROTO_TCP = 6;
  public static final int TCP_NODELAY = 1;
  public static final int SOL_SOCKET = 0xffff;
  public static final int SO_TYPE = 0x1008;
  public static final int FIONBIO = 0x8004667E;
  public static final int WSA_FLAG_OVERLAPPED = 0x01;
  /** The socket's handle is not inherited by a child process (Windows 7 SP1 and later). */
  public static final int WSA_FLAG_NO_HANDLE_INHERIT = 0x80;
  /** {@code ~SO_REUSEADDR}: no other socket may bind the same address, even with SO_REUSEADDR. */
  public static final int SO_EXCLUSIVEADDRUSE = ~4;
  public static final int SD_SEND = 1;
  public static final short POLLRDNORM = 0x0100;
  public static final short POLLRDBAND = 0x0200;
  public static final short POLLIN = (short) (POLLRDNORM | POLLRDBAND);
  public static final short POLLERR = 0x0001;
  public static final short POLLHUP = 0x0002;
  public static final short POLLNVAL = 0x0004;

  private static final Linker LINKER = Linker.nativeLinker();
  private static final StructLayout CAPTURE = Linker.Option.captureStateLayout();
  private static final VarHandle WSA_ERROR =
      CAPTURE.varHandle(MemoryLayout.PathElement.groupElement("WSAGetLastError"));

  private static final class H {
    static final SymbolLookup WS2 = SymbolLookup.libraryLookup("ws2_32", Arena.global());

    static MethodHandle fn(String name, FunctionDescriptor fd) {
      return LINKER.downcallHandle(
          WS2.findOrThrow(name), fd, Linker.Option.captureCallState("WSAGetLastError"));
    }

    static final MethodHandle WSASTARTUP =
        LINKER.downcallHandle(WS2.findOrThrow("WSAStartup"), FunctionDescriptor.of(JAVA_INT, JAVA_SHORT, ADDRESS));
    static final MethodHandle WSASOCKETW =
        fn("WSASocketW", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, JAVA_INT, JAVA_INT, ADDRESS, JAVA_INT, JAVA_INT));
    static final MethodHandle CONNECT = fn("connect", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, JAVA_INT));
    static final MethodHandle BIND = fn("bind", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, JAVA_INT));
    static final MethodHandle LISTEN = fn("listen", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, JAVA_INT));
    static final MethodHandle ACCEPT = fn("accept", FunctionDescriptor.of(JAVA_LONG, JAVA_LONG, ADDRESS, ADDRESS));
    static final MethodHandle GETSOCKNAME =
        fn("getsockname", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, ADDRESS));
    static final MethodHandle GETPEERNAME =
        fn("getpeername", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, ADDRESS));
    static final MethodHandle SEND = fn("send", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, JAVA_INT, JAVA_INT));
    static final MethodHandle RECV = fn("recv", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, ADDRESS, JAVA_INT, JAVA_INT));
    static final MethodHandle CLOSESOCKET = fn("closesocket", FunctionDescriptor.of(JAVA_INT, JAVA_LONG));
    static final MethodHandle SHUTDOWN = fn("shutdown", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, JAVA_INT));
    static final MethodHandle IOCTLSOCKET =
        fn("ioctlsocket", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, JAVA_INT, ADDRESS));
    static final MethodHandle SETSOCKOPT =
        fn("setsockopt", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, JAVA_INT, JAVA_INT, ADDRESS, JAVA_INT));
    static final MethodHandle GETSOCKOPT =
        fn("getsockopt", FunctionDescriptor.of(JAVA_INT, JAVA_LONG, JAVA_INT, JAVA_INT, ADDRESS, ADDRESS));
    static final MethodHandle WSAPOLL = fn("WSAPoll", FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_INT, JAVA_INT));

    static {
      try (Arena a = Arena.ofConfined()) {
        int r = (int) WSASTARTUP.invokeExact((short) 0x0202, a.allocate(1024, 8));
        if (r != 0) throw new ExceptionInInitializerError("WSAStartup failed: " + r);
      } catch (Throwable t) {
        throw new ExceptionInInitializerError(t);
      }
    }
  }

  private static int err(MemorySegment cap) {
    return (int) WSA_ERROR.get(cap, 0L);
  }

  private static RuntimeException linkage(Throwable t) {
    if (t instanceof RuntimeException r) return r;
    if (t instanceof Error e) throw e;
    return new IllegalStateException("native call failed", t);
  }

  private static MemorySegment sockaddrIn(Arena a, int port) {
    MemorySegment sa = a.allocate(16, 4);
    sa.set(JAVA_SHORT, 0, (short) AF_INET);
    sa.set(JAVA_BYTE, 2, (byte) (port >>> 8));
    sa.set(JAVA_BYTE, 3, (byte) port);
    sa.set(JAVA_BYTE, 4, (byte) 127);
    sa.set(JAVA_BYTE, 7, (byte) 1);
    return sa;
  }

  /**
   * A TCP socket whose handle a child process does not inherit: {@code socket()} makes an
   * inheritable handle, and the JDK's process launcher passes every inheritable handle on.
   */
  private static long tcpSocket(MemorySegment cap) throws Throwable {
    long s = (long) H.WSASOCKETW.invokeExact(cap, AF_INET, SOCK_STREAM, IPPROTO_TCP, MemorySegment.NULL, 0,
        WSA_FLAG_OVERLAPPED | WSA_FLAG_NO_HANDLE_INHERIT);
    if (s == INVALID_SOCKET) throw new OsException("WSASocketW", err(cap));
    return s;
  }

  private static int port(MemorySegment sockaddrIn) {
    return ((sockaddrIn.get(JAVA_BYTE, 2) & 0xff) << 8) | (sockaddrIn.get(JAVA_BYTE, 3) & 0xff);
  }

  private static int portOf(MethodHandle nameFn, MemorySegment cap, Arena a, long s, String what) throws Throwable {
    MemorySegment name = a.allocate(16, 4);
    MemorySegment len = a.allocate(JAVA_INT);
    len.set(JAVA_INT, 0, 16);
    if ((int) nameFn.invokeExact(cap, s, name, len) != 0) throw new OsException(what, err(cap));
    return port(name);
  }

  /** A blocking TCP connection to {@code 127.0.0.1:port}, not inheritable. */
  public static long connectLoopback(int port) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      long s = tcpSocket(cap);
      int r = (int) H.CONNECT.invokeExact(cap, s, sockaddrIn(a, port), 16);
      if (r != 0) {
        int e = err(cap);
        closesocket(s);
        throw new OsException("connect", e);
      }
      return s;
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * A connected loopback TCP pair, the portable wake endpoint (§6 rule 3: a pipe is not a socket
   * and {@code WSAPoll} cannot wait on one).
   */
  public static long[] loopbackPair() throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      long l = tcpSocket(cap);
      try {
        MemorySegment one = a.allocate(JAVA_INT);
        one.set(JAVA_INT, 0, 1);
        if ((int) H.SETSOCKOPT.invokeExact(cap, l, SOL_SOCKET, SO_EXCLUSIVEADDRUSE, one, 4) != 0) {
          throw new OsException("setsockopt(SO_EXCLUSIVEADDRUSE)", err(cap));
        }
        if ((int) H.BIND.invokeExact(cap, l, sockaddrIn(a, 0), 16) != 0) throw new OsException("bind", err(cap));
        if ((int) H.LISTEN.invokeExact(cap, l, 4) != 0) throw new OsException("listen", err(cap));
        int port = portOf(H.GETSOCKNAME, cap, a, l, "getsockname");
        long c = connectLoopback(port);
        int mine = portOf(H.GETSOCKNAME, cap, a, c, "getsockname");
        // any local process can reach the port before we accept: take only our own connector
        for (int tries = 0; tries < 16; tries++) {
          long s = (long) H.ACCEPT.invokeExact(cap, l, MemorySegment.NULL, MemorySegment.NULL);
          if (s == INVALID_SOCKET) {
            int e = err(cap);
            closesocket(c);
            throw new OsException("accept", e);
          }
          if (portOf(H.GETPEERNAME, cap, a, s, "getpeername") == mine) {
            Kernel32.clearInherit(s); // an accepted socket inherits the listener's attributes; assert it
            return new long[] {s, c};
          }
          closesocket(s); // a stranger
        }
        closesocket(c);
        throw new OsException("accept (the wake pair's own connection never arrived)", 0);
      } finally {
        closesocket(l);
      }
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** send(); returns the count or -1 with the code in {@code out[0]}. */
  public static int send(long s, byte[] data, int off, int len, int[] errOut) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(Math.max(1, len));
      MemorySegment.copy(data, off, buf, JAVA_BYTE, 0, len);
      int n = (int) H.SEND.invokeExact(cap, s, buf, len, 0);
      if (n < 0) errOut[0] = err(cap);
      return n;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** recv() into {@code out[off..off+len)}; returns the count or -1 with the code in {@code errOut[0]}. */
  public static int recv(long s, byte[] out, int off, int len, int[] errOut) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(Math.max(1, len));
      int n = (int) H.RECV.invokeExact(cap, s, buf, len, 0);
      if (n < 0) {
        errOut[0] = err(cap);
        return n;
      }
      MemorySegment.copy(buf, JAVA_BYTE, 0, out, off, n);
      return n;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void setNonBlocking(long s) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment mode = a.allocate(JAVA_INT);
      mode.set(JAVA_INT, 0, 1);
      if ((int) H.IOCTLSOCKET.invokeExact(cap, s, FIONBIO, mode) != 0) throw new OsException("ioctlsocket", err(cap));
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void setNoDelay(long s) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment one = a.allocate(JAVA_INT);
      one.set(JAVA_INT, 0, 1);
      if ((int) H.SETSOCKOPT.invokeExact(cap, s, IPPROTO_TCP, TCP_NODELAY, one, 4) != 0) {
        throw new OsException("setsockopt(TCP_NODELAY)", err(cap));
      }
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * The socket type, read as a 4-byte int. (Winsock answers {@code TCP_NODELAY} with one byte, so
   * a caller reading a boolean option must not assume the documented size; this reads SO_TYPE.)
   */
  public static int socketType(long s) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment v = a.allocate(JAVA_INT);
      MemorySegment len = a.allocate(JAVA_INT);
      len.set(JAVA_INT, 0, 4);
      if ((int) H.GETSOCKOPT.invokeExact(cap, s, SOL_SOCKET, SO_TYPE, v, len) != 0) {
        throw new OsException("getsockopt(SO_TYPE)", err(cap));
      }
      return v.get(JAVA_INT, 0);
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** WSAPoll for readability; revents filled; returns count, 0 on timeout, or the code negated. */
  public static int poll(long[] sockets, short[] revents, int timeoutMs) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment p = a.allocate(16L * sockets.length, 8);
      for (int i = 0; i < sockets.length; i++) {
        p.set(JAVA_LONG, 16L * i, sockets[i]);
        p.set(JAVA_SHORT, 16L * i + 8, POLLIN);
      }
      int r = (int) H.WSAPOLL.invokeExact(cap, p, sockets.length, timeoutMs);
      if (r < 0) return -err(cap);
      for (int i = 0; i < sockets.length; i++) revents[i] = p.get(JAVA_SHORT, 16L * i + 10);
      return r;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void shutdownSend(long s) {
    try (Arena a = Arena.ofConfined()) {
      int ignored = (int) H.SHUTDOWN.invokeExact(a.allocate(CAPTURE), s, SD_SEND);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static int closesocket(long s) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.CLOSESOCKET.invokeExact(cap, s);
      return r == 0 ? 0 : err(cap);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }
}
