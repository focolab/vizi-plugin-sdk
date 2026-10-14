package vizi.plugin.javasdk.nativeio;

import java.io.IOException;

/** A failed system call, carrying the typed error code rather than only its text. */
public final class OsException extends IOException {
  private static final long serialVersionUID = 1L;

  private final String call;
  private final int code;

  public OsException(String call, int code) {
    super(call + " failed: " + Errno.describe(code));
    this.call = call;
    this.code = code;
  }

  public OsException(String call, int code, String detail) {
    super(call + " failed: " + Errno.describe(code) + "; " + detail);
    this.call = call;
    this.code = code;
  }

  /** The call that failed, e.g. {@code "recvmsg"}. */
  public String call() {
    return call;
  }

  /** errno on unix; GetLastError or WSAGetLastError on Windows. */
  public int code() {
    return code;
  }
}
