package vizi.plugin.javasdk.control;

import vizi.plugin.v1.ViziPlugin.Error;

/**
 * The host answered a plugin request with {@code host_error}: carries the host's {@code
 * Error.Kind}, so an author can branch on {@code UNSUPPORTED} rather than parse text.
 *
 * <p>A later host may send a kind this build does not know: {@link #kind()} is then {@code
 * UNRECOGNIZED}, and {@link #kindValue()} keeps the number the host sent.
 */
public class HostException extends Exception {
  private static final long serialVersionUID = 1L;
  private final Error.Kind kind;
  private final int kindValue;

  public HostException(Error.Kind kind, String message) {
    this(kind, kind == Error.Kind.UNRECOGNIZED ? -1 : kind.getNumber(), message);
  }

  /** From the wire number of {@code Error.Kind}, known to this build or not. */
  public HostException(int kindValue, String message) {
    this(known(kindValue), kindValue, message);
  }

  private HostException(Error.Kind kind, int kindValue, String message) {
    super(!message.isEmpty() ? message : kind == Error.Kind.UNRECOGNIZED ? "error kind " + kindValue : kind.name());
    this.kind = kind;
    this.kindValue = kindValue;
  }

  private static Error.Kind known(int value) {
    Error.Kind k = Error.Kind.forNumber(value);
    return k == null ? Error.Kind.UNRECOGNIZED : k;
  }

  /** The host's kind; {@code UNRECOGNIZED} for a kind this build does not know. */
  public Error.Kind kind() {
    return kind;
  }

  /**
   * The kind's number as the host sent it, for a kind this build does not know as well as one it
   * does ({@code -1} only when the exception was built from {@code UNRECOGNIZED} itself).
   */
  public int kindValue() {
    return kindValue;
  }
}
