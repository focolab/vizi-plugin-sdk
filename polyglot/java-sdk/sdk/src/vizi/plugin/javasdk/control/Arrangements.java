package vizi.plugin.javasdk.control;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.DatasetState;
import vizi.plugin.v1.ViziPlugin.EpochAck;
import vizi.plugin.v1.ViziPlugin.EpochChange;
import vizi.plugin.v1.ViziPlugin.Geometry;
import vizi.plugin.v1.ViziPlugin.PaneEpoch;
import vizi.plugin.v1.ViziPlugin.TimeMapping;
import vizi.plugin.v1.ViziPlugin.TimeMappingAck;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.ToolBindingAck;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * The host→plugin arrangements the fast path references (control_plane_protocol.md §5.5,
 * §8.10, §8.11): tool bindings, pane epochs and time mappings. Each is installed locally, the
 * transport told first, and only then acknowledged; a repeat is acknowledged again. It refuses
 * (without acknowledging) only what §5.5 names — a statement that names nothing ({@code tool} or
 * {@code revision} 0), a content mismatch under a known identity, a regression — and installs and
 * diagnoses everything merely odd. Serialized by the caller (the dispatch thread).
 */
public final class Arrangements {

  /** The doorbell's lifecycle inputs, as the arrangements need them. */
  public interface Transport {
    boolean toolActivated(long handle, StringBuilder why);

    boolean toolRetired(long handle, StringBuilder why);

    boolean epochAcknowledged(int viewId, long epoch, StringBuilder why);
  }

  /** Reports a host bug the plugin can see. */
  public interface Reporter {
    void violation(ViolationSite where, String detail);

    void diagnostic(String text);
  }

  /** One pane's installed interpretation (§8.10): what an {@code E} record for it means. */
  public record PaneState(
      int viewId, long epoch, long gen, List<Integer> channelsHigh, Geometry geometry, int timeMappingRevision,
      Optional<double[]> timeMapping) {}

  /** What a tool binding did. */
  public sealed interface ToolChange {
    /** Newly activated, or updated in place; the activated hook fires for a new one. */
    record Activated(ToolBinding binding, boolean isNew) implements ToolChange {}

    /** Withdrawn: the deactivated hook fires (after the transport's cancellations). */
    record Retired(ToolBinding binding) implements ToolChange {}

    /** Re-acknowledged, or refused: no hook fires. */
    record None() implements ToolChange {}
  }

  public record ToolResult(@Nullable ToolBindingAck ack, ToolChange change) {}

  private final Transport transport;
  private final Reporter reporter;
  private final Map<Long, ToolBinding> bindings = new java.util.concurrent.ConcurrentHashMap<>();
  private final Map<Integer, PaneEpoch> panes = new java.util.concurrent.ConcurrentHashMap<>();
  private final Map<Integer, PaneState> paneStates = new java.util.concurrent.ConcurrentHashMap<>();
  private final Map<Long, DatasetState> datasets = new HashMap<>();
  private final Map<Long, TimeMapping> mappings = new HashMap<>();

  public Arrangements(Transport transport, Reporter reporter) {
    this.transport = transport;
    this.reporter = reporter;
  }

  // ---- tool bindings (§8.10) ----

