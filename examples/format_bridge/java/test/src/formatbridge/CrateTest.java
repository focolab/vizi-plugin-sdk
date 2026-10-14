package formatbridge;

import static org.junit.jupiter.api.Assertions.assertArrayEquals;
import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertNotEquals;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.OptionalDouble;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import vizi.plugin.javasdk.ChannelName;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Dataset;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Exporter;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.control.Environment;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/** The offline checks, against the committed fixtures. */
class CrateTest {

  static final Path HERE = Path.of(System.getenv("FORMAT_BRIDGE_DIR"));
  static final Path FIXTURES = HERE.resolve("../fixtures").normalize();
  static final Path INTEROP = HERE.resolve("../interop").normalize();

  @TempDir Path tmp;

  static Path fixture(String name) {
    return FIXTURES.resolve(name);
  }

  /** SPEC.md's formula, written here and nowhere else. */
  static int expected(int t, int c, int z, int y, int x) {
    return 10000 * t + 1000 * c + 100 * z + 10 * y + x;
  }

  static void assertSample(Volume v, int t, int c) {
    assertEquals(4, v.z());
    assertEquals(6, v.y());
    assertEquals(5, v.x());
    assertEquals(DType.U16, v.dtype());
    for (int z = 0; z < 4; z++) {
      for (int y = 0; y < 6; y++) {
        for (int x = 0; x < 5; x++) assertEquals(expected(t, c, z, y, x), v.u16(v.index(z, y, x)), "(t=" + t + ", c=" + c + ") at " + z + "," + y + "," + x);
      }
    }
  }

  static Calibration table() throws Refusal {
    return Calibration.load(fixture("vizi-crate-cal.bin"));
  }

  // ---- the format, against the committed fixtures ----

  @Test
  void crcCheckValue() {
    assertEquals(0xCBF43926L, Crate.crc32("123456789".getBytes(StandardCharsets.US_ASCII)));
  }

  @Test
  void sampleIndexTableIsSpecsWorkedExample() throws Refusal {
    long[][] table = {
        {1746, 240, 0x8d53b95bL}, {1506, 240, 0x1c3015abL}, {1266, 240, 0xd56706b3L},
        {1026, 240, 0x03eaaa2eL}, {786, 240, 0x5889bb44L}, {546, 240, 0x5aa02fddL}};
    try (Crate cr = Crate.open(fixture("sample.crate"))) {
      Crate.Header h = cr.header();
      assertEquals(2162, cr.size());
      assertEquals("3×2×4×6×5", h.shape());
      assertEquals(DType.U16, h.dtype());
      assertEquals(0, h.flags());
      assertEquals(546, h.dataStart());
      assertEquals(443, h.textLen());
      assertEquals("ome-xml", cr.metadata()[0]);
      assertEquals(6, cr.index().size());
      for (int i = 0; i < 6; i++) {
        assertEquals(table[i][0], cr.index().get(i).offset(), "offset " + i);
        assertEquals(table[i][1], cr.index().get(i).length(), "length " + i);
        assertEquals(table[i][2], cr.index().get(i).crc32(), "crc " + i);
      }
      Volume first = cr.readBlock(0, 0, true, null);
      int[] ten = {0, 1, 2, 3, 4, 10, 11, 12, 13, 14};
      for (int i = 0; i < 10; i++) assertEquals(ten[i], first.u16(i));
    }
  }

  @Test
  void everyVoxelOfSampleMatchesTheFormula() throws Refusal {
    try (Crate cr = Crate.open(fixture("sample.crate"))) {
      for (int t = 0; t < 3; t++) for (int c = 0; c < 2; c++) assertSample(cr.readBlock(t, c, true, null), t, c);
      Volume plane = cr.readPlane(1, 2, 1, null);
      assertEquals(1, plane.z());
      for (int y = 0; y < 6; y++) for (int x = 0; x < 5; x++) assertEquals(expected(1, 1, 2, y, x), plane.u16(plane.index(0, y, x)));
      assertEquals(ErrorKind.OUT_OF_BOUNDS, assertThrows(Refusal.class, () -> cr.readBlock(3, 0, true, null)).kind());
      assertEquals(ErrorKind.OUT_OF_BOUNDS, assertThrows(Refusal.class, () -> cr.readBlock(0, 2, true, null)).kind());
      assertEquals(ErrorKind.OUT_OF_BOUNDS, assertThrows(Refusal.class, () -> cr.readPlane(0, 4, 0, null)).kind());
    }
  }

  @Test
  void calibratedDecodesToSampleAndFailsCleanlyWithoutTheTable() throws Refusal {
    Calibration cal = table();
    assertEquals(Map.of(0, 7, 1, 13), cal.offsets());
    try (Crate cr = Crate.open(fixture("sample-calibrated.crate"))) {
      assertTrue(cr.header().calibrated());
      for (int t = 0; t < 3; t++) for (int c = 0; c < 2; c++) assertSample(cr.readBlock(t, c, true, cal), t, c);
      Refusal r = assertThrows(Refusal.class, () -> cr.readBlock(0, 0, true, null));
      assertEquals(ErrorKind.IO_ERROR, r.kind());
      assertTrue(r.getMessage().contains("Plugin Manager"), r.getMessage());
    }
    // an uncalibrated crate decodes identically with or without the table
    try (Crate cr = Crate.open(fixture("sample.crate"))) {
      assertSample(cr.readBlock(1, 1, true, cal), 1, 1);
    }
  }

