package vizi.plugin.javasdk.memory;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.TreeMap;

/**
 * The arena of a plugin-produced {@code LOANED} region (shared_memory_protocol.md §7,
 * control_plane_protocol.md §5.6): ONE allocator per region, shared by every writer, so no two
 * are handed the same byte; no range is reused until its {@code ReleaseLoan} arrives; and a
 * reservation that does not fit fails, is counted, and never blocks.
 *
 * <p>Allocation is first-fit over the gaps between live reservations, each padded to 64 bytes.
 * The release key is the REQUESTED length: a release naming the padded length names nothing.
 * Offsets outside this class are mapping-relative; inside, ring addresses are region-relative.
 */
public final class LoanArena {

  /** A reservation, as the wire names it. {@code offset} is mapping-relative. */
  public record Loan(long segmentId, int region, long offset, long len, int uses) {}

  /** The arena could not fit {@code wanted} bytes; {@code largestFree} is its longest free run. */
  public static final class ArenaFullException extends Exception {
    private static final long serialVersionUID = 1L;
    public final long segmentId;
    public final int region;
    public final long wanted;
    public final long largestFree;

    ArenaFullException(long segmentId, int region, long wanted, long largestFree) {
      super("the arena of segment " + Long.toUnsignedString(segmentId) + " region " + region + " cannot fit "
          + wanted + " bytes (largest free run " + largestFree + ")");
      this.segmentId = segmentId;
      this.region = region;
      this.wanted = wanted;
      this.largestFree = largestFree;
    }
  }

  static final long ALIGN = 64;

  private static final class Res {
    final long start;
    final long padded;
    final long len;
    final int uses;

    Res(long start, long padded, long len, int uses) {
      this.start = start;
      this.padded = padded;
      this.len = len;
      this.uses = uses;
    }
  }

  final long segmentId;
  final int region;
  final long regionOffset;
  final long extent;
  private final MemorySegment mapping;
  private final TreeMap<Long, Res> live = new TreeMap<>();
  private final List<Runnable> onFree = new ArrayList<>();
  private long failed;

  LoanArena(long segmentId, int region, long regionOffset, long extent, MemorySegment mapping) {
    this.segmentId = segmentId;
    this.region = region;
    this.regionOffset = regionOffset;
    this.extent = extent;
    this.mapping = mapping;
  }

  /**
   * Reserves {@code len} bytes (padded to 64) and copies {@code data} in. The caller has already
   * checked the segment may start work. {@code uses} is how many messages will name the loan.
   */
  synchronized Loan reserve(long len, int uses) throws ArenaFullException {
    if (len <= 0) throw new IllegalArgumentException("a loan names at least one byte");
    long padded = (len + ALIGN - 1) & -ALIGN;
    long cursor = 0;
    long best = 0;
    for (Res r : live.values()) {
      long gap = r.start - cursor;
      if (gap >= padded) return place(cursor, padded, len, uses);
      best = Math.max(best, gap);
      cursor = r.start + r.padded;
    }
    long tail = extent - cursor;
    if (tail >= len) return place(cursor, Math.min(padded, tail), len, uses);
    best = Math.max(best, tail);
    failed++;
    throw new ArenaFullException(segmentId, region, len, Math.max(0, best));
  }

  private Loan place(long start, long padded, long len, int uses) {
    live.put(start, new Res(start, padded, len, Math.max(1, uses)));
    return new Loan(segmentId, region, regionOffset + start, len, Math.max(1, uses));
  }

  /** The bytes of a live loan, writable. */
  MemorySegment bytes(Loan loan) {
    return mapping.asSlice(loan.offset(), loan.len());
  }

  void write(Loan loan, byte[] data) {
    MemorySegment.copy(data, 0, mapping, ValueLayout.JAVA_BYTE, loan.offset(), data.length);
  }

  void write(Loan loan, MemorySegment data) {
    MemorySegment.copy(data, 0, mapping, loan.offset(), data.byteSize());
  }

  /** How a release named in a {@code ReleaseLoan} was disposed of. */
  enum Released {
    OK,
    /** Offset outside this region: {@code RELEASE_REGION_MISMATCH}. */
    REGION_MISMATCH,
    /** Names no live reservation (a padded length included): {@code UNKNOWN_RELEASE}. */
    UNKNOWN
  }

  /** Applies a {@code ReleaseLoan}; always permitted, retiring included (§9.1). */
  Released release(long offset, long len) {
    List<Runnable> wake;
    synchronized (this) {
      if (offset < regionOffset || offset - regionOffset >= extent) return Released.REGION_MISMATCH;
      Res r = live.get(offset - regionOffset);
      if (r == null || r.len != len) return Released.UNKNOWN;
      live.remove(r.start);
      wake = List.copyOf(onFree);
    }
    for (Runnable w : wake) w.run();
    return Released.OK;
  }

  /** Called after every successful release (a chunk source waiting for room). */
  synchronized void onFree(Runnable r) {
    onFree.add(r);
  }

  public synchronized int outstanding() {
    return live.size();
  }

  /** The reservations the host has not released, as the loans that named them (offsets in the mapping). */
  synchronized List<Loan> outstandingLoans() {
    List<Loan> out = new ArrayList<>();
    for (Res r : live.values()) out.add(new Loan(segmentId, region, regionOffset + r.start, r.len, r.uses));
    return out;
  }

  public synchronized long failedReservations() {
    return failed;
  }

  public synchronized long largestFree() {
    long cursor = 0;
    long best = 0;
    for (Map.Entry<Long, Res> e : live.entrySet()) {
      best = Math.max(best, e.getKey() - cursor);
      cursor = e.getKey() + e.getValue().padded;
    }
    return Math.max(best, extent - cursor);
  }

  /** The whole usable size. */
  public long capacity() {
    return extent;
  }
}
