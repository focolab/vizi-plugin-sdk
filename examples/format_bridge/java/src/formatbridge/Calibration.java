package formatbridge;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.Map;
import java.util.TreeMap;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Refusal;

/**
 * {@code vizi-crate-cal.bin} (SPEC.md §"The calibration table"): a per-channel offset, applied as
 * {@code reported = raw - offset[c]} in the dtype's own arithmetic, saturating for the unsigned
 * dtypes and unclamped for f32. A channel absent from the table has offset 0.
 */
public record Calibration(Map<Integer, Integer> offsets) {

  static final byte[] MAGIC = "VIZCAL01".getBytes(StandardCharsets.US_ASCII);

  public Calibration {
    offsets = Map.copyOf(offsets);
  }

  public static Calibration parse(byte[] data) throws Refusal {
    if (data.length < 16) throw new Refusal(ErrorKind.IO_ERROR, "calibration table is shorter than its header");
    for (int i = 0; i < 8; i++) {
      if (data[i] != MAGIC[i]) throw new Refusal(ErrorKind.IO_ERROR, "calibration table has the wrong magic");
    }
    ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
    long count = Integer.toUnsignedLong(b.getInt(8));
    if (data.length != 16 + count * 8) {
      throw new Refusal(ErrorKind.IO_ERROR, "calibration table declares " + count + " entries but is " + data.length + " bytes");
    }
    Map<Integer, Integer> offsets = new TreeMap<>();
    for (int i = 0; i < count; i++) {
      // channel is u32; a channel past Integer.MAX_VALUE can never be read, so it is skipped
      long channel = Integer.toUnsignedLong(b.getInt(16 + 8 * i));
      int offset = b.getInt(16 + 8 * i + 4);
      if (channel <= Integer.MAX_VALUE) offsets.put((int) channel, offset);
    }
    return new Calibration(offsets);
  }

  public static Calibration load(Path path) throws Refusal {
    byte[] data;
    try {
      data = Files.readAllBytes(path);
    } catch (IOException e) {
      throw new Refusal(ErrorKind.IO_ERROR, "calibration table " + path + " cannot be read: " + e.getMessage());
    }
    return parse(data);
  }

  /** Decodes {@code raw} (little-endian samples of {@code dtype}) in place for channel {@code c}. */
  public void apply(byte[] raw, DType dtype, int c) {
    int off = offsets.getOrDefault(c, 0);
    if (off == 0) return;
    ByteBuffer b = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN);
    switch (dtype) {
      case U8 -> {
        for (int i = 0; i < raw.length; i++) raw[i] = (byte) clamp((long) (raw[i] & 0xFF) - off, 0xFF);
      }
      case U16 -> {
        for (int i = 0; i < raw.length; i += 2) b.putShort(i, (short) clamp((long) (b.getShort(i) & 0xFFFF) - off, 0xFFFF));
      }
      case F32 -> {
        float fo = (float) off;
        for (int i = 0; i < raw.length; i += 4) b.putFloat(i, b.getFloat(i) - fo);
      }
      case F64 -> throw new IllegalArgumentException("f64 has no crate encoding");
    }
  }

  private static long clamp(long v, long max) {
    return Math.max(0, Math.min(max, v));
  }

  public int channels() {
    return offsets.size();
  }
}