  @Test
  void calibrationSaturatesUnsignedAndNotF32() throws Refusal {
    Calibration cal = new Calibration(Map.of(0, 7, 1, -3));
    byte[] u8 = {3, 7, 10, (byte) 254};
    cal.apply(u8, DType.U8, 0);
    assertArrayEquals(new byte[] {0, 0, 3, (byte) 247}, u8);
    byte[] up = {(byte) 254};
    cal.apply(up, DType.U8, 1);
    assertEquals((byte) 255, up[0]); // 254 + 3 saturates at the top too
    ByteBuffer f = ByteBuffer.allocate(8).order(ByteOrder.LITTLE_ENDIAN).putFloat(3f).putFloat(10.5f);
    byte[] fb = f.array();
    cal.apply(fb, DType.F32, 0);
    ByteBuffer g = ByteBuffer.wrap(fb).order(ByteOrder.LITTLE_ENDIAN);
    assertEquals(-4f, g.getFloat(0));
    assertEquals(3.5f, g.getFloat(4));
    // a malformed table is refused, never a crash
    assertThrows(Refusal.class, () -> Calibration.parse("not a table".getBytes(StandardCharsets.US_ASCII)));
    byte[] lying = new byte[24];
    System.arraycopy("VIZCAL01".getBytes(StandardCharsets.US_ASCII), 0, lying, 0, 8);
    lying[8] = 5; // five entries in 24 bytes
    assertThrows(Refusal.class, () -> Calibration.parse(lying));
  }

  @Test
  void badcrcFailsOneBlockAndOnlyThatOne() throws Refusal {
    try (Crate cr = Crate.open(fixture("badcrc.crate"))) {
      Refusal r = assertThrows(Refusal.class, () -> cr.readBlock(1, 0, true, null));
      assertEquals(ErrorKind.IO_ERROR, r.kind());
      assertTrue(r.getMessage().contains("(t=1, c=0)") && r.getMessage().contains("CRC"), r.getMessage());
      for (int[] tc : new int[][] {{0, 0}, {0, 1}, {1, 1}, {2, 0}, {2, 1}}) assertSample(cr.readBlock(tc[0], tc[1], true, null), tc[0], tc[1]);
      Volume wrong = cr.readBlock(1, 0, false, null);
      boolean differs = false;
      for (long i = 0; i < wrong.samples(); i++) differs |= wrong.u16(i) != expected(1, 0, (int) (i / 30), (int) (i / 5 % 6), (int) (i % 5));
      assertTrue(differs, "verification off serves the flipped bit");
    }
    Crate.CheckResult check = Crate.check(fixture("badcrc.crate"));
    assertEquals("1 of 6 blocks failed CRC in badcrc.crate; first: (t=1, c=0)", check.summary("badcrc.crate"));
  }

  @Test
  void unfinalizedIsUnfinishedNotCorrupt() {
    Refusal r = assertThrows(Refusal.class, () -> Crate.open(fixture("unfinalized.crate")));
    assertEquals(ErrorKind.IO_ERROR, r.kind());
    assertTrue(r.getMessage().contains("unfinished") && r.getMessage().contains("did not complete"), r.getMessage());
    // a file shorter than a footer, too
    Refusal tiny = assertThrows(Refusal.class, () -> Crate.open(write("short.crate", handHeader(1, 2, 0, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0))));
    assertEquals(ErrorKind.IO_ERROR, tiny.kind());
  }

  // ---- hand-built bytes ----

  /** A header written field by field, independently of {@link Crate.Header#pack}. */
  static byte[] handHeader(int version, int dtype, int flags, int[] tczyx, int formLen, int textLen, double sz, double sy, double sx,
      double dt) {
    ByteBuffer b = ByteBuffer.allocate(96).order(ByteOrder.LITTLE_ENDIAN);
    b.put("VIZCRATE".getBytes(StandardCharsets.US_ASCII));
    b.putShort((short) version).putShort((short) dtype).putInt(flags);
    for (int v : tczyx) b.putInt(v);
    b.putShort((short) formLen).putInt(textLen);
    b.position(48);
    b.putDouble(sz).putDouble(sy).putDouble(sx).putDouble(dt);
    return b.array();
  }

  /** A whole crate, one block per (t, c) in canonical order, u8 value = 10*t + c. */
  static byte[] handCrate(int flags, int[] tczyx, byte[] form, byte[] text, double[] s, byte[] reservedFill) {
    return handCrate(flags, tczyx, form, text, new byte[0], new byte[0], s, reservedFill);
  }

  /** As above, with the provenance and names sections, their lengths put at offsets 80 and 84 by hand. */
  static byte[] handCrate(int flags, int[] tczyx, byte[] form, byte[] text, byte[] provenance, byte[] names, double[] s,
      byte[] reservedFill) {
    ByteBuffer b = ByteBuffer.allocate(8192).order(ByteOrder.LITTLE_ENDIAN);
    byte[] h = handHeader(1, 1, flags, tczyx, form.length, text.length, s[0], s[1], s[2], s[3]);
    ByteBuffer.wrap(h).order(ByteOrder.LITTLE_ENDIAN).putInt(80, provenance.length).putInt(84, names.length);
    if (reservedFill != null) {
      for (int i = 42; i < 48; i++) h[i] = reservedFill[0];
      for (int i = 88; i < 96; i++) h[i] = reservedFill[0];
    }
    b.put(h).put(form).put(text).put(provenance).put(names);
    int blocks = tczyx[0] * tczyx[1];
    int vol = tczyx[2] * tczyx[3] * tczyx[4];
    long[] offsets = new long[blocks];
    long[] crcs = new long[blocks];
    for (int i = 0; i < blocks; i++) {
      byte[] data = new byte[vol];
      java.util.Arrays.fill(data, (byte) (10 * (i / tczyx[1]) + i % tczyx[1]));
      offsets[i] = b.position();
      crcs[i] = Crate.crc32(data);
      b.put(data);
    }
    long indexAt = b.position();
    ByteBuffer ix = ByteBuffer.allocate(24 * blocks).order(ByteOrder.LITTLE_ENDIAN);
    for (int i = 0; i < blocks; i++) ix.putLong(offsets[i]).putLong(vol).putInt((int) crcs[i]).putInt(reservedFill == null ? 0 : reservedFill[0]);
    b.put(ix.array());
    b.putLong(indexAt).putLong(blocks).putInt((int) Crate.crc32(ix.array())).putInt(reservedFill == null ? 0 : reservedFill[0]);
    b.put("ETARCZIV".getBytes(StandardCharsets.US_ASCII));
    return java.util.Arrays.copyOf(b.array(), b.position());
  }

