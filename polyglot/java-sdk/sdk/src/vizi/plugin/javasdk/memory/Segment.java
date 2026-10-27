package vizi.plugin.javasdk.memory;

import java.lang.foreign.MemorySegment;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.TreeMap;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;

/**
 * An installed segment: its mapping, its validated region table, and the discipline handles for
 * the regions this endpoint uses. Operations on it go through {@link SegmentStore}, which
 * enforces retirement and quarantine at the operation, not at the handle (§9.1).
 */
public final class Segment {

  public enum State {
    LIVE,
    /** {@code RetireSegment} arrived: finishing is permitted, starting is not. */
    RETIRING,
    /** Unmapped and closed; {@code SegmentReleased} sent. */
    RELEASED
  }

  final long id;
  final String purpose;
  final String subject;
  final Mapping mapping;
  final List<RegionTable.Spec> regions;
  final byte[] proof;
  final int perms;

  volatile State state = State.LIVE;
  int inflight;
  final @Nullable LoanArena arena;
  final @Nullable PulsedLane pluginLane;
  final Map<Integer, PulsedLane> hostLanes = new TreeMap<>();
  boolean arenaQuarantined;

  Segment(long id, String purpose, String subject, Mapping mapping, List<RegionTable.Spec> regions, byte[] proof,
      int perms, PulsedLane.Reporter reporter) {
    this.id = id;
    this.purpose = purpose;
    this.subject = subject;
    this.mapping = mapping;
    this.regions = List.copyOf(regions);
    this.proof = proof.clone();
    this.perms = perms;
    LoanArena a = null;
    PulsedLane lane = null;
    for (RegionTable.Spec s : regions) {
      if (s.discipline() == RegionDiscipline.REGION_DISCIPLINE_LOANED && s.pluginProduced()) {
        a = new LoanArena(id, s.index(), s.offset(), s.extent(), mapping.segment());
      } else if (s.discipline() == RegionDiscipline.REGION_DISCIPLINE_PULSED) {
        PulsedLane l = new PulsedLane(s, id, mapping.segment(), reporter);
        if (s.pluginProduced()) lane = l;
        else hostLanes.put(s.index(), l);
      }
    }
    this.arena = a;
    this.pluginLane = lane;
  }

  /** The segment's lifecycle state, as of this read. */
  public State state() {
    return state;
  }

  public long id() {
    return id;
  }

  public String purpose() {
    return purpose;
  }

  public String subject() {
    return subject;
  }

  public List<RegionTable.Spec> regions() {
    return regions;
  }

  public long size() {
    return mapping.size;
  }

  public Optional<LoanArena> arena() {
    return Optional.ofNullable(arena);
  }

  public Optional<PulsedLane> pluginLane() {
    return Optional.ofNullable(pluginLane);
  }

  public Map<Integer, PulsedLane> hostLanes() {
    return hostLanes;
  }

  /** The mapping, read-only when the grant is; use only inside an operation. */
  MemorySegment memory() {
    return mapping.segment();
  }

  /** The host-produced LOANED region containing {@code [offset, offset+len)}, if any. */
  RegionTable.@Nullable Spec hostDepositRegion(long offset, long len) {
    for (RegionTable.Spec s : regions) {
      if (s.discipline() == RegionDiscipline.REGION_DISCIPLINE_LOANED
          && s.producer() == RegionProducer.REGION_PRODUCER_HOST
          && s.contains(offset, len)) {
        return s;
      }
    }
    return null;
  }

  /** The {@code STAGED} slot regions, in index order. */
  public List<RegionTable.Spec> stagedSlots() {
    return regions.stream().filter(s -> s.discipline() == RegionDiscipline.REGION_DISCIPLINE_STAGED).toList();
  }

  @Override
  public String toString() {
    return "segment " + Long.toUnsignedString(id) + " (" + purpose + (subject.isEmpty() ? "" : " " + subject) + ", "
        + state + ")";
  }
}
