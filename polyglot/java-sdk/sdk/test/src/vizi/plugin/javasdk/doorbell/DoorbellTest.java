package vizi.plugin.javasdk.doorbell;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.function.Predicate;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;

/** The transport over real sockets: framing, stream chunking, lifecycle, sending, terminals. */
class DoorbellTest {

  /** Records every outcome, in emission order; tests wait on the log, never sleep for it. */
  static final class Log implements Doorbell.Listener {
    final List<Outcome> seen = new ArrayList<>();
    boolean claimGrants;
    boolean failFirst; // the first frame's handler throws an Error
    final List<Integer> claimed = new ArrayList<>();

    @Override
    public synchronized void onFrame(Outcome.Frame f) {
      if (claimGrants && f.disposition() instanceof Outcome.Decoded(Outcome.GrantBody g)) {
        g.descriptor().ifPresent(d -> claimed.add(d.claim()));
      }
      seen.add(f);
      notifyAll();
      if (failFirst && seen.size() == 1) throw new AssertionError("a handler's assertion");
    }

    @Override
    public synchronized void onCancellation(Outcome.Cancellation c) {
      seen.add(c);
      notifyAll();
    }

    @Override
    public synchronized void onTerminal(Outcome t) {
      seen.add(t);
      notifyAll();
    }

    synchronized List<Outcome> await(Predicate<List<Outcome>> done) throws InterruptedException {
      long deadline = System.nanoTime() + Duration.ofSeconds(5).toNanos();
      while (!done.test(seen)) {
        long left = deadline - System.nanoTime();
        if (left <= 0) throw new AssertionError("timed out; saw " + seen);
        wait(Math.max(1, left / 1_000_000));
      }
      return new ArrayList<>(seen);
    }

    List<Outcome> awaitFrames(int n) throws InterruptedException {
      return await(l -> l.stream().filter(o -> o instanceof Outcome.Frame).count() >= n || terminal(l));
    }

    List<Outcome> awaitTerminal() throws InterruptedException {
      return await(DoorbellTest::terminal);
    }
  }

  static boolean terminal(List<Outcome> l) {
    return l.stream().anyMatch(o -> o instanceof Outcome.Fault || o instanceof Outcome.Teardown);
  }

  int hostFd = -1;
  Doorbell bell;
  final Log log = new Log();
  final List<Outcome> sunk = new ArrayList<>();

  void open(int type) throws Exception {
    assumeTrue(Platform.isUnix(), "unix sockets");
    if (type == Posix.SOCK_SEQPACKET) assumeTrue(Platform.OS == Platform.LINUX, "SEQPACKET is Linux's");
    int[] sv = Posix.socketpair(type);
    hostFd = sv[0];
    bell = Doorbell.adoptForTest(sv[1], log, t -> {
      synchronized (sunk) {
        sunk.add(t);
      }
    });
  }

  @AfterEach
  void close() throws Exception {
    if (bell != null) {
      bell.stop();
      assertTrue(bell.join(Duration.ofSeconds(5)));
    }
    if (hostFd >= 0) Posix.close(hostFd);
  }

  void send(byte[] rec, int... fds) {
    assertEquals(rec.length, Posix.sendmsg(hostFd, rec, fds).n());
  }

  static byte[] press(long interaction, long tool, int view, long epoch) {
    return InputRecord.builder().phase(InputRecord.PRESS).interaction(interaction).tool(tool).viewId(view)
        .epoch(epoch).voxel(1, 2, 3).build().encode(interaction);
  }

  static byte[] next(int phase, long interaction, long seq, long tool, int view, long epoch) {
    return InputRecord.builder().phase(phase).interaction(interaction).seq(seq).tool(tool).viewId(view)
        .epoch(epoch).voxel(1, 2, 3).build().encode(100 + seq);
  }

  static Outcome.Disposition disp(Outcome o) {
    return ((Outcome.Frame) o).disposition();
  }

  // ---- primitives ----

