package vizi.plugin.javasdk.testkit;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.StageSlot;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StageShape;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * Builds a processing-stage slot in ordinary memory (processing_stages.md §5.1), as a host fills
 * one, and the arm to hold it to: for testing a stage's arithmetic with no host.
 *
 * <pre>{@code
 * TestSlot t = TestSlot.of(DType.U16, 4, 16, 16).channel(0, plane0).channel(1, plane1)
 *     .param("factor", 1.5).param("channel", 0L);
 * StageSlot slot = t.slot();          // then stage.process(slot)
 * }</pre>
 */
public final class TestSlot {

  private static final ValueLayout.OfInt U32 = ValueLayout.JAVA_INT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  private static final ValueLayout.OfLong U64 = ValueLayout.JAVA_LONG_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  private static final ValueLayout.OfShort U16 = ValueLayout.JAVA_SHORT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  private static final ValueLayout.OfDouble F64 = ValueLayout.JAVA_DOUBLE_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);

  private final DType dtype;
  private final int z, y, x;
  private final List<Integer> channels = new ArrayList<>();
  private final List<Volume> planes = new ArrayList<>();
  private final Map<String, Value> params = new LinkedHashMap<>();
  private final Map<String, Integer> newChannels = new LinkedHashMap<>();
  private int[] extent;
  private double[] origin = {0, 0, 0};
  private double[] poseR = {1, 0, 0, 0, 1, 0, 0, 0, 1};
  private double[] poseT = {0, 0, 0};
  private long t;
  private int planeCap = -1;
  private long capacity = -1;
  private int pipeline = 1, position = 1;
  private DType outDtype;
  private int[] outAlloc;

  private TestSlot(DType dtype, int z, int y, int x) {
    this.dtype = dtype;
    this.z = z;
    this.y = y;
    this.x = x;
    this.extent = new int[] {0, z, 0, y, 0, x};
    this.outDtype = dtype;
    this.outAlloc = new int[] {z, y, x};
  }

  /** A slot whose planes are {@code dtype} laid out {@code (z, y, x)}, full extent, identity pose. */
  public static TestSlot of(DType dtype, int z, int y, int x) {
    return new TestSlot(dtype, z, y, x);
  }

  /** Appends a plane holding {@code channel}; {@code data} is copied (same dtype and shape). */
  public TestSlot channel(int channel, Volume data) {
    if (data.dtype() != dtype || data.z() != z || data.y() != y || data.x() != x) throw new IllegalArgumentException("a plane is the slot's dtype and shape");
    channels.add(channel);
    planes.add(data);
    return this;
  }

  public TestSlot extent(int z0, int zN, int y0, int yN, int x0, int xN) {
    extent = new int[] {z0, zN, y0, yN, x0, xN};
    return this;
  }

  public TestSlot origin(double oz, double oy, double ox) {
    origin = new double[] {oz, oy, ox};
    return this;
  }

  public TestSlot pose(double[] r, double[] tr) {
    poseR = r.clone();
    poseT = tr.clone();
    return this;
  }

  public TestSlot t(long t) {
    this.t = t;
    return this;
  }

  /** The arm's position (default pipeline 1, position 1). */
  public TestSlot at(int pipeline, int position) {
    this.pipeline = pipeline;
    this.position = position;
    return this;
  }

  public TestSlot param(String key, double f) {
    params.put(key, Value.newBuilder().setF(f).build());
    return this;
  }

  public TestSlot param(String key, long i) {
    params.put(key, Value.newBuilder().setI(i).build());
    return this;
  }

  public TestSlot param(String key, boolean b) {
    params.put(key, Value.newBuilder().setB(b).build());
    return this;
  }

  public TestSlot param(String key, String s) {
    params.put(key, Value.newBuilder().setS(s).build());
    return this;
  }

  /** The arm assigns {@code index} to the plan's new channel {@code key}; room is made for it. */
  public TestSlot newChannel(String key, int index) {
    newChannels.put(key, index);
    return this;
  }

  /** The armed plan's output (default: the input's dtype and allocation). */
  public TestSlot output(DType d, int oz, int oy, int ox) {
    outDtype = d;
    outAlloc = new int[] {oz, oy, ox};
    return this;
  }

  /** Planes allocated, and bytes of data space (defaults: enough for every plane and new channel at 8 bytes a voxel). */
  public TestSlot sized(int planeCap, long capacity) {
    this.planeCap = planeCap;
    this.capacity = capacity;
    return this;
  }

  public StageArm arm() {
    StageShape.Builder in = StageShape.newBuilder().setZ(z).setY(y).setX(x).setDtype(dtype.wire()).addAllChannels(channels);
    return StageArm.newBuilder().setStageId("test").setRevision(1).setPipeline(pipeline).setPosition(position)
        .setInput(in).setOutput(StageShape.newBuilder().setZ(outAlloc[0]).setY(outAlloc[1]).setX(outAlloc[2]).setDtype(outDtype.wire()))
        .putAllNewChannels(newChannels).putAllParams(params).build();
  }

  /** The slot, written as a host writes it, in memory the garbage collector frees. */
  public StageSlot slot() {
    int cap = planeCap >= 0 ? planeCap : channels.size() + newChannels.size();
    long bytes = capacity >= 0 ? capacity : (long) cap * z * y * x * 8;
    long hb = (256 + 2L * cap + 63) & ~63L;
    MemorySegment m = Arena.ofAuto().allocate(hb + bytes, 64);
    m.set(U64, 0, 1L | ((long) pipeline << 32) | ((long) position << 48));
    m.set(U32, 16, (int) t);
    m.set(U32, 28, dtype.wire().getNumber());
    m.set(U32, 32, channels.size());
    m.set(U32, 36, cap);
    m.set(U32, 40, z);
    m.set(U32, 44, y);
    m.set(U32, 48, x);
    m.set(U64, 56, bytes);
    for (int i = 0; i < 6; i++) m.set(U32, 64 + 4L * i, extent[i]);
    for (int i = 0; i < 3; i++) m.set(F64, 88 + 8L * i, origin[i]);
    for (int i = 0; i < 9; i++) m.set(F64, 112 + 8L * i, poseR[i]);
    for (int i = 0; i < 3; i++) m.set(F64, 184 + 8L * i, poseT[i]);
    for (int p = 0; p < channels.size(); p++) {
      m.set(U16, 256 + 2L * p, (short) (int) channels.get(p));
      long pb = planes.get(p).data().byteSize();
      MemorySegment.copy(planes.get(p).data(), 0, m, hb + p * pb, pb);
    }
    return StageSlot.over(m, arm());
  }
}
