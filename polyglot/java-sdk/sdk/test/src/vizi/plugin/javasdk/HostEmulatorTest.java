package vizi.plugin.javasdk;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;
import static org.junit.jupiter.api.Assumptions.assumeTrue;
import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.image;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.ByteOrder;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Records;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.javasdk.memory.PulsedLane;
import vizi.plugin.javasdk.memory.RegionTable;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.javasdk.testkit.HostSegment;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.ChunkWant;
import vizi.plugin.v1.ViziPlugin.ChunksWanted;
import vizi.plugin.v1.ViziPlugin.DataVolume;
import vizi.plugin.v1.ViziPlugin.DatasetState;
import vizi.plugin.v1.ViziPlugin.EdgeBox;
import vizi.plugin.v1.ViziPlugin.EpochChange;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.LevelScale;
import vizi.plugin.v1.ViziPlugin.OpenReader;
import vizi.plugin.v1.ViziPlugin.OverlayUpdate;
import vizi.plugin.v1.ViziPlugin.PaneEpoch;
import vizi.plugin.v1.ViziPlugin.PanelOpened;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ReadVolume;
import vizi.plugin.v1.ViziPlugin.RegionProducer;
import vizi.plugin.v1.ViziPlugin.ReleaseLoan;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.Shutdown;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;
import vizi.plugin.v1.ViziPlugin.ShutdownReport;
import vizi.plugin.v1.ViziPlugin.TimeMapping;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * The author API end to end over the real transports: a Java host written from the spec drives a
 * plugin built with {@link Plugin} through a real doorbell socketpair, real shared-memory grants
 * (both orders) and a real gRPC stream. Reads through {@code G}, a panel image through {@code P},
 * an SDK-answered widget, a tool whose live marker rides the Pulsed lane with a {@code D} hint
 * and whose release is a loan, a job making a host request answered by a deposit, a chunked
 * overlay, and the shutdown window with a job runner.
 */
class HostEmulatorTest {

  static final Duration T = Duration.ofSeconds(10);
  static final byte[] PNG = {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n', 1, 2, 3};
  static final ValueLayout.OfDouble F64 = ValueLayout.JAVA_DOUBLE_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);

  static final java.util.concurrent.ConcurrentLinkedQueue<String> EXPORTED = new java.util.concurrent.ConcurrentLinkedQueue<>();

  TestHost host;
  final AtomicLong recordIds = new AtomicLong();

  @AfterEach
  void down() {
    if (host != null) host.close();
  }

  static Plugin plugin() {
    var b = Plugin.builder("org.test.emulated", "Emulated", "0.1").entry("./run").permissions(Permission.DATA_ACCESS);
    b.reader(ReaderDecl.of("vol", ".vol"), (uri, dir) -> new Dataset() {
      public Info info() {
        return Info.of(1, 1, 2, 3, 4, DType.U8);
      }

      public Volume volume(int t, int c) {
        byte[] v = new byte[24];
        for (int i = 0; i < 24; i++) v[i] = (byte) (10 + i);
        return Volume.u8(2, 3, 4, v);
      }
    });
    b.exporter(vizi.plugin.javasdk.manifest.ExporterDecl.of("raw", ".raw"), req -> new Exporter.Export() {
      public void chunk(Exporter.Chunk c) {
        long sum = 0;
        for (int k = 0; k < c.channels().size(); k++) {
          Volume v = c.volume(k);
          for (long i = 0; i < v.samples(); i++) sum += v.u8(i);
        }
        EXPORTED.add(req.locator() + " t" + c.t() + " rank " + c.tRank() + " channels " + c.channels() + " sum " + sum);
      }

      public void finish() {
        EXPORTED.add("finished " + req.z() + "x" + req.y() + "x" + req.x());
      }
    });
    Overlay marks = b.overlay(OverlayDecl.drawn("marks", 2));
    b.chunkedOverlay(OverlayDecl.chunked("seg", OverlayDecl.TimeMode.PER_T), (ctx, want) -> {
      byte[] v = new byte[8];
      java.util.Arrays.fill(v, (byte) (1 + want.t().orElse(0)));
      return ChunkedOverlay.Chunk.of(Volume.u8(2, 2, 2, v), 1, 1, 1);
    });
    Panel p = b.panel(PanelDecl.of("p", "P", column("root",
        label("status", "idle"),
        image("img"),
        WidgetDecl.slider("gain", "set_gain").range(0, 10).continuous(true),
        button("snap", "snap", "Snap"),
        button("fetch", "fetch", "Fetch"))));
    Panel.Label status = p.label("status");
    p.number("gain", v -> v > 8 ? "too loud" : null).reportTo(status);
    b.command(CommandDecl.of("set_gain", "Gain").unlisted());
    Jobs jobs = b.jobs(o -> status.set(o.text()));
    b.command(CommandDecl.of("snap", "Snap"), ctx -> {
      p.image("img").push(PNG);
      status.set("snapped");
      return "snapped";
    });
    b.command(CommandDecl.of("fetch", "Fetch"), ctx -> jobs.start("fetch", j -> {
      Volume v = j.host().readVolume(0, 0).volume();
      long sum = 0;
      for (long i = 0; i < v.samples(); i++) sum += v.u8(i);
      return "sum " + sum;
    }));
    b.tool(ToolDecl.of("probe"), new Tool() {
      @Override
      public void press(ToolContext ctx, InputEvent.Press ev) {
        ev.info().voxel().ifPresent(at -> marks.publishLive(ev.info(), List.of(Item.markers("m", at.z(), at.y(), at.x()))));
      }

      @Override
      public void release(ToolContext ctx, InputEvent.Release ev) {
        ev.info().voxel().ifPresent(at -> marks.setItems(List.of(Item.markers("m", at.z(), at.y(), at.x()))));
      }
    });
    return b.build();
  }

  private void grant(HostSegment h, int perms, InstallSegment install, boolean capabilityFirst) {
    if (capabilityFirst) h.sendGrant(host.hostDoorbell, recordIds.incrementAndGet(), perms);
    host.send(HostMsg.newBuilder().setInstallSegment(install).build());
    if (!capabilityFirst) h.sendGrant(host.hostDoorbell, recordIds.incrementAndGet(), perms);
  }

  private PluginMsg acked(HostSegment h) throws InterruptedException {
    PluginMsg ack = host.expect(m -> m.hasGrantAck() && m.getGrantAck().getSegmentId() == h.id, T);
    assertTrue(ack.getGrantAck().hasInstalled(), "segment " + h.id + ": " + ack);
    assertArrayEquals(h.proof, ack.getGrantAck().getInstalled().getIdentityProof().toByteArray());
    return ack;
  }

  private PluginMsg command(String id, Value value) throws InterruptedException {
    String rid = host.nextId();
    InvokeCommand.Builder ic = InvokeCommand.newBuilder().setCommandId(id);
    if (value != null) ic.putArgs("value", value);
    host.send(HostMsg.newBuilder().setId(rid).setInvokeCommand(ic).build());
    return host.reply(rid, T);
  }

