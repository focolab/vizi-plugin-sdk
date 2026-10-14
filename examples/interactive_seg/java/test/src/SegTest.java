import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.google.protobuf.ByteString;
import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.zip.CRC32;
import java.util.zip.Inflater;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.ManifestDiff;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.GeomBuffer;
import vizi.plugin.v1.ViziPlugin.OverlayItem;
import vizi.plugin.v1.ViziPlugin.OverlayMarker;
import vizi.plugin.v1.ViziPlugin.OverlayStyle;
import vizi.plugin.v1.ViziPlugin.OverlayTransform;
import vizi.plugin.v1.ViziPlugin.OverlayUpdate;

/**
 * The offline checks, against expectations worked
 * by hand (in the comments), not computed by the code under test.
 */
class SegTest {

  static final double INF = Double.POSITIVE_INFINITY;
  static final double NAN = Double.NaN;

  // ---- helpers ----

  static boolean[] select(double[][] yx, int h, int w) {
    double[] ys = new double[yx.length], xs = new double[yx.length];
    for (int i = 0; i < yx.length; i++) {
      ys[i] = yx[i][0];
      xs[i] = yx[i][1];
    }
    return Lasso.selectedColumns(ys, xs, h, w);
  }

  static int count(boolean[] m) {
    int n = 0;
    for (boolean b : m) if (b) n++;
    return n;
  }

  static int rowCount(boolean[] m, int w, int y) {
    int n = 0;
    for (int x = 0; x < w; x++) if (m[y * w + x]) n++;
    return n;
  }

  /** A (d, h, w) f64 window from explicit voxels over a background. */
  static Volume window(int d, int h, int w, double bg, double[]... voxels) {
    double[] v = new double[d * h * w];
    Arrays.fill(v, bg);
    for (double[] p : voxels) v[((int) p[0] * h + (int) p[1]) * w + (int) p[2]] = p[3];
    return Volume.f64(d, h, w, v);
  }

  static boolean[] all(int n) {
    boolean[] b = new boolean[n];
    Arrays.fill(b, true);
    return b;
  }

  static final boolean[][] ALL_FACES = {{true, true}, {true, true}, {true, true}};

  // ---- §4: point in polygon ----

  @Test
  void convexSquare() {
    // vertices on integers: centres y + 0.5 in (2, 6) and x + 0.5 in (2, 6): y, x in 2..5
    boolean[] m = select(new double[][] {{2, 2}, {2, 6}, {6, 6}, {6, 2}}, 10, 10);
    assertEquals(16, count(m));
    for (int y = 2; y < 6; y++) for (int x = 2; x < 6; x++) assertTrue(m[y * 10 + x]);
  }

  @Test
  void nonConvexU() {
    // a U: the 8x8 block at (1..9) minus the notch y in [1, 6), x in [4, 6)
    boolean[] m = select(new double[][] {{1, 1}, {1, 4}, {6, 4}, {6, 6}, {1, 6}, {1, 9}, {9, 9}, {9, 1}}, 12, 12);
    assertEquals(64 - 10, count(m));
    assertFalse(m[3 * 12 + 4]); // in the notch
    assertFalse(m[5 * 12 + 5]);
    assertTrue(m[6 * 12 + 4]); // under the notch
    assertTrue(m[3 * 12 + 3]); // left arm
    assertTrue(m[3 * 12 + 6]); // right arm
  }

  @Test
  void selfIntersectingBowTie() {
    // (0,0) → (0,8) → (8,0) → (8,8): two triangles meeting at (4, 4). Row y's crossings are
    // py and 8 − py; a column is in when exactly one of them lies right of px.
    boolean[] m = select(new double[][] {{0, 0}, {0, 8}, {8, 0}, {8, 8}}, 8, 8);
    int[] rows = new int[8];
    for (int y = 0; y < 8; y++) rows[y] = rowCount(m, 8, y);
    assertArrayEquals(new int[] {7, 5, 3, 1, 1, 3, 5, 7}, rows);
    assertTrue(m[0 * 8 + 0]); // the crossing at 0.5 is not right of px = 0.5, the one at 7.5 is
    assertFalse(m[0 * 8 + 7]);
    assertTrue(m[3 * 8 + 3] && m[4 * 8 + 3]);
    assertFalse(m[4 * 8 + 4]);
  }

