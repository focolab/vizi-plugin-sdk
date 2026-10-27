import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNotNull;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.ChunkedOverlay;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.ManifestException;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.v1.ViziPlugin.Value;

/** The offline checks: the arithmetic, the tables, the replies and the manifest. */
class MovieSegTest {

  static final double T = 400, W = Bands.BAND_WIDTH;

  static Volume f64(int z, int y, int x, double... v) {
    return Volume.f64(z, y, x, java.util.Arrays.copyOf(v, z * y * x));
  }

  static Volume u16(int z, int y, int x, int... v) {
    short[] s = new short[z * y * x];
    for (int i = 0; i < v.length; i++) s[i] = (short) v[i];
    return Volume.u16(z, y, x, s);
  }

  static int[] u8s(Volume v) {
    int[] out = new int[(int) v.samples()];
    for (int i = 0; i < out.length; i++) out[i] = v.u8(i);
    return out;
  }

  // ---- the band rule ----

  @Test
  void bandEdges() {
    assertEquals(0, Bands.label(Math.nextDown(T), T));
    assertEquals(0, Bands.label(T - 1e-9, T));
    assertEquals(1, Bands.label(T, T));
    assertEquals(1, Bands.label(T + W - 1, T));
    assertEquals(1, Bands.label(Math.nextDown(T + W), T));
    assertEquals(2, Bands.label(T + W, T));
    assertEquals(3, Bands.label(T + 2 * W, T));
    assertEquals(3, Bands.label(1e300, T));
    assertEquals(3, Bands.label(Double.MAX_VALUE, T));
    assertEquals(0, Bands.label(Double.NaN, T));
    assertEquals(0, Bands.label(Double.NEGATIVE_INFINITY, T));
    assertEquals(3, Bands.label(Double.POSITIVE_INFINITY, T));
  }

  @Test
  void thresholdZero() {
    assertEquals(1, Bands.label(0, 0));
    assertEquals(1, Bands.label(199, 0));
    assertEquals(2, Bands.label(200, 0));
    assertEquals(0, Bands.label(-0.5, 0));
    assertEquals(0, Bands.label(Double.NaN, 0));
    assertEquals(1, Bands.label(-0.0, 0));
  }

  @Test
  void integerInput() {
    Volume v = u16(1, 1, 6, 0, 399, 400, 599, 600, 65535);
    Volume l = Bands.bandLabels(v, T);
    assertEquals(DType.U8, l.dtype());
    assertArrayEquals(new int[] {0, 0, 1, 1, 2, 3}, u8s(l));
  }

  @Test
  void floatInput() {
    Volume v = f64(1, 1, 5, Double.NaN, Double.NEGATIVE_INFINITY, Double.POSITIVE_INFINITY, 800, 799.5);
    assertArrayEquals(new int[] {0, 0, 3, 3, 2}, u8s(Bands.bandLabels(v, T)));
  }

  // ---- the tight box ----

  @Test
  void tightBoxOffOrigin() {
    Volume l = Volume.zeros(3, 4, 5, DType.U8);
    l.setU8(l.index(1, 1, 2), 2);
    l.setU8(l.index(2, 3, 4), 1);
    ChunkedOverlay.Chunk c = Bands.bboxTight(l);
    assertNotNull(c);
    assertEquals(List.of(1, 1, 2), List.of(c.z0(), c.y0(), c.x0()));
    Volume t = c.labels();
    assertEquals(List.of(2, 3, 3), List.of(t.z(), t.y(), t.x()));
    assertEquals(2, t.u8(t.index(0, 0, 0)));
    assertEquals(1, t.u8(t.index(1, 2, 2)));
    assertEquals(0, t.u8(t.index(0, 1, 1))); // zeros inside kept
  }

  @Test
  void fullVolumeIsItsOwnBox() {
    Volume l = Bands.bandLabels(u16(2, 2, 2, 500, 500, 500, 500, 500, 500, 500, 500), T);
    ChunkedOverlay.Chunk c = Bands.bboxTight(l);
    assertNotNull(c);
    assertEquals(List.of(0, 0, 0), List.of(c.z0(), c.y0(), c.x0()));
    assertEquals(List.of(2, 2, 2), List.of(c.labels().z(), c.labels().y(), c.labels().x()));
  }

  @Test
  void emptyVolumeIsEmpty() {
    assertNull(Bands.bboxTight(Volume.zeros(2, 3, 4, DType.U8)));
    ChunkedOverlay.Chunk c = Bands.chunk(Volume.zeros(2, 3, 4, DType.U8));
    assertNull(c.labels());
  }

  // ---- the footprint ----

  @Test
  void footprintIsTheUnion() {
    Volume a = f64(1, 1, 4, 400, 0, Double.NaN, 0);
    Volume b = f64(1, 1, 4, 0, 0, 0, 1000);
    Volume c = f64(1, 1, 4, 0, 399.9, Double.NaN, 0);
    assertArrayEquals(new int[] {1, 0, 0, 1}, u8s(Bands.footprintLabels(List.of(a, b, c), T)));
  }