  Path write(String name, byte[] bytes) {
    try {
      return Files.write(tmp.resolve(name), bytes);
    } catch (java.io.IOException e) {
      throw new java.io.UncheckedIOException(e);
    }
  }

  @Test
  void handBuiltHeaderIsLittleEndianAndReservedBytesAreIgnored() throws Refusal {
    byte[] form = "ome-ngff".getBytes(StandardCharsets.UTF_8);
    byte[] text = "{}".getBytes(StandardCharsets.UTF_8);
    Path p = write("reserved.crate", handCrate(0xFFFF_FFFE, new int[] {2, 3, 1, 2, 2}, form, text, new double[] {0, 0, 0, 0}, new byte[] {(byte) 0xAB}));
    try (Crate cr = Crate.open(p)) {
      assertFalse(cr.header().calibrated(), "only bit 0 is CALIBRATED; the rest are ignored");
      assertTrue(cr.header().derived(), "bit 1 is DERIVED");
      assertArrayEquals(new String[] {"ome-ngff", "{}"}, cr.document());
      assertEquals(2, cr.header().t());
      assertEquals(3, cr.header().c());
      assertEquals(12, cr.readBlock(1, 2, true, null).u8(3));
      assertArrayEquals(new String[] {"", ""}, cr.metadata(), "a DERIVED crate serves no document");
    }
    // a writer zeroes every reserved byte
    Crate.Header h = new Crate.Header(2, 0, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0, 0, List.of());
    byte[] packed = h.pack();
    for (int i = 42; i < 48; i++) assertEquals(0, packed[i]);
    for (int i = 80; i < 96; i++) assertEquals(0, packed[i]);
    assertArrayEquals(handHeader(1, 2, 0, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0), packed);
    // and puts provenance_len and names_len at 80 and 84, with 88..96 still zero
    byte[] lens = new Crate.Header(2, Crate.FLAG_DERIVED, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0x01020304, 0x0A0B, List.of()).pack();
    byte[] expected = handHeader(1, 2, 2, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0);
    ByteBuffer.wrap(expected).order(ByteOrder.LITTLE_ENDIAN).putInt(80, 0x01020304).putInt(84, 0x0A0B);
    assertArrayEquals(expected, lens);
  }

  @Test
  void metadataOfLengthZeroAnUnknownFormAndNonUtf8() throws Refusal {
    Path none = write("none.crate", handCrate(0, new int[] {1, 1, 1, 1, 1}, new byte[0], new byte[0], new double[4], null));
    try (Crate cr = Crate.open(none)) {
      assertArrayEquals(new String[] {"", ""}, cr.metadata());
      assertEquals(96, cr.header().dataStart());
    }
    Path odd = write("odd.crate", handCrate(0, new int[] {1, 1, 1, 1, 1}, "tiff-tags".getBytes(StandardCharsets.UTF_8),
        "x=1".getBytes(StandardCharsets.UTF_8), new double[4], null));
    try (Crate cr = Crate.open(odd)) {
      assertArrayEquals(new String[] {"tiff-tags", "x=1"}, cr.metadata(), "an unknown form is carried, not judged");
    }
    Path textOnly = write("textonly.crate", handCrate(0, new int[] {1, 1, 1, 1, 1}, new byte[0], "orphan".getBytes(StandardCharsets.UTF_8),
        new double[4], null));
    try (Crate cr = Crate.open(textOnly)) {
      assertArrayEquals(new String[] {"", ""}, cr.metadata(), "an empty form with a text is no document");
    }
    Path bad = write("latin1.crate", handCrate(0, new int[] {1, 1, 1, 1, 1}, "ome-xml".getBytes(StandardCharsets.UTF_8),
        new byte[] {'<', (byte) 0xE9, '>'}, new double[4], null));
    try (Crate cr = Crate.open(bad)) {
      assertFalse(cr.metadataUtf8());
      assertArrayEquals(new String[] {"", ""}, cr.metadata());
      assertEquals(0, cr.readBlock(0, 0, true, null).u8(0), "the voxels still read");
    }
  }

  @Test
  void aZeroAxisIsNotACrate() {
    String[] names = {"t_count", "c_count", "z", "y", "x"};
    for (int i = 0; i < 5; i++) {
      int[] shape = {1, 1, 1, 1, 1};
      shape[i] = 0;
      Refusal r = assertThrows(Refusal.class, () -> Crate.Header.parse(handHeader(1, 1, 0, shape, 0, 0, 0, 0, 0, 0)));
      assertEquals(ErrorKind.INVALID_ARGUMENT, r.kind());
      assertTrue(r.getMessage().contains(names[i]), r.getMessage());
    }
    assertEquals(ErrorKind.INVALID_ARGUMENT, assertThrows(Refusal.class,
        () -> Crate.Header.parse(handHeader(2, 1, 0, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0))).kind());
    assertEquals(ErrorKind.UNSUPPORTED, assertThrows(Refusal.class,
        () -> Crate.Header.parse(handHeader(1, 4, 0, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0))).kind());
    byte[] notCrate = handHeader(1, 1, 0, new int[] {1, 1, 1, 1, 1}, 0, 0, 0, 0, 0, 0);
    notCrate[0] = 'X';
    assertEquals(ErrorKind.INVALID_ARGUMENT, assertThrows(Refusal.class, () -> Crate.Header.parse(notCrate)).kind());
  }

