import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;

import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Stage;
import vizi.plugin.javasdk.StageSlot;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.testkit.TestSlot;

/** SPEC §3's arithmetic, offline. */
class StageChainTest {

  static Volume u16(int z, int y, int x, int... v) {
    short[] s = new short[z * y * x];
    for (int i = 0; i < v.length; i++) s[i] = (short) v[i];
    return Volume.u16(z, y, x, s);
  }

  static Volume u8(int z, int y, int x, int... v) {
    byte[] s = new byte[z * y * x];
    for (int i = 0; i < v.length; i++) s[i] = (byte) v[i];
    return Volume.u8(z, y, x, s);
  }

  static Volume f32(int z, int y, int x, float... v) {
    return Volume.f32(z, y, x, java.util.Arrays.copyOf(v, z * y * x));
  }

  static TestSlot gain(TestSlot t, double factor, long channel) {
    return t.param("factor", factor).param("channel", channel).param("stall_ms", 0L);
  }

  @Test
  void gainRoundsHalfToEvenAndClamps() throws Exception {
    StageSlot s = gain(TestSlot.of(DType.U16, 1, 1, 4).channel(0, u16(1, 1, 4, 1, 3, 5, 50000)), 1.5, 0).slot();
    new StageChain.Gain().process(s);
    Volume v = s.plane(0);
    assertEquals(2, v.u16(0)); // 1.5 → 2
    assertEquals(4, v.u16(1)); // 4.5 → 4
    assertEquals(8, v.u16(2)); // 7.5 → 8
    assertEquals(65535, v.u16(3));

    StageSlot half = gain(TestSlot.of(DType.U16, 1, 1, 3).channel(0, u16(1, 1, 3, 1, 3, 5)), 0.5, 0).slot();
    new StageChain.Gain().process(half);
    assertEquals(0, half.plane(0).u16(0)); // 0.5 → 0
    assertEquals(2, half.plane(0).u16(1)); // 1.5 → 2
    assertEquals(2, half.plane(0).u16(2)); // 2.5 → 2

    StageSlot bytes = gain(TestSlot.of(DType.U8, 1, 1, 2).channel(0, u8(1, 1, 2, 200, 3)), 1.5, 0).slot();
    new StageChain.Gain().process(bytes);
    assertEquals(255, bytes.plane(0).u8(0));
    assertEquals(4, bytes.plane(0).u8(1));

    StageSlot floats = gain(TestSlot.of(DType.F32, 1, 1, 1).channel(0, f32(1, 1, 1, 1.1f)), 1.5, 0).slot();
    new StageChain.Gain().process(floats);
    assertEquals((float) ((double) 1.1f * 1.5), floats.plane(0).f32(0));
  }

  @Test
  void gainTouchesOnlyItsChannelInsideTheExtent() throws Exception {
    StageSlot s = gain(TestSlot.of(DType.U16, 1, 1, 3).channel(1, u16(1, 1, 3, 10, 10, 10)).channel(0, u16(1, 1, 3, 7, 7, 7))
        .extent(0, 1, 0, 1, 1, 2), 2, 0).slot();
    new StageChain.Gain().process(s);
    assertEquals(7, s.plane(0).u16(0));
    assertEquals(14, s.plane(0).u16(1));
    assertEquals(7, s.plane(0).u16(2));
    assertEquals(10, s.plane(1).u16(1), "plane 0 is channel 1 here, untouched");
  }

  static TestSlot diff(TestSlot t, long a, long b) {
    return t.param("a", a).param("b", b).newChannel("absdiff", 7);
  }

