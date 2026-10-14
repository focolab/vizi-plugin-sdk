import java.util.HashMap;
import java.util.Map;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.Stage;
import vizi.plugin.javasdk.StageSlot;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.StageDecl;
import vizi.plugin.javasdk.manifest.StageParam;

/**
 * The stage-chain example (../SPEC.md): four processing stages. {@code gain} rewrites one
 * channel, {@code diff} appends one, {@code widen} repacks as f32 and may crop, {@code drift}
 * writes only the pose. The arithmetic is SPEC §3's exactly, so a chain mixing languages has one
 * right answer.
 */
public final class StageChain {
  private StageChain() {}

  public static void main(String[] args) {
    System.exit(PluginMain.run(plugin(), args));
  }

  public static Plugin plugin() {
    return Plugin.builder("org.example.stage_chain", "Stage Chain", "0.1.0")
        .manifestVersion(1)
        .entry("./stage-chain")
        .stage(StageDecl.of("gain",
                StageParam.ofFloat("factor", 1.0).withName("Factor").withMin(0).withMax(16),
                StageParam.ofInt("channel", 0).withName("Channel").withMin(0).withMax(65535),
                StageParam.ofInt("stall_ms", 0).withName("Stall (ms)").withMin(0).withMax(60000))
            .withName("Gain"), new Gain())
        .stage(StageDecl.of("diff",
                StageParam.ofInt("a", 0).withMin(0).withMax(65535),
                StageParam.ofInt("b", 1).withMin(0).withMax(65535))
            .withName("Absolute difference"), new Diff())
        .stage(StageDecl.of("widen",
                StageParam.ofInt("crop", 0).withName("Crop (voxels, y and x)").withMin(0).withMax(1024))
            .withName("Widen to f32"), new Widen())
        .stage(StageDecl.of("drift", StageParam.ofInt("channel", 0).withMin(0).withMax(65535))
            .withName("Drift (pose only)"), new Drift())
        .build();
  }

  static Refusal decline(String why) {
    return Refusal.unsupported(why);
  }

  /** SPEC §3.1: {@code v × factor} in f64, then ties-to-even and a clamp for integers. */
  public static final class Gain implements Stage {
    @Override
    public Plan plan(Input in, Params p) throws Refusal {
      int c = p.integer("channel");
      if (!in.has(c)) throw decline("channel " + c + " is not in the input");
      return Plan.of().modifies(c, c);
    }

    @Override
    public void process(StageSlot slot) throws InterruptedException {
      long stall = slot.params().i("stall_ms");
      if (stall > 0) Thread.sleep(stall); // holding the turn: how the driver misses a deadline
      double factor = slot.params().f("factor");
      Volume v = slot.plane(slot.params().integer("channel"));
      int[] e = slot.extent();
      for (int z = e[0]; z < e[1]; z++) {
        for (int y = e[2]; y < e[3]; y++) {
          for (int x = e[4]; x < e[5]; x++) {
            long i = v.index(z, y, x);
            double p = v.get(i) * factor;
            switch (v.dtype()) {
              case U8 -> v.setU8(i, (int) Math.max(0, Math.min(255, Math.rint(p))));
              case U16 -> v.setU16(i, (int) Math.max(0, Math.min(65535, Math.rint(p))));
              case F32 -> v.setF32(i, (float) p);
              case F64 -> v.setF64(i, p);
            }
          }
        }
      }
    }
  }

  /** SPEC §3.2: appends {@code |A − B|}, exact for integers, zero outside the extent. */
  public static final class Diff implements Stage {
    @Override
    public Plan plan(Input in, Params p) throws Refusal {
      int a = p.integer("a"), b = p.integer("b");
      if (!in.has(a) || !in.has(b)) throw decline("channels " + a + " and " + b + " are not both in the input");
      return Plan.of().adds("absdiff", "|a - b|", a, b);
    }

