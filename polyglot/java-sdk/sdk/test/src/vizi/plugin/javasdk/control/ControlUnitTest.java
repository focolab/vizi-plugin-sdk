package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.TimeoutException;
import org.junit.jupiter.api.Test;
import vizi.plugin.v1.ViziPlugin.GetViewState;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.RoiPoseExtension;
import vizi.plugin.v1.ViziPlugin.TimeMapping;
import vizi.plugin.v1.ViziPlugin.ViewState;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/** The pieces of the control layer that need no stream. */
class ControlUnitTest {

  /** The id ledger remembers the last 65,536 ids received; an older id is not checked. */
  @Test
  void theIdWindowSlides() {
    IdLedger ledger = new IdLedger(java.util.Set.of(HostMsg.MsgCase.INVOKE_COMMAND), java.util.Set.of(HostMsg.MsgCase.VIEW_STATE));
    java.util.function.BiFunction<String, Long, HostMsg> query = (id, segment) -> HostMsg.newBuilder().setId(id)
        .setGrantStateQuery(vizi.plugin.v1.ViziPlugin.GrantStateQuery.newBuilder().setSegmentId(segment)).build();
    for (int i = 0; i <= IdLedger.WINDOW; i++) assertEquals(IdLedger.Seen.NEW, ledger.observe(query.apply("r" + i, 1L)));
    assertEquals(IdLedger.WINDOW, ledger.size());
    assertEquals(IdLedger.Seen.NEW, ledger.observe(query.apply("r0", 2L)), "older than the window: not checked");
    assertEquals(IdLedger.Seen.RESENT, ledger.observe(query.apply("r2", 1L)));
    assertEquals(IdLedger.Seen.REUSED, ledger.observe(query.apply("r3", 2L)));
  }

  @Test
  void violationsReportTheFirstAtOnceThenAggregateUnderABudget() throws Exception {
    List<PluginMsg> out = new ArrayList<>();
    try (Violations v = new Violations(m -> {
      synchronized (out) {
        out.add(m);
      }
    })) {
      byte[] rec = new byte[112];
      for (int i = 0; i < 1000; i++) {
        v.report(i % 2 == 0 ? ViolationSite.VIOLATION_SITE_EPOCH_MISMATCH : ViolationSite.VIOLATION_SITE_UNKNOWN_TOOL, "x", rec);
      }
      synchronized (out) {
        assertEquals(2, out.size(), "two alternating sites: two first reports, the rest counted");
        assertEquals(1, out.get(0).getContractViolation().getCount());
        assertEquals(112, out.get(0).getContractViolation().getRecord().size());
      }
      v.flush(true);
      synchronized (out) {
        assertEquals(4, out.size());
        long total = out.stream().mapToLong(m -> m.getContractViolation().getCount()).sum();
        assertEquals(1000, total, "count is the number of records affected, nothing lost");
      }
      for (int i = 0; i < 50; i++) v.reportZeroIdGrant("M 0", rec);
      synchronized (out) {
        assertEquals(54, out.size(), "M with zero id is exempt from aggregation");
      }
    }
  }

  @Test
  void theBudgetBoundsTheRateOfFirstReports() throws Exception {
    List<PluginMsg> out = new ArrayList<>();
    try (Violations v = new Violations(m -> {
      synchronized (out) {
        out.add(m);
      }
    })) {
      for (ViolationSite s : ViolationSite.values()) {
        if (s == ViolationSite.UNRECOGNIZED || s == ViolationSite.VIOLATION_SITE_UNSPECIFIED) continue;
        v.report(s, "x", new byte[0]);
      }
      synchronized (out) {
        assertEquals(Violations.BUDGET, out.size(), "a change of where does not reset the budget");
      }
    }
  }

  /** Only Netty's SO_KEEPALIVE warning is dropped; every other bootstrap warning is kept. */
  @Test
  void onlyTheKeepAliveWarningIsFiltered() {
    var noise = new java.util.logging.LogRecord(java.util.logging.Level.WARNING,
        "Unknown channel option 'SO_KEEPALIVE' for channel '[id: 0x1]' of type 'EpollServerDomainSocketChannel'");
    var other = new java.util.logging.LogRecord(java.util.logging.Level.WARNING, "Failed to set channel option 'SO_RCVBUF'");
    assertFalse(Server.notKeepAliveNoise(noise));
    assertTrue(Server.notKeepAliveNoise(other));
  }