  @Test
  void diffAppendsTheAbsoluteDifference() {
    StageSlot s = diff(TestSlot.of(DType.U16, 1, 1, 3).channel(0, u16(1, 1, 3, 5, 9, 0)).channel(1, u16(1, 1, 3, 9, 5, 65535))
        .extent(0, 1, 0, 1, 0, 2), 0, 1).slot();
    new StageChain.Diff().process(s);
    assertEquals(java.util.List.of(0, 1, 7), s.channels());
    Volume d = s.plane(7);
    assertEquals(4, d.u16(0));
    assertEquals(4, d.u16(1));
    assertEquals(0, d.u16(2), "zero outside the extent");

    StageSlot same = diff(TestSlot.of(DType.U16, 1, 1, 2).channel(0, u16(1, 1, 2, 5, 9)), 0, 0).slot();
    new StageChain.Diff().process(same);
    assertEquals(0, same.plane(7).u16(1));

    StageSlot f = diff(TestSlot.of(DType.F32, 1, 1, 1).channel(0, f32(1, 1, 1, 0.1f)).channel(1, f32(1, 1, 1, 0.3f)), 0, 1).slot();
    new StageChain.Diff().process(f);
    assertEquals(Math.abs(0.1f - 0.3f), f.plane(7).f32(0));
  }

  @Test
  void widenRepacksAndRestatesThePlacement() {
    short[] v = new short[2 * 8 * 8];
    for (int i = 0; i < v.length; i++) v[i] = (short) i;
    StageSlot s = TestSlot.of(DType.U16, 2, 8, 8).channel(0, Volume.u16(2, 8, 8, v)).param("crop", 2L)
        .extent(0, 2, 1, 7, 3, 8).origin(10, 20, 30).output(DType.F32, 2, 4, 4).slot();
    new StageChain.Widen().process(s);
    assertEquals(DType.F32, s.dtype());
    assertArrayEquals(new int[] {2, 4, 4}, s.alloc());
    assertArrayEquals(new int[] {0, 2, 0, 4, 1, 4}, s.extent());
    assertArrayEquals(new double[] {10, 21, 30}, s.origin());
    assertEquals(2 * 8 + 2, s.plane(0).f32(0)); // new (0,0,0) is old (0,2,2)
    assertEquals(64 + 5 * 8 + 5, s.plane(0).f32(s.plane(0).index(1, 3, 3)));
  }

  static StageSlot drift(Volume v, long t, int position) {
    return TestSlot.of(DType.U16, 1, 3, 3).channel(0, v).param("channel", 0L).t(t).at(1, position).origin(5, 0, 0).slot();
  }

  @Test
  void driftComposesThePoseFromTheReferenceCentroid() throws Exception {
    StageChain.Drift d = new StageChain.Drift();
    StageSlot first = drift(u16(1, 3, 3, 0, 0, 0, 0, 4, 0), 0, 1); // centroid (5, 1, 1)
    d.process(first);
    assertArrayEquals(new double[] {0, 0, 0}, first.poseT());
    StageSlot second = drift(u16(1, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 8), 1, 1); // centroid (5, 2, 2)
    d.process(second);
    assertArrayEquals(new double[] {0, -1, -1}, second.poseT());
    // another arm keeps its own state: its first volume is its reference
    StageSlot other = drift(u16(1, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 8), 5, 3);
    d.process(other);
    assertArrayEquals(new double[] {0, 0, 0}, other.poseT());
  }

  @Test
  void driftRefusesAGapAndStartsOver() throws Exception {
    StageChain.Drift d = new StageChain.Drift();
    d.process(drift(u16(1, 3, 3, 4), 0, 1));
    Stage.Refuse r = assertThrows(Stage.Refuse.class, () -> d.process(drift(u16(1, 3, 3, 4), 2, 1)));
    assertEquals(Stage.Refuse.CAUSAL, r.reason());
    StageSlot again = drift(u16(1, 3, 3, 0, 0, 0, 0, 0, 0, 0, 0, 8), 3, 1);
    d.process(again);
    assertArrayEquals(new double[] {0, 0, 0}, again.poseT(), "a fresh start: this volume is the reference");
  }

  @Test
  void driftWithNoSignalWritesNoPose() throws Exception {
    StageChain.Drift d = new StageChain.Drift();
    StageSlot empty = drift(u16(1, 3, 3), 0, 1);
    d.process(empty);
    StageSlot later = drift(u16(1, 3, 3, 9), 1, 1);
    d.process(later);
    assertArrayEquals(new double[] {0, 0, 0}, later.poseT(), "no reference: never a pose until it starts over");
  }

  @Test
  void theManifestIsValid() {
    assertEquals("org.example.stage_chain", StageChain.plugin().manifest().id());
  }
}
