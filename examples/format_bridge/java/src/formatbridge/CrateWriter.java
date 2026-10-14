package formatbridge;

import java.io.IOException;
import java.nio.ByteBuffer;
import java.nio.ByteOrder;
import java.nio.channels.FileChannel;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
import java.util.List;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Refusal;

/**
 * Writes a crate block by block, in arrival order, and finalizes it — or abandons it, leaving
 * nothing behind. Blocks are given as little-endian bytes in the format's fixed layout; {@link
 * #swapped} turns big-endian samples into them.
 */
public final class CrateWriter {

  private final Path path;
  private final Crate.Header header;
  private final byte[] form;
  private final byte[] text;
  private final byte[] provenance;
  private final byte[] names;
  private final Crate.IndexEntry[] entries;
  private FileChannel ch;
  private long position;
  private boolean finished;
  private boolean began;

  /** A crate that is its own: not {@code DERIVED}, with no provenance and no names. */
  public CrateWriter(Path path, int[] shapeTczyx, DType dtype, byte[] form, byte[] text, double[] spacingZyx, double timeIncrement)
      throws Refusal {
    this(path, shapeTczyx, dtype, false, form, text, new byte[0], List.of(), spacingZyx, timeIncrement);
  }

  /**
   * @param derived the metadata section is the source's document, kept as provenance (SPEC.md §7)
   * @param provenance the provenance section, verbatim
   * @param names the channels' names by index, {@code ""} unnamed, at most {@code C}; see {@link #encodeNames}
   * @param spacingZyx micrometres, 0 unknown; malformed values are written as 0
   * @param timeIncrement seconds, 0 unknown
   */
  public CrateWriter(Path path, int[] shapeTczyx, DType dtype, boolean derived, byte[] form, byte[] text, byte[] provenance,
      List<String> names, double[] spacingZyx, double timeIncrement) throws Refusal {
    int code = Crate.codeOf(dtype);
    if (code == 0) throw new Refusal(ErrorKind.UNSUPPORTED, "dtype " + dtype + " has no crate encoding (u8, u16 and f32 do)");
    String[] axes = {"T", "C", "Z", "Y", "X"};
    for (int i = 0; i < 5; i++) {
      if (shapeTczyx[i] <= 0) throw new Refusal(ErrorKind.INVALID_ARGUMENT, "a crate cannot have " + axes[i] + " = " + shapeTczyx[i]);
    }
    if (form.length > 0xFFFF) throw new Refusal(ErrorKind.INVALID_ARGUMENT, "metadata form too long for the crate header's length field");
    if ((long) shapeTczyx[0] * shapeTczyx[1] > Integer.MAX_VALUE) throw new Refusal(ErrorKind.UNSUPPORTED, "too many blocks");
    this.path = path;
    this.names = encodeNames(names, shapeTczyx[1]);
    this.header = new Crate.Header(code, derived ? Crate.FLAG_DERIVED : 0, shapeTczyx[0], shapeTczyx[1], shapeTczyx[2], shapeTczyx[3],
        shapeTczyx[4], form.length, text.length, Crate.sanitize(spacingZyx[0]), Crate.sanitize(spacingZyx[1]),
        Crate.sanitize(spacingZyx[2]), Crate.sanitize(timeIncrement), provenance.length, this.names.length, List.of());
    this.form = form.clone();
    this.text = text.clone();
    this.provenance = provenance.clone();
    this.entries = new Crate.IndexEntry[header.blocks()];
  }

  public Path path() {
    return path;
  }

  public String name() {
    return String.valueOf(path.getFileName());
  }

  public Crate.Header header() {
    return header;
  }

  /**
   * The names section: {@code 0x00}-separated UTF-8, trailing {@code ""}s dropped (so naming none is
   * zero bytes), a name holding {@code 0x00} written as {@code ""}.
   */
  public static byte[] encodeNames(List<String> names, int cCount) throws Refusal {
    if (names.size() > cCount) throw new Refusal(ErrorKind.INVALID_ARGUMENT, names.size() + " channel names for " + cCount + " channels");
    List<String> out = new java.util.ArrayList<>();
    for (String n : names) out.add(n.indexOf('\0') >= 0 ? "" : n);
    while (!out.isEmpty() && out.get(out.size() - 1).isEmpty()) out.remove(out.size() - 1);
    return String.join("\0", out).getBytes(java.nio.charset.StandardCharsets.UTF_8);
  }

  /** Creates the file and writes the header and the metadata, provenance and names sections. */
  public void begin() throws Refusal {
    try {
      ch = FileChannel.open(path, StandardOpenOption.CREATE, StandardOpenOption.TRUNCATE_EXISTING, StandardOpenOption.WRITE);
      began = true;
      writeAll(ByteBuffer.wrap(header.pack()));
      writeAll(ByteBuffer.wrap(form));
      writeAll(ByteBuffer.wrap(text));
      writeAll(ByteBuffer.wrap(provenance));
      writeAll(ByteBuffer.wrap(names));
    } catch (IOException e) {
      abort();
      throw new Refusal(ErrorKind.IO_ERROR, "cannot write " + path + ": " + e.getMessage());
    }
  }

