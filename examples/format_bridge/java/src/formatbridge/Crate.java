package formatbridge;

import java.io.IOException;
import java.lang.foreign.MemorySegment;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.CRC32;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Volume;

/**
 * An opened, validated crate (SPEC.md §"The .crate format"): header, metadata, the index, and
 * positional reads off one channel, so the reader worker and a describe job never race on a file
 * position. Refusals are {@link Refusal}s carrying the kind SPEC.md assigns.
 */
public final class Crate implements AutoCloseable {

  public static final byte[] MAGIC = "VIZCRATE".getBytes(StandardCharsets.US_ASCII);
  public static final byte[] FOOTER_MAGIC = "ETARCZIV".getBytes(StandardCharsets.US_ASCII);
  public static final int FORMAT_VERSION = 1;
  public static final int HEADER_BYTES = 96;
  public static final int INDEX_ENTRY_BYTES = 24;
  public static final int FOOTER_BYTES = 32;
  public static final int FLAG_CALIBRATED = 1;
  /** The metadata section is the SOURCE's document, kept as provenance and never served. */
  public static final int FLAG_DERIVED = 1 << 1;

  static final String UNFINISHED = "unfinished crate — an export of this file did not complete";

  /** IEEE 802.3 / zlib CRC-32 (java.util.zip.CRC32): reflected 0xEDB88320, init and xorout 0xFFFFFFFF. */
  public static long crc32(byte[] data) {
    CRC32 c = new CRC32();
    c.update(data);
    return c.getValue();
  }

  public static Refusal refusal(ErrorKind kind, String message) {
    return new Refusal(kind, message);
  }

  // ---- the header ----

  /** The 96-byte header, spacing sanitized (0.0 = unknown). */
  public record Header(
      int dtypeCode, int flags, int t, int c, int z, int y, int x, int formLen, long textLen,
      double spacingZ, double spacingY, double spacingX, double timeIncrement, long provenanceLen, long namesLen,
      List<String> malformed) {

    public DType dtype() {
      return dtypeOf(dtypeCode);
    }

    public String dtypeName() {
      return switch (dtypeCode) {
        case 1 -> "u8";
        case 2 -> "u16";
        default -> "f32";
      };
    }

    public int itemsize() {
      return dtype().size;
    }

    public long blockBytes() {
      return (long) z * y * x * itemsize();
    }

    public long planeBytes() {
      return (long) y * x * itemsize();
    }

    public boolean calibrated() {
      return (flags & FLAG_CALIBRATED) != 0;
    }

    public boolean derived() {
      return (flags & FLAG_DERIVED) != 0;
    }

    /** After the header, the metadata, the provenance and the names. */
    public long dataStart() {
      return HEADER_BYTES + formLen + textLen + provenanceLen + namesLen;
    }

    public int blocks() {
      return t * c;
    }

    public double[] spacingZyx() {
      return new double[] {spacingZ, spacingY, spacingX};
    }

    public String shape() {
      return t + "×" + c + "×" + z + "×" + y + "×" + x;
    }

    /** Parses and validates; reserved bytes and unknown flag bits are ignored. */
    public static Header parse(byte[] data) throws Refusal {
      if (data.length < HEADER_BYTES) throw refusal(ErrorKind.INVALID_ARGUMENT, "not a crate: shorter than a header");
      ByteBuffer b = ByteBuffer.wrap(data).order(ByteOrder.LITTLE_ENDIAN);
      for (int i = 0; i < 8; i++) {
        if (data[i] != MAGIC[i]) throw refusal(ErrorKind.INVALID_ARGUMENT, "not a crate: bad magic");
      }
      int version = Short.toUnsignedInt(b.getShort(8));
      if (version != FORMAT_VERSION) {
        throw refusal(ErrorKind.INVALID_ARGUMENT, "not a crate this reader knows: format_version " + version);
      }
      int dtype = Short.toUnsignedInt(b.getShort(10));
      if (dtype < 1 || dtype > 3) throw refusal(ErrorKind.UNSUPPORTED, "dtype code " + dtype + " is not defined by the crate format");
      int flags = b.getInt(12);
      long[] extents = new long[5];
      String[] names = {"t_count", "c_count", "z", "y", "x"};
      for (int i = 0; i < 5; i++) {
        extents[i] = Integer.toUnsignedLong(b.getInt(16 + 4 * i));
        if (extents[i] == 0) {
          throw refusal(ErrorKind.INVALID_ARGUMENT, "not a crate: " + names[i] + " is 0, and a volume with no voxels is not a smaller volume");
        }
        if (extents[i] > Integer.MAX_VALUE) {
          throw refusal(ErrorKind.UNSUPPORTED, names[i] + " = " + extents[i] + " is larger than this reader addresses");
        }
      }
      int formLen = Short.toUnsignedInt(b.getShort(36));
      long textLen = Integer.toUnsignedLong(b.getInt(38));
      double[] raw = {b.getDouble(48), b.getDouble(56), b.getDouble(64), b.getDouble(72)};
      long provenanceLen = Integer.toUnsignedLong(b.getInt(80));
      long namesLen = Integer.toUnsignedLong(b.getInt(84));
      String[] rawNames = {"spacing_z", "spacing_y", "spacing_x", "time_increment"};
      List<String> malformed = new ArrayList<>();
      for (int i = 0; i < 4; i++) {
        if (!(Double.isFinite(raw[i]) && raw[i] >= 0.0)) malformed.add(rawNames[i]);
      }
      return new Header(dtype, flags, (int) extents[0], (int) extents[1], (int) extents[2], (int) extents[3], (int) extents[4],
          formLen, textLen, sanitize(raw[0]), sanitize(raw[1]), sanitize(raw[2]), sanitize(raw[3]), provenanceLen, namesLen,
          List.copyOf(malformed));
    }

    /** The 96 bytes, reserved fields zero. */
    public byte[] pack() {
      ByteBuffer b = ByteBuffer.allocate(HEADER_BYTES).order(ByteOrder.LITTLE_ENDIAN);
      b.put(MAGIC);
      b.putShort(8, (short) FORMAT_VERSION);
      b.putShort(10, (short) dtypeCode);
      b.putInt(12, flags);
      b.putInt(16, t).putInt(20, c).putInt(24, z).putInt(28, y).putInt(32, x);
      b.putShort(36, (short) formLen);
      b.putInt(38, (int) textLen);
      b.putDouble(48, spacingZ).putDouble(56, spacingY).putDouble(64, spacingX).putDouble(72, timeIncrement);
      b.putInt(80, (int) provenanceLen).putInt(84, (int) namesLen);
      return b.array();
    }
  }

