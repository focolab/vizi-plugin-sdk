package vizi.plugin.javasdk.control;

import vizi.plugin.v1.ViziPlugin.Error;

/**
 * A handler's chosen refusal: the reply is {@code Error{kind, message}}. The author-facing layer
 * translates its own refusal type into this one; any other exception a handler throws becomes
 * {@code INTERNAL} (or the handler area's default kind) with its message.
 */
public final class HandlerFailure extends Exception {
  private static final long serialVersionUID = 1L;
  private final Error.Kind kind;

  public HandlerFailure(Error.Kind kind, String message) {
    super(message);
    this.kind = kind;
  }

  public Error.Kind kind() {
    return kind;
  }
}