  private void writeAll(ByteBuffer b) throws IOException {
    while (b.hasRemaining()) position += ch.write(b);
  }

  /**
   * Appends one volume for artifact indices {@code (t, c)}. A repeat {@code (t, c)} wins: the new
   * block is appended and the index slot overwritten, the old bytes left stranded (SPEC.md §8).
   *
   * @return whether the slot already held a block
   */
  public boolean writeBlock(int t, int c, byte[] le) throws Refusal {
    if (ch == null) throw new Refusal(ErrorKind.IO_ERROR, "the writer of " + name() + " has not begun, or has ended");
    if (t < 0 || t >= header.t()) throw new Refusal(ErrorKind.OUT_OF_BOUNDS, "artifact timepoint " + t + " is outside [0, " + header.t() + ")");
    if (c < 0 || c >= header.c()) throw new Refusal(ErrorKind.OUT_OF_BOUNDS, "artifact channel " + c + " is outside [0, " + header.c() + ")");
    if (le.length != header.blockBytes()) {
      throw new Refusal(ErrorKind.INVALID_ARGUMENT, "a block of " + le.length + " bytes where the declared geometry needs " + header.blockBytes());
    }
    long at = position;
    try {
      writeAll(ByteBuffer.wrap(le));
    } catch (IOException e) {
      throw new Refusal(ErrorKind.IO_ERROR, "writing (t=" + t + ", c=" + c + ") of " + name() + ": " + e.getMessage());
    }
    int slot = t * header.c() + c;
    boolean repeat = entries[slot] != null;
    entries[slot] = new Crate.IndexEntry(at, le.length, Crate.crc32(le));
    return repeat;
  }

  /** How many slots never received a block, and the first as {@code (t, c)}, or null when none. */
  public int[] missing() {
    int n = 0;
    int first = -1;
    for (int i = 0; i < entries.length; i++) {
      if (entries[i] == null) {
        if (first < 0) first = i;
        n++;
      }
    }
    return n == 0 ? null : new int[] {n, first / header.c(), first % header.c()};
  }

  /** Index, footer, flush, close. An incomplete index fails the finish and deletes the file. */
  public void finish() throws Refusal {
    if (ch == null) throw new Refusal(ErrorKind.IO_ERROR, "the writer of " + name() + " has not begun, or has ended");
    int[] miss = missing();
    if (miss != null) {
      abort();
      throw new Refusal(ErrorKind.IO_ERROR, miss[0] + " of " + header.blocks() + " blocks never arrived (first missing: t=" + miss[1]
          + ", c=" + miss[2] + "); " + name() + " was not written");
    }
    ByteBuffer index = ByteBuffer.allocate(entries.length * Crate.INDEX_ENTRY_BYTES).order(ByteOrder.LITTLE_ENDIAN);
    for (Crate.IndexEntry e : entries) e.pack(index);
    byte[] ib = index.array();
    ByteBuffer footer = ByteBuffer.allocate(Crate.FOOTER_BYTES).order(ByteOrder.LITTLE_ENDIAN);
    footer.putLong(position).putLong(entries.length).putInt((int) Crate.crc32(ib)).putInt(0).put(Crate.FOOTER_MAGIC);
    try {
      writeAll(ByteBuffer.wrap(ib));
      writeAll(footer.flip());
      ch.force(true);
      ch.close();
      ch = null;
      finished = true;
    } catch (IOException e) {
      abort();
      throw new Refusal(ErrorKind.IO_ERROR, "finalizing " + name() + ": " + e.getMessage() + "; the partial file was removed");
    }
  }

  /** Closes and deletes the file unless it was finalized; idempotent. */
  public void abort() {
    if (finished || !began) return;
    FileChannel c = ch;
    ch = null;
    if (c != null) {
      try {
        c.close();
      } catch (IOException ignored) {
        // deleting it anyway
      }
    }
    try {
      Files.deleteIfExists(path);
    } catch (IOException ignored) {
      // nothing more to do
    }
  }

  /** Big-endian samples of {@code itemsize} bytes, reversed into the format's little-endian layout. */
  public static byte[] swapped(byte[] be, int itemsize) {
    byte[] out = be.clone();
    if (itemsize == 1) return out;
    for (int i = 0; i + itemsize <= out.length; i += itemsize) {
      for (int k = 0; k < itemsize; k++) out[i + k] = be[i + itemsize - 1 - k];
    }
    return out;
  }
}