  @Test
  void aCorruptIndexIsRefused() throws Exception {
    byte[] b = Files.readAllBytes(fixture("sample.crate"));
    b[1986 + 3] ^= 1; // inside the index
    assertEquals(ErrorKind.IO_ERROR, assertThrows(Refusal.class, () -> Crate.open(write("badindex.crate", b))).kind());
  }

  // ---- voxel size ----

  @Test
  void sampleHeaderSpacing() throws Refusal {
    try (Crate cr = Crate.open(fixture("sample.crate"))) {
      assertArrayEquals(new double[] {2.0, 0.325, 0.325}, cr.header().spacingZyx());
      assertEquals(1.5, cr.header().timeIncrement());
      assertEquals("2.000 × 0.325 × 0.325 µm (z × y × x), Δt 1.5 s", cr.spacingLabel());
    }
  }

  @Test
  void spacingConflictReportsTheHeaderNeverTheDocument() throws Exception {
    try (Crate cr = Crate.open(fixture("spacing-conflict.crate"))) {
      assertArrayEquals(new double[] {2.0, 0.325, 0.325}, cr.header().spacingZyx());
      assertEquals(1.5, cr.header().timeIncrement());
    }
    FormatBridge fb = new FormatBridge(Environment.of(Map.of()));
    fb.plugin();
    try (var d = fb.openReader(fixture("spacing-conflict.crate").toString(), tmp)) {
      var info = d.info();
      assertEquals(OptionalDouble.of(2.0), info.spacingZ());
      assertEquals(OptionalDouble.of(0.325), info.spacingX());
      assertEquals(OptionalDouble.of(1.5), info.timeIncrement());
    }
  }

  @Test
  void nospacingOpensReadsAndReportsUnknownWithoutAnError() throws Exception {
    try (Crate cr = Crate.open(fixture("nospacing.crate"))) {
      assertArrayEquals(new double[] {0, 0, 0}, cr.header().spacingZyx());
      assertEquals(0, cr.header().timeIncrement());
      assertTrue(cr.header().malformed().isEmpty(), "0.0 is unknown, not malformed");
      assertEquals("(unknown)", cr.spacingLabel());
      assertSample(cr.readBlock(2, 1, true, null), 2, 1);
    }
    // and it recreates ("exports") with zeros
    Path out = tmp.resolve("nospacing-out.crate");
    CrateTool.recreate(fixture("nospacing.crate"), out, "ascending", null);
    try (Crate cr = Crate.open(out)) {
      assertArrayEquals(new double[] {0, 0, 0, 0}, new double[] {cr.header().spacingZ(), cr.header().spacingY(), cr.header().spacingX(),
          cr.header().timeIncrement()});
    }
  }

  @Test
  void bothSpellingsOfMicroConvertIdentically() {
    String micro = "\u00B5m";
    String mu = "\u03BCm";
    assertNotEquals(micro, mu);
    assertEquals(0.325, Ome.toUm(0.325, micro));
    assertEquals(0.325, Ome.toUm(0.325, mu));
    // every unit in the table, and one that is not
    assertEquals(1e6, Ome.toUm(1, "m"));
    assertEquals(1e4, Ome.toUm(1, "cm"));
    assertEquals(1e3, Ome.toUm(1, "mm"));
    assertEquals(1.0, Ome.toUm(1, "um"));
    assertEquals(1.0, Ome.toUm(1, "micron"));
    assertEquals(1e-3, Ome.toUm(1, "nm"));
    assertEquals(1e-4, Ome.toUm(1, "\u00C5"));
    assertEquals(1e-4, Ome.toUm(1, "angstrom"));
    assertEquals(1.0, Ome.toUm(1, ""), "an empty unit is OME's default");
    assertEquals(0.0, Ome.toUm(1, "furlong"));
    assertEquals(0.0, Ome.toUm(-1, "um"));
    assertEquals(0.0, Ome.toUm(Double.NaN, "um"));
    assertEquals(0.0, Ome.toUm(Double.POSITIVE_INFINITY, "um"));
    assertEquals(1e-3, Ome.toS(1, "ms"));
    assertEquals(1.0, Ome.toS(1, "sec"));
    assertEquals(60.0, Ome.toS(1, "min"));
    assertEquals(3600.0, Ome.toS(1, "h"));
    assertEquals(3600.0, Ome.toS(1, "hr"));
    assertEquals(2.0, Ome.toS(2, ""), "an empty unit is OME's default");
    assertEquals(0.0, Ome.toS(1, "fortnight"));
  }

  @Test
  void theExporterTakesTheFieldsOrNothing() {
    OptionalDouble none = OptionalDouble.empty();
    Ome.Resolved fields = Ome.resolve(OptionalDouble.of(2), OptionalDouble.of(0.325), OptionalDouble.of(0.325), "um", OptionalDouble.of(1500), "ms");
    assertEquals(new Ome.Spacing(2, 0.325, 0.325, 1.5), fields.spacing());
    assertFalse(fields.partial(), "the ordinary case: nothing to log");
    Ome.Resolved mixed = Ome.resolve(OptionalDouble.of(4), OptionalDouble.of(0), none, "nm", none, "");
    assertEquals(new Ome.Spacing(0.004, 0, 0, 0), mixed.spacing());
    assertTrue(mixed.partial(), "some but not all unknown is logged");
    assertEquals("y, x, t", mixed.unknown());
    Ome.Resolved neither = Ome.resolve(none, none, none, "", none, "");
    assertFalse(neither.anyKnown());
    assertFalse(neither.partial(), "nothing known is the panel's line, not a log");
    Ome.Resolved badUnit = Ome.resolve(OptionalDouble.of(2), OptionalDouble.of(2), OptionalDouble.of(2), "parsec", OptionalDouble.of(1), "eon");
    assertFalse(badUnit.anyKnown(), "a unit not in the table is unknown, never a guess");
  }

