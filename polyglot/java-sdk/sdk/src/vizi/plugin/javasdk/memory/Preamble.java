package vizi.plugin.javasdk.memory;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.Records;

/**
 * The first 32 bytes of every granted mapping (doorbell_M_memory.md §5):
 *
 * <pre>
 *   [0..8) segment_id  [8..12) padding  [12..28) identity proof  [28..32) CRC-32/ISO-HDLC of [0..28)
 * </pre>
 *
 * The padding word {@code [8..12)} is reserved: a sender writes zero and a receiver does not
 * check it (the checksum still covers it). Bytes from 32 up to the lowest region offset are
 * host-reserved and never read.
 *
 * <p>A fresh zero-filled object fails its own checksum ({@code crc32} of 28 zero bytes is
 * 0x807077e9), which is what catches a Windows create-or-open that landed on a new section.
 */
public final class Preamble {
  private Preamble() {}

  public static final int SIZE = 32;

  /** Null when the preamble checksums and names {@code segmentId}; else why not. */
  static @Nullable String check(MemorySegment m, long segmentId) {
    byte[] p = m.asSlice(0, SIZE).toArray(ValueLayout.JAVA_BYTE);
    long stored = Records.u32(p, 28);
    long computed = Records.crc(p, 0, 28);
    if (stored != computed) {
      return "preamble checksum 0x" + Long.toHexString(stored) + " != computed 0x" + Long.toHexString(computed)
          + (computed == 0x807077e9L ? " (the object is zero-filled: not the host's)" : "");
    }
    long id = Records.u64(p, 0);
    if (id != segmentId) {
      return "preamble names segment " + Long.toUnsignedString(id) + ", the grant " + Long.toUnsignedString(segmentId);
    }
    return null;
  }

  /** The 16 identity-proof bytes, read back out of the mapping, never echoed from the record. */
  static byte[] proof(MemorySegment m) {
    return m.asSlice(12, 16).toArray(ValueLayout.JAVA_BYTE);
  }

  /** Writes a preamble (hosts, test peers and the testkit). */
  public static void write(MemorySegment m, long segmentId, byte[] proof) {
    if (proof.length != 16) throw new IllegalArgumentException("the identity proof is 16 bytes");
    byte[] p = new byte[SIZE];
    Records.putU64(p, 0, segmentId);
    System.arraycopy(proof, 0, p, 12, 16);
    Records.putU32(p, 28, Records.crc(p, 0, 28));
    MemorySegment.copy(p, 0, m, ValueLayout.JAVA_BYTE, 0, SIZE);
  }
}