  public ToolResult onToolBinding(ToolBinding b) {
    long h = Integer.toUnsignedLong(b.getTool());
    int rev = b.getRevision();
    if (h == 0 || rev == 0) {
      reporter.violation(ViolationSite.VIOLATION_SITE_ZERO_ID,
          "ToolBinding with " + (h == 0 ? "tool 0" : "revision 0") + " names no statement; refused, not acknowledged");
      return new ToolResult(null, new ToolChange.None());
    }
    ToolBindingAck ack = ToolBindingAck.newBuilder().setTool(b.getTool()).setRevision(rev).build();
    ToolBinding known = bindings.get(h);
    if (known != null) {
      int have = known.getRevision();
      if (Integer.compareUnsigned(rev, have) < 0) {
        reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_REGRESSED,
            "ToolBinding for tool " + h + " at revision " + Integer.toUnsignedString(rev) + " after " + Integer.toUnsignedString(have));
        return new ToolResult(null, new ToolChange.None());
      }
      if (rev == have) {
        if (known.equals(b)) return new ToolResult(ack, new ToolChange.None());
        reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_CONTENT_MISMATCH,
            "ToolBinding for tool " + h + " revision " + rev + " restated with different content");
        return new ToolResult(null, new ToolChange.None());
      }
      // a newer statement: update in place
      if (b.getRetired() && !known.getRetired()) {
        StringBuilder why = new StringBuilder();
        if (!transport.toolRetired(h, why)) {
          reporter.diagnostic("tool_retired(" + h + ") failed: " + why + "; not acknowledged");
          return new ToolResult(null, new ToolChange.None());
        }
        bindings.put(h, b);
        return new ToolResult(ack, new ToolChange.Retired(b));
      }
      if (!b.getRetired() && known.getRetired()) {
        reporter.diagnostic("ToolBinding revives retired tool " + h + "; a handle is never reused, so it stays retired");
        bindings.put(h, b);
        return new ToolResult(ack, new ToolChange.None());
      }
      bindings.put(h, b);
      return new ToolResult(ack, b.getRetired() ? new ToolChange.None() : new ToolChange.Activated(b, false));
    }
    // a new identity
    if (rev != 1) {
      reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_FIRST_REVISION,
          "tool " + h + "'s first ToolBinding carries revision " + Integer.toUnsignedString(rev) + ", not 1; installed");
    }
    if (b.getRetired()) {
      StringBuilder why = new StringBuilder();
      if (!transport.toolRetired(h, why)) {
        reporter.diagnostic("a retired ToolBinding for tool " + h + ", which this plugin never saw activated: " + why);
      }
      bindings.put(h, b);
      return new ToolResult(ack, new ToolChange.None());
    }
    StringBuilder why = new StringBuilder();
    if (!transport.toolActivated(h, why)) {
      reporter.diagnostic("tool_activated(" + h + ") failed: " + why + "; not acknowledged");
      return new ToolResult(null, new ToolChange.None());
    }
    bindings.put(h, b);
    return new ToolResult(ack, new ToolChange.Activated(b, true));
  }

  public Optional<ToolBinding> binding(long handle) {
    return Optional.ofNullable(bindings.get(handle));
  }

  /** The live (not retired) binding for a manifest tool id, if one is engaged. */
  public Optional<ToolBinding> bindingFor(String toolId) {
    ToolBinding best = null;
    for (ToolBinding b : bindings.values()) {
      if (!b.getRetired() && b.getToolId().equals(toolId)
          && (best == null || Integer.compareUnsigned(b.getTool(), best.getTool()) > 0)) {
        best = b;
      }
    }
    return Optional.ofNullable(best);
  }

  // ---- time mappings (§8.11) ----

  public @Nullable TimeMappingAck onTimeMapping(TimeMapping m) {
    int rev = m.getRevision();
    if (rev == 0) {
      reporter.violation(ViolationSite.VIOLATION_SITE_ZERO_ID, "TimeMapping for gen " + m.getGen() + " with revision 0; refused");
      return null;
    }
    TimeMappingAck ack = TimeMappingAck.newBuilder().setGen(m.getGen()).setRevision(rev).build();
    TimeMapping have = mappings.get(m.getGen());
    if (have != null) {
      if (Integer.compareUnsigned(rev, have.getRevision()) < 0) {
        reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_REGRESSED,
            "TimeMapping for gen " + m.getGen() + " at revision " + rev + " after " + have.getRevision());
        return null;
      }
      if (rev == have.getRevision()) {
        if (have.equals(m)) return ack;
        reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_CONTENT_MISMATCH,
            "TimeMapping for gen " + m.getGen() + " revision " + rev + " restated with different content");
        return null;
      }
    } else if (rev != 1) {
      reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_FIRST_REVISION,
          "gen " + m.getGen() + "'s first TimeMapping carries revision " + rev + "; installed");
    }
    double prev = Double.NEGATIVE_INFINITY;
    for (double t : m.getTList()) {
      if (!Double.isFinite(t) || t <= prev) {
        reporter.diagnostic("TimeMapping for gen " + m.getGen() + " is not finite and ascending; installed anyway");
        break;
      }
      prev = t;
    }
    mappings.put(m.getGen(), m);
    return ack;
  }

  // ---- epochs (§8.10) ----

  /** The panes that changed, installed, for the application to learn of after the ack. */
  public record EpochResult(EpochAck ack, List<PaneState> installed) {}

  public EpochResult onEpochChange(EpochChange ec) {
    Map<Long, DatasetState> byGen = new HashMap<>();
    for (DatasetState d : ec.getDatasetsList()) {
      if (byGen.put(d.getGen(), d) != null) reporter.diagnostic("EpochChange carries two DatasetStates for gen " + d.getGen());
      if (!d.hasGeometry()) reporter.diagnostic("DatasetState for gen " + d.getGen() + " has no geometry; installed anyway");
      else if (d.getGeometry().getGen() != d.getGen()) reporter.diagnostic("DatasetState gen " + d.getGen() + " carries geometry for gen " + d.getGeometry().getGen());
    }
    EpochAck.Builder ack = EpochAck.newBuilder();
    List<PaneState> installed = new ArrayList<>();
    java.util.Set<Integer> seen = new java.util.HashSet<>();
    for (PaneEpoch p : ec.getPanesList()) {
      int view = p.getViewId();
      long epoch = Integer.toUnsignedLong(p.getEpoch());
      if (!seen.add(view)) reporter.diagnostic("EpochChange names pane " + Integer.toUnsignedString(view) + " twice; the last stands");
      if (epoch == 0) {
        reporter.violation(ViolationSite.VIOLATION_SITE_ZERO_ID, "EpochChange for pane " + view + " with epoch 0; not acknowledged");
        continue;
      }
      DatasetState ds = byGen.getOrDefault(p.getGen(), datasets.get(p.getGen()));
      if (ds == null) {
        reporter.diagnostic("pane " + view + " names gen " + p.getGen() + ", for which no DatasetState was sent; installed anyway");
        ds = DatasetState.newBuilder().setGen(p.getGen()).build();
      }
      int tmRev = ds.getTimeMappingRevision();
      Optional<double[]> mapping = Optional.empty();
      if (tmRev != 0) {
        TimeMapping tm = mappings.get(p.getGen());
        if (tm == null || tm.getRevision() != tmRev) {
          reporter.violation(ViolationSite.VIOLATION_SITE_MISSING_ARRANGEMENT,
              "pane " + view + " epoch " + epoch + " names time mapping revision " + tmRev + " of gen " + p.getGen()
                  + ", which this plugin has not acknowledged; the pane is not acknowledged");
          continue;
        }
        mapping = Optional.of(tm.getTList().stream().mapToDouble(Double::doubleValue).toArray());
      }
      PaneEpoch have = panes.get(view);
      if (have != null) {
        long haveEpoch = Integer.toUnsignedLong(have.getEpoch());
        if (epoch < haveEpoch) {
          reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_REGRESSED,
              "EpochChange for pane " + view + " to epoch " + epoch + " after " + haveEpoch);
          continue;
        }
        if (epoch == haveEpoch) {
          DatasetState haveDs = datasets.get(have.getGen());
          if (have.equals(p) && (haveDs == null || haveDs.equals(ds))) {
            ack.addPanes(p);
            continue;
          }
          reporter.violation(ViolationSite.VIOLATION_SITE_ARRANGEMENT_CONTENT_MISMATCH,
              "pane " + view + " epoch " + epoch + " restated with different state");
          continue;
        }
      }
      if (Integer.compareUnsigned(view, 255) > 0) {
        // no E record can name it: install and acknowledge, no transport call
        reporter.diagnostic("pane view_id " + Integer.toUnsignedString(view) + " is above 255; installed, it will receive no E");
      } else {
        StringBuilder why = new StringBuilder();
        if (!transport.epochAcknowledged(view, epoch, why)) {
          reporter.diagnostic("epoch_acknowledged(" + view + ", " + epoch + ") failed: " + why + "; not acknowledged");
          continue;
        }
      }
      panes.put(view, p);
      datasets.put(p.getGen(), ds);
      PaneState st = new PaneState(view, epoch, p.getGen(), List.copyOf(p.getChannelsHighList()),
          ds.getGeometry(), tmRev, mapping);
      paneStates.put(view, st);
      installed.add(st);
      ack.addPanes(p);
    }
    return new EpochResult(ack.build(), installed);
  }

  public Optional<PaneState> pane(int viewId) {
    return Optional.ofNullable(paneStates.get(viewId));
  }

  /** The epoch this plugin acknowledged for a pane, or 0 (for {@code D}'s pane rule). */
  public long installedEpoch(int viewId) {
    PaneEpoch p = panes.get(viewId);
    return p == null ? 0 : Integer.toUnsignedLong(p.getEpoch());
  }

  /** Every installed pane, for a report. */
  public List<PaneState> panes() {
    return List.copyOf(paneStates.values());
  }

  /** Visits the time mapping of a gen, if one is arranged. */
  public Optional<double[]> timeMapping(long gen) {
    TimeMapping m = mappings.get(gen);
    return m == null ? Optional.empty() : Optional.of(m.getTList().stream().mapToDouble(Double::doubleValue).toArray());
  }
}
