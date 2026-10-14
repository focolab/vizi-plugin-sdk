package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.HostSegment;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.CloseReader;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.DatasetState;
import vizi.plugin.v1.ViziPlugin.EpochChange;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.Hello;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.LevelScale;
import vizi.plugin.v1.ViziPlugin.MetadataResult;
import vizi.plugin.v1.ViziPlugin.OpenReader;
import vizi.plugin.v1.ViziPlugin.PaneEpoch;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ReadVolume;
import vizi.plugin.v1.ViziPlugin.ReaderOpened;
import vizi.plugin.v1.ViziPlugin.RegionProducer;
import vizi.plugin.v1.ViziPlugin.ReleaseLoan;
import vizi.plugin.v1.ViziPlugin.Shutdown;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;
import vizi.plugin.v1.ViziPlugin.ShutdownReport;
import vizi.plugin.v1.ViziPlugin.TimeMapping;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Value;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/** The control plane over a real gRPC stream and a real doorbell, against a test host. */
class ControlTest {

  static final Duration T = Duration.ofSeconds(10);

  /** A small application recording what reached it. */
  static class App implements Application {
    final List<String> calls = new CopyOnWriteArrayList<>();
    final List<InputRecord> inputs = new CopyOnWriteArrayList<>();
    final List<Boolean> usable = new CopyOnWriteArrayList<>();
    volatile Optional<ShutdownHook> hook = Optional.empty();

    @Override
    public Manifest manifest() {
      return Manifest.builder("org.test.control", "Control test", "0.1")
          .entryCommand("./run")
          .command(CommandDecl.of("hello", "Hello"))
          .command(CommandDecl.of("fail", "Fail"))
          .reader(ReaderDecl.of("vol", ".vol"))
          .tool(ToolDecl.of("probe"))
          .build();
    }

    @Override
    public String versionString() {
      return "org.test.control 0.1 (vizi-sdk-java test)";
    }

    @Override
    public String invokeCommand(String id, Map<String, Value> args) throws Exception {
      calls.add("command " + id);
      if (id.equals("fail")) throw new IllegalStateException("it failed on purpose");
      return "hi " + (args.containsKey("value") ? args.get("value").getF() : "");
    }

    @Override
    public Dataset openReader(String readerId, String uri, String dir) throws Exception {
      calls.add("open " + uri);
      if (uri.endsWith("missing.vol")) throw new HandlerFailure(Error.Kind.NOT_FOUND, "no file " + uri);
      return new Dataset() {
        @Override
        public ReaderOpened describe() {
          return ReaderOpened.newBuilder().addShapeTczyx(2).addShapeTczyx(1).addShapeTczyx(2).addShapeTczyx(3).addShapeTczyx(4)
              .setDtype(DType.DTYPE_U8).build();
        }

        @Override
        public Volume volume(int t, int c) {
          MemorySegment m = Arena.ofAuto().allocate(24);
          for (int i = 0; i < 24; i++) m.set(ValueLayout.JAVA_BYTE, i, (byte) (t * 100 + i));
          return new Volume(2, 3, 4, DType.DTYPE_U8, m);
        }

        @Override
        public MetadataResult metadata() {
          return MetadataResult.getDefaultInstance();
        }

        @Override
        public void close() {
          calls.add("close " + uri);
        }
      };
    }

    @Override
    public void toolActivated(ToolBinding b) {
      calls.add("activated " + b.getTool());
    }

    @Override
    public void toolDeactivated(ToolBinding b) {
      calls.add("deactivated " + b.getTool());
    }

    @Override
    public void input(ToolBinding b, InputRecord ev, long closed, boolean ok, Optional<Arrangements.PaneState> pane) {
      inputs.add(ev);
      usable.add(ok);
    }

    @Override
    public void cancelled(Optional<ToolBinding> b, Outcome.Cancellation c) {
      calls.add("cancel " + c.interaction());
    }

    @Override
    public Optional<ShutdownHook> shutdownHook() {
      return hook;
    }
  }

  TestHost host;
  App app = new App();

