import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.Locale;
import java.util.Map;
import java.util.zip.CRC32;
import java.util.zip.Deflater;

/**
 * A mean-versus-t plot as a grayscale PNG (SPEC.md §"Measuring" step 5): axes, the points
 * joined, the y-range printed and the axes named, in a 3×5 bitmap font, through a minimal encoder
 * (signature, IHDR, IDAT, IEND) on {@code java.util.zip}.
 */
final class Plot {
  private Plot() {}

  private static final Map<Character, String[]> FONT = Map.ofEntries(
      Map.entry('0', new String[] {"111", "101", "101", "101", "111"}),
      Map.entry('1', new String[] {"010", "110", "010", "010", "111"}),
      Map.entry('2', new String[] {"111", "001", "111", "100", "111"}),
      Map.entry('3', new String[] {"111", "001", "111", "001", "111"}),
      Map.entry('4', new String[] {"101", "101", "111", "001", "001"}),
      Map.entry('5', new String[] {"111", "100", "111", "001", "111"}),
      Map.entry('6', new String[] {"111", "100", "111", "101", "111"}),
      Map.entry('7', new String[] {"111", "001", "001", "001", "001"}),
      Map.entry('8', new String[] {"111", "101", "111", "101", "111"}),
      Map.entry('9', new String[] {"111", "101", "111", "001", "111"}),
      Map.entry('.', new String[] {"000", "000", "000", "000", "010"}),
      Map.entry('-', new String[] {"000", "000", "111", "000", "000"}),
      Map.entry('e', new String[] {"000", "111", "111", "100", "111"}),
      Map.entry('t', new String[] {"010", "111", "010", "010", "011"}),
      Map.entry('m', new String[] {"000", "110", "111", "101", "101"}),
      Map.entry('a', new String[] {"000", "111", "001", "111", "111"}),
      Map.entry('n', new String[] {"000", "110", "101", "101", "101"}));

  static final class Canvas {
    final int width;
    final int height;
    final byte[] px;

    Canvas(int width, int height) {
      this.width = width;
      this.height = height;
      this.px = new byte[width * height];
    }

    void set(int y, int x, int ink) {
      if (y >= 0 && y < height && x >= 0 && x < width) px[y * width + x] = (byte) ink;
    }

    int get(int y, int x) {
      return px[y * width + x] & 0xFF;
    }

    void text(int y, int x, String s) {
      for (char ch : s.toCharArray()) {
        String[] g = FONT.get(ch);
        if (g != null) {
          for (int r = 0; r < 5; r++) for (int c = 0; c < 3; c++) if (g[r].charAt(c) == '1') set(y + r, x + c, 255);
        }
        x += 4;
      }
    }

    void line(int y0, int x0, int y1, int x1, int ink) {
      int steps = Math.max(Math.max(Math.abs(y1 - y0), Math.abs(x1 - x0)), 1);
      for (int i = 0; i <= steps; i++) {
        set((int) Math.rint(y0 + (double) (y1 - y0) * i / steps), (int) Math.rint(x0 + (double) (x1 - x0) * i / steps), ink);
      }
    }
  }

  /** {@code v} in at most six characters (the plot's left margin), at the most precision that fits. */
  static String num(double v) {
    String s = "";
    for (int digits = 4; digits >= 1; digits--) {
      s = String.format(Locale.ROOT, "%." + digits + "g", v);
      if (s.contains("e")) {
        s = s.replaceAll("\\.?0+e", "e").replace("e+", "e").replaceAll("e(-?)0+(\\d)", "e$1$2");
      } else if (s.contains(".")) {
        s = s.replaceAll("0+$", "").replaceAll("\\.$", "");
      }
      if (s.length() <= 6) return s;
    }
    return s;
  }

  /** A {@code (height, width)} raster of {@code means}, one point per timepoint. */
  static Canvas render(double[] means, int width, int height) {
    Canvas cv = new Canvas(width, height);
    int left = 28, top = 8, right = width - 8, bottom = height - 18;
    cv.line(top, left, bottom, left, 128);
    cv.line(bottom, left, bottom, right, 128);
    cv.text(bottom + 8, (left + right) / 2 - 1, "t");
    cv.text(top, 2, "mean");
    double lo = Double.POSITIVE_INFINITY, hi = Double.NEGATIVE_INFINITY;
    for (double v : means) {
      if (!Double.isFinite(v)) continue;
      lo = Math.min(lo, v);
      hi = Math.max(hi, v);
    }
    if (lo > hi) return cv;
    if (hi == lo) {
      lo -= 1;
      hi += 1;
    }
    cv.text(top + 8, 2, num(hi));
    cv.text(bottom - 6, 2, num(lo));
    int n = means.length;
    int[][] pts = new int[n][];
    for (int i = 0; i < n; i++) {
      if (!Double.isFinite(means[i])) continue;
      int x = left + (right - left) * i / Math.max(n - 1, 1);
      int y = bottom - (int) Math.rint((bottom - top) * (means[i] - lo) / (hi - lo));
      pts[i] = new int[] {y, x};
    }
    for (int i = 1; i < n; i++) if (pts[i] != null && pts[i - 1] != null) cv.line(pts[i - 1][0], pts[i - 1][1], pts[i][0], pts[i][1], 200);
    for (int[] p : pts) {
      if (p == null) continue;
      for (int dy = -1; dy <= 1; dy++) for (int dx = -1; dx <= 1; dx++) cv.set(p[0] + dy, p[1] + dx, 255);
    }
    return cv;
  }

  /** An 8-bit grayscale PNG. */
  static byte[] png(Canvas cv) {
    ByteArrayOutputStream raw = new ByteArrayOutputStream();
    for (int y = 0; y < cv.height; y++) {
      raw.write(0);
      raw.write(cv.px, y * cv.width, cv.width);
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
    chunk(out, "IHDR", ByteBuffer.allocate(13).putInt(cv.width).putInt(cv.height).put((byte) 8).put((byte) 0).put((byte) 0).put((byte) 0).put((byte) 0).array());
    chunk(out, "IDAT", z.toByteArray());
    chunk(out, "IEND", new byte[0]);
    return out.toByteArray();
  }

  private static void chunk(ByteArrayOutputStream out, String kind, byte[] body) {
    byte[] k = kind.getBytes(StandardCharsets.US_ASCII);
    out.writeBytes(ByteBuffer.allocate(4).putInt(body.length).array());
    out.writeBytes(k);
    out.writeBytes(body);
    CRC32 crc = new CRC32();
    crc.update(k);
    crc.update(body);
    out.writeBytes(ByteBuffer.allocate(4).putInt((int) crc.getValue()).array());
  }
}
