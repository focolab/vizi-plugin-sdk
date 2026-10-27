import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.zip.CRC32;
import java.util.zip.Deflater;

/**
 * The profile as a PNG (SPEC.md §5 step 3): value against distance along the probe, the coarse
 * pick marked. An 8-bit grayscale raster with two axes and the profile joined point to point, and
 * a minimal encoder (signature, IHDR, IDAT, IEND). No dependency.
 */
final class Plot {
  private Plot() {}

  static final int WIDTH = 240;
  static final int HEIGHT = 140;

  /** The raster, row-major {@code HEIGHT × WIDTH}. {@code pick} indexes {@code values}, or −1. */
  static byte[] render(double[] values, double[] xs, int pick) {
    byte[] img = new byte[WIDTH * HEIGHT];
    int left = 12, top = 8, right = WIDTH - 8, bottom = HEIGHT - 12;
    line(img, top, left, bottom, left, 128);
    line(img, bottom, left, bottom, right, 128);
    double lo = Double.POSITIVE_INFINITY, hi = Double.NEGATIVE_INFINITY;
    for (double v : values) {
      if (Double.isFinite(v)) {
        lo = Math.min(lo, v);
        hi = Math.max(hi, v);
      }
    }
    if (lo > hi) return img;
    if (hi == lo) {
      lo -= 1;
      hi += 1;
    }
    double x0 = xs.length == 0 ? 0 : xs[0], x1 = xs.length == 0 ? 1 : xs[xs.length - 1];
    double span = x1 - x0 == 0 ? 1 : x1 - x0;
    int[][] pts = new int[values.length][];
    for (int i = 0; i < values.length; i++) {
      if (!Double.isFinite(values[i])) continue;
      int x = left + (int) Math.round((right - left) * (xs[i] - x0) / span);
      int y = bottom - (int) Math.round((bottom - top) * (values[i] - lo) / (hi - lo));
      pts[i] = new int[] {y, x};
    }
    int[] prev = null;
    for (int[] p : pts) {
      if (p != null && prev != null) line(img, prev[0], prev[1], p[0], p[1], 200);
      prev = p;
    }
    if (pick >= 0 && pick < pts.length && pts[pick] != null) {
      for (int dy = -2; dy <= 2; dy++) for (int dx = -2; dx <= 2; dx++) put(img, pts[pick][0] + dy, pts[pick][1] + dx, 255);
    }
    return img;
  }

  private static void put(byte[] img, int y, int x, int ink) {
    if (y >= 0 && y < HEIGHT && x >= 0 && x < WIDTH) img[y * WIDTH + x] = (byte) ink;
  }

  private static void line(byte[] img, int y0, int x0, int y1, int x1, int ink) {
    int steps = Math.max(1, Math.max(Math.abs(y1 - y0), Math.abs(x1 - x0)));
    for (int i = 0; i <= steps; i++) {
      put(img, (int) Math.round(y0 + (y1 - y0) * (double) i / steps), (int) Math.round(x0 + (x1 - x0) * (double) i / steps), ink);
    }
  }

  /** An 8-bit grayscale PNG of a {@code HEIGHT × WIDTH} raster. */
  static byte[] png(byte[] gray) {
    ByteArrayOutputStream raw = new ByteArrayOutputStream();
    for (int y = 0; y < HEIGHT; y++) {
      raw.write(0);
      raw.write(gray, y * WIDTH, WIDTH);
    }
    Deflater d = new Deflater();
    d.setInput(raw.toByteArray());
    d.finish();
    ByteArrayOutputStream z = new ByteArrayOutputStream();
    byte[] buf = new byte[8192];
    while (!d.finished()) z.write(buf, 0, d.deflate(buf));
    d.end();
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    out.writeBytes(new byte[] {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'});
    chunk(out, "IHDR", ByteBuffer.allocate(13).putInt(WIDTH).putInt(HEIGHT).put((byte) 8).put((byte) 0).put((byte) 0)
        .put((byte) 0).put((byte) 0).array());
    chunk(out, "IDAT", z.toByteArray());
    chunk(out, "IEND", new byte[0]);
    return out.toByteArray();
  }

  private static void chunk(ByteArrayOutputStream out, String kind, byte[] body) {
    byte[] k = kind.getBytes(java.nio.charset.StandardCharsets.US_ASCII);
    out.writeBytes(ByteBuffer.allocate(4).putInt(body.length).array());
    out.writeBytes(k);
    out.writeBytes(body);
    CRC32 crc = new CRC32();
    crc.update(k);
    crc.update(body);
    out.writeBytes(ByteBuffer.allocate(4).putInt((int) crc.getValue()).array());
  }
}