  /** A negative, infinite or NaN spacing is malformed metadata, not a malformed file: unknown. */
  public static double sanitize(double v) {
    return Double.isFinite(v) && v > 0.0 ? v : 0.0;
  }

  public static DType dtypeOf(int code) {
    return switch (code) {
      case 1 -> DType.U8;
      case 2 -> DType.U16;
      case 3 -> DType.F32;
      default -> throw new IllegalArgumentException("dtype code " + code);
    };
  }

  /** The format's code for a dtype, or 0 when it has no crate encoding (f64). */
  public static int codeOf(DType d) {
    return switch (d) {
      case U8 -> 1;
      case U16 -> 2;
      case F32 -> 3;
      case F64 -> 0;
    };
  }

  public record IndexEntry(long offset, long length, long crc32) {
    void pack(ByteBuffer b) {
      b.putLong(offset).putLong(length).putInt((int) crc32).putInt(0);
    }
  }

  // ---- an open crate ----

  private final Path path;
  private final FileChannel ch;
  private final long size;
  private final Header header;
  private final byte[] form;
  private final byte[] text;
  private final byte[] provenance;
  private final byte[] names;
  private final List<IndexEntry> index;

  private Crate(Path path, FileChannel ch, long size, Header header, byte[] form, byte[] text, byte[] provenance, byte[] names,
      List<IndexEntry> index) {
    this.path = path;
    this.ch = ch;
    this.size = size;
    this.header = header;
    this.form = form;
    this.text = text;
    this.provenance = provenance;
    this.names = names;
    this.index = index;
  }

