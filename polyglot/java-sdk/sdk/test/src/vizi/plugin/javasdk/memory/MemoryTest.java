package vizi.plugin.javasdk.memory;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.doorbell.Descriptor;
import vizi.plugin.javasdk.doorbell.MemoryGrant;
import vizi.plugin.javasdk.doorbell.Notification;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.javasdk.nativeio.Atomics;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.javasdk.testkit.HostSegment;
import vizi.plugin.v1.ViziPlugin.GrantFailureClass;
import vizi.plugin.v1.ViziPlugin.GrantStateKind;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.Region;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;
import vizi.plugin.v1.ViziPlugin.ReleaseLoan;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/** The memory lifecycle over real mappings (shared_memory_protocol.md §10's list, and §9a). */
class MemoryTest {

  static final RegionProducer HOST = RegionProducer.REGION_PRODUCER_HOST;
  static final RegionProducer PLUGIN = RegionProducer.REGION_PRODUCER_PLUGIN;

  record Violation(ViolationSite where, String detail) {}

  static final class Sink implements SegmentStore.Sink {
    final List<PluginMsg> pushes = new ArrayList<>();
    final List<Violation> violations = new ArrayList<>();
    final List<String> diags = new ArrayList<>();
    final List<String> traces = new ArrayList<>();

    @Override
    public synchronized void push(PluginMsg msg) {
      pushes.add(msg);
    }

    @Override
    public synchronized void violation(ViolationSite where, String detail, byte[] record) {
      violations.add(new Violation(where, detail));
    }

    @Override
    public synchronized void diagnostic(String text) {
      diags.add(text);
    }

    @Override
    public synchronized void trace(String text) {
      traces.add(text);
    }

    synchronized PluginMsg last() {
      return pushes.get(pushes.size() - 1);
    }

    synchronized List<PluginMsg> take() {
      List<PluginMsg> l = List.copyOf(pushes);
      pushes.clear();
      return l;
    }
  }

  Sink sink;
  SegmentStore store;
  final List<HostSegment> hosts = new ArrayList<>();

  @BeforeEach
  void setUp() {
    assumeTrue(Platform.isUnix(), "unix mappings");
    sink = new Sink();
    store = new SegmentStore(sink, null);
  }

  @AfterEach
  void tearDown() {
    if (store != null) store.closeAll();
    hosts.forEach(HostSegment::close);
  }

  HostSegment host(long id, long size) throws Exception {
    HostSegment h = HostSegment.create(id, size);
    hosts.add(h);
    return h;
  }

  /** Delivers an M for {@code h} with a fresh duplicate of its descriptor. */
  void grant(HostSegment h, int perms) throws Exception {
    grant(h, perms, 1);
  }

  void grant(HostSegment h, int perms, long recordId) throws Exception {
    MemoryGrant g = MemoryGrant.unix(h.id, h.size, perms);
    int dup = Posix.dup(h.fd);
    store.onGrant(new Outcome.GrantBody(g, recordId, Optional.of(Descriptor.adopt(dup))), g.encode(recordId));
  }

  static InstallSegment arenaInstall(HostSegment h, String purpose, String subject) {
    return h.install(purpose, subject, List.of(HostSegment.loaned(0, 64, h.size - 64, PLUGIN)));
  }

  static void assertInstalledAck(PluginMsg m, HostSegment h) {
    assertTrue(m.hasGrantAck(), m.toString());
    assertEquals(h.id, m.getGrantAck().getSegmentId());
    assertTrue(m.getGrantAck().hasInstalled(), m.toString());
    assertArrayEquals(h.proof, m.getGrantAck().getInstalled().getIdentityProof().toByteArray(), "the proof is read back from the mapping");
  }

  static GrantFailureClass failedClass(PluginMsg m) {
    assertTrue(m.hasGrantAck() && m.getGrantAck().hasFailed(), m.toString());
    return m.getGrantAck().getFailed().getFailureClass();
  }

  // ---- the grant state machine (§8) ----

  @Test
  void eitherOrderInstallsAndTheSecondHalfAcks() throws Exception {
    HostSegment a = host(0x100000001L, 4096);
    grant(a, HostSegment.READ_WRITE);
    assertTrue(sink.pushes.isEmpty(), "the first half answers nothing");
    assertEquals(GrantStateKind.GRANT_STATE_PENDING, store.query(a.id).getState());
    assertTrue(store.query(a.id).getHaveCapability());
    assertFalse(store.query(a.id).getHaveInstall());
    store.onInstall(arenaInstall(a, "P", "panel"));
    assertInstalledAck(sink.last(), a);

    HostSegment b = host(0x100000002L, 4096);
    store.onInstall(arenaInstall(b, "P", "other"));
    assertEquals(GrantStateKind.GRANT_STATE_PENDING, store.query(b.id).getState());
    assertTrue(store.query(b.id).getHaveInstall());
    grant(b, HostSegment.READ_WRITE);
    assertInstalledAck(sink.last(), b);
    assertEquals(GrantStateKind.GRANT_STATE_INSTALLED, store.query(b.id).getState());
    assertArrayEquals(b.proof, store.query(b.id).getIdentityProof().toByteArray());
    assertEquals(GrantStateKind.GRANT_STATE_NOT_PRESENT, store.query(99).getState());
  }

