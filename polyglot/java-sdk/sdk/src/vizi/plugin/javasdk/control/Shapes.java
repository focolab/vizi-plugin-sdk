package vizi.plugin.javasdk.control;

import java.util.ArrayDeque;
import java.util.HashSet;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.Map;
import java.util.Set;
import java.util.function.BooleanSupplier;
import org.jspecify.annotations.Nullable;
import pois_math.v1.RoiOuterClass.Resolution;
import pois_math.v1.RoiOuterClass.Vec3;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ShapeEdit;
import vizi.plugin.v1.ViziPlugin.ShapeEditEnded;
import vizi.plugin.v1.ViziPlugin.ShapeResult;

/**
 * Region types a plugin shapes (control_plane_protocol.md §8.8): the plugin half of the
 * exchange, and what the spec says an SDK owes, whatever the author registers.
 *
 * <ul>
 *   <li><b>Every {@code shape_commit} is answered</b>, {@code REJECTED} with a message when no
 *       shaper is registered for the type (quietly for a type the manifest declares with no
 *       shaper; once, with a note, for any other), when the shaper throws, or when what it returned is not
 *       one finite position per vertex. The one exception is a commit whose drag has already
 *       ended: the host no longer waits on it, and an answer would be dropped.
 *   <li><b>Nothing is proposed for a drag once its commit or its end has arrived</b>: the drag is
 *       remembered as stopped, its pending input dropped, and a proposal still queued unsent is
 *       withdrawn from the outbound map, so the host's proposal keys stay bounded (§9's slack of 4).
 *   <li><b>A drag is coalesced</b>: input that arrives while the shaper runs replaces the pending
 *       input for its drag, so a shaper slower than the host's 40 ms tick answers the newest
 *       position next rather than every one in turn. An answer it has already computed is still
 *       sent: the host shows the newest proposal above the one on screen, so it is an improvement.
 *   <li><b>The silent drops</b> — a {@code shape_drag} after its drag's commit or end, a {@code
 *       ShapeEditEnded} for a drag never answered — are dropped without a word.
 * </ul>
 *
 * <p>One worker ({@value #THREAD}) runs every shaper and the ended hook, so an author's shapers
 * never run concurrently with one another; it is not the command worker, so a commit never waits
 * behind a command. One-shots (commits, ends) go in arrival order, ahead of any pending drag.
 */
public final class Shapes {

  public static final String THREAD = "vizi-shape";

  /** How many stopped drags are remembered, so their late input is dropped. */
  static final int REMEMBERED = 256;

  /** What the control layer needs from the application. */
  public interface Shaper {
    /** Whether the plugin computes the shape of {@code typeName}. */
    boolean shapes(String typeName);

    /** Whether the manifest declares {@code typeName} in {@code region_types}, shaper or not. */
    boolean declared(String typeName);

    /**
     * The shape for {@code edit}: its {@code resolution}, {@code positions} and {@code message};
     * control fills in {@code drag_id} and {@code seq}. {@code stale} turns true once the answer is
     * no longer wanted (the drag was committed or ended, or newer input arrived), for a shaper
     * that can stop early.
     */
    ShapeResult shape(ShapeEdit edit, boolean commit, BooleanSupplier stale) throws Exception;

    /** How a drag ended; {@code last} is the newest edit seen for it, or null if none was. */
    void ended(ShapeEditEnded ended, @Nullable ShapeEdit last);
  }

  /** A commit or an end, in arrival order. */
  private sealed interface OneShot {}

  private record Commit(ShapeEdit edit) implements OneShot {}

  private record Ended(ShapeEditEnded ended, @Nullable ShapeEdit last) implements OneShot {}

  private final Shaper shaper;
  private final Outbound out;
  private final Session.Diagnostics diag;

  private final Object lock = new Object();
  private final ArrayDeque<OneShot> oneShots = new ArrayDeque<>();
  /** The newest unanswered input per drag, oldest drag first. */
  private final LinkedHashMap<Long, ShapeEdit> drags = new LinkedHashMap<>();
  /** Drags whose commit or end has arrived. */
  private final Set<Long> stopped = bounded();
  /** Drags whose end has arrived. */
  private final Set<Long> ended = bounded();
  /** The newest edit per drag not yet ended, for the ended hook. */
  private final Map<Long, ShapeEdit> latest = new LinkedHashMap<>() {
    private static final long serialVersionUID = 1L;

    @Override
    protected boolean removeEldestEntry(Map.Entry<Long, ShapeEdit> e) {
      return size() > REMEMBERED;
    }
  };
  private final Set<String> warnedTypes = new HashSet<>();
  private @Nullable Thread worker;
  private boolean closed;
  private long superseded;