  @Test
  void malformedHeaderSpacingIsUnknownAndTheVoxelsStillRead() throws Refusal {
    Path p = write("malformed.crate", handCrate(0, new int[] {1, 1, 2, 2, 2}, new byte[0], new byte[0],
        new double[] {-1, Double.POSITIVE_INFINITY, Double.NaN, -0.5}, null));
    try (Crate cr = Crate.open(p)) {
      assertArrayEquals(new double[] {0, 0, 0}, cr.header().spacingZyx());
      assertEquals(0, cr.header().timeIncrement());
      assertEquals(List.of("spacing_z", "spacing_y", "spacing_x", "time_increment"), cr.header().malformed());
      assertEquals("(unknown)", cr.spacingLabel());
      assertEquals(8, cr.readBlock(0, 0, true, null).samples());
    }
  }

  @Test
  void spacingLabelMarksEachUnknownComponent() {
    assertEquals("2.000 × ? × 0.325 µm (z × y × x), Δt ? s", Labels.spacing(new double[] {2, 0, 0.325}, 0));
    assertEquals("? × ? × ? µm (z × y × x), Δt 0.5 s", Labels.spacing(new double[] {0, 0, 0}, 0.5));
    assertEquals("(unknown)", Labels.spacing(new double[] {0, 0, 0}, 0));
    assertEquals("1.5", Labels.g(1.5));
    assertEquals("21354", Labels.g(21354));
    assertEquals("1e+06", Labels.g(1e6));
    assertEquals("0.0001", Labels.g(1e-4));
    assertEquals("1e-05", Labels.g(1e-5));
    assertEquals("0.333333", Labels.g(1.0 / 3));
  }

  // ---- the sections: DERIVED, provenance, channel names ----

  static final byte[] NUL = {0};

  static byte[] utf8(String s) {
    return s.getBytes(StandardCharsets.UTF_8);
  }

  @Test
  void handBuiltSectionsFollowTheMetadataAndTheBlocksFollowThem() throws Refusal {
    byte[] form = utf8("ome-xml");
    byte[] text = utf8("<OME/>");
    byte[] prov = utf8("{\"source\": {\"name\": \"a.tif\"}}");
    byte[] names = utf8("GFP\0\0D\u00C4PI");
    Path p = write("sections.crate", handCrate(Crate.FLAG_DERIVED, new int[] {2, 3, 1, 2, 2}, form, text, prov, names, new double[4], null));
    try (Crate cr = Crate.open(p)) {
      Crate.Header h = cr.header();
      assertEquals(prov.length, h.provenanceLen());
      assertEquals(names.length, h.namesLen());
      assertEquals(96 + form.length + text.length + prov.length + names.length, h.dataStart());
      assertTrue(h.derived());
      assertFalse(h.calibrated());
      for (int t = 0; t < 2; t++) for (int c = 0; c < 3; c++) assertEquals(10 * t + c, cr.readBlock(t, c, true, null).u8(3));
      assertEquals(List.of("GFP", "", "D\u00C4PI"), cr.channelNames());
      assertArrayEquals(prov, cr.provenanceBytes());
      assertArrayEquals(new String[] {"ome-xml", "<OME/>"}, cr.document(), "the section is kept");
      assertArrayEquals(new String[] {"", ""}, cr.metadata(), "and a DERIVED crate serves none");
    }
    // without the flag the same document is the crate's own, and served
    Path own = write("own.crate", handCrate(0, new int[] {1, 1, 1, 1, 1}, form, text, prov, new byte[0], new double[4], null));
    try (Crate cr = Crate.open(own)) {
      assertArrayEquals(new String[] {"ome-xml", "<OME/>"}, cr.metadata());
      assertEquals(List.of(), cr.channelNames(), "zero bytes are no names");
    }
  }

  @Test
  void namesStopShortAndMalformedNamesAreNoneWhileTheVoxelsRead() throws Refusal {
    int[] shape = {1, 3, 1, 1, 1};
    try (Crate cr = Crate.open(write("short.crate", handCrate(0, shape, new byte[0], new byte[0], new byte[0], utf8("A"), new double[4], null)))) {
      assertEquals(List.of("A"), cr.channelNames(), "the list may stop short of c_count");
      assertNull(cr.namesProblem());
    }
    try (Crate cr = Crate.open(write("many.crate", handCrate(0, shape, new byte[0], new byte[0], new byte[0], utf8("A\0B\0C\0D"), new double[4], null)))) {
      assertNull(cr.channelNames(), "four names for three channels");
      assertTrue(cr.namesProblem().contains("more channels than its 3"), cr.namesProblem());
      assertEquals(2, cr.readBlock(0, 2, true, null).u8(0));
    }
    byte[] latin1 = {'G', (byte) 0xE9, 0, 'X'};
    try (Crate cr = Crate.open(write("latin.crate", handCrate(0, shape, new byte[0], new byte[0], new byte[0], latin1, new double[4], null)))) {
      assertNull(cr.channelNames());
      assertTrue(cr.namesProblem().contains("UTF-8"), cr.namesProblem());
      assertEquals(1, cr.readBlock(0, 1, true, null).u8(0));
    }
    // the reader serves no names for a malformed section and says so in status; the open succeeds
    FormatBridge fb = new FormatBridge(Environment.of(Map.of()));
    fb.plugin();
    try (var d = fb.openReader(tmp.resolve("many.crate").toString(), tmp)) {
      assertEquals(List.of(), d.info().channelNames());
    }
    try (var d = fb.openReader(tmp.resolve("short.crate").toString(), tmp)) {
      assertEquals(List.of("A"), d.info().channelNames(), "declared as ReaderOpened.channel_names");
    }
  }