  @Test
  void aZeroFilledObjectFailsItsPreambleAtOnceAndReleasesTheMapping() throws Exception {
    assumeTrue(Platform.OS == Platform.LINUX, "counts descriptors in /proc");
    int before = openFds();
    int fd = Posix.createSharedObject("zero", 4096);
    MemoryGrant g = MemoryGrant.unix(7, 4096, HostSegment.READ_WRITE);
    store.onGrant(new Outcome.GrantBody(g, 1, Optional.of(Descriptor.adopt(Posix.dup(fd)))), g.encode(1));
    PluginMsg ack = sink.last();
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(ack));
    assertTrue(ack.getGrantAck().getFailed().getDetail().contains("zero-filled"), ack.toString());
    Posix.close(fd);
    assertEquals(before, openFds(), "a failed install closes what it acquired");
  }

  @Test
  void aWrongObjectSizeFails() throws Exception {
    HostSegment h = host(8, 4096);
    MemoryGrant g = MemoryGrant.unix(h.id, 8192, HostSegment.READ_WRITE);
    store.onGrant(new Outcome.GrantBody(g, 1, Optional.of(Descriptor.adopt(Posix.dup(h.fd)))), g.encode(1));
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(sink.last()));
  }

  @Test
  void anInstallThatFailsBeforeAnyMReportsNowAndALaterMReplays() throws Exception {
    HostSegment h = host(9, 4096);
    store.onInstall(h.install("Q", "", List.of()));
    assertEquals(GrantFailureClass.GRANT_FAILURE_UNSUPPORTED, failedClass(sink.last()));
    grant(h, HostSegment.READ_WRITE);
    assertEquals(GrantFailureClass.GRANT_FAILURE_UNSUPPORTED, failedClass(sink.last()), "the failure is replayed");
    assertEquals(GrantStateKind.GRANT_STATE_FAILED, store.query(h.id).getState());
    assertEquals(GrantFailureClass.GRANT_FAILURE_UNSUPPORTED, store.query(h.id).getFailureClass());
  }

  @Test
  void duplicatesReplayAndDifferentHalvesConflictWithoutReplacing() throws Exception {
    HostSegment h = host(10, 4096);
    grant(h, HostSegment.READ_WRITE);
    grant(h, HostSegment.READ_WRITE);
    assertTrue(sink.pushes.isEmpty(), "a duplicate M while pending closes and stays pending");
    store.onInstall(arenaInstall(h, "P", "p"));
    assertInstalledAck(sink.last(), h);
    grant(h, HostSegment.READ_WRITE);
    assertInstalledAck(sink.last(), h);
    store.onInstall(arenaInstall(h, "P", "p"));
    assertInstalledAck(sink.last(), h);
    grant(h, HostSegment.READ_WRITE, 77); // same grant, a fresh doorbell id: 112 bytes differ
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(sink.last()));
    assertTrue(sink.last().getGrantAck().getFailed().getDetail().contains("conflicting"));
    assertEquals(GrantStateKind.GRANT_STATE_INSTALLED, store.query(h.id).getState(), "not allowed to replace the recorded state");
  }

  @Test
  void aTransportRejectedGrantFailsThroughTheStore() throws Exception {
    MemoryGrant g = MemoryGrant.unix(12, 16, 1);
    store.onGrantRejected(12, g.encode(1), "size 16 is smaller than the 32-byte preamble");
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(sink.last()));
    AtomicReference<SegmentStore.Settlement> s = new AtomicReference<>();
    store.whenSettled(12, s::set);
    assertInstanceOf(SegmentStore.Settlement.Failed.class, s.get());
  }

  // ---- retirement (§9) ----

  @Test
  void retiringAPendingSegmentReleasesItsHalfAndLaterHalvesGetReleased() throws Exception {
    HostSegment h = host(20, 4096);
    grant(h, HostSegment.READ_WRITE);
    store.onRetire(h.id);
    assertTrue(sink.last().hasSegmentReleased());
    store.onInstall(arenaInstall(h, "P", "p"));
    assertTrue(sink.last().hasSegmentReleased(), "a half after release is answered SegmentReleased");
    grant(h, HostSegment.READ_WRITE);
    assertTrue(sink.last().hasSegmentReleased());
    assertEquals(GrantStateKind.GRANT_STATE_RELEASED, store.query(h.id).getState());
    store.onRetire(h.id);
    assertTrue(sink.last().hasSegmentReleased(), "a duplicate retire replays");
    store.onRetire(4242);
    assertTrue(sink.last().hasSegmentReleased(), "an unknown id is released at once");
  }

  @Test
  void retirementIsEnforcedAtTheOperationAndWaitsForItsOps() throws Exception {
    HostSegment h = host(21, 4096);
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(h, "P", "p"));
    Segment s = store.live("P", "p");
    LoanArena.Loan loan = store.lend(s, new byte[100], 1);
    SegmentStore.Op op = store.begin(s, false);
    store.onRetire(h.id);
    assertFalse(sink.last().hasSegmentReleased(), "an operation in flight holds the release");
    assertThrows(SegmentStore.UnavailableException.class, () -> store.lend(s, new byte[1], 1),
        "a handle fetched before RetireSegment refuses to start");
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(h.id).setRegion(0).setOffset(loan.offset()).setLen(100).build());
    assertTrue(sink.violations.isEmpty(), "a ReleaseLoan is applied while retiring");
    op.close();
    assertTrue(sink.last().hasSegmentReleased(), "released after the last operation");
    assertThrows(SegmentStore.UnavailableException.class, () -> store.begin(s, false));
  }

  /**
   * An overlay arena retired while a push is parked mid-write.
   * The reservation and the copy are one operation, so the release (the unmap) waits for the copy,
   * the bytes land whole, and a normal retirement reports nothing.
   */
  @Test
  void anOverlayArenaRetiredWhileALendIsParkedMidWriteReleasesOnlyAfterTheWrite() throws Exception {
    HostSegment h = host(60, 1 << 16);
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(h, "O", "marks"));
    Segment s = store.live("O", "marks");
    sink.take();
    byte[] data = new byte[8192];
    for (int i = 0; i < data.length; i++) data[i] = (byte) (i * 7);
    var parked = new java.util.concurrent.CountDownLatch(1);
    var go = new java.util.concurrent.CountDownLatch(1);
    var lent = new java.util.concurrent.atomic.AtomicReference<Object>();
    SegmentStore.afterReserveForTest = () -> {
      parked.countDown();
      try {
        go.await();
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
      }
    };
    Thread writer = Thread.ofPlatform().start(() -> {
      try {
        lent.set(store.lend(s, data, 1));
      } catch (Exception e) {
        lent.set(e);
      }
    });
    try {
      assertTrue(parked.await(5, java.util.concurrent.TimeUnit.SECONDS), "the lend reached its write");
      store.onRetire(h.id);
      assertEquals(Segment.State.RETIRING, s.state(), "retiring, not released: the write is in flight");
      assertTrue(sink.take().stream().noneMatch(PluginMsg::hasSegmentReleased), "no release while the write is parked");
    } finally {
      SegmentStore.afterReserveForTest = null;
      go.countDown();
    }
    writer.join(5000);
    assertInstanceOf(LoanArena.Loan.class, lent.get(), "the parked write completed: " + lent.get());
    LoanArena.Loan loan = (LoanArena.Loan) lent.get();
    assertArrayEquals(data, h.view.asSlice(loan.offset(), loan.len()).toArray(java.lang.foreign.ValueLayout.JAVA_BYTE),
        "the bytes landed whole, before the unmap");
    assertTrue(sink.last().hasSegmentReleased(), "released after the write");
    assertEquals(Segment.State.RELEASED, s.state());
    assertTrue(sink.violations.isEmpty() && sink.diags.isEmpty(), "a normal retirement reports nothing: "
        + sink.violations + " " + sink.diags);
  }

  /** A lend that would START on a retiring arena goes to its successor, or is refused quietly and counted. */
  @Test
  void aLendStartingOnARetiringArenaUsesItsSuccessorElseIsDroppedQuietly() throws Exception {
    HostSegment old = host(61, 4096);
    grant(old, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(old, "O", "marks"));
    Segment s = store.live("O", "marks");
    SegmentStore.Op inFlight = store.begin(s, false); // holds the old arena retiring
    store.onRetire(old.id);
    // no successor yet: refused as a retirement, counted, nothing reported
    assertThrows(SegmentStore.RetiredException.class, () -> store.lend(s, new byte[16], 1));
    assertEquals(1, store.retiredDrops());
    HostSegment next = host(62, 4096);
    grant(next, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(next, "O", "marks"));
    // a successor: the lend goes there, and the loan names it
    LoanArena.Loan loan = store.lend(s, new byte[16], 1);
    assertEquals(next.id, loan.segmentId(), "the loan names the successor");
    assertEquals(1, store.retiredDrops());
    inFlight.close();
    assertEquals(Segment.State.RELEASED, s.state());
    assertTrue(sink.violations.isEmpty() && sink.diags.isEmpty(), sink.violations + " " + sink.diags);
  }

  /**
   * §9.1: a loan's claim lasts until the message naming it is sent; a deposit whose
   * descriptor has arrived counts as begun; a late ReleaseLoan for a released segment is quiet.
   */
  @Test
  void claimsHoldARetiringSegmentUntilTheyEndAndItIsReleasedOnce() throws Exception {
    // a loaned write: claimed from the reservation until endClaim (the message was sent)
    HostSegment o = host(70, 4096);
    grant(o, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(o, "O", "marks"));
    Segment os = store.live("O", "marks");
    LoanArena.Loan loan = store.lendClaimed(os, java.lang.foreign.MemorySegment.ofArray(new byte[32]), 1);
    sink.take();
    store.onRetire(o.id);
    assertEquals(Segment.State.RETIRING, os.state());
    assertTrue(sink.take().stream().noneMatch(PluginMsg::hasSegmentReleased), "the claim holds the release past the copy");
    store.endClaim(loan);
    List<PluginMsg> out = sink.take();
    assertEquals(1, out.stream().filter(PluginMsg::hasSegmentReleased).count(), "released when the last claim ends, once");
    store.endClaim(loan); // a stray second end changes nothing
    assertTrue(sink.take().stream().noneMatch(PluginMsg::hasSegmentReleased), "never a second SegmentReleased");

    // a late ReleaseLoan for the released segment: quiet; for a segment never installed: still a violation
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(o.id).setRegion(0).setOffset(loan.offset()).setLen(32).build());
    assertTrue(sink.violations.isEmpty(), "late, not unknown: " + sink.violations);
    assertEquals(1, store.lateReleaseLoans());
    assertTrue(sink.diags.isEmpty(), "late, so quiet: " + sink.diags);
    assertEquals(1, sink.traces.size(), "logged at debug (trace) level: " + sink.traces);
    // §9.3: per loan. The same loan crossing again, or a loan never out at the
    // release, is unknown_release, never counted silently
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(o.id).setRegion(0).setOffset(loan.offset()).setLen(32).build());
    assertEquals(1, sink.violations.size(), "a second crossing for one loan: " + sink.violations);
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(o.id).setRegion(0).setOffset(loan.offset() + 64).setLen(8).build());
    assertEquals(2, sink.violations.size(), "a loan never out: " + sink.violations);
    assertEquals(1, store.lateReleaseLoans(), "only the first was counted");
    assertTrue(sink.diags.isEmpty(), sink.diags.toString());
    sink.violations.clear();
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(4343).setRegion(0).setOffset(0).setLen(8).build());
    assertEquals(1, sink.violations.size(), "an id never installed is still reported");
    sink.violations.clear();

    // a deposit whose descriptor has arrived: the retirement waits until its naming is disposed of
    HostSegment v = host(71, 4096);
    grant(v, HostSegment.READ_WRITE);
    store.onInstall(v.install("V", "", List.of(HostSegment.loaned(0, 64, 4032, HOST))));
    for (int i = 0; i < 16; i++) v.view.set(java.lang.foreign.ValueLayout.JAVA_BYTE, 64 + i, (byte) (i + 1));
    SegmentRef ref = SegmentRef.newBuilder().setSegmentId(v.id).setOffset(64).setLen(16).build();
    sink.take();
    store.claimDeposit(ref); // the reply carrying it arrived
    store.onRetire(v.id);
    assertTrue(sink.take().stream().noneMatch(PluginMsg::hasSegmentReleased), "an arrived deposit holds the release");
    byte[] read = store.readDeposit(ref); // finishing: permitted while retiring
    assertEquals(16, read.length);
    assertEquals(16, read[15]);
    out = sink.take();
    int dep = -1, rel = -1;
    for (int i = 0; i < out.size(); i++) {
      if (out.get(i).hasReleaseDeposit()) dep = i;
      if (out.get(i).hasSegmentReleased()) rel = i;
    }
    assertTrue(dep >= 0 && rel > dep, "ReleaseDeposit, then SegmentReleased: " + out);
    assertTrue(sink.violations.isEmpty() && sink.diags.isEmpty(), sink.violations + " " + sink.diags);
  }

  @Test
  void supersedesTakesEffectOnlyWhenTheReplacementInstalls() throws Exception {
    HostSegment old = host(30, 4096);
    grant(old, HostSegment.READ_WRITE);
    store.onInstall(old.install("G", "", List.of(HostSegment.loaned(0, 64, 4032, PLUGIN))));
    sink.take();
    HostSegment next = host(31, 4096);
    store.onInstall(next.install("G", "", old.id, List.of(HostSegment.loaned(0, 64, 4032, PLUGIN))));
    assertEquals(old.id, store.live("G", "").id(), "the incumbent stays live while the replacement is pending");
    grant(next, HostSegment.READ_WRITE);
    List<PluginMsg> out = sink.take();
    assertInstalledAck(out.get(0), next);
    assertTrue(out.get(1).hasSegmentReleased());
    assertEquals(old.id, out.get(1).getSegmentReleased().getSegmentId());
    assertEquals(next.id, store.live("G", "").id());

    HostSegment bad = host(32, 4096);
    store.onInstall(bad.install("G", "", next.id, List.of(HostSegment.loaned(0, 64, 4032, PLUGIN))));
    MemoryGrant g = MemoryGrant.unix(bad.id, 8192, HostSegment.READ_WRITE); // wrong size: fails
    store.onGrant(new Outcome.GrantBody(g, 1, Optional.of(Descriptor.adopt(Posix.dup(bad.fd)))), g.encode(1));
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(sink.last()));
    assertEquals(next.id, store.live("G", "").id(), "a failed replacement leaves the old untouched");
  }

  @Test
  void twoLiveSegmentsForOnePurposeAreRefusedByName() throws Exception {
    HostSegment a = host(40, 4096);
    HostSegment b = host(41, 4096);
    for (HostSegment h : List.of(a, b)) {
      grant(h, HostSegment.READ_WRITE);
      store.onInstall(h.install("G", "", List.of(HostSegment.loaned(0, 64, 4032, PLUGIN))));
    }
    SegmentStore.UnavailableException e = assertThrows(SegmentStore.UnavailableException.class, () -> store.live("G", ""));
    assertTrue(e.getMessage().contains("40") && e.getMessage().contains("41"), e.getMessage());
  }

  @Test
  void releaseClosesTheDescriptorNotOnlyTheMapping() throws Exception {
    assumeTrue(Platform.OS == Platform.LINUX, "counts descriptors in /proc");
    HostSegment h = host(50, 4096);
    int before = openFds();
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(h, "P", "p"));
    assertEquals(before + 1, openFds());
    store.onRetire(h.id);
    assertTrue(sink.last().hasSegmentReleased());
    assertEquals(before, openFds(), "SegmentReleased only after the descriptor is closed");
  }

  // ---- the validation matrix ----

  InstallSegment table(HostSegment h, String purpose, String subject, Region... regions) {
    return h.install(purpose, subject, List.of(regions));
  }

  GrantFailureClass installWith(int perms, java.util.function.Function<HostSegment, InstallSegment> install) throws Exception {
    HostSegment h = host(1000 + hosts.size(), 8192);
    grant(h, perms);
    store.onInstall(install.apply(h));
    PluginMsg m = sink.last();
    return m.getGrantAck().hasInstalled() ? GrantFailureClass.GRANT_FAILURE_UNSPECIFIED : failedClass(m);
  }

  @Test
  void theReceiverMatrix() throws Exception {
    GrantFailureClass ok = GrantFailureClass.GRANT_FAILURE_UNSPECIFIED;
    GrantFailureClass malformed = GrantFailureClass.GRANT_FAILURE_MALFORMED;
    GrantFailureClass unsupported = GrantFailureClass.GRANT_FAILURE_UNSUPPORTED;
    int rw = HostSegment.READ_WRITE;
    int ro = HostSegment.READ_ONLY;
    assertEquals(ok, installWith(ro, h -> table(h, "V", "", HostSegment.loaned(0, 64, 4096, HOST))), "a consumer maps read-only");
    assertEquals(malformed, installWith(ro, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 3, 64, HOST, false))),
        "a PULSED region under a read-only grant fails whichever end produces it");
    assertEquals(malformed, installWith(ro, h -> table(h, "P", "p", HostSegment.loaned(0, 64, 4096, PLUGIN))),
        "a plugin-produced region needs PLUGIN_WRITE");
    assertEquals(ok, installWith(rw, h -> table(h, "V", "", HostSegment.loaned(0, 64, 4096, HOST).toBuilder().setSlotBytes(0).build())),
        "LOANED slot_bytes 0 installs");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.loaned(0, 64, 4096, HOST).toBuilder().setBuffers(3).build())),
        "buffers outside PULSED");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 0, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST))),
        "extent 0");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 16, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST))),
        "offset under 32");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 1000, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST),
        HostSegment.of(1, 1000, 100, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST))), "overlap");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 9000, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST))),
        "out of bounds");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.loaned(0, 64, 1024, PLUGIN), HostSegment.loaned(1, 2048, 1024, PLUGIN))),
        "two plugin arenas");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 3, 64, PLUGIN, false), HostSegment.pulsed(1, 1024, 3, 64, PLUGIN, false))),
        "two plugin lanes");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, RegionProducer.REGION_PRODUCER_UNSPECIFIED))),
        "producer unspecified is malformed, never a default");
    assertEquals(unsupported, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST).toBuilder().setDisciplineValue(9).build())),
        "an unknown discipline");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 64, 64, RegionDiscipline.REGION_DISCIPLINE_UNSPECIFIED, HOST))),
        "a zero discipline");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 60, 3, 64, PLUGIN, false))),
        "a misaligned PULSED region fails the grant");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 3, 64, PLUGIN, false).toBuilder().setExtent(100).build())),
        "an inconsistent PULSED extent");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 2, 64, PLUGIN, false))),
        "two buffers");
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 3, 64, PLUGIN, false).toBuilder().setEpochOffset(8).build())),
        "a PULSED epoch_offset other than slot_bytes - 8");
    assertEquals(ok, installWith(rw, h -> table(h, "O", "o", HostSegment.pulsed(0, 64, 3, 64, PLUGIN, true), HostSegment.loaned(1, 1024, 4096, PLUGIN))));
    assertEquals(malformed, installWith(rw, h -> table(h, "O", "", HostSegment.loaned(0, 64, 64, PLUGIN))), "O needs a subject");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "x", HostSegment.loaned(0, 64, 64, HOST))), "V needs an empty subject");
    assertEquals(malformed, installWith(rw, h -> table(h, "", "")), "an empty purpose");
    assertEquals(malformed, installWith(rw, h -> h.install("V", "", h.id, List.of())), "supersedes names itself");
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(1, 64, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST))),
        "a gap in the indices");
    assertEquals(malformed, installWith(rw, h -> table(h, "S", "", HostSegment.of(0, 96, 256, RegionDiscipline.REGION_DISCIPLINE_STAGED, HOST))),
        "a stage slot offset not a multiple of 64");
    assertEquals(ok, installWith(rw, h -> table(h, "S", "", HostSegment.of(0, 128, 256, RegionDiscipline.REGION_DISCIPLINE_STAGED, HOST))));
    assertEquals(malformed, installWith(rw, h -> table(h, "V", "", HostSegment.of(0, 128, 256, RegionDiscipline.REGION_DISCIPLINE_STAGED, HOST))),
        "a STAGED region outside an S segment");
  }

  @Test
  void aSubjectCheckWhenTheManifestIsKnown() throws Exception {
    store = new SegmentStore(sink, (purpose, subject) -> subject.equals("known"));
    HostSegment a = host(60, 4096);
    grant(a, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(a, "O", "stranger"));
    assertEquals(GrantFailureClass.GRANT_FAILURE_MALFORMED, failedClass(sink.last()));
    HostSegment b = host(61, 4096);
    grant(b, HostSegment.READ_WRITE);
    store.onInstall(arenaInstall(b, "O", "known"));
    assertInstalledAck(sink.last(), b);
  }

  // ---- loans ----

  @Test
  void theArenaLendsDistinctBytesAndChecksEveryRelease() throws Exception {
    HostSegment h = host(70, 1024);
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(h.install("P", "p", List.of(HostSegment.of(0, 64, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST),
        HostSegment.loaned(1, 128, 512, PLUGIN))));
    Segment s = store.live("P", "p");
    LoanArena.Loan a = store.lend(s, "hello".getBytes(), 1);
    LoanArena.Loan b = store.lend(s, new byte[100], 1);
    assertEquals(128, a.offset(), "offsets are mapping-relative");
    assertEquals(1, a.region());
    assertEquals(192, b.offset(), "padded to 64, never overlapping");
    assertEquals('h', h.view.get(ValueLayout.JAVA_BYTE, 128), "the host sees the bytes");
    LoanArena.ArenaFullException full = assertThrows(LoanArena.ArenaFullException.class, () -> store.lend(s, new byte[400], 1));
    assertEquals(400, full.wanted);
    assertEquals(512 - 64 - 128, full.largestFree);

    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(h.id).setRegion(1).setOffset(128).setLen(5).build());
    assertTrue(sink.violations.isEmpty());
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(h.id).setRegion(0).setOffset(192).setLen(100).build());
    assertEquals(ViolationSite.VIOLATION_SITE_WRONG_PRODUCER_RELEASE, sink.violations.get(0).where());
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(h.id).setRegion(1).setOffset(40).setLen(100).build());
    assertEquals(ViolationSite.VIOLATION_SITE_RELEASE_REGION_MISMATCH, sink.violations.get(1).where());
    store.onReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(h.id).setRegion(1).setOffset(192).setLen(128).build());
    assertEquals(ViolationSite.VIOLATION_SITE_UNKNOWN_RELEASE, sink.violations.get(2).where(), "a padded length names nothing");
    assertThrows(SegmentStore.UnavailableException.class, () -> store.lend(s, new byte[1], 1), "the arena is isolated");
  }

  // ---- deposits ----

  @Test
  void aDepositIsCopiedOutAndReleasedWithTheRegionAndEchoedOffset() throws Exception {
    HostSegment h = host(80, 8192);
    grant(h, HostSegment.READ_ONLY);
    store.onInstall(h.install("V", "", List.of(HostSegment.of(0, 64, 64, RegionDiscipline.REGION_DISCIPLINE_CONSTANT, HOST),
        HostSegment.loaned(1, 4096, 4096, HOST))));
    MemorySegment.copy(new byte[] {1, 2, 3, 4}, 0, h.view, ValueLayout.JAVA_BYTE, 4160, 4);
    SegmentRef ref = SegmentRef.newBuilder().setSegmentId(h.id).setOffset(4160).setLen(300).build();
    byte[] got = store.readDeposit(ref);
    assertEquals(300, got.length);
    assertEquals(3, got[2]);
    PluginMsg rel = sink.last();
    assertTrue(rel.hasReleaseDeposit());
    assertEquals(1, rel.getReleaseDeposit().getRegion(), "the region comes from the table, never defaulted to 0");
    assertEquals(4160, rel.getReleaseDeposit().getOffset());
    assertEquals(300, rel.getReleaseDeposit().getLen());

    SegmentRef twice = ref.toBuilder().setOffset(4800).setLen(10).setUses(2).build();
    store.readDeposit(twice);
    assertFalse(sink.last().hasReleaseDeposit() && sink.last().getReleaseDeposit().getOffset() == 4800, "uses 2: not yet");
    store.disposeDeposit(twice.toBuilder().setUses(3).build());
    assertEquals(ViolationSite.VIOLATION_SITE_USES_DISAGREEMENT, sink.violations.get(0).where(), "the first declaration stands");
    assertEquals(4800, sink.last().getReleaseDeposit().getOffset());
    assertThrows(SegmentStore.DepositException.class,
        () -> store.readDeposit(ref.toBuilder().setOffset(64).setLen(8).build()), "outside every host LOANED region");
  }

  // ---- Pulsed (§6) ----

  static RegionTable.Spec lane(int buffers, long slot, boolean header) {
    return new RegionTable.Spec(0, 64, 16L * buffers + buffers * slot, RegionDiscipline.REGION_DISCIPLINE_PULSED,
        PLUGIN, buffers, slot, "", header ? java.util.OptionalLong.of(slot - 8) : java.util.OptionalLong.empty());
  }

  @Test
  void aPublishClaimsTheOldestReadyNeverTheNewest() throws Exception {
    HostSegment h = host(90, 4096);
    List<Violation> v = new ArrayList<>();
    PulsedLane w = PulsedLane.over(h.view, h.id, lane(3, 64, true), (s, d) -> v.add(new Violation(s, d)));
    for (int i = 0; i < 3; i++) assertEquals(PulsedLane.Published.PUBLISHED, w.publish(new byte[] {(byte) i}, 2, 5));
    long[] words = new long[3];
    for (int i = 0; i < 3; i++) words[i] = Atomics.loadAcquire(h.view, 64 + 16L * i);
    assertEquals(PulsedLane.Published.PUBLISHED, w.publish(new byte[] {9}, 2, 5));
    int changed = -1;
    for (int i = 0; i < 3; i++) if (Atomics.loadAcquire(h.view, 64 + 16L * i) != words[i]) changed = i;
    assertEquals(4, Atomics.loadAcquire(h.view, 64 + 16L * changed));
    assertEquals(1, words[changed], "revision 1, the oldest, was overwritten");

    PulsedLane r = PulsedLane.over(h.view, h.id, lane(3, 64, true), (s, d) -> v.add(new Violation(s, d)));
    PulsedLane.Value got = r.read();
    assertNotNull(got);
    assertEquals(4, got.revision(), "the reader consumes the newest");
    assertEquals(9, got.payload()[0]);
    assertEquals(2, got.viewId().getAsInt());
    assertEquals(5, got.epoch().getAsLong());
    assertNull(r.read(), "revisions at or below last_seen are not re-read");
    assertTrue(v.isEmpty());
  }

  @Test
  void aHeldBufferBlocksOnlyItselfAndEightFailuresCoalesce() throws Exception {
    HostSegment h = host(91, 4096);
    PulsedLane w = PulsedLane.over(h.view, h.id, lane(3, 64, false), (s, d) -> fail(d));
    // every buffer claimed by a reader: nothing can be claimed
    for (int i = 0; i < 3; i++) Atomics.storeRelease(h.view, 64 + 16L * i, (i + 1) | PulsedLane.READ);
    assertEquals(PulsedLane.Published.COALESCED, w.publish(new byte[] {1}, 0, 0));
    Atomics.storeRelease(h.view, 64, 0);
    assertEquals(PulsedLane.Published.PUBLISHED, w.publish(new byte[] {1}, 0, 0), "the free buffer is used");
    assertEquals(2 | PulsedLane.READ, Atomics.loadAcquire(h.view, 64 + 16), "the held ones are untouched");
  }

  @Test
  void impossibleWordsAndBadLengthsQuarantineWithoutClearing() throws Exception {
    HostSegment h = host(92, 4096);
    List<Violation> v = new ArrayList<>();
    PulsedLane r = PulsedLane.over(h.view, h.id, lane(3, 64, false), (s, d) -> v.add(new Violation(s, d)));
    Atomics.storeRelease(h.view, 64, PulsedLane.WRITE | PulsedLane.READ | 3);
    assertNull(r.read());
    assertEquals(ViolationSite.VIOLATION_SITE_IMPOSSIBLE_STATE_WORD, v.get(0).where());
    assertEquals(PulsedLane.WRITE | PulsedLane.READ | 3, Atomics.loadAcquire(h.view, 64), "never cleared");
    assertTrue(r.quarantined());
    assertEquals(PulsedLane.Published.REFUSED, r.publish(new byte[] {1}, 0, 0));

    for (long len : new long[] {0, 65, 1L << 40}) {
      HostSegment g = host(93 + hosts.size(), 4096);
      List<Violation> v2 = new ArrayList<>();
      PulsedLane rr = PulsedLane.over(g.view, g.id, lane(3, 64, false), (s, d) -> v2.add(new Violation(s, d)));
      Atomics.storeRelease(g.view, 64, 1);
      g.view.set(ValueLayout.JAVA_LONG_UNALIGNED, 72, len);
      assertNull(rr.read());
      assertEquals(ViolationSite.VIOLATION_SITE_INVALID_PUBLISHED_LENGTH, v2.get(0).where(), "length " + len);
      assertEquals(0, Atomics.loadAcquire(g.view, 64), "the claim was released");
    }
  }

  @Test
  void aBadPaneHeaderCostsThePairNotThePayload() throws Exception {
    HostSegment h = host(96, 4096);
    PulsedLane w = PulsedLane.over(h.view, h.id, lane(3, 64, true), (s, d) -> fail(d));
    PulsedLane r = PulsedLane.over(h.view, h.id, lane(3, 64, true), (s, d) -> fail(d));
    assertEquals(PulsedLane.Published.PUBLISHED, w.publish(new byte[] {7}, 0x100, 5)); // high bytes set
    PulsedLane.Value v = r.read();
    assertNotNull(v);
    assertEquals(7, v.payload()[0]);
    assertTrue(v.viewId().isEmpty());
    assertThrows(IllegalArgumentException.class, () -> w.publish(new byte[57], 0, 1), "larger than capacity: refused, not truncated");
  }

  @Test
  void twoMappingsUnderConcurrentPublishAndReadNeverTearOrRegress() throws Exception {
    HostSegment h = host(97, 64 + 16 * 3 + 3 * 1024);
    MemorySegment other = Posix.mmap(h.fd, h.size, true);
    List<Violation> v = new ArrayList<>();
    PulsedLane w = PulsedLane.over(h.view, h.id, lane(3, 1024, true), (s, d) -> v.add(new Violation(s, d)));
    PulsedLane r = PulsedLane.over(other, h.id, lane(3, 1024, true), (s, d) -> v.add(new Violation(s, d)));
    int n = 20000;
    CountDownLatch done = new CountDownLatch(1);
    Thread writer = Thread.ofPlatform().start(() -> {
      byte[] p = new byte[1000];
      for (int i = 1; i <= n; i++) {
        java.util.Arrays.fill(p, (byte) i);
        w.publish(p, 1, i);
      }
      done.countDown();
    });
    long last = 0;
    int reads = 0;
    while (done.getCount() > 0 || reads == 0) {
      PulsedLane.Value val = r.read();
      if (val == null) continue;
      reads++;
      assertTrue(val.revision() > last, "revisions only increase");
      last = val.revision();
      byte first = val.payload()[0];
      for (byte b : val.payload()) assertEquals(first, b, "a consumed value is never torn");
      assertEquals(val.epoch().getAsLong() & 0xff, first & 0xff, "the pane pair travels with its payload");
    }
    writer.join();
    PulsedLane.Value tail = r.read();
    if (tail != null) last = tail.revision();
    assertTrue(v.isEmpty(), v.toString());
    assertTrue(reads > 10, "reads " + reads);
    assertEquals(n, last, "the last publication is always readable");
    Posix.munmap(other);
  }

  // ---- hints ----

  @Test
  void hintsFollowTheDispositionTable() throws Exception {
    HostSegment h = host(110, 16384);
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(h.install("O", "o", List.of(
        HostSegment.pulsed(0, 64, 3, 64, PLUGIN, false),
        HostSegment.loaned(1, 1024, 1024, PLUGIN),
        HostSegment.of(2, 2048, 64, RegionDiscipline.REGION_DISCIPLINE_OWNED, HOST),
        HostSegment.pulsed(3, 4096, 3, 64, HOST, false),
        HostSegment.pulsed(4, 8192, 3, 64, HOST, false))));
    List<String> brought = new ArrayList<>();
    store.setHints((seg, region) -> brought.add(seg + ":" + region));
    Notification hint = Notification.of(h.id, 0, 1, 2, 3);
    store.onHint(hint, hint.encode(1), view -> 0);
    assertEquals(List.of(h.id + ":3"), brought, "only the peer-produced Pulsed region is scheduled");
    assertEquals(ViolationSite.VIOLATION_SITE_OWNED_REGION_HINT, sink.violations.get(0).where());
    brought.clear();
    Notification sweep = Notification.of(h.id, 0, 200);
    store.onHint(sweep, sweep.encode(2), view -> 0);
    assertEquals(List.of(h.id + ":3", h.id + ":4"), brought, "an out-of-range bit sweeps the peer-produced Pulsed regions only");
    brought.clear();
    Notification stale = Notification.forPane(h.id, 1, 4, 3);
    store.onHint(stale, stale.encode(3), view -> 5);
    assertTrue(brought.isEmpty(), "a stale pane epoch is ignored");
    store.onRetire(h.id);
    store.onHint(hint, hint.encode(4), view -> 0);
    assertTrue(brought.isEmpty(), "a retired segment's bits are counted and ignored");
  }

  @Test
  void theRecheckScheduleDeliversWithoutAHint() throws Exception {
    HostSegment h = host(120, 8192);
    grant(h, HostSegment.READ_WRITE);
    store.onInstall(h.install("O", "o", List.of(HostSegment.pulsed(0, 64, 3, 64, HOST, false))));
    Segment s = store.installed(h.id).orElseThrow();
    PulsedLane hostWriter = PulsedLane.over(h.view, h.id, lane(3, 64, false), (x, d) -> fail(d));
    CountDownLatch got = new CountDownLatch(1);
    List<String> diags = new ArrayList<>();
    try (Recheck re = new Recheck(diags::add)) {
      store.setHints(re);
      Recheck.LaneSubscription sub = re.subscribe(store, s, 0, v -> {
        if (v.payload()[0] == 42) got.countDown();
      });
      hostWriter.publish(new byte[] {42}, 0, 0);
      assertTrue(got.await(2, TimeUnit.SECONDS), "the schedule found the value with no D at all");
      sub.close();
    }
  }

  static int openFds() throws Exception {
    try (var s = Files.list(Path.of("/proc/self/fd"))) {
      return (int) s.count();
    }
  }
}
