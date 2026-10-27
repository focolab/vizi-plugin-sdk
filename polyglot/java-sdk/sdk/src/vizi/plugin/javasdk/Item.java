package vizi.plugin.javasdk;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.ByteOrder;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Objects;
import java.util.OptionalInt;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.OverlayStyle;
import vizi.plugin.v1.ViziPlugin.OverlayTransform;

/**
 * One overlay item (control_plane_protocol.md §8.5): a marker set, a line set, a text label, a
 * mesh primitive or a label volume. Geometry is ZYX doubles; the SDK decides at push time whether
 * it rides the overlay's arena as a loan (the default) or inline ({@link #inline()}, required on
 * the live lane, and at most 8 MiB).
 *
 * <p>A label volume is built as a voxel selection by construction: voxel frame, an integer
 * position, unit scale, identity rotation.
 */
public final class Item {

  enum Kind {
    MARKER,
    LINE_SET,
    TEXT,
    MESH,
    LABEL_VOLUME
  }

  private static final ValueLayout.OfDouble F64 = ValueLayout.JAVA_DOUBLE_UNALIGNED.withOrder(ByteOrder.LITTLE_ENDIAN);

  final String id;
  final Kind kind;
  double @Nullable [] positions; // N*3, ZYX
  byte @Nullable [] colors; // N*4 RGBA u8
  @Nullable Volume labels;
  byte @Nullable [] lut; // 256*4 RGBA
  String text = "";
  double[] anchor = {0, 0, 0};
  String shape = "";
  String topology = "";
  String mesh = "";
  double sizePx;
  final OverlayTransform.Builder transform = OverlayTransform.newBuilder();
  final OverlayStyle.Builder style = OverlayStyle.newBuilder();
  OptionalInt t = OptionalInt.empty();
  final Map<Integer, String> names = new LinkedHashMap<>();
  final Map<Integer, Boolean> visible = new LinkedHashMap<>();
  boolean inline;
  int uses = 1;
  long geometryVersion;

  private Item(String id, Kind kind) {
    this.id = Objects.requireNonNull(id, "id");
    this.kind = kind;
  }

  /** Markers at {@code zyx} (flat triples). */
  public static Item markers(String id, double... zyx) {
    if (zyx.length % 3 != 0) throw new IllegalArgumentException("positions are (z, y, x) triples");
    Item i = new Item(id, Kind.MARKER);
    i.positions = zyx.clone();
    return i;
  }

  /**
   * An open polyline through {@code zyx} (flat triples), sent as the points themselves with the
   * default topology.
   */
  public static Item line(String id, double... zyx) {
    if (zyx.length % 3 != 0) throw new IllegalArgumentException("positions are (z, y, x) triples");
    Item i = new Item(id, Kind.LINE_SET);
    i.positions = zyx.clone();
    return i;
  }

  /**
   * Independent segments: each pair of points in {@code zyx} (flat triples) is one segment, sent
   * with topology {@code "segments"} (§8.5). The point count must be even.
   */
  public static Item segments(String id, double... zyx) {
    if (zyx.length % 3 != 0) throw new IllegalArgumentException("positions are (z, y, x) triples");
    if (zyx.length / 3 % 2 != 0) throw new IllegalArgumentException("segments are pairs of points, and " + zyx.length / 3 + " is odd");
    Item i = new Item(id, Kind.LINE_SET);
    i.positions = zyx.clone();
    i.topology = "segments";
    return i;
  }

  /** A closed polyline through {@code zyx} (flat triples). */
  public static Item loop(String id, double... zyx) {
    if (zyx.length % 3 != 0) throw new IllegalArgumentException("positions are (z, y, x) triples");
    Item i = new Item(id, Kind.LINE_SET);
    i.positions = zyx.clone();
    i.topology = "loop";
    return i;
  }

  /** A text label at {@code (z, y, x)}. */
  public static Item text(String id, String text, double z, double y, double x) {
    Item i = new Item(id, Kind.TEXT);
    i.text = Objects.requireNonNull(text, "text");
    i.anchor = new double[] {z, y, x};
    return i;
  }

  /** A sphere of {@code radius} (world units) centered at {@code (z, y, x)}. */
  public static Item sphere(String id, double z, double y, double x, double radius) {
    Item i = new Item(id, Kind.MESH);
    i.mesh = "sphere";
    i.transform.addPositionZyx(z).addPositionZyx(y).addPositionZyx(x).addScaleZyx(radius).addScaleZyx(radius).addScaleZyx(radius);
    return i;
  }