  /** Opens and validates header, footer, index CRC and every index entry against the header. */
  public static Crate open(Path path) throws Refusal {
    FileChannel ch;
    try {
      ch = FileChannel.open(path, StandardOpenOption.READ);
    } catch (NoSuchFileException e) {
      throw refusal(ErrorKind.NOT_FOUND, path + ": no such file");
    } catch (IOException e) {
      throw refusal(ErrorKind.IO_ERROR, path + ": " + e.getMessage());
    }
    String name = String.valueOf(path.getFileName());
    try {
      long size = ch.size();
      Header h = Header.parse(pread(ch, HEADER_BYTES, 0, name, "the header", true));
      byte[] form = pread(ch, h.formLen(), HEADER_BYTES, name, "the metadata form", false);
      if (h.textLen() > Integer.MAX_VALUE - 16) throw refusal(ErrorKind.UNSUPPORTED, name + ": a metadata document over 2 GiB");
      byte[] text = pread(ch, (int) h.textLen(), HEADER_BYTES + h.formLen(), name, "the metadata text", false);
      if (form == null || text == null) {
        throw refusal(ErrorKind.IO_ERROR, name + ": the metadata section is shorter than the header declares");
      }
      if (h.provenanceLen() > Integer.MAX_VALUE - 16 || h.namesLen() > Integer.MAX_VALUE - 16) {
        throw refusal(ErrorKind.UNSUPPORTED, name + ": a provenance or names section over 2 GiB");
      }
      long after = HEADER_BYTES + h.formLen() + h.textLen();
      byte[] provenance = pread(ch, (int) h.provenanceLen(), after, name, "the provenance", false);
      byte[] names = pread(ch, (int) h.namesLen(), after + h.provenanceLen(), name, "the channel names", false);
      if (provenance == null || names == null) {
        throw refusal(ErrorKind.IO_ERROR, name + ": the provenance or names section is shorter than the header declares");
      }
      if (size < FOOTER_BYTES + h.dataStart()) throw refusal(ErrorKind.IO_ERROR, UNFINISHED);
      byte[] foot = pread(ch, FOOTER_BYTES, size - FOOTER_BYTES, name, "the footer", true);
      for (int i = 0; i < 8; i++) {
        if (foot[24 + i] != FOOTER_MAGIC[i]) throw refusal(ErrorKind.IO_ERROR, UNFINISHED);
      }
      ByteBuffer fb = ByteBuffer.wrap(foot).order(ByteOrder.LITTLE_ENDIAN);
      long indexOffset = fb.getLong(0);
      long entries = fb.getLong(8);
      long indexCrc = Integer.toUnsignedLong(fb.getInt(16));
      if (entries != h.blocks()) {
        throw refusal(ErrorKind.IO_ERROR, name + ": the footer declares " + Long.toUnsignedString(entries) + " index entries, the header "
            + h.blocks() + " blocks");
      }
      long indexBytes = entries * INDEX_ENTRY_BYTES;
      if (indexOffset < h.dataStart() || indexOffset + indexBytes != size - FOOTER_BYTES) {
        throw refusal(ErrorKind.IO_ERROR, name + ": the index does not sit between the blocks and the footer");
      }
      byte[] raw = pread(ch, (int) indexBytes, indexOffset, name, "the index", true);
      if (crc32(raw) != indexCrc) throw refusal(ErrorKind.IO_ERROR, name + ": index CRC-32 mismatch — the index is corrupt");
      ByteBuffer ib = ByteBuffer.wrap(raw).order(ByteOrder.LITTLE_ENDIAN);
      List<IndexEntry> index = new ArrayList<>();
      for (int i = 0; i < entries; i++) {
        long off = ib.getLong(i * INDEX_ENTRY_BYTES);
        long len = ib.getLong(i * INDEX_ENTRY_BYTES + 8);
        long crc = Integer.toUnsignedLong(ib.getInt(i * INDEX_ENTRY_BYTES + 16));
        if (len != h.blockBytes()) {
          throw refusal(ErrorKind.IO_ERROR, name + ": index entry " + i + " has length " + Long.toUnsignedString(len) + ", the header says "
              + h.blockBytes());
        }
        if (off < h.dataStart() || off + len > indexOffset) {
          throw refusal(ErrorKind.IO_ERROR, name + ": index entry " + i + " points outside the block region");
        }
        index.add(new IndexEntry(off, len, crc));
      }
      return new Crate(path, ch, size, h, form, text, provenance, names, List.copyOf(index));
    } catch (Refusal | RuntimeException e) {
      closeQuietly(ch);
      throw e;
    } catch (IOException e) {
      closeQuietly(ch);
      throw refusal(ErrorKind.IO_ERROR, name + ": " + e.getMessage());
    }
  }

