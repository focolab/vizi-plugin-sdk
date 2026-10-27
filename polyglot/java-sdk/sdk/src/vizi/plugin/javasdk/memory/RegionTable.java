package vizi.plugin.javasdk.memory;

import java.util.ArrayList;
import java.util.Comparator;
import java.util.List;
import java.util.OptionalLong;
import java.util.Set;
import java.util.function.BiPredicate;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.MemoryGrant;
import vizi.plugin.v1.ViziPlugin.GrantFailureClass;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.Region;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;

/**
 * An {@code InstallSegment}'s region table, validated against shared_memory_protocol.md §8's
 * receiver matrix (and processing_stages.md §4's rules for {@code S}). A receiver rules on this
 * table and on nothing inferred from a field's prose.
 *
 * <p>Checks run in a fixed order and the first failure decides the class: the segment-level
 * fields ({@code purpose}, {@code subject}, {@code supersedes}), then each region in index order,
 * then the cross-region rules (overlap, fit, the one-arena and one-lane limits).
 */
public final class RegionTable {
  private RegionTable() {}

  /** The purposes this SDK implements (§3.1's table). Matched as whole strings, exactly. */
  public static final Set<String> PURPOSES = Set.of("G", "V", "O", "P", "X", "S");

  /** One validated region. {@code epochOffset} is absent when the region is not pane-associated. */
  public record Spec(
      int index,
      long offset,
      long extent,
      RegionDiscipline discipline,
      RegionProducer producer,
      int buffers,
      long slotBytes,
      String id,
      OptionalLong epochOffset) {

    public boolean pluginProduced() {
      return producer == RegionProducer.REGION_PRODUCER_PLUGIN;
    }

    public boolean contains(long mappingOffset, long len) {
      return mappingOffset >= offset && len >= 0 && mappingOffset - offset <= extent - len;
    }
  }

  /** A failed validation: its class and the detail to surface. */
  public record Failure(GrantFailureClass failureClass, String detail) {}

  /** Either the validated regions or the failure. */
  public record Result(List<Spec> regions, @Nullable Failure failure) {
    static Result fail(GrantFailureClass c, String detail) {
      return new Result(List.of(), new Failure(c, detail));
    }
  }

  private static final GrantFailureClass MALFORMED = GrantFailureClass.GRANT_FAILURE_MALFORMED;
  private static final GrantFailureClass UNSUPPORTED = GrantFailureClass.GRANT_FAILURE_UNSUPPORTED;