  @Test
  void theWriterDropsTrailingEmptiesAndWritesANameHoldingNulAsEmpty() throws Refusal {
    assertArrayEquals(new byte[0], CrateWriter.encodeNames(List.of(), 3));
    assertArrayEquals(new byte[0], CrateWriter.encodeNames(List.of("", "", ""), 3), "naming no channel is zero bytes");
    assertArrayEquals(utf8("A\0\0C"), CrateWriter.encodeNames(List.of("A", "", "C"), 3));
    assertArrayEquals(utf8("A"), CrateWriter.encodeNames(List.of("A", "", ""), 3));
    assertArrayEquals(utf8("\0B"), CrateWriter.encodeNames(List.of("x\0y", "B"), 3));
    assertEquals(ErrorKind.INVALID_ARGUMENT, assertThrows(Refusal.class, () -> CrateWriter.encodeNames(List.of("a", "b"), 1)).kind());

    Path p = tmp.resolve("written.crate");
    CrateWriter w = new CrateWriter(p, new int[] {1, 2, 1, 1, 1}, DType.U8, true, utf8("ome-xml"), utf8("<OME/>"), utf8("{}"),
        List.of("", "DAPI"), new double[3], 0);
    w.begin();
    w.writeBlock(0, 0, new byte[] {7});
    w.writeBlock(0, 1, new byte[] {8});
    w.finish();
    try (Crate cr = Crate.open(p)) {
      assertTrue(cr.header().derived());
      assertEquals(List.of("", "DAPI"), cr.channelNames());
      assertArrayEquals(utf8("{}"), cr.provenanceBytes());
      assertEquals(8, cr.readBlock(0, 1, true, null).u8(0));
    }
  }

  /** A request as the SDK would hand it over. */
  static Exporter.Request request(Path locator, int c, Dataset.Metadata source, List<ChannelName> channels, String provenance) {
    return new Exporter.Request("crate", locator.toString(), 1, c, 1, 1, 2, DType.U8, OptionalDouble.empty(), OptionalDouble.empty(),
        OptionalDouble.empty(), "", OptionalDouble.empty(), "", locator.getParent(), source, channels, provenance, true,
        vizi.plugin.v1.ViziPlugin.OpenExport.getDefaultInstance());
  }

  static ChannelName name(int index, String name, vizi.plugin.v1.ViziPlugin.ChannelNameSource source) {
    return new ChannelName(index, name, source, source.getNumber());
  }

  static void export(Exporter.Export x, int c) throws Exception {
    List<Integer> chans = new ArrayList<>();
    for (int k = 0; k < c; k++) chans.add(k);
    byte[] bytes = new byte[2 * c];
    for (int k = 0; k < bytes.length; k++) bytes[k] = (byte) k;
    x.chunk(new Exporter.Chunk(0, 0, chans, chans, List.of(), 1, 1, 2, DType.U8, java.lang.foreign.MemorySegment.ofArray(bytes)));
    x.finish();
  }

  @Test
  void theExporterKeepsTheSourcesDocumentAndTheRecordAndWritesOnlyDeclaredNames() throws Exception {
    FormatBridge fb = new FormatBridge(Environment.of(Map.of()));
    fb.plugin();
    List<ChannelName> channels = List.of(
        name(2, "GFP", vizi.plugin.v1.ViziPlugin.ChannelNameSource.CHANNEL_NAME_SOURCE_FILE),
        name(0, "Channel 1", vizi.plugin.v1.ViziPlugin.ChannelNameSource.CHANNEL_NAME_SOURCE_PLACEHOLDER),
        name(1, "Mine", vizi.plugin.v1.ViziPlugin.ChannelNameSource.CHANNEL_NAME_SOURCE_USER),
        name(3, "Who", vizi.plugin.v1.ViziPlugin.ChannelNameSource.CHANNEL_NAME_SOURCE_UNSPECIFIED));
    String record = "{\"source\": {\"name\": \"a.tif\"},  \"selection\": {\"channels\": [2, 0, 1, 3]}}";
    Path out = tmp.resolve("exported.crate");
    // the source's document says 1000 nm: it is kept, and never read for the spacing
    Dataset.Metadata doc = new Dataset.Metadata("ome-xml", "<OME><Image><Pixels PhysicalSizeZ=\"1000\" PhysicalSizeZUnit=\"nm\"/></Image></OME>");
    export(fb.openExport(request(out, 4, doc, channels, record)), 4);
    try (Crate cr = Crate.open(out)) {
      assertTrue(cr.header().derived(), "an exported crate is DERIVED");
      assertFalse(cr.header().calibrated());
      assertArrayEquals(new String[] {doc.form(), doc.text()}, cr.document(), "the source's document, verbatim, as provenance");
      assertArrayEquals(new String[] {"", ""}, cr.metadata());
      assertArrayEquals(utf8(record), cr.provenanceBytes(), "the record as it arrived");
      assertEquals(List.of("GFP", "", "Mine"), cr.channelNames(), "FILE and USER written; a placeholder and an unspecified one are not");
      assertArrayEquals(new double[] {0, 0, 0}, cr.header().spacingZyx(), "never scraped from the source's document");
      assertEquals(1, cr.readBlock(0, 0, true, null).u8(1));
    }
    // a record that is not a JSON object is none; no document is none
    Path bare = tmp.resolve("bare.crate");
    export(fb.openExport(request(bare, 1, Dataset.Metadata.NONE, List.of(), "not json")), 1);
    try (Crate cr = Crate.open(bare)) {
      assertTrue(cr.header().derived());
      assertEquals(0, cr.header().provenanceLen());
      assertEquals(0, cr.header().namesLen());
      assertEquals(96, cr.header().dataStart());
    }
    // reopened in this plugin's own reader: the names declared, no document served
    try (var d = fb.openReader(out.toString(), tmp)) {
      assertEquals(List.of("GFP", "", "Mine"), d.info().channelNames());
      assertEquals(Dataset.Metadata.NONE, d.metadata());
    }
  }

