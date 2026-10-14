package vizi.plugin.javasdk.nativeio;

/**
 * The error numbers the transport classifies, per platform (doorbell_protocol.md §6 rules 6–7,
 * §7 rule 5). The values differ between Linux and Darwin, so none is ever written as a literal
 * outside this class. Winsock codes are fixed across Windows versions and are listed as {@code
 * WSA*}; on unix they are unused values that never match.
 */
public final class Errno {
  private Errno() {}

  private static final boolean MAC = Platform.OS == Platform.DARWIN;

  public static final int EPERM = 1;
  public static final int ENOENT = 2;
  public static final int EINTR = 4;
  public static final int EBADF = 9;
  public static final int ENOMEM = 12;
  public static final int EACCES = 13;
  public static final int ENODEV = 19;
  public static final int EINVAL = 22;
  public static final int ENFILE = 23;
  public static final int EMFILE = 24;
  public static final int EPIPE = 32;
  public static final int EAGAIN = MAC ? 35 : 11;
  public static final int EWOULDBLOCK = EAGAIN;
  public static final int ENOTSOCK = MAC ? 38 : 88;
  public static final int EDESTADDRREQ = MAC ? 39 : 89;
  public static final int ENOPROTOOPT = MAC ? 42 : 92;
  public static final int ECONNRESET = MAC ? 54 : 104;
  public static final int ENOBUFS = MAC ? 55 : 105;
  public static final int ENOTCONN = MAC ? 57 : 107;
  public static final int ETOOMANYREFS = MAC ? 59 : 109;
  public static final int ECONNREFUSED = MAC ? 61 : 111;
  public static final int EOVERFLOW = MAC ? 84 : 75;

  public static final int WSAEINTR = 10004;
  public static final int WSAEWOULDBLOCK = 10035;
  public static final int WSAECONNABORTED = 10053;
  public static final int WSAECONNRESET = 10054;
  public static final int WSAENOBUFS = 10055;
  public static final int WSAENOTCONN = 10057;
  public static final int WSAESHUTDOWN = 10058;

  /** Windows system error codes a mapping can fail with (GetLastError). */
  public static final int ERROR_FILE_NOT_FOUND = 2;
  public static final int ERROR_TOO_MANY_OPEN_FILES = 4;
  public static final int ERROR_ACCESS_DENIED = 5;
  public static final int ERROR_NOT_ENOUGH_MEMORY = 8;
  public static final int ERROR_OUTOFMEMORY = 14;
  public static final int ERROR_NO_SYSTEM_RESOURCES = 1450;
  public static final int ERROR_COMMITMENT_LIMIT = 1455;

  /**
   * The peer-gone set (§6 rule 7). A set, because the value differs between the first and later
   * sends and between platforms, and a classifier holding only the first faults on the second
   * record of an ordinary teardown.
   */
  public static boolean isPeerGone(int e) {
    return e == EPIPE
        || e == ECONNREFUSED
        || e == ECONNRESET
        || e == EDESTADDRREQ
        || e == ENOTCONN
        || e == WSAECONNRESET
        || e == WSAECONNABORTED;
  }

  /** §6 rule 6's backpressure class. */
  public static boolean isBackpressure(int e) {
    return e == EAGAIN
        || e == EWOULDBLOCK
        || e == ENOBUFS
        || e == ETOOMANYREFS
        || e == WSAEWOULDBLOCK
        || e == WSAENOBUFS;
  }

  public static boolean isInterrupted(int e) {
    return e == EINTR || e == WSAEINTR;
  }

  public static boolean isWouldBlock(int e) {
    return e == EAGAIN || e == EWOULDBLOCK || e == WSAEWOULDBLOCK;
  }

  /** {@code WSAESHUTDOWN}: our own shutdown, teardown but never attributed to the peer. */
  public static boolean isLocallyClosed(int e) {
    return e == WSAESHUTDOWN;
  }

  /**
   * doorbell_M_memory.md §7's closed {@code retryable} set: out of address space, out of handles,
   * out of memory. Nothing else is retryable, so the default is permanent.
   */
  public static boolean isRetryableMappingError(int e) {
    if (Platform.OS == Platform.WINDOWS) {
      return e == ERROR_NOT_ENOUGH_MEMORY
          || e == ERROR_OUTOFMEMORY
          || e == ERROR_COMMITMENT_LIMIT
          || e == ERROR_NO_SYSTEM_RESOURCES
          || e == ERROR_TOO_MANY_OPEN_FILES;
    }
    return e == ENOMEM || e == EMFILE || e == ENFILE;
  }

  /** A symbolic name for diagnostics; the number is always reported beside it. */
  public static String name(int e) {
    if (e == 0) return "0";
    if (e == EINTR) return "EINTR";
    if (e == EAGAIN) return "EAGAIN";
    if (e == EBADF) return "EBADF";
    if (e == ENOMEM) return "ENOMEM";
    if (e == EACCES) return "EACCES";
    if (e == EINVAL) return "EINVAL";
    if (e == ENFILE) return "ENFILE";
    if (e == EMFILE) return "EMFILE";
    if (e == EPIPE) return "EPIPE";
    if (e == ENOTSOCK) return "ENOTSOCK";
    if (e == EDESTADDRREQ) return "EDESTADDRREQ";
    if (e == ENOPROTOOPT) return "ENOPROTOOPT";
    if (e == ECONNRESET) return "ECONNRESET";
    if (e == ENOBUFS) return "ENOBUFS";
    if (e == ENOTCONN) return "ENOTCONN";
    if (e == ETOOMANYREFS) return "ETOOMANYREFS";
    if (e == ECONNREFUSED) return "ECONNREFUSED";
    if (e == ENODEV) return "ENODEV";
    if (e == WSAEWOULDBLOCK) return "WSAEWOULDBLOCK";
    if (e == WSAECONNABORTED) return "WSAECONNABORTED";
    if (e == WSAECONNRESET) return "WSAECONNRESET";
    if (e == WSAENOBUFS) return "WSAENOBUFS";
    if (e == WSAESHUTDOWN) return "WSAESHUTDOWN";
    return "errno " + e;
  }

  /** "name (n)" for a diagnostic. */
  public static String describe(int e) {
    return name(e) + " (" + e + ")";
  }
}
