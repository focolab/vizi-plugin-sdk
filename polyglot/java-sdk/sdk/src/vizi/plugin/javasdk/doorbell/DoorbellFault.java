package vizi.plugin.javasdk.doorbell;

import java.util.Optional;
import java.util.OptionalInt;
import java.util.OptionalLong;

/**
 * The transport's facts about a terminal fault, as doorbell_protocol.md §12 lists them. Which
 * are present depends on the class (§12's table), and every absence is absent rather than zero.
 *
 * @param capture the bytes held when the fault was raised: a whole record, the head of an
 *     oversized datagram, or a partial stream window
 * @param observedLen bytes the receive returned (for {@code short_write}: bytes the send accepted)
 * @param ordinal frames this receiver had examined, counting the failing one
 * @param osError the raw errno or WSA code
 */
public record DoorbellFault(
    FaultClass failureClass,
    Optional<byte[]> capture,
    OptionalLong observedLen,
    Optional<Boolean> msgCtrunc,
    OptionalLong ordinal,
    OptionalInt osError,
    OptionalLong expectedCrc,
    OptionalLong observedCrc,
    TransportClass transportClass,
    String direction,
    String detail) {

  /** Direction names: this transport only ever faults on what it received or sent. */
  public static final String RECEIVE = "host_to_plugin";

  public static final String SEND = "plugin_to_host";

  /** A receive-side fault on a frame that was read (every class but {@code recv_error}). */
  static DoorbellFault onFrame(
      FaultClass c, byte[] capture, long observedLen, boolean ctrunc, long ordinal, TransportClass tc, String detail) {
    boolean crcs = capture.length >= Records.SIZE;
    return new DoorbellFault(
        c,
        Optional.of(capture.clone()),
        OptionalLong.of(observedLen),
        Optional.of(ctrunc),
        OptionalLong.of(ordinal),
        OptionalInt.empty(),
        crcs ? OptionalLong.of(Records.computedCrc(capture)) : OptionalLong.empty(),
        crcs ? OptionalLong.of(Records.storedCrc(capture)) : OptionalLong.empty(),
        tc,
        RECEIVE,
        detail);
  }

  static DoorbellFault recvError(int errno, long ordinal, TransportClass tc, String detail) {
    return new DoorbellFault(
        FaultClass.RECV_ERROR, Optional.empty(), OptionalLong.empty(), Optional.empty(), OptionalLong.of(ordinal),
        OptionalInt.of(errno), OptionalLong.empty(), OptionalLong.empty(), tc, RECEIVE, detail);
  }

  static DoorbellFault shortWrite(long accepted, int errno, TransportClass tc, String detail) {
    return new DoorbellFault(
        FaultClass.SHORT_WRITE, Optional.empty(), OptionalLong.of(accepted), Optional.empty(), OptionalLong.empty(),
        OptionalInt.of(errno), OptionalLong.empty(), OptionalLong.empty(), tc, SEND, detail);
  }

  static DoorbellFault ambiguousSend(int errno, TransportClass tc, String detail) {
    return new DoorbellFault(
        FaultClass.AMBIGUOUS_SEND, Optional.empty(), OptionalLong.empty(), Optional.empty(), OptionalLong.empty(),
        OptionalInt.of(errno), OptionalLong.empty(), OptionalLong.empty(), tc, SEND, detail);
  }

  @Override
  public String toString() {
    return "DoorbellFault{" + failureClass.wireName() + ", " + detail + "}";
  }
}
