package vizi.plugin.javasdk.doorbell;

import vizi.plugin.javasdk.nativeio.Errno;
import vizi.plugin.javasdk.nativeio.OsException;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.javasdk.nativeio.Winsock;

/**
 * The doorbell socket and its wake endpoint, for one transport class. The socket is
 * non-blocking from setup, so the listener is readiness-polled everywhere and something must
 * wake it: a socket pair polled beside the doorbell (doorbell_protocol.md §6 rule 3). Nothing
 * here classifies an error; it returns the code.
 */
abstract sealed class Wire permits Wire.UnixMessage, Wire.UnixStream, Wire.WinsockStream {

  static final int READABLE = 1;
  static final int WOKEN = 2;

  abstract TransportClass transportClass();

  /** One receive: a datagram (message classes) or up to a buffer of bytes (stream). */
  abstract Posix.Received receive();

  /** One send call, never looped. */
  abstract Posix.Sent send(byte[] data, int... fds);

  /**
   * Waits for the doorbell to be readable or the wake to fire. Returns a mask of {@link
   * #READABLE} and {@link #WOKEN}, 0 on timeout, or a negated error code.
   */
  abstract int await(int timeoutMs);

  /** Thread-safe: makes {@link #await} return. */
  abstract void wake();

  /** Releases the doorbell socket. The wake endpoint stays until {@link #closeWake}. */
  abstract void closeSocket();

  abstract void closeWake();

  /** A diagnostic description of the socket state (§12). */
  abstract String describe();

  // ---- unix: SOCK_SEQPACKET (Linux) or SOCK_DGRAM (macOS) ----

  static final class UnixMessage extends Wire {
    final int fd;
    final TransportClass cls;
    final int[] wakePair;

    UnixMessage(int fd, TransportClass cls) throws OsException {
      this.fd = fd;
      this.cls = cls;
      this.wakePair = Posix.socketpair(Posix.SOCK_STREAM);
      Posix.setNonBlocking(wakePair[0]);
      Posix.setNonBlocking(wakePair[1]);
    }

    @Override
    TransportClass transportClass() {
      return cls;
    }

    @Override
    Posix.Received receive() {
      return Posix.recvmsg(fd, Records.RECEIVE_BUFFER);
    }

    @Override
    Posix.Sent send(byte[] data, int... fds) {
      return Posix.sendmsg(fd, data, fds);
    }

    @Override
    int await(int timeoutMs) {
      return pollPair(new int[] {fd, wakePair[0]}, timeoutMs, wakePair[0]);
    }

    @Override
    void wake() {
      Posix.send(wakePair[1], new byte[] {1}, 0, 1, Posix.MSG_DONTWAIT | Posix.MSG_NOSIGNAL);
    }

    @Override
    void closeSocket() {
      Posix.close(fd);
    }

    @Override
    void closeWake() {
      Posix.close(wakePair[0]);
      Posix.close(wakePair[1]);
    }

    @Override
    String describe() {
      return cls.wireName() + " fd " + fd;
    }
  }

  /**
   * A unix {@code SOCK_STREAM} socket framed as a stream transport. Production never adopts one
   * (Windows is the only stream platform); it is the stand-in the tests use to exercise stream
   * framing, token-less, on a unix machine.
   */
  static final class UnixStream extends Wire {
    final int fd;
    final int[] wakePair;
    final byte[] buf = new byte[1024];

    UnixStream(int fd) throws OsException {
      this.fd = fd;
      this.wakePair = Posix.socketpair(Posix.SOCK_STREAM);
      Posix.setNonBlocking(wakePair[0]);
      Posix.setNonBlocking(wakePair[1]);
    }

    @Override
    TransportClass transportClass() {
      return TransportClass.STREAM;
    }

    @Override
    Posix.Received receive() {
      Posix.Sent r = Posix.recv(fd, buf, 0, buf.length, 0);
      if (r.n() < 0) return new Posix.Received(-1, r.errno(), false, new int[0], new byte[0]);
      return new Posix.Received(r.n(), 0, false, new int[0], java.util.Arrays.copyOf(buf, (int) r.n()));
    }