  private void release(long segment, int region, SegmentRef ref) {
    host.send(HostMsg.newBuilder().setReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(segment).setRegion(region)
        .setOffset(ref.getOffset()).setLen(ref.getLen())).build());
  }

  /** The panel as the host would hold it: the last op of each kind per widget, from every patch seen. */
  final java.util.Map<String, vizi.plugin.v1.ViziPlugin.PatchOp> panel = new java.util.HashMap<>();

  /**
   * Waits until the panel's accumulated state satisfies {@code ok}. Patches coalesce and batch
   * several widgets, so a test asserts the state they produce, never that one push arrived alone.
   */
  private vizi.plugin.v1.ViziPlugin.PatchOp awaitPanel(String widget, vizi.plugin.v1.ViziPlugin.PatchOp.OpCase kind,
      java.util.function.Predicate<vizi.plugin.v1.ViziPlugin.PatchOp> ok) throws InterruptedException {
    long end = System.nanoTime() + T.toNanos();
    while (true) {
      var op = panel.get(widget + "/" + kind);
      if (op != null && ok.test(op)) return op;
      long left = end - System.nanoTime();
      if (left <= 0) throw new AssertionError(widget + "/" + kind + " never satisfied; the panel holds " + panel);
      PluginMsg m = host.expect(PluginMsg::hasPanelPatch, Duration.ofNanos(left));
      for (var o : m.getPanelPatch().getOpsList()) panel.put(o.getWidgetId() + "/" + o.getOpCase(), o);
    }
  }


  @Test
  void theAuthorApiOverRealTransports() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().runtime()).open();
    host.hello();

    try (HostSegment g = HostSegment.create(0x1_0000_0001L, 8192);
        HostSegment o = HostSegment.create(0x1_0000_0002L, 4 << 20);
        HostSegment pp = HostSegment.create(0x1_0000_0003L, 1 << 16);
        HostSegment v = HostSegment.create(0x1_0000_0004L, 8192);
        HostSegment seg = HostSegment.create(0x1_0000_0005L, 1 << 16)) {

      // ---- grants, in both orders ----
      long laneOffset = 64 + (1 << 20);
      grant(o, HostSegment.READ_WRITE, o.install("O", "marks", List.of(
          HostSegment.loaned(0, 64, 1 << 20, RegionProducer.REGION_PRODUCER_PLUGIN),
          HostSegment.pulsed(1, laneOffset, 3, 16384, RegionProducer.REGION_PRODUCER_PLUGIN, true))), true);
      acked(o);
      grant(pp, HostSegment.READ_WRITE, pp.install("P", "p", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), false);
      acked(pp);

      // ---- a reader, through G ----
      String open = host.nextId();
      host.send(HostMsg.newBuilder().setId(open).setOpenReader(OpenReader.newBuilder().setUri("/d/a.vol").setReaderId("vol")).build());
      var opened = host.reply(open, T).getReaderOpened();
      assertEquals(List.of(1, 1, 2, 3, 4), opened.getShapeTczyxList());
      grant(g, HostSegment.READ_WRITE, g.install("G", "", List.of(
          HostSegment.loaned(0, 64, 8192 - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      acked(g);
      String read = host.nextId();
      host.send(HostMsg.newBuilder().setId(read).setReadVolume(ReadVolume.newBuilder().setHandle(opened.getHandle())).build());
      SegmentRef ref = host.reply(read, T).getSegment();
      assertEquals(24, ref.getLen());
      assertEquals(10 + 7, g.view.get(ValueLayout.JAVA_BYTE, ref.getOffset() + 7));
      release(g.id, 0, ref);

      // ---- a command pushing a panel image through P ----
      host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
      PluginMsg done = command("snap", null);
      assertTrue(done.getCommandDone().getOk());
      assertEquals("snapped", done.getCommandDone().getMessage());
      var ir = awaitPanel("img", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_IMAGE, op -> true).getSetImage();
      assertEquals(pp.id, ir.getSegmentId());
      assertArrayEquals(PNG, pp.view.asSlice(ir.getOffset(), ir.getLen()).toArray(ValueLayout.JAVA_BYTE));
      host.send(HostMsg.newBuilder().setReleaseLoan(ReleaseLoan.newBuilder().setSegmentId(pp.id).setRegion(0)
          .setOffset(ir.getOffset()).setLen(ir.getLen())).build());

      // ---- a widget the SDK answers ----
      assertTrue(command("set_gain", Value.newBuilder().setF(2).build()).getCommandDone().getOk());
      PluginMsg refused = command("set_gain", Value.newBuilder().setF(9).build());
      assertFalse(refused.getCommandDone().getOk());
      assertEquals("too loud", refused.getCommandDone().getMessage());
      awaitPanel("gain", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getF() == 2.0);
      awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("too loud"));

      // ---- arrangements, then a tool gesture: live lane + D, then a loan ----
      Geometry geom = Geometry.newBuilder().setGen(1).addLevels(LevelScale.newBuilder().setZ(1).setY(1).setX(1))
          .setCrop(EdgeBox.newBuilder().setZ0(-0.5).setZ1(9.5).setY0(-0.5).setY1(9.5).setX0(-0.5).setX1(9.5)).build();
      host.send(HostMsg.newBuilder().setTimeMapping(TimeMapping.newBuilder().setGen(1).setRevision(1).addT(0)).build());
      host.expect(PluginMsg::hasTimeMappingAck, T);
      host.send(HostMsg.newBuilder().setEpochChange(EpochChange.newBuilder()
          .addDatasets(DatasetState.newBuilder().setGen(1).setGeometry(geom).setTimeMappingRevision(1))
          .addPanes(PaneEpoch.newBuilder().setViewId(0).setEpoch(1).setGen(1))).build());
      host.expect(PluginMsg::hasEpochAck, T);
      host.send(HostMsg.newBuilder().setToolBinding(ToolBinding.newBuilder().setTool(1).setToolId("probe").setRevision(1)).build());
      host.expect(PluginMsg::hasToolBindingAck, T);

      host.sendRecord(InputRecord.builder().phase(InputRecord.PRESS).interaction(1).voxel(1, 2, 3).build().encode(recordIds.incrementAndGet()));
      Posix.Received d = host.receiveRecord(T);
      assertNotNull(d, "no D after a live publish");
      assertEquals(Records.OP_D, Records.op(d.data()));
      RegionTable.Spec laneSpec = host.serving.session().store().installed(o.id).orElseThrow().regions().get(1);
      PulsedLane lane = PulsedLane.over(o.view, o.id, laneSpec, (site, detail) -> {});
      PulsedLane.Value live = lane.read();
      assertNotNull(live, "the lane holds no value");
      assertEquals(0, live.viewId().orElseThrow());
      assertEquals(1, live.epoch().orElseThrow());
      OverlayUpdate lu = OverlayUpdate.parseFrom(live.payload());
      MemorySegment inline = MemorySegment.ofArray(lu.getItems(0).getMarker().getPositionsZyx().getInline().toByteArray());
      assertEquals(3.0, inline.get(F64, 16));

      host.sendRecord(InputRecord.builder().phase(InputRecord.RELEASE).interaction(1).seq(1).voxel(1, 2, 4).build().encode(recordIds.incrementAndGet()));
      PluginMsg pushed = host.expect(PluginMsg::hasOverlayUpdate, T);
      assertTrue(pushed.getOverlayUpdate().getRevision() > lu.getRevision(), "one revision counter across both lanes");
      SegmentRef loan = pushed.getOverlayUpdate().getItems(0).getMarker().getPositionsZyx().getSegment();
      assertEquals(o.id, loan.getSegmentId());
      assertEquals(4.0, o.view.get(F64, loan.getOffset() + 16));
      release(o.id, 0, loan);

      // ---- a job's host request, answered by a deposit ----
      assertEquals("fetch", command("fetch", null).getCommandDone().getMessage());
      PluginMsg ask = host.expect(PluginMsg::hasReadDataVolume, T);
      grant(v, HostSegment.READ_ONLY, v.install("V", "", List.of(
          HostSegment.loaned(0, 64, 8192 - 64, RegionProducer.REGION_PRODUCER_HOST))), false);
      acked(v);
      for (int i = 0; i < 24; i++) v.view.set(ValueLayout.JAVA_BYTE, 64 + i, (byte) 2);
      SegmentRef dep = SegmentRef.newBuilder().setSegmentId(v.id).setOffset(64).setLen(24).addShapeZyx(2).addShapeZyx(3).addShapeZyx(4)
          .setDtype(vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8).build();
      host.send(HostMsg.newBuilder().setId(ask.getId()).setDataVolume(DataVolume.newBuilder().setSegment(dep)).build());
      var released = host.expect(PluginMsg::hasReleaseDeposit, T).getReleaseDeposit();
      assertEquals(v.id, released.getSegmentId());
      assertEquals(24, released.getLen());
      awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("sum 48"));

      // ---- a chunked overlay: wanted, served into its arena ----
      grant(seg, HostSegment.READ_WRITE, seg.install("O", "seg", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      acked(seg);
      host.send(HostMsg.newBuilder().setChunksWanted(ChunksWanted.newBuilder().setOverlayId("seg")
          .addWants(ChunkWant.newBuilder().setLevel(0).setT(2))).build());
      var chunk = host.expect(PluginMsg::hasOverlayChunk, T).getOverlayChunk();
      assertEquals(2, chunk.getT());
      assertEquals(List.of(1, 1, 1), chunk.getPositionZyxList());
      SegmentRef cl = chunk.getLabels().getSegment();
      assertEquals(3, seg.view.get(ValueLayout.JAVA_BYTE, cl.getOffset() + 7));
      release(seg.id, 0, cl);

      // ---- an export: chunks in, the artifact out ----
      String ox = host.nextId();
      host.send(HostMsg.newBuilder().setId(ox).setOpenExport(vizi.plugin.v1.ViziPlugin.OpenExport.newBuilder().setExporterId("raw")
          .setLocator("/out/a.raw").addShapeTczyx(1).addShapeTczyx(1).addShapeTczyx(2).addShapeTczyx(3).addShapeTczyx(4)
          .setDtype(vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8).setLittleEndian(true)).build());
      String handle = host.reply(ox, T).getExportOpened().getHandle();
      byte[] slab = new byte[24];
      java.util.Arrays.fill(slab, (byte) 3);
      String xc = host.nextId();
      host.send(HostMsg.newBuilder().setId(xc).setExportChunk(vizi.plugin.v1.ViziPlugin.ExportChunk.newBuilder().setHandle(handle)
          .setT(0).addChannels(0).setInline(com.google.protobuf.ByteString.copyFrom(slab))).build());
      assertEquals(24, host.reply(xc, T).getChunkWritten().getTotalBytes());
      String fx = host.nextId();
      host.send(HostMsg.newBuilder().setId(fx).setFinishExport(vizi.plugin.v1.ViziPlugin.FinishExport.newBuilder().setHandle(handle)).build());
      assertTrue(host.reply(fx, T).hasExportDone());
      assertEquals(List.of("/out/a.raw t0 rank 0 channels [0] sum 72", "finished 2x3x4"), List.copyOf(EXPORTED));

      // ---- the shutdown window ----
      host.send(HostMsg.newBuilder().setShutdown(Shutdown.newBuilder().setReason(ShutdownReason.SHUTDOWN_REASON_RESTART).setDeadlineMs(3000)).build());
      assertEquals(ShutdownReport.Outcome.COMPLETED, host.expect(PluginMsg::hasShutdownReport, T).getShutdownReport().getOutcome());
      assertTrue(host.closed().get(5, TimeUnit.SECONDS).isOk());
      assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
          .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
    }
  }

  // ---- subscribed pushes, region identity, a colormap, a save dialog; held patches ----

  static Plugin subscriptionsPlugin() {
    var b = Plugin.builder("org.test.subscriptions", "Subscriptions", "0.1").entry("./run").permissions(Permission.DATA_ACCESS);
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", label("status", "idle"), button("cmap", "cmap", "C"),
        button("save", "save", "S"), button("insave", "insave", "B"),
        WidgetDecl.list("cells", label("name")).selectable("pick"), button("fill", "fill", "F"))));
    Panel.Rows cells = p.rows("cells");
    b.command(CommandDecl.of("pick", "Pick"));
    b.command(CommandDecl.of("fill", "F"), ctx -> {
      cells.set(List.of("a", "b"));
      cells.row("a").set("name", "Alpha");
      cells.row("b").set("name", "Beta");
      return "";
    });
    Panel.Label status = p.label("status");
    cells.onSelect(key -> status.set("picked " + key));
    Jobs jobs = b.jobs(o -> status.set(o.text()));
    b.onViewChanged((ctx, v) -> status.set("view gen " + v.openGeneration() + " t " + v.t() + " ch " + v.shownChannel()
        + " sel " + v.selectedRegionId()));
    b.onRegionsChanged((ctx, r) -> {
      var ids = r.changes().stream().filter(c -> !c.deleted()).map(Subscriptions.RegionChange::regionId).toList();
      var rois = ctx.host().rois(ids);
      StringBuilder sb = new StringBuilder(r.reset() ? "reset" : "delta");
      for (var rec : rois.getRoisList()) sb.append(' ').append(rec.getRegionId()).append('@').append(rec.getRevision()).append('#').append(rec.getContentHash());
      status.set(sb.toString());
    });
    b.command(CommandDecl.of("cmap", "C"), ctx -> jobs.start("cmap", j -> "cmap " + j.host().colormap("gray").length));
    b.command(CommandDecl.of("save", "S"), ctx -> jobs.start("save",
        j -> j.host().requestSavePath("out.csv", "csv", "Save").map(pa -> "save " + pa).orElse("declined")));
    // a picker from a command is sent, and the command waits for the person
    b.command(CommandDecl.of("insave", "B"), ctx -> ctx.host().requestSavePath("x", "", "").map(pa -> "in " + pa).orElse("declined"));
    return b.build();
  }

  @Test
  void subscriptionsPickersAndListsOverRealTransports() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(subscriptionsPlugin().application()).open();
    host.hello();

    // a view push before the panel is open: the handler sets the label, and it is sent at once (round
    // 57: the host keeps a closed panel's state); the open re-sends it
    host.send(HostMsg.newBuilder().setViewChanged(vizi.plugin.v1.ViziPlugin.ViewChanged.newBuilder().setOpenGeneration(5).setT(3)
        .addVisibleChannels(1).addVisibleChannels(2).setSelectedRegionId("r1")).build());
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE,
        op -> op.getSetValue().getS().equals("view gen 5 t 3 ch 1 sel r1"));
    host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE,
        op -> op.getSetValue().getS().equals("view gen 5 t 3 ch 1 sel r1"));

    // a regions push: the handler re-reads just the regions that changed
    host.send(HostMsg.newBuilder().setRegionsChanged(vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(5).setReset(true)
        .addChanges(vizi.plugin.v1.ViziPlugin.RegionChange.newBuilder().setRegionId("r1").setRevision(2).setOrigin("user"))
        .addChanges(vizi.plugin.v1.ViziPlugin.RegionChange.newBuilder().setRegionId("gone").setDeleted(true))).build());
    PluginMsg get = host.expect(PluginMsg::hasGetRois, T);
    assertEquals(List.of("r1"), get.getGetRois().getRegionIdsList());
    host.send(HostMsg.newBuilder().setId(get.getId()).setRois(vizi.plugin.v1.ViziPlugin.RoiList.newBuilder()
        .addRois(vizi.plugin.v1.ViziPlugin.RoiRecord.newBuilder().setRegionId("r1").setRevision(2).setContentHash("00ff00ff00ff00ff"))).build());
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("reset r1@2#00ff00ff00ff00ff"));

    // a colormap, from a job
    assertEquals("cmap", command("cmap", null).getCommandDone().getMessage());
    PluginMsg cm = host.expect(PluginMsg::hasGetColormap, T);
    assertEquals("gray", cm.getGetColormap().getName());
    host.send(HostMsg.newBuilder().setId(cm.getId()).setColormapTable(vizi.plugin.v1.ViziPlugin.ColormapTable.newBuilder().setName("gray")
        .setRgba(com.google.protobuf.ByteString.copyFrom(new byte[1024]))).build());
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("cmap 1024"));

    // a save dialog, from a job, and from a command (never refused)
    assertEquals("save", command("save", null).getCommandDone().getMessage());
    PluginMsg sp = host.expect(PluginMsg::hasRequestSavePath, T);
    assertEquals("out.csv", sp.getRequestSavePath().getSuggestedName());
    assertEquals("csv", sp.getRequestSavePath().getExtension());
    host.send(HostMsg.newBuilder().setId(sp.getId()).setSavePathResult(vizi.plugin.v1.ViziPlugin.SavePathResult.newBuilder()
        .setPath("/data/out.csv")).build());
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("save /data/out.csv"));
    String inId = host.nextId();
    host.send(HostMsg.newBuilder().setId(inId).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("insave")).build());
    PluginMsg inSp = host.expect(PluginMsg::hasRequestSavePath, T);
    assertEquals("x", inSp.getRequestSavePath().getSuggestedName());
    host.send(HostMsg.newBuilder().setId(inSp.getId()).setSavePathResult(vizi.plugin.v1.ViziPlugin.SavePathResult.newBuilder()
        .setPath("/data/x")).build());
    PluginMsg in = host.reply(inId, T);
    assertTrue(in.getCommandDone().getOk(), in.toString());
    assertEquals("in /data/x", in.getCommandDone().getMessage());

    // a list: the rows, then each row's cell, both rows arriving; a selection from the host
    assertTrue(command("fill", null).getCommandDone().getOk());
    java.util.List<vizi.plugin.v1.ViziPlugin.PatchOp> seenOps = new java.util.ArrayList<>();
    long end = System.nanoTime() + T.toNanos();
    while (seenOps.stream().filter(o -> o.getWidgetId().equals("name")).count() < 2 && System.nanoTime() < end) {
      seenOps.addAll(host.expect(PluginMsg::hasPanelPatch, T).getPanelPatch().getOpsList());
    }
    int rowsAt = -1;
    for (int i = 0; i < seenOps.size(); i++) if (seenOps.get(i).hasSetRows()) rowsAt = i;
    assertTrue(rowsAt >= 0, "set_rows sent: " + seenOps);
    assertEquals(List.of("a", "b"), seenOps.get(rowsAt).getSetRows().getKeysList());
    var names = seenOps.stream().filter(o -> o.getWidgetId().equals("name")).toList();
    assertEquals(List.of("a:Alpha", "b:Beta"), names.stream().map(o -> o.getRowKey() + ":" + o.getSetValue().getS()).toList());
    assertTrue(seenOps.indexOf(names.get(0)) > rowsAt, "the rows go before their cells");
    String pick = host.nextId();
    host.send(HostMsg.newBuilder().setId(pick).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("pick")
        .putArgs("row", Value.newBuilder().setS("b").build())).build());
    assertTrue(host.reply(pick, T).getCommandDone().getOk());
    for (var o : seenOps) panel.put(o.getWidgetId() + "/" + o.getOpCase(), o);
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("picked b"));

    assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
        .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
  }

  static Plugin setViewPlugin(java.util.concurrent.BlockingQueue<Object> toolResults) {
    var b = Plugin.builder("org.test.setview", "SetView", "0.1").entry("./run");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", label("status", "idle"),
            image("trace").clickable("click").movesView(true),
            WidgetDecl.slider("frame", "frame").range(0, 100).movesView(true),
            button("silent", "silent", "S").movesView(true),
            button("plain", "plain", "P"),
            WidgetDecl.roiPicker("roi", "roi").followsSelection(true).filter(WidgetDecl.RoiFilter.MINE),
            WidgetDecl.list("cells", label("n")).selectable("rowpick").movesView(true)))
        .withViewTargets(vizi.plugin.javasdk.manifest.ViewTarget.T, vizi.plugin.javasdk.manifest.ViewTarget.SELECT_REGION));
    b.command(CommandDecl.of("click", "Click"), ctx -> {
      var c = ctx.imageClick().orElseThrow();
      ViewResult r = c.x() < 10 ? ctx.declineView() : ctx.moveView(ViewMove.to().t(c.x()));
      return c.button() + "/" + c.modifiers() + "/" + c.image() + " " + r;
    });
    b.command(CommandDecl.of("frame", "Frame"));
    Panel.Whole frame = p.integer("frame");
    Panel.Label status = p.label("status");
    frame.onCause(c -> status.set("frame " + c.move(ViewMove.to().t((int) frame.get()))));
    b.command(CommandDecl.of("silent", "Silent"), ctx -> "");
    b.command(CommandDecl.of("plain", "Plain"), ctx -> ctx.moveView(ViewMove.to().t(1)).toString());
    b.command(CommandDecl.of("roi", "Roi"));
    p.picked("roi");
    b.command(CommandDecl.of("rowpick", "Row"));
    Panel.Rows cells = p.rows("cells");
    cells.set(List.of("a", "b"));
    cells.onCause(c -> status.set("row " + c.row().orElse("?") + " " + c.commandId() + " " + c.decline()));
    b.tool(ToolDecl.of("jump").withMode(ToolDecl.Mode.FIRE).withViewTargets(vizi.plugin.javasdk.manifest.ViewTarget.T), new Tool() {
      @Override
      public void press(ToolContext ctx, InputEvent.Press ev) {
        try {
          ctx.moveView(ev, ViewMove.to().t(1));
          toolResults.add("a press moved");
        } catch (IllegalArgumentException e) {
          toolResults.add(e);
        }
      }

      @Override
      public void fire(ToolContext ctx, InputEvent.Fire ev) {
        ctx.moveView(ev, ViewMove.to().t(9)).whenComplete((r, t) -> toolResults.add(t != null ? t : r));
      }
    });
    return b.build();
  }

  @Test
  void setViewOverRealTransports() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var toolResults = new java.util.concurrent.LinkedBlockingQueue<Object>();
    Plugin plugin = setViewPlugin(toolResults);
    // the declarations, as written
    var panelDecl = plugin.manifest().contributions().panels().get(0);
    assertEquals(List.of(vizi.plugin.javasdk.manifest.ViewTarget.T, vizi.plugin.javasdk.manifest.ViewTarget.SELECT_REGION), panelDecl.viewTargets());
    assertTrue(panelDecl.widget("trace").orElseThrow().movesView());
    host = TestHost.start(plugin.application()).open();
    host.step("hello: Ready").hello();
    host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());

    // a click on the image, far enough right to move: the command's cause, answered APPLIED
    String click = host.nextId();
    host.send(HostMsg.newBuilder().setId(click).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("click").setCause(7)
        .putArgs("x", Value.newBuilder().setI(12).build()).putArgs("y", Value.newBuilder().setI(5).build())
        .putArgs("button", Value.newBuilder().setS("right").build()).putArgs("modifiers", Value.newBuilder().setS("shift").build())
        .putArgs("image", Value.newBuilder().setI(4096).build())).build());
    host.step("click: its ViewRequest");
    PluginMsg vr = host.expect(PluginMsg::hasViewRequest, T);
    assertEquals(7, vr.getViewRequest().getCause());
    assertEquals(12, vr.getViewRequest().getT());
    assertFalse(vr.getViewRequest().getDecline());
    host.send(HostMsg.newBuilder().setId(vr.getId()).setViewRequestResult(vizi.plugin.v1.ViziPlugin.ViewRequestResult.newBuilder()
        .setCause(7).setOutcome(vizi.plugin.v1.ViziPlugin.ViewOutcome.VIEW_OUTCOME_APPLIED).setDetail("t 12")).build());
    host.step("click: CommandDone");
    assertEquals("right/shift/4096 APPLIED: t 12", host.reply(click, T).getCommandDone().getMessage());

    // a moves_view slider through its typed handle: the value first, then the cause; UNSUPPORTED is an ordinary refusal
    String fr = host.nextId();
    host.send(HostMsg.newBuilder().setId(fr).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("frame").setCause(8)
        .putArgs("value", Value.newBuilder().setI(40).build())).build());
    host.step("frame slider: its ViewRequest");
    vr = host.expect(PluginMsg::hasViewRequest, T);
    assertEquals(8, vr.getViewRequest().getCause());
    assertEquals(40, vr.getViewRequest().getT());
    host.send(HostMsg.newBuilder().setId(vr.getId()).setViewRequestResult(vizi.plugin.v1.ViziPlugin.ViewRequestResult.newBuilder()
        .setCause(8).setOutcome(vizi.plugin.v1.ViziPlugin.ViewOutcome.VIEW_OUTCOME_REFUSED)
        .setReason(vizi.plugin.v1.ViziPlugin.ViewRefusal.VIEW_REFUSAL_NOT_ENABLED)).build());
    host.step("frame slider: CommandDone");
    assertTrue(host.reply(fr, T).getCommandDone().getOk());
    host.step("frame slider: status 'frame REFUSED NOT_ENABLED'");
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("frame REFUSED NOT_ENABLED"));

    // a list row's click: the cause listener reads the row from the cause itself
    String rowpick = host.nextId();
    host.send(HostMsg.newBuilder().setId(rowpick).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("rowpick").setCause(10)
        .putArgs("row", Value.newBuilder().setS("b").build())).build());
    host.step("row click: its ViewRequest (a decline)");
    vr = host.expect(PluginMsg::hasViewRequest, T);
    assertEquals(10, vr.getViewRequest().getCause());
    assertTrue(vr.getViewRequest().getDecline());
    host.send(HostMsg.newBuilder().setId(vr.getId()).setViewRequestResult(vizi.plugin.v1.ViziPlugin.ViewRequestResult.newBuilder()
        .setCause(10).setOutcome(vizi.plugin.v1.ViziPlugin.ViewOutcome.VIEW_OUTCOME_DECLINED)).build());
    host.step("row click: CommandDone");
    assertTrue(host.reply(rowpick, T).getCommandDone().getOk());
    host.step("row click: status 'row b rowpick DECLINED'");
    awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, op -> op.getSetValue().getS().equals("row b rowpick DECLINED"));

    // a cause the handler leaves unanswered: the SDK declines it, before CommandDone, and says so in the log
    java.io.PrintStream err = System.err;
    java.io.ByteArrayOutputStream logged = new java.io.ByteArrayOutputStream();
    System.setErr(new java.io.PrintStream(logged, true, java.nio.charset.StandardCharsets.UTF_8));
    String silent = host.nextId();
    try {
      host.send(HostMsg.newBuilder().setId(silent).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("silent").setCause(9)).build());
      host.step("silent: the SDK's decline");
      vr = host.expect(PluginMsg::hasViewRequest, T);
      assertEquals(9, vr.getViewRequest().getCause());
      assertTrue(vr.getViewRequest().getDecline());
      host.step("silent: CommandDone");
      assertTrue(host.reply(silent, T).getCommandDone().getOk());
    } finally {
      System.setErr(err);
    }
    String log = logged.toString(java.nio.charset.StandardCharsets.UTF_8);
    assertTrue(log.contains("command silent returned without answering its moves_view cause 9"), log);
    host.send(HostMsg.newBuilder().setId(vr.getId()).setViewRequestResult(vizi.plugin.v1.ViziPlugin.ViewRequestResult.newBuilder()
        .setCause(9).setOutcome(vizi.plugin.v1.ViziPlugin.ViewOutcome.VIEW_OUTCOME_DECLINED)).build());

    // no cause, no move: refused locally, and nothing is sent
    host.step("plain (no cause): CommandDone");
    PluginMsg plain = command("plain", null);
    assertFalse(plain.getCommandDone().getOk());
    assertTrue(plain.getCommandDone().getMessage().contains("cause"), plain.getCommandDone().getMessage());

    // a tool: a press is never a cause (refused locally); a FIRE's (tool, interaction) is
    Geometry geom = Geometry.newBuilder().setGen(1).addLevels(LevelScale.newBuilder().setZ(1).setY(1).setX(1))
        .setCrop(EdgeBox.newBuilder().setZ0(-0.5).setZ1(9.5).setY0(-0.5).setY1(9.5).setX0(-0.5).setX1(9.5)).build();
    host.send(HostMsg.newBuilder().setTimeMapping(TimeMapping.newBuilder().setGen(1).setRevision(1).addT(0)).build());
    host.step("tool: TimeMappingAck").expect(PluginMsg::hasTimeMappingAck, T);
    host.send(HostMsg.newBuilder().setEpochChange(EpochChange.newBuilder()
        .addDatasets(DatasetState.newBuilder().setGen(1).setGeometry(geom).setTimeMappingRevision(1))
        .addPanes(PaneEpoch.newBuilder().setViewId(0).setEpoch(1).setGen(1))).build());
    host.step("tool: EpochAck").expect(PluginMsg::hasEpochAck, T);
    host.send(HostMsg.newBuilder().setToolBinding(ToolBinding.newBuilder().setTool(2).setToolId("jump").setRevision(1)
        .setMode(vizi.plugin.v1.ViziPlugin.ToolMode.TOOL_MODE_FIRE)).build());
    host.step("tool: ToolBindingAck").expect(PluginMsg::hasToolBindingAck, T);
    host.sendRecord(InputRecord.builder().phase(InputRecord.PRESS).tool(2).interaction(4).epoch(1).gen(1).voxel(1, 2, 3).build()
        .encode(recordIds.incrementAndGet()));
    host.sendRecord(InputRecord.builder().phase(InputRecord.FIRE).tool(2).interaction(5).epoch(1).gen(1).voxel(1, 2, 3).build().encode(recordIds.incrementAndGet()));
    host.step("tool: the FIRE's ViewRequest");
    vr = host.expect(m -> m.hasViewRequest() || m.hasContractViolation(), T);
    assertFalse(vr.hasContractViolation(), "" + vr);
    assertEquals(2, vr.getViewRequest().getToolCause().getTool());
    assertEquals(5, vr.getViewRequest().getToolCause().getInteraction());
    assertEquals(0, vr.getViewRequest().getCause());
    assertEquals(9, vr.getViewRequest().getT());
    host.send(HostMsg.newBuilder().setId(vr.getId()).setViewRequestResult(vizi.plugin.v1.ViziPlugin.ViewRequestResult.newBuilder()
        .setToolCause(vr.getViewRequest().getToolCause()).setOutcome(vizi.plugin.v1.ViziPlugin.ViewOutcome.VIEW_OUTCOME_APPLIED)).build());
    Object pressed = toolResults.poll(T.toMillis(), TimeUnit.MILLISECONDS);
    assertTrue(pressed instanceof IllegalArgumentException e && e.getMessage().contains("only a FIRE"), "" + pressed);
    Object got = toolResults.poll(T.toMillis(), TimeUnit.MILLISECONDS);
    assertTrue(got instanceof ViewResult r && r.applied(), "" + got);

    assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
        .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
  }

  static Plugin followPlugin(java.util.concurrent.BlockingQueue<String> notes) {
    var b = Plugin.builder("org.test.follow", "Follow", "0.1").entry("./run");
    Panel p = b.panel(PanelDecl.of("p", "P", label("status", "idle"))
        .withFollowTargets(vizi.plugin.javasdk.manifest.ViewTarget.T, vizi.plugin.javasdk.manifest.ViewTarget.FRAME_REGION));
    b.panel(PanelDecl.of("q", "Q", label("q", "")).withFollowTargets(vizi.plugin.javasdk.manifest.ViewTarget.T)); // no driver
    b.command(CommandDecl.of("offer", "Offer"), ctx -> {
      p.offerFollow("Tour 3 frames");
      return "";
    });
    b.command(CommandDecl.of("withdraw", "Withdraw"), ctx -> {
      p.withdrawFollow();
      return "";
    });
    b.follow("p", (ctx, f) -> {
      if (!f.resumed()) {
        try {
          f.move(ViewMove.to().z(1));
          notes.add("z was sent");
        } catch (IllegalArgumentException e) {
          notes.add("refused z");
        }
        for (int t = 0; t < 3; t++) {
          if (!f.move(ViewMove.to().t(t).frameRegion("r" + t)) || !f.waitFor(Duration.ofMillis(150))) return;
        }
        f.end();
        notes.add("toured; move after end is " + f.move(ViewMove.to().t(0)));
      } else {
        f.move(ViewMove.to().t(2));
        boolean dwelt = f.waitFor(Duration.ofSeconds(20));
        notes.add("resumed dwelt " + dwelt + " " + f.reason().map(Enum::name).orElse("?") + " " + f.detail());
      }
    });
    return b.build();
  }

  @Test
  void followOverRealTransports() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var notes = new java.util.concurrent.LinkedBlockingQueue<String>();
    Plugin plugin = followPlugin(notes);
    assertEquals(List.of(vizi.plugin.javasdk.manifest.ViewTarget.T, vizi.plugin.javasdk.manifest.ViewTarget.FRAME_REGION),
        plugin.manifest().contributions().panels().get(0).followTargets());
    host = TestHost.start(plugin.application()).open();
    host.hello();

    // an offer is panel state: sent at once, before the panel opens, and again at every open
    assertTrue(command("offer", null).getCommandDone().getOk());
    var offer = host.expect(PluginMsg::hasOfferFollow, T).getOfferFollow();
    assertEquals("p", offer.getPanelId());
    assertEquals("Tour 3 frames", offer.getLabel());
    host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
    assertEquals("Tour 3 frames", host.expect(PluginMsg::hasOfferFollow, T).getOfferFollow().getLabel());

    // the person presses Follow: states with a rising seq, an undeclared target refused locally, then EndFollow
    host.send(HostMsg.newBuilder().setFollowStarted(vizi.plugin.v1.ViziPlugin.FollowStarted.newBuilder().setFollowId(5)
        .setPanelId("p").addTargets("t").addTargets("frame_region")).build());
    long lastSeq = 0;
    java.util.Set<Integer> ts = new java.util.TreeSet<>();
    while (true) {
      PluginMsg m = host.expect(x -> x.hasFollowState() || x.hasEndFollow(), T);
      if (m.hasEndFollow()) {
        assertEquals(5, m.getEndFollow().getFollowId());
        break;
      }
      var st = m.getFollowState();
      assertEquals(5, st.getFollowId());
      assertTrue(st.getSeq() > lastSeq, "seq rises: " + st.getSeq() + " after " + lastSeq);
      lastSeq = st.getSeq();
      assertEquals("r" + st.getT(), st.getFrameRegion());
      ts.add(st.getT());
    }
    assertEquals(java.util.Set.of(0, 1, 2), ts, "each state, 150 ms apart, reaches the host");
    assertEquals("refused z", notes.poll(T.toMillis(), TimeUnit.MILLISECONDS));
    assertEquals("toured; move after end is false", notes.poll(T.toMillis(), TimeUnit.MILLISECONDS));
    host.send(HostMsg.newBuilder().setFollowEnded(vizi.plugin.v1.ViziPlugin.FollowEnded.newBuilder().setFollowId(5)
        .setReason(vizi.plugin.v1.ViziPlugin.FollowEndReason.FOLLOW_END_PLUGIN_ENDED)).build());

    // Resume: the host ends it (the person took control); the driver hears it at once and sends no EndFollow
    host.send(HostMsg.newBuilder().setFollowStarted(vizi.plugin.v1.ViziPlugin.FollowStarted.newBuilder().setFollowId(6)
        .setPanelId("p").addTargets("t").addTargets("frame_region").setResumed(true)).build());
    assertEquals(2, host.expect(PluginMsg::hasFollowState, T).getFollowState().getT());
    host.send(HostMsg.newBuilder().setFollowEnded(vizi.plugin.v1.ViziPlugin.FollowEnded.newBuilder().setFollowId(6)
        .setReason(vizi.plugin.v1.ViziPlugin.FollowEndReason.FOLLOW_END_USER_TOOK_CONTROL).setDetail("t scrubbed")).build());
    assertEquals("resumed dwelt false FOLLOW_END_USER_TOOK_CONTROL t scrubbed", notes.poll(T.toMillis(), TimeUnit.MILLISECONDS));
    assertEquals(0, host.quiet(Duration.ofMillis(300)).stream().filter(x -> x.hasEndFollow() && x.getEndFollow().getFollowId() == 6).count(),
        "a Follow the host ended is not ended again");

    // a Follow started on a panel with no driver is ended at once
    host.send(HostMsg.newBuilder().setFollowStarted(vizi.plugin.v1.ViziPlugin.FollowStarted.newBuilder().setFollowId(7)
        .setPanelId("q").addTargets("t")).build());
    assertEquals(7, host.expect(PluginMsg::hasEndFollow, T).getEndFollow().getFollowId());

    // withdrawing is an offer with an empty label
    assertTrue(command("withdraw", null).getCommandDone().getOk());
    offer = host.expect(PluginMsg::hasOfferFollow, T).getOfferFollow();
    assertEquals("", offer.getLabel());

    assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
        .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
  }

  /** A wait that starves says where: the step, what was taken last, how long it has been quiet, and the threads. */
  @Test
  void aStarvedWaitNamesItsStep() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.quiet", "Quiet", "0.1").entry("./run");
    b.command(CommandDecl.of("c", "C"), ctx -> "");
    host = TestHost.start(b.build().application()).open();
    host.step("hello: Ready").hello();
    AssertionError e = assertThrows(AssertionError.class,
        () -> host.step("a message that never comes").expect(m -> false, Duration.ofMillis(200)));
    String msg = e.getMessage();
    assertTrue(msg.contains("at step 'a message that never comes'"), msg);
    assertTrue(msg.contains("last taken [[READY"), msg);
    assertTrue(msg.contains("ms ago") && msg.contains("threads:") && msg.contains("vizi-"), msg);
  }

  /**
   * A race: a PanelOpened re-push on the command worker, against a list
   * re-stated by another thread. Played as the host plays it: every cell op must name a row the
   * list holds at that point of the stream.
   */
  @Test
  void aRepushNeverSendsACellForARowAConcurrentSetDropped() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.race", "Race", "0.1").entry("./run");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", WidgetDecl.list("cells", label("n")), button("churn", "churn", "C"))));
    Panel.Rows cells = p.rows("cells");
    Jobs jobs = b.jobs(o -> {});
    var stop = new java.util.concurrent.atomic.AtomicBoolean();
    b.command(CommandDecl.of("churn", "Churn"), ctx -> jobs.start("churn", job -> {
      for (int i = 0; !stop.get(); i++) {
        List<String> keys = i % 2 == 0 ? List.of("a", "b") : List.of("a");
        cells.set(keys);
        for (String k : keys) cells.row(k).set("n", k + i);
      }
      return "";
    }));
    host = TestHost.start(b.build().application()).open();
    host.hello();
    host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
    assertTrue(command("churn", null).getCommandDone().getOk());
    for (int i = 0; i < 400; i++) host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
    // churn, still re-pushing, until enough cell ops have crossed the re-pushes, not for a fixed
    // time: under load a fixed time can carry fewer than the 100 the last assertion asks for
    long churnEnd = System.nanoTime() + T.toNanos();
    while (System.nanoTime() < churnEnd && host.seen().stream().filter(PluginMsg::hasPanelPatch)
        .mapToLong(m -> m.getPanelPatch().getOpsList().stream().filter(o -> !o.getRowKey().isEmpty()).count()).sum() < 200) {
      for (int i = 0; i < 20; i++) host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
      Thread.sleep(20);
    }
    stop.set(true);
    host.quiet(Duration.ofMillis(300));
    java.util.Set<String> held = new java.util.HashSet<>();
    int checked = 0;
    for (PluginMsg m : host.seen()) {
      if (!m.hasPanelPatch()) continue;
      for (var op : m.getPanelPatch().getOpsList()) {
        if (op.hasSetRows()) {
          held = new java.util.HashSet<>(op.getSetRows().getKeysList());
        } else if (!op.getRowKey().isEmpty()) {
          if (!held.contains(op.getRowKey())) {
            StringBuilder trail = new StringBuilder();
            int idx = host.seen().indexOf(m);
            int shown = 0;
            List<PluginMsg> prior = new java.util.ArrayList<>();
            for (int k = idx; k >= 0 && shown < 4; k--) if (host.seen().get(k).hasPanelPatch()) { prior.add(0, host.seen().get(k)); shown++; }
            for (var mm : prior) {
              trail.append("\n  patch:");
              for (var o : mm.getPanelPatch().getOpsList()) trail.append(o.hasSetRows() ? " rows" + o.getSetRows().getKeysList() : " " + o.getRowKey() + "=" + o.getSetValue().getS());
            }
            org.junit.jupiter.api.Assertions.fail("a cell op for row " + op.getRowKey() + " the list does not hold" + trail);
          }
          checked++;
        }
      }
    }
    assertTrue(checked > 100, "the race was exercised: " + checked + " cell ops");
  }

  /**
   * §3.3: once teardown begins the host answers no data request. A request
   * pending at Shutdown fails at once, so a hook that needs the lock its caller holds completes;
   * the push handler hears SessionEndedException, and nothing is reported as thrown.
   */
  @Test
  void aRequestPendingAtShutdownFailsAndTheHookCompletes() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var lock = new java.util.concurrent.locks.ReentrantLock();
    var b = Plugin.builder("org.test.sd", "SD", "0.1").entry("./run").permissions(Permission.DATA_ACCESS);
    b.panel(PanelDecl.of("p", "P", label("status", "")));
    b.onRegionsChanged((ctx, r) -> {
      lock.lock();
      try {
        ctx.host().rois(r.changes().stream().map(Subscriptions.RegionChange::regionId).toList()); // never answered
      } finally {
        lock.unlock();
      }
    });
    b.onShutdown((reason, detail) -> {
      lock.lock(); // the caller's lock: free only once the pending request has failed
      lock.unlock();
    });
    java.io.PrintStream err = System.err;
    java.io.ByteArrayOutputStream logged = new java.io.ByteArrayOutputStream();
    System.setErr(new java.io.PrintStream(logged, true, java.nio.charset.StandardCharsets.UTF_8));
    try {
      host = TestHost.start(b.build().application()).open();
      host.hello();
      host.send(HostMsg.newBuilder().setRegionsChanged(vizi.plugin.v1.ViziPlugin.RegionsChanged.newBuilder().setOpenGeneration(1).setReset(true)
          .addChanges(vizi.plugin.v1.ViziPlugin.RegionChange.newBuilder().setRegionId("r1").setRevision(1))).build());
      host.expect(PluginMsg::hasGetRois, T);
      long sent = System.nanoTime();
      host.send(HostMsg.newBuilder().setShutdown(Shutdown.newBuilder().setReason(ShutdownReason.SHUTDOWN_REASON_HOST_EXIT).setDeadlineMs(3000)).build());
      var report = host.expect(PluginMsg::hasShutdownReport, T).getShutdownReport();
      assertEquals(ShutdownReport.Outcome.COMPLETED, report.getOutcome(), report.toString());
      assertTrue(System.nanoTime() - sent < TimeUnit.SECONDS.toNanos(1), "the hook waited on the pending request");
      assertTrue(host.closed().get(5, TimeUnit.SECONDS).isOk());
    } finally {
      System.setErr(err);
    }
    String log = logged.toString(java.nio.charset.StandardCharsets.UTF_8);
    assertFalse(log.contains("threw"), log);
  }

  /**
   * A panel opened while a command runs is re-pushed at once, on the panel
   * worker, and its plugin hook runs on the command worker after the command, so hooks and commands
   * never overlap.
   */
  @Test
  void aPanelOpenedDuringALongCommandShowsAtOnceAndItsHookWaitsForTheCommand() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.busy", "Busy", "0.1").entry("./run");
    b.panel(PanelDecl.of("p", "P", column("root", label("status", "idle"), button("go", "go", "Go"))));
    var started = new java.util.concurrent.CountDownLatch(1);
    var release = new java.util.concurrent.CountDownLatch(1);
    var commandRunning = new java.util.concurrent.atomic.AtomicBoolean();
    var hookSawCommand = new java.util.concurrent.atomic.AtomicReference<Boolean>();
    b.command(CommandDecl.of("go", "Go"), ctx -> {
      commandRunning.set(true);
      started.countDown();
      try {
        release.await(10, java.util.concurrent.TimeUnit.SECONDS);
      } finally {
        commandRunning.set(false);
      }
      return "went";
    });
    b.onPanelOpened("p", ctx -> hookSawCommand.set(commandRunning.get()));
    host = TestHost.start(b.build().application()).open();
    try {
      host.hello();
      String go = host.nextId();
      host.send(HostMsg.newBuilder().setId(go).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("go")).build());
      assertTrue(started.await(5, java.util.concurrent.TimeUnit.SECONDS));
      host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
      awaitPanel("status", vizi.plugin.v1.ViziPlugin.PatchOp.OpCase.SET_VALUE, o -> o.getSetValue().getS().equals("idle"));
      assertTrue(commandRunning.get(), "the re-push did not wait for the command");
      assertNull(hookSawCommand.get(), "the hook waits for the command");
      release.countDown();
      assertEquals("went", host.reply(go, T).getCommandDone().getMessage());
      long end = System.nanoTime() + T.toNanos();
      while (hookSawCommand.get() == null && System.nanoTime() < end) Thread.sleep(5);
      assertEquals(Boolean.FALSE, hookSawCommand.get(), "the hook ran after the command, never beside it");
    } finally {
      release.countDown();
    }
  }

  /**
   * §8.4: images pushed while the panel has no P segment are held, then sent when P
   * installs, and again at open ahead of its hook, so what the hook pushes wins. The promise is per
   * (widget, row, property): a hook's op for the same widget supersedes the pending one or goes out
   * after it. Ops for different widgets may share one patch in either order (the hook's set can
   * merge into the re-push's pending entry for that widget), and nothing depends on that order.
   */
  @Test
  void anImagePushedBeforeThePanelHasItsSegmentIsHeldAndSentWhenItOpens() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.held", "Held", "0.1").entry("./run");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", label("status", ""), image("img"),
        WidgetDecl.list("cells", image("cell")), button("push", "push", "Push"))));
    Panel.Rows cells = p.rows("cells");
    b.command(CommandDecl.of("push", "Push"), ctx -> {
      cells.set(List.of("a", "b"));
      p.image("img").push(new byte[] {1, 2, 3});
      cells.row("a").image("cell", new byte[] {4});
      cells.row("b").image("cell", new byte[] {5}); // held, then its row goes
      cells.set(List.of("a"));
      return "";
    });
    var opens = new java.util.concurrent.atomic.AtomicInteger();
    b.onPanelOpened("p", ctx -> {
      int n = opens.incrementAndGet();
      p.label("status").set("hooked " + n);
      if (n == 2) p.image("img").push(new byte[] {7, 8}); // newer than the held image: it must win
    });
    host = TestHost.start(b.build().application()).open();
    host.hello();
    // opened before the host grants P: the push is held, and nothing fails
    host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
    PluginMsg done = command("push", null);
    assertTrue(done.getCommandDone().getOk(), done.getCommandDone().getMessage());
    assertEquals(0, host.quiet(Duration.ofMillis(300)).stream().filter(PluginMsg::hasPanelPatch)
        .flatMap(m -> m.getPanelPatch().getOpsList().stream()).filter(o -> o.hasSetImage()).count(), "no image without a segment");
    try (HostSegment pp = HostSegment.create(0x1_0000_0009L, 1 << 16)) {
      grant(pp, HostSegment.READ_WRITE, pp.install("P", "p", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), false);
      acked(pp);
      // the install alone sends the held images; the open below does not lend them again
      List<vizi.plugin.v1.ViziPlugin.PatchOp> ops = new java.util.ArrayList<>();
      long flushEnd = System.nanoTime() + T.toNanos();
      while (ops.stream().filter(o -> o.hasSetImage()).count() < 2 && System.nanoTime() < flushEnd) {
        ops.addAll(host.expect(PluginMsg::hasPanelPatch, T).getPanelPatch().getOpsList());
      }
      assertEquals(java.util.Set.of("img/", "cell/a"), ops.stream().filter(o -> o.hasSetImage())
          .map(o -> o.getWidgetId() + "/" + o.getRowKey()).collect(java.util.stream.Collectors.toSet()), "flushed at install: " + ops);
      host.send(HostMsg.newBuilder().setPanelOpened(PanelOpened.newBuilder().setPanelId("p")).build());
      java.util.function.Predicate<vizi.plugin.v1.ViziPlugin.PatchOp> hooked =
          o -> o.getWidgetId().equals("status") && o.getSetValue().getS().equals("hooked 2");
      java.util.function.Predicate<vizi.plugin.v1.ViziPlugin.PatchOp> hookImage =
          o -> o.getWidgetId().equals("img") && o.hasSetImage() && o.getSetImage().getLen() == 2;
      long end = System.nanoTime() + T.toNanos();
      while ((ops.stream().noneMatch(hooked) || ops.stream().noneMatch(hookImage)) && System.nanoTime() < end) {
        ops.addAll(host.expect(PluginMsg::hasPanelPatch, T).getPanelPatch().getOpsList());
      }
      ops.addAll(host.quiet(Duration.ofMillis(300)).stream().filter(PluginMsg::hasPanelPatch)
          .flatMap(m -> m.getPanelPatch().getOpsList().stream()).toList());
      List<String> images = ops.stream().filter(o -> o.hasSetImage()).map(o -> o.getWidgetId() + "/" + o.getRowKey()).toList();
      assertEquals(java.util.Set.of("img/", "cell/a"), java.util.Set.copyOf(images), "held images, and none for the removed row b: " + ops);
      // what went at the install is not lent again by the open that follows it
      assertEquals(1, images.stream().filter("cell/a"::equals).count(), "cell a's image lent once for this open: " + ops);
      assertEquals(1, ops.stream().filter(o -> o.getWidgetId().equals("img") && o.hasSetImage() && o.getSetImage().getLen() == 3).count(),
          "the held img lent once: " + ops);
      // per widget, the hook's state is the last on the wire; the held img went first, or was superseded
      var imgs = ops.stream().filter(o -> o.getWidgetId().equals("img") && o.hasSetImage()).map(o -> o.getSetImage()).toList();
      assertEquals(2, imgs.getLast().getLen(), "the hook's image wins over the held one: " + ops);
      var statuses = ops.stream().filter(o -> o.getWidgetId().equals("status") && o.hasSetValue()).toList();
      assertTrue(hooked.test(statuses.getLast()), "the hook's status wins: " + ops);
      var mine = imgs.getLast();
      assertEquals(8, pp.view.get(java.lang.foreign.ValueLayout.JAVA_BYTE, mine.getOffset() + 1));
      var cell = ops.stream().filter(o -> o.getWidgetId().equals("cell") && o.hasSetImage()).findFirst().orElseThrow().getSetImage();
      assertEquals(1, cell.getLen());
      assertEquals(4, pp.view.get(java.lang.foreign.ValueLayout.JAVA_BYTE, cell.getOffset()));
    }
  }

  /**
   * vizi relaunches a crashed plugin whose panel is open by granting P with no
   * PanelOpened. Every update still arrives as it happens, and the images held for want of P go out
   * when it installs.
   */
  @Test
  void aPanelGrantedPWithNoPanelOpenedGetsEveryUpdate() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.relaunch", "Relaunch", "0.1").entry("./run");
    Panel p = b.panel(PanelDecl.of("p", "P", column("root", label("status", ""), image("img"),
        WidgetDecl.progress("bar"), WidgetDecl.list("cells", label("n"), image("cell")), button("fill", "fill", "Fill"))));
    Panel.Rows cells = p.rows("cells");
    b.command(CommandDecl.of("fill", "Fill"), ctx -> {
      p.label("status").set("filled");
      p.progress("bar").set(0.5);
      cells.set(List.of("a", "b"));
      cells.row("a").set("n", "Alpha").image("cell", new byte[] {4});
      cells.row("b").set("n", "Beta");
      p.image("img").push(new byte[] {1, 2, 3});
      return "";
    });
    host = TestHost.start(b.build().application()).open();
    host.hello();
    assertTrue(command("fill", null).getCommandDone().getOk());
    List<vizi.plugin.v1.ViziPlugin.PatchOp> ops = new java.util.ArrayList<>();
    java.util.function.Predicate<List<vizi.plugin.v1.ViziPlugin.PatchOp>> allState = l ->
        l.stream().anyMatch(o -> o.getWidgetId().equals("status") && o.getSetValue().getS().equals("filled"))
            && l.stream().anyMatch(o -> o.getWidgetId().equals("bar") && o.hasSetProgress())
            && l.stream().anyMatch(o -> o.hasSetRows())
            && l.stream().filter(o -> o.getWidgetId().equals("n")).count() == 2;
    long end = System.nanoTime() + T.toNanos();
    while (!allState.test(ops) && System.nanoTime() < end) ops.addAll(host.expect(PluginMsg::hasPanelPatch, T).getPanelPatch().getOpsList());
    assertTrue(allState.test(ops), "every non-image op is sent with no PanelOpened: " + ops);
    assertEquals(0, ops.stream().filter(o -> o.hasSetImage()).count(), "no image without P: " + ops);
    try (HostSegment pp = HostSegment.create(0x1_0000_000aL, 1 << 16)) {
      grant(pp, HostSegment.READ_WRITE, pp.install("P", "p", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      acked(pp);
      List<vizi.plugin.v1.ViziPlugin.PatchOp> images = new java.util.ArrayList<>();
      end = System.nanoTime() + T.toNanos();
      while (images.size() < 2 && System.nanoTime() < end) {
        host.expect(PluginMsg::hasPanelPatch, T).getPanelPatch().getOpsList().stream().filter(o -> o.hasSetImage()).forEach(images::add);
      }
      assertEquals(java.util.Set.of("img/", "cell/a"),
          images.stream().map(o -> o.getWidgetId() + "/" + o.getRowKey()).collect(java.util.stream.Collectors.toSet()));
      var img = images.stream().filter(o -> o.getWidgetId().equals("img")).findFirst().orElseThrow().getSetImage();
      assertEquals(3, pp.view.get(ValueLayout.JAVA_BYTE, img.getOffset() + 2));
    }
    assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
        .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
  }

  /**
   * The settings folder reaches plugin code through its context,
   * created on first use; unset, it is empty; a file where it should be throws.
   */
  @Test
  void theSettingsFolderIsCreatedOnFirstUse(@org.junit.jupiter.api.io.TempDir java.nio.file.Path tmp) throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    java.nio.file.Path dir = tmp.resolve("settings").resolve("org.test.settings");
    var b = Plugin.builder("org.test.settings", "Settings", "0.1").entry("./run");
    b.command(CommandDecl.of("where", "Where"), ctx -> ctx.settingsDir().map(p -> p + " " + java.nio.file.Files.isDirectory(p)).orElse("none"));
    Plugin plugin = b.build();
    assertTrue(java.nio.file.Files.notExists(dir));
    host = TestHost.start(plugin.application(),
        vizi.plugin.javasdk.control.Environment.of(java.util.Map.of("VIZI_PLUGIN_SETTINGS_DIR", dir.toString()))).open();
    host.hello();
    assertEquals(dir + " true", command("where", null).getCommandDone().getMessage());

    assertEquals(java.util.Optional.empty(), vizi.plugin.javasdk.control.Environment.of(java.util.Map.of()).settingsDir(), "unset");
    java.nio.file.Path file = java.nio.file.Files.writeString(tmp.resolve("a-file"), "x");
    assertThrows(java.io.IOException.class,
        () -> vizi.plugin.javasdk.control.Environment.of(java.util.Map.of("VIZI_PLUGIN_SETTINGS_DIR", file.toString())).settingsDir());
  }

  /**
   * A job cancelled while it waits for CompileLabelVolume's reply. The request
   * went, so the host still names its bytes: the next push must not be lent the same range.
   */
  @Test
  void aCompileInterruptedAfterItWentKeepsItsLoans() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.compile", "Compile", "0.1").entry("./run");
    Overlay marks = b.overlay(OverlayDecl.drawn("marks", 1));
    Jobs jobs = b.jobs(o -> {});
    b.command(CommandDecl.of("compile", "Compile"), ctx -> jobs.start("compile", j -> {
      j.host().compileLabelVolume(marks, Item.labels("l", Volume.u8(1, 2, 2, new byte[] {1, 1, 2, 2}), 0, 0, 0, new byte[1024]), false);
      return "compiled";
    }));
    b.command(CommandDecl.of("cancel", "Cancel"), ctx -> {
      jobs.cancelAll();
      return "";
    });
    b.command(CommandDecl.of("draw", "Draw"), ctx -> {
      marks.setItems(List.of(Item.markers("m", 1, 2, 3)));
      return "";
    });
    host = TestHost.start(b.build().application()).open();
    host.hello();
    try (HostSegment arena = HostSegment.create(0x1_0000_0031L, 1 << 16)) {
      grant(arena, HostSegment.READ_WRITE, arena.install("O", "marks", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      acked(arena);
      assertTrue(command("compile", null).getCommandDone().getOk());
      var lv = host.expect(PluginMsg::hasCompileLabelVolume, T).getCompileLabelVolume().getItem().getLabelVolume();
      long lo = Math.min(lv.getLabels().getSegment().getOffset(), lv.getColormap().getSegment().getOffset());
      long hi = Math.max(lv.getLabels().getSegment().getOffset() + lv.getLabels().getSegment().getLen(),
          lv.getColormap().getSegment().getOffset() + lv.getColormap().getSegment().getLen());
      assertTrue(command("cancel", null).getCommandDone().getOk()); // never answered: the job is interrupted waiting
      host.quiet(Duration.ofMillis(200));
      assertTrue(command("draw", null).getCommandDone().getOk());
      var ref = host.expect(PluginMsg::hasOverlayUpdate, T).getOverlayUpdate().getItems(0).getMarker().getPositionsZyx().getSegment();
      assertTrue(ref.getOffset() >= hi || ref.getOffset() + ref.getLen() <= lo,
          "the push [" + ref.getOffset() + ", +" + ref.getLen() + ") lands in the compile's [" + lo + ", " + hi + ")");
    }
  }

  /**
   * §9.1: an overlay arena superseded while a
   * reserved push is parked. The old arena is not released until that push is SENT; SegmentReleased
   * follows it; the next push lands in the successor; nothing is reported.
   */
  @Test
  void anOverlayArenaRetiredUnderAParkedPushIsReleasedOnlyAfterThePushIsSent() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    var b = Plugin.builder("org.test.arena", "Arena", "0.1").entry("./run");
    Overlay marks = b.overlay(OverlayDecl.drawn("marks", 1));
    b.command(CommandDecl.of("draw", "Draw"), ctx -> {
      marks.setItems(List.of(Item.markers("m", 1, 2, 3)));
      return "";
    });
    host = TestHost.start(b.build().application()).open();
    host.hello();
    try (HostSegment oldArena = HostSegment.create(0x1_0000_0021L, 1 << 16);
        HostSegment next = HostSegment.create(0x1_0000_0022L, 1 << 16)) {
      grant(oldArena, HostSegment.READ_WRITE, oldArena.install("O", "marks", List.of(
          HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
      acked(oldArena);
      var parked = new java.util.concurrent.CountDownLatch(1);
      var go = new java.util.concurrent.CountDownLatch(1);
      vizi.plugin.javasdk.memory.SegmentStore.afterReserveForTest = () -> {
        parked.countDown();
        try {
          go.await();
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        }
      };
      String draw = host.nextId();
      try {
        host.send(HostMsg.newBuilder().setId(draw).setInvokeCommand(InvokeCommand.newBuilder().setCommandId("draw")).build());
        assertTrue(parked.await(5, TimeUnit.SECONDS), "the push parked after its reservation");
        // the host supersedes the arena: retires the old, grants the successor
        host.send(HostMsg.newBuilder().setRetireSegment(vizi.plugin.v1.ViziPlugin.RetireSegment.newBuilder().setSegmentId(oldArena.id)).build());
        grant(next, HostSegment.READ_WRITE, next.install("O", "marks", List.of(
            HostSegment.loaned(0, 64, (1 << 16) - 64, RegionProducer.REGION_PRODUCER_PLUGIN))), true);
        acked(next);
        assertEquals(0, host.quiet(Duration.ofMillis(300)).stream()
            .filter(m -> m.hasSegmentReleased() && m.getSegmentReleased().getSegmentId() == oldArena.id).count(),
            "not released while the push is parked");
      } finally {
        vizi.plugin.javasdk.memory.SegmentStore.afterReserveForTest = null;
        go.countDown();
      }
      assertTrue(host.reply(draw, T).getCommandDone().getOk());
      // both in the log, neither taken yet: their order is the order they were sent in
      java.util.function.Predicate<PluginMsg> isPush = m -> m.hasOverlayUpdate()
          && m.getOverlayUpdate().getItems(0).getMarker().getPositionsZyx().getSegment().getSegmentId() == oldArena.id;
      java.util.function.Predicate<PluginMsg> isRelease = m -> m.hasSegmentReleased() && m.getSegmentReleased().getSegmentId() == oldArena.id;
      long until = System.nanoTime() + T.toNanos();
      List<PluginMsg> seen = host.seen();
      while ((seen.stream().noneMatch(isPush) || seen.stream().noneMatch(isRelease)) && System.nanoTime() < until) {
        Thread.sleep(20);
        seen = host.seen();
      }
      int push = -1, released = -1;
      for (int k = 0; k < seen.size(); k++) {
        if (isPush.test(seen.get(k))) push = k;
        if (isRelease.test(seen.get(k))) released = k;
      }
      assertTrue(push >= 0, "the parked push was sent, naming the old arena");
      assertTrue(released > push, "SegmentReleased comes after the push naming the old arena: push " + push + ", release " + released);
      host.expect(isPush, T);
      host.expect(isRelease, T);
      // the next push lands in the successor
      assertTrue(command("draw", null).getCommandDone().getOk());
      PluginMsg again = host.expect(PluginMsg::hasOverlayUpdate, T);
      assertEquals(next.id, again.getOverlayUpdate().getItems(0).getMarker().getPositionsZyx().getSegment().getSegmentId());
      assertEquals(List.of(), host.seen().stream().filter(PluginMsg::hasContractViolation)
          .map(m -> m.getContractViolation().getWhere() + ": " + m.getContractViolation().getDetail()).toList());
    }
  }
}