  @Test
  void pentagramHasAnEmptyCore() {
    // a five-pointed star drawn as one loop, radius 10 about (20, 20): even-odd leaves its
    // pentagon out and keeps the tips
    double[][] v = new double[5][];
    for (int i = 0; i < 5; i++) {
      double a = Math.toRadians(-90 + 144 * i);
      v[i] = new double[] {20 + 10 * Math.sin(a), 20 + 10 * Math.cos(a)};
    }
    boolean[] m = select(v, 40, 40);
    assertFalse(m[19 * 40 + 19], "the core is wound twice: outside");
    assertFalse(m[20 * 40 + 20]);
    assertTrue(m[12 * 40 + 19] && m[12 * 40 + 20], "the top tip is wound once: inside");
  }

  @Test
  void centreOnAnEdge() {
    // edges through voxel centres: top and left count, bottom and right do not
    boolean[] m = select(new double[][] {{2.5, 2.5}, {2.5, 5.5}, {5.5, 5.5}, {5.5, 2.5}}, 10, 10);
    assertEquals(9, count(m));
    for (int y = 2; y < 5; y++) for (int x = 2; x < 5; x++) assertTrue(m[y * 10 + x]);
  }

  @Test
  void loopLeavingTheVolume() {
    boolean[] m = select(new double[][] {{-5, -5}, {-5, 5}, {5, 5}, {5, -5}}, 10, 10);
    assertEquals(25, count(m)); // y, x in 0..4
    assertEquals(0, count(select(new double[][] {{-9, -9}, {-9, -2}, {-2, -2}, {-2, -9}}, 10, 10)));
  }

  @Test
  void degeneratePathsSelectNothing() {
    assertEquals(0, count(select(new double[][] {{3, 3}, {3, 3}}, 10, 10))); // a bare click
    assertEquals(0, count(select(new double[][] {{3, 3}, {3, 7}, {3, 3}, {3, 7}}, 10, 10))); // back and forth
  }

  // ---- §2, §5: decimation and the search window ----

  @Test
  void decimation() {
    assertArrayEquals(new int[] {0}, Lasso.decimate(1, 512));
    assertArrayEquals(new int[] {0, 1, 2, 3, 4}, Lasso.decimate(5, 512));
    assertArrayEquals(new int[] {0, 2, 5}, Lasso.decimate(6, 3)); // 2.5 → 2, half to even (Math.round says 3)
    int[] d = Lasso.decimate(1000, 512);
    assertEquals(512, d.length);
    assertEquals(0, d[0]);
    assertEquals(2, d[1]); // 999 / 511 = 1.955
    assertEquals(999, d[511]);
    assertEquals(0, Lasso.decimate(0, 512).length);
  }

  @Test
  void searchWindow() {
    boolean[] sel = new boolean[100 * 100];
    sel[10 * 100 + 20] = sel[19 * 100 + 29] = true; // bbox y [10, 20), x [20, 30): margins 8
    Lasso.Window w = Lasso.searchWindow(sel, 12, 100, 100);
    assertArrayEquals(new int[] {0, 2, 12}, w.lo());
    assertArrayEquals(new int[] {12, 28, 38}, w.hi());
    // extent 42 → 0.25 × 42 = 10.5 → 10 (half to even), extent 34 → 8.5 → 8
    boolean[] wide = new boolean[100 * 100];
    wide[30 * 100 + 30] = wide[71 * 100 + 63] = true; // y [30, 72) = 42, x [30, 64) = 34
    w = Lasso.searchWindow(wide, 12, 100, 100);
    assertArrayEquals(new int[] {0, 20, 22}, w.lo());
    assertArrayEquals(new int[] {12, 82, 72}, w.hi());
    // clipped to the volume
    boolean[] corner = new boolean[20 * 20];
    corner[0] = true;
    w = Lasso.searchWindow(corner, 3, 20, 20);
    assertArrayEquals(new int[] {0, 0, 0}, w.lo());
    assertArrayEquals(new int[] {3, 9, 9}, w.hi());
  }

  // ---- §6: the threshold ----

