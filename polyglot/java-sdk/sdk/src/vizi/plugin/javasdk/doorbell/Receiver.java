package vizi.plugin.javasdk.doorbell;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.nativeio.Posix;

/**
 * Classifies received frames in doorbell_protocol.md §7.5's order and decodes {@code M}, {@code
 * E} and {@code D}. It performs no I/O and holds no lock of its own: the transport feeds it
 * whatever the socket returned, under the one serialization that also covers the lifecycle
 * inputs, and emits what it returns in order.
 *
 * <p>Every exit path closes every descriptor that arrived, except a descriptor handed up with a
 * decoded {@code M}, which is claimable until the consumer's callback returns.
 */
final class Receiver {

  /** What one frame produced; {@code terminal} is set when it faulted. */
  record Result(List<Outcome> outcomes, @Nullable DoorbellFault terminal, boolean teardown) {}

  private final TransportClass transportClass;
  private final InputMachine input = new InputMachine();
  private long ordinal;
  private long expectedId = 1;
  private final byte[] window = new byte[Records.SIZE];
  private int windowFill;
  final Stats stats;

  Receiver(TransportClass transportClass, Stats stats) {
    this.transportClass = transportClass;
    this.stats = stats;
  }

  InputMachine input() {
    return input;
  }

  long ordinal() {
    return ordinal;
  }

  /** Bytes buffered in a partial stream window. */
  byte[] partialWindow() {
    return Arrays.copyOf(window, windowFill);
  }

  // ---- message transports: one datagram is one frame ----

  Result onDatagram(byte[] data, long n, int[] fds, boolean ctrunc) {
    List<Outcome> out = new ArrayList<>();
    // A 0-byte read with no ancillary data is not a frame: teardown on SEQPACKET, bad_length on DGRAM.
    if (n == 0 && fds.length == 0 && !ctrunc) {
      if (transportClass == TransportClass.SEQPACKET) return new Result(out, null, true);
    }
    ordinal++;
    byte[] capture = Arrays.copyOf(data, (int) Math.min(n, data.length));
    // 1. frame acquisition: MSG_CTRUNC and the protocol-wide cap of one descriptor, for any op.
    if (ctrunc) {
      closeAll(fds);
      return fault(out, capture, n, true, FaultClass.CTRUNC, "MSG_CTRUNC: descriptors were lost");
    }
    if (fds.length > 1) {
      closeAll(fds);
      return fault(out, capture, n, false, FaultClass.WRONG_FD_COUNT, fds.length + " descriptors on one record; the cap is one");
    }
    if (n != Records.SIZE) {
      closeAll(fds);
      String what = n == 0 ? "an empty datagram" : n < Records.SIZE ? "an undersized datagram" : "an oversized datagram";
      return fault(out, capture, n, false, FaultClass.BAD_LENGTH, what + " (" + n + " bytes)");
    }
    return onRecord(capture, n, fds, out);
  }

  // ---- stream transport: accumulate exactly 112 bytes, arbitrary chunking ----

  /** Feeds {@code len} received bytes; may complete zero or more records. */
  Result onStreamBytes(byte[] chunk, int len) {
    List<Outcome> out = new ArrayList<>();
    int off = 0;
    while (off < len) {
      int take = Math.min(len - off, Records.SIZE - windowFill);
      System.arraycopy(chunk, off, window, windowFill, take);
      windowFill += take;
      off += take;
      if (windowFill == Records.SIZE) {
        windowFill = 0;
        ordinal++;
        Result r = onRecord(window.clone(), Records.SIZE, new int[0], out);
        if (r.terminal() != null) return r;
      }
    }
    return new Result(out, null, false);
  }

  // ---- §7.5 steps 2–7 on an acquired 112-byte frame ----

