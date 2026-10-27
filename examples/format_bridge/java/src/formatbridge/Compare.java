package formatbridge;

import java.io.IOException;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import vizi.plugin.javasdk.ErrorKind;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Volume;

/**
 * The semantic comparator (SPEC.md §"The round trip"): which of the three tiers two crates match
 * at, never a boolean.
 *
 * <pre>
 *   byte-equal          the files are identical (both open as crates)
 *   semantically equal  voxel-equal, plus metadata form and text byte for byte, header spacing and time increment,
 *                       and channel names (DERIVED and the provenance record are only noted)
 *   voxel-equal         same dtype and shape, identical decoded voxels
 *   different           anything less
 * </pre>
 *
 * Voxels are compared DECODED: a calibrated crate and the plain crate it decodes to are
 * voxel-equal (or better), with a note. A block that fails its CRC is compared on its bytes, with
 * a note. The flag word, reserved bytes and block layout are not compared.
 */
public final class Compare {
  private Compare() {}

  public static final String BYTE_EQUAL = "byte-equal";
  public static final String SEMANTIC = "semantically equal";
  public static final String VOXEL = "voxel-equal";
  public static final String DIFFERENT = "different";

  public record Verdict(String tier, List<String> notes) {
    @Override
    public String toString() {
      return notes.isEmpty() ? tier : tier + ": " + String.join("; ", notes);
    }
  }

  /** Throws {@link Refusal} when either file cannot be opened or decoded. */
  public static Verdict compare(Path a, Path b, Calibration cal) throws Refusal {
    List<String> notes = new ArrayList<>();
    try (Crate ca = Crate.open(a); Crate cb = Crate.open(b)) {
      Crate.Header ha = ca.header();
      Crate.Header hb = cb.header();
      if (Arrays.equals(bytes(a), bytes(b))) {
        decodeAll(ca, cal, notes); // a calibrated crate with no table cannot be decoded
        return new Verdict(BYTE_EQUAL, notes);
      }
      if (ha.dtypeCode() != hb.dtypeCode()) return new Verdict(DIFFERENT, List.of("dtype " + ha.dtypeName() + " vs " + hb.dtypeName()));
      if (ha.t() != hb.t() || ha.c() != hb.c() || ha.z() != hb.z() || ha.y() != hb.y() || ha.x() != hb.x()) {
        return new Verdict(DIFFERENT, List.of("shape " + ha.shape() + " vs " + hb.shape()));
      }
      List<byte[]> va = decodeAll(ca, cal, notes);
      List<byte[]> vb = decodeAll(cb, cal, notes);
      int differing = 0;
      int first = -1;
      for (int i = 0; i < va.size(); i++) {
        if (!Arrays.equals(va.get(i), vb.get(i))) {
          if (first < 0) first = i;
          differing++;
        }
      }
      if (differing > 0) {
        notes.add(differing + " of " + ha.blocks() + " volumes differ; first: (t=" + first / ha.c() + ", c=" + first % ha.c() + ")");
        return new Verdict(DIFFERENT, notes);
      }
      boolean semantic = true;
      if (!Arrays.equals(ca.formBytes(), cb.formBytes()) || !Arrays.equals(ca.textBytes(), cb.textBytes())) {
        semantic = false;
        notes.add("metadata form or text differs byte-for-byte (" + ca.textBytes().length + " vs " + cb.textBytes().length + " bytes)");
      }
      if (!Arrays.equals(ha.spacingZyx(), hb.spacingZyx()) || Double.compare(ha.timeIncrement(), hb.timeIncrement()) != 0) {
        semantic = false;
        notes.add("header spacing or time increment differs (" + Arrays.toString(ha.spacingZyx()) + ", " + ha.timeIncrement() + " vs "
            + Arrays.toString(hb.spacingZyx()) + ", " + hb.timeIncrement() + ")");
      }
      List<String> na = ca.channelNames();
      List<String> nb = cb.channelNames();
      if (!java.util.Objects.equals(na, nb)) {
        semantic = false;
        notes.add("channel names differ (" + names(na) + " vs " + names(nb) + ")");
      }
      if (ha.calibrated() != hb.calibrated()) {
        notes.add("one crate stores calibrated counts and the other decoded values (equal once decoded)");
      }
      if (ha.derived() != hb.derived()) {
        notes.add((ha.derived() ? a : b).getFileName() + " is DERIVED: its document is its source's, and its reader serves none");
      }
      if (!Arrays.equals(ca.provenanceBytes(), cb.provenanceBytes())) {
        notes.add("provenance records differ (" + ca.provenanceBytes().length + " vs " + cb.provenanceBytes().length
            + " bytes; not compared)");
      }
      return new Verdict(semantic ? SEMANTIC : VOXEL, notes);
    }
  }

  private static String names(List<String> n) {
    if (n == null) return "malformed";
    if (n.isEmpty()) return "none";
    List<String> quoted = new ArrayList<>();
    for (String s : n) quoted.add("'" + s + "'");
    return String.join(", ", quoted);
  }

  private static byte[] bytes(Path p) throws Refusal {
    try {
      return Files.readAllBytes(p);
    } catch (IOException e) {
      throw new Refusal(ErrorKind.IO_ERROR, p + ": " + e.getMessage());
    }
  }

  /**
   * Every block decoded, in index order. A float's bits are compared, so a NaN equals the same
   * NaN and {@code -0.0} differs from {@code 0.0}: the tier is about bytes surviving, not arithmetic.
   */
  private static List<byte[]> decodeAll(Crate cr, Calibration cal, List<String> notes) throws Refusal {
    Crate.Header h = cr.header();
    List<byte[]> out = new ArrayList<>();
    for (int i = 0; i < cr.index().size(); i++) {
      int t = i / h.c();
      int c = i % h.c();
      if (Crate.crc32(cr.rawBlock(i)) != cr.index().get(i).crc32()) {
        notes.add(cr.name() + ": block (t=" + t + ", c=" + c + ") fails its CRC-32; compared on its bytes anyway");
      }
      Volume v = cr.readBlock(t, c, false, cal);
      out.add(v.data().toArray(ValueLayout.JAVA_BYTE));
    }
    return out;
  }

  static byte[] toBytes(MemorySegment s) {
    return s.toArray(ValueLayout.JAVA_BYTE);
  }
}
