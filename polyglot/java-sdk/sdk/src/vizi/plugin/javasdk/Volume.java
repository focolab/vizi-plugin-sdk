package vizi.plugin.javasdk;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.ByteOrder;
import java.util.Objects;

/**
 * A C-order {@code (Z, Y, X)} array of samples, backed by a {@link MemorySegment} so it may
 * exceed 2 GiB and be indexed with a {@code long}. Samples are little-endian, as on the wire.
 * Unsigned samples are widened: {@link #u8} and {@link #u16} return {@code int}, so a 16-bit
 * value is never a negative {@code short}.
 */
public final class Volume {

  private static final ValueLayout.OfShort U16 = ValueLayout.JAVA_SHORT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  private static final ValueLayout.OfFloat F32 = ValueLayout.JAVA_FLOAT_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);
  private static final ValueLayout.OfDouble F64 = ValueLayout.JAVA_DOUBLE_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);

  private final int z;
  private final int y;
  private final int x;
  private final DType dtype;
  private final MemorySegment data;

  private Volume(int z, int y, int x, DType dtype, MemorySegment data) {
    if (z < 0 || y < 0 || x < 0) throw new IllegalArgumentException("negative shape");
    long need = (long) z * y * x * dtype.size;
    if (data.byteSize() != need) {
      throw new IllegalArgumentException("a (" + z + ", " + y + ", " + x + ") " + dtype + " volume needs " + need
          + " bytes, the data has " + data.byteSize());
    }
    this.z = z;
    this.y = y;
    this.x = x;
    this.dtype = dtype;
    this.data = data;
  }

  /** Wraps {@code data} (which the caller owns and keeps alive). */
  public static Volume of(int z, int y, int x, DType dtype, MemorySegment data) {
    return new Volume(z, y, x, dtype, Objects.requireNonNull(data, "data"));
  }

  /** A zeroed volume in a memory region the garbage collector frees. */
  public static Volume zeros(int z, int y, int x, DType dtype) {
    return new Volume(z, y, x, dtype, Arena.ofAuto().allocate((long) z * y * x * dtype.size, 8));
  }

  public static Volume u8(int z, int y, int x, byte[] samples) {
    Volume v = zeros(z, y, x, DType.U8);
    MemorySegment.copy(samples, 0, v.data, ValueLayout.JAVA_BYTE, 0, samples.length);
    return v;
  }

  /** From Java shorts holding u16 bit patterns (so 65535 is -1 here). */
  public static Volume u16(int z, int y, int x, short[] samples) {
    Volume v = zeros(z, y, x, DType.U16);
    for (int i = 0; i < samples.length; i++) v.data.setAtIndex(U16, i, samples[i]);
    return v;
  }

  public static Volume f32(int z, int y, int x, float[] samples) {
    Volume v = zeros(z, y, x, DType.F32);
    for (int i = 0; i < samples.length; i++) v.data.setAtIndex(F32, i, samples[i]);
    return v;
  }

  public static Volume f64(int z, int y, int x, double[] samples) {
    Volume v = zeros(z, y, x, DType.F64);
    for (int i = 0; i < samples.length; i++) v.data.setAtIndex(F64, i, samples[i]);
    return v;
  }

  public int z() {
    return z;
  }

  public int y() {
    return y;
  }

  public int x() {
    return x;
  }

  public DType dtype() {
    return dtype;
  }

  /** The bytes, little-endian, C order. */
  public MemorySegment data() {
    return data;
  }

  public long samples() {
    return (long) z * y * x;
  }

  /** The flat index of {@code (k, j, i)}. */
  public long index(int k, int j, int i) {
    return ((long) k * y + j) * x + i;
  }

  public int u8(long i) {
    return data.get(ValueLayout.JAVA_BYTE, i) & 0xFF;
  }

  public int u16(long i) {
    return data.getAtIndex(U16, i) & 0xFFFF;
  }

  public float f32(long i) {
    return data.getAtIndex(F32, i);
  }

  public double f64(long i) {
    return data.getAtIndex(F64, i);
  }

  /** Any sample, as a double. */
  public double get(long i) {
    return switch (dtype) {
      case U8 -> u8(i);
      case U16 -> u16(i);
      case F32 -> f32(i);
      case F64 -> f64(i);
    };
  }

  public void setU8(long i, int v) {
    data.set(ValueLayout.JAVA_BYTE, i, (byte) v);
  }

  public void setU16(long i, int v) {
    data.setAtIndex(U16, i, (short) v);
  }

  public void setF32(long i, float v) {
    data.setAtIndex(F32, i, v);
  }

  public void setF64(long i, double v) {
    data.setAtIndex(F64, i, v);
  }

  @Override
  public String toString() {
    return "Volume(" + z + ", " + y + ", " + x + ") " + dtype;
  }
}
