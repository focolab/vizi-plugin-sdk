package vizi.plugin.javasdk.memory;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.util.ArrayList;
import java.util.List;
import java.util.OptionalInt;
import java.util.OptionalLong;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.nativeio.Atomics;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * One {@code PULSED} region (shared_memory_protocol.md §6): N ≥ 3 buffers, single writer, single
 * reader, consume once. The same class serves both roles; which one this endpoint plays is the
 * region's {@code producer}.
 *
 * <pre>
 *   offset + i*16        state[i]   u64, atomic: bits 0-61 revision, 62 WRITE, 63 READ; 0 = EMPTY
 *   offset + i*16 + 8    length[i]  u64, plain
 *   offset + N*16 + i*S  payload[i] S bytes; the pane header (view_id u32, epoch u32), when
 *                                   declared, is the slot's last 8 bytes (§3.2)
 * </pre>
 *
 * The claims this endpoint holds are tracked here and never inferred from memory (§9.1): a claim
 * bit does not say whose it is.
 */
public final class PulsedLane {

  static final long WRITE = 1L << 62;
  static final long READ = 1L << 63;
  static final long REVISION_MASK = WRITE - 1;
  static final int ATTEMPTS = 8;

  /** A consumed value, with its pane pair when the slot carried a valid one. */
  public record Value(byte[] payload, long revision, OptionalInt viewId, OptionalLong epoch) {}

  /** What became of a publish. */
  public enum Published {
    PUBLISHED,
    /** No buffer could be claimed in 8 attempts: coalesced, the newest READY untouched (§6.4 step 5). */
    COALESCED,
    /** The region is quarantined or the revision space is exhausted: nothing is published. */
    REFUSED
  }

  /** Reports a runtime violation (no doorbell record: the detection is the occurrence). */
  interface Reporter {
    void violation(ViolationSite where, String detail);
  }

  final long segmentId;
  final int region;
  final long offset;
  final int buffers;
  final long slotBytes;
  final boolean paneHeader;
  final long capacity;
  private final MemorySegment m;
  private final Reporter reporter;

  private long nextRevision = 1;
  private long lastSeen;
  private volatile boolean quarantined;
  private @Nullable String quarantineReason;
  /** Buffers this endpoint holds a claim on, and the claimed word. */
  private final List<long[]> heldClaims = new ArrayList<>();

  PulsedLane(RegionTable.Spec spec, long segmentId, MemorySegment mapping, Reporter reporter) {
    this.segmentId = segmentId;
    this.region = spec.index();
    this.offset = spec.offset();
    this.buffers = spec.buffers();
    this.slotBytes = spec.slotBytes();
    this.paneHeader = spec.epochOffset().isPresent();
    this.capacity = paneHeader ? slotBytes - 8 : slotBytes;
    this.m = mapping;
    this.reporter = reporter;
  }

  /**
   * A lane over any mapping, for the other end of a test or the testkit's host: publishes or
   * reads exactly as the plugin does, reporting violations to {@code reporter}.
   */
  public static PulsedLane over(
      MemorySegment mapping, long segmentId, RegionTable.Spec spec, java.util.function.BiConsumer<ViolationSite, String> reporter) {
    return new PulsedLane(spec, segmentId, mapping, reporter::accept);
  }

  public boolean quarantined() {
    return quarantined;
  }

  public @Nullable String quarantineReason() {
    return quarantineReason;
  }

  /** Revisions allocated so far; a writer that reached 2⁶²−1 stops (§6.3), and this shows it. */
  public synchronized long revisionsAllocated() {
    return nextRevision - 1;
  }

  public long capacity() {
    return capacity;
  }

  synchronized boolean holdsClaim() {
    return !heldClaims.isEmpty();
  }

  private long stateAt(int i) {
    return offset + 16L * i;
  }

  private long slotAt(int i) {
    return offset + 16L * buffers + (long) i * slotBytes;
  }

  private static boolean impossible(long w) {
    long rev = w & REVISION_MASK;
    boolean write = (w & WRITE) != 0;
    boolean read = (w & READ) != 0;
    return (rev == 0 && (write || read)) || (write && read);
  }

  private void quarantine(ViolationSite site, String why) {
    if (quarantined) return;
    quarantined = true;
    quarantineReason = why;
    reporter.violation(site, "segment " + Long.toUnsignedString(segmentId) + " region " + region + ": " + why);
  }

  private long[] loadAll() {
    long[] w = new long[buffers];
    for (int i = 0; i < buffers; i++) w[i] = Atomics.loadAcquire(m, stateAt(i));
    return w;
  }

  /** Reports and quarantines on an impossible word; true when all are possible. */
  private boolean checkStates(long[] w) {
    for (int i = 0; i < w.length; i++) {
      if (impossible(w[i])) {
        quarantine(ViolationSite.VIOLATION_SITE_IMPOSSIBLE_STATE_WORD,
            "state[" + i + "] = 0x" + Long.toHexString(w[i]) + " is impossible");
        return false;
      }
    }
    return true;
  }

  // ---- publishing (§6.4) ----