  @Test
  void recreateCarriesDerivedTheRecordAndTheNamesAndTheComparatorNotesThem() throws Exception {
    byte[] form = utf8("ome-xml");
    byte[] text = utf8("<OME/>");
    Path src = write("derived.crate", handCrate(Crate.FLAG_DERIVED, new int[] {1, 2, 1, 1, 2}, form, text, utf8("{\"a\": 1}"),
        utf8("\0B"), new double[] {1, 1, 1, 1}, null));
    Path out = tmp.resolve("derived-out.crate");
    CrateTool.recreate(src, out, "reverse", null);
    try (Crate cr = Crate.open(out)) {
      assertTrue(cr.header().derived());
      assertArrayEquals(utf8("{\"a\": 1}"), cr.provenanceBytes());
      assertEquals(List.of("", "B"), cr.channelNames());
      assertArrayEquals(new String[] {"ome-xml", "<OME/>"}, cr.document());
    }
    assertEquals(Compare.SEMANTIC, Compare.compare(src, out, null).tier());
    // names count toward semantic equality
    Path renamed = write("renamed.crate", handCrate(Crate.FLAG_DERIVED, new int[] {1, 2, 1, 1, 2}, form, text, utf8("{\"a\": 1}"),
        utf8("\0C"), new double[] {1, 1, 1, 1}, null));
    Compare.Verdict v = Compare.compare(src, renamed, null);
    assertEquals(Compare.VOXEL, v.tier());
    assertTrue(v.toString().contains("channel names differ ('', 'B' vs '', 'C')"), v.toString());
    // DERIVED and the record are noted, never compared
    Path plain = write("plain.crate", handCrate(0, new int[] {1, 2, 1, 1, 2}, form, text, utf8("{\"a\": 2}"),
        utf8("\0B"), new double[] {1, 1, 1, 1}, null));
    Compare.Verdict n = Compare.compare(src, plain, null);
    assertEquals(Compare.SEMANTIC, n.tier(), n.toString());
    assertTrue(n.toString().contains("derived.crate is DERIVED"), n.toString());
    assertTrue(n.toString().contains("provenance records differ"), n.toString());
  }

  // ---- the writer ----

  @Test
  void roundTripByteStabilityAndScrambledOrder() throws Exception {
    Path a = tmp.resolve("a.crate");
    Path b = tmp.resolve("b.crate");
    Path s = tmp.resolve("s.crate");
    Path r = tmp.resolve("r.crate");
    CrateTool.recreate(fixture("sample.crate"), a, "ascending", null);
    CrateTool.recreate(fixture("sample.crate"), b, "ascending", null);
    assertArrayEquals(Files.readAllBytes(a), Files.readAllBytes(b), "the writer is byte-stable");
    CrateTool.recreate(a, r, "ascending", null);
    assertArrayEquals(Files.readAllBytes(a), Files.readAllBytes(r), "re-import and export again: still byte-identical");
    CrateTool.recreate(fixture("sample.crate"), s, "scrambled", null);
    try (Crate cr = Crate.open(s)) {
      List<Long> offsets = new ArrayList<>();
      for (Crate.IndexEntry e : cr.index()) offsets.add(e.offset());
      List<Long> sorted = new ArrayList<>(offsets);
      Collections.sort(sorted);
      assertNotEquals(sorted, offsets, "the scrambled crate is not in canonical order");
      for (int t = 0; t < 3; t++) for (int c = 0; c < 2; c++) assertSample(cr.readBlock(t, c, true, null), t, c);
    }
    assertEquals(Compare.SEMANTIC, Compare.compare(fixture("sample.crate"), a, null).tier());
    assertEquals(Compare.SEMANTIC, Compare.compare(s, a, null).tier());
    assertEquals(Compare.BYTE_EQUAL, Compare.compare(a, b, null).tier());
  }

