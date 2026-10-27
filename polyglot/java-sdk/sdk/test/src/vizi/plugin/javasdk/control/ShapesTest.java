package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Semaphore;
import java.util.concurrent.TimeUnit;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import pois_math.v1.RoiOuterClass.AutoSurface;
import pois_math.v1.RoiOuterClass.ConstrainMoveRequest;
import pois_math.v1.RoiOuterClass.DofMode;
import pois_math.v1.RoiOuterClass.LinearBinding;
import pois_math.v1.RoiOuterClass.Resolution;
import pois_math.v1.RoiOuterClass.Roi;
import pois_math.v1.RoiOuterClass.Vec3;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Shape;
import vizi.plugin.javasdk.ShapeEnded;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.ManifestException;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.RegionTypeDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ShapeEdit;
import vizi.plugin.v1.ViziPlugin.ShapeEditEnded;
import vizi.plugin.v1.ViziPlugin.ShapeResult;

/**
 * control_plane_protocol.md §8.8: region types a plugin shapes. The SDK answers
 * every {@code shape_commit}, stops proposing for a drag once its commit or end arrives, checks a
 * result before sending it, coalesces a drag's input, drops the orderings that are not errors
 * silently, and reports how each edit ended. Tested against the SDK's own test host: no
 * emulator sends a {@code ShapeEdit}.
 */
class ShapesTest {
  static final Duration T = Duration.ofSeconds(10);

  TestHost host;
  final List<String> calls = new CopyOnWriteArrayList<>();
  final List<ShapeEnded> ends = new CopyOnWriteArrayList<>();
  final Semaphore slow = new Semaphore(0);
  final CountDownLatch slowEntered = new CountDownLatch(1);
  final CountDownLatch commandStarted = new CountDownLatch(1);
  final CountDownLatch commandRelease = new CountDownLatch(1);

  @AfterEach
  void down() {
    slow.release(1000);
    commandRelease.countDown();
    if (host != null) host.close();
  }

  /** The tetrahedron's corners, about the origin, and its center, vertex 0. */
  static final double[][] CORNERS = {{1, 0, 0}, {0, 2, 0}, {0, 0, 3}, {-1, -1, -1}};

  static Roi tetra(String type) {
    return Rois.pointCloud(type, new double[] {0, 0, 0}, CORNERS, AutoSurface.AUTO_SURFACE_STAR).build();
  }

  /** Moves the grabbed handle to p1, the rest unchanged. */
  static Shape follow(vizi.plugin.javasdk.ShapeEdit e) {
    double[][] p = e.positions();
    p[e.handle()] = new double[] {e.p1().getZ(), e.p1().getY(), e.p1().getX()};
    return e.commit() ? Shape.ok(p) : Shape.clamped(p);
  }

  Plugin plugin() {
    var b = Plugin.builder("org.test.shapes", "Shapes", "0.1").entry("./run").permissions(Permission.CONTRIBUTE_ROIS);
    b.regionType(RegionTypeDecl.of("tetra").withName("Tetrahedron"), e -> {
      calls.add("tetra " + (e.commit() ? "c" : "d") + e.dragId() + "/" + e.seq());
      return follow(e);
    });
    b.regionType(RegionTypeDecl.of("slow"), e -> {
      calls.add("slow " + (e.commit() ? "c" : "d") + e.dragId() + "/" + e.seq());
      slowEntered.countDown();
      assertTrue(slow.tryAcquire(10, TimeUnit.SECONDS));
      return follow(e);
    });
    b.regionType(RegionTypeDecl.of("short"), e -> Shape.ok(new double[][] {{0, 0, 0}, {1, 1, 1}, {2, 2, 2}}));
    b.regionType(RegionTypeDecl.of("nan"), e -> {
      double[][] p = e.positions();
      p[2][1] = Double.NaN;
      return Shape.ok(p);
    });
    b.regionType(RegionTypeDecl.of("refuses"), e -> {
      throw Refusal.invalid("no shape here");
    });
    b.regionType(RegionTypeDecl.of("rejects"), e -> Shape.rejected("too thin"));
    b.regionType(RegionTypeDecl.of("unready"));
    b.onShapeEnded(ends::add);
    b.command(CommandDecl.of("block", "Block"), ctx -> {
      commandStarted.countDown();
      commandRelease.await(10, TimeUnit.SECONDS);
      return "unblocked";
    });
    return b.build();
  }

