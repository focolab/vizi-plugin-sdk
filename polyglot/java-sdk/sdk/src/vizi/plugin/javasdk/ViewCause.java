package vizi.plugin.javasdk;

import java.util.Map;
import java.util.Optional;
import java.util.concurrent.TimeoutException;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.v1.ViziPlugin.Value;
import vizi.plugin.v1.ViziPlugin.ViewRequest;

/**
 * The host's token for one interaction with a {@code moves_view} widget (§8.15), and the one
 * answer the plugin owes it: {@link #move} or {@link #decline}.
 *
 * <p><b>Every cause is answered.</b> When the command (or a widget handle's listeners) returns
 * without answering and without {@link #defer()}, the SDK declines the cause, so the widget does
 * not show as pending until it expires, and logs a line naming the command, so a path that forgot
 * to answer shows up. A cause is {@code STALE} 2 s after the interaction; answer within 1 s.
 *
 * <p>The cause carries the invocation it came with: {@link #commandId()}, {@link #args()}, and
 * {@link #row()} for a list, so a typed handle's {@code onCause} listener knows what was clicked.
 *
 * <p>Answering blocks until the host replies, so it is refused on the doorbell listener and the
 * dispatch thread, like every host request. Answering twice with the same request returns the
 * first answer without sending again; a different request is sent, and the host refuses it {@code
 * CONSUMED}.
 */
public final class ViewCause {

  private final Plugin.Runtime rt;
  private final long token;
  private final String commandId;
  private final Map<String, Value> args;
  private boolean deferred;
  private @Nullable ViewRequest answer;
  private @Nullable ViewResult result;

  ViewCause(Plugin.Runtime rt, long token, String commandId, Map<String, Value> args) {
    if (token == 0) throw new IllegalArgumentException("0 is no cause");
    this.rt = rt;
    this.token = token;
    this.commandId = commandId;
    this.args = Map.copyOf(args);
  }

  /** The command whose invocation carried this cause. */
  public String commandId() {
    return commandId;
  }

  /** That invocation's arguments, as the widget sent them. */
  public Map<String, Value> args() {
    return args;
  }

  /**
   * The row the interaction came from ({@code args["row"]}): the clicked row of a selectable list,
   * or the row of a list cell's widget. Empty for any other widget.
   */
  public Optional<String> row() {
    Value v = args.get("row");
    return v != null && v.hasS() ? Optional.of(v.getS()) : Optional.empty();
  }

  /** The host's token ({@code InvokeCommand.cause}). */
  public long token() {
    return token;
  }

  /** Moves the view, whole or not at all. */
  public ViewResult move(ViewMove move) throws HostException, TimeoutException, InterruptedException {
    var r = ViewRequest.newBuilder().setCause(token);
    move.into(r);
    return answer(r.build());
  }

  /** Answers the cause and moves nothing, for example a click on an empty part of a trace. */
  public ViewResult decline() throws HostException, TimeoutException, InterruptedException {
    return answer(ViewRequest.newBuilder().setCause(token).setDecline(true).build());
  }

  /**
   * Keeps the cause open past the handler's return, for a job that answers it: the SDK then does
   * not decline it. Answer it within 1 s of the interaction.
   */
  public synchronized ViewCause defer() {
    deferred = true;
    return this;
  }

  /** Whether an answer has been sent. */
  public synchronized boolean answered() {
    return answer != null;
  }

  private ViewResult answer(ViewRequest r) throws HostException, TimeoutException, InterruptedException {
    synchronized (this) {
      if (r.equals(answer) && result != null) return result;
      if (answer == null) answer = r;
    }
    ViewResult got = rt.host().requestView(r);
    synchronized (this) {
      if (r.equals(answer) && result == null) result = got;
    }
    return got;
  }

  /** After the handler: declines an unanswered, undeferred cause without waiting for the reply. */
  void settle() {
    ViewRequest decline;
    synchronized (this) {
      if (answer != null || deferred) return;
      decline = ViewRequest.newBuilder().setCause(token).setDecline(true).build();
      answer = decline;
    }
    rt.log("command " + commandId + " returned without answering its moves_view cause " + Long.toUnsignedString(token)
        + " (neither a move nor a decline, nor defer()); the SDK declined it");
    rt.host().requestViewLater(decline).whenComplete((res, t) -> {
      synchronized (this) {
        if (res != null) result = res;
      }
      if (t != null) rt.log("declining unanswered cause " + Long.toUnsignedString(token) + " failed: " + t);
      else if (!res.declined()) rt.log("declining unanswered cause " + Long.toUnsignedString(token) + ": " + res);
    });
  }
}
