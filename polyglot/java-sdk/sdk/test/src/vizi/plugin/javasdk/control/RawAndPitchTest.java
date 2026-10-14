package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.OptionalDouble;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.AppliedFilter;
import vizi.plugin.v1.ViziPlugin.DataVolume;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ReadDataVolume;
import vizi.plugin.v1.ViziPlugin.SpacingProvenance;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * {@code ReadDataVolume.raw}, a reply's {@code filter}, and {@code ViewState}'s
 * pitch per axis with NaN where a file records none (control_plane_protocol.md §8.7).
 */
class RawAndPitchTest {
  static final Duration T = Duration.ofSeconds(10);
  TestHost host;

  @AfterEach
  void down() {
    if (host != null) host.close();
  }

  /** Each command makes one read and reports what failed; the test answers with a host error. */
  Plugin plugin() {
    var b = Plugin.builder("org.test.raw", "Raw", "0.1").entry("./run").permissions(Permission.DATA_ACCESS);
    b.command(CommandDecl.of("raw", "Raw"), ctx -> read(() -> ctx.host().readRawVolume(1, 2)));
    b.command(CommandDecl.of("raw_level", "Raw level"), ctx -> read(() -> ctx.host().readRawVolume(1, 2, 3)));
    b.command(CommandDecl.of("plain", "Plain"), ctx -> read(() -> ctx.host().readVolume(1, 2)));
    return b.build();
  }

  interface Read {
    Host.Deposit<DataVolume> run() throws Exception;
  }

  static String read(Read r) throws Exception {
    try {
      r.run();
      return "read";
    } catch (HostException e) {
      return "refused: " + e.getMessage();
    }
  }

  ReadDataVolume invoke(String command) throws InterruptedException {
    String id = host.nextId();
    host.step(command).send(HostMsg.newBuilder().setId(id).setInvokeCommand(InvokeCommand.newBuilder().setCommandId(command)).build());
    PluginMsg ask = host.expect(PluginMsg::hasReadDataVolume, T);
    host.send(HostMsg.newBuilder().setId(ask.getId()).setHostError(Error.newBuilder().setKind(Error.Kind.NOT_FOUND)
        .setMessage("no dataset")).build());
    assertTrue(host.reply(id, T).getCommandDone().getMessage().startsWith("refused: "));
    return ask.getReadDataVolume();
  }

  @Test
  void aRawReadSetsTheFieldAndAPlainOneDoesNot() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().application()).open();
    host.hello();
    ReadDataVolume raw = invoke("raw");
    assertTrue(raw.getRaw());
    assertEquals(List.of(1, 2, 0), List.of(raw.getT(), raw.getC(), raw.getLevel()));
    ReadDataVolume rawLevel = invoke("raw_level");
    assertTrue(rawLevel.getRaw());
    assertEquals(3, rawLevel.getLevel());
    assertFalse(invoke("plain").getRaw(), "a plain read leaves raw false: the source, or what is shared");
  }

  @Test
  void aReplyNamesItsFilterOrNone() {
    assertEquals(Optional.empty(), Host.filterOf(DataVolume.getDefaultInstance()), "the file's own voxels");
    AppliedFilter f = AppliedFilter.newBuilder().setPluginId("org.example.stab").setStageId("s").build();
    assertEquals(Optional.of(f), Host.filterOf(DataVolume.newBuilder().setFilter(f).build()));
  }

  static ViewState view(String unit, SpacingProvenance p, double... zyx) {
    ViewState.Builder b = ViewState.newBuilder().setSpacingUnit(unit).setSpacingProvenance(p);
    for (double d : zyx) b.addSpacingZyx(d);
    return b.build();
  }

  @Test
  void aPartlyRecordedPitchYieldsTheKnownAxesAndNotTheOther() {
    Host.Pitch part = Host.pitch(view("um", SpacingProvenance.SPACING_PROVENANCE_FILE, Double.NaN, 0.25, 0.5));
    assertEquals(OptionalDouble.empty(), part.z(), "Z unrecorded: NaN on the wire, unknown here");
    assertEquals(OptionalDouble.of(0.25), part.y());
    assertEquals(OptionalDouble.of(0.5), part.x());
    assertEquals(List.of(OptionalDouble.empty(), OptionalDouble.of(0.25), OptionalDouble.of(0.5)), part.zyx());
    assertTrue(part.anyKnown());
    assertFalse(part.allKnown());
    assertEquals("um", part.unit());

    Host.Pitch all = Host.pitch(view("um", SpacingProvenance.SPACING_PROVENANCE_FILE, 2, 0.25, 0.5));
    assertTrue(all.allKnown());
    assertEquals(OptionalDouble.of(2), all.z());

    for (Host.Pitch none : List.of(
        Host.pitch(view("", SpacingProvenance.SPACING_PROVENANCE_DISPLAY_FALLBACK, 1, 1, 1)),
        Host.pitch(view("um", SpacingProvenance.SPACING_PROVENANCE_DISPLAY_FALLBACK, 1, 1, 1)),
        Host.pitch(view("", SpacingProvenance.SPACING_PROVENANCE_FILE, 1, 1, 1)),
        Host.pitch(ViewState.getDefaultInstance()))) {
      assertFalse(none.anyKnown(), none.toString());
    }
  }

  /**
   * Each axis is empty exactly where it is not known, and positive and finite
   * wherever present; {@code allKnown} and {@code anyKnown} agree with the axes; nothing of a
   * garbage value leaks through.
   */
  @Test
  void thePerAxisInvariantHoldsOverEveryKindOfViewState() {
    record Case(String what, ViewState view, List<Boolean> known) {}
    SpacingProvenance file = SpacingProvenance.SPACING_PROVENANCE_FILE;
    SpacingProvenance fallback = SpacingProvenance.SPACING_PROVENANCE_DISPLAY_FALLBACK;
    double inf = Double.POSITIVE_INFINITY;
    List<Case> cases = List.of(
        new Case("partly recorded", view("um", file, Double.NaN, 0.25, 0.5), List.of(false, true, true)),
        new Case("all three", view("um", file, 2, 0.25, 0.5), List.of(true, true, true)),
        new Case("display fallback", view("", fallback, 1, 1, 1), List.of(false, false, false)),
        new Case("a fallback carrying a unit", view("um", fallback, 1, 1, 1), List.of(false, false, false)),
        new Case("FILE with no unit", view("", file, 1, 1, 1), List.of(false, false, false)),
        new Case("garbage", view("um", file, 0, -1, inf), List.of(false, false, false)),
        new Case("garbage beside a measurement", view("um", file, 0, -1, 2), List.of(false, false, true)),
        new Case("no dataset", ViewState.getDefaultInstance(), List.of(false, false, false)));
    for (Case c : cases) {
      Host.Pitch p = Host.pitch(c.view());
      List<OptionalDouble> axes = p.zyx();
      for (int i = 0; i < 3; i++) {
        OptionalDouble a = axes.get(i);
        assertEquals(c.known().get(i), a.isPresent(), c.what() + ": axis " + i);
        if (a.isPresent()) {
          double d = a.getAsDouble();
          assertTrue(Double.isFinite(d) && d > 0, c.what() + ": axis " + i + " is " + d);
          assertEquals(c.view().getSpacingZyx(i), d, c.what() + ": axis " + i + " is the host's value");
        }
      }
      assertEquals(c.known().stream().allMatch(k -> k), p.allKnown(), c.what() + ": allKnown");
      assertEquals(c.known().contains(true), p.anyKnown(), c.what() + ": anyKnown");
    }
  }
}
