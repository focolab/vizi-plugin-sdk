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