  // ---- the tables ----

  static int[] row(byte[] lut, int r) {
    return new int[] {lut[r * 4] & 0xFF, lut[r * 4 + 1] & 0xFF, lut[r * 4 + 2] & 0xFF, lut[r * 4 + 3] & 0xFF};
  }

  @Test
  void bandLut() {
    byte[] lut = Bands.lut(Bands.BAND_COLORS);
    assertArrayEquals(new int[] {0, 0, 0, 0}, row(lut, 0));
    assertArrayEquals(new int[] {60, 120, 255, 255}, row(lut, 1));
    assertArrayEquals(new int[] {255, 200, 0, 255}, row(lut, 2));
    assertArrayEquals(new int[] {255, 60, 60, 255}, row(lut, 3));
    for (int r = 4; r < 256; r++) assertArrayEquals(new int[] {0, 0, 0, 0}, row(lut, r));
    assertEquals(java.util.Map.of(1, "dim", 2, "mid", 3, "bright"), Bands.BAND_NAMES);
    assertNotNull(Bands.bandTable(Set.of(2)));
    assertArrayEquals(lut, Bands.bandLut(Set.of()), "every band shown: the plain LUT");
  }

  /** SPEC §3: a hidden band keeps its color with alpha 0; the others are unchanged. */
  @Test
  void aHiddenBandIsItsRowsAlpha() {
    byte[] lut = Bands.bandLut(Set.of(2));
    assertArrayEquals(new int[] {60, 120, 255, 255}, row(lut, 1));
    assertArrayEquals(new int[] {255, 200, 0, 0}, row(lut, 2));
    assertArrayEquals(new int[] {255, 60, 60, 255}, row(lut, 3));
    assertArrayEquals(new int[] {255, 60, 60, 255}, row(Bands.lut(Bands.BAND_COLORS), 3), "the colors are not mutated");
    assertArrayEquals(new int[] {60, 120, 255, 0}, row(Bands.bandLut(Set.of(1, 3)), 1));
  }

  @Test
  void footprintLut() {
    byte[] lut = Bands.lut(Bands.FOOTPRINT_COLOR);
    assertArrayEquals(new int[] {0, 0, 0, 0}, row(lut, 0));
    assertArrayEquals(new int[] {200, 200, 200, 255}, row(lut, 1));
    for (int r = 2; r < 256; r++) assertArrayEquals(new int[] {0, 0, 0, 0}, row(lut, r));
  }

  // ---- the replies ----

  @Test
  void replies() {
    assertEquals("threshold 400; 3 wanted volume(s) to serve again", Bands.thresholdReply(400.0, 3));
    assertEquals("threshold 0; 0 wanted volume(s) to serve again", Bands.thresholdReply(0.0, 0));
    assertEquals("0", Bands.formatThreshold(-0.0));
    assertEquals("412.5", Bands.formatThreshold(412.5));
    assertEquals("0.1", Bands.formatThreshold(0.1));
    assertEquals("5000", Bands.formatThreshold(5000));
    assertEquals("10000000000000000", Bands.formatThreshold(1e16));
    assertEquals("band 2 (mid) hidden", Bands.bandReply(2, false));
    assertEquals("band 3 (bright) visible", Bands.bandReply(3, true));
  }

  /** SPEC §5: a number, or the text of one (the panel's box); a bool, a word or a negative is refused. */
  @Test
  void thresholdTakesANumberOrItsText() {
    assertEquals(800.0, MovieSeg.parseThreshold(Value.newBuilder().setS(" 800 ").build()).getAsDouble());
    assertEquals("800", Bands.formatThreshold(MovieSeg.parseThreshold(Value.newBuilder().setS(" 800 ").build()).getAsDouble()));
    assertEquals(412.5, MovieSeg.parseThreshold(Value.newBuilder().setS("412.5").build()).getAsDouble());
    assertEquals(1000.0, MovieSeg.parseThreshold(Value.newBuilder().setS("1e3").build()).getAsDouble());
    assertEquals(400.0, MovieSeg.parseThreshold(Value.newBuilder().setI(400).build()).getAsDouble());
    assertEquals(0.5, MovieSeg.parseThreshold(Value.newBuilder().setF(0.5).build()).getAsDouble());
    for (Value bad : List.of(Value.newBuilder().setS("high").build(), Value.newBuilder().setS("-1").build(),
        Value.newBuilder().setS("").build(), Value.newBuilder().setS("NaN").build(), Value.newBuilder().setS("Infinity").build(),
        Value.newBuilder().setS("0x10").build(), Value.newBuilder().setS("5d").build(), Value.newBuilder().setB(true).build(),
        Value.newBuilder().setF(-2).build(), Value.newBuilder().setF(Double.NaN).build())) {
      assertTrue(MovieSeg.parseThreshold(bad).isEmpty(), "refused: " + bad);
    }
    assertTrue(MovieSeg.parseThreshold(null).isEmpty());
  }

  // ---- the manifest ----