  @Test
  void aTimedOutRequestsLateReplyIsReportedAbandoned() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
      }
    }, t -> false);
    assertThrows(TimeoutException.class, () -> hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofMillis(50)));
    String id;
    synchronized (sent) {
      id = sent.get(0).getId();
    }
    assertEquals("q1", id);
    assertFalse(hc.deliver(HostMsg.newBuilder().setId(id).setViewState(ViewState.getDefaultInstance()).build()),
        "no waiter: the dispatcher releases what the reply names");
    assertEquals(1, hc.unmatched());
  }

  @Test
  void aReplyReachesItsWaiterAndAHostErrorCarriesItsKind() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
        sent.notifyAll();
      }
    }, t -> false);
    CompletableFuture<HostMsg> got = CompletableFuture.supplyAsync(() -> {
      try {
        return hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(5));
      } catch (Exception e) {
        throw new RuntimeException(e);
      }
    });
    String id = awaitSent(sent, 0);
    assertTrue(hc.inFlight(id));
    assertTrue(hc.deliver(HostMsg.newBuilder().setId(id).setViewState(ViewState.newBuilder().setT(7)).build()));
    assertEquals(7, got.get().getViewState().getT());

    CompletableFuture<Throwable> err = CompletableFuture.supplyAsync(() -> {
      try {
        hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(5));
        return null;
      } catch (Exception e) {
        return e;
      }
    });
    String id2 = awaitSent(sent, 1);
    hc.deliver(HostMsg.newBuilder().setId(id2).setHostError(vizi.plugin.v1.ViziPlugin.Error.newBuilder()
        .setKind(vizi.plugin.v1.ViziPlugin.Error.Kind.UNSUPPORTED).setMessage("self-serve")).build());
    Throwable e = err.get();
    assertInstanceOf(HostException.class, e);
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.UNSUPPORTED, ((HostException) e).kind());
  }

  @Test
  void aKnownReplyOfAnotherKindIsRefusedAndItsWaiterNeverReceivesIt() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
        sent.notifyAll();
      }
    }, t -> false);
    CompletableFuture<Throwable> err = CompletableFuture.supplyAsync(() -> {
      try {
        hc.call(PluginMsg.newBuilder().setGetRois(vizi.plugin.v1.ViziPlugin.GetRois.getDefaultInstance()), Duration.ofSeconds(5));
        return null;
      } catch (Exception e) {
        return e;
      }
    });
    String id = awaitSent(sent, 0);
    var wrong = assertThrows(HostClient.WrongReplyKind.class,
        () -> hc.deliver(HostMsg.newBuilder().setId(id).setViewState(ViewState.getDefaultInstance()).build()));
    for (String name : List.of("view_state", "get_rois", "\"" + id + "\"")) {
      assertTrue(wrong.getMessage().contains(name), wrong.getMessage());
    }
    assertTrue(hc.inFlight(id), "the waiter keeps its place until the session ends");
    hc.endSession(wrong.getMessage());
    assertInstanceOf(SessionEndedException.class, err.get());
    assertEquals(0, hc.unmatched());
    // every plugin request names one reply kind
    for (PluginMsg.MsgCase c : PluginMsg.MsgCase.values()) {
      String n = c.name();
      boolean request = n.startsWith("GET_") || n.startsWith("READ_") || n.startsWith("REQUEST_")
          || n.equals("CONTRIBUTE_ROIS") || n.equals("EXTEND_ROI_POSES") || n.equals("COMPILE_LABEL_VOLUME")
          || n.equals("ACTIVATE_TOOL") || n.equals("VIEW_REQUEST");
      assertEquals(request, HostClient.REPLIES.containsKey(c), n);
    }
  }

  /** A later host's reply kind, as this build parses it: an unknown field, the oneof unset. */
  static HostMsg laterKind(String id) {
    return HostMsg.newBuilder().setId(id).setUnknownFields(com.google.protobuf.UnknownFieldSet.newBuilder()
        .addField(9999, com.google.protobuf.UnknownFieldSet.Field.newBuilder()
            .addLengthDelimited(com.google.protobuf.ByteString.copyFromUtf8("later")).build()).build()).build();
  }

  @Test
  void aReplyOfAnUnknownKindIsAProtocolErrorNotADefaultInstance() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
        sent.notifyAll();
      }
    }, t -> false);
    CompletableFuture<Throwable> err = CompletableFuture.supplyAsync(() -> {
      try {
        hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(5));
        return null;
      } catch (Exception e) {
        return e;
      }
    });
    String id = awaitSent(sent, 0);
    HostMsg later = laterKind(id);
    assertEquals(HostMsg.MsgCase.MSG_NOT_SET, HostMsg.parseFrom(later.toByteArray()).getMsgCase());
    assertTrue(hc.deliver(later));
    HostException e = assertInstanceOf(HostException.class, err.get());
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.PROTOCOL, e.kind());
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.PROTOCOL_VALUE, e.kindValue());
    assertTrue(e.getMessage().contains("GET_VIEW_STATE"), e.getMessage());

    // the non-blocking form says the same
    CompletableFuture<HostMsg> f = hc.submit(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(5));
    String id2 = awaitSent(sent, 1);
    assertTrue(hc.deliver(laterKind(id2)));
    var thrown = assertThrows(java.util.concurrent.ExecutionException.class, () -> f.get(5, java.util.concurrent.TimeUnit.SECONDS));
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.PROTOCOL, assertInstanceOf(HostException.class, thrown.getCause()).kind());
  }

  @Test
  void aHostErrorOfAKindThisBuildDoesNotKnowKeepsItsNumber() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
        sent.notifyAll();
      }
    }, t -> false);
    CompletableFuture<Throwable> err = CompletableFuture.supplyAsync(() -> {
      try {
        hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(5));
        return null;
      } catch (Exception e) {
        return e;
      }
    });
    String id = awaitSent(sent, 0);
    hc.deliver(HostMsg.newBuilder().setId(id).setHostError(vizi.plugin.v1.ViziPlugin.Error.newBuilder().setKindValue(42)).build());
    HostException e = assertInstanceOf(HostException.class, err.get());
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.UNRECOGNIZED, e.kind());
    assertEquals(42, e.kindValue());
    assertEquals("error kind 42", e.getMessage());

    HostException known = new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "");
    assertEquals(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND_VALUE, known.kindValue());
    assertEquals("NOT_FOUND", known.getMessage());
  }

  @Test
  void aProtocolVersionIncludesEarlierMinorsOfItsMajorOnly() {
    ProtocolVersion v = new ProtocolVersion(1, 3);
    assertTrue(v.includes(1, 0));
    assertTrue(v.includes(1, 3));
    assertFalse(v.includes(1, 4));
    assertFalse(v.includes(2, 0));
    assertEquals("1.3", v.toString());
  }

  @Test
  void aRequestFromAForbiddenThreadNamesTheFix() {
    HostClient hc = new HostClient(m -> {}, t -> true);
    WrongThreadException e = assertThrows(WrongThreadException.class,
        () -> hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(1)));
    assertTrue(e.getMessage().contains("job"), e.getMessage());
  }

  @Test
  void theEndOfTheSessionFailsEveryWaiter() throws Exception {
    List<PluginMsg> sent = new ArrayList<>();
    HostClient hc = new HostClient(m -> {
      synchronized (sent) {
        sent.add(m);
        sent.notifyAll();
      }
    }, t -> false);
    CompletableFuture<Throwable> err = CompletableFuture.supplyAsync(() -> {
      try {
        hc.call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), Duration.ofSeconds(30));
        return null;
      } catch (Throwable e) {
        return e;
      }
    });
    awaitSent(sent, 0);
    hc.endSession("the host went away");
    assertInstanceOf(SessionEndedException.class, err.get());
  }

  @Test
  void boundsKnowTheirExceptions() {
    TimeMapping ok = TimeMapping.newBuilder().addAllT(java.util.Collections.nCopies(100_000, 1.0)).build();
    assertNull(Bounds.check(ok), "TimeMapping.t may hold a million");
    RoiPoseExtension ext = RoiPoseExtension.newBuilder().addAllPoseStretch(java.util.Collections.nCopies(70_000, 1.0)).build();
    assertNull(Bounds.check(ext), "so may a pose extension");
    ViewState tooMany = ViewState.newBuilder().addAllVisibleChannels(java.util.Collections.nCopies(65_537, 1)).build();
    Bounds.Breach b = Bounds.check(tooMany);
    assertNotNull(b);
    assertTrue(b.repeatedCount());
    var geo = vizi.plugin.v1.ViziPlugin.Geometry.newBuilder();
    for (int i = 0; i < 65; i++) geo.addLevels(vizi.plugin.v1.ViziPlugin.LevelScale.getDefaultInstance());
    assertNotNull(Bounds.check(geo.build()), "Geometry.levels is bounded at 64");
    assertNotNull(Bounds.check(ViewState.newBuilder().setDatasetName("x".repeat(Bounds.MAX_INLINE + 1)).build()),
        "a string counts as an inline byte field");
  }

  @Test
  void identityStringsAreCheckedForFormOnly() {
    assertNull(Identity.checkHello("Hello from vizi 1.0.3"));
    assertNotNull(Identity.checkHello("Hello from "));
    assertNotNull(Identity.checkHello("hello from vizi"));
    assertNotNull(Identity.checkHello("Hello from vizi\u0085"));
    assertNotNull(Identity.checkHello("Hello from " + "é".repeat(1100)));
    assertEquals("Ready; I am a b", Identity.readyId("a\nb"));
    assertTrue(Identity.readyId("x".repeat(5000)).getBytes(java.nio.charset.StandardCharsets.UTF_8).length <= 2048);
  }

  @Test
  void theReleaseFormOfAMavenVersion() {
    assertEquals("1.0.0", Identity.releaseForm("1.0.0"));
    assertEquals("1.0.1", Identity.releaseForm("1.0.1"));
    assertEquals("1.2.3", Identity.releaseForm("1.2.3"));
    assertEquals("1.0a104", Identity.releaseForm("1.0.0-alpha.104"));
    assertEquals("1.2b3", Identity.releaseForm("1.2.0-beta.3"));
    assertEquals("2.0rc1", Identity.releaseForm("2.0.0-rc.1"));
    assertEquals(Identity.SDK_VERSION, Identity.SDK_RELEASE);
  }

  static String awaitSent(List<PluginMsg> sent, int index) throws InterruptedException {
    synchronized (sent) {
      long deadline = System.currentTimeMillis() + 5000;
      while (sent.size() <= index) {
        long left = deadline - System.currentTimeMillis();
        if (left <= 0) throw new AssertionError("nothing sent");
        sent.wait(left);
      }
      return sent.get(index).getId();
    }
  }
}
