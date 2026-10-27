package vizi.plugin.javasdk;

/**
 * Thrown by a reader, exporter, stage or command to choose the reply: {@code Error{kind,
 * message}} for a request, or {@code ok = false} with the message for a command. Any other
 * exception is a failure too, carrying its message ({@code IO_ERROR} for readers and exporters,
 * {@code INTERNAL} elsewhere).
 */
public final class Refusal extends Exception {
  private static final long serialVersionUID = 1L;
  private final ErrorKind kind;

  public Refusal(ErrorKind kind, String message) {
    super(message);
    this.kind = kind;
  }

  public ErrorKind kind() {
    return kind;
  }

  public static Refusal notFound(String message) {
    return new Refusal(ErrorKind.NOT_FOUND, message);
  }

  public static Refusal outOfBounds(String message) {
    return new Refusal(ErrorKind.OUT_OF_BOUNDS, message);
  }

  public static Refusal invalid(String message) {
    return new Refusal(ErrorKind.INVALID_ARGUMENT, message);
  }

  public static Refusal unsupported(String message) {
    return new Refusal(ErrorKind.UNSUPPORTED, message);
  }
}
