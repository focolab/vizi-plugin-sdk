package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.google.protobuf.ByteString;
import com.google.protobuf.Message;
import com.google.protobuf.UnknownFieldSet;
import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CopyOnWriteArrayList;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.StageDecl;
import vizi.plugin.javasdk.manifest.StageParam;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.HostSegment;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.DatasetState;
import vizi.plugin.v1.ViziPlugin.EpochChange;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.PaneEpoch;
import vizi.plugin.v1.ViziPlugin.PlanStage;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StagePlanned;
import vizi.plugin.v1.ViziPlugin.StageShape;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.ToolMode;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * What a later minor's values do here: an unknown dtype or tool mode refuses its one item, with a
 * local diagnostic and never a ContractViolation, and an unknown field takes no part in an
 * arrangement's content or a plan's identity, and is never echoed back.
 */
class LaterValuesTest {

  static final Duration T = Duration.ofSeconds(10);
  static final long RING = 0x10000000BL;

  static final class App implements Application {
    final List<String> calls = new CopyOnWriteArrayList<>();

    @Override
    public Manifest manifest() {
      return Manifest.builder("org.test.later", "Later values", "0.1")
          .entryCommand("./run")
          .command(CommandDecl.of("barrier", "Barrier"))
          .stage(StageDecl.of("s"))
          .stage(StageDecl.of("p", StageParam.ofInt("mode", 1).withMin(1).withMax(2)))
          .tool(ToolDecl.of("probe"))
          .build();
    }

    @Override
    public String versionString() {
      return "org.test.later 0.1 (vizi-sdk-java test)";
    }

    @Override
    public String invokeCommand(String id, Map<String, Value> args) {
      return "";
    }

    @Override
    public StagePlanned planStage(PlanStage request) {
      calls.add("plan");
      return StagePlanned.newBuilder().setOutput(request.getInput()).build();
    }

    @Override
    public void toolActivated(ToolBinding b) {
      calls.add("activated " + b.getTool());
    }
  }

  TestHost host;
  HostSegment segment;
  App app = new App();

