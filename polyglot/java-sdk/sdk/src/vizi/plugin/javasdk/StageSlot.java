package vizi.plugin.javasdk;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.ByteOrder;
import java.util.ArrayList;
import java.util.List;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.StageArm;

/**
 * One slot during this stage's turn (processing_stages.md §5): the header, and the planes it
 * holds. Everything here is valid only inside {@link Stage#process}; the SDK has validated the
 * header already.
 *
 * <p>Planes are {@link Volume} views of the shared memory, laid out {@code (z_alloc, y_alloc,
 * x_alloc)}; the valid data is the {@link #extent()} of each. Writes go straight to the slot.
 */
public final class StageSlot {

  static final ValueLayout.OfInt U32 = ValueLayout.JAVA_INT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  static final ValueLayout.OfLong U64 = ValueLayout.JAVA_LONG_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  static final ValueLayout.OfShort U16 = ValueLayout.JAVA_SHORT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  static final ValueLayout.OfDouble F64 = ValueLayout.JAVA_DOUBLE_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);

  // header offsets (§5.1)
  static final long TURN = 0, OUTCOME = 8, RESERVED_12 = 12, T = 16, DONE_THROUGH = 20, REASON = 24, DTYPE = 28, PLANES = 32,
      PLANE_CAP = 36, Z_ALLOC = 40, Y_ALLOC = 44, X_ALLOC = 48, RESERVED_52 = 52, CAPACITY = 56, EXTENT = 64, ORIGIN = 88,
      POSE_R = 112, POSE_T = 184, OPEN_GENERATION = 208, RESERVED_216 = 216, CHANNELS = 256;

  private final MemorySegment m;
  private final StageArm arm;
  private final Stage.Params params;
  private boolean open = true;

  StageSlot(MemorySegment slot, StageArm arm) {
    this.m = slot;
    this.arm = arm;
    this.params = new Stage.Params(arm.getParamsMap());
  }

  /**
   * A slot over {@code slot} (one region, header first) held to {@code arm}: for testing a stage
   * offline and for tools. The SDK's runner validates the header before a turn; this does not
   * (the testkit's {@code TestSlot} writes valid ones).
   */
  public static StageSlot over(MemorySegment slot, StageArm arm) {
    return new StageSlot(slot, arm);
  }

  /** Ends the turn: every later call throws. */
  public void close() {
    open = false;
  }

  /** The header's problems against the region and the arm (§7), or null when it is valid. */
  public @Nullable String validate() {
    return problem(m, m.byteSize(), arm);
  }

  private void check() {
    if (!open) throw new IllegalStateException("the turn is over: a slot is valid only inside process()");
  }

  static long headerBytes(long planeCap) {
    return (256 + 2 * planeCap + 63) & ~63L;
  }

  // ---- identity ----

  public long t() {
    return Integer.toUnsignedLong(m.get(U32, T));
  }

  public long openGeneration() {
    return m.get(U64, OPEN_GENERATION);
  }

  public int pipeline() {
    return arm.getPipeline();
  }

  public int position() {
    return arm.getPosition();
  }

  public Stage.Params params() {
    return params;
  }

  // ---- layout ----

  public DType dtype() {
    check();
    return DType.of(vizi.plugin.v1.ViziPlugin.DType.forNumber(m.get(U32, DTYPE))).orElseThrow();
  }

  /** {@code {z_alloc, y_alloc, x_alloc}}. */
  public int[] alloc() {
    check();
    return new int[] {m.get(U32, Z_ALLOC), m.get(U32, Y_ALLOC), m.get(U32, X_ALLOC)};
  }

  public int planes() {
    return m.get(U32, PLANES);
  }

  int planeCap() {
    return m.get(U32, PLANE_CAP);
  }

  long capacity() {
    return m.get(U64, CAPACITY);
  }

  /** The channel index of each plane, in plane order. */
  public List<Integer> channels() {
    check();
    List<Integer> out = new ArrayList<>();
    for (int p = 0; p < planes(); p++) out.add(Short.toUnsignedInt(m.get(U16, CHANNELS + 2L * p)));
    return out;
  }

  public boolean has(int channel) {
    return channels().contains(channel);
  }

  private long planeBytes() {
    int[] a = alloc();
    return (long) a[0] * a[1] * a[2] * dtype().size;
  }

  private long dataStart() {
    return headerBytes(planeCap());
  }

  /** The plane holding {@code channel}, as a view of the slot. */
  public Volume plane(int channel) {
    check();
    List<Integer> ch = channels();
    int p = ch.indexOf(channel);
    if (p < 0) throw new IllegalArgumentException("channel " + channel + " is not in this slot (it holds " + ch + ")");
    int[] a = alloc();
    long pb = planeBytes();
    return Volume.of(a[0], a[1], a[2], dtype(), m.asSlice(dataStart() + p * pb, pb));
  }

  /**
   * Appends the channel the arm assigned to {@code key} (a key of the plan's {@code adds}), zeroed,
   * and returns its plane.
   */
  public Volume addChannel(String key) {
    check();
    Integer index = arm.getNewChannelsMap().get(key);
    if (index == null) throw new IllegalArgumentException("the arm assigns no channel to \"" + key + "\" (its keys: " + arm.getNewChannelsMap().keySet() + ")");
    int planes = planes();
    if (planes >= planeCap()) throw new IllegalStateException("the slot has no plane left: " + planes + " of " + planeCap());
    long pb = planeBytes();
    if ((planes + 1) * pb > capacity()) throw new IllegalStateException("another plane would exceed the slot's capacity");
    MemorySegment plane = m.asSlice(dataStart() + planes * pb, pb);
    plane.fill((byte) 0);
    m.set(U16, CHANNELS + 2L * planes, (short) (int) index);
    m.set(U32, PLANES, planes + 1);
    int[] a = alloc();
    return Volume.of(a[0], a[1], a[2], dtype(), plane);
  }

  // ---- extent, origin, pose ----

  /** {@code {z0, zN, y0, yN, x0, xN}}: the valid box, the same for every plane. */
  public int[] extent() {
    check();
    int[] e = new int[6];
    for (int i = 0; i < 6; i++) e[i] = m.get(U32, EXTENT + 4L * i);
    return e;
  }

  /** Shrinks (never grows beyond the allocation) the valid box. */
  public void setExtent(int z0, int zN, int y0, int yN, int x0, int xN) {
    check();
    int[] a = alloc();
    int[] e = {z0, zN, y0, yN, x0, xN};
    for (int k = 0; k < 3; k++) {
      if (e[2 * k] < 0 || e[2 * k] > e[2 * k + 1] || e[2 * k + 1] > a[k]) {
        throw new IllegalArgumentException("the extent must lie within the allocation (" + a[0] + ", " + a[1] + ", " + a[2] + ")");
      }
    }
    for (int i = 0; i < 6; i++) m.set(U32, EXTENT + 4L * i, e[i]);
  }

  /** Where voxel {@code (z0, y0, x0)} sits in vizi's level-0 voxel space. */
  public double[] origin() {
    check();
    return new double[] {m.get(F64, ORIGIN), m.get(F64, ORIGIN + 8), m.get(F64, ORIGIN + 16)};
  }

  public void setOrigin(double z, double y, double x) {
    check();
    finite(z, y, x);
    m.set(F64, ORIGIN, z);
    m.set(F64, ORIGIN + 8, y);
    m.set(F64, ORIGIN + 16, x);
  }

  /** The absolute pose's rotation, row-major, acting on column {@code (z, y, x)}. */
  public double[] poseR() {
    check();
    double[] r = new double[9];
    for (int i = 0; i < 9; i++) r[i] = m.get(F64, POSE_R + 8L * i);
    return r;
  }

  public double[] poseT() {
    check();
    return new double[] {m.get(F64, POSE_T), m.get(F64, POSE_T + 8), m.get(F64, POSE_T + 16)};
  }

  /** Writes an absolute pose (a registration to a fixed reference). */
  public void setPose(double[] r, double[] t) {
    check();
    if (r.length != 9 || t.length != 3) throw new IllegalArgumentException("a pose is a 3×3 rotation and a translation");
    finite(r);
    finite(t);
    for (int i = 0; i < 9; i++) m.set(F64, POSE_R + 8L * i, r[i]);
    for (int i = 0; i < 3; i++) m.set(F64, POSE_T + 8L * i, t[i]);
  }

  /**
   * Composes this stage's own motion {@code s_in = Rk · s_out + Tk} with the pose in the header,
   * as one update (§5.5): {@code (R, T) ← (R · Rk, R · Tk + T)}.
   */
  public void composePose(double[] rk, double[] tk) {
    double[] r = poseR();
    double[] t = poseT();
    double[] nr = new double[9];
    double[] nt = new double[3];
    for (int i = 0; i < 3; i++) {
      for (int j = 0; j < 3; j++) {
        double s = 0;
        for (int k = 0; k < 3; k++) s += r[3 * i + k] * rk[3 * k + j];
        nr[3 * i + j] = s;
      }
      double s = 0;
      for (int k = 0; k < 3; k++) s += r[3 * i + k] * tk[k];
      nt[i] = s + t[i];
    }
    setPose(nr, nt);
  }

  private static void finite(double... v) {
    for (double d : v) if (!Double.isFinite(d)) throw new IllegalArgumentException("a pose or origin value is not finite: " + d);
  }

  // ---- changing the layout (§5.3) ----

  /**
   * Repacks every plane as {@code dtype} in the allocation {@code (z, y, x)}, taking voxel
   * {@code (k, j, i)} of the new layout from {@code (k + oz, j + oy, i + ox)} of the old. The
   * extent is re-stated in the new layout (clipped to it) and the origin follows the data, so
   * every valid voxel keeps its place in vizi's space. Converting to an integer type rounds half
   * to even and clamps; NaN becomes 0.
   */
  public void relayout(DType dtype, int z, int y, int x, int oz, int oy, int ox) {
    check();
    int[] a = alloc();
    int[] na = {z, y, x};
    int[] off = {oz, oy, ox};
    for (int k = 0; k < 3; k++) {
      if (na[k] < 1 || off[k] < 0 || off[k] + na[k] > a[k]) {
        throw new IllegalArgumentException("the new layout must lie within the old allocation (" + a[0] + ", " + a[1] + ", " + a[2] + ")");
      }
    }
    DType od = dtype();
    int planes = planes();
    long newPlane = (long) z * y * x * dtype.size;
    if (planes * newPlane > capacity()) throw new IllegalStateException("the new layout exceeds the slot's capacity");
    long oldPlane = planeBytes();
    long start = dataStart();
    try (Arena scratch = Arena.ofConfined()) {
      MemorySegment old = scratch.allocate(Math.max(1, planes * oldPlane), 8);
      MemorySegment.copy(m, start, old, 0, planes * oldPlane);
      for (int p = 0; p < planes; p++) {
        Volume src = Volume.of(a[0], a[1], a[2], od, old.asSlice(p * oldPlane, oldPlane));
        Volume dst = Volume.of(z, y, x, dtype, m.asSlice(start + p * newPlane, newPlane));
        for (int k = 0; k < z; k++) {
          for (int j = 0; j < y; j++) {
            for (int i = 0; i < x; i++) {
              put(dst, dst.index(k, j, i), src.get(src.index(k + oz, j + oy, i + ox)));
            }
          }
        }
      }
    }
    int[] e = extent();
    double[] o = origin();
    int[] ne = new int[6];
    double[] no = new double[3];
    for (int k = 0; k < 3; k++) {
      int lo = Math.max(e[2 * k] - off[k], 0);
      int hi = Math.min(Math.max(e[2 * k + 1] - off[k], 0), na[k]);
      lo = Math.min(lo, hi);
      ne[2 * k] = lo;
      ne[2 * k + 1] = hi;
      no[k] = o[k] + (lo + off[k] - e[2 * k]);
    }
    m.set(U32, DTYPE, dtype.wire().getNumber());
    m.set(U32, Z_ALLOC, z);
    m.set(U32, Y_ALLOC, y);
    m.set(U32, X_ALLOC, x);
    for (int i = 0; i < 6; i++) m.set(U32, EXTENT + 4L * i, ne[i]);
    setOrigin(no[0], no[1], no[2]);
  }

  private static void put(Volume v, long i, double d) {
    switch (v.dtype()) {
      case F64 -> v.setF64(i, d);
      case F32 -> v.setF32(i, (float) d);
      case U8 -> v.setU8(i, clamp(d, 255));
      case U16 -> v.setU16(i, clamp(d, 65535));
    }
  }

  private static int clamp(double d, int top) {
    if (Double.isNaN(d)) return 0;
    double r = Math.rint(d);
    return (int) Math.max(0, Math.min(top, r));
  }

  // ---- validation (§7) ----

  /** The header's problems against the region's extent and the arm, or null when it is valid. */
  static @Nullable String problem(MemorySegment m, long regionExtent, StageArm arm) {
    if (regionExtent < 256) return "the region is " + regionExtent + " bytes, less than the 256-byte header";
    if (m.get(U32, RESERVED_12) != 0 || m.get(U32, RESERVED_52) != 0) return "a reserved header field is not zero";
    for (long o = RESERVED_216; o < CHANNELS; o += 8) if (m.get(U64, o) != 0) return "a reserved header field is not zero";
    long planes = Integer.toUnsignedLong(m.get(U32, PLANES));
    long cap = Integer.toUnsignedLong(m.get(U32, PLANE_CAP));
    if (cap > 65536) return "plane_cap " + cap + " exceeds 65536";
    if (planes > cap) return "planes " + planes + " exceeds plane_cap " + cap;
    long hb = headerBytes(cap);
    long capacity = m.get(U64, CAPACITY);
    if (capacity < 0 || hb + capacity > regionExtent) return "header_bytes " + hb + " + capacity " + Long.toUnsignedString(capacity) + " does not fit the region's " + regionExtent;
    var wire = vizi.plugin.v1.ViziPlugin.DType.forNumber(m.get(U32, DTYPE));
    DType dt = wire == null ? null : DType.of(wire).orElse(null);
    if (dt == null) return "dtype " + Integer.toUnsignedString(m.get(U32, DTYPE)) + " is not a known DType";
    long[] a = {Integer.toUnsignedLong(m.get(U32, Z_ALLOC)), Integer.toUnsignedLong(m.get(U32, Y_ALLOC)), Integer.toUnsignedLong(m.get(U32, X_ALLOC))};
    long pb = a[0] * a[1] * a[2] * dt.size;
    if (a[0] > Integer.MAX_VALUE || a[1] > Integer.MAX_VALUE || a[2] > Integer.MAX_VALUE || planes * pb > capacity) {
      return "planes × plane_bytes exceeds capacity " + capacity;
    }
    java.util.Set<Integer> seen = new java.util.HashSet<>();
    for (int p = 0; p < planes; p++) {
      int c = Short.toUnsignedInt(m.get(U16, CHANNELS + 2L * p));
      if (!seen.add(c)) return "channel " + c + " is named at two planes";
    }
    for (int k = 0; k < 3; k++) {
      long lo = Integer.toUnsignedLong(m.get(U32, EXTENT + 8L * k));
      long hi = Integer.toUnsignedLong(m.get(U32, EXTENT + 8L * k + 4));
      if (lo > hi || hi > a[k]) return "the extent does not lie within the allocation";
    }
    for (long o = ORIGIN; o < OPEN_GENERATION; o += 8) {
      if (!Double.isFinite(m.get(F64, o))) return "origin or pose is not finite";
    }
    var in = arm.getInput();
    if (dt.wire() != in.getDtype()) return "dtype " + dt + " is not the armed input's " + in.getDtype();
    if (a[0] > Integer.toUnsignedLong(in.getZ()) || a[1] > Integer.toUnsignedLong(in.getY()) || a[2] > Integer.toUnsignedLong(in.getX())) {
      return "the allocation (" + a[0] + ", " + a[1] + ", " + a[2] + ") exceeds the armed input's";
    }
    return null;
  }

  /** Whether the turn kept to the armed plan: null, or what it broke. */
  @Nullable String planBreach() {
    var out = arm.getOutput();
    DType d = dtype();
    if (d.wire() != out.getDtype()) return "the turn finished with dtype " + d + ", the armed plan says " + out.getDtype();
    int[] a = alloc();
    if (a[0] > out.getZ() || a[1] > out.getY() || a[2] > out.getX()) {
      return "the turn finished with allocation (" + a[0] + ", " + a[1] + ", " + a[2] + "), larger than the armed plan's";
    }
    return null;
  }
}
