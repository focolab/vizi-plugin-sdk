package vizi.plugin.javasdk.doorbell;

import java.util.Locale;

/**
 * The {@code where} of a semantic rejection the transport makes (doorbell_E_input.md §2's first
 * block, plus {@code zero_id} for {@code D} and {@code M}). {@link #UNUSABLE_CONTEXT} is never
 * raised here: the supervisor raises it against a record the transport surfaced.
 */
public enum Violation {
  ZERO_ID,
  SEQ_ON_OPEN,
  POSITION_TAIL,
  NONFINITE_POSITION,
  MISS_WITH_PICKED,
  INVALID_TARGET,
  INTERACTION_MISMATCH,
  UNKNOWN_TOOL,
  EPOCH_MISMATCH,
  UNUSABLE_CONTEXT;

  /** The lower-case E §2 spelling, e.g. {@code seq_on_open}. */
  public String wireName() {
    return name().toLowerCase(Locale.ROOT);
  }
}
