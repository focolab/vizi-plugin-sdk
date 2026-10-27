package vizi.plugin.javasdk.doorbell;

import java.util.List;
import java.util.Optional;
import org.jspecify.annotations.Nullable;

/**
 * What the transport emits, in one total order (doorbell_implementation_details.md §6): a
 * disposition per frame, interaction cancellations, and exactly one terminal. A cancellation a
 * frame caused follows that frame; the cancellations a terminal implies precede it.
 */
public sealed interface Outcome {

  /** One per frame examined. */
  record Frame(long ordinal, byte[] raw, Disposition disposition, List<String> diagnostics) implements Outcome {
    public Frame {
      diagnostics = List.copyOf(diagnostics);
    }

    @Override
    public String toString() {
      return "Frame#" + ordinal + " " + disposition + (diagnostics.isEmpty() ? "" : " " + diagnostics);
    }
  }

  /**
   * An interaction ended with neither its own terminal nor a successor announcing it
   * (doorbell_E_input.md §6). A local event, never a synthesized record.
   */
  record Cancellation(long interaction, long tool, int viewId, CancelReason reason) implements Outcome {}

  /** A terminal fault: once, ever; the channel is already closed to traffic. */
  record Fault(DoorbellFault fault) implements Outcome {}

  /** An orderly close, or a local stop. Not a fault, and never both. */
  record Teardown(String reason) implements Outcome {}

  /**
   * Work a caller asked to run on the listener in order with the outcomes already queued (the
   * SDK's tool hooks, so a cancellation always precedes the deactivation that caused it). Never
   * handed to a {@link Doorbell.Listener}; the listener thread runs it.
   */
  record Deferred(Runnable action) implements Outcome {}

  enum CancelReason {
    /** A rejected non-opening record named it (E §2's one rule). */
    REJECTED_RECORD,
    /** A rejected PRESS performed the implicit close its acceptance would have. */
    REJECTED_PRESS_IMPLICIT_CLOSE,
    TOOL_RETIRED,
    /** A newer pane epoch was installed. */
    EPOCH_INSTALLED,
    /** {@code cancel_interaction(n)} or {@code cancel_interactions(view_id)}. */
    REQUESTED,
    /** The session is ending: fault or teardown. */
    TERMINAL
  }

  /** The disposition of one frame. */
  sealed interface Disposition {}

  /** A record decoded and handed up. */
  record Decoded(Body body) implements Disposition {}

  /** An op this version does not define: dropped and counted. */
  record UnknownOp(int op) implements Disposition {}

  /** A well-formed record whose content was wrong: reported as ContractViolation, dropped. */
  record Rejected(Violation where) implements Disposition {}

  /** An {@code M} whose record-local checks failed: a grant failure {@code malformed} on the ack. */
  record GrantRejected(long segmentId, MemoryGrant grant, String detail) implements Disposition {}

  /** Dropped without a report: a duplicate, a stale epoch, an unknown interaction… */
  record Ignored(String reason) implements Disposition {}

  /** The frame that revealed a terminal fault. */
  record FramingRejection(FaultClass failureClass) implements Disposition {}

  /** The payload of a decoded record. */
  sealed interface Body {}

  /**
   * A grant, with its descriptor (unix) claimable until the callback returns.
   *
   * @param descriptor empty on Windows, where the name is the capability
   */
  record GrantBody(MemoryGrant grant, long recordId, Optional<Descriptor> descriptor) implements Body {}

  /**
   * An input event.
   *
   * @param implicitlyClosed for an accepted PRESS, the interaction of the same tool it closed, or 0
   */
  record InputBody(InputRecord event, long implicitlyClosed) implements Body {}

  /**
   * A hint. A non-zero {@code view_id} with {@code epoch == 0} is counted and ignored as a field,
   * and the record is still delivered (D §1).
   */
  record NotificationBody(Notification hint) implements Body {}

  /** Convenience for tests and the replayer. */
  static @Nullable Violation whereOf(Disposition d) {
    return d instanceof Rejected r ? r.where() : null;
  }
}