  TestHost up() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(app).open();
    return host;
  }

  @AfterEach
  void down() {
    if (host != null) host.close();
  }

  // ---- handshake ----

  @Test
  void headersArriveBeforeAnyMessage() throws Exception {
    up();
    host.headers().get(5, java.util.concurrent.TimeUnit.SECONDS);
    assertTrue(host.seen().isEmpty(), "headers came with no message sent");
    PluginMsg ready = host.hello();
    assertEquals("Ready; I am org.test.control 0.1 (vizi-sdk-java test)", ready.getId());
    assertEquals(1, ready.getReady().getProtocolMajor());
    assertEquals("org.test.control", ready.getReady().getPluginId());
    assertEquals("vizi-java-testkit 1.0", host.serving.session().hostIdentity());
  }

  @Test
  void aWrongMajorFailsTheSession() throws Exception {
    up();
    host.send(HostMsg.newBuilder().setId("Hello from future 2.0").setHello(Hello.newBuilder().setProtocolMajor(2)).build());
    var status = host.closed().get(5, java.util.concurrent.TimeUnit.SECONDS);
    assertFalse(status.isOk());
    assertTrue(status.getDescription().contains("2.0") && status.getDescription().contains("1.0"), status.getDescription());
    assertEquals(status.getDescription().contains("Ready; I am"), true, "names both versions");
  }

  @Test
  void aMalformedHelloIdFailsTheSession() throws Exception {
    up();
    host.send(HostMsg.newBuilder().setId("Hello from vizi\u0007forged").setHello(Hello.newBuilder().setProtocolMajor(1)).build());
    var status = host.closed().get(5, java.util.concurrent.TimeUnit.SECONDS);
    assertTrue(status.getDescription().contains("control character"), status.getDescription());
  }

  // ---- classification ----

  @Test
  void anUnknownKindWithAnIdIsAnsweredUnsupportedAndAnEmptyOneIsNot() throws Exception {
    up().hello();
    host.send(HostMsg.newBuilder().setId("r77").build());
    PluginMsg r = host.reply("r77", T);
    assertEquals(Error.Kind.UNSUPPORTED, r.getError().getKind());
    host.send(HostMsg.newBuilder().build());
    assertTrue(host.quiet(Duration.ofMillis(200)).isEmpty(), "a push of an unknown kind is only diagnosed");
  }

  @Test
  void commandsReplyOnceAndARaisingHandlerIsOkFalse() throws Exception {
    up().hello();
    String a = host.nextId();
    host.send(HostMsg.newBuilder().setId(a).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("hello")
        .putArgs("value", Value.newBuilder().setF(2.5).build())).build());
    PluginMsg done = host.reply(a, T);
    assertTrue(done.getCommandDone().getOk());
    assertEquals("hi 2.5", done.getCommandDone().getMessage());
    String b = host.nextId();
    host.send(HostMsg.newBuilder().setId(b).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("fail")).build());
    PluginMsg failed = host.reply(b, T);
    assertFalse(failed.getCommandDone().getOk());
    assertTrue(failed.getCommandDone().getMessage().contains("on purpose"));
    String c = host.nextId();
    host.send(HostMsg.newBuilder().setId(c).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("nope")).build());
    assertEquals(Error.Kind.NOT_FOUND, host.reply(c, T).getError().getKind());
  }

  @Test
  void aRequestOverABoundIsRefusedProtocol() throws Exception {
    up().hello();
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setOpenReader(OpenReader.newBuilder().setUri("x".repeat(9 * 1024 * 1024))).build());
    PluginMsg r = host.reply(id, T);
    assertEquals(Error.Kind.PROTOCOL, r.getError().getKind());
    assertFalse(app.calls.stream().anyMatch(s -> s.startsWith("open")), "the handler never ran");
  }

  // ---- grants and readers ----

  @Test
  void aGrantInstallsInEitherOrderAndAReaderServesThroughG() throws Exception {
    up().hello();
    try (HostSegment early = HostSegment.create(0x10000000AL, 4096)) {
      host.grant(early, HostSegment.READ_WRITE, early.install("P", "unknown-panel", List.of(HostSegment.loaned(0, 64, 1024, RegionProducer.REGION_PRODUCER_PLUGIN))), false);
      PluginMsg ack = host.expect(PluginMsg::hasGrantAck, T);
      assertTrue(ack.getGrantAck().hasFailed(), "a P subject naming no declared panel is malformed");
    }
    String open = host.nextId();
    host.send(HostMsg.newBuilder().setId(open).setOpenReader(OpenReader.newBuilder().setUri("/data/a.vol")).build());
    PluginMsg opened = host.reply(open, T);
    assertEquals("r0", opened.getReaderOpened().getHandle());
    assertTrue(opened.getReaderOpened().getLittleEndian());
    try (HostSegment g = HostSegment.create(0x100000001L, 4096)) {
      host.grant(g, HostSegment.READ_WRITE, g.install("G", "", List.of(HostSegment.loaned(0, 64, 4032, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      PluginMsg ack = host.expect(m -> m.hasGrantAck() && m.getGrantAck().getSegmentId() == g.id, T);
      assertArrayEquals(g.proof, ack.getGrantAck().getInstalled().getIdentityProof().toByteArray());

      String read = host.nextId();
      host.send(HostMsg.newBuilder().setId(read).setReadVolume(ReadVolume.newBuilder().setHandle("r0").setT(1)).build());
      var ref = host.reply(read, T).getSegment();
      assertEquals(g.id, ref.getSegmentId());
      assertEquals(24, ref.getLen());
      assertEquals(List.of(2, 3, 4), ref.getShapeZyxList());
      assertEquals(100 + 5, g.view.get(ValueLayout.JAVA_BYTE, ref.getOffset() + 5), "the bytes are in G");

      String level = host.nextId();
      host.send(HostMsg.newBuilder().setId(level).setReadVolume(ReadVolume.newBuilder().setHandle("r0").setLevel(1)).build());
      assertEquals(Error.Kind.OUT_OF_BOUNDS, host.reply(level, T).getError().getKind());
      String stale = host.nextId();
      host.send(HostMsg.newBuilder().setId(stale).setReadVolume(ReadVolume.newBuilder().setHandle("r9")).build());
      assertEquals(Error.Kind.NOT_FOUND, host.reply(stale, T).getError().getKind());

      host.send(HostMsg.newBuilder().setReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(g.id).setRegion(0)
          .setOffset(ref.getOffset()).setLen(24)).build());

      // a refused open leaves the old reader untouched
      String bad = host.nextId();
      host.send(HostMsg.newBuilder().setId(bad).setOpenReader(OpenReader.newBuilder().setUri("/data/missing.vol")).build());
      assertEquals(Error.Kind.NOT_FOUND, host.reply(bad, T).getError().getKind());
      String again = host.nextId();
      host.send(HostMsg.newBuilder().setId(again).setReadVolume(ReadVolume.newBuilder().setHandle("r0").setT(0)).build());
      assertTrue(host.reply(again, T).hasSegment(), "r0 still reads after a refused replacement");

      String close = host.nextId();
      host.send(HostMsg.newBuilder().setId(close).setCloseReader(CloseReader.newBuilder().setHandle("r0")).build());
      assertTrue(host.reply(close, T).hasClosed());
      String twice = host.nextId();
      host.send(HostMsg.newBuilder().setId(twice).setCloseReader(CloseReader.newBuilder().setHandle("r0")).build());
      assertTrue(host.reply(twice, T).hasClosed(), "closing is idempotent");
    }
  }

  // ---- tools ----

  @Test
  void aToolBindingIsAcknowledgedAfterTheTransportAndInputFlows() throws Exception {
    up().hello();
    Geometry geom = Geometry.newBuilder().setGen(1).addLevels(LevelScale.newBuilder().setZ(1).setY(1).setX(1))
        .setCrop(vizi.plugin.v1.ViziPlugin.EdgeBox.newBuilder().setZ0(-0.5).setZ1(9.5).setY0(-0.5).setY1(9.5).setX0(-0.5).setX1(9.5))
        .build();
    host.send(HostMsg.newBuilder().setTimeMapping(TimeMapping.newBuilder().setGen(1).setRevision(1).addT(0).addT(0.5)).build());
    assertEquals(1, host.expect(PluginMsg::hasTimeMappingAck, T).getTimeMappingAck().getRevision());
    host.send(HostMsg.newBuilder().setEpochChange(EpochChange.newBuilder()
        .addDatasets(DatasetState.newBuilder().setGen(1).setGeometry(geom).setTimeMappingRevision(1))
        .addPanes(PaneEpoch.newBuilder().setViewId(0).setEpoch(3).setGen(1))).build());
    assertEquals(1, host.expect(PluginMsg::hasEpochAck, T).getEpochAck().getPanesCount());
    host.send(HostMsg.newBuilder().setToolBinding(ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(1)).build());
    assertEquals(1, host.expect(PluginMsg::hasToolBindingAck, T).getToolBindingAck().getTool());

    byte[] press = InputRecord.builder().phase(InputRecord.PRESS).interaction(1).tool(1).epoch(3).voxel(1, 2, 3).build().encode(1);
    byte[] far = InputRecord.builder().phase(InputRecord.DRAG).interaction(1).seq(1).tool(1).epoch(3).voxel(50, 2, 3).build().encode(2);
    byte[] late = InputRecord.builder().phase(InputRecord.DRAG).interaction(1).seq(2).tool(1).epoch(3).t(5).voxel(1, 1, 1).build().encode(3);
    host.sendRecord(press);
    host.sendRecord(far);
    host.sendRecord(late);
    long deadline = System.nanoTime() + T.toNanos();
    while (app.inputs.size() < 3 && System.nanoTime() < deadline) Thread.onSpinWait();
    assertEquals(3, app.inputs.size());
    assertEquals(List.of(true, false, false), app.usable, "outside the clip box, then past the time mapping");
    PluginMsg v = host.expect(PluginMsg::hasContractViolation, T);
    assertEquals(ViolationSite.VIOLATION_SITE_UNUSABLE_CONTEXT, v.getContractViolation().getWhere());
    assertEquals(112, v.getContractViolation().getRecord().size(), "the first report carries the record");
    assertTrue(app.calls.contains("activated 1"));

    host.send(HostMsg.newBuilder().setToolBinding(ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(2).setRetired(true)).build());
    host.expect(m -> m.hasToolBindingAck() && m.getToolBindingAck().getRevision() == 2, T);
    deadline = System.nanoTime() + T.toNanos();
    while (!app.calls.contains("deactivated 1") && System.nanoTime() < deadline) Thread.onSpinWait();
    int cancel = app.calls.indexOf("cancel 1");
    int deact = app.calls.indexOf("deactivated 1");
    assertTrue(cancel >= 0 && cancel < deact, "a cancel comes before the deactivation that caused it: " + app.calls);
  }

  @Test
  void anEpochNamingAnUnackedMappingIsNotAcknowledged() throws Exception {
    up().hello();
    host.send(HostMsg.newBuilder().setEpochChange(EpochChange.newBuilder()
        .addDatasets(DatasetState.newBuilder().setGen(4).setTimeMappingRevision(2))
        .addPanes(PaneEpoch.newBuilder().setViewId(1).setEpoch(1).setGen(4))).build());
    assertEquals(0, host.expect(PluginMsg::hasEpochAck, T).getEpochAck().getPanesCount());
    assertEquals(ViolationSite.VIOLATION_SITE_MISSING_ARRANGEMENT, host.expect(PluginMsg::hasContractViolation, T).getContractViolation().getWhere());
  }

  // ---- shutdown ----

  @Test
  void theShutdownWindowRunsTheHookReportsAndCloses() throws Exception {
    List<String> order = new ArrayList<>();
    java.util.concurrent.CountDownLatch release = new java.util.concurrent.CountDownLatch(1);
    app.hook = Optional.of((reason, detail) -> {
      release.await(5, java.util.concurrent.TimeUnit.SECONDS);
      order.add("hook " + reason);
    });
    up().hello();
    host.send(HostMsg.newBuilder().setShutdown(Shutdown.newBuilder().setReason(ShutdownReason.SHUTDOWN_REASON_RESTART).setDeadlineMs(2000)).build());
    String late = host.nextId();
    host.send(HostMsg.newBuilder().setId(late).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("hello")).build());
    assertEquals(Error.Kind.PROTOCOL, host.reply(late, T).getError().getKind(), "new work is refused in the window");
    release.countDown();
    PluginMsg report = host.expect(PluginMsg::hasShutdownReport, T);
    assertEquals(ShutdownReport.Outcome.COMPLETED, report.getShutdownReport().getOutcome());
    assertTrue(host.closed().get(5, java.util.concurrent.TimeUnit.SECONDS).isOk(), "the plugin closes the stream");
    assertEquals(List.of("hook SHUTDOWN_REASON_RESTART"), order);
    assertNotNull(host.serving.ended().get(5, java.util.concurrent.TimeUnit.SECONDS));
  }

  @Test
  void aMissingDeadlineIsReportedAndAdopted() throws Exception {
    up().hello();
    host.send(HostMsg.newBuilder().setShutdown(Shutdown.newBuilder()).build());
    PluginMsg v = host.expect(PluginMsg::hasContractViolation, T);
    assertEquals(ViolationSite.VIOLATION_SITE_MISSING_DEADLINE, v.getContractViolation().getWhere());
    assertEquals(ShutdownReport.Outcome.NO_HOOK, host.expect(PluginMsg::hasShutdownReport, T).getShutdownReport().getOutcome());
    assertTrue(host.closed().get(5, java.util.concurrent.TimeUnit.SECONDS).isOk());
  }

  @Test
  void theHostGoingAwayEndsTheSession() throws Exception {
    up().hello();
    host.halfClose();
    assertNotNull(host.serving.ended().get(5, java.util.concurrent.TimeUnit.SECONDS));
  }
}