  @Test
  void medianOddAndEven() {
    // one plane, three columns 1, 5, 9: median 5; with scale 1.5 → 7.5
    Volume w = Volume.f64(1, 1, 3, new double[] {1, 5, 9});
    assertEquals(7.5, Grow.threshold(w, all(3), 1.5));
    // four columns: (3 + 5) / 2
    assertEquals(4.0, Grow.threshold(Volume.f64(1, 1, 4, new double[] {9, 1, 5, 3}), all(4), 1.0));
    // the column maximum is over z: planes (1, 2) and (7, 0) → maxima 7, 2 → 4.5
    assertEquals(4.5, Grow.threshold(Volume.f64(2, 1, 2, new double[] {1, 2, 7, 0}), all(2), 1.0));
    // only selected columns count
    assertEquals(9.0, Grow.threshold(Volume.f64(1, 1, 3, new double[] {1, 5, 9}), new boolean[] {false, false, true}, 1.0));
  }

  @Test
  void nonFinitePolicy() {
    // a NaN or −∞ voxel never contributes; an all-NaN/−∞ column is dropped; +∞ contributes
    assertEquals(2.0, Grow.threshold(Volume.f64(2, 1, 2, new double[] {NAN, 3, 1, NAN}), all(2), 1.0)); // maxima 1, 3
    assertEquals(4.0, Grow.threshold(Volume.f64(2, 1, 3, new double[] {NAN, 2, 6, Double.NEGATIVE_INFINITY, 1, 0}), all(3), 1.0)); // [dropped], 2, 6
    assertEquals(INF, Grow.threshold(Volume.f64(1, 1, 2, new double[] {Double.NEGATIVE_INFINITY, INF}), all(2), 1.0)); // only +∞ counts
    assertEquals(INF, Grow.threshold(Volume.f64(1, 1, 2, new double[] {5, INF}), all(2), 1.0)); // (5 + ∞) / 2
    Grow.Refused r = assertThrows(Grow.Refused.class,
        () -> Grow.threshold(Volume.f64(2, 1, 1, new double[] {NAN, Double.NEGATIVE_INFINITY}), all(1), 1.0));
    assertEquals("Nothing measurable under the loop.", r.getMessage());
  }

  @Test
  void nonFiniteVoxelsInTheGrowth() {
    // T = +∞: the +∞ voxels are the whole foreground; NaN is background
    Volume w = Volume.f64(1, 1, 4, new double[] {INF, INF, NAN, INF});
    Grow.Grown g = Grow.grow(w, all(4), 0, INF, true, 1, ALL_FACES);
    assertEquals(2, g.size()); // {0, 1}; 3 is cut off by the NaN
    assertTrue(g.component()[0] && g.component()[1] && !g.component()[2] && !g.component()[3]);
  }

  // ---- §7, §8: components, the anchor, the choice ----

  @Test
  void faceConnectivity() {
    List<Integer> sizes = new ArrayList<>();
    // corner-touching voxels (0,0,0) and (1,1,1): two components under 6-connectivity
    boolean[] corner = new boolean[8];
    corner[0] = corner[7] = true;
    int[] l = Grow.components(corner, 2, 2, 2, sizes);
    assertEquals(3, sizes.size());
    assertEquals(1, l[0]);
    assertEquals(2, l[7]);
    // edge-touching (0,0,0) and (0,1,1): two
    boolean[] edge = new boolean[8];
    edge[0] = edge[3] = true;
    Grow.components(edge, 2, 2, 2, sizes);
    assertEquals(3, sizes.size());
    // face-touching (0,0,0) and (1,0,0): one of two voxels
    boolean[] face = new boolean[8];
    face[0] = face[4] = true;
    Grow.components(face, 2, 2, 2, sizes);
    assertEquals(List.of(0, 2), sizes);
    // in a plane (4-connectivity): diagonal neighbours are two components
    boolean[] diag = {true, false, false, true};
    Grow.components(diag, 1, 2, 2, sizes);
    assertEquals(List.of(0, 1, 1), sizes);
  }

  @Test
  void longThinObject() {
    // a 1-voxel-wide serpentine through a 1 × 9 × 9 plane is one component
    boolean[] m = new boolean[81];
    for (int y = 0; y < 9; y += 2) for (int x = 0; x < 9; x++) m[y * 9 + x] = true;
    for (int y = 1; y < 9; y += 2) m[y * 9 + ((y / 2) % 2 == 0 ? 8 : 0)] = true;
    List<Integer> sizes = new ArrayList<>();
    Grow.components(m, 1, 9, 9, sizes);
    assertEquals(List.of(0, 45 + 4), sizes);
  }

