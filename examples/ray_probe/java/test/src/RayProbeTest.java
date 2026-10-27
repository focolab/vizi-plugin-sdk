import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.OptionalDouble;
import javax.xml.parsers.DocumentBuilderFactory;
import org.junit.jupiter.api.Test;
import pois_math.v1.RoiOuterClass.Roi;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.ManifestDiff;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/** The pick rules, the ellipsoid, the words, the PNG and the manifest, offline. */
class RayProbeTest {

  static Path dir() {
    String d = System.getenv("RAY_PROBE_DIR");
    return d != null ? Path.of(d) : Path.of("").toAbsolutePath();
  }

  // ---- the argmax and both tie rules ----

  @Test
  void firstMaxAlongTheRay() {
    assertEquals(new Picking.Best(1, 5.0), Picking.firstMax(new double[] {1, 5, 3, 5}));
    assertEquals(new Picking.Best(2, 7.0), Picking.firstMax(new double[] {Double.NaN, 1, 7, Double.NaN}));
    // -inf is a value; NaN is skipped
    assertEquals(new Picking.Best(1, Double.NEGATIVE_INFINITY),
        Picking.firstMax(new double[] {Double.NaN, Double.NEGATIVE_INFINITY, Double.NEGATIVE_INFINITY}));
    assertEquals(new Picking.Best(2, Double.POSITIVE_INFINITY), Picking.firstMax(new double[] {3, 1e300, Double.POSITIVE_INFINITY}));
    assertNull(Picking.firstMax(new double[] {Double.NaN, Double.NaN}));
    assertNull(Picking.firstMax(new double[0]));
  }

  @Test
  void blockMaxTiesToZThenYThenX() {
    // (2, 2, 2): value 9 at (0,1,1), (1,0,0) and (1,1,0) -> the smallest z wins: (0,1,1)
    float[] v = {1, 2, 3, 9, 9, 4, 9, 5};
    Picking.Peak p = Picking.blockMax(Volume.f32(2, 2, 2, v), new int[] {10, 20, 30});
    assertEquals(new Picking.Peak(10, 21, 31, 9.0), p);
    // same z: the smallest y; same y: the smallest x
    float[] w = {0, 0, 0, 0, 1, 8, 8, 8};
    assertEquals(new Picking.Peak(1, 0, 1, 8.0), Picking.blockMax(Volume.f32(2, 2, 2, w), new int[] {0, 0, 0}));
    float[] nan = {Float.NaN, Float.NaN, Float.NEGATIVE_INFINITY, Float.NaN};
    assertEquals(new Picking.Peak(0, 1, 0, Double.NEGATIVE_INFINITY), Picking.blockMax(Volume.f32(1, 2, 2, nan), new int[] {0, 0, 0}));
    assertNull(Picking.blockMax(Volume.f32(1, 1, 2, new float[] {Float.NaN, Float.NaN}), new int[] {0, 0, 0}));
  }

  @Test
  void distancesAndDecimation() {
    List<Lattice.Cell> cells = List.of(new Lattice.Cell(0, 0, 0), new Lattice.Cell(1, 0, 0), new Lattice.Cell(1, 1, 0));
    assertArrayEquals(new double[] {0, 1, 2}, Picking.distances(cells, null), 1e-12);
    assertArrayEquals(new double[] {0, 2, 2.5}, Picking.distances(cells, new double[] {2, 0.5, 0.5}), 1e-12);
    assertEquals(List.of(0, 1, 2), Picking.sampleIndices(3, 1024));
    List<Integer> d = Picking.sampleIndices(3000, 1024);
    assertTrue(d.size() <= 1025 && d.get(0) == 0 && d.get(d.size() - 1) == 2999);
  }

  // ---- the ellipsoid (peak_roi/SPEC.md §"Building the ellipsoid Roi") ----

  @Test
  void ellipsoidStructure() {
    Roi r = Rois.ellipsoid(18, 22, 42, 2, 2, 2);
    assertEquals("ellipsoid", r.getTypeName());
    assertEquals(0, r.getRoot());
    assertEquals(4, r.getVerticesCount());
    double[][] want = {{18, 22, 42}, {20, 22, 42}, {18, 24, 42}, {18, 22, 44}};
    for (int i = 0; i < 4; i++) {
      var p = r.getVertices(i).getPosition();
      assertArrayEquals(want[i], new double[] {p.getZ(), p.getY(), p.getX()}, 0);
    }
    assertEquals(List.of(0, 1, 2, 3), r.getCompile().getControlsList());
    double[][] display = Rois.compileDisplay(r);
    assertEquals(18, display.length);
    // the six tips are c ± r on each axis
    for (int i = 0; i < 6; i++) {
      double dist = 0;
      for (int k = 0; k < 3; k++) dist += Math.abs(display[i][k] - want[0][k]);
      assertEquals(2.0, dist, 1e-12);
    }
    assertEquals(12, r.getEdgesCount());
    assertTrue(!r.hasTStart() && !r.hasTEnd(), "a pick's ROI is timeless");
  }