  @Test
  void manifestMatchesTheSpec() throws Exception {
    String json = Files.readString(Path.of(System.getenv().getOrDefault("MILL_WORKSPACE_ROOT", "."), "vizi-plugin.json"));
    var parsed = ManifestParser.parseDetached(json).manifest();
    assertEquals(new MovieSeg().plugin().manifest(), parsed);
    assertTrue(parsed.contributions().overlays().stream().allMatch(OverlayDecl::isChunked));
  }

  /** SPEC §1 verbatim (with this launcher): the written file omits the default {@code "time": "per_t"}. */
  @Test
  void specSectionOneIsTheSameManifest() throws Exception {
    String spec = """
        {
          "manifest_version": 1,
          "id": "org.example.movie_seg",
          "name": "Movie seg",
          "version": "0.1.0",
          "engines": { "vizi": "1.0" },
          "entry": { "command": ["./movie-seg"] },
          "permissions": ["data_access"],
          "contributions": {
            "overlays": [
              { "id": "bands", "name": "Intensity bands", "chunked": true, "time": "per_t" },
              { "id": "footprint", "name": "Footprint", "chunked": true, "time": "static" }
            ],
            "commands": [
              { "id": "movie_seg.threshold",    "title": "Set threshold",        "menu": null },
              { "id": "movie_seg.band_visible", "title": "Show or hide a band",  "menu": null },
              { "id": "movie_seg.band_1",       "title": "Show band 1 (dim)",    "menu": null },
              { "id": "movie_seg.band_2",       "title": "Show band 2 (mid)",    "menu": null },
              { "id": "movie_seg.band_3",       "title": "Show band 3 (bright)", "menu": null }
            ],
            "panels": [
              {
                "id": "movie_seg",
                "title": "Movie seg",
                "root": {
                  "kind": "column", "id": "root",
                  "children": [
                    { "kind": "row", "id": "rt", "children": [
                      { "kind": "label", "id": "lt", "text": "Threshold" },
                      { "kind": "line_edit", "id": "threshold", "command": "movie_seg.threshold", "value": "400" }
                    ] },
                    { "kind": "checkbox", "id": "band_1", "command": "movie_seg.band_1", "text": "Band 1 (dim)", "value": true },
                    { "kind": "checkbox", "id": "band_2", "command": "movie_seg.band_2", "text": "Band 2 (mid)", "value": true },
                    { "kind": "checkbox", "id": "band_3", "command": "movie_seg.band_3", "text": "Band 3 (bright)", "value": true }
                  ]
                }
              }
            ]
          }
        }""";
    // The section states the interface; the SDK release that writes the manifest, and the
    // oldest vizi it declares, are added to it.
    String written = spec.replace(
        "\"engines\": { \"vizi\": \"1.0\" },",
        "\"engines\": { \"vizi\": \"1.0\" }, \"min_vizi\": \"" + vizi.plugin.javasdk.control.Identity.DEFAULT_MIN_VIZI
            + "\", \"sdk_version\": \"" + vizi.plugin.javasdk.Sdk.RELEASE + "\",");
    assertEquals(new MovieSeg().plugin().manifest(), ManifestParser.parseDetached(written).manifest());
  }

  static final String HEAD ="{\"manifest_version\": 1, \"id\": \"x.y\", \"name\": \"n\", \"version\": \"0\","
      + " \"engines\": {\"vizi\": \"1.0\"}, \"entry\": {\"command\": [\"./x\"]}, \"contributions\": {\"overlays\": [";
  /** Closes the overlays with a panel, so an overlay has a starter (manifest §3.5) and each case tests what it names. */
  static final String TAIL = "], \"panels\": [{\"id\": \"p\", \"title\": \"P\", \"root\": {\"kind\": \"label\", \"id\": \"l\"}}]}}";

  @Test
  void chunkedRefusesSegmentMib() {
    assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(
        HEAD + "{\"id\": \"a\", \"name\": \"a\", \"chunked\": true, \"time\": \"per_t\", \"segment_mib\": 8}" + TAIL));
  }

  @Test
  void timeNeedsChunked() {
    assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(
        HEAD + "{\"id\": \"a\", \"name\": \"a\", \"time\": \"static\"}" + TAIL));
    assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(
        HEAD + "{\"id\": \"a\", \"name\": \"a\", \"chunked\": true, \"time\": \"sometimes\"}" + TAIL));
    assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(
        HEAD + "{\"id\": \"a\", \"name\": \"a\", \"chunked\": \"yes\"}" + TAIL));
  }

  @Test
  void anOverlayNeedsAStarter() {
    ManifestException e = assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(
        HEAD + "{\"id\": \"a\", \"name\": \"a\", \"chunked\": true, \"time\": \"static\"}]}}"));
    assertTrue(e.getMessage().contains("nothing declared here starts it"), e.getMessage());
  }

  @Test
  void aValidChunkedOverlayParses() throws Exception {
    ManifestParser.parseDetached(HEAD + "{\"id\": \"a\", \"name\": \"a\", \"chunked\": true, \"time\": \"static\"}" + TAIL);
  }
}