  @Test
  void largestWinsOverTheSeedsComponent() {
    // plane 0 of a 1 × 3 × 8 window: a 1-voxel spot at x = 0 and a 5-voxel bar at x = 3..7 on
    // row 1; the lasso covers both. The brightest (the seed a peak rule would pick) is the spot.
    Volume w = window(1, 3, 8, 0,
        new double[] {0, 1, 0, 90},
        new double[] {0, 1, 3, 50}, new double[] {0, 1, 4, 50}, new double[] {0, 1, 5, 50}, new double[] {0, 1, 6, 50},
        new double[] {0, 1, 7, 50});
    Grow.Grown g = Grow.grow(w, all(24), 0, 40, true, 1, ALL_FACES);
    assertEquals(5, g.size());
    assertFalse(g.component()[8]);
    assertTrue(g.component()[8 + 3]);
  }

  @Test
  void tiesGoToTheLowestRasterVoxel() {
    // two 2-voxel components on row 0 (x 0..1) and row 2 (x 0..1): the first in raster order wins
    Volume w = window(1, 3, 2, 0,
        new double[] {0, 2, 0, 9}, new double[] {0, 2, 1, 9}, new double[] {0, 0, 0, 9}, new double[] {0, 0, 1, 9});
    Grow.Grown g = Grow.grow(w, all(6), 0, 5, true, 1, ALL_FACES);
    assertTrue(g.component()[0] && g.component()[1] && !g.component()[4]);
  }

  @Test
  void anchorRefusesAComponentOffTheDrawnPlane() {
    // a bright voxel on plane 0 under the loop, drawn on plane 2: nothing there is above T
    Volume w = window(3, 2, 2, 0, new double[] {0, 1, 1, 100}, new double[] {0, 0, 0, 100});
    Grow.Refused r = assertThrows(Grow.Refused.class, () -> Grow.grow(w, all(4), 2, 50, true, 1, ALL_FACES));
    assertEquals("Nothing above the threshold on plane 2. The brightest voxel you enclosed is at z=0 — scroll there, or lower"
        + " Threshold ×.", r.getMessage());
    // the same component reaching plane 2 through z is accepted, whole
    Volume reach = window(3, 2, 2, 0, new double[] {0, 1, 1, 100}, new double[] {1, 1, 1, 100}, new double[] {2, 1, 1, 100});
    assertEquals(3, Grow.grow(reach, all(4), 2, 50, true, 1, ALL_FACES).size());
    // …and requires the drawn-plane voxel to be under the loop
    boolean[] notThere = {true, true, true, false};
    assertThrows(Grow.Refused.class, () -> Grow.grow(reach, notThere, 2, 50, true, 1, ALL_FACES));
  }

  @Test
  void diagnosticTieBreak() {
    // two maxima of 7: (0, 1, 1) and (1, 0, 0); raster order puts (0, 1, 1) first
    Volume w = window(2, 2, 2, 0, new double[] {1, 0, 0, 7}, new double[] {0, 1, 1, 7});
    assertArrayEquals(new int[] {0, 1, 1}, Grow.diagnostic(w, all(4)));
    // an unselected brighter column does not count
    Volume v = window(1, 1, 2, 0, new double[] {0, 0, 0, 9}, new double[] {0, 0, 1, 3});
    assertArrayEquals(new int[] {0, 0, 1}, Grow.diagnostic(v, new boolean[] {false, true}));
  }

  @Test
  void singlePlaneMode() {
    // a 3-voxel column through z; grow3d off keeps the drawn plane's voxel only
    Volume w = window(3, 1, 1, 0, new double[] {0, 0, 0, 9}, new double[] {1, 0, 0, 9}, new double[] {2, 0, 0, 9});
    Grow.Grown g = Grow.grow(w, all(1), 1, 5, false, 1, ALL_FACES);
    assertEquals(1, g.size());
    assertArrayEquals(new boolean[] {false, true, false}, g.component());
  }

  @Test
  void minimumSize() {
    Volume w = window(1, 1, 3, 0, new double[] {0, 0, 0, 9}, new double[] {0, 0, 1, 9});
    Grow.Refused r = assertThrows(Grow.Refused.class, () -> Grow.grow(w, all(3), 0, 5, true, 3, ALL_FACES));
    assertEquals("No component of at least 3 voxels.", r.getMessage());
    assertEquals(2, Grow.grow(w, all(3), 0, 5, true, 2, ALL_FACES).size());
  }

