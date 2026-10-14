package vizi.plugin.javasdk.control;

import com.google.protobuf.ByteString;
import java.util.EnumMap;
import java.util.Map;
import java.util.function.Consumer;
import vizi.plugin.v1.ViziPlugin.ContractViolation;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * The plugin's {@code ContractViolation} reporter, rate-bounded as
 * shared_memory_implementation_details.md §2 requires: the first occurrence of each {@code
 * where} goes at once with its record and {@code count = 1}; later ones are counted per {@code
 * where} and re-reported every {@value #INTERVAL_MS} ms with the count since the last report,
 * under a per-session budget of {@value #BUDGET} reports refilling at {@value #REFILL_PER_S} per
 * second. A change of {@code where} does not reset the budget, and there is no lifetime cap.
 *
 * <p>{@code ZERO_ID} for an {@code M} record is exempt from aggregation: each goes at once, with
 * its record, because the host needs each record to close out the grant it names.
 */
public final class Violations implements AutoCloseable {

  public static final int BUDGET = 16;
  public static final double REFILL_PER_S = 4.0;
  public static final long INTERVAL_MS = 1000;

  private static final class Site {
    boolean firstSent;
    long pending;
    String lastDetail = "";
    long lastReportMs;
  }

  private final Consumer<PluginMsg> out;
  private final Map<ViolationSite, Site> sites = new EnumMap<>(ViolationSite.class);
  private double tokens = BUDGET;
  private long refilledAt = System.nanoTime();
  private final Thread ticker;
  private volatile boolean closed;
  private long reported;
  private long suppressed;

  public Violations(Consumer<PluginMsg> out) {
    this.out = out;
    this.ticker = Thread.ofVirtual().name("vizi-violations").unstarted(this::tickLoop);
    this.ticker.start();
  }

  /** One occurrence of {@code where}; {@code record} is the 112-byte record, or empty. */
  public void report(ViolationSite where, String detail, byte[] record) {
    PluginMsg now = null;
    synchronized (this) {
      if (closed) return;
      Site s = sites.computeIfAbsent(where, w -> new Site());
      s.lastDetail = detail;
      if (!s.firstSent && take()) {
        s.firstSent = true;
        s.lastReportMs = System.currentTimeMillis();
        now = build(where, detail, record, 1);
      } else {
        s.pending++;
      }
    }
    if (now != null) send(now);
  }

  /** An {@code M} with {@code segment_id == 0}: exempt from aggregation, sent at once. */
  public void reportZeroIdGrant(String detail, byte[] record) {
    if (closed) return;
    send(build(ViolationSite.VIOLATION_SITE_ZERO_ID, detail, record, 1));
  }

  private boolean take() {
    long now = System.nanoTime();
    tokens = Math.min(BUDGET, tokens + (now - refilledAt) / 1e9 * REFILL_PER_S);
    refilledAt = now;
    if (tokens >= 1) {
      tokens -= 1;
      return true;
    }
    return false;
  }

  private void tickLoop() {
    while (!closed) {
      try {
        Thread.sleep(250);
      } catch (InterruptedException e) {
        return;
      }
      flush(false);
    }
  }

  /** Re-reports pending counts whose interval has passed; {@code force} ignores interval and budget. */
  public void flush(boolean force) {
    java.util.List<PluginMsg> batch = new java.util.ArrayList<>();
    synchronized (this) {
      long nowMs = System.currentTimeMillis();
      for (Map.Entry<ViolationSite, Site> e : sites.entrySet()) {
        Site s = e.getValue();
        if (s.pending == 0) continue;
        if (!force && nowMs - s.lastReportMs < INTERVAL_MS) continue;
        if (!force && !take()) continue;
        batch.add(build(e.getKey(), s.lastDetail, new byte[0], (int) Math.min(Integer.MAX_VALUE, s.pending)));
        s.pending = 0;
        s.firstSent = true;
        s.lastReportMs = nowMs;
      }
    }
    batch.forEach(this::send);
  }

  private static PluginMsg build(ViolationSite where, String detail, byte[] record, int count) {
    return PluginMsg.newBuilder()
        .setContractViolation(ContractViolation.newBuilder()
            .setWhere(where)
            .setDetail(detail.length() > 4096 ? detail.substring(0, 4096) : detail)
            .setRecord(ByteString.copyFrom(record))
            .setCount(count))
        .build();
  }

  private void send(PluginMsg m) {
    synchronized (this) {
      reported++;
    }
    out.accept(m);
  }

  public synchronized long reported() {
    return reported;
  }

  public synchronized long pending() {
    long n = 0;
    for (Site s : sites.values()) n += s.pending;
    return n + suppressed;
  }

  @Override
  public void close() {
    closed = true;
    ticker.interrupt();
  }
}
