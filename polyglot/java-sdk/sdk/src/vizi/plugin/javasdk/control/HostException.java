package vizi.plugin.javasdk.control;

import vizi.plugin.v1.ViziPlugin.Error;

/**
 * The host answered a plugin request with {@code host_error}: carries the host's {@code
 * Error.Kind}, so an author can branch on {@code UNSUPPORTED} rather than parse text.
 */
public final class HostException extends Exception {
  private static final long serialVersionUID = 1L;
  private final Error.Kind kind;

  public HostException(Error.Kind kind, String message) {
    super(message.isEmpty() ? kind.name() : message);
    this.kind = kind;
  }

  public Error.Kind kind() {
    return kind;
  }
}