  Shapes(Shaper shaper, Outbound out, Session.Diagnostics diag) {
    this.shaper = shaper;
    this.out = out;
    this.diag = diag;
  }

  private static Set<Long> bounded() {
    return java.util.Collections.newSetFromMap(new LinkedHashMap<Long, Boolean>() {
      private static final long serialVersionUID = 1L;

      @Override
      protected boolean removeEldestEntry(Map.Entry<Long, Boolean> e) {
        return size() > REMEMBERED;
      }
    });
  }

  // ---- arrivals (dispatch thread; none blocks) ----

  /** {@code shape_drag}: coalesced per drag; after the drag's commit or end, dropped silently. */
  void onDrag(ShapeEdit e) {
    synchronized (lock) {
      if (closed || stopped.contains(e.getDragId())) return;
      ShapeEdit pending = drags.remove(e.getDragId());
      if (pending != null) {
        superseded++;
        if (Integer.compareUnsigned(pending.getSeq(), e.getSeq()) > 0) e = pending; // the newer input stays
      }
      drags.put(e.getDragId(), e);
      latest.put(e.getDragId(), e);
      wake();
    }
  }

  /** {@code shape_commit}: proposing for the drag stops here; the commit is queued to be answered. */
  void onCommit(ShapeEdit e) {
    synchronized (lock) {
      if (closed) return;
      stop(e.getDragId());
      if (ended.contains(e.getDragId())) return; // over before its commit arrived: nobody waits on it
      latest.put(e.getDragId(), e);
      oneShots.add(new Commit(e));
      wake();
    }
  }

  /** {@code shape_edit_ended}: the drag is over; an unanswered commit for it is not answered. */
  void onEnded(ShapeEditEnded m) {
    synchronized (lock) {
      if (closed) return;
      long id = m.getDragId();
      stop(id);
      ended.add(id);
      oneShots.removeIf(o -> o instanceof Commit c && c.edit().getDragId() == id);
      oneShots.add(new Ended(m, latest.remove(id)));
      wake();
    }
  }

  /** Under the lock: no input, and no queued proposal, for {@code dragId} from here on. */
  private void stop(long dragId) {
    stopped.add(dragId);
    if (drags.remove(dragId) != null) superseded++;
    out.dropShapeProposal(dragId);
  }

  private void wake() {
    if (worker == null) {
      Thread t = new Thread(this::run, THREAD);
      t.setDaemon(true);
      worker = t;
      t.start();
    }
    lock.notifyAll();
  }

  /** The session ended: nothing more is shaped or sent. */
  void close() {
    synchronized (lock) {
      closed = true;
      oneShots.clear();
      drags.clear();
      lock.notifyAll();
    }
  }

  /** Drag input replaced before its shaper ran, so far. */
  public long superseded() {
    synchronized (lock) {
      return superseded;
    }
  }

  // ---- the worker ----

  private void run() {
    while (true) {
      Object next;
      synchronized (lock) {
        while (!closed && oneShots.isEmpty() && drags.isEmpty()) {
          try {
            lock.wait();
          } catch (InterruptedException e) {
            return;
          }
        }
        if (closed) return;
        if (!oneShots.isEmpty()) {
          next = oneShots.poll();
        } else {
          Iterator<ShapeEdit> it = drags.values().iterator();
          next = it.next();
          it.remove();
        }
      }
      try {
        switch (next) {
          case Commit c -> commit(c.edit());
          case Ended en -> shaper.ended(en.ended(), en.last());
          case ShapeEdit d -> drag(d);
          default -> throw new IllegalStateException(String.valueOf(next));
        }
      } catch (RuntimeException | java.lang.Error t) {
        diag.note("the shape worker raised " + t);
        if (vizi.plugin.javasdk.doorbell.Doorbell.fatal(t)) throw t;
      }
    }
  }

