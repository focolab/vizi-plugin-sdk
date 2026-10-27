package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * A processing stage (processing_stages.md): a plugin that edits a volume in place, in shared
 * memory, between vizi reading it and vizi using it.
 *
 * <ul>
 *   <li>{@link #plan} answers {@code PlanStage}: what the stage will produce from an input with
 *       these parameters, and which channels it needs. Throw {@link Refusal} to decline
 *       ({@code UNSUPPORTED}, typically). Runs on the command worker.
 *   <li>{@link #process} is one turn on one slot. Throw {@link Refuse} to refuse the volume
 *       (before writing anything); any other exception fails the turn. Runs on the stage
 *       thread, one turn at a time, where host requests are allowed.
 * </ul>
 *
 * The SDK validates every slot header before the turn, refuses a turn whose declared input
 * channels are missing, holds the stage to the armed plan (a turn finishing with another dtype or
 * a larger allocation fails), writes the outcome and hands the turn on.
 */
public interface Stage {

  Plan plan(Input input, Params params) throws Exception;

  void process(StageSlot slot) throws Exception;

  /** What the stage would be handed: channel indices in plane order, the allocation, the dtype. */
  record Input(List<Integer> channels, int z, int y, int x, DType dtype) {
    public Input {
      channels = List.copyOf(channels);
    }

    public boolean has(int channel) {
      return channels.contains(channel);
    }
  }

  /** The parameters of one position, every declared key present and checked. */
  final class Params {
    private final Map<String, Value> values;

    Params(Map<String, Value> values) {
      this.values = Map.copyOf(values);
    }

    public Map<String, Value> raw() {
      return values;
    }

    private Value get(String key) {
      Value v = values.get(key);
      if (v == null) throw new IllegalArgumentException("no parameter " + key);
      return v;
    }

    /** A {@code float} parameter. */
    public double f(String key) {
      return get(key).getF();
    }

    /** An {@code int} parameter. */
    public long i(String key) {
      return get(key).getI();
    }

    /** An {@code int} parameter that fits an {@code int}. */
    public int integer(String key) {
      return Math.toIntExact(get(key).getI());
    }

    public boolean b(String key) {
      return get(key).getB();
    }

    /** A {@code choice} or {@code string} parameter. */
    public String s(String key) {
      return get(key).getS();
    }
  }

  /**
   * A plan: the largest output, and the channels it needs. The output allocation and dtype
   * default to the input's.
   */
  final class Plan {
    final Map<Integer, List<Integer>> modifies = new LinkedHashMap<>();
    final List<NewChannel> adds = new ArrayList<>();
    final List<Integer> reads = new ArrayList<>();
    int[] alloc = {-1, -1, -1};
    Optional<DType> dtype = Optional.empty();

    record NewChannel(String key, String name, List<Integer> needs) {}

    public static Plan of() {
      return new Plan();
    }

    /** Rewrites {@code channel} in place, needing {@code needs}. */
    public Plan modifies(int channel, int... needs) {
      modifies.put(channel, box(needs));
      return this;
    }

    /** Appends a channel under the plugin-local {@code key}, needing {@code needs}. */
    public Plan adds(String key, String name, int... needs) {
      adds.add(new NewChannel(Objects.requireNonNull(key, "key"), Objects.requireNonNull(name, "name"), box(needs)));
      return this;
    }

    /** Reads {@code channels} without writing them. */
    public Plan reads(int... channels) {
      reads.addAll(box(channels));
      return this;
    }

    /** The output allocation (each at most the input's). */
    public Plan alloc(int z, int y, int x) {
      alloc = new int[] {z, y, x};
      return this;
    }

    /** The dtype of every channel afterwards. */
    public Plan dtype(DType d) {
      dtype = Optional.of(d);
      return this;
    }

    private static List<Integer> box(int[] a) {
      List<Integer> out = new ArrayList<>();
      for (int v : a) out.add(v);
      return List.copyOf(out);
    }
  }

  /** Refuses the volume (§6.3): the chain ends, the data is left as the previous stage's. */
  final class Refuse extends Exception {
    private static final long serialVersionUID = 1L;

    /** It needs earlier state it does not have (causal). */
    public static final int CAUSAL = 1;
    /** A declared input channel is absent. */
    public static final int MISSING_INPUT = 2;
    public static final int OTHER = 3;

    private final int reason;

    public Refuse(int reason, String message) {
      super(message);
      if (reason < 1 || reason > 3) throw new IllegalArgumentException("a refusal's reason is 1, 2 or 3");
      this.reason = reason;
    }

    public int reason() {
      return reason;
    }
  }
}