  void start() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().application()).open();
    host.hello();
  }

  static ShapeEdit edit(String type, long drag, int seq, double z) {
    return ShapeEdit.newBuilder().setRegionId("r" + drag).setContribId("c").setTypeName(type).setDragId(drag).setSeq(seq)
        .setMove(ConstrainMoveRequest.newBuilder().setRoi(tetra(type)).setHandle(1).setMode(DofMode.ALL)
            .setP0(Vec3.newBuilder().setZ(1)).setP1(Vec3.newBuilder().setZ(z).setY(0.5)))
        .build();
  }

  void drag(String type, long drag, int seq, double z) {
    host.send(HostMsg.newBuilder().setShapeDrag(edit(type, drag, seq, z)).build());
  }

  void commit(String type, long drag) {
    host.send(HostMsg.newBuilder().setShapeCommit(edit(type, drag, 0, 4)).build());
  }

  void ended(long drag, ShapeEditEnded.Outcome outcome, String detail) {
    host.send(HostMsg.newBuilder().setShapeEditEnded(ShapeEditEnded.newBuilder().setDragId(drag).setOutcome(outcome).setDetail(detail)).build());
  }

  ShapeResult answer(long drag) throws InterruptedException {
    return host.expect(m -> m.hasShapeAnswer() && m.getShapeAnswer().getDragId() == drag, T).getShapeAnswer();
  }

  ShapeResult proposal(long drag, int seq) throws InterruptedException {
    return host.expect(m -> m.hasShapeProposal() && m.getShapeProposal().getDragId() == drag && m.getShapeProposal().getSeq() == seq, T)
        .getShapeProposal();
  }

  static void until(java.util.function.BooleanSupplier ok) throws InterruptedException {
    long end = System.nanoTime() + T.toNanos();
    while (!ok.getAsBoolean()) {
      if (System.nanoTime() > end) throw new AssertionError("never happened");
      Thread.sleep(2);
    }
  }

  int diagnostics() {
    return host.serving.session().diagnosticCount();
  }

  // ---- the exchange ----

  @Test
  void aCommitIsAnsweredWithAPositionForEveryVertex() throws Exception {
    start();
    commit("tetra", 1);
    ShapeResult a = answer(1);
    assertEquals(0, a.getSeq());
    assertEquals(Resolution.OK, a.getResolution());
    assertEquals(5, a.getPositionsCount(), "the center and four corners");
    assertEquals(4.0, a.getPositions(1).getZ(), "the handle where the person put it");
    assertEquals(CORNERS[1][1], a.getPositions(2).getY(), "the others where they were");
  }

  @Test
  void aDragIsProposedNamingTheSeqItAnswers() throws Exception {
    start();
    drag("tetra", 2, 1, 3);
    ShapeResult p = proposal(2, 1);
    assertEquals(Resolution.CLAMPED, p.getResolution());
    assertEquals(3.0, p.getPositions(1).getZ());
    commit("tetra", 2);
    assertEquals(Resolution.OK, answer(2).getResolution());
  }

  @Test
  void everyCommitIsAnsweredEvenWithNoShaper() throws Exception {
    start();
    int before = diagnostics();
    commit("unready", 3);
    ShapeResult a = answer(3);
    assertEquals(Resolution.REJECTED, a.getResolution());
    assertTrue(a.getMessage().contains("no shaper registered for region type 'unready'"), a.getMessage());
    assertEquals(0, a.getPositionsCount(), "none with REJECTED");
    assertEquals(before, diagnostics(), "a type declared with no shaper is the plugin's choice: no note");
    commit("someone.elses", 4);
    assertEquals(Resolution.REJECTED, answer(4).getResolution(), "a type the plugin does not declare: still an answer");
    drag("unready", 5, 1, 2);
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream().filter(PluginMsg::hasShapeProposal).toList(),
        "nothing to propose without a shaper");
  }

  @Test
  void aResultThatIsNotOneFinitePositionPerVertexIsNeverSent() throws Exception {
    start();
    commit("short", 1);
    ShapeResult a = answer(1);
    assertEquals(Resolution.REJECTED, a.getResolution());
    assertTrue(a.getMessage().contains("3 positions for 5 vertices"), a.getMessage());
    commit("nan", 2);
    ShapeResult n = answer(2);
    assertEquals(Resolution.REJECTED, n.getResolution());
    assertTrue(n.getMessage().contains("position 2 is not finite"), n.getMessage());
    commit("refuses", 3);
    assertEquals("no shape here", answer(3).getMessage(), "a Refusal's message, for the person");
    commit("rejects", 4);
    ShapeResult r = answer(4);
    assertEquals(Resolution.REJECTED, r.getResolution());
    assertEquals("too thin", r.getMessage());
    drag("short", 5, 1, 2);
    drag("nan", 6, 1, 2);
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream().filter(PluginMsg::hasShapeProposal).toList(),
        "a bad proposal is not sent");
  }

  /** The mandated stop: no proposal for a drag once its commit has arrived, and its late input is dropped silently. */
  @Test
  void nothingIsProposedForADragOnceItsCommitArrives() throws Exception {
    start();
    drag("slow", 7, 1, 2);
    assertTrue(slowEntered.await(5, TimeUnit.SECONDS));
    drag("slow", 7, 2, 2.5);
    drag("slow", 7, 3, 3);
    commit("slow", 7);
    // the commit reaches the shape worker's queue while the first drag is still shaped
    until(() -> host.serving.session().shapes().stoppedForTest().contains(7L));
    int before = diagnostics();
    slow.release(10);
    ShapeResult a = answer(7);
    assertEquals(Resolution.OK, a.getResolution());
    drag("slow", 7, 4, 5); // after its commit: dropped, not an error
    List<PluginMsg> after = host.quiet(Duration.ofMillis(300));
    assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasShapeProposal).toList(), "no proposal for drag 7 at all");
    assertEquals(List.of(), after.stream().filter(m -> m.hasShapeAnswer() || m.hasContractViolation()).toList());
    assertEquals(List.of("slow d7/1", "slow c7/0"), calls, "the pending input was dropped at the commit, the late drag never shaped");
    assertEquals(before, diagnostics(), "a drag after its commit is not logged");
  }

  /** A shaper slower than the host's tick answers the newest input next, not each one in turn. */
  @Test
  void aSlowShaperIsHandedTheNewestInputNotAQueue() throws Exception {
    start();
    drag("slow", 8, 1, 2);
    assertTrue(slowEntered.await(5, TimeUnit.SECONDS));
    for (int seq = 2; seq <= 5; seq++) drag("slow", 8, seq, 2 + seq);
    until(() -> host.serving.session().shapes().superseded() >= 3);
    slow.release(10);
    ShapeResult p = proposal(8, 5);
    assertEquals(7.0, p.getPositions(1).getZ());
    assertEquals(List.of("slow d8/1", "slow d8/5"), calls);
    assertTrue(host.serving.session().shapes().superseded() >= 3, "seq 2-4 superseded unshaped");
  }

  @Test
  void aCommitNeverWaitsBehindACommand() throws Exception {
    start();
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("block")).build());
    assertTrue(commandStarted.await(5, TimeUnit.SECONDS));
    commit("tetra", 9);
    assertEquals(Resolution.OK, answer(9).getResolution(), "answered while the command holds its worker");
    commandRelease.countDown();
    assertEquals("unblocked", host.reply(id, T).getCommandDone().getMessage());
  }

  @Test
  void theEndedHookHearsEveryOutcomeAndAnEndForADragNeverAnsweredIsQuiet() throws Exception {
    start();
    commit("tetra", 10);
    answer(10);
    ended(10, ShapeEditEnded.Outcome.COMMITTED, "");
    int before = diagnostics();
    ended(11, ShapeEditEnded.Outcome.CANCELLED, "the person pressed Escape"); // a drag never answered
    ended(12, ShapeEditEnded.Outcome.REVERTED, "too thin");
    long end = System.nanoTime() + T.toNanos();
    while (ends.size() < 3 && System.nanoTime() < end) Thread.sleep(5);
    assertEquals(3, ends.size());
    assertEquals(ShapeEditEnded.Outcome.COMMITTED, ends.get(0).outcome());
    assertTrue(ends.get(0).last().orElseThrow().commit(), "the commit was the last edit it saw");
    assertEquals("tetra", ends.get(0).last().get().typeName());
    assertEquals(ShapeEditEnded.Outcome.CANCELLED, ends.get(1).outcome());
    assertEquals("the person pressed Escape", ends.get(1).detail());
    assertTrue(ends.get(1).last().isEmpty());
    assertEquals(ShapeEditEnded.Outcome.REVERTED, ends.get(2).outcome());
    assertEquals(before, diagnostics(), "an end for a drag never answered is not logged");
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream()
        .filter(m -> m.hasShapeAnswer() || m.hasShapeProposal() || m.hasContractViolation()).toList(), "and sends nothing");
  }

  /** The wire does not order a one-shot against a coalescing push: an end may overtake its own commit. */
  @Test
  void aCommitWhoseEndHasArrivedIsNotAnswered() throws Exception {
    start();
    ended(13, ShapeEditEnded.Outcome.CANCELLED, "");
    commit("tetra", 13);
    drag("tetra", 13, 1, 2);
    assertEquals(List.of(), host.quiet(Duration.ofMillis(300)).stream()
        .filter(m -> m.hasShapeAnswer() || m.hasShapeProposal()).toList(), "over before its commit arrived: nobody waits on it");
  }

  // ---- the outbound key ----

  @Test
  void aCommitWithdrawsAProposalStillQueued() throws Exception {
    OutboundTest.Held ch = new OutboundTest.Held();
    Outbound out = new Outbound(ch, s -> {});
    out.shapeProposal(PluginMsg.newBuilder().setShapeProposal(ShapeResult.newBuilder().setDragId(1).setSeq(1)).build(), 1);
    out.shapeProposal(PluginMsg.newBuilder().setShapeProposal(ShapeResult.newBuilder().setDragId(1).setSeq(2)).build(), 1);
    out.shapeProposal(PluginMsg.newBuilder().setShapeProposal(ShapeResult.newBuilder().setDragId(2).setSeq(1)).build(), 2);
    out.dropShapeProposal(2);
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    assertEquals(List.of(2), ch.got().stream().map(m -> m.getShapeProposal().getSeq()).toList(),
        "the latest for drag 1; drag 2's withdrawn");
  }

  @Test
  void theResultCheck() {
    ShapeEdit e = edit("tetra", 1, 1, 2);
    List<Vec3> five = new ArrayList<>();
    for (int i = 0; i < 5; i++) five.add(Vec3.newBuilder().setZ(i).build());
    assertNull(Shapes.problem(ShapeResult.newBuilder().addAllPositions(five).build(), e));
    assertNull(Shapes.problem(ShapeResult.newBuilder().setResolution(Resolution.REJECTED).build(), e));
    assertEquals("4 positions for 5 vertices", Shapes.problem(ShapeResult.newBuilder().addAllPositions(five.subList(0, 4)).build(), e));
    five.set(3, Vec3.newBuilder().setX(Double.POSITIVE_INFINITY).build());
    assertEquals("position 3 is not finite", Shapes.problem(ShapeResult.newBuilder().addAllPositions(five).build(), e));
    assertNotNull(Shapes.problem(ShapeResult.newBuilder().setResolutionValue(7).build(), e));
  }

  // ---- the manifest ----

  @Test
  void regionTypesNeedContributeRoisAndAreWritten() throws Exception {
    var b = Plugin.builder("org.test.rt", "RT", "0.1").entry("./run");
    b.regionType(RegionTypeDecl.of("blob"), e -> Shape.rejected("no"));
    b.command(CommandDecl.of("add", "Add"), ctx -> "");
    IllegalStateException x = assertThrows(IllegalStateException.class, b::build);
    assertTrue(x.getMessage().contains("CONTRIBUTE_ROIS"), x.getMessage());

    var ok = Plugin.builder("org.test.rt", "RT", "0.1").entry("./run").permissions(Permission.CONTRIBUTE_ROIS);
    ok.regionType(RegionTypeDecl.of("tetra.volume").withName("Tetrahedron (keeps its volume)"), e -> Shape.rejected("no"));
    ok.regionType(RegionTypeDecl.of("blob"));
    assertThrows(IllegalArgumentException.class, () -> ok.regionType(RegionTypeDecl.of("blob")), "declared twice");
    String json = ManifestWriter.write(ok.build().manifest());
    assertTrue(json.contains("\"region_types\""), json);
    var back = ManifestParser.parseDetached(json).manifest();
    assertEquals(List.of("tetra.volume", "blob"), back.contributions().regionTypeIds());
    assertEquals("Tetrahedron (keeps its volume)", back.contributions().regionType("tetra.volume").orElseThrow().name());

    assertEquals("region type id 'a b' may hold only letters, digits, '.', '_' and '-'", RegionTypeDecl.idProblem("a b"));
    assertThrows(IllegalArgumentException.class, () -> RegionTypeDecl.of("surfq6"));
    assertThrows(IllegalArgumentException.class, () -> RegionTypeDecl.of("vizi.x"));
  }

  /** §8.4: one panel's lists together realize at most 4096 cells. */
  @Test
  void aPanelsListsTogetherAreCapped() throws Exception {
    WidgetDecl a = new WidgetDecl.RowList("a", List.of(label("a1"), label("a2")), 1024, 8, false, "");
    WidgetDecl b = new WidgetDecl.RowList("b", List.of(label("b1"), label("b2")), 1024, 8, false, "");
    WidgetDecl c = new WidgetDecl.RowList("c", List.of(label("c1")), 1, 1, false, "");
    PanelDecl.of("fits", "Fits", column("root", a, b)); // 4096 exactly
    IllegalArgumentException x = assertThrows(IllegalArgumentException.class, () -> PanelDecl.of("over", "Over", column("root", a, b, c)));
    assertTrue(x.getMessage().contains("4097"), x.getMessage());
    String json = """
        { "manifest_version": 1, "id": "org.example.x", "name": "X", "version": "1", "engines": { "vizi": "1.0" }, "entry": { "command": ["x"] },
          "contributions": { "panels": [ { "id": "p", "title": "P", "root": { "kind": "column", "id": "root", "children": [
            { "kind": "list", "id": "a", "max_rows": 1024, "row": [ { "kind": "label", "id": "a1" }, { "kind": "label", "id": "a2" } ] },
            { "kind": "list", "id": "b", "max_rows": 1024, "row": [ { "kind": "label", "id": "b1" }, { "kind": "label", "id": "b2" } ] },
            { "kind": "list", "id": "c", "max_rows": 1, "row": [ { "kind": "label", "id": "c1" } ] } ] } } ] } }""";
    ManifestException m = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(json));
    assertTrue(m.getMessage().contains("panel 'p''s lists may realize at most 4096 cells together (each list's max_rows × row cells), not 4097"),
        m.getMessage());
  }

  // ---- the helpers ----

  /** The scale case's bindings are exact under a uniform scaling about the center, from any handle. */
  @Test
  void scaleBindingsAreExactUnderUniformScaling() {
    Roi.Builder r = Rois.pointCloud("scaled", new double[] {0.25, 0.25, 0.5}, CORNERS, AutoSurface.AUTO_SURFACE_HULL);
    for (int i = 1; i <= 4; i++) r.getVerticesBuilder(i).setHandle(Rois.radialHandle(0, 0.1));
    List<LinearBinding> bindings = Rois.scaleBindings(r, 0, 1, 2, 3, 4);
    assertEquals(12, bindings.size());
    double k = 1.7;
    for (int i = 1; i <= 4; i++) {
      double[] c = zyx(r, 0), vi = zyx(r, i);
      double[] delta = {(k - 1) * (vi[0] - c[0]), (k - 1) * (vi[1] - c[1]), (k - 1) * (vi[2] - c[2])};
      for (LinearBinding b : bindings) {
        if (b.getTarget() != i) continue;
        double[] vj = zyx(r, b.getMoved());
        for (int a = 0; a < 3; a++) {
          double moved = b.getM(3 * a) * delta[0] + b.getM(3 * a + 1) * delta[1] + b.getM(3 * a + 2) * delta[2];
          assertEquals((k - 1) * (vj[a] - c[a]), moved, 1e-12, "binding " + b.getMoved() + "<-" + i + " axis " + a);
        }
      }
    }
    assertEquals(DofMode.PRIMARY, r.getVertices(1).getHandle().getMode());
    assertEquals(0, r.getVertices(1).getHandle().getDofMask(), "a PRIMARY handle's mask is left 0");
    assertEquals(1.0, Rois.radialHandle(4, 1.0).getMinExtent());
    assertFalse(Rois.radialHandle(4).hasMinExtent());
    assertFalse(r.getVertices(0).getEnclosing(), "the center is not on the surface");
    assertTrue(r.getVertices(1).getEnclosing());
    assertEquals(AutoSurface.AUTO_SURFACE_HULL, r.getAutoSurface());
  }

  static double[] zyx(Roi.Builder r, int i) {
    Vec3 v = r.getVertices(i).getPosition();
    return new double[] {v.getZ(), v.getY(), v.getX()};
  }
}
