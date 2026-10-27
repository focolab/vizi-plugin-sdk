package vizi.plugin.javasdk.doorbell;

import java.nio.charset.StandardCharsets;
import java.util.Arrays;

/**
 * A decoded {@code M} payload (doorbell_M_memory.md §4).
 *
 * <pre>
 *   [0..8) segment_id u64  [8..16) size u64  [16] perms  [17] padding (framing's)
 *   [18] name_len  [19..) name, NUL, zero to [100)
 * </pre>
 *
 * @param nameBytes the raw name bytes {@code [19..19+name_len)} when {@code name_len <= 80}, else
 *     empty. On unix the field is ignored whole and these are diagnostic only.
 */
public record MemoryGrant(long segmentId, long size, int perms, int nameLen, byte[] nameBytes) {

  public static final int PLUGIN_READ = 0x01;
  public static final int PLUGIN_WRITE = 0x02;
  public static final int HOST_READ = 0x04;
  public static final int HOST_WRITE = 0x08;
  public static final int RESERVED_PERMS = 0xF0;
  public static final int MAX_NAME = 80;

  /** Minimum size: the 32-byte preamble. */
  public static final long PREAMBLE = 32;

  public boolean pluginWritable() {
    return (perms & PLUGIN_WRITE) != 0;
  }

  /** The section name as text (Windows), meaningful only once validated. */
  public String name() {
    return new String(nameBytes, StandardCharsets.UTF_8);
  }

  static MemoryGrant decode(byte[] r) {
    int p = Records.PAYLOAD;
    int nameLen = Records.u8(r, p + 18);
    byte[] name = nameLen <= MAX_NAME ? Arrays.copyOfRange(r, p + 19, p + 19 + nameLen) : new byte[0];
    return new MemoryGrant(Records.u64(r, p), Records.u64(r, p + 8), Records.u8(r, p + 16), nameLen, name);
  }

  /**
   * The record-local checks doorbell_M_memory.md §7 assigns to the transport, excluding {@code
   * segment_id == 0} (which reports on ContractViolation, not the grant ack). Returns null when
   * the grant may be handed up, else the failure detail (class {@code malformed}).
   */
  static @org.jspecify.annotations.Nullable String recordFailure(MemoryGrant g, byte[] r, boolean windows) {
    if (Long.compareUnsigned(g.size, PREAMBLE) < 0) {
      return "size " + Long.toUnsignedString(g.size) + " is smaller than the 32-byte preamble";
    }
    if ((g.perms & PLUGIN_READ) == 0) return "perms 0x" + Integer.toHexString(g.perms) + " lacks PLUGIN_READ";
    if ((g.perms & RESERVED_PERMS) != 0) {
      return "perms 0x" + Integer.toHexString(g.perms) + " sets a reserved bit (4-7)";
    }
    if (windows) {
      int p = Records.PAYLOAD;
      int len = g.nameLen;
      if (len == 0) return "the section name is empty";
      if (len > MAX_NAME) return "name_len " + len + " exceeds 80";
      if (r[p + 19 + len] != 0) return "no NUL terminator at payload[" + (19 + len) + "]";
      for (int i = 0; i < len; i++) {
        if (r[p + 19 + i] == 0) return "a NUL byte inside the name at payload[" + (19 + i) + "]";
      }
      if (!validUtf8(r, p + 19, len)) return "the name is not valid UTF-8";
      if (!Records.allZero(r, p + 20 + len, p + Records.PAYLOAD_SIZE)) {
        return "non-zero padding after the name terminator";
      }
    }
    return null;
  }

  private static boolean validUtf8(byte[] b, int off, int len) {
    var dec = StandardCharsets.UTF_8.newDecoder();
    try {
      dec.decode(java.nio.ByteBuffer.wrap(b, off, len));
      return true;
    } catch (java.nio.charset.CharacterCodingException e) {
      return false;
    }
  }

  /** Encodes this grant as a record with the given id (hosts, test peers and the testkit). */
  public byte[] encode(long id) {
    byte[] pl = new byte[Records.PAYLOAD_SIZE];
    Records.putU64(pl, 0, segmentId);
    Records.putU64(pl, 8, size);
    pl[16] = (byte) perms;
    pl[18] = (byte) nameLen;
    System.arraycopy(nameBytes, 0, pl, 19, Math.min(nameBytes.length, Records.PAYLOAD_SIZE - 20));
    return Records.seal(Records.OP_M, id, pl);
  }

  /** A unix grant: no name. */
  public static MemoryGrant unix(long segmentId, long size, int perms) {
    return new MemoryGrant(segmentId, size, perms, 0, new byte[0]);
  }

  @Override
  public boolean equals(Object o) {
    return o instanceof MemoryGrant g
        && g.segmentId == segmentId
        && g.size == size
        && g.perms == perms
        && g.nameLen == nameLen
        && Arrays.equals(g.nameBytes, nameBytes);
  }

  @Override
  public int hashCode() {
    return Long.hashCode(segmentId) * 31 + Arrays.hashCode(nameBytes);
  }

  @Override
  public String toString() {
    return "M{segment=" + Long.toUnsignedString(segmentId) + " size=" + Long.toUnsignedString(size)
        + " perms=0x" + Integer.toHexString(perms) + (nameLen > 0 ? " name_len=" + nameLen : "") + "}";
  }
}
