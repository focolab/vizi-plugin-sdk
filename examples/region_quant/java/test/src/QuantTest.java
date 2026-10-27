import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.OptionalInt;
import java.util.zip.InflaterInputStream;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/** The offline checks: the arithmetic, the order, the CSV, the tour and the PNG. */
class QuantTest {

  @Test
  void theMeanText() {
    assertEquals("1.063", Quant.meanText(17, 16)); // 1.0625: the tie rounds up
    assertEquals("531.250", Quant.meanText(2125, 4));
    assertEquals("", Quant.meanText(0, 0));
    assertEquals("0.000", Quant.meanText(0, 3));
    assertEquals("0.333", Quant.meanText(1, 3));
    assertEquals("0.667", Quant.meanText(2, 3));
    // a large sum: 2000·sum overflows 64 bits, and the text is still exact
    assertEquals("18446744073709551615.000", Quant.meanText(-1L, 1)); // 2^64 - 1, unsigned
    assertEquals("9223372036854775807.500", Quant.meanText(-1L, 2));
  }

  @Test
  void theSwatchIndex() {
    assertEquals(0, Quant.swatchIndex(0, 0));
    assertEquals(0, Quant.swatchIndex(123456, 0));
    assertEquals(0, Quant.swatchIndex(255, 1));
    assertEquals(1, Quant.swatchIndex(256, 1));
    assertEquals(1, Quant.swatchIndex(1023, 2)); // floor(511.5) >> 8
    assertEquals(255, Quant.swatchIndex(65535, 1));
    assertEquals(255, Quant.swatchIndex(1L << 40, 1)); // the clamp
  }

  @Test
  void theOrder() {
    Map<String, Quant.Row> rows = new LinkedHashMap<>();
    rows.put("r3", new Quant.Row("beta", 0, 0));
    rows.put("r1", new Quant.Row("", 0, 0)); // sorts by its id, "r1"
    rows.put("r2", new Quant.Row("alpha", 0, 0));
    rows.put("r9", new Quant.Row("same", 0, 0));
    rows.put("r10", new Quant.Row("same", 0, 0)); // names equal: ids by code point, "r10" < "r9"
    assertEquals(List.of("r2", "r3", "r1", "r10", "r9"), Quant.ordered(rows));
  }

  @Test
  void codePointsNotUtf16Units() {
    String astral = new String(Character.toChars(0x1F600)); // a surrogate pair, D83D DE00
    String high = "Ａ"; // FULLWIDTH A, above the surrogates as a unit, below 0x1F600 as a code point
    assertTrue(astral.compareTo(high) < 0, "UTF-16 order puts the surrogate first");
    assertTrue(Quant.compareCodePoints(high, astral) < 0, "code point order does not");
    assertTrue(Quant.compareCodePoints("ab", "abc") < 0);
    assertEquals(0, Quant.compareCodePoints("x", "x"));
  }

  @Test
  void theCsv() {
    assertEquals("plain", Quant.csvField("plain"));
    assertEquals("\"a,b\"", Quant.csvField("a,b"));
    assertEquals("\"say \"\"hi\"\"\"", Quant.csvField("say \"hi\""));
    assertEquals("\"two\nlines\"", Quant.csvField("two\nlines"));
    Map<String, Quant.Row> rows = new LinkedHashMap<>();
    rows.put("r2", new Quant.Row("b,c", 4, 2125));
    rows.put("r1", new Quant.Row("", 0, 0));
    assertEquals("region_id,name,t,channel,voxels,mean\nr2,\"b,c\",3,1,4,531.250\nr1,,3,1,0,\n", Quant.csv(rows, 3, 1));
  }

  /** A leading space is not quoted (the rule is comma, quote, line break); an f32 row's numbers are empty. */
  @Test
  void aLeadingSpaceIsNotQuotedAndAnUnmeasuredRowIsEmpty() {
    assertEquals(" padded", Quant.csvField(" padded"));
    Map<String, Quant.Row> rows = new LinkedHashMap<>();
    rows.put("r1", Quant.Row.unmeasured(" alpha"));
    assertEquals("region_id,name,t,channel,voxels,mean\nr1, alpha,3,0,,\n", Quant.csv(rows, 3, 0));
    assertEquals(0, Quant.swatchIndex(0, -1));
    assertEquals("", Quant.meanText(0, -1));
  }

  @Test
  void theBestFrame() {
    // {count, sum}: frames 1 and 3 tie at 5.0; frame 0 covers nothing
    assertEquals(OptionalInt.of(1), Quant.bestFrame(new long[][] {{0, 0}, {2, 10}, {4, 12}, {1, 5}}));
    assertEquals(OptionalInt.empty(), Quant.bestFrame(new long[][] {{0, 0}, {0, 0}}));
    assertEquals(OptionalInt.of(2), Quant.bestFrame(new long[][] {{3, 3}, {0, 0}, {3, 4}}));
    assertTrue(Quant.brighter(-1L, 3, -2L, 3), "unsigned sums compare exactly");
  }

  @Test
  void thePng() throws Exception {
    byte[] png = Quant.png(8, 68, 1, 84);
    ByteBuffer b = ByteBuffer.wrap(png);
    assertEquals(0x89504E47, b.getInt(0));
    assertEquals(8, b.getInt(16));
    assertEquals(8, b.getInt(20));
    assertEquals(2, png[25]); // color type RGB
    int idatLen = b.getInt(33);
    byte[] z = java.util.Arrays.copyOfRange(png, 41, 41 + idatLen);
    ByteArrayOutputStream raw = new ByteArrayOutputStream();
    new InflaterInputStream(new ByteArrayInputStream(z)).transferTo(raw);
    byte[] r = raw.toByteArray();
    assertEquals(8 * 25, r.length);
    for (int y = 0; y < 8; y++) {
      assertEquals(0, r[y * 25]);
      for (int x = 0; x < 8; x++) assertArrayEquals(new byte[] {68, 1, 84}, java.util.Arrays.copyOfRange(r, y * 25 + 1 + 3 * x, y * 25 + 4 + 3 * x));
    }
  }

  @Test
  void theTourLabelAndWhereAResumePicksUp() {
    assertEquals("", Quant.tourLabel(0));
    assertEquals("Tour 1 region", Quant.tourLabel(1));
    assertEquals("Tour 2 regions", Quant.tourLabel(2));
    // a resume starts after the last shown key: by display name, then id, by code point
    var last = new Quant.OrderKey("same", "r10");
    assertTrue(new Quant.OrderKey("same", "r9").compareTo(last) > 0, "r9 follows r10 by code point");
    assertTrue(new Quant.OrderKey("same", "r1").compareTo(last) < 0);
    assertTrue(new Quant.OrderKey("alpha", "r99").compareTo(last) < 0, "the name decides first");
    assertEquals(0, new Quant.OrderKey("same", "r10").compareTo(last), "the key shown last is not shown again");
  }

  @Test
  void theManifestOnDiskIsTheDeclaredOne() throws Exception {
    String disk = Files.readString(Path.of(System.getenv().getOrDefault("MILL_WORKSPACE_ROOT", "."), "vizi-plugin.json"));
    assertEquals(ManifestParser.parseDetached(disk).manifest(), RegionQuant.build().manifest());
    assertEquals(disk, ManifestWriter.write(RegionQuant.build().manifest()));
  }
}
