import java.io.ByteArrayOutputStream;
import java.math.BigInteger;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.Map;
import java.util.OptionalInt;
import java.util.zip.CRC32;
import java.util.zip.Deflater;

/** SPEC.md's arithmetic and formatting, in integers, with no SDK in sight. */
final class Quant {
  private Quant() {}

  /**
   * One region's row: its {@code name} (maybe empty), voxel count and exact sum (unsigned). A
   * voxel count of -1 is a row not measured (§2: a dataset that is not integer data).
   */
  record Row(String name, long voxels, long sum) {
    static Row unmeasured(String name) {
      return new Row(name, -1, 0);
    }

    boolean measured() {
      return voxels >= 0;
    }

    String display(String id) {
      return name.isEmpty() ? id : name;
    }

    /** §3: the count, or {@code ""} for a row not measured. */
    String voxelsText() {
      return measured() ? Long.toString(voxels) : "";
    }
  }

  /** §3: the most rows the list shows (max_rows; four cells a row fill the 4096-cell ceiling). */
  static final int MAX_ROWS = 1024;

  /** §3: the status past {@link #MAX_ROWS} regions. */
  static String showing(int n) {
    return "Showing " + MAX_ROWS + " of " + n + " regions";
  }

  private static final BigInteger THOUSAND = BigInteger.valueOf(1000);

  /** §3: the mean in thousandths, rounded half up in integers; {@code ""} for a count of 0. */
  static String meanText(long sum, long count) {
    if (count <= 0) return ""; // none covered, or not measured
    BigInteger s = new BigInteger(Long.toUnsignedString(sum));
    BigInteger c = BigInteger.valueOf(count);
    BigInteger th = s.multiply(BigInteger.valueOf(2000)).add(c).divide(c.shiftLeft(1));
    BigInteger[] qr = th.divideAndRemainder(THOUSAND);
    return qr[0] + "." + String.format("%03d", qr[1].intValue());
  }

  /** §3: the colormap entry, {@code min(255, (sum div count) >> 8)}; 0 for a count of 0. */
  static int swatchIndex(long sum, long count) {
    if (count <= 0) return 0;
    long mean = Long.divideUnsigned(sum, count);
    return (int) Math.min(255, mean >>> 8);
  }

  /** Two strings compared by code point (String.compareTo compares UTF-16 units). */
  static int compareCodePoints(String a, String b) {
    int i = 0, j = 0;
    while (i < a.length() && j < b.length()) {
      int ca = a.codePointAt(i), cb = b.codePointAt(j);
      if (ca != cb) return Integer.compare(ca, cb);
      i += Character.charCount(ca);
      j += Character.charCount(cb);
    }
    return Integer.compare(a.length() - i, b.length() - j);
  }

  /** A row's order key (§3, §7): its display name, then its id. */
  record OrderKey(String display, String id) implements Comparable<OrderKey> {
    @Override
    public int compareTo(OrderKey o) {
      int c = compareCodePoints(display, o.display);
      return c != 0 ? c : compareCodePoints(id, o.id);
    }
  }

  /** §7: the offer's label for {@code n} rows; {@code ""} (withdraw) for none. */
  static String tourLabel(int n) {
    return n == 0 ? "" : "Tour " + n + (n == 1 ? " region" : " regions");
  }

  /** §3: keys by display name, then by region id, both by code point. */
  static List<String> ordered(Map<String, Row> rows) {
    List<String> keys = new ArrayList<>(rows.keySet());
    keys.sort(Comparator.<String, String>comparing(k -> rows.get(k).display(k), Quant::compareCodePoints)
        .thenComparing(k -> k, Quant::compareCodePoints));
    return keys;
  }

  /** §5: whether mean a = sa/ca is above mean b = sb/cb, exactly. */
  static boolean brighter(long sa, long ca, long sb, long cb) {
    BigInteger l = new BigInteger(Long.toUnsignedString(sa)).multiply(BigInteger.valueOf(cb));
    BigInteger r = new BigInteger(Long.toUnsignedString(sb)).multiply(BigInteger.valueOf(ca));
    return l.compareTo(r) > 0;
  }

  /**
   * §5: the frame with the highest mean, the earliest on a tie, skipping frames with no voxel;
   * empty when none has one. {@code measured[t] = {count, sum}}.
   */
  static OptionalInt bestFrame(long[][] measured) {
    int best = -1;
    for (int t = 0; t < measured.length; t++) {
      long count = measured[t][0], sum = measured[t][1];
      if (count == 0) continue;
      if (best < 0 || brighter(sum, count, measured[best][1], measured[best][0])) best = t;
    }
    return best < 0 ? OptionalInt.empty() : OptionalInt.of(best);
  }

  /** §6: RFC 4180, quoted only when it holds a comma, a double quote or a line break. */
  static String csvField(String s) {
    if (s.indexOf(',') < 0 && s.indexOf('"') < 0 && s.indexOf('\r') < 0 && s.indexOf('\n') < 0) return s;
    return '"' + s.replace("\"", "\"\"") + '"';
  }

  /** §6: the table, one line per row in list order, {@code \n} endings. */
  static String csv(Map<String, Row> rows, long t, long c) {
    StringBuilder sb = new StringBuilder("region_id,name,t,channel,voxels,mean\n");
    for (String k : ordered(rows)) {
      Row r = rows.get(k);
      sb.append(csvField(k)).append(',').append(csvField(r.name())).append(',').append(t).append(',').append(c).append(',')
          .append(r.voxelsText()).append(',').append(meanText(r.sum(), r.voxels())).append('\n');
    }
    return sb.toString();
  }

  /** A {@code side} × {@code side} RGB PNG of one color, written by hand (zlib from java.util.zip). */
  static byte[] png(int side, int r, int g, int b) {
    byte[] raw = new byte[side * (1 + 3 * side)];
    for (int y = 0, p = 0; y < side; y++) {
      raw[p++] = 0; // filter: none
      for (int x = 0; x < side; x++) {
        raw[p++] = (byte) r;
        raw[p++] = (byte) g;
        raw[p++] = (byte) b;
      }
    }
    Deflater d = new Deflater();
    d.setInput(raw);
    d.finish();
    ByteArrayOutputStream z = new ByteArrayOutputStream();
    byte[] buf = new byte[512];
    while (!d.finished()) z.write(buf, 0, d.deflate(buf));
    d.end();
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    out.writeBytes(new byte[] {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'});
    chunk(out, "IHDR", ByteBuffer.allocate(13).putInt(side).putInt(side).put((byte) 8).put((byte) 2).put((byte) 0)
        .put((byte) 0).put((byte) 0).array());
    chunk(out, "IDAT", z.toByteArray());
    chunk(out, "IEND", new byte[0]);
    return out.toByteArray();
  }

  private static void chunk(ByteArrayOutputStream out, String kind, byte[] data) {
    byte[] k = kind.getBytes(StandardCharsets.US_ASCII);
    CRC32 crc = new CRC32();
    crc.update(k);
    crc.update(data);
    out.writeBytes(ByteBuffer.allocate(4).putInt(data.length).array());
    out.writeBytes(k);
    out.writeBytes(data);
    out.writeBytes(ByteBuffer.allocate(4).putInt((int) crc.getValue()).array());
  }
}
