package vizi.plugin.javasdk;

import java.util.Objects;
import vizi.plugin.v1.ViziPlugin.ViewOutcome;
import vizi.plugin.v1.ViziPlugin.ViewRefusal;
import vizi.plugin.v1.ViziPlugin.ViewRequestResult;

/**
 * The host's one answer to a set-view request (§8.15). It is the acknowledgement: never infer a
 * move from a later {@code ViewChanged}. A refusal is ordinary, not an error, so it is never
 * thrown, {@code UNSUPPORTED} and {@code NOT_ENABLED} (the person's switch, on the panel's first
 * row, is off or the panel is not showing) included.
 *
 * @param reason {@code VIEW_REFUSAL_NONE} unless {@code outcome} is REFUSED
 * @param detail a sentence for a log
 */
public record ViewResult(ViewOutcome outcome, ViewRefusal reason, String detail) {
  public ViewResult {
    Objects.requireNonNull(outcome, "outcome");
    Objects.requireNonNull(reason, "reason");
    Objects.requireNonNull(detail, "detail");
  }

  static ViewResult of(ViewRequestResult r) {
    return new ViewResult(r.getOutcome(), r.getReason(), r.getDetail());
  }

  /** The view moved. */
  public boolean applied() {
    return outcome == ViewOutcome.VIEW_OUTCOME_APPLIED;
  }

  /** The cause was released, and nothing moved. */
  public boolean declined() {
    return outcome == ViewOutcome.VIEW_OUTCOME_DECLINED;
  }

  /** The host refused; {@link #reason()} says why. */
  public boolean refused() {
    return outcome == ViewOutcome.VIEW_OUTCOME_REFUSED;
  }

  @Override
  public String toString() {
    String o = outcome.name().replace("VIEW_OUTCOME_", "");
    if (refused()) o += " " + reason.name().replace("VIEW_REFUSAL_", "");
    return detail.isEmpty() ? o : o + ": " + detail;
  }
}
