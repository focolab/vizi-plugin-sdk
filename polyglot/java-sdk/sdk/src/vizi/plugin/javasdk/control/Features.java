package vizi.plugin.javasdk.control;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.OptionalInt;
import org.jspecify.annotations.Nullable;

/**
 * Version gating: the features a session may use, by the protocol minor that introduced each.
 *
 * <p>A plugin's {@code Ready} carries the lower of the host's {@code Hello} minor and this SDK's own
 * ({@link #readyMinor}), and the session runs at that minor. Minors only add, so a session at minor
 * {@code m} has every feature introduced at or below {@code m}. Every SDK uses the same feature
 * names; check one with {@code Host.supports} and guard a call with {@code Host.require}.
 */
public final class Features {
  private Features() {}

  /** The "view" subscription and its {@code ViewChanged} push. */
  public static final String VIEW_SUBSCRIPTION = "view_subscription";

  /** The "regions" subscription and its {@code RegionsChanged} push. */
  public static final String REGIONS_SUBSCRIPTION = "regions_subscription";

  /** {@code ReadDataVolume.raw}: the file's own voxels, not a filter's output. */
  public static final String RAW_READ = "raw_read";

  /** {@code PlanStage} and {@code StageArm}: the plugin as a stage in the host's chain. */
  public static final String PROCESSING_STAGES = "processing_stages";

  /** A region type the plugin shapes: {@code ShapeDrag}, {@code ShapeCommit}, {@code ShapeEditEnded}. */
  public static final String REGION_SHAPERS = "region_shapers";

  /** {@code SetView}: the plugin moves the view when the person asked it to. */
  public static final String SET_VIEW = "set_view";

  /** {@code OfferFollow}, {@code FollowStarted}, {@code FollowState} and {@code FollowEnded}. */
  public static final String FOLLOW = "follow";

  private static final Map<String, Integer> TABLE = table();

  private static Map<String, Integer> table() {
    var t = new LinkedHashMap<String, Integer>();
    t.put(VIEW_SUBSCRIPTION, 0);
    t.put(REGIONS_SUBSCRIPTION, 0);
    t.put(RAW_READ, 0);
    t.put(PROCESSING_STAGES, 0);
    t.put(REGION_SHAPERS, 0);
    t.put(SET_VIEW, 0);
    t.put(FOLLOW, 0);
    return Map.copyOf(t);
  }

  /** Every feature this SDK knows, with the protocol minor that introduced it. */
  public static Map<String, Integer> all() {
    return TABLE;
  }

  /** The protocol minor that introduced {@code feature}; empty for a name this SDK does not know. */
  public static OptionalInt minor(String feature) {
    Integer m = TABLE.get(feature);
    return m == null ? OptionalInt.empty() : OptionalInt.of(m);
  }

  /** The minor this SDK announces in {@code Ready} to a host whose {@code Hello} says {@code hostMinor}. */
  public static int readyMinor(int hostMinor) {
    return Math.min(hostMinor, Identity.PROTOCOL_MINOR);
  }

  /** The handshake's refusal of a {@code Ready} below the plugin's own {@code engines.vizi}, or null. */
  static @Nullable String enginesRefusal(long enginesMinor, int readyMinor) {
    if (enginesMinor <= readyMinor) return null;
    int major = Identity.PROTOCOL_MAJOR;
    return "this plugin's manifest requires protocol " + major + "." + enginesMinor + " but its code speaks "
        + major + "." + readyMinor;
  }

  /**
   * Why a session at {@code session} (null before the handshake) may not use {@code feature}, or
   * null when it may.
   */
  public static @Nullable String unavailable(String feature, @Nullable ProtocolVersion session) {
    return refusal(feature, minor(feature), session);
  }

  static @Nullable String refusal(String feature, OptionalInt needs, @Nullable ProtocolVersion session) {
    if (needs.isEmpty()) return "feature \"" + feature + "\" is not one this SDK knows";
    int major = Identity.PROTOCOL_MAJOR;
    if (session == null) {
      return "feature \"" + feature + "\" needs protocol " + major + "." + needs.getAsInt()
          + "; the session has no handshake yet";
    }
    if (needs.getAsInt() > session.minor()) {
      return "feature \"" + feature + "\" needs protocol " + major + "." + needs.getAsInt() + "; this session speaks "
          + session;
    }
    return null;
  }
}
