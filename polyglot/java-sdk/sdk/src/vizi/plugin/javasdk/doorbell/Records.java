package vizi.plugin.javasdk.doorbell;

import java.util.zip.CRC32;

/**
 * The fixed 112-byte record (doorbell_protocol.md §4) and little-endian field access.
 *
 * <pre>
 *   [0] op  [1] ver=2  [2] flags=0  [3] reserved=0  [4..8) id u32
 *   [8..108) payload   [108..112) crc = CRC-32/ISO-HDLC over [0..108)
 * </pre>
 *
 * {@link CRC32} is exactly CRC-32/ISO-HDLC as zlib computes it (reflected, init and final xor
 * 0xFFFFFFFF); {@code 0xFFFFFFFF} is an ordinary valid checksum.
 */
public final class Records {
  private Records() {}

  public static final int SIZE = 112;
  public static final int PAYLOAD = 8;
  public static final int PAYLOAD_SIZE = 100;
  public static final int CRC_OFFSET = 108;
  public static final int VERSION = 2;

  public static final int OP_M = 0x4D;
  public static final int OP_E = 0x45;
  public static final int OP_D = 0x44;
  public static final int OP_RESERVED = 0xFF;

  /** Receive buffer: strictly larger than a record, so an oversized datagram shows as > 112. */
  public static final int RECEIVE_BUFFER = 120;

  public static long crc(byte[] b, int off, int len) {
    CRC32 c = new CRC32();
    c.update(b, off, len);
    return c.getValue();
  }

  /** The checksum a record's first 108 bytes compute to. */
  public static long computedCrc(byte[] record) {
    return crc(record, 0, CRC_OFFSET);
  }

  /** The checksum stored in bytes [108..112). */
  public static long storedCrc(byte[] record) {
    return u32(record, CRC_OFFSET);
  }

  /** Builds a record: header, a 100-byte payload, and its CRC. */
  public static byte[] seal(int op, long id, byte[] payload) {
    if (payload.length != PAYLOAD_SIZE) throw new IllegalArgumentException("payload must be 100 bytes");
    byte[] r = new byte[SIZE];
    r[0] = (byte) op;
    r[1] = (byte) VERSION;
    putU32(r, 4, id);
    System.arraycopy(payload, 0, r, PAYLOAD, PAYLOAD_SIZE);
    putU32(r, CRC_OFFSET, computedCrc(r));
    return r;
  }

  /** Rewrites the id and the CRC of an existing record (for a writer that allocates on admission). */
  public static void restamp(byte[] record, long id) {
    putU32(record, 4, id);
    putU32(record, CRC_OFFSET, computedCrc(record));
  }

  public static int op(byte[] r) {
    return r[0] & 0xff;
  }

  public static long id(byte[] r) {
    return u32(r, 4);
  }

  // ---- little-endian access ----

  public static int u8(byte[] b, int off) {
    return b[off] & 0xff;
  }

  public static int u16(byte[] b, int off) {
    return (b[off] & 0xff) | ((b[off + 1] & 0xff) << 8);
  }

  public static long u32(byte[] b, int off) {
    return (b[off] & 0xffL)
        | ((b[off + 1] & 0xffL) << 8)
        | ((b[off + 2] & 0xffL) << 16)
        | ((b[off + 3] & 0xffL) << 24);
  }

  public static long u64(byte[] b, int off) {
    return u32(b, off) | (u32(b, off + 4) << 32);
  }

  public static float f32(byte[] b, int off) {
    return Float.intBitsToFloat((int) u32(b, off));
  }

  public static void putU8(byte[] b, int off, int v) {
    b[off] = (byte) v;
  }

  public static void putU16(byte[] b, int off, int v) {
    b[off] = (byte) v;
    b[off + 1] = (byte) (v >>> 8);
  }

  public static void putU32(byte[] b, int off, long v) {
    b[off] = (byte) v;
    b[off + 1] = (byte) (v >>> 8);
    b[off + 2] = (byte) (v >>> 16);
    b[off + 3] = (byte) (v >>> 24);
  }

  public static void putU64(byte[] b, int off, long v) {
    putU32(b, off, v);
    putU32(b, off + 4, v >>> 32);
  }

  public static void putF32(byte[] b, int off, float v) {
    putU32(b, off, Float.floatToRawIntBits(v) & 0xffffffffL);
  }

  /** True when every byte of {@code b[from..to)} is zero. */
  public static boolean allZero(byte[] b, int from, int to) {
    for (int i = from; i < to; i++) if (b[i] != 0) return false;
    return true;
  }

  /** Lower-case hex, for diagnostics and the cross-check corpus. */
  public static String hex(byte[] b) {
    StringBuilder sb = new StringBuilder(b.length * 2);
    for (byte x : b) sb.append(Character.forDigit((x >> 4) & 0xf, 16)).append(Character.forDigit(x & 0xf, 16));
    return sb.toString();
  }

  public static byte[] unhex(String s) {
    if (s.length() % 2 != 0) throw new IllegalArgumentException("odd-length hex");
    byte[] out = new byte[s.length() / 2];
    for (int i = 0; i < out.length; i++) {
      int hi = Character.digit(s.charAt(2 * i), 16);
      int lo = Character.digit(s.charAt(2 * i + 1), 16);
      if (hi < 0 || lo < 0) throw new IllegalArgumentException("not hex: " + s);
      out[i] = (byte) ((hi << 4) | lo);
    }
    return out;
  }
}