  /**
   * Validates {@code install} against a mapping of {@code size} bytes granted with {@code perms}.
   *
   * @param knownSubject answers whether a purpose's subject names a contribution this plugin
   *     declared ({@code O}: an overlay, {@code P}: a panel), or null when the manifest is not
   *     known, in which case the name is trusted (§8: the check is a strengthening, never an
   *     obligation on a receiver that cannot perform it)
   */
  public static Result validate(
      InstallSegment install, long size, int perms, @Nullable BiPredicate<String, String> knownSubject) {
    String purpose = install.getPurpose();
    if (purpose.isEmpty()) return Result.fail(MALFORMED, "purpose is empty");
    if (!PURPOSES.contains(purpose)) return Result.fail(UNSUPPORTED, "purpose \"" + purpose + "\" is not one this SDK implements");
    boolean needsSubject = purpose.equals("O") || purpose.equals("P");
    String subject = install.getSubject();
    if (needsSubject && subject.isEmpty()) {
      return Result.fail(MALFORMED, "purpose " + purpose + " requires a non-empty subject");
    }
    if (!needsSubject && !subject.isEmpty()) {
      return Result.fail(MALFORMED, "purpose " + purpose + " requires an empty subject, got \"" + subject + "\"");
    }
    if (needsSubject && knownSubject != null && !knownSubject.test(purpose, subject)) {
      return Result.fail(MALFORMED, "subject \"" + subject + "\" names no "
          + (purpose.equals("O") ? "overlay" : "panel") + " this plugin declares");
    }
    if (install.getSupersedes() != 0 && install.getSupersedes() == install.getSegmentId()) {
      return Result.fail(MALFORMED, "supersedes names the segment being installed");
    }

    List<Region> rs = install.getRegionsList();
    int n = rs.size();
    boolean staged = purpose.equals("S");
    if (staged && n > 512) return Result.fail(MALFORMED, n + " stage slots; a ring holds at most 512");
    boolean[] seen = new boolean[n];
    for (Region r : rs) {
      long ix = Integer.toUnsignedLong(r.getIndex());
      if (ix >= n) return Result.fail(MALFORMED, "region index " + ix + " is out of range for " + n + " regions");
      if (seen[(int) ix]) return Result.fail(MALFORMED, "region index " + ix + " appears twice");
      seen[(int) ix] = true;
    }
    Spec[] specs = new Spec[n];
    boolean pluginWrite = (perms & MemoryGrant.PLUGIN_WRITE) != 0;
    for (Region r : rs.stream().sorted(Comparator.comparingInt(Region::getIndex)).toList()) {
      int ix = r.getIndex();
      String at = "region " + ix + ": ";
      RegionDiscipline d = r.getDiscipline();
      if (r.getDisciplineValue() == 0) return Result.fail(MALFORMED, at + "discipline is unspecified");
      if (d == RegionDiscipline.UNRECOGNIZED) {
        return Result.fail(UNSUPPORTED, at + "discipline " + r.getDisciplineValue() + " is not one this SDK implements");
      }
      if (r.getProducerValue() == 0) return Result.fail(MALFORMED, at + "producer is unspecified");
      if (r.getProducer() == RegionProducer.UNRECOGNIZED) {
        return Result.fail(UNSUPPORTED, at + "producer " + r.getProducerValue() + " is not one this SDK implements");
      }
      long off = r.getOffset();
      long ext = r.getExtent();
      if (Long.compareUnsigned(off, MemoryGrant.PREAMBLE) < 0) {
        return Result.fail(MALFORMED, at + "offset " + Long.toUnsignedString(off) + " overlaps the 32-byte preamble");
      }
      if (ext == 0) return Result.fail(MALFORMED, at + "extent is zero");
      if (Long.compareUnsigned(off, size) > 0 || Long.compareUnsigned(ext, size - off) > 0) {
        return Result.fail(MALFORMED, at + "[" + Long.toUnsignedString(off) + ", +" + Long.toUnsignedString(ext)
            + ") lies outside the " + Long.toUnsignedString(size) + "-byte mapping");
      }
      int buffers = r.getBuffers();
      long slot = r.getSlotBytes();
      OptionalLong eo = r.hasEpochOffset() ? OptionalLong.of(r.getEpochOffset()) : OptionalLong.empty();
      switch (d) {
        case REGION_DISCIPLINE_PULSED -> {
          if (Integer.compareUnsigned(buffers, 3) < 0) return Result.fail(MALFORMED, at + "PULSED needs at least 3 buffers, has " + buffers);
          if (off % 8 != 0) return Result.fail(MALFORMED, at + "PULSED offset " + off + " is not 8-aligned");
          long want = pulsedExtent(Integer.toUnsignedLong(buffers), slot);
          if (want < 0 || want != ext) {
            return Result.fail(MALFORMED, at + "PULSED extent " + ext + " is not buffers*16 + buffers*slot_bytes = "
                + (want < 0 ? "overflow" : Long.toString(want)));
          }
          if (eo.isPresent()) {
            if (Long.compareUnsigned(slot, 8) <= 0) return Result.fail(MALFORMED, at + "slot_bytes " + slot + " leaves no payload beside the pane header");
            if (eo.getAsLong() != slot - 8) {
              return Result.fail(MALFORMED, at + "PULSED epoch_offset " + eo.getAsLong() + " is not slot_bytes - 8 = " + (slot - 8));
            }
          }
          if (!pluginWrite) return Result.fail(MALFORMED, at + "a PULSED region needs PLUGIN_WRITE, whichever end produces it");
        }
        case REGION_DISCIPLINE_STAGED -> {
          if (!staged) return Result.fail(MALFORMED, at + "a STAGED region in a segment of purpose " + purpose);
          if (r.getProducer() != RegionProducer.REGION_PRODUCER_HOST) return Result.fail(MALFORMED, at + "a stage slot must be host-produced");
          if (off % 64 != 0) return Result.fail(MALFORMED, at + "stage slot offset " + off + " is not a multiple of 64");
          if (Long.compareUnsigned(ext, 256) < 0) return Result.fail(MALFORMED, at + "stage slot extent " + ext + " is under the 256 header bytes");
          if (buffers != 0 || slot != 0) return Result.fail(MALFORMED, at + "a stage slot carries buffers/slot_bytes");
          if (eo.isPresent()) return Result.fail(MALFORMED, at + "a stage slot declares an epoch_offset");
          if (!pluginWrite) return Result.fail(MALFORMED, at + "a stage ring needs PLUGIN_WRITE");
        }
        default -> {
          if (buffers != 0) return Result.fail(MALFORMED, at + "buffers " + buffers + " on a " + name(d) + " region");
          if (eo.isPresent() && (Long.compareUnsigned(eo.getAsLong(), ext) > 0 || Long.compareUnsigned(eo.getAsLong() + 8, ext) > 0)) {
            return Result.fail(MALFORMED, at + "epoch_offset " + eo.getAsLong() + " + 8 does not fit the extent " + ext);
          }
        }
      }
      if (staged && d != RegionDiscipline.REGION_DISCIPLINE_STAGED) {
        return Result.fail(MALFORMED, at + "a stage ring holds only STAGED regions, not " + name(d));
      }
      if (r.getProducer() == RegionProducer.REGION_PRODUCER_PLUGIN && !pluginWrite) {
        return Result.fail(MALFORMED, at + "a plugin-produced region needs PLUGIN_WRITE");
      }
      specs[ix] = new Spec(ix, off, ext, d, r.getProducer(), buffers, slot, r.getId(), eo);
    }
    // cross-region rules
    List<Spec> byOffset = new ArrayList<>(List.of(specs));
    byOffset.sort(Comparator.comparingLong(Spec::offset));
    long total = MemoryGrant.PREAMBLE;
    for (int i = 0; i < byOffset.size(); i++) {
      Spec a = byOffset.get(i);
      if (i + 1 < byOffset.size()) {
        Spec b = byOffset.get(i + 1);
        if (Long.compareUnsigned(a.offset() + a.extent(), b.offset()) > 0) {
          return Result.fail(MALFORMED, "regions " + a.index() + " and " + b.index() + " overlap");
        }
      }
      total += a.extent();
      if (Long.compareUnsigned(total, size) > 0 || total < 0) {
        return Result.fail(MALFORMED, "32 + the regions' extents exceeds the " + size + "-byte mapping");
      }
    }
    int loaned = 0;
    int pulsed = 0;
    for (Spec s : specs) {
      if (s.pluginProduced() && s.discipline() == RegionDiscipline.REGION_DISCIPLINE_LOANED) loaned++;
      if (s.pluginProduced() && s.discipline() == RegionDiscipline.REGION_DISCIPLINE_PULSED) pulsed++;
    }
    if (loaned > 1) return Result.fail(MALFORMED, loaned + " plugin-produced LOANED regions; a segment may declare one");
    if (pulsed > 1) return Result.fail(MALFORMED, pulsed + " plugin-produced PULSED regions; a segment may declare one");
    return new Result(List.of(specs), null);
  }

  /** {@code N*16 + N*S}, or -1 on overflow. */
  static long pulsedExtent(long n, long s) {
    try {
      return Math.addExact(Math.multiplyExact(n, 16L), Math.multiplyExact(n, s));
    } catch (ArithmeticException e) {
      return -1;
    }
  }

  static String name(RegionDiscipline d) {
    return d.name().replace("REGION_DISCIPLINE_", "");
  }
}
