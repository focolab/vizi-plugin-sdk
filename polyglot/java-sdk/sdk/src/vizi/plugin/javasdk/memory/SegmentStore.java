package vizi.plugin.javasdk.memory;

import com.google.protobuf.ByteString;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.function.BiPredicate;
import java.util.function.Consumer;
import java.util.function.IntToLongFunction;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.MemoryGrant;
import vizi.plugin.javasdk.doorbell.Notification;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.v1.ViziPlugin.GrantAck;
import vizi.plugin.v1.ViziPlugin.GrantFailed;
import vizi.plugin.v1.ViziPlugin.GrantFailureClass;
import vizi.plugin.v1.ViziPlugin.GrantInstalled;
import vizi.plugin.v1.ViziPlugin.GrantState;
import vizi.plugin.v1.ViziPlugin.GrantStateKind;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;
import vizi.plugin.v1.ViziPlugin.ReleaseDeposit;
import vizi.plugin.v1.ViziPlugin.ReleaseLoan;
import vizi.plugin.v1.ViziPlugin.ReleaseRequest;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.SegmentReleased;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * The plugin's half of the memory lifecycle (shared_memory_protocol.md §8, §9, §9a), keyed by
 * {@code segment_id}: the grant's two halves in either order, the terminal answers replayed to
 * duplicates, retirement that permits finishing and forbids starting, and release that unmaps
 * AND closes before {@code SegmentReleased}.
 *
 * <p><b>This store is the only producer of {@code GrantAck}</b>, including for an {@code M} the
 * transport refused, so a request waiting on a grant is never stranded by a path that forgot to
 * answer. Its outbound messages go to a {@link Sink} that must not block.
 *
 * <p>Thread-safe. Halves arrive on the doorbell listener ({@code M}) and the dispatch thread
 * ({@code InstallSegment}); the mapping is made under the store's lock, which is session-scale
 * and bounded. Settlement callbacks run after the lock is released.
 */
public final class SegmentStore {

  /** Where the store's messages go. One-shot pushes; never blocks. */
  public interface Sink {
    void push(PluginMsg msg);

    /** A ContractViolation occurrence; {@code record} is empty when there was none. */
    void violation(ViolationSite where, String detail, byte[] record);

    void diagnostic(String text);

    /** A debug-level line: only when tracing. */
    default void trace(String text) {}
  }

  /** Brings a region's scheduled check forward (a {@code D} hint). */
  public interface Hints {
    void bringForward(long segmentId, int region);
  }

  /** How a segment's grant settled, for a message held until it did. */
  public sealed interface Settlement {
    record Installed(Segment segment) implements Settlement {}

    record Failed(GrantFailureClass failureClass, String detail) implements Settlement {}

    record Released() implements Settlement {}
  }

  /**
   * A start refused on a normal retirement: the segment is retiring or released, and no live
   * segment of its purpose and subject supersedes it. Not an error: the caller drops the operation
   * quietly ({@link #retiredDrops()} counts it).
   */
  public static final class RetiredException extends UnavailableException {
    private static final long serialVersionUID = 1L;

    public RetiredException(String message) {
      super(message);
    }
  }

  /** An operation could not run: the segment is missing, retiring, released or quarantined. */
  public static class UnavailableException extends Exception {
    private static final long serialVersionUID = 1L;

    public UnavailableException(String message) {
      super(message);
    }
  }

  /**
   * Two live segments of one purpose and subject: the host granted a second without superseding
   * the first. The host's error, never a hold.
   */
  public static final class AmbiguousException extends UnavailableException {
    private static final long serialVersionUID = 1L;

    public AmbiguousException(String message) {
      super(message);
    }
  }

  /** A host deposit that cannot be read. */
  public static final class DepositException extends Exception {
    private static final long serialVersionUID = 1L;

    public DepositException(String message) {
      super(message);
    }
  }

  private enum Kind {
    PENDING,
    INSTALLED,
    FAILED,
    RELEASED
  }

  /** The {@code M} half: the record, and the descriptor (unix) or section name (Windows). */
  private static final class Capability {
    final MemoryGrant grant;
    final byte[] raw;
    int fd;
    @Nullable Mapping mapping;

    Capability(MemoryGrant grant, byte[] raw, int fd) {
      this.grant = grant;
      this.raw = raw.clone();
      this.fd = fd;
    }

    void discard() {
      if (mapping != null) {
        mapping.release();
        mapping = null;
        fd = -1;
      } else if (fd >= 0) {
        vizi.plugin.javasdk.nativeio.Posix.close(fd);
        fd = -1;
      }
    }
  }

  private static final class Entry {
    final long id;
    Kind kind = Kind.PENDING;
    @Nullable Capability cap;
    @Nullable InstallSegment install;
    byte @Nullable [] recordedM;
    @Nullable InstallSegment recordedInstall;
    @Nullable Segment segment;
    GrantFailureClass failClass = GrantFailureClass.GRANT_FAILURE_UNSPECIFIED;
    String failDetail = "";
    final List<Consumer<Settlement>> waiters = new ArrayList<>();

    Entry(long id) {
      this.id = id;
    }
  }

  private record DepositKey(long segment, long offset, long len) {}