  // ---- the words (the pinned forms) ----

  @Test
  void pinnedForms() {
    assertEquals("No picks", RayProbe.countText(0));
    assertEquals("1 pick", RayProbe.countText(1));
    assertEquals("2 picks", RayProbe.countText(2));
    RayProbe.Pick p = new RayProbe.Pick(1, new double[] {18, 22, 42}, 2901, 2, OptionalDouble.of(2.5), 0, 0, false);
    assertEquals("Pick 1: (18, 22, 42) = (36, 11, 21) um, value 2901, c=0, t=2 (2.5 s)", p.label(new double[] {2, 0.5, 0.5}, "um"));
    RayProbe.Pick q = new RayProbe.Pick(3, new double[] {17.5, 21.5, 41.5}, 2537, 0, OptionalDouble.empty(), 1, 2, false);
    assertEquals("Pick 3: (17.5, 21.5, 41.5), value 2537, c=1, t=0", q.label(null, ""));
  }

  @Test
  void keysFromTheBinding() {
    var b = vizi.plugin.v1.ViziPlugin.ToolBinding.newBuilder()
        .addTriggers(vizi.plugin.v1.ViziPlugin.Trigger.newBuilder().setKey("Shift+P"))
        .addTriggers(vizi.plugin.v1.ViziPlugin.Trigger.newBuilder().setButton(vizi.plugin.v1.ViziPlugin.MouseButton.MOUSE_BUTTON_MIDDLE))
        .addActions(vizi.plugin.v1.ViziPlugin.ToolAction.newBuilder().setAction(1).setActionId("undo").setKey("U"))
        .addActions(vizi.plugin.v1.ViziPlugin.ToolAction.newBuilder().setAction(2).setActionId("clear").setKey("X"));
    assertEquals("Shift+P toggles, middle button holds; U undoes, X clears", RayProbe.keysOf(b.build()));
    // the key trigger refused and clear's key withheld: only what the host honored is said
    b.removeTriggers(0).setActions(1, vizi.plugin.v1.ViziPlugin.ToolAction.newBuilder().setAction(2).setActionId("clear").setKey(""));
    assertEquals("middle button holds; U undoes", RayProbe.keysOf(b.build()));
    assertEquals("", RayProbe.keysOf(vizi.plugin.v1.ViziPlugin.ToolBinding.getDefaultInstance()));
  }

  @Test
  void png() {
    byte[] png = Plot.png(Plot.render(new double[] {1, 3, 2, Double.NaN, 5}, new double[] {0, 1, 2, 3, 4}, 4));
    assertArrayEquals(new byte[] {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}, java.util.Arrays.copyOf(png, 8));
    assertEquals(Plot.WIDTH, java.nio.ByteBuffer.wrap(png, 16, 4).getInt());
    assertEquals(Plot.HEIGHT, java.nio.ByteBuffer.wrap(png, 20, 4).getInt());
  }

  // ---- the manifest and the icons ----

  @Test
  void manifestMatchesTheCodeAndValidates() throws Exception {
    Manifest declared = new RayProbe().plugin().manifest();
    assertEquals(1, declared.manifestVersion());
    Path d = dir();
    var onDisk = ManifestParser.read(d); // the host's reader, icons included
    assertEquals(List.of(), ManifestDiff.differences(declared, onDisk.manifest()));
    assertEquals(ManifestWriter.write(declared), Files.readString(d.resolve("vizi-plugin.json")));
    String json = Files.readString(d.resolve("vizi-plugin.json"));
    assertTrue(json.contains("\"manifest_version\": 1"));
    assertTrue(json.contains("\"vizi\": \"1.0\""));
    for (String icon : List.of("icons/probe.svg", "icons/undo.svg", "icons/clear.svg")) {
      Path p = d.resolve(icon);
      assertTrue(Files.size(p) <= 64 << 10, icon);
      var doc = DocumentBuilderFactory.newInstance().newDocumentBuilder().parse(p.toFile());
      assertEquals("svg", doc.getDocumentElement().getTagName(), icon);
    }
  }
}