  private Result onRecord(byte[] rec, long n, int[] fds, List<Outcome> out) {
    stats.framesExamined.increment();
    // 2. CRC before any field is trusted
    if (Records.computedCrc(rec) != Records.storedCrc(rec)) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.BAD_CRC, "crc mismatch");
    }
    // 3. header
    if (rec[1] != Records.VERSION) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.BAD_VER, "ver " + (rec[1] & 0xff));
    }
    if (rec[2] != 0) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.BAD_FLAGS, "flags 0x" + Integer.toHexString(rec[2] & 0xff));
    }
    if (rec[3] != 0) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.BAD_RESERVED, "reserved 0x" + Integer.toHexString(rec[3] & 0xff));
    }
    // id tracking: after step 3, before step 4, for every frame that got this far
    List<String> diags = new ArrayList<>();
    trackId(Records.id(rec), diags);
    // 4. op
    int op = Records.op(rec);
    if (op == Records.OP_RESERVED) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.RESERVED_OP, "op 0xFF outside a negotiated recovery");
    }
    if (op != Records.OP_M && op != Records.OP_E && op != Records.OP_D) {
      closeAll(fds);
      stats.unknownOps.increment();
      out.add(new Outcome.Frame(ordinal, rec, new Outcome.UnknownOp(op), diags));
      return new Result(out, null, false);
    }
    // 5. the op's declared descriptor count on this transport class
    int declared = op == Records.OP_M && transportClass.messages() ? 1 : 0;
    if (fds.length != declared) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.WRONG_FD_COUNT,
          (char) op + " declares " + declared + " descriptor(s) here and carried " + fds.length);
    }
    // 6. fixed padding (payload-relative: E [92..100), D [77..100), M byte 17)
    int p = Records.PAYLOAD;
    boolean padOk = switch (op) {
      case Records.OP_E -> Records.allZero(rec, p + 92, p + 100);
      case Records.OP_D -> Records.allZero(rec, p + 77, p + 100);
      default -> rec[p + 17] == 0;
    };
    if (!padOk) {
      closeAll(fds);
      return fault(out, rec, n, false, FaultClass.BAD_PADDING, "non-zero fixed padding in " + (char) op);
    }
    // 7. the op's own rules: reported and dropped, never a fault
    switch (op) {
      case Records.OP_M -> onGrant(rec, fds, diags, out);
      case Records.OP_E -> onInput(rec, diags, out);
      default -> onNotification(rec, diags, out);
    }
    return new Result(out, null, false);
  }

  private void onGrant(byte[] rec, int[] fds, List<String> diags, List<Outcome> out) {
    MemoryGrant g = MemoryGrant.decode(rec);
    if (g.segmentId() == 0) {
      closeAll(fds);
      out.add(new Outcome.Frame(ordinal, rec, new Outcome.Rejected(Violation.ZERO_ID), diags));
      return;
    }
    String failure = MemoryGrant.recordFailure(g, rec, transportClass == TransportClass.STREAM);
    if (failure != null) {
      closeAll(fds);
      out.add(new Outcome.Frame(ordinal, rec, new Outcome.GrantRejected(g.segmentId(), g, failure), diags));
      return;
    }
    if (transportClass.messages()) {
      int len = g.nameLen();
      if (len != 0 || !Records.allZero(rec, Records.PAYLOAD + 19, Records.PAYLOAD + Records.PAYLOAD_SIZE)) {
        diags.add("unix grant carries name bytes (name_len " + len + "): ignored");
      }
    }
    Optional<Descriptor> d = fds.length == 1 ? Optional.of(new Descriptor(fds[0])) : Optional.empty();
    out.add(new Outcome.Frame(ordinal, rec, new Outcome.Decoded(new Outcome.GrantBody(g, Records.id(rec), d)), diags));
  }

  private void onInput(byte[] rec, List<String> diags, List<Outcome> out) {
    InputRecord ev = InputRecord.decode(rec);
    InputMachine.Decision r = input.onRecord(ev, rec);
    List<String> all = new ArrayList<>(diags);
    all.addAll(r.diagnostics());
    if (r.disposition() instanceof Outcome.Ignored) stats.inputIgnored.increment();
    out.add(new Outcome.Frame(ordinal, rec, r.disposition(), all));
    out.addAll(r.after());
  }

  private void onNotification(byte[] rec, List<String> diags, List<Outcome> out) {
    Notification d = Notification.decode(rec);
    if (d.segmentId() == 0) {
      out.add(new Outcome.Frame(ordinal, rec, new Outcome.Rejected(Violation.ZERO_ID), diags));
      return;
    }
    if (d.emptyMask()) {
      stats.hintsIgnored.increment();
      out.add(new Outcome.Frame(ordinal, rec, new Outcome.Ignored("all-zero mask"), diags));
      return;
    }
    if (d.epoch() == 0 && d.viewId() != 0) {
      diags.add("view_id " + d.viewId() + " with epoch 0: the pane field is ignored");
      d = new Notification(d.segmentId(), d.mask(), 0, 0);
    }
    out.add(new Outcome.Frame(ordinal, rec, new Outcome.Decoded(new Outcome.NotificationBody(d)), diags));
  }

  /**
   * Expected-next starts at 1 and advances modulo 2³², only on an in-sequence or ahead id; a
   * regression leaves it where it was (doorbell_protocol.md §4).
   */
  private void trackId(long id, List<String> diags) {
    long delta = (id - expectedId) & 0xffffffffL;
    if (delta == 0) {
      expectedId = (id + 1) & 0xffffffffL;
    } else if (delta < 0x80000000L) {
      stats.idGaps.increment();
      diags.add("id gap: expected " + expectedId + ", got " + id);
      expectedId = (id + 1) & 0xffffffffL;
    } else {
      stats.idRegressions.increment();
      diags.add("id regression: expected " + expectedId + ", got " + id);
    }
  }

  private Result fault(List<Outcome> out, byte[] capture, long n, boolean ctrunc, FaultClass c, String detail) {
    DoorbellFault f = DoorbellFault.onFrame(c, capture, n, ctrunc, ordinal, transportClass, detail);
    out.add(new Outcome.Frame(ordinal, capture, new Outcome.FramingRejection(c), List.of()));
    return new Result(out, f, false);
  }

  static void closeAll(int[] fds) {
    for (int fd : fds) Posix.close(fd);
  }
}