  private void drag(ShapeEdit e) {
    long id = e.getDragId();
    if (!shaper.shapes(e.getTypeName())) {
      unshaped(e.getTypeName());
      return;
    }
    ShapeResult r;
    try {
      r = shaper.shape(e, false, () -> {
        synchronized (lock) {
          return closed || stopped.contains(id) || drags.containsKey(id);
        }
      });
    } catch (CancelSignal c) {
      return; // the shaper stopped for staleness: nothing to propose
    } catch (Exception ex) {
      diag.count("a shaper raised on a drag; nothing proposed", e.getTypeName() + ": " + ex);
      return;
    }
    String bad = problem(r, e);
    if (bad != null) {
      diag.count("a shaper's proposal was not sent: " + bad.replaceAll("[0-9]+", "#"), e.getTypeName() + ": " + bad);
      return;
    }
    ShapeResult.Builder pb = r.toBuilder().setDragId(id).setSeq(e.getSeq());
    if (r.getResolution() == Resolution.REJECTED) pb.clearPositions(); // none with REJECTED
    ShapeResult p = pb.build();
    synchronized (lock) {
      // admitted under the lock that stop() takes, so a commit or end that has arrived withdraws it
      if (closed || stopped.contains(id)) return;
      out.shapeProposal(PluginMsg.newBuilder().setShapeProposal(p).build(), id);
    }
  }

  private void commit(ShapeEdit e) {
    long id = e.getDragId();
    ShapeResult r;
    if (!shaper.shapes(e.getTypeName())) {
      unshaped(e.getTypeName());
      r = rejected("this plugin has no shaper registered for region type '" + e.getTypeName() + "'");
    } else {
      try {
        r = shaper.shape(e, true, () -> {
          synchronized (lock) {
            return closed || ended.contains(id);
          }
        });
        String bad = problem(r, e);
        if (bad != null) {
          diag.note("a shaper's answer to a commit of region type '" + e.getTypeName() + "' was refused before sending: " + bad);
          r = rejected("the plugin's shape was not valid: " + bad);
        } else if (r.getResolution() == Resolution.REJECTED) {
          r = r.toBuilder().clearPositions().build(); // none with REJECTED
        }
      } catch (HandlerFailure f) {
        r = rejected(f.getMessage() == null ? "rejected" : f.getMessage());
      } catch (CancelSignal c) {
        r = rejected(c.getMessage() == null ? "cancelled" : c.getMessage());
      } catch (Exception ex) {
        r = rejected(ex.getMessage() == null ? ex.getClass().getSimpleName() : ex.getMessage());
      }
    }
    ShapeResult a = r.toBuilder().setDragId(id).setSeq(0).build();
    synchronized (lock) {
      if (closed || ended.contains(id)) return; // ended while it was shaped: nobody waits on it
    }
    PluginMsg m = PluginMsg.newBuilder().setShapeAnswer(a).build();
    Bounds.Breach b = Bounds.check(m);
    if (b != null) {
      m = PluginMsg.newBuilder().setShapeAnswer(rejected("the shape exceeded a bound: " + b).toBuilder().setDragId(id)).build();
    }
    out.send(m);
  }

  private void unshaped(String typeName) {
    // a type declared with no shaper is the plugin's choice (§8.8): answered REJECTED, quietly
    if (shaper.declared(typeName)) return;
    synchronized (lock) {
      if (!warnedTypes.add(typeName)) return;
    }
    diag.note("an edit of region type '" + typeName + "', for which this plugin registers no shaper: its drags are not"
        + " answered, and its commits are answered REJECTED");
  }

  private static ShapeResult rejected(String message) {
    return ShapeResult.newBuilder().setResolution(Resolution.REJECTED).setMessage(WorkRegistry.clip(message)).build();
  }

  /**
   * Why {@code r} is not a shape the host would apply (§8.8: one finite position per vertex, or
   * none with {@code REJECTED}), or null. A {@code REJECTED} result's positions are ignored.
   */
  static @Nullable String problem(ShapeResult r, ShapeEdit e) {
    if (r.getResolution() == Resolution.UNRECOGNIZED) return "resolution " + r.getResolutionValue() + " is not a Resolution";
    if (r.getResolution() == Resolution.REJECTED) return null;
    int want = e.getMove().getRoi().getVerticesCount();
    if (r.getPositionsCount() != want) return r.getPositionsCount() + " positions for " + want + " vertices";
    for (int i = 0; i < want; i++) {
      Vec3 v = r.getPositions(i);
      if (!Double.isFinite(v.getZ()) || !Double.isFinite(v.getY()) || !Double.isFinite(v.getX())) {
        return "position " + i + " is not finite";
      }
    }
    return null;
  }

  /** The drags stopped so far, for tests. */
  Set<Long> stoppedForTest() {
    synchronized (lock) {
      return new LinkedHashSet<>(stopped);
    }
  }
}