  @Test
  void crcCheckValueAndRecordLayout() {
    byte[] s = "123456789".getBytes(java.nio.charset.StandardCharsets.US_ASCII);
    assertEquals(0xCBF43926L, Records.crc(s, 0, s.length));
    assertEquals(0x807077e9L, Records.crc(new byte[28], 0, 28), "a zero preamble fails its own checksum");
    byte[] r = Notification.of(7, 0, 9, 511).encode(0xFFFFFFFFL);
    assertEquals(112, r.length);
    assertEquals('D', r[0]);
    assertEquals(2, r[1]);
    assertEquals(0xFFFFFFFFL, Records.id(r));
    assertEquals(Records.computedCrc(r), Records.storedCrc(r));
    // bit order: region r is bit r&7 of payload byte 8+(r>>3)
    assertEquals(0x01, r[8 + 8] & 0xff);
    assertEquals(0x02, r[8 + 8 + 1] & 0xff);
    assertEquals(0x80, r[8 + 8 + 63] & 0xff);
    Notification back = Notification.decode(r);
    assertArrayEquals(new int[] {0, 9, 511}, back.regions());
  }

  @Test
  void inputRecordRoundTrips() {
    InputRecord e = InputRecord.builder().phase(InputRecord.FIRE).interaction(0xFFFFFFFEL).tool(3).viewId(255)
        .epoch(9).gen(0x80000000L).channels(0x8001).mods(InputRecord.MOD_SHIFT).clickCount(2).action(3)
        .target(InputRecord.TARGET_ROI, 5, 1).ray(new float[] {1, 2, 3}, new float[] {4, 5, 6}, 0.25f, 1.5f)
        .build();
    byte[] r = e.encode(42);
    InputRecord d = InputRecord.decode(r);
    assertEquals(e, d);
    assertEquals(0xFFFFFFFEL, d.interaction(), "u32 read unsigned");
    assertEquals(0x80000000L, d.gen());
    assertEquals(0x8001, d.channels());
    assertEquals(3, d.action());
    assertEquals(0.25f, d.position(6));
  }

  // ---- RAY_AND_PICKED carries back_frac; the union is 48 bytes, the padding [92..100) ----

  static final float[] CAMERA = {-10, 4, 4}, FAR = {20, 4, 4}, PICK = {5, 4, 4};

  static InputRecord picked(long interaction, float backFrac) {
    return InputRecord.builder().phase(InputRecord.FIRE).interaction(interaction)
        .rayAndPicked(CAMERA, FAR, 0.25f, 0f, PICK, backFrac).build();
  }

  @Test
  void aRayAndPickedRoundTripsWithItsBackFrac() {
    assertEquals(48, InputRecord.positionBytes(InputRecord.KIND_RAY_AND_PICKED));
    byte[] r = picked(1, 0.75f).encode(1);
    assertEquals(0.75f, Records.f32(r, Records.PAYLOAD + 88), "back_frac at union offset 44, payload 88");
    InputRecord d = InputRecord.decode(r);
    assertEquals(picked(1, 0.75f), d);
    assertEquals(0.75f, d.position(11));
    assertTrue(Float.isNaN(InputRecord.decode(picked(2, Float.NaN).encode(2)).position(11)), "NaN survives: a point pick");
  }