    @Override
    Posix.Sent send(byte[] data, int... fds) {
      return Posix.send(fd, data, 0, data.length, Posix.MSG_DONTWAIT | Posix.MSG_NOSIGNAL);
    }

    @Override
    int await(int timeoutMs) {
      return pollPair(new int[] {fd, wakePair[0]}, timeoutMs, wakePair[0]);
    }

    @Override
    void wake() {
      Posix.send(wakePair[1], new byte[] {1}, 0, 1, Posix.MSG_DONTWAIT | Posix.MSG_NOSIGNAL);
    }

    @Override
    void closeSocket() {
      Posix.close(fd);
    }

    @Override
    void closeWake() {
      Posix.close(wakePair[0]);
      Posix.close(wakePair[1]);
    }

    @Override
    String describe() {
      return "unix stream stand-in fd " + fd;
    }
  }

  private static int pollPair(int[] fds, int timeoutMs, int wakeFd) {
    short[] rev = new short[2];
    int r = Posix.poll(fds, rev, timeoutMs);
    if (r < 0) return r;
    int mask = 0;
    if (rev[0] != 0) mask |= READABLE; // POLLIN, POLLHUP, POLLERR: the receive says which
    if (rev[1] != 0) {
      mask |= WOKEN;
      byte[] sink = new byte[64];
      while (Posix.recv(wakeFd, sink, 0, sink.length, Posix.MSG_DONTWAIT).n() > 0) {
        // drain the wake bytes
      }
    }
    return mask;
  }

  // ---- Windows: loopback TCP ----

  static final class WinsockStream extends Wire {
    final long socket;
    final long[] wakePair;
    final byte[] buf = new byte[1024];

    WinsockStream(long socket) throws OsException {
      this.socket = socket;
      this.wakePair = Winsock.loopbackPair();
      Winsock.setNonBlocking(wakePair[0]);
      Winsock.setNonBlocking(wakePair[1]);
    }

    @Override
    TransportClass transportClass() {
      return TransportClass.STREAM;
    }

    @Override
    Posix.Received receive() {
      int[] err = new int[1];
      int n = Winsock.recv(socket, buf, 0, buf.length, err);
      if (n < 0) return new Posix.Received(-1, err[0], false, new int[0], new byte[0]);
      return new Posix.Received(n, 0, false, new int[0], java.util.Arrays.copyOf(buf, n));
    }

    @Override
    Posix.Sent send(byte[] data, int... fds) {
      int[] err = new int[1];
      int n = Winsock.send(socket, data, 0, data.length, err);
      return n < 0 ? new Posix.Sent(-1, err[0]) : new Posix.Sent(n, 0);
    }

    @Override
    int await(int timeoutMs) {
      short[] rev = new short[2];
      int r = Winsock.poll(new long[] {socket, wakePair[0]}, rev, timeoutMs);
      if (r < 0) return r;
      int mask = 0;
      if (rev[0] != 0) mask |= READABLE;
      if (rev[1] != 0) {
        mask |= WOKEN;
        byte[] sink = new byte[64];
        int[] err = new int[1];
        while (Winsock.recv(wakePair[0], sink, 0, sink.length, err) > 0) {
          // drain
        }
      }
      return mask;
    }

    @Override
    void wake() {
      Winsock.send(wakePair[1], new byte[] {1}, 0, 1, new int[1]);
    }

    @Override
    void closeSocket() {
      Winsock.closesocket(socket);
    }

    @Override
    void closeWake() {
      Winsock.closesocket(wakePair[0]);
      Winsock.closesocket(wakePair[1]);
    }

    @Override
    String describe() {
      return "loopback tcp socket " + socket;
    }
  }

  static boolean isInterrupted(int code) {
    return Errno.isInterrupted(code);
  }
}
