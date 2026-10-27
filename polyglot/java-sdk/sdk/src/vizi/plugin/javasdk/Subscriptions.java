package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Optional;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.RegionsChanged;
import vizi.plugin.v1.ViziPlugin.ViewChanged;

/**
 * The subscribed state pushes (control_plane_protocol.md §8.7), as the author sees them, and
 * the workers that deliver them. Each subscription has a worker of its own, so a handler may make
 * host requests; while it runs, newer pushes wait, and the handler then sees the newest state:
 *
 * <ul>
 *   <li>{@code view}: only the latest {@link ViewChange} is kept;
 *   <li>{@code regions}: pushes are MERGED by region (a later report of a region replaces an
 *       earlier one, a deletion included), never dropped, and a {@code reset} voids everything
 *       held before it.
 * </ul>
 */
public final class Subscriptions {
  private Subscriptions() {}

  /**
   * The view the person is on: the dataset generation (0 when none is open; a new value is a new
   * dataset), {@code t}, {@code z}, the visible channels ascending, the selected region
   * ({@code ""} for none), and the filter applied to what the views show (empty for the file's own
   * voxels). Like {@code ViewState.filter} it is session-level: it says nothing about
   * what a read returns. A read is the source unless the person shares a filter's output with
   * plugins and the read is not raw ({@link Host#readRawVolume}); {@link Host#filterOf} on its reply
   * says which, and a raw read's reply names no filter while this one still names the views'.
   */
  public record ViewChange(long openGeneration, long t, long z, List<Integer> visibleChannels, String selectedRegionId,
      Optional<vizi.plugin.v1.ViziPlugin.AppliedFilter> filter) {
    public ViewChange {
      visibleChannels = List.copyOf(visibleChannels);
      java.util.Objects.requireNonNull(filter, "filter");
    }

    /** The channel shown first: {@code visibleChannels[0]}, or 0 when none is shown. */
    public int shownChannel() {
      return visibleChannels.isEmpty() ? 0 : visibleChannels.get(0);
    }

    static ViewChange of(ViewChanged v) {
      return new ViewChange(v.getOpenGeneration(), Integer.toUnsignedLong(v.getT()), Integer.toUnsignedLong(v.getZ()),
          v.getVisibleChannelsList(), v.getSelectedRegionId(), v.hasFilter() ? Optional.of(v.getFilter()) : Optional.empty());
    }
  }

  /**
   * One region's change: its {@code revision} (as {@code RoiRecord.revision}; 0 when deleted), its
   * {@code origin} ({@code "user"} or {@code "plugin:<id>"}; {@code ""} when deleted). Never its
   * geometry: {@code Host.rois(ids)} reads that for just these regions.
   */
  public record RegionChange(String regionId, long revision, String origin, boolean deleted) {
    static RegionChange of(vizi.plugin.v1.ViziPlugin.RegionChange c) {
      return new RegionChange(c.getRegionId(), c.getRevision(), c.getOrigin(), c.getDeleted());
    }
  }

  /**
   * What changed since the handler last ran. With {@code reset} (the session's first push, or a
   * dataset change), {@code changes} is every region there is and anything held before is void.
   */
  public record RegionsChange(long openGeneration, boolean reset, List<RegionChange> changes) {
    public RegionsChange {
      changes = List.copyOf(changes);
    }
  }

  @FunctionalInterface
  public interface ViewHandler {
    void changed(CommandContext ctx, ViewChange change) throws Exception;
  }

  @FunctionalInterface
  public interface RegionsHandler {
    void changed(CommandContext ctx, RegionsChange change) throws Exception;
  }

  /** A worker delivering the newest state; {@code offer} never blocks. */
  abstract static class Worker<T> {
    private final String name;
    private final Plugin.Runtime rt;
    protected final Object lock = new Object();
    private boolean running;
    private boolean stopped;

    Worker(String name, Plugin.Runtime rt) {
      this.name = name;
      this.rt = rt;
    }

    /** Folds a push into what is pending (under the lock). */
    abstract void fold(T push);

    /** Takes what is pending, or null (under the lock). */
    abstract @Nullable Object take();

    abstract void deliver(CommandContext ctx, Object pending) throws Exception;

    final void offer(T push) {
      boolean start;
      synchronized (lock) {
        if (stopped) return;
        fold(push);
        start = !running;
        running = true;
      }
      if (start) Thread.ofVirtual().name("vizi-" + name).start(this::drain);
    }

    private void drain() {
      while (true) {
        Object pending;
        synchronized (lock) {
          pending = stopped ? null : take();
          if (pending == null) {
            running = false;
            return;
          }
        }
        try {
          deliver(new CommandContext(rt, "", Map.of()), pending);
        } catch (vizi.plugin.javasdk.control.SessionEndedException e) {
          return; // the session is ending: its requests fail, and nothing more is delivered
        } catch (Exception | Error e) {
          rt.log("the " + name + " handler threw: " + e);
        }
      }
    }

    final void stop() {
      synchronized (lock) {
        stopped = true;
      }
    }
  }

  static final class Views extends Worker<ViewChanged> {
    private final ViewHandler handler;
    private @Nullable ViewChange pending;

    Views(Plugin.Runtime rt, ViewHandler handler) {
      super("view", rt);
      this.handler = handler;
    }

    @Override
    void fold(ViewChanged push) {
      pending = ViewChange.of(push);
    }

    @Override
    @Nullable Object take() {
      ViewChange p = pending;
      pending = null;
      return p;
    }

    @Override
    void deliver(CommandContext ctx, Object p) throws Exception {
      handler.changed(ctx, (ViewChange) p);
    }
  }

  static final class Regions extends Worker<RegionsChanged> {
    private final RegionsHandler handler;
    private boolean any;
    private boolean reset;
    private long generation;
    private final Map<String, RegionChange> held = new LinkedHashMap<>();

    Regions(Plugin.Runtime rt, RegionsHandler handler) {
      super("regions", rt);
      this.handler = handler;
    }

    @Override
    void fold(RegionsChanged push) {
      if (push.getReset()) {
        held.clear();
        reset = true;
      }
      any = true;
      generation = push.getOpenGeneration();
      for (vizi.plugin.v1.ViziPlugin.RegionChange c : push.getChangesList()) {
        held.remove(c.getRegionId()); // re-insert: the newest report of a region wins, and keeps its place last
        held.put(c.getRegionId(), RegionChange.of(c));
      }
    }

    @Override
    @Nullable Object take() {
      if (!any) return null;
      RegionsChange p = new RegionsChange(generation, reset, new ArrayList<>(held.values()));
      any = false;
      reset = false;
      held.clear();
      return p;
    }

    @Override
    void deliver(CommandContext ctx, Object p) throws Exception {
      handler.changed(ctx, (RegionsChange) p);
    }
  }

  /** For tests: folds pushes as the worker does and returns what a handler would see next. */
  static RegionsChange fold(List<RegionsChanged> pushes) {
    Regions r = new Regions(new Plugin.Runtime(), (c, x) -> {});
    for (RegionsChanged p : pushes) r.fold(p);
    Object t = r.take();
    return t == null ? new RegionsChange(0, false, List.of()) : (RegionsChange) t;
  }
}