  private static void closeQuietly(FileChannel ch) {
    try {
      ch.close();
    } catch (IOException ignored) {
      // closing a read-only channel
    }
  }

  /** {@code len} bytes at {@code pos}; a short read is null, or a refusal when {@code strict}. */
  private static byte[] pread(FileChannel ch, int len, long pos, String name, String what, boolean strict) throws IOException, Refusal {
    ByteBuffer buf = ByteBuffer.allocate(len);
    while (buf.hasRemaining()) {
      int n = ch.read(buf, pos + buf.position());
      if (n < 0) break;
    }
    if (buf.hasRemaining()) {
      if (!strict) return null;
      throw refusal(ErrorKind.IO_ERROR, what.equals("the footer") || what.equals("the index") ? UNFINISHED : name + ": short read of " + what);
    }
    return buf.array();
  }

  @Override
  public void close() {
    closeQuietly(ch);
  }

  public boolean isOpen() {
    return ch.isOpen();
  }

  public Path path() {
    return path;
  }

  public String name() {
    return String.valueOf(path.getFileName());
  }

  public long size() {
    return size;
  }

  public Header header() {
    return header;
  }

  public List<IndexEntry> index() {
    return index;
  }

  public byte[] formBytes() {
    return form.clone();
  }

  public byte[] textBytes() {
    return text.clone();
  }

  /** Whether both metadata strings are valid UTF-8 (else the crate carries no document). */
  public boolean metadataUtf8() {
    return utf8(form) != null && utf8(text) != null;
  }

  /**
   * The metadata section as it is, {@code (form, text)} verbatim, or {@code ("", "")} when either
   * is not UTF-8 or the form is empty. For a {@code DERIVED} crate this is its SOURCE's document.
   */
  public String[] document() {
    String f = utf8(form);
    String t = utf8(text);
    if (f == null || t == null || f.isEmpty()) return new String[] {"", ""};
    return new String[] {f, t};
  }

  /** What the reader serves (SPEC.md §4): {@link #document}, or none for a {@code DERIVED} crate. */
  public String[] metadata() {
    return header.derived() ? new String[] {"", ""} : document();
  }

  public byte[] provenanceBytes() {
    return provenance.clone();
  }

  public byte[] namesBytes() {
    return names.clone();
  }

  /**
   * The names section by index ({@code ""} unnamed; it may stop short of {@code c_count}), or null
   * when it is malformed metadata: not UTF-8, or more names than {@code c_count}.
   */
  public List<String> channelNames() {
    if (names.length == 0) return List.of();
    String s = utf8(names);
    if (s == null) return null;
    List<String> out = List.of(s.split("\0", -1));
    return out.size() <= header.c() ? out : null;
  }

  /** Why the names section is not served, or null when it is fine. */
  public String namesProblem() {
    if (channelNames() != null) return null;
    return utf8(names) == null ? "channel names are not valid UTF-8 and are not served"
        : "the crate names more channels than its " + header.c() + " and none are served";
  }

  static String utf8(byte[] b) {
    try {
      return StandardCharsets.UTF_8.newDecoder()
          .onMalformedInput(CodingErrorAction.REPORT)
          .onUnmappableCharacter(CodingErrorAction.REPORT)
          .decode(ByteBuffer.wrap(b)).toString();
    } catch (CharacterCodingException e) {
      return null;
    }
  }

  public String spacingLabel() {
    return Labels.spacing(header.spacingZyx(), header.timeIncrement());
  }

  // ---- reads ----

  public IndexEntry entry(int t, int c) throws Refusal {
    if (t < 0 || t >= header.t()) throw refusal(ErrorKind.OUT_OF_BOUNDS, "t=" + t + " is outside [0, " + header.t() + ") in " + name());
    if (c < 0 || c >= header.c()) throw refusal(ErrorKind.OUT_OF_BOUNDS, "c=" + c + " is outside [0, " + header.c() + ") in " + name());
    return index.get(t * header.c() + c);
  }