  @Test
  void clipReport() {
    // the component touches the window's x = 0 face: a clip unless that face is the volume's
    Volume w = window(1, 1, 3, 0, new double[] {0, 0, 0, 9});
    assertFalse(Grow.grow(w, all(3), 0, 5, true, 1, ALL_FACES).clipped());
    boolean[][] innerLeft = {{true, true}, {true, true}, {false, true}};
    assertTrue(Grow.grow(w, all(3), 0, 5, true, 1, innerLeft).clipped());
    boolean[][] innerRight = {{true, true}, {true, true}, {true, false}};
    assertFalse(Grow.grow(w, all(3), 0, 5, true, 1, innerRight).clipped());
  }

  // ---- §9: the label map, the LUT, the item ----

  @Test
  void lut() {
    byte[] l = Grow.lut();
    assertEquals(1024, l.length);
    assertArrayEquals(new byte[] {0, 0, 0, 0}, Arrays.copyOfRange(l, 0, 4));
    assertArrayEquals(new byte[] {(byte) 230, 25, 75, (byte) 180}, Arrays.copyOfRange(l, 4, 8));
    assertArrayEquals(new byte[] {(byte) 191, (byte) 239, 69, (byte) 180}, Arrays.copyOfRange(l, 32, 36)); // row 8
    assertArrayEquals(new byte[] {(byte) 230, 25, 75, (byte) 180}, Arrays.copyOfRange(l, 36, 40)); // row 9 wraps
    assertArrayEquals(new byte[] {(byte) 240, 50, (byte) 230, (byte) 180}, Arrays.copyOfRange(l, 1020, 1024)); // row 255: (254 % 8) = 6
  }

  @Test
  void labelMapNewerWinsAndIsTight() {
    Grow.LabelMap m = new Grow.LabelMap();
    // label 1: a 1 × 1 × 3 bar at (2, 5, 10); label 2: a 1 × 1 × 2 bar at (2, 5, 12) overlapping its end
    m.add(new Grow.Placed(new int[] {2, 5, 10}, new int[] {1, 1, 3}, new boolean[] {true, true, true}));
    m.add(new Grow.Placed(new int[] {2, 5, 12}, new int[] {1, 1, 2}, new boolean[] {true, true}));
    Grow.Rendered r = m.render();
    assertArrayEquals(new int[] {2, 5, 10}, r.origin());
    assertArrayEquals(new int[] {1, 1, 4}, r.shape());
    assertArrayEquals(new byte[] {1, 1, 2, 2}, r.labels());
    // a component with an empty border is placed tight
    Grow.Placed t = Grow.tight(new boolean[] {false, false, false, true}, 1, 2, 2, new int[] {4, 6, 8});
    assertArrayEquals(new int[] {4, 7, 9}, t.origin());
    assertArrayEquals(new int[] {1, 1, 1}, t.shape());
  }

  @Test
  void labelMapTightAfterANewerLabelCoversAnEdge() {
    Grow.LabelMap m = new Grow.LabelMap();
    m.add(new Grow.Placed(new int[] {0, 0, 0}, new int[] {1, 1, 2}, new boolean[] {true, true}));
    m.add(new Grow.Placed(new int[] {0, 0, 0}, new int[] {1, 1, 2}, new boolean[] {true, true}));
    Grow.Rendered r = m.render();
    assertArrayEquals(new byte[] {2, 2}, r.labels());
  }

  @Test
  void twoHundredFiftyFiveLabelsAtMost() {
    Grow.LabelMap m = new Grow.LabelMap();
    for (int i = 0; i < 255; i++) m.add(new Grow.Placed(new int[] {0, 0, i}, new int[] {1, 1, 1}, new boolean[] {true}));
    Grow.Refused r = assertThrows(Grow.Refused.class, () -> m.add(new Grow.Placed(new int[] {0, 0, 0}, new int[] {1, 1, 1}, new boolean[] {true})));
    assertTrue(r.getMessage().startsWith("At most 255 labels"));
    Grow.Rendered out = m.render();
    assertEquals(255, out.shape()[2]);
    assertEquals((byte) 255, out.labels()[254]);
  }

  // ---- §2: the live payload against the slot ----