  /** A cube of edge {@code edge} (world units) centered at {@code (z, y, x)}. */
  public static Item cube(String id, double z, double y, double x, double edge) {
    Item i = new Item(id, Kind.MESH);
    i.mesh = "cube";
    i.transform.addPositionZyx(z).addPositionZyx(y).addPositionZyx(x).addScaleZyx(edge).addScaleZyx(edge).addScaleZyx(edge);
    return i;
  }

  /**
   * A label volume: {@code labels} (u8 or u16) placed with its {@code [0,0,0]} at the level-0
   * voxel {@code (z0, y0, x0)}, drawn with {@code lut}, a (256, 4) RGBA table (row 0 is the
   * background; label {@code id ≥ 1} uses row {@code 1 + (id − 1) mod 255}).
   */
  public static Item labels(String id, Volume labels, int z0, int y0, int x0, byte[] lut) {
    if (labels.dtype() != DType.U8 && labels.dtype() != DType.U16) throw new IllegalArgumentException("labels are u8 or u16");
    if (lut.length != 256 * 4) throw new IllegalArgumentException("the LUT is (256, 4) RGBA: 1024 bytes");
    Item i = new Item(id, Kind.LABEL_VOLUME);
    i.labels = labels;
    i.lut = lut.clone();
    i.transform.setFrame("voxel").addPositionZyx(z0).addPositionZyx(y0).addPositionZyx(x0);
    return i;
  }

  // ---- style and placement ----

  public Item color(int r, int g, int b, int a) {
    style.clearColorRgba().addColorRgba(r).addColorRgba(g).addColorRgba(b).addColorRgba(a);
    return this;
  }

  /** Per-vertex colors, RGBA u8, one per point (markers and lines). */
  public Item vertexColors(byte[] rgba) {
    colors = rgba.clone();
    return this;
  }

  /** Marker diameter: screen pixels in 3D, voxels in 2D. */
  public Item sizePx(double px) {
    sizePx = px;
    return this;
  }

  /** Text size in screen pixels. */
  public Item textSize(double px) {
    sizePx = px;
    return this;
  }

  public Item thickness(double px) {
    style.setThickness(px);
    return this;
  }

  public Item opacity(double o) {
    style.setOpacity(o);
    return this;
  }

  public Item hidden(boolean h) {
    style.setHidden(h);
    return this;
  }

  public Item depthTest(boolean d) {
    style.setDepthTest(d);
    return this;
  }

  /** Higher draws later, on top (see {@link ChunkedOverlay.LabelTable#drawPriority}). */
  public Item drawPriority(int p) {
    style.setDrawPriority(p);
    return this;
  }

  /** "disc" or "square". */
  public Item markerShape(String s) {
    shape = s;
    return this;
  }

  /** Coordinates are level-0 voxels ("voxel") rather than world ("world", the default). */
  public Item voxelFrame() {
    transform.setFrame("voxel");
    return this;
  }

  /** Shown at one timepoint only (label volumes). */
  public Item at(int t) {
    this.t = OptionalInt.of(t);
    return this;
  }

  /** A label's name, keyed by label id. */
  public Item name(int labelId, String name) {
    names.put(labelId, name);
    return this;
  }

  /** A label's visibility in a compile, keyed by label id. */
  public Item visible(int labelId, boolean v) {
    visible.put(labelId, v);
    return this;
  }

  /** Carry the geometry inline rather than as a loan: required on the live lane. */
  public Item inline() {
    inline = true;
    return this;
  }

  /**
   * How many messages will name this item's pushed loans (control_plane_protocol.md §5.6), default
   * 1. {@link Host#compileLabelVolume} lends the item's geometry AFRESH, named by the compile
   * alone (§8.8's route 2), so a label volume compiled through it keeps the default: {@code uses}
   * is not the compile's, and 2 here would pin the push's loans.
   */
  public Item uses(int n) {
    if (n < 1) throw new IllegalArgumentException("uses is at least 1");
    uses = n;
    return this;
  }

  public Item geometryVersion(long v) {
    geometryVersion = v;
    return this;
  }

  public String id() {
    return id;
  }

  /** The positions as little-endian bytes. */
  static MemorySegment f64Bytes(double[] v) {
    MemorySegment m = java.lang.foreign.Arena.ofAuto().allocate(Math.max(8, 8L * v.length), 8);
    for (int i = 0; i < v.length; i++) m.setAtIndex(F64, i, v[i]);
    return m.asSlice(0, 8L * v.length);
  }
}
