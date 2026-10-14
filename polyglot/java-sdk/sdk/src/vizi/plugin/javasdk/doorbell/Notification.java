package vizi.plugin.javasdk.doorbell;

import java.util.Arrays;
import java.util.BitSet;

/**
 * A {@code D} hint (doorbell_D_notification.md §1): "look at shared memory now".
 *
 * <pre>
 *   [0..8) segment_id  [8..72) mask, 512 bits  [72..76) epoch u32  [76] view_id  [77..100) padding
 * </pre>
 *
 * Region {@code r} is bit {@code r & 7} of payload byte {@code 8 + (r >> 3)}, LSB first;
 * equivalently bit {@code r & 63} of little-endian word {@code r >> 6}. The pane pair is present
 * exactly when {@code epoch != 0}: the discriminator is the epoch, because 0 is a valid pane.
 *
 * @param mask eight little-endian u64 words; word {@code w} holds regions {@code 64w .. 64w+63}
 */
public record Notification(long segmentId, long[] mask, long epoch, int viewId) {

  public static final int MASK_BITS = 512;

  public Notification {
    if (mask.length != 8) throw new IllegalArgumentException("mask is eight u64 words");
    mask = mask.clone();
  }

  /** A hint naming the given regions, with no pane association. */
  public static Notification of(long segmentId, int... regions) {
    return new Notification(segmentId, maskOf(regions), 0, 0);
  }

  /** A hint naming the given regions for pane {@code viewId} under {@code epoch} (non-zero). */
  public static Notification forPane(long segmentId, int viewId, long epoch, int... regions) {
    return new Notification(segmentId, maskOf(regions), epoch, viewId);
  }

  public static long[] maskOf(int... regions) {
    long[] m = new long[8];
    for (int r : regions) {
      if (r < 0 || r >= MASK_BITS) throw new IllegalArgumentException("region " + r + " is outside the 512-bit mask");
      m[r >> 6] |= 1L << (r & 63);
    }
    return m;
  }

  public boolean isSet(int region) {
    return region >= 0 && region < MASK_BITS && (mask[region >> 6] & (1L << (region & 63))) != 0;
  }

  public boolean emptyMask() {
    for (long w : mask) if (w != 0) return false;
    return true;
  }

  /** The set regions, ascending. */
  public int[] regions() {
    BitSet bs = BitSet.valueOf(mask);
    return bs.stream().toArray();
  }

  @Override
  public long[] mask() {
    return mask.clone();
  }

  /** True when the pane pair is present. */
  public boolean paneAssociated() {
    return epoch != 0;
  }

  static Notification decode(byte[] r) {
    int p = Records.PAYLOAD;
    long[] m = new long[8];
    for (int i = 0; i < 8; i++) m[i] = Records.u64(r, p + 8 + 8 * i);
    return new Notification(Records.u64(r, p), m, Records.u32(r, p + 72), Records.u8(r, p + 76));
  }

  public byte[] encode(long id) {
    byte[] pl = new byte[Records.PAYLOAD_SIZE];
    Records.putU64(pl, 0, segmentId);
    for (int i = 0; i < 8; i++) Records.putU64(pl, 8 + 8 * i, mask[i]);
    Records.putU32(pl, 72, epoch);
    pl[76] = (byte) viewId;
    return Records.seal(Records.OP_D, id, pl);
  }

  @Override
  public boolean equals(Object o) {
    return o instanceof Notification n
        && n.segmentId == segmentId
        && Arrays.equals(n.mask, mask)
        && n.epoch == epoch
        && n.viewId == viewId;
  }

  @Override
  public int hashCode() {
    return Long.hashCode(segmentId) * 31 + Arrays.hashCode(mask);
  }

  @Override
  public String toString() {
    return "D{segment=" + Long.toUnsignedString(segmentId) + " regions=" + Arrays.toString(regions())
        + (epoch != 0 ? " pane=" + viewId + " epoch=" + epoch : "") + "}";
  }
}