  /** Step 5 on its own: back_frac NaN or in [0, 1]; everything else in the ray still finite. */
  @Test
  void backFracIsNaNOrAFractionAndNothingElse() {
    InputMachine m = new InputMachine();
    assertTrue(m.toolActivated(1).ok());
    assertTrue(m.epochAcknowledged(0, 1).ok());
    long[] id = {0};
    java.util.function.Function<InputRecord, Outcome.Disposition> rule = ev -> {
      byte[] raw = ev.encode(++id[0]);
      return m.onRecord(InputRecord.decode(raw), raw).disposition();
    };
    for (float ok : new float[] {Float.NaN, 0f, 0.5f, 1f}) {
      assertInstanceOf(Outcome.Decoded.class, rule.apply(picked(id[0] + 1, ok)), "back_frac " + ok);
    }
    for (float bad : new float[] {1.25f, -0.5f, Float.POSITIVE_INFINITY, Float.NEGATIVE_INFINITY}) {
      assertEquals(new Outcome.Rejected(Violation.NONFINITE_POSITION), rule.apply(picked(id[0] + 1, bad)), "back_frac " + bad);
    }
    // the blanket check still covers the eleven values before it: a NaN picked voxel is refused
    InputRecord nanPick = InputRecord.builder().phase(InputRecord.FIRE).interaction(id[0] + 1)
        .rayAndPicked(CAMERA, FAR, 0.25f, 0f, new float[] {Float.NaN, 4, 4}, 0.5f).build();
    assertEquals(new Outcome.Rejected(Violation.NONFINITE_POSITION), rule.apply(nanPick));
    // a pick on a DRAG is the host's opinion, not a receiver fault
    InputRecord press = InputRecord.builder().phase(InputRecord.PRESS).interaction(500).ray(CAMERA, FAR, 0.25f, 0f).build();
    assertInstanceOf(Outcome.Decoded.class, rule.apply(press));
    InputRecord drag = InputRecord.builder().phase(InputRecord.DRAG).interaction(500).seq(1)
        .rayAndPicked(CAMERA, FAR, 0.25f, 0f, PICK, 0.5f).build();
    assertInstanceOf(Outcome.Decoded.class, rule.apply(drag));
  }

  /** A record with payload byte {@code at} set, resealed so only that byte differs. */
  static byte[] withByte(byte[] rec, int at) {
    byte[] payload = java.util.Arrays.copyOfRange(rec, Records.PAYLOAD, Records.PAYLOAD + Records.PAYLOAD_SIZE);
    payload[at] = 1;
    return Records.seal(Records.OP_E, Records.id(rec), payload);
  }

