package vizi.plugin.javasdk.doorbell;

import java.util.Locale;

/**
 * doorbell_protocol.md §12's closed set of fault classes. A plugin raises twelve of the thirteen:
 * {@link #ILLEGAL_DIRECTION} is host-only, since every op is legal inbound to a plugin (§11a).
 * The set is total by construction, so there is no catch-all.
 */
public enum FaultClass {
  BAD_CRC,
  BAD_LENGTH,
  BAD_VER,
  BAD_FLAGS,
  BAD_RESERVED,
  BAD_PADDING,
  CTRUNC,
  WRONG_FD_COUNT,
  RESERVED_OP,
  ILLEGAL_DIRECTION,
  SHORT_WRITE,
  AMBIGUOUS_SEND,
  RECV_ERROR;

  /** The §12 spelling, e.g. {@code bad_crc}. */
  public String wireName() {
    return name().toLowerCase(Locale.ROOT);
  }

  public boolean sendSide() {
    return this == SHORT_WRITE || this == AMBIGUOUS_SEND;
  }
}