  TestHost up() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(app).open();
    host.hello();
    return host;
  }

  @AfterEach
  void down() {
    if (host != null) host.close();
    if (segment != null) segment.close();
  }

  @SuppressWarnings("unchecked")
  static <M extends Message> M withUnknown(M m) {
    return (M) m.toBuilder().setUnknownFields(UnknownFieldSet.newBuilder()
        .addField(9999, UnknownFieldSet.Field.newBuilder().addVarint(1).build()).build()).build();
  }

  static StageShape shape() {
    return StageShape.newBuilder().addChannels(0).setZ(1).setY(2).setX(4).setDtype(DType.DTYPE_U8).build();
  }

  /** Everything the plugin sent between now and a command's reply. */
  List<PluginMsg> upToBarrier() throws InterruptedException {
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("barrier")).build());
    host.reply(id, T);
    return host.quiet(Duration.ofMillis(200));
  }

  void plan(StageShape input) throws InterruptedException {
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setPlanStage(PlanStage.newBuilder().setStageId("s").setInput(input)).build());
    assertTrue(host.reply(id, T).hasStagePlanned());
  }

  void ring() throws Exception {
    HostSegment s = segment = HostSegment.create(RING, 4096);
    host.grant(s, HostSegment.READ_WRITE, s.install("S", "", List.of(
        HostSegment.of(0, 64, 1024, RegionDiscipline.REGION_DISCIPLINE_STAGED, RegionProducer.REGION_PRODUCER_HOST))), true);
    assertTrue(host.expect(m -> m.hasGrantAck() && m.getGrantAck().getSegmentId() == RING, T).getGrantAck().hasInstalled());
  }

  StageArm arm(StageShape input) {
    return StageArm.newBuilder().setStageId("s").setRevision(1).setSegmentId(RING).setPipeline(1).setPosition(1)
        .setInput(input).setOutput(shape()).build();
  }

  @Test
  void aPlanNamingAnUnknownDtypeIsRefusedUnsupportedAlone() throws Exception {
    up();
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setPlanStage(PlanStage.newBuilder().setStageId("s")
        .setInput(shape().toBuilder().setDtypeValue(5))).build());
    Error e = host.reply(id, T).getError();
    assertEquals(Error.Kind.UNSUPPORTED, e.getKind());
    assertTrue(e.getMessage().contains("dtype 5"), e.getMessage());
    assertFalse(app.calls.contains("plan"), "the stage never saw it");
    assertTrue(upToBarrier().stream().noneMatch(PluginMsg::hasContractViolation));
    plan(shape());
  }

  @Test
  void anArmNamingAnUnknownDtypeIsNeitherAcknowledgedNorReported() throws Exception {
    up();
    ring();
    plan(shape());
    host.send(HostMsg.newBuilder().setStageArm(arm(shape().toBuilder().setDtypeValue(5).build())).build());
    host.send(HostMsg.newBuilder().setStageArm(arm(shape()).toBuilder()
        .setOutput(shape().toBuilder().setDtypeValue(1000)).build()).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(seen.stream().noneMatch(m -> m.hasStageArmAck() || m.hasContractViolation()), seen.toString());
    host.send(HostMsg.newBuilder().setStageArm(arm(shape())).build());
    assertEquals(1, host.expect(PluginMsg::hasStageArmAck, T).getStageArmAck().getRevision());
  }

  /** A Value whose kind is a later minor's arm: field 7 (6 is {@code strings}), carrying {@code 08 01}. */
  static Value laterKind() {
    return Value.newBuilder().setUnknownFields(UnknownFieldSet.newBuilder()
        .addField(7, UnknownFieldSet.Field.newBuilder().addLengthDelimited(ByteString.copyFrom(new byte[] {0x08, 0x01})).build())
        .build()).build();
  }

  @Test
  void anArmWithAnUnknownValueKindIsNeitherAcknowledgedNorReportedAndItsPlanStaysPlanned() throws Exception {
    up();
    ring();
    Value mode = Value.newBuilder().setI(1).build();
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setPlanStage(PlanStage.newBuilder().setStageId("p").setInput(shape())
        .putParams("mode", mode)).build());
    assertTrue(host.reply(id, T).hasStagePlanned());
    StageArm a = arm(shape()).toBuilder().setStageId("p").putParams("mode", mode).build();
    host.send(HostMsg.newBuilder().setStageArm(a.toBuilder().putParams("mode", laterKind())).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(seen.stream().noneMatch(m -> m.hasStageArmAck() || m.hasContractViolation()), seen.toString());
    // Nothing was installed and the plan stays planned: the arm with a known kind is acknowledged.
    host.send(HostMsg.newBuilder().setStageArm(a).build());
    assertEquals(1, host.expect(PluginMsg::hasStageArmAck, T).getStageArmAck().getRevision());
  }

  @Test
  void anUnknownOneofArmIsNamedWithItsPath() {
    PlanStage later = PlanStage.newBuilder().setInput(shape()).putParams("b", Value.newBuilder().setI(1).build())
        .putParams("a", laterKind()).build();
    assertEquals("params[\"a\"] Value.kind (field 7)", KnownFields.unknownOneofArm(later));
    // An unknown field beside a known arm is not an unknown arm.
    assertNull(KnownFields.unknownOneofArm(PlanStage.newBuilder().setInput(withUnknown(shape()))
        .putParams("k", withUnknown(Value.newBuilder().setI(3).build())).build()));
  }

  @Test
  void anArmMatchesItsPlanAndARestatementItsArmOnKnownFieldsOnly() throws Exception {
    up();
    ring();
    plan(withUnknown(shape()));
    StageArm a = arm(shape()); // the plan's input carried a field this build does not know; the arm's does not
    host.send(HostMsg.newBuilder().setStageArm(a).build());
    host.expect(PluginMsg::hasStageArmAck, T);
    StageArm restated = withUnknown(a.toBuilder().setInput(withUnknown(a.getInput())).build());
    host.send(HostMsg.newBuilder().setStageArm(restated).build());
    host.expect(PluginMsg::hasStageArmAck, T);
    assertTrue(upToBarrier().stream().noneMatch(PluginMsg::hasContractViolation));
  }

  @Test
  void anUnknownToolModeIsRefusedUnacknowledgedAndNeverActivated() throws Exception {
    up();
    ToolBinding b = ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(1).setMode(ToolMode.TOOL_MODE_FIRE).build();
    host.send(HostMsg.newBuilder().setToolBinding(b.toBuilder().setModeValue(3)).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(seen.stream().noneMatch(m -> m.hasToolBindingAck() || m.hasContractViolation()), seen.toString());
    assertFalse(app.calls.contains("activated 1"));
    // Nothing was installed: the same identity's first known statement is new, not a mismatch.
    host.send(HostMsg.newBuilder().setToolBinding(b).build());
    assertEquals(1, host.expect(PluginMsg::hasToolBindingAck, T).getToolBindingAck().getRevision());
    // A restatement differing only by an unknown field is the same content.
    host.send(HostMsg.newBuilder().setToolBinding(withUnknown(b)).build());
    assertEquals(1, host.expect(PluginMsg::hasToolBindingAck, T).getToolBindingAck().getRevision());
    assertTrue(upToBarrier().stream().noneMatch(PluginMsg::hasContractViolation));
  }

  static boolean firstRevisionReported(List<PluginMsg> seen) {
    return seen.stream().anyMatch(m -> m.hasContractViolation()
        && m.getContractViolation().getWhere() == vizi.plugin.v1.ViziPlugin.ViolationSite.VIOLATION_SITE_ARRANGEMENT_FIRST_REVISION);
  }

  @Test
  void aReArmAfterARefusedFirstArmIsNotReportedAsAFirstRevision() throws Exception {
    up();
    ring();
    Value mode = Value.newBuilder().setI(1).build();
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setPlanStage(PlanStage.newBuilder().setStageId("p").setInput(shape())
        .putParams("mode", mode)).build());
    assertTrue(host.reply(id, T).hasStagePlanned());
    StageArm a = arm(shape()).toBuilder().setStageId("p").putParams("mode", mode).build();
    host.send(HostMsg.newBuilder().setStageArm(a.toBuilder().putParams("mode", laterKind())).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(seen.stream().noneMatch(m -> m.hasStageArmAck() || m.hasContractViolation()), seen.toString());
    // The refused revision 1 was received: the re-arm with a known kind is revision 2, and not a first.
    host.send(HostMsg.newBuilder().setStageArm(a.toBuilder().setRevision(2)).build());
    seen = upToBarrier();
    assertTrue(seen.stream().anyMatch(m -> m.hasStageArmAck() && m.getStageArmAck().getRevision() == 2), seen.toString());
    assertTrue(seen.stream().noneMatch(PluginMsg::hasContractViolation), seen.toString());
  }

  @Test
  void aKnownModeAfterARefusedFirstBindingIsNotReportedAsAFirstRevision() throws Exception {
    up();
    ToolBinding b = ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(1).setMode(ToolMode.TOOL_MODE_FIRE).build();
    host.send(HostMsg.newBuilder().setToolBinding(b.toBuilder().setModeValue(3)).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(seen.stream().noneMatch(m -> m.hasToolBindingAck() || m.hasContractViolation()), seen.toString());
    host.send(HostMsg.newBuilder().setToolBinding(b.toBuilder().setRevision(2)).build());
    seen = upToBarrier();
    assertTrue(seen.stream().anyMatch(m -> m.hasToolBindingAck() && m.getToolBindingAck().getRevision() == 2), seen.toString());
    assertTrue(seen.stream().noneMatch(PluginMsg::hasContractViolation), seen.toString());
    assertTrue(app.calls.contains("activated 1"), app.calls.toString());
  }

  // The first report of a site goes at once and later ones are aggregated, so one site per test.
  @Test
  void aFirstArmAtRevisionTwoWithNothingBeforeItIsStillReported() throws Exception {
    up();
    ring();
    plan(shape());
    host.send(HostMsg.newBuilder().setStageArm(arm(shape()).toBuilder().setRevision(2)).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(firstRevisionReported(seen), seen.toString());
    assertTrue(seen.stream().anyMatch(m -> m.hasStageArmAck() && m.getStageArmAck().getRevision() == 2), seen.toString());
  }

  @Test
  void aFirstBindingAtRevisionTwoWithNothingBeforeItIsStillReported() throws Exception {
    up();
    host.send(HostMsg.newBuilder().setToolBinding(ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(2)
        .setMode(ToolMode.TOOL_MODE_FIRE)).build());
    List<PluginMsg> seen = upToBarrier();
    assertTrue(firstRevisionReported(seen), seen.toString());
    assertTrue(seen.stream().anyMatch(m -> m.hasToolBindingAck() && m.getToolBindingAck().getRevision() == 2), seen.toString());
  }

  @Test
  void anEpochAckNamesTheKnownPairAndEchoesNoUnknownField() throws Exception {
    up();
    DatasetState ds = DatasetState.newBuilder().setGen(1).setGeometry(Geometry.newBuilder().setGen(1)).build();
    PaneEpoch pane = withUnknown(PaneEpoch.newBuilder().setViewId(2).setEpoch(3).setGen(1).build());
    for (int i = 0; i < 2; i++) {
      DatasetState sent = i == 0 ? ds : withUnknown(ds);
      host.send(HostMsg.newBuilder().setEpochChange(EpochChange.newBuilder().addDatasets(sent).addPanes(pane)).build());
      List<PaneEpoch> acked = host.expect(PluginMsg::hasEpochAck, T).getEpochAck().getPanesList();
      assertEquals(List.of(PaneEpoch.newBuilder().setViewId(2).setEpoch(3).build()), acked);
      assertTrue(acked.get(0).getUnknownFields().asMap().isEmpty());
    }
    assertTrue(upToBarrier().stream().noneMatch(PluginMsg::hasContractViolation));
  }

  @Test
  void knownFieldsStripEveryDepthAndLeaveTheOriginal() {
    PlanStage withNew = withUnknown(PlanStage.newBuilder().setStageId("s").setInput(withUnknown(shape()))
        .putParams("k", withUnknown(Value.newBuilder().setI(3).build())).build());
    PlanStage plain = PlanStage.newBuilder().setStageId("s").setInput(shape()).putParams("k", Value.newBuilder().setI(3).build()).build();
    assertNotEquals(plain, withNew);
    assertEquals(plain, KnownFields.of(withNew));
    assertTrue(KnownFields.same(plain, withNew));
    assertFalse(withNew.getInput().getUnknownFields().asMap().isEmpty(), "the original keeps its fields");
    assertFalse(KnownFields.same(plain, plain.toBuilder().setStageId("t").build()));
  }
}