  @Test
  void aRepeatedBlockWinsAndAHoleFailsTheFinishAndLeavesNothing() throws Refusal {
    Path p = tmp.resolve("w.crate");
    CrateWriter w = new CrateWriter(p, new int[] {1, 2, 1, 1, 2}, DType.U8, new byte[0], new byte[0], new double[3], 0);
    w.begin();
    assertFalse(w.writeBlock(0, 1, new byte[] {1, 1}));
    assertTrue(w.writeBlock(0, 1, new byte[] {2, 2}));
    Refusal r = assertThrows(Refusal.class, w::finish);
    assertTrue(r.getMessage().contains("1 of 2 blocks") && r.getMessage().contains("t=0, c=0"), r.getMessage());
    assertFalse(Files.exists(p));
    CrateWriter w2 = new CrateWriter(p, new int[] {1, 2, 1, 1, 2}, DType.U8, new byte[0], new byte[0], new double[3], 0);
    w2.begin();
    w2.writeBlock(0, 1, new byte[] {1, 1});
    w2.writeBlock(0, 1, new byte[] {2, 2});
    w2.writeBlock(0, 0, new byte[] {3, 3});
    w2.finish();
    w2.abort(); // a late abort does not delete a finished file
    try (Crate cr = Crate.open(p)) {
      assertEquals(2, cr.readBlock(0, 1, true, null).u8(0));
      assertEquals(3, cr.readBlock(0, 0, true, null).u8(1));
    }
    CrateWriter w3 = new CrateWriter(tmp.resolve("aborted.crate"), new int[] {1, 1, 1, 1, 1}, DType.U8, new byte[0], new byte[0], new double[3], 0);
    w3.begin();
    assertTrue(Files.exists(tmp.resolve("aborted.crate")));
    w3.abort();
    assertFalse(Files.exists(tmp.resolve("aborted.crate")));
    assertEquals(ErrorKind.UNSUPPORTED,
        assertThrows(Refusal.class, () -> new CrateWriter(p, new int[] {1, 1, 1, 1, 1}, DType.F64, new byte[0], new byte[0], new double[3], 0)).kind());
  }

  @Test
  void bigEndianSourceIsSwappedIntoTheFixedLayout() {
    // u16 0x0102 and 0xA0B0, big-endian on the wire, must land as 02 01 B0 A0
    assertArrayEquals(new byte[] {2, 1, (byte) 0xB0, (byte) 0xA0}, CrateWriter.swapped(new byte[] {1, 2, (byte) 0xA0, (byte) 0xB0}, 2));
    // f32 1.5f is 3F C0 00 00 big-endian, 00 00 C0 3F little-endian
    assertArrayEquals(new byte[] {0, 0, (byte) 0xC0, 0x3F}, CrateWriter.swapped(new byte[] {0x3F, (byte) 0xC0, 0, 0}, 4));
    assertArrayEquals(new byte[] {1, 2, 3}, CrateWriter.swapped(new byte[] {1, 2, 3}, 1));
  }

  // ---- interop ----

  @Test
  void everyInteropCrateOpensAndOursMatchesTheirBytes() throws Exception {
    Path ours = tmp.resolve("java.crate");
    CrateTool.recreate(fixture("sample.crate"), ours, "ascending", null);
    try (var files = Files.list(INTEROP)) {
      for (Path p : files.filter(f -> f.toString().endsWith(".crate")).toList()) {
        try (Crate cr = Crate.open(p)) {
          for (int t = 0; t < 3; t++) for (int c = 0; c < 2; c++) assertSample(cr.readBlock(t, c, true, null), t, c);
        }
        assertEquals(Compare.SEMANTIC, Compare.compare(fixture("sample.crate"), p, null).tier(), p.toString());
        assertArrayEquals(Files.readAllBytes(p), Files.readAllBytes(ours), p + " and this writer's recreate differ");
      }
    }
  }

  // ---- the comparator's tiers and exit codes ----

  static int tool(String... args) {
    return CrateTool.run(args, new PrintStream(new ByteArrayOutputStream()), new PrintStream(new ByteArrayOutputStream()));
  }

  static String toolOut(String... args) {
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    CrateTool.run(args, new PrintStream(out), new PrintStream(new ByteArrayOutputStream()));
    return out.toString(StandardCharsets.UTF_8).strip();
  }

  @Test
  void comparatorTiersAndExitCodes() throws Refusal {
    String sample = fixture("sample.crate").toString();
    String cal = fixture("vizi-crate-cal.bin").toString();
    assertEquals(Compare.BYTE_EQUAL, Compare.compare(fixture("sample.crate"), fixture("sample.crate"), null).tier());
    assertEquals(Compare.SEMANTIC, Compare.compare(fixture("sample.crate"), fixture("sample-calibrated.crate"), table()).tier());
    assertEquals(Compare.VOXEL, Compare.compare(fixture("sample.crate"), fixture("nospacing.crate"), null).tier());
    assertEquals(Compare.VOXEL, Compare.compare(fixture("sample.crate"), fixture("spacing-conflict.crate"), null).tier());
    assertEquals(Compare.DIFFERENT, Compare.compare(fixture("sample.crate"), fixture("badcrc.crate"), null).tier());
    assertEquals(0, tool("compare", sample, fixture("badcrc.crate").toString()));
    assertTrue(toolOut("compare", sample, fixture("badcrc.crate").toString()).startsWith("different"));
    assertEquals(2, tool("compare", sample, fixture("unfinalized.crate").toString()));
    assertEquals(2, tool("compare", sample, fixture("sample-calibrated.crate").toString()), "a calibrated crate with no table cannot be decoded");
    assertEquals(0, tool("compare", sample, fixture("sample-calibrated.crate").toString(), "--calibration", cal));
    assertEquals(2, tool("compare", sample, tmp.resolve("absent.crate").toString()));
    assertEquals(1, tool("compare", sample));
    assertEquals(1, tool("compare", sample, sample, "--bogus"));
    assertEquals(0, tool("check", sample));
    assertEquals(3, tool("check", fixture("badcrc.crate").toString()));
    assertEquals(2, tool("check", fixture("unfinalized.crate").toString()));
  }

  // ---- the manifest ----

  @Test
  void theCommittedManifestIsTheOneTheCodeDeclares() throws Exception {
    var plugin = new FormatBridge(Environment.of(Map.of())).plugin();
    assertEquals(Files.readString(HERE.resolve("vizi-plugin.json")), ManifestWriter.write(plugin.manifest()));
  }
}