  /**
   * Publishes {@code payload} (and the pane pair, when the region declares a header). Throws
   * {@link IllegalArgumentException} for an empty payload or one larger than the capacity: the
   * sender fails its own call rather than truncating (§3.3).
   */
  public synchronized Published publish(byte[] payload, int viewId, long epoch) {
    if (payload.length == 0) throw new IllegalArgumentException("a published value has at least one byte");
    if (payload.length > capacity) {
      throw new IllegalArgumentException("payload of " + payload.length + " bytes exceeds the lane's capacity " + capacity);
    }
    if (quarantined) return Published.REFUSED;
    if (nextRevision > REVISION_MASK) return Published.REFUSED;
    long rev = nextRevision++;
    int claimed = -1;
    long claimedWord = 0;
    for (int attempt = 0; attempt < ATTEMPTS && claimed < 0; attempt++) {
      long[] w = loadAll();
      if (!checkStates(w)) return Published.REFUSED;
      for (int c : candidates(w, rev)) {
        long want = rev | WRITE;
        if (Atomics.compareAndSet(m, stateAt(c), w[c], want)) {
          claimed = c;
          claimedWord = want;
          break;
        }
      }
    }
    if (claimed < 0) return Published.COALESCED;
    heldClaims.add(new long[] {claimed, claimedWord});
    try {
      long slot = slotAt(claimed);
      MemorySegment.copy(payload, 0, m, ValueLayout.JAVA_BYTE, slot, payload.length);
      if (paneHeader) {
        m.set(ValueLayout.JAVA_INT_UNALIGNED, slot + slotBytes - 8, viewId);
        m.set(ValueLayout.JAVA_INT_UNALIGNED, slot + slotBytes - 4, (int) epoch);
      }
      m.set(ValueLayout.JAVA_LONG_UNALIGNED, stateAt(claimed) + 8, payload.length);
      Atomics.storeRelease(m, stateAt(claimed), rev);
      return Published.PUBLISHED;
    } catch (RuntimeException e) {
      if (!Atomics.compareAndSet(m, stateAt(claimed), claimedWord, 0)) {
        quarantine(ViolationSite.VIOLATION_SITE_RELEASE_CAS_FAILED, "releasing a failed publish found a foreign value");
      }
      throw e;
    } finally {
      heldClaims.clear();
    }
  }

  /**
   * EMPTY buffers rotating from {@code rev % N}, then READY buffers oldest first, excluding the
   * newest READY: it stays visible to a reader who has not arrived yet.
   */
  private List<Integer> candidates(long[] w, long rev) {
    List<Integer> out = new ArrayList<>();
    int n = w.length;
    int start = (int) (rev % n);
    for (int k = 0; k < n; k++) {
      int i = (start + k) % n;
      if (w[i] == 0) out.add(i);
    }
    List<Integer> ready = new ArrayList<>();
    for (int i = 0; i < n; i++) {
      if (w[i] != 0 && (w[i] & (WRITE | READ)) == 0) ready.add(i);
    }
    ready.sort((a, b) -> Long.compare(w[a] & REVISION_MASK, w[b] & REVISION_MASK));
    if (!ready.isEmpty()) ready.remove(ready.size() - 1);
    out.addAll(ready);
    return out;
  }

  // ---- reading (§6.5) ----

  /** Consumes the newest value past the last seen, if any. */
  public synchronized @Nullable Value read() {
    if (quarantined) return null;
    for (int attempt = 0; attempt < ATTEMPTS; attempt++) {
      long[] w = loadAll();
      if (!checkStates(w)) return null;
      int best = -1;
      long bestRev = lastSeen;
      for (int i = 0; i < w.length; i++) {
        long rev = w[i] & REVISION_MASK;
        if (w[i] != 0 && (w[i] & (WRITE | READ)) == 0 && Long.compareUnsigned(rev, bestRev) > 0) {
          best = i;
          bestRev = rev;
        }
      }
      if (best < 0) return null;
      long claimedWord = bestRev | READ;
      if (!Atomics.compareAndSet(m, stateAt(best), bestRev, claimedWord)) continue;
      heldClaims.add(new long[] {best, claimedWord});
      try {
        long len = m.get(ValueLayout.JAVA_LONG_UNALIGNED, stateAt(best) + 8);
        long slot = slotAt(best);
        if (len <= 0 || len > capacity || slot + len > m.byteSize()) {
          Atomics.compareAndSet(m, stateAt(best), claimedWord, 0);
          quarantine(ViolationSite.VIOLATION_SITE_INVALID_PUBLISHED_LENGTH,
              "buffer " + best + " publishes length " + len + " against a capacity of " + capacity);
          return null;
        }
        byte[] payload = m.asSlice(slot, len).toArray(ValueLayout.JAVA_BYTE);
        OptionalInt view = OptionalInt.empty();
        OptionalLong epoch = OptionalLong.empty();
        if (paneHeader) {
          int v = m.get(ValueLayout.JAVA_INT_UNALIGNED, slot + slotBytes - 8);
          long e = Integer.toUnsignedLong(m.get(ValueLayout.JAVA_INT_UNALIGNED, slot + slotBytes - 4));
          // a bad header costs the pair, not the payload (§2)
          if ((v & 0xFFFFFF00) == 0 && e != 0) {
            view = OptionalInt.of(v);
            epoch = OptionalLong.of(e);
          }
        }
        if (!Atomics.compareAndSet(m, stateAt(best), claimedWord, 0)) {
          quarantine(ViolationSite.VIOLATION_SITE_RELEASE_CAS_FAILED,
              "consuming buffer " + best + " found a foreign value: proven interference");
          return null;
        }
        lastSeen = bestRev;
        return new Value(payload, bestRev, view, epoch);
      } finally {
        heldClaims.clear();
      }
    }
    return null;
  }
}