  @Test
  void a512PointTrailFitsTheEmulatorsSlot() {
    // The OverlayUpdate the lane carries for the longest preview, built from the generated types
    // as the SDK builds it (Overlay.materialize: inline f64 (N, 3), size_px, colour, voxel frame).
    // The SDK exposes no slot capacity to the author, so this is measured against the grant the
    // emulator makes (16 KiB slots) less the 8-byte pane header.
    ByteBuffer b = ByteBuffer.allocate(512 * 24).order(ByteOrder.LITTLE_ENDIAN);
    for (int i = 0; i < 512 * 3; i++) b.putDouble(1000.0 + i * 0.37);
    OverlayUpdate u = OverlayUpdate.newBuilder().setOverlayId("seg.trail").setRevision(123456)
        .addItems(OverlayItem.newBuilder().setItemId("trail")
            .setTransform(OverlayTransform.newBuilder().setFrame("voxel"))
            .setStyle(OverlayStyle.newBuilder().addColorRgba(255).addColorRgba(255).addColorRgba(255).addColorRgba(220))
            .setMarker(OverlayMarker.newBuilder().setSizePx(5)
                .setPositionsZyx(GeomBuffer.newBuilder().addShape(512).addShape(3).setDtype(DType.DTYPE_F64)
                    .setInline(ByteString.copyFrom(b.array())))))
        .build();
    int bytes = u.getSerializedSize();
    System.out.println("a 512-point trail encodes to " + bytes + " bytes; the emulator's lane holds " + (16 * 1024 - 8));
    assertTrue(bytes > 512 * 24 && bytes < 16 * 1024 - 8, bytes + " bytes");
  }

  // ---- the plot ----

  @Test
  void pngIsWellFormed() throws Exception {
    byte[] png = Plot.png(Plot.render(new double[] {1040, 1080, 1120}, 240, 140));
    assertArrayEquals(new byte[] {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'}, Arrays.copyOf(png, 8));
    ByteBuffer bb = ByteBuffer.wrap(png);
    int pos = 8;
    ByteArrayOutputStream idat = new ByteArrayOutputStream();
    List<String> kinds = new ArrayList<>();
    int width = 0, height = 0;
    while (pos < png.length) {
      int len = bb.getInt(pos);
      String kind = new String(png, pos + 4, 4, java.nio.charset.StandardCharsets.US_ASCII);
      CRC32 crc = new CRC32();
      crc.update(png, pos + 4, 4 + len);
      assertEquals((int) crc.getValue(), bb.getInt(pos + 8 + len), kind + " CRC");
      if (kind.equals("IHDR")) {
        width = bb.getInt(pos + 8);
        height = bb.getInt(pos + 12);
      }
      if (kind.equals("IDAT")) idat.write(png, pos + 8, len);
      kinds.add(kind);
      pos += 12 + len;
    }
    assertEquals(List.of("IHDR", "IDAT", "IEND"), kinds);
    assertEquals(240, width);
    assertEquals(140, height);
    Inflater inf = new Inflater();
    inf.setInput(idat.toByteArray());
    byte[] raw = new byte[140 * 241 + 1];
    assertEquals(140 * 241, inf.inflate(raw));
    // three points, the highest top-left of the plot area and the lowest at the bottom
    Plot.Canvas c = Plot.render(new double[] {1040, 1080, 1120}, 240, 140);
    assertEquals(255, c.get(122, 28)); // (t = 0, lowest) at (bottom, left)
    assertEquals(255, c.get(8, 232)); // (t = 2, highest) at (top, right)
    assertEquals("1120", Plot.num(1120));
    assertEquals("1.23e5", Plot.num(123456));
    assertEquals("0.25", Plot.num(0.25));
    assertEquals("-3e-6", Plot.num(-3e-6)); // "-3.5e-7" would be 7 characters
  }

  // ---- the manifest ----

  @Test
  void manifestOnDiskIsTheCodes() throws Exception {
    Path dir = Path.of(System.getenv("PLUGIN_DIR")).getParent();
    var onDisk = ManifestParser.read(dir).manifest();
    var declared = new InteractiveSeg().plugin().manifest();
    assertEquals(List.of(), ManifestDiff.differences(declared, onDisk));
    String json = Files.readString(dir.resolve("vizi-plugin.json"));
    assertTrue(json.contains("\"vizi\": \"1.0\""));
    assertEquals(9, json.split("\"menu\": null", -1).length - 1, "every command has \"menu\": null");
  }
}