  @Test
  void aStrayByteAtPayload88IsTheRecordsOwnPositionTail() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    assertTrue(bell.toolActivated(1).ok());
    assertTrue(bell.epochAcknowledged(0, 1).ok());
    send(withByte(press(1, 1, 0, 1), 88));
    send(withByte(press(2, 1, 0, 1), 91));
    List<Outcome> l = log.awaitFrames(2);
    assertEquals(new Outcome.Rejected(Violation.POSITION_TAIL), disp(l.get(0)), "byte 88 is inside the union now");
    assertEquals(new Outcome.Rejected(Violation.POSITION_TAIL), disp(l.get(1)));
    assertFalse(terminal(l), "a position tail is the record's own, never fatal");
  }

  @Test
  void aByteInPadding92To100IsFatal() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(withByte(press(1, 1, 0, 1), 92));
    List<Outcome> l = log.awaitTerminal();
    assertEquals(FaultClass.BAD_PADDING, ((Outcome.Fault) l.get(l.size() - 1)).fault().failureClass());
  }

  // ---- framing ----

  @Test
  void aGrantIsHandedUpWithAClaimableDescriptor() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    log.claimGrants = true;
    int shm = Posix.createSharedObject("grant", 4096);
    send(MemoryGrant.unix(5, 4096, MemoryGrant.PLUGIN_READ | MemoryGrant.PLUGIN_WRITE).encode(1), shm);
    Outcome o = log.awaitFrames(1).get(0);
    Outcome.GrantBody g = (Outcome.GrantBody) ((Outcome.Decoded) disp(o)).body();
    assertEquals(5, g.grant().segmentId());
    assertEquals(1, g.recordId());
    assertEquals(1, log.claimed.size());
    assertEquals(4096, Posix.objectSize(log.claimed.get(0)), "the claimed descriptor is still open");
    Posix.close(log.claimed.get(0));
    Posix.close(shm);
  }

  @Test
  void grantRecordFailuresRideTheAckAndZeroIdIsAViolation() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    int shm = Posix.createSharedObject("grant", 4096);
    send(MemoryGrant.unix(0, 4096, 1).encode(1), shm);
    send(MemoryGrant.unix(6, 16, 1).encode(2), shm);
    send(MemoryGrant.unix(7, 4096, MemoryGrant.PLUGIN_WRITE).encode(3), shm);
    send(MemoryGrant.unix(8, 4096, 0x11).encode(4), shm);
    send(new MemoryGrant(9, 4096, 1, 3, "abc".getBytes()).encode(5), shm);
    List<Outcome> l = log.awaitFrames(5);
    assertEquals(new Outcome.Rejected(Violation.ZERO_ID), disp(l.get(0)));
    assertInstanceOf(Outcome.GrantRejected.class, disp(l.get(1)));
    assertInstanceOf(Outcome.GrantRejected.class, disp(l.get(2)));
    assertInstanceOf(Outcome.GrantRejected.class, disp(l.get(3)));
    Outcome.Frame named = (Outcome.Frame) l.get(4);
    assertInstanceOf(Outcome.Decoded.class, named.disposition(), "a unix name is ignored whole");
    assertFalse(named.diagnostics().isEmpty(), "and diagnosed locally");
    Posix.close(shm);
  }

  @Test
  void wrongLengthsAndDescriptorCountsFault() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    int[] spare = Posix.socketpair(Posix.SOCK_DGRAM);
    send(Notification.of(1, 3).encode(1), spare[0]);
    List<Outcome> l = log.awaitTerminal();
    assertEquals(new Outcome.FramingRejection(FaultClass.WRONG_FD_COUNT), disp(l.get(0)));
    Outcome.Fault f = (Outcome.Fault) l.get(1);
    assertEquals(FaultClass.WRONG_FD_COUNT, f.fault().failureClass());
    assertTrue(f.fault().expectedCrc().isPresent(), "112 bytes were read, so the CRCs are present");
    assertEquals(1, f.fault().ordinal().getAsLong());
    synchronized (sunk) {
      assertEquals(List.of(f), sunk, "the terminal went to the sink as well");
    }
    assertEquals(Doorbell.SendResult.NOT_READY, bell.sendHint(Notification.of(1, 1)));
    bell.enableSends();
    assertEquals(Doorbell.SendResult.CLOSED, bell.sendHint(Notification.of(1, 1)), "no send after the fault");
    assertFalse(bell.toolActivated(1).ok(), "a lifecycle input fails after the latch");
    Posix.close(spare[0]);
    Posix.close(spare[1]);
  }

  @Test
  void anUndersizedDatagramIsBadLengthWithNoCrcs() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(new byte[20]);
    List<Outcome> l = log.awaitTerminal();
    Outcome.Fault f = (Outcome.Fault) l.get(l.size() - 1);
    assertEquals(FaultClass.BAD_LENGTH, f.fault().failureClass());
    assertEquals(20, f.fault().observedLen().getAsLong());
    assertEquals(20, f.fault().capture().orElseThrow().length);
    assertTrue(f.fault().expectedCrc().isEmpty(), "absent, never zero");
    assertTrue(f.fault().osError().isEmpty());
  }

  @Test
  void anOversizedDatagramIsBadLengthAndKeepsTheSurplus() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(new byte[130]);
    List<Outcome> l = log.awaitTerminal();
    Outcome.Fault f = (Outcome.Fault) l.get(l.size() - 1);
    assertEquals(FaultClass.BAD_LENGTH, f.fault().failureClass());
    assertEquals(120, f.fault().observedLen().getAsLong(), "the read's own length, > 112");
    assertTrue(f.fault().expectedCrc().isPresent());
  }

  @Test
  void aZeroByteDatagramIsBadLengthOnDgramButTeardownOnSeqpacket() throws Exception {
    open(Posix.SOCK_DGRAM);
    send(new byte[0]);
    List<Outcome> l = log.awaitTerminal();
    assertEquals(FaultClass.BAD_LENGTH, ((Outcome.Fault) l.get(l.size() - 1)).fault().failureClass());
  }

  @Test
  void aPeerCloseOnSeqpacketIsTeardownNotAFault() throws Exception {
    open(Posix.SOCK_SEQPACKET);
    Posix.close(hostFd);
    hostFd = -1;
    List<Outcome> l = log.awaitTerminal();
    assertInstanceOf(Outcome.Teardown.class, l.get(l.size() - 1));
  }

  @Test
  void anUnknownOpIsSkippedItsDescriptorClosedAndFramingContinues() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    int[] spare = Posix.socketpair(Posix.SOCK_DGRAM);
    byte[] payload = new byte[100];
    payload[5] = 9; // padding is not checked on an unknown op
    send(Records.seal('Z', 1, payload), spare[0]);
    send(Notification.of(3, 2).encode(3));
    List<Outcome> l = log.awaitFrames(2);
    assertEquals(new Outcome.UnknownOp('Z'), disp(l.get(0)));
    Outcome.Frame f = (Outcome.Frame) l.get(1);
    assertInstanceOf(Outcome.Decoded.class, f.disposition());
    assertTrue(f.diagnostics().stream().anyMatch(s -> s.contains("id gap")), f.diagnostics().toString());
    assertEquals(1, bell.stats().idGaps.sum());
    Posix.close(spare[0]);
    Posix.close(spare[1]);
  }

  @Test
  void idRegressionsAreCountedAndTheRecordStillProcessed() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(Notification.of(3, 1).encode(1));
    send(Notification.of(3, 1).encode(2));
    send(Notification.of(3, 1).encode(1));
    send(Notification.of(3, 1).encode(3));
    List<Outcome> l = log.awaitFrames(4);
    for (Outcome o : l) assertInstanceOf(Outcome.Decoded.class, disp(o));
    assertEquals(1, bell.stats().idRegressions.sum());
    assertEquals(0, bell.stats().idGaps.sum(), "a regression leaves expected-next where it was");
  }

  /** An Error from a handler is reported, and the listener keeps reading. */
  @Test
  void anErrorFromTheConsumerDoesNotStopTheListener() throws Exception {
    log.failFirst = true;
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(Notification.of(3, 1).encode(1));
    send(Notification.of(3, 1).encode(2));
    List<Outcome> l = log.awaitFrames(2);
    assertFalse(terminal(l), "the listener lived: " + l);
    assertEquals(2, l.size());
  }

  @Test
  void idTrackingWrapsThroughZero() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(Notification.of(3, 1).encode(0x7FFFFFFFL)); // a gap: ahead by less than 2^31
    send(Notification.of(3, 1).encode(0xFFFFFFFEL)); // and another
    send(Notification.of(3, 1).encode(0xFFFFFFFFL));
    send(Notification.of(3, 1).encode(0));
    send(Notification.of(3, 1).encode(1));
    log.awaitFrames(5);
    assertEquals(2, bell.stats().idGaps.sum());
    assertEquals(0, bell.stats().idRegressions.sum());
  }

  @Test
  void notificationFieldRules() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    send(new Notification(4, Notification.maskOf(1), 0, 7).encode(1));
    send(Notification.of(4).encode(2));
    send(Notification.forPane(4, 0, 3, 2).encode(3));
    List<Outcome> l = log.awaitFrames(3);
    Outcome.Frame paneless = (Outcome.Frame) l.get(0);
    Outcome.NotificationBody nb = (Outcome.NotificationBody) ((Outcome.Decoded) paneless.disposition()).body();
    assertEquals(0, nb.hint().viewId(), "the pane FIELD is ignored; the record is delivered");
    assertFalse(paneless.diagnostics().isEmpty());
    assertInstanceOf(Outcome.Ignored.class, disp(l.get(1)), "an all-zero mask");
    Outcome.NotificationBody pane0 = (Outcome.NotificationBody) ((Outcome.Decoded) disp(l.get(2))).body();
    assertTrue(pane0.hint().paneAssociated(), "pane 0 is a valid pane: the discriminator is the epoch");
  }

  // ---- stream framing (Windows' class, on a unix stand-in) ----

  @Test
  void streamChunkingReconstructsRecords() throws Exception {
    open(Posix.SOCK_STREAM);
    byte[] a = Notification.of(1, 1).encode(1);
    byte[] b = Notification.of(2, 2).encode(2);
    byte[] both = new byte[224];
    System.arraycopy(a, 0, both, 0, 112);
    System.arraycopy(b, 0, both, 112, 112);
    int[] cuts = {1, 50, 111, 112, 113, 200, 224};
    int prev = 0;
    for (int c : cuts) {
      assertEquals(c - prev, Posix.send(hostFd, both, prev, c - prev, 0).n());
      prev = c;
    }
    List<Outcome> l = log.awaitFrames(2);
    assertEquals(1, ((Outcome.NotificationBody) ((Outcome.Decoded) disp(l.get(0))).body()).hint().segmentId());
    assertEquals(2, ((Outcome.NotificationBody) ((Outcome.Decoded) disp(l.get(1))).body()).hint().segmentId());
  }

  @Test
  void streamEofMidRecordIsTeardownWithTheBytesInTheDiagnostic() throws Exception {
    open(Posix.SOCK_STREAM);
    assertEquals(40, Posix.send(hostFd, new byte[40], 0, 40, 0).n());
    Posix.close(hostFd);
    hostFd = -1;
    List<Outcome> l = log.awaitTerminal();
    Outcome.Teardown t = (Outcome.Teardown) l.get(l.size() - 1);
    assertTrue(t.reason().contains("40 bytes"), t.reason());
  }

  @Test
  void onAStreamAGrantCarriesNoDescriptorAndTheNameIsChecked() throws Exception {
    open(Posix.SOCK_STREAM);
    byte[] ok = new MemoryGrant(3, 4096, 1, 11, "Local\\vizi-1".substring(0, 11).getBytes()).encode(1);
    byte[] empty = MemoryGrant.unix(4, 4096, 1).encode(2);
    byte[] tail = new MemoryGrant(5, 4096, 1, 3, "abc".getBytes()).encode(3);
    tail[8 + 19 + 3 + 2] = 1; // non-zero after the terminator
    Records.restamp(tail, 3);
    for (byte[] r : List.of(ok, empty, tail)) assertEquals(112, Posix.send(hostFd, r, 0, 112, 0).n());
    List<Outcome> l = log.awaitFrames(3);
    Outcome.GrantBody g = (Outcome.GrantBody) ((Outcome.Decoded) disp(l.get(0))).body();
    assertTrue(g.descriptor().isEmpty());
    assertEquals("Local\\vizi-", g.grant().name());
    assertInstanceOf(Outcome.GrantRejected.class, disp(l.get(1)), "an empty name grants nothing");
    assertInstanceOf(Outcome.GrantRejected.class, disp(l.get(2)));
  }

  // ---- E through the transport ----

  @Test
  void lifecycleGatesAndCancellationOrder() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    assertFalse(bell.toolActivated(0).ok());
    assertTrue(bell.toolActivated(2).ok());
    assertFalse(bell.toolActivated(1).ok(), "1 is below the mark: retired forever");
    assertTrue(bell.toolActivated(2).ok(), "a live handle again is a no-op");
    assertFalse(bell.epochAcknowledged(0, 0).ok());
    assertTrue(bell.epochAcknowledged(0, 4).ok());
    send(press(1, 2, 0, 4));
    send(next(InputRecord.DRAG, 1, 1, 2, 0, 4));
    log.awaitFrames(2);
    assertTrue(bell.epochAcknowledged(0, 4).ok(), "equal: a no-op that closes nothing");
    assertFalse(bell.epochAcknowledged(0, 3).ok(), "older is rejected");
    assertTrue(bell.epochAcknowledged(0, 5).ok(), "newer installs and closes the pane");
    List<Outcome> l = log.await(s -> s.size() >= 3);
    assertEquals(new Outcome.Cancellation(1, 2, 0, Outcome.CancelReason.EPOCH_INSTALLED), l.get(2));
    send(next(InputRecord.RELEASE, 1, 2, 2, 0, 4));
    l = log.awaitFrames(3);
    assertInstanceOf(Outcome.Ignored.class, disp(l.get(3)), "a stale record closes nothing");

    send(press(2, 2, 0, 5));
    log.awaitFrames(4);
    assertTrue(bell.toolRetired(2).ok());
    assertTrue(bell.toolRetired(2).ok(), "idempotent");
    assertFalse(bell.toolRetired(9).ok(), "never activated");
    l = log.await(s -> s.size() >= 6);
    assertEquals(new Outcome.Cancellation(2, 2, 0, Outcome.CancelReason.TOOL_RETIRED), l.get(5));
  }

  @Test
  void aTerminalsCancellationsPrecedeIt() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    bell.toolActivated(1);
    bell.toolActivated(2);
    bell.epochAcknowledged(3, 1);
    send(press(1, 1, 3, 1));
    send(press(2, 2, 3, 1));
    log.awaitFrames(2);
    bell.stop();
    List<Outcome> l = log.awaitTerminal();
    assertEquals(Outcome.CancelReason.TERMINAL, ((Outcome.Cancellation) l.get(2)).reason());
    assertEquals(Outcome.CancelReason.TERMINAL, ((Outcome.Cancellation) l.get(3)).reason());
    assertInstanceOf(Outcome.Teardown.class, l.get(4));
    bell.stop();
    assertEquals(5, log.await(s -> true).size(), "a second stop emits nothing");
  }

  // ---- sending ----

  @Test
  void hintsAreSentOnceDroppedOnBackpressureAndNeverBeforeTheHandshake() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    assertEquals(Doorbell.SendResult.NOT_READY, bell.sendHint(Notification.of(9, 1)));
    bell.enableSends();
    assertEquals(Doorbell.SendResult.SUPPRESSED, bell.sendHint(Notification.of(9)));
    assertEquals(Doorbell.SendResult.SENT, bell.sendHint(Notification.forPane(9, 2, 7, 1, 64)));
    Posix.Received r = Posix.recvmsg(hostFd, 120);
    assertEquals(112, r.n());
    assertEquals(1, Records.id(r.data()), "ids start at 1 per direction");
    Notification back = Notification.decode(r.data());
    assertArrayEquals(new int[] {1, 64}, back.regions());
    assertEquals(7, back.epoch());
    int sent = 0;
    int dropped = 0;
    for (int i = 0; i < 5000 && dropped == 0; i++) {
      Doorbell.SendResult s = bell.sendHint(Notification.of(9, 1));
      if (s == Doorbell.SendResult.SENT) sent++;
      else if (s == Doorbell.SendResult.DROPPED) dropped++;
      else fail("unexpected " + s);
    }
    assertEquals(1, dropped, "a full queue drops the hint rather than blocking");
    assertEquals(1, bell.stats().hintsDropped.sum());
    assertTrue(sent > 10);
  }

  @Test
  void aPeerGoneOnSendIsTeardown() throws Exception {
    open(Platform.OS == Platform.LINUX ? Posix.SOCK_SEQPACKET : Posix.SOCK_DGRAM);
    bell.enableSends();
    Posix.close(hostFd);
    hostFd = -1;
    Doorbell.SendResult r = bell.sendHint(Notification.of(9, 1));
    List<Outcome> l = log.awaitTerminal();
    assertInstanceOf(Outcome.Teardown.class, l.get(l.size() - 1));
    assertEquals(Doorbell.SendResult.CLOSED, r == Doorbell.SendResult.CLOSED ? r : bell.sendHint(Notification.of(9, 1)));
  }

  @Test
  void adoptionRefusesTheWrongTypeAndABadDescriptorWithAHint() throws Exception {
    assumeTrue(Platform.isUnix());
    int[] sv = Posix.socketpair(Posix.SOCK_STREAM);
    LaunchFailure wrong = assertThrows(LaunchFailure.class, () -> Doorbell.adoptUnix(sv[1], true));
    assertTrue(wrong.getMessage().contains("type") || wrong.getMessage().contains("doorbell"), wrong.getMessage());
    Posix.close(sv[0]);
    Posix.close(sv[1]);
    LaunchFailure bad = assertThrows(LaunchFailure.class, () -> Doorbell.adoptUnix(sv[1], true));
    assertTrue(bad.getMessage().contains("exec"), bad.getMessage());
    LaunchFailure missing = assertThrows(LaunchFailure.class, () -> Doorbell.adoptFromEnvironment(java.util.Map.of()));
    assertTrue(missing.getMessage().contains("VIZI_PLUGIN_DOORBELL_FD"));
  }
}
