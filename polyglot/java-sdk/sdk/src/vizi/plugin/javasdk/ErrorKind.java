package vizi.plugin.javasdk;

import vizi.plugin.v1.ViziPlugin.Error;

/** The kind a refusal carries on the wire ({@code Error.Kind}). */
public enum ErrorKind {
  INVALID_ARGUMENT(Error.Kind.INVALID_ARGUMENT),
  NOT_FOUND(Error.Kind.NOT_FOUND),
  UNSUPPORTED(Error.Kind.UNSUPPORTED),
  OUT_OF_BOUNDS(Error.Kind.OUT_OF_BOUNDS),
  IO_ERROR(Error.Kind.IO_ERROR),
  PROTOCOL(Error.Kind.PROTOCOL),
  INTERNAL(Error.Kind.INTERNAL);

  final Error.Kind wire;

  ErrorKind(Error.Kind wire) {
    this.wire = wire;
  }

  public Error.Kind wire() {
    return wire;
  }

  /** The kind of a wire value; an unknown one reads as {@link #INTERNAL}. */
  public static ErrorKind of(Error.Kind k) {
    for (ErrorKind e : values()) if (e.wire == k) return e;
    return INTERNAL;
  }
}
