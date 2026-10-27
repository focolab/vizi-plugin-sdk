package vizi.plugin.javasdk.memory;

import java.time.Duration;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.function.Consumer;
import org.jspecify.annotations.Nullable;

/**
 * The unconditional re-check schedule doorbell_D_notification.md §5a obliges an SDK to provide
 * for every region it consumes on the application's behalf. Published policy:
 *
 * <ul>
 *   <li><b>Interval: 20 ms.</b> Every registered check runs at least that often, whatever hints
 *       arrive or do not.
 *   <li><b>Hints only bring a check forward</b>: a {@code D} naming a registered region runs its
 *       check at once; no hint, and no missing hint, ever postpones one.
 *   <li><b>Delivery</b> of a consumed Pulsed value runs on the subscription's own thread through a
 *       one-value mailbox: a slow callback never delays the schedule, and a value that arrives
 *       while the previous one is still being handled replaces the undelivered one (the older is
 *       dropped and counted), so the application always gets the latest.
 *   <li><b>Coalescing</b>: several hints for one region before its check runs are one check.
 *   <li>A region whose segment retires or is released, or whose lane is quarantined, leaves the
 *       schedule, and a diagnostic says which and why.
 * </ul>
 *
 * Checks run on this scheduler's own platform thread and must be short and non-blocking; a
 * check that has real work to do hands it to a worker.
 */
public final class Recheck implements SegmentStore.Hints, AutoCloseable {

  public static final Duration INTERVAL = Duration.ofMillis(20);

  /** One registered check. Returning false removes it from the schedule. */
  public interface Check {
    boolean run();
  }

  private record Key(long segment, int region) {}

  private final Object lock = new Object();
  private final Map<Key, List<Check>> checks = new LinkedHashMap<>();
  private final List<Key> forwarded = new ArrayList<>();
  private final Consumer<String> diagnostics;
  private final Thread thread;
  private boolean stopped;

  public Recheck(Consumer<String> diagnostics) {
    this.diagnostics = diagnostics;
    this.thread = new Thread(this::run, "vizi-recheck");
    this.thread.setDaemon(true);
    this.thread.start();
  }

  /** Registers {@code check} for a region; the returned handle removes it. */
  public AutoCloseable register(long segmentId, int region, Check check) {
    Key k = new Key(segmentId, region);
    synchronized (lock) {
      checks.computeIfAbsent(k, x -> new ArrayList<>()).add(check);
      forwarded.add(k);
      lock.notifyAll();
    }
    return () -> {
      synchronized (lock) {
        List<Check> l = checks.get(k);
        if (l != null) {
          l.remove(check);
          if (l.isEmpty()) checks.remove(k);
        }
      }
    };
  }

  @Override
  public void bringForward(long segmentId, int region) {
    Key k = new Key(segmentId, region);
    synchronized (lock) {
      if (!checks.containsKey(k) || forwarded.contains(k)) return;
      forwarded.add(k);
      lock.notifyAll();
    }
  }

  private void run() {
    long next = System.nanoTime() + INTERVAL.toNanos();
    while (true) {
      List<Map.Entry<Key, Check>> due = new ArrayList<>();
      synchronized (lock) {
        while (!stopped && forwarded.isEmpty() && System.nanoTime() < next) {
          long waitMs = Math.max(1, (next - System.nanoTime()) / 1_000_000);
          try {
            lock.wait(waitMs);
          } catch (InterruptedException e) {
            return;
          }
        }
        if (stopped) return;
        if (System.nanoTime() >= next) {
          for (Map.Entry<Key, List<Check>> e : checks.entrySet()) {
            for (Check c : e.getValue()) due.add(Map.entry(e.getKey(), c));
          }
          forwarded.clear();
          next = System.nanoTime() + INTERVAL.toNanos();
        } else {
          for (Key k : forwarded) {
            List<Check> l = checks.get(k);
            if (l != null) for (Check c : l) due.add(Map.entry(k, c));
          }
          forwarded.clear();
        }
      }
      for (Map.Entry<Key, Check> d : due) {
        boolean keep;
        try {
          keep = d.getValue().run();
        } catch (RuntimeException e) {
          diagnostics.accept("re-check of segment " + Long.toUnsignedString(d.getKey().segment()) + " region "
              + d.getKey().region() + " raised " + e + "; it leaves the schedule");
          keep = false;
        }
        if (!keep) {
          synchronized (lock) {
            List<Check> l = checks.get(d.getKey());
            if (l != null) {
              l.remove(d.getValue());
              if (l.isEmpty()) checks.remove(d.getKey());
            }
          }
        }
      }
    }
  }

  @Override
  public void close() {
    synchronized (lock) {
      stopped = true;
      lock.notifyAll();
    }
  }

  /**
   * Subscribes to a host-produced Pulsed lane: each value is consumed on the schedule and
   * delivered, latest-wins, on the subscription's own thread.
   */
  public LaneSubscription subscribe(SegmentStore store, Segment segment, int region, Consumer<PulsedLane.Value> onValue) {
    PulsedLane lane = segment.hostLanes().get(region);
    if (lane == null) throw new IllegalArgumentException(segment + " region " + region + " is not a host-produced PULSED lane");
    LaneSubscription sub = new LaneSubscription(onValue, diagnostics);
    sub.registration = register(segment.id(), region, () -> {
      if (sub.closed) return false;
      if (lane.quarantined()) {
        diagnostics.accept(segment + " region " + region + " left the schedule: quarantined (" + lane.quarantineReason() + ")");
        sub.close();
        return false;
      }
      try (SegmentStore.Op op = store.begin(segment, true)) {
        PulsedLane.Value v = lane.read();
        if (v != null) sub.offer(v);
        return true;
      } catch (SegmentStore.UnavailableException e) {
        diagnostics.accept(segment + " region " + region + " left the schedule: " + e.getMessage());
        sub.close();
        return false;
      }
    });
    return sub;
  }

  /** A lane subscription and its one-value mailbox. */
  public static final class LaneSubscription implements AutoCloseable {
    private final Consumer<PulsedLane.Value> onValue;
    private final Consumer<String> diagnostics;
    private @Nullable AutoCloseable registration;
    private PulsedLane.@Nullable Value pending;
    private boolean delivering;
    private long dropped;
    private volatile boolean closed;

    LaneSubscription(Consumer<PulsedLane.Value> onValue, Consumer<String> diagnostics) {
      this.onValue = onValue;
      this.diagnostics = diagnostics;
    }

    synchronized void offer(PulsedLane.Value v) {
      if (pending != null) dropped++;
      pending = v;
      if (!delivering) {
        delivering = true;
        Thread.ofVirtual().name("vizi-lane-delivery").start(this::deliver);
      }
    }

    private void deliver() {
      while (true) {
        PulsedLane.Value v;
        synchronized (this) {
          v = pending;
          pending = null;
          if (v == null || closed) {
            delivering = false;
            return;
          }
        }
        try {
          onValue.accept(v);
        } catch (RuntimeException e) {
          diagnostics.accept("a lane subscriber raised " + e);
        }
      }
    }

    /** Values replaced in the mailbox before delivery. */
    public synchronized long dropped() {
      return dropped;
    }

    @Override
    public void close() {
      closed = true;
      AutoCloseable r = registration;
      if (r != null) {
        try {
          r.close();
        } catch (Exception ignored) {
          // removal cannot fail
        }
      }
    }
  }
}