  /** The raw bytes of the block for index entry {@code i}, unverified. */
  public byte[] rawBlock(int i) throws Refusal {
    IndexEntry e = index.get(i);
    return read((int) e.length(), e.offset(), "block " + i);
  }

  private byte[] read(int len, long pos, String what) throws Refusal {
    if (!ch.isOpen()) throw refusal(ErrorKind.NOT_FOUND, name() + " is closed");
    try {
      byte[] b = pread(ch, len, pos, name(), what, false);
      if (b == null) throw refusal(ErrorKind.IO_ERROR, "short read of " + what + " in " + name());
      return b;
    } catch (IOException e) {
      if (!ch.isOpen()) throw refusal(ErrorKind.NOT_FOUND, name() + " is closed");
      throw refusal(ErrorKind.IO_ERROR, "reading " + what + " in " + name() + ": " + e.getMessage());
    }
  }

  /** One {@code (Z, Y, X)} volume, CRC-checked when {@code verify}, decoded when the flag is set. */
  public Volume readBlock(int t, int c, boolean verify, Calibration cal) throws Refusal {
    IndexEntry e = entry(t, c);
    byte[] raw = read((int) e.length(), e.offset(), "block (t=" + t + ", c=" + c + ")");
    if (verify && crc32(raw) != e.crc32()) {
      throw refusal(ErrorKind.IO_ERROR, "CRC-32 mismatch at (t=" + t + ", c=" + c + ") in " + name()
          + ": the block's bytes do not match its index entry");
    }
    decode(raw, c, cal, "(t=" + t + ", c=" + c + ")");
    return Volume.of(header.z(), header.y(), header.x(), header.dtype(), MemorySegment.ofArray(raw));
  }

  /** One {@code (1, Y, X)} plane: one seek, one short read, and no CRC (SPEC.md §3). */
  public Volume readPlane(int t, int z, int c, Calibration cal) throws Refusal {
    IndexEntry e = entry(t, c);
    if (z < 0 || z >= header.z()) throw refusal(ErrorKind.OUT_OF_BOUNDS, "z=" + z + " is outside [0, " + header.z() + ") in " + name());
    long pb = header.planeBytes();
    byte[] raw = read((int) pb, e.offset() + z * pb, "plane (t=" + t + ", z=" + z + ", c=" + c + ")");
    decode(raw, c, cal, "(t=" + t + ", c=" + c + ")");
    return Volume.of(1, header.y(), header.x(), header.dtype(), MemorySegment.ofArray(raw));
  }

  private void decode(byte[] raw, int c, Calibration cal, String where) throws Refusal {
    if (!header.calibrated()) return;
    if (cal == null) {
      throw refusal(ErrorKind.IO_ERROR, name() + " " + where + " is calibrated and no calibration table is available — provision"
          + " vizi-crate-cal.bin in Plugin Manager");
    }
    cal.apply(raw, header.dtype(), c);
  }

  // ---- whole-file validation (crate.check_file) ----

  /** Every block's CRC, counted; the first failure named. */
  public record CheckResult(int blocks, List<int[]> failed) {
    public boolean ok() {
      return failed.isEmpty();
    }

    public String summary(String name) {
      if (ok()) return "OK: " + name + " — " + blocks + " blocks, index and footer verified";
      int[] f = failed.get(0);
      return failed.size() + " of " + blocks + " blocks failed CRC in " + name + "; first: (t=" + f[0] + ", c=" + f[1] + ")";
    }
  }

  public static CheckResult check(Path path) throws Refusal {
    try (Crate cr = open(path)) {
      List<int[]> failed = new ArrayList<>();
      int cc = cr.header.c();
      for (int i = 0; i < cr.index.size(); i++) {
        if (crc32(cr.rawBlock(i)) != cr.index.get(i).crc32()) failed.add(new int[] {i / cc, i % cc});
      }
      return new CheckResult(cr.header.blocks(), List.copyOf(failed));
    }
  }
}
