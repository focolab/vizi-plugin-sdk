package vizi.plugin.javasdk.doorbell;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.concurrent.atomic.LongAdder;

/**
 * The transport's counters: every "counted, not reported" condition in the doorbell documents
 * surfaces here (doorbell_implementation_details.md §6), readable from any thread.
 */
public final class Stats {
  public final LongAdder framesExamined = new LongAdder();
  public final LongAdder idGaps = new LongAdder();
  public final LongAdder idRegressions = new LongAdder();
  public final LongAdder unknownOps = new LongAdder();
  public final LongAdder inputIgnored = new LongAdder();
  public final LongAdder hintsIgnored = new LongAdder();
  public final LongAdder hintsSent = new LongAdder();
  /** {@code D} records refused by backpressure and dropped (never retried, never queued). */
  public final LongAdder hintsDropped = new LongAdder();
  /** {@code D} records refused because the handshake had not completed or the channel is closed. */
  public final LongAdder hintsRefused = new LongAdder();

  public Map<String, Long> snapshot() {
    Map<String, Long> m = new LinkedHashMap<>();
    m.put("frames_examined", framesExamined.sum());
    m.put("id_gaps", idGaps.sum());
    m.put("id_regressions", idRegressions.sum());
    m.put("unknown_ops", unknownOps.sum());
    m.put("input_ignored", inputIgnored.sum());
    m.put("hints_ignored", hintsIgnored.sum());
    m.put("hints_sent", hintsSent.sum());
    m.put("hints_dropped", hintsDropped.sum());
    m.put("hints_refused", hintsRefused.sum());
    return m;
  }

  @Override
  public String toString() {
    return snapshot().toString();
  }
}
