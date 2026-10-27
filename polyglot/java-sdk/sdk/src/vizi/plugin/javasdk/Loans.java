package vizi.plugin.javasdk;

import com.google.protobuf.ByteString;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.util.ArrayList;
import java.util.List;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Outbound;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.memory.LoanArena;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.GeomBuffer;
import vizi.plugin.v1.ViziPlugin.SegmentRef;

/**
 * The loans one push takes, held under a guard until the push is admitted: if anything fails
 * part-way, every loan taken so far goes back. No loan ever reaches an author.
 */
final class Loans implements AutoCloseable {

  private final Session session;
  private final List<LoanArena.Loan> taken = new ArrayList<>();
  /**
   * Loans whose claim is still open: each holds its segment's release back (§9.1) from
   * the reservation until the message naming it is sent, or the loan comes back unsent.
   */
  private final java.util.Set<LoanArena.Loan> claimed = java.util.Collections.newSetFromMap(new java.util.IdentityHashMap<>());
  private boolean admitted;
  private boolean multiUse;

  Loans(Session session) {
    this.session = session;
  }

  /** Lends {@code bytes} from {@code seg}'s arena; a full arena sends a shortfall and throws. */
  SegmentRef lend(Segment seg, MemorySegment bytes, int uses) {
    try {
      LoanArena.Loan loan = session.store().lendClaimedOrSuccessor(seg, bytes, uses);
      taken.add(loan);
      claim(loan);
      if (uses > 1) multiUse = true;
      SegmentRef.Builder r = SegmentRef.newBuilder().setSegmentId(loan.segmentId()).setOffset(loan.offset()).setLen(loan.len());
      if (uses > 1) r.setUses(uses);
      return r.build();
    } catch (LoanArena.ArenaFullException full) {
      session.shortfall(full);
      throw new ArenaFullException(full.getMessage() == null ? "arena full" : full.getMessage(), full.wanted, full.largestFree);
    } catch (SegmentStore.RetiredException e) {
      throw new Retired(e);
    } catch (SegmentStore.UnavailableException e) {
      throw new IllegalStateException(e.getMessage(), e);
    }
  }

  /**
   * A push's arena was retired with nothing superseding it (§9): the push is dropped quietly, as a
   * normal retirement is not an error. Caught by each push; never reaches an author.
   */
  static final class Retired extends RuntimeException {
    Retired(SegmentStore.RetiredException cause) {
      super(cause.getMessage(), cause, false, false);
    }
  }

  /** A geometry buffer: inline, or a loan from {@code seg} (which must then be non-null). */
  GeomBuffer geom(MemorySegment bytes, List<Integer> shape, DType dtype, boolean inline, @Nullable Segment seg, int uses) {
    GeomBuffer.Builder b = GeomBuffer.newBuilder().addAllShape(shape).setDtype(dtype.wire());
    if (inline) {
      b.setInline(ByteString.copyFrom(bytes.toArray(ValueLayout.JAVA_BYTE)));
    } else {
      if (seg == null) throw new IllegalStateException("a loaned buffer needs the overlay's arena");
      b.setSegment(lend(seg, bytes, uses).toBuilder().setDtype(dtype.wire()));
    }
    return b.build();
  }

  /**
   * A loaned geometry buffer, or null when the arena cannot hold it now. No shortfall is
   * reported: for a solicited chunk the arena is not short, the host is about to release the
   * chunk before it (control_plane_protocol.md §5.7).
   */
  @Nullable GeomBuffer tryGeom(MemorySegment bytes, List<Integer> shape, DType dtype, Segment seg) {
    LoanArena.Loan loan;
    try {
      loan = session.store().lendClaimedOrSuccessor(seg, bytes, 1);
      claim(loan);
    } catch (LoanArena.ArenaFullException full) {
      return null;
    } catch (SegmentStore.RetiredException e) {
      throw new Retired(e);
    } catch (SegmentStore.UnavailableException e) {
      throw new IllegalStateException(e.getMessage(), e);
    }
    taken.add(loan);
    SegmentRef ref = SegmentRef.newBuilder().setSegmentId(loan.segmentId()).setOffset(loan.offset()).setLen(loan.len())
        .setDtype(dtype.wire()).build();
    return GeomBuffer.newBuilder().addAllShape(shape).setDtype(dtype.wire()).setSegment(ref).build();
  }

  /** The reclaimers for the outbound queue: each hands a loan back if the push is superseded unsent. */
  Outbound.Loans forQueue() {
    List<Runnable> r = new ArrayList<>();
    List<Runnable> sent = new ArrayList<>();
    for (LoanArena.Loan l : taken) {
      r.add(() -> {
        session.store().reclaimUnsent(l);
        endClaim(l);
      });
      sent.add(() -> endClaim(l));
    }
    return new Outbound.Loans(List.copyOf(r), List.copyOf(sent), multiUse);
  }

  private synchronized void claim(LoanArena.Loan l) {
    claimed.add(l);
  }

  /** Ends one loan's claim, once; the segment may release if it is retiring and this was the last. */
  private void endClaim(LoanArena.Loan l) {
    boolean mine;
    synchronized (this) {
      mine = claimed.remove(l);
    }
    if (mine) session.store().endClaim(l);
  }

  /** A request naming these loans has been sent (its reply came, or it timed out): the claims end. */
  void sent() {
    List<LoanArena.Loan> all;
    synchronized (this) {
      all = List.copyOf(claimed);
    }
    for (LoanArena.Loan l : all) endClaim(l);
  }

  boolean any() {
    return !taken.isEmpty();
  }

  /** The push was admitted: its loans are now the host's to release. */
  void admitted() {
    admitted = true;
  }

  @Override
  public void close() {
    if (admitted) return; // the queue ends the claims: when the message is sent, or its loans come back
    for (LoanArena.Loan l : taken) {
      session.store().reclaimUnsent(l);
      endClaim(l);
    }
    taken.clear();
  }
}