  private final Sink sink;
  private final @Nullable BiPredicate<String, String> knownSubject;
  private volatile Hints hints = (s, r) -> {};
  private volatile Consumer<Segment> onInstalled = s -> {};
  private final Object lock = new Object();
  private final Map<Long, Entry> entries = new HashMap<>();
  private final Map<DepositKey, int[]> deposits = new HashMap<>();
  private long hintsIgnored;

  /**
   * @param knownSubject whether an {@code O}/{@code P} subject names a declared contribution, or
   *     null to trust the name (no manifest known)
   */
  public SegmentStore(Sink sink, @Nullable BiPredicate<String, String> knownSubject) {
    this.sink = sink;
    this.knownSubject = knownSubject;
  }

  public void setHints(Hints hints) {
    this.hints = hints;
  }

  /** Called with each segment as it installs, after the store's lock is released. */
  public void setOnInstalled(Consumer<Segment> onInstalled) {
    this.onInstalled = onInstalled;
  }

  // ---- the two halves ----

  /**
   * The {@code M} half, as the transport handed it up. The descriptor is claimed here, before
   * anything can fail, so every path below owns exactly one close.
   */
  public void onGrant(Outcome.GrantBody body, byte[] raw) {
    int fd = body.descriptor().map(vizi.plugin.javasdk.doorbell.Descriptor::claim).orElse(-1);
    Capability cap = new Capability(body.grant(), raw, fd);
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      Entry e = entries.computeIfAbsent(body.grant().segmentId(), Entry::new);
      switch (e.kind) {
        case RELEASED -> {
          cap.discard();
          after.add(() -> pushReleased(e.id));
        }
        case INSTALLED, FAILED -> {
          cap.discard();
          if (e.recordedM == null || Arrays.equals(e.recordedM, raw)) {
            if (e.recordedM == null) e.recordedM = raw.clone();
            after.add(replay(e));
          } else {
            after.add(conflict(e.id, "a second, different M for segment " + Long.toUnsignedString(e.id)));
          }
        }
        case PENDING -> {
          if (e.cap != null) {
            cap.discard();
            if (!Arrays.equals(e.cap.raw, raw)) {
              after.add(conflict(e.id, "a second, different M while the grant is pending"));
            }
          } else {
            e.cap = cap;
            e.recordedM = raw.clone();
            mapCapability(e, after);
          }
        }
      }
    }
    after.forEach(Runnable::run);
  }

  /** An {@code M} the transport rejected on its own record-local checks. */
  public void onGrantRejected(long segmentId, byte[] raw, String detail) {
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      Entry e = entries.computeIfAbsent(segmentId, Entry::new);
      switch (e.kind) {
        case RELEASED -> after.add(() -> pushReleased(e.id));
        case INSTALLED, FAILED -> {
          if (e.recordedM != null && Arrays.equals(e.recordedM, raw)) after.add(replay(e));
          else if (e.kind == Kind.FAILED && e.recordedM == null) {
            e.recordedM = raw.clone();
            after.add(replay(e));
          } else after.add(conflict(e.id, "a second, different M: " + detail));
        }
        case PENDING -> {
          if (e.cap != null) {
            after.add(conflict(e.id, "a second, different M while pending: " + detail));
          } else {
            e.recordedM = raw.clone();
            fail(e, GrantFailureClass.GRANT_FAILURE_MALFORMED, detail, after);
          }
        }
      }
    }
    after.forEach(Runnable::run);
  }

  /** The {@code InstallSegment} half, standalone or riding a reply. */
  public void onInstall(InstallSegment install) {
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      long id = install.getSegmentId();
      if (id == 0) {
        after.add(() -> sink.diagnostic("InstallSegment for segment_id 0 ignored: 0 names no segment"));
      } else {
        Entry e = entries.computeIfAbsent(id, Entry::new);
        switch (e.kind) {
          case RELEASED -> after.add(() -> pushReleased(e.id));
          case INSTALLED, FAILED -> {
            if (e.recordedInstall == null || e.recordedInstall.equals(install)) {
              if (e.recordedInstall == null) e.recordedInstall = install;
              after.add(replay(e));
            } else {
              after.add(conflict(e.id, "a second, different InstallSegment for segment " + Long.toUnsignedString(id)));
            }
          }
          case PENDING -> {
            if (e.install != null) {
              if (!e.install.equals(install)) after.add(conflict(e.id, "a second, different InstallSegment while pending"));
            } else {
              e.install = install;
              e.recordedInstall = install;
              RegionTable.Failure f = prevalidate(install);
              if (f != null) fail(e, f.failureClass(), f.detail(), after);
              else if (e.cap != null && e.cap.mapping != null) complete(e, after);
            }
          }
        }
      }
    }
    after.forEach(Runnable::run);
  }

  /** The checks that need no mapping, so an {@code InstallSegment} ahead of its {@code M} fails at once. */
  private RegionTable.@Nullable Failure prevalidate(InstallSegment install) {
    RegionTable.Result r = RegionTable.validate(install, Long.MAX_VALUE, 0xFF, knownSubject);
    return r.failure();
  }

  private void mapCapability(Entry e, List<Runnable> after) {
    Capability cap = e.cap;
    if (cap == null) return;
    MemoryGrant g = cap.grant;
    boolean writable = g.pluginWritable();
    try {
      Mapping m = Mapping.windows()
          ? Mapping.Windows.map(g.name(), g.size(), writable)
          : Mapping.Unix.map(cap.fd, g.size(), writable);
      cap.mapping = m;
    } catch (Mapping.MappingException ex) {
      cap.fd = -1; // Unix.map closed it
      fail(e, ex.failure.failureClass(), ex.failure.detail(), after);
      return;
    }
    String bad = Preamble.check(cap.mapping.segment(), e.id);
    if (bad != null) {
      fail(e, GrantFailureClass.GRANT_FAILURE_MALFORMED, bad, after);
      return;
    }
    if (!Preamble.paddingZero(cap.mapping.segment())) {
      after.add(() -> sink.diagnostic("segment " + Long.toUnsignedString(e.id) + ": preamble padding [8..12) is not zero"));
    }
    if (e.install != null) complete(e, after);
  }

  /** Both halves are in and the mapping is good: validate the table, install, acknowledge. */
  private void complete(Entry e, List<Runnable> after) {
    Capability cap = e.cap;
    InstallSegment install = e.install;
    if (cap == null || install == null || cap.mapping == null) return;
    RegionTable.Result r = RegionTable.validate(install, cap.grant.size(), cap.grant.perms(), knownSubject);
    if (r.failure() != null) {
      fail(e, r.failure().failureClass(), r.failure().detail(), after);
      return;
    }
    Mapping m = cap.mapping;
    byte[] proof = Preamble.proof(m.segment());
    Segment seg = new Segment(e.id, install.getPurpose(), install.getSubject(), m, r.regions(), proof,
        cap.grant.perms(), (site, detail) -> sink.violation(site, detail, new byte[0]));
    e.segment = seg;
    e.kind = Kind.INSTALLED;
    e.cap = null;
    e.install = null;
    PluginMsg ack = PluginMsg.newBuilder()
        .setGrantAck(GrantAck.newBuilder().setSegmentId(e.id)
            .setInstalled(GrantInstalled.newBuilder().setIdentityProof(ByteString.copyFrom(proof))))
        .build();
    after.add(() -> sink.push(ack));
    long superseded = install.getSupersedes();
    if (superseded != 0) retireLocked(superseded, after);
    settle(e, new Settlement.Installed(seg), after);
    after.add(() -> onInstalled.accept(seg));
  }

  /** Enters FAILED: releases everything acquired, then reports. */
  private void fail(Entry e, GrantFailureClass c, String detail, List<Runnable> after) {
    if (e.cap != null) e.cap.discard();
    e.cap = null;
    e.install = null;
    e.kind = Kind.FAILED;
    e.failClass = c;
    e.failDetail = detail;
    PluginMsg ack = failedAck(e.id, c, detail);
    after.add(() -> sink.push(ack));
    settle(e, new Settlement.Failed(c, detail), after);
  }

  private static PluginMsg failedAck(long id, GrantFailureClass c, String detail) {
    return PluginMsg.newBuilder()
        .setGrantAck(GrantAck.newBuilder().setSegmentId(id)
            .setFailed(GrantFailed.newBuilder().setFailureClass(c).setDetail(detail)))
        .build();
  }

  private Runnable replay(Entry e) {
    PluginMsg ack = e.kind == Kind.INSTALLED && e.segment != null
        ? PluginMsg.newBuilder()
            .setGrantAck(GrantAck.newBuilder().setSegmentId(e.id)
                .setInstalled(GrantInstalled.newBuilder().setIdentityProof(ByteString.copyFrom(e.segment.proof))))
            .build()
        : failedAck(e.id, e.failClass, e.failDetail);
    return () -> sink.push(ack);
  }

  /** A conflicting grant: a failure in its own right, not replacing the recorded state. */
  private Runnable conflict(long id, String detail) {
    PluginMsg ack = failedAck(id, GrantFailureClass.GRANT_FAILURE_MALFORMED, "conflicting grant: " + detail);
    return () -> sink.push(ack);
  }

  private void settle(Entry e, Settlement s, List<Runnable> after) {
    List<Consumer<Settlement>> ws = List.copyOf(e.waiters);
    e.waiters.clear();
    for (Consumer<Settlement> w : ws) after.add(() -> w.accept(s));
  }

  private void pushReleased(long id) {
    sink.push(PluginMsg.newBuilder().setSegmentReleased(SegmentReleased.newBuilder().setSegmentId(id)).build());
  }

  /**
   * Calls {@code then} once the segment has settled: at once when it already has, else when its
   * grant installs, fails or is released. A message naming a segment not yet installed is held
   * this way and dispatched in arrival order (control_plane_protocol.md §8.12).
   */
  public void whenSettled(long segmentId, Consumer<Settlement> then) {
    Settlement now = null;
    synchronized (lock) {
      Entry e = entries.computeIfAbsent(segmentId, Entry::new);
      switch (e.kind) {
        case PENDING -> e.waiters.add(then);
        case INSTALLED -> now = e.segment != null && e.segment.state != Segment.State.RELEASED
            ? new Settlement.Installed(e.segment)
            : new Settlement.Released();
        case FAILED -> now = new Settlement.Failed(e.failClass, e.failDetail);
        case RELEASED -> now = new Settlement.Released();
      }
    }
    if (now != null) then.accept(now);
  }

  // ---- retirement (§9) ----

  public void onRetire(long segmentId) {
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      retireLocked(segmentId, after);
    }
    after.forEach(Runnable::run);
  }

  private void retireLocked(long id, List<Runnable> after) {
    Entry e = entries.computeIfAbsent(id, Entry::new);
    switch (e.kind) {
      case PENDING -> {
        if (e.cap != null) e.cap.discard();
        e.cap = null;
        e.install = null;
        e.kind = Kind.RELEASED;
        after.add(() -> pushReleased(id));
        settle(e, new Settlement.Released(), after);
      }
      case FAILED -> {
        e.kind = Kind.RELEASED;
        after.add(() -> pushReleased(id));
      }
      case RELEASED -> after.add(() -> pushReleased(id));
      case INSTALLED -> {
        Segment s = e.segment;
        if (s != null && s.state == Segment.State.LIVE) {
          s.state = Segment.State.RETIRING;
          maybeRelease(e, after);
        }
      }
    }
  }

  private void maybeRelease(Entry e, List<Runnable> after) {
    Segment s = e.segment;
    if (s == null || s.state != Segment.State.RETIRING || s.inflight > 0) return;
    if (s.pluginLane != null && s.pluginLane.holdsClaim()) return;
    String err = s.mapping.release();
    s.state = Segment.State.RELEASED;
    // each loan still out may be released by one ReleaseLoan crossing our SegmentReleased (§9.3)
    if (s.arena != null) {
      Set<LoanKey> out = new HashSet<>();
      for (LoanArena.Loan l : s.arena.outstandingLoans()) out.add(new LoanKey(l.region(), l.offset(), l.len()));
      if (!out.isEmpty()) loansOutAtRelease.put(e.id, out);
    }
    e.kind = Kind.RELEASED;
    deposits.keySet().removeIf(k -> k.segment() == e.id);
    if (err != null) after.add(() -> sink.diagnostic("releasing segment " + Long.toUnsignedString(e.id) + ": " + err));
    after.add(() -> pushReleased(e.id));
  }

  /**
   * Asks the host to retire a segment this plugin has acknowledged (§9.6): a hint, once. Nothing
   * about the segment changes until {@code RetireSegment} arrives.
   */
  public boolean requestRelease(long segmentId, String reason) {
    synchronized (lock) {
      Entry e = entries.get(segmentId);
      if (e == null || e.kind != Kind.INSTALLED) return false;
    }
    sink.push(PluginMsg.newBuilder()
        .setReleaseRequest(ReleaseRequest.newBuilder().setSegmentId(segmentId).setReason(reason)).build());
    return true;
  }

  // ---- queries ----

  /** The answer to {@code GrantStateQuery}; the caller echoes the query's id. */
  public GrantState query(long segmentId) {
    synchronized (lock) {
      GrantState.Builder b = GrantState.newBuilder().setSegmentId(segmentId);
      Entry e = entries.get(segmentId);
      // an entry with neither half is only a waiter's placeholder: nothing has arrived
      if (e == null || (e.kind == Kind.PENDING && e.cap == null && e.install == null)) {
        return b.setState(GrantStateKind.GRANT_STATE_NOT_PRESENT).build();
      }
      return switch (e.kind) {
        case PENDING -> b.setState(GrantStateKind.GRANT_STATE_PENDING)
            .setHaveCapability(e.cap != null)
            .setHaveInstall(e.install != null)
            .build();
        case INSTALLED -> e.segment != null && e.segment.state != Segment.State.RELEASED
            ? b.setState(GrantStateKind.GRANT_STATE_INSTALLED).setIdentityProof(ByteString.copyFrom(e.segment.proof)).build()
            : b.setState(GrantStateKind.GRANT_STATE_RELEASED).build();
        case FAILED -> b.setState(GrantStateKind.GRANT_STATE_FAILED).setFailureClass(e.failClass).setDetail(e.failDetail).build();
        case RELEASED -> b.setState(GrantStateKind.GRANT_STATE_RELEASED).build();
      };
    }
  }

  /** The installed (live or retiring) segment with this id. */
  public Optional<Segment> installed(long segmentId) {
    synchronized (lock) {
      Entry e = entries.get(segmentId);
      if (e == null || e.segment == null || e.segment.state == Segment.State.RELEASED) return Optional.empty();
      return Optional.of(e.segment);
    }
  }

  /**
   * The one live segment of a purpose and subject. Two live ones (a grant installed beside
   * another without {@code supersedes}) refuse the lookup and name both (reader_writer_contract
   * .md §1.2, I2).
   */
  public Segment live(String purpose, String subject) throws UnavailableException {
    synchronized (lock) {
      List<Segment> found = new ArrayList<>();
      for (Entry e : entries.values()) {
        Segment s = e.segment;
        if (s != null && s.state == Segment.State.LIVE && s.purpose.equals(purpose) && s.subject.equals(subject)) {
          found.add(s);
        }
      }
      if (found.isEmpty()) {
        throw new UnavailableException("no " + purpose + " segment" + (subject.isEmpty() ? "" : " for \"" + subject + "\"")
            + " is installed");
      }
      if (found.size() > 1) {
        found.sort((a, b) -> Long.compareUnsigned(a.id, b.id));
        throw new AmbiguousException("two live " + purpose + " segments" + (subject.isEmpty() ? "" : " for \"" + subject + "\"")
            + ": " + Long.toUnsignedString(found.get(0).id) + " and " + Long.toUnsignedString(found.get(1).id)
            + " (the host granted a second without superseding the first)");
      }
      return found.get(0);
    }
  }

  /** Every installed segment (live or retiring), for the re-check schedule and tests. */
  public List<Segment> installedSegments() {
    synchronized (lock) {
      List<Segment> out = new ArrayList<>();
      for (Entry e : entries.values()) {
        if (e.segment != null && e.segment.state != Segment.State.RELEASED) out.add(e.segment);
      }
      return out;
    }
  }

  // ---- operations: enforced here, at the operation (§9.1) ----

  /** An operation on a segment's mapping; while one is open the segment is not released. */
  public final class Op implements AutoCloseable {
    final Segment segment;
    private boolean closed;

    Op(Segment segment) {
      this.segment = segment;
    }

    public MemorySegment memory() {
      return segment.memory();
    }

    @Override
    public void close() {
      List<Runnable> after = new ArrayList<>();
      synchronized (lock) {
        if (closed) return;
        closed = true;
        segment.inflight--;
        Entry e = entries.get(segment.id);
        if (e != null) maybeRelease(e, after);
      }
      after.forEach(Runnable::run);
    }
  }

  /**
   * Begins an operation. {@code starting} operations (a new claim, publish, reservation or turn)
   * are refused while retiring; finishing ones (a read already begun, a release) are not.
   */
  public Op begin(Segment s, boolean starting) throws UnavailableException {
    synchronized (lock) {
      if (s.state == Segment.State.RELEASED) {
        throw starting ? new RetiredException(s + " has been released") : new UnavailableException(s + " has been released");
      }
      if (starting && s.state == Segment.State.RETIRING) {
        throw new RetiredException(s + " is retiring: finishing is permitted, starting is not");
      }
      s.inflight++;
      return new Op(s);
    }
  }

  // ---- loans: plugin-produced LOANED ----

  /**
   * Lends {@code data} from the segment's arena: reserves, copies in, and returns the loan the
   * descriptor names. Never blocks and never reuses; a full arena throws.
   */
  public LoanArena.Loan lend(Segment s, byte[] data, int uses)
      throws UnavailableException, LoanArena.ArenaFullException {
    return lend(s, MemorySegment.ofArray(data), uses);
  }

  /**
   * As {@link #lend(Segment, byte[], int)}, copying from a segment the caller owns. The reservation
   * and the copy are one {@link Op}: a retirement arriving meanwhile waits for the copy (§9.1), and
   * the unmap comes after it. A lend that would START on a retiring segment goes to the segment
   * that supersedes it, if one is live; otherwise {@link RetiredException}, counted.
   */
  public LoanArena.Loan lend(Segment s, MemorySegment data, int uses)
      throws UnavailableException, LoanArena.ArenaFullException {
    LoanArena.Loan loan = lendClaimedOrSuccessor(s, data, uses);
    endClaim(loan);
    return loan;
  }

  /**
   * As {@link #lend(Segment, MemorySegment, int)}, but the loan's CLAIM stays open: the segment
   * counts it in flight, and a retirement does not release (unmap) the segment until {@link
   * #endClaim} (§9.1). The claim spans the reservation, the copy, and the send of every
   * message naming it: the caller ends it once that message has gone to the channel, or when the
   * loan is handed back unsent.
   */
  public LoanArena.Loan lendClaimed(Segment s, MemorySegment data, int uses)
      throws UnavailableException, LoanArena.ArenaFullException {
    Op op = begin(s, true); // counted BEFORE anything reads the reservation: the claim
    boolean keep = false;
    try {
      LoanArena a = arenaOf(s);
      LoanArena.Loan loan = a.reserve(data.byteSize(), uses);
      Runnable parked = afterReserveForTest;
      if (parked != null) parked.run();
      a.write(loan, data);
      keep = true;
      return loan; // the Op stays counted: endClaim ends it
    } finally {
      if (!keep) op.close(); // refused or failed: nothing is claimed
    }
  }

  /** Ends the claim a {@link #lendClaimed} loan holds; a retiring segment may then release. */
  public void endClaim(LoanArena.Loan loan) {
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      Entry e = entries.get(loan.segmentId());
      if (e == null || e.segment == null) return;
      e.segment.inflight--;
      maybeRelease(e, after); // settled here, when the last claim ends, never at a later message
    }
    after.forEach(Runnable::run);
  }

  /**
   * {@link #lendClaimed}, and a start refused on a retiring segment goes to the segment that
   * supersedes it, if one is live; otherwise {@link RetiredException}, counted in {@link
   * #retiredDrops()} (a normal retirement is not an error).
   */
  public LoanArena.Loan lendClaimedOrSuccessor(Segment s, MemorySegment data, int uses)
      throws UnavailableException, LoanArena.ArenaFullException {
    Segment target = s;
    for (int attempt = 0; ; attempt++) {
      try {
        return lendClaimed(target, data, uses);
      } catch (RetiredException retired) {
        Segment next = attempt == 0 ? successor(target) : null;
        if (next == null) {
          synchronized (lock) {
            retiredDrops++;
          }
          throw retired;
        }
        target = next;
      }
    }
  }

  /** The live segment of {@code s}'s purpose and subject, other than {@code s}; null if none. */
  private @Nullable Segment successor(Segment s) {
    try {
      Segment n = live(s.purpose, s.subject);
      return n == s ? null : n;
    } catch (UnavailableException none) {
      return null;
    }
  }

  private long retiredDrops;

  /** Starts refused on a normal retirement with no successor: dropped quietly, counted here. */
  public long retiredDrops() {
    synchronized (lock) {
      return retiredDrops;
    }
  }

  /** Counts a start another layer dropped on a retirement (a stage turn on a retiring {@code S}). */
  public void countRetiredDrop() {
    synchronized (lock) {
      retiredDrops++;
    }
  }

  /** Tests only: runs inside a lend's {@link Op}, between the reservation and the copy. */
  public static volatile @Nullable Runnable afterReserveForTest;

  private LoanArena arenaOf(Segment s) throws UnavailableException {
    LoanArena a = s.arena;
    if (a == null) throw new UnavailableException(s + " has no plugin-produced LOANED region");
    synchronized (lock) {
      if (s.arenaQuarantined) throw new UnavailableException(s + "'s arena is isolated after a release naming no reservation");
    }
    return a;
  }

  /**
   * Hands back a loan whose message was never sent (superseded on the plugin's own outbound
   * queue, or refused before admission): the host never saw it, so it will never release it.
   */
  public void reclaimUnsent(LoanArena.Loan loan) {
    Segment s = installed(loan.segmentId()).orElse(null);
    if (s == null || s.arena == null) return;
    if (s.arena.release(loan.offset(), loan.len()) != LoanArena.Released.OK) {
      sink.diagnostic("reclaiming an unsent loan (" + loan.offset() + ", " + loan.len() + ") in " + s + " found nothing");
    }
  }

  private long lateReleaseLoans;

  private record LoanKey(int region, long offset, long len) {}

  /**
   * Per released segment: the loans out at its release, the only {@code ReleaseLoan}s that may
   * cross it, each once (§9.3). A crossing that names none of them is {@code
   * unknown_release}.
   */
  private final Map<Long, Set<LoanKey>> loansOutAtRelease = new HashMap<>();

  /**
   * {@code ReleaseLoan}s for a segment this session had already released: the host's return of a
   * push it dropped into a retiring segment can arrive after our {@code SegmentReleased}.
   * Ignored quietly, and counted here.
   */
  public long lateReleaseLoans() {
    synchronized (lock) {
      return lateReleaseLoans;
    }
  }

  /** A host {@code ReleaseLoan}: applied always, retiring included. */
  public void onReleaseLoan(ReleaseLoan rl) {
    byte[] none = new byte[0];
    Segment s;
    boolean unknown;
    boolean strayCrossing = false;
    synchronized (lock) {
      Entry e = entries.get(rl.getSegmentId());
      s = e == null ? null : e.segment;
      unknown = s == null || s.state == Segment.State.RELEASED;
      if (e != null && e.kind == Kind.RELEASED) {
        // released here already: ids are never reused, so a crossing naming a loan that was out at
        // the release is late, not unknown -- once per loan (§9.3)
        Set<LoanKey> out = loansOutAtRelease.get(e.id);
        if (out != null && out.remove(new LoanKey(rl.getRegion(), rl.getOffset(), rl.getLen()))) {
          if (out.isEmpty()) loansOutAtRelease.remove(e.id);
          lateReleaseLoans++;
          sink.trace("ReleaseLoan for segment " + Long.toUnsignedString(rl.getSegmentId())
              + ", released here already (crossed our SegmentReleased; " + lateReleaseLoans + " so far)");
          return;
        }
        strayCrossing = true;
      }
    }
    if (strayCrossing) {
      // a loan never out at the release, or one already returned: the host's bug, which a crossing
      // must not hide
      sink.violation(ViolationSite.VIOLATION_SITE_UNKNOWN_RELEASE, "ReleaseLoan (" + rl.getOffset() + ", " + rl.getLen()
          + ") in region " + Integer.toUnsignedString(rl.getRegion()) + " of released segment "
          + Long.toUnsignedString(rl.getSegmentId()) + ": no such loan was out at the release", none);
      return;
    }
    // reported outside the lock: a report reaches the outbound queue, whose lock is held while a
    // superseded push's loans come back here
    if (unknown || s == null) {
      sink.violation(ViolationSite.VIOLATION_SITE_UNKNOWN_RELEASE,
          "ReleaseLoan for segment " + Long.toUnsignedString(rl.getSegmentId()) + ", which is not installed", none);
      return;
    }
    int region = rl.getRegion();
    if (Integer.toUnsignedLong(region) >= s.regions.size()) {
      sink.violation(ViolationSite.VIOLATION_SITE_UNKNOWN_RELEASE,
          "ReleaseLoan names region " + Integer.toUnsignedString(region) + " of " + s + ", which has " + s.regions.size(), none);
      return;
    }
    RegionTable.Spec spec = s.regions.get(region);
    if (spec.discipline() != RegionDiscipline.REGION_DISCIPLINE_LOANED || !spec.pluginProduced() || s.arena == null) {
      sink.violation(ViolationSite.VIOLATION_SITE_WRONG_PRODUCER_RELEASE,
          "ReleaseLoan names region " + region + " of " + s + ", which is not plugin-produced LOANED", none);
      return;
    }
    switch (s.arena.release(rl.getOffset(), rl.getLen())) {
      case OK -> {}
      case REGION_MISMATCH -> sink.violation(ViolationSite.VIOLATION_SITE_RELEASE_REGION_MISMATCH,
          "ReleaseLoan offset " + rl.getOffset() + " is outside region " + region + " of " + s, none);
      case UNKNOWN -> {
        synchronized (lock) {
          s.arenaQuarantined = true;
        }
        sink.violation(ViolationSite.VIOLATION_SITE_UNKNOWN_RELEASE,
            "ReleaseLoan (" + rl.getOffset() + ", " + rl.getLen() + ") in " + s + " names no outstanding reservation; the arena is isolated",
            none);
      }
    }
  }

  // ---- deposits: host-produced LOANED ----

  /**
   * Copies a host deposit out and disposes of this naming of it. The segment must be installed;
   * a caller holding a message for a pending segment uses {@link #whenSettled} first.
   */
  public byte[] readDeposit(SegmentRef ref) throws DepositException {
    Segment s = installed(ref.getSegmentId()).orElseThrow(
        () -> new DepositException("segment " + Long.toUnsignedString(ref.getSegmentId()) + " is not installed"));
    RegionTable.Spec spec = s.hostDepositRegion(ref.getOffset(), ref.getLen());
    if (spec == null) {
      throw new DepositException("the deposit (" + ref.getOffset() + ", " + ref.getLen() + ") lies in no host-produced LOANED region of " + s);
    }
    byte[] out;
    try (Op op = begin(s, false)) {
      if (ref.getLen() > Integer.MAX_VALUE - 8) throw new DepositException("a deposit of " + ref.getLen() + " bytes is too large to copy into one array");
      out = op.memory().asSlice(ref.getOffset(), ref.getLen()).toArray(ValueLayout.JAVA_BYTE);
    } catch (UnavailableException e) {
      throw new DepositException(e.getMessage() == null ? "unavailable" : e.getMessage());
    }
    dispose(s, spec, ref);
    return out;
  }

  /**
   * Copies a host deposit into {@code dst} (which the caller owns) and disposes of this naming.
   */
  public void readDeposit(SegmentRef ref, MemorySegment dst) throws DepositException {
    Segment s = installed(ref.getSegmentId()).orElseThrow(
        () -> new DepositException("segment " + Long.toUnsignedString(ref.getSegmentId()) + " is not installed"));
    RegionTable.Spec spec = s.hostDepositRegion(ref.getOffset(), ref.getLen());
    if (spec == null) {
      throw new DepositException("the deposit (" + ref.getOffset() + ", " + ref.getLen() + ") lies in no host-produced LOANED region of " + s);
    }
    if (dst.byteSize() < ref.getLen()) throw new DepositException("destination too small");
    try (Op op = begin(s, false)) {
      MemorySegment.copy(op.memory(), ref.getOffset(), dst, 0, ref.getLen());
    } catch (UnavailableException e) {
      throw new DepositException(e.getMessage() == null ? "unavailable" : e.getMessage());
    }
    dispose(s, spec, ref);
  }

  /**
   * Disposes of one message's naming of a host deposit without reading it: a refused, abandoned
   * or unmatched message still releases what it names, exactly once per message.
   */
  public void disposeDeposit(SegmentRef ref) {
    Optional<Segment> s = installed(ref.getSegmentId());
    if (s.isEmpty()) {
      sink.diagnostic("a deposit in segment " + Long.toUnsignedString(ref.getSegmentId()) + ", which is not installed, cannot be released");
      return;
    }
    RegionTable.Spec spec = s.get().hostDepositRegion(ref.getOffset(), ref.getLen());
    if (spec == null) {
      sink.diagnostic("a deposit (" + ref.getOffset() + ", " + ref.getLen() + ") outside every host-produced region of " + s.get() + " cannot be released");
      return;
    }
    dispose(s.get(), spec, ref);
  }

  /**
   * The {@code uses} ledger (control_plane_protocol.md §5.6): the first declaration stands, a
   * later disagreement is reported, and the release goes when every declared naming is disposed.
   */
  /** Deposit claims: messages whose descriptor has arrived and whose naming is not yet disposed. */
  private final Map<DepositKey, Integer> depositClaims = new HashMap<>();

  /**
   * A message naming this deposit has ARRIVED (call it on the dispatch thread, in order with
   * {@code RetireSegment}): it counts as begun, and a retirement waits for it (§9.1). The
   * claim ends when this message's naming is disposed of, read or not. A segment already released,
   * or not installed, takes no claim, and nor does a ref outside every host-produced region: no
   * disposal would ever end that one, and the segment could never release.
   */
  public void claimDeposit(SegmentRef ref) {
    synchronized (lock) {
      Entry e = entries.get(ref.getSegmentId());
      if (e == null || e.segment == null || e.segment.state == Segment.State.RELEASED) return;
      if (e.segment.hostDepositRegion(ref.getOffset(), ref.getLen()) == null) return;
      e.segment.inflight++;
      depositClaims.merge(new DepositKey(e.id, ref.getOffset(), ref.getLen()), 1, Integer::sum);
    }
  }

  private void dispose(Segment s, RegionTable.Spec spec, SegmentRef ref) {
    int declared = Math.max(1, ref.getUses());
    boolean release;
    String disagreement = null;
    List<Runnable> after = new ArrayList<>();
    synchronized (lock) {
      DepositKey k = new DepositKey(s.id, ref.getOffset(), ref.getLen());
      Integer claims = depositClaims.get(k);
      if (claims != null) { // this message's claim ends with its naming
        if (claims <= 1) depositClaims.remove(k);
        else depositClaims.put(k, claims - 1);
        s.inflight--;
        Entry e = entries.get(s.id);
        if (e != null) maybeRelease(e, after);
      }
      int[] entry = deposits.get(k);
      if (entry == null) {
        entry = new int[] {declared, declared};
        deposits.put(k, entry);
      } else if (entry[0] != declared) {
        disagreement = "a deposit declared with uses " + entry[0] + " is named again with uses " + declared;
      }
      entry[1]--;
      release = entry[1] <= 0;
      if (release) deposits.remove(k);
    }
    if (disagreement != null) sink.violation(ViolationSite.VIOLATION_SITE_USES_DISAGREEMENT, disagreement, new byte[0]);
    if (release) {
      sink.push(PluginMsg.newBuilder().setReleaseDeposit(ReleaseDeposit.newBuilder()
          .setSegmentId(s.id).setRegion(spec.index()).setOffset(ref.getOffset()).setLen(ref.getLen())).build());
    }
    after.forEach(Runnable::run); // a deferred SegmentReleased, after the deposit's own release
  }

  // ---- hints (doorbell_D_notification.md §3) ----

  /**
   * One {@code D} record, disposed of per region by the region's discipline. {@code
   * installedEpoch} answers the epoch this plugin acknowledged for a pane (0 for none).
   */
  public void onHint(Notification hint, byte[] raw, IntToLongFunction installedEpoch) {
    Segment s = installed(hint.segmentId()).orElse(null);
    if (s == null) {
      countIgnored();
      return;
    }
    List<Integer> bring = new ArrayList<>();
    List<Integer> owned = new ArrayList<>();
    boolean sweep = false;
    synchronized (lock) {
      if (s.state != Segment.State.LIVE) {
        hintsIgnored++;
        return;
      }
      if (hint.paneAssociated()) {
        long installed = installedEpoch.applyAsLong(hint.viewId());
        if (installed == 0 || installed != hint.epoch()) {
          hintsIgnored++;
          return;
        }
      }
      for (int r : hint.regions()) {
        if (r >= s.regions.size()) {
          sweep = true;
          continue;
        }
        RegionTable.Spec spec = s.regions.get(r);
        switch (spec.discipline()) {
          case REGION_DISCIPLINE_PULSED -> {
            if (spec.producer() == RegionProducer.REGION_PRODUCER_HOST) bring.add(r);
          }
          case REGION_DISCIPLINE_STAGED -> bring.add(r);
          case REGION_DISCIPLINE_OWNED -> owned.add(r);
          default -> {}
        }
      }
      if (sweep) {
        for (RegionTable.Spec spec : s.regions) {
          if (spec.discipline() == RegionDiscipline.REGION_DISCIPLINE_PULSED
              && spec.producer() == RegionProducer.REGION_PRODUCER_HOST
              && !bring.contains(spec.index())) {
            bring.add(spec.index());
          }
        }
      }
    }
    if (!owned.isEmpty()) {
      sink.violation(ViolationSite.VIOLATION_SITE_OWNED_REGION_HINT,
          "D names OWNED region(s) " + owned + " of " + s + ", whose read is the handoff", raw);
    }
    Hints h = hints;
    for (int r : bring) h.bringForward(s.id, r);
  }

  private void countIgnored() {
    synchronized (lock) {
      hintsIgnored++;
    }
  }

  public long hintsIgnored() {
    synchronized (lock) {
      return hintsIgnored;
    }
  }

  /** At process exit: unmap and close everything. The plugin owes the host nothing here (§9.4). */
  public void closeAll() {
    synchronized (lock) {
      for (Entry e : entries.values()) {
        if (e.cap != null) e.cap.discard();
        if (e.segment != null && e.segment.state != Segment.State.RELEASED) {
          e.segment.mapping.release();
          e.segment.state = Segment.State.RELEASED;
        }
      }
    }
  }
}
