package vizi.plugin.javasdk;

import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.manifest.ViewTarget;
import vizi.plugin.v1.ViziPlugin.FollowEndReason;
import vizi.plugin.v1.ViziPlugin.FollowState;

/**
 * One running Follow (control_plane_protocol.md §8.15): timed movement the person started from a
 * panel's offer, and the plugin drives. Its handler ({@link Plugin.Builder#follow}) runs on a
 * thread of its own for as long as the Follow lasts, and returning or throwing from it ends the
 * Follow.
 *
 * <pre>{@code
 * b.follow("quant", (ctx, f) -> {
 *   for (String r : regions) {
 *     if (!f.move(ViewMove.to().frameRegion(r)) || !f.waitFor(Duration.ofMillis(500))) return;
 *   }
 *   f.end();
 * });
 * }</pre>
 *
 * <p>States are whole, coalescing and carry no cause: the host applies the newest at up to 30 Hz.
 * A state it cannot apply ends the Follow {@code INVALID}, with a detail, since a push has no reply.
 * The person's input on anything the Follow drives ends all of it ({@code USER_TOOK_CONTROL}), and
 * a Resume starts it again with {@link #resumed()}; where to pick up is the plugin's to decide.
 */
public final class Follow {

  private final Plugin.Runtime rt;
  private final long id;
  private final String panelId;
  private final Set<ViewTarget> targets;
  private final List<String> rawTargets;
  private final boolean resumed;
  private long seq;
  private boolean ending; // the plugin sent EndFollow
  private boolean ended; // the host said so, or the session went
  private @Nullable FollowEndReason reason;
  private String detail = "";

  Follow(Plugin.Runtime rt, long id, String panelId, List<String> rawTargets, Set<ViewTarget> targets, boolean resumed) {
    this.rt = rt;
    this.id = id;
    this.panelId = panelId;
    this.rawTargets = List.copyOf(rawTargets);
    this.targets = Set.copyOf(targets);
    this.resumed = resumed;
  }

  /** The host's id for this Follow. */
  public long id() {
    return id;
  }

  public String panelId() {
    return panelId;
  }

  /** What it drives: the panel's {@code follow_targets}, as the host sent them. */
  public Set<ViewTarget> targets() {
    return targets;
  }

  /** The targets as the host named them, a name this SDK does not know included. */
  public List<String> targetNames() {
    return rawTargets;
  }

  /** Started by the person's Resume after they took control. */
  public boolean resumed() {
    return resumed;
  }

  /** Neither ended by the host nor by this plugin, and the session is up. */
  public synchronized boolean active() {
    return !ended && !ending;
  }

  /** How the host ended it, once it has ({@code PLUGIN_ENDED} after {@link #end()}). */
  public synchronized Optional<FollowEndReason> reason() {
    return Optional.ofNullable(reason);
  }

  /** The host's sentence with the end, such as why a state was {@code INVALID}. */
  public synchronized String detail() {
    return detail;
  }

  /**
   * Streams where the view should be now, as a whole state with a rising {@code seq}. A target the
   * Follow does not drive is refused here ({@link IllegalArgumentException}), before anything is
   * sent. Returns false, sending nothing, once the Follow has ended.
   */
  public boolean move(ViewMove move) {
    Set<ViewTarget> named = move.targets();
    for (ViewTarget t : named) {
      if (!targets.contains(t)) {
        throw new IllegalArgumentException("this Follow drives " + targets + ", not " + t.wire() + " (panel " + panelId
            + "'s follow_targets)");
      }
    }
    var state = FollowState.newBuilder().setFollowId(id);
    move.into(state);
    Session s;
    synchronized (this) {
      if (!active()) return false;
      s = rt.sessionOrNull();
      if (s == null) return false;
      state.setSeq(++seq);
      s.outbound().followState(state.build()); // under the lock, so states go out in seq order
    }
    return true;
  }

  /**
   * Waits up to {@code d} (a dwell), returning true when it passed with the Follow still running,
   * and false the moment the Follow ends.
   */
  public synchronized boolean waitFor(Duration d) throws InterruptedException {
    long end = System.nanoTime() + d.toNanos();
    while (active()) {
      long left = end - System.nanoTime();
      if (left <= 0) return true;
      wait(Math.max(1, java.util.concurrent.TimeUnit.NANOSECONDS.toMillis(left)));
    }
    return false;
  }

  /** Ends it ("the tour is done"): sends {@code EndFollow} once; the host answers {@code PLUGIN_ENDED}. */
  public void end() {
    Session s;
    synchronized (this) {
      if (!active()) return;
      ending = true;
      notifyAll();
      s = rt.sessionOrNull();
    }
    if (s != null) s.outbound().endFollow(id);
  }

  /** The host's {@code FollowEnded}, or the session's end (no reason). */
  synchronized void ended(@Nullable FollowEndReason why, String text) {
    if (reason == null && why != null) {
      reason = why;
      detail = text;
    }
    ended = true;
    notifyAll();
  }

  /** Whether the host has said it ended (as opposed to the plugin only having asked). */
  synchronized boolean hostEnded() {
    return ended;
  }

  @Override
  public String toString() {
    return "Follow " + Long.toUnsignedString(id) + " on " + panelId + " " + targets + (resumed ? " (resumed)" : "");
  }
}