    @Override
    public void process(StageSlot slot) {
      Volume a = slot.plane(slot.params().integer("a"));
      Volume b = slot.plane(slot.params().integer("b"));
      Volume out = slot.addChannel("absdiff"); // zeroed
      int[] e = slot.extent();
      for (int z = e[0]; z < e[1]; z++) {
        for (int y = e[2]; y < e[3]; y++) {
          for (int x = e[4]; x < e[5]; x++) {
            long i = a.index(z, y, x);
            switch (a.dtype()) {
              case U8 -> out.setU8(i, Math.abs(a.u8(i) - b.u8(i)));
              case U16 -> out.setU16(i, Math.abs(a.u16(i) - b.u16(i)));
              case F32 -> out.setF32(i, Math.abs(a.f32(i) - b.f32(i)));
              case F64 -> out.setF64(i, Math.abs(a.f64(i) - b.f64(i)));
            }
          }
        }
      }
    }
  }

  /** SPEC §3.3: every plane as f32, cropped in y and x; the SDK's relayout restates extent and origin. */
  public static final class Widen implements Stage {
    @Override
    public Plan plan(Input in, Params p) throws Refusal {
      int crop = p.integer("crop");
      if (in.y() - 2 * crop < 1 || in.x() - 2 * crop < 1) {
        throw decline("a crop of " + crop + " leaves nothing of " + in.y() + " x " + in.x());
      }
      Plan plan = Plan.of().alloc(in.z(), in.y() - 2 * crop, in.x() - 2 * crop).dtype(DType.F32);
      for (int c : in.channels()) plan.modifies(c, c);
      return plan;
    }

    @Override
    public void process(StageSlot slot) {
      int crop = slot.params().integer("crop");
      int[] a = slot.alloc();
      slot.relayout(DType.F32, a[0], a[1] - 2 * crop, a[2] - 2 * crop, 0, crop, crop);
    }
  }

  /** SPEC §3.4: the pose from a centroid, per arm, with the causal rule. */
  public static final class Drift implements Stage {
    record ArmKey(int pipeline, int position) {}

    record State(long t, double[] reference) {}

    private final Map<ArmKey, State> state = new HashMap<>();

    @Override
    public Plan plan(Input in, Params p) throws Refusal {
      int c = p.integer("channel");
      if (!in.has(c)) throw decline("channel " + c + " is not in the input");
      if (in.dtype() != DType.U8 && in.dtype() != DType.U16) {
        throw decline("drift sums integers exactly; " + in.dtype() + " is not an integer type");
      }
      return Plan.of().reads(c);
    }

    @Override
    public void process(StageSlot slot) throws Stage.Refuse {
      ArmKey key = new ArmKey(slot.pipeline(), slot.position());
      State last = state.get(key);
      if (last != null && slot.t() != last.t() + 1) {
        state.remove(key); // the next volume starts over
        throw new Stage.Refuse(Stage.Refuse.CAUSAL, "t " + slot.t() + " does not follow t " + last.t());
      }
      Volume v = slot.plane(slot.params().integer("channel"));
      int[] e = slot.extent();
      long s = 0, sz = 0, sy = 0, sx = 0;
      for (int z = e[0]; z < e[1]; z++) {
        for (int y = e[2]; y < e[3]; y++) {
          for (int x = e[4]; x < e[5]; x++) {
            long w = v.dtype() == DType.U8 ? v.u8(v.index(z, y, x)) : v.u16(v.index(z, y, x));
            s += w;
            sz += w * z;
            sy += w * y;
            sx += w * x;
          }
        }
      }
      double[] centroid = null;
      if (s != 0) {
        double[] o = slot.origin();
        long[] sums = {sz, sy, sx};
        centroid = new double[3];
        for (int k = 0; k < 3; k++) centroid[k] = o[k] + ((double) sums[k] / (double) s - e[2 * k]);
      }
      double[] reference = last == null ? centroid : last.reference();
      if (centroid != null && reference != null) {
        double[] tk = {reference[0] - centroid[0], reference[1] - centroid[1], reference[2] - centroid[2]};
        slot.composePose(new double[] {1, 0, 0, 0, 1, 0, 0, 0, 1}, tk);
      }
      state.put(key, new State(slot.t(), reference));
    }
  }
}
