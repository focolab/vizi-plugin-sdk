package vizi.plugin.javasdk;

import java.lang.foreign.MemorySegment;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.doorbell.Doorbell;
import vizi.plugin.javasdk.doorbell.Notification;
import vizi.plugin.javasdk.memory.RegionTable;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.javasdk.nativeio.Atomics;
import vizi.plugin.v1.ViziPlugin.PlanStage;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StageChannelNeed;
import vizi.plugin.v1.ViziPlugin.StageNewChannel;
import vizi.plugin.v1.ViziPlugin.StagePlanned;
import vizi.plugin.v1.ViziPlugin.StageShape;
import vizi.plugin.v1.ViziPlugin.Value;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * Runs the plugin's stages (processing_stages.md §6): watches every slot of every ring an arm
 * names (on the re-check schedule, brought forward by {@code D}), and takes a turn when one
 * acquire-load of the turn word shows a {@code (pipeline, position)} this plugin is armed at with
 * an iteration it has not processed there. Turns run one at a time on the stage thread.
 */
final class StageRunner {

  private record ArmKey(int pipeline, int position) {}

  private record PlanKey(String stage, StageShape input, Map<String, Value> params) {}

  private record SlotKey(long segment, int region) {}

  private record Seen(long segment, int region, int pipeline, int position) {}

  private final Plugin.Runtime rt;
  private final Map<String, Stage> stages;
  private final Object lock = new Object();
  private final Map<ArmKey, StageArm> arms = new HashMap<>();
  private final Map<PlanKey, StagePlanned> plans = new HashMap<>();
  private final Set<Long> watched = new HashSet<>();
  private final Set<SlotKey> busy = new HashSet<>();
  private final Map<Seen, Long> lastIteration = new HashMap<>();
  private final ExecutorService worker = Executors.newSingleThreadExecutor(r -> {
    Thread t = new Thread(r, "vizi-stage");
    t.setDaemon(true);
    return t;
  });

  StageRunner(Plugin.Runtime rt, Map<String, Stage> stages) {
    this.rt = rt;
    this.stages = stages;
  }

  // ---- planning (command worker) ----

  StagePlanned plan(PlanStage p) throws Exception {
    Stage s = stages.get(p.getStageId());
    if (s == null) throw new vizi.plugin.javasdk.control.HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "no stage " + p.getStageId());
    StageShape in = p.getInput();
    DType dt = DType.of(in.getDtype()).orElseThrow(
        () -> new vizi.plugin.javasdk.control.HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.PROTOCOL, "input dtype " + in.getDtype()));
    Stage.Input input = new Stage.Input(in.getChannelsList(), in.getZ(), in.getY(), in.getX(), dt);
    Stage.Plan plan = Plugin.Runtime.refusals(() -> s.plan(input, new Stage.Params(p.getParamsMap())));
    StagePlanned.Builder b = StagePlanned.newBuilder();
    int[] a = plan.alloc;
    b.setOutput(StageShape.newBuilder()
        .setZ(a[0] < 0 ? in.getZ() : a[0])
        .setY(a[1] < 0 ? in.getY() : a[1])
        .setX(a[2] < 0 ? in.getX() : a[2])
        .setDtype(plan.dtype.map(DType::wire).orElse(in.getDtype())));
    for (Map.Entry<Integer, List<Integer>> e : plan.modifies.entrySet()) {
      b.addModifies(StageChannelNeed.newBuilder().setChannel(e.getKey()).addAllNeeds(e.getValue()));
    }
    for (Stage.Plan.NewChannel n : plan.adds) {
      b.addAdds(StageNewChannel.newBuilder().setKey(n.key()).setName(n.name()).addAllNeeds(n.needs()));
    }
    b.addAllReads(plan.reads);
    StagePlanned out = b.build();
    synchronized (lock) {
      plans.put(new PlanKey(p.getStageId(), in, Map.copyOf(p.getParamsMap())), out);
    }
    return out;
  }

  // ---- arming (dispatch thread) ----

  void armed(StageArm a) {
    boolean watch;
    synchronized (lock) {
      arms.put(new ArmKey(a.getPipeline(), a.getPosition()), a);
      watch = watched.add(a.getSegmentId());
    }
    if (!watch) return;
    Session s = rt.session();
    Optional<Segment> seg = s.store().installed(a.getSegmentId());
    if (seg.isEmpty()) return;
    for (RegionTable.Spec slot : seg.get().stagedSlots()) {
      long segId = seg.get().id();
      s.recheck().register(segId, slot.index(), () -> poll(seg.get(), slot));
    }
  }

  // ---- watching (re-check thread: short, never blocking) ----

  private boolean poll(Segment seg, RegionTable.Spec slot) {
    Session s = rt.sessionOrNull();
    if (s == null) return false;
    if (seg.state() != Segment.State.LIVE) {
      synchronized (lock) {
        watched.remove(seg.id());
      }
      return false;
    }
    long word;
    try (SegmentStore.Op op = s.store().begin(seg, false)) {
      word = Atomics.loadAcquire(op.memory(), slot.offset() + StageSlot.TURN);
    } catch (SegmentStore.UnavailableException e) {
      return false;
    }
    long iteration = word & 0xFFFF_FFFFL;
    int pipeline = (int) ((word >>> 32) & 0xFFFF);
    int position = (short) (word >>> 48);
    if (iteration == 0 || position <= 0) return true;
    SlotKey sk = new SlotKey(seg.id(), slot.index());
    synchronized (lock) {
      StageArm arm = arms.get(new ArmKey(pipeline, position));
      if (arm == null || arm.getSegmentId() != seg.id()) return true;
      Long last = lastIteration.get(new Seen(seg.id(), slot.index(), pipeline, position));
      if (last != null && last == iteration) return true;
      if (!busy.add(sk)) return true;
    }
    worker.execute(() -> {
      try {
        turn(seg, slot, iteration, pipeline, position);
      } finally {
        synchronized (lock) {
          busy.remove(sk);
        }
      }
    });
    return true;
  }

  // ---- a turn (stage thread) ----

  private static final int PROCESSED = 1, REFUSED = 2, FAILED = 3;

  private void turn(Segment seg, RegionTable.Spec spec, long iteration, int pipeline, int position) {
    Session s = rt.sessionOrNull();
    if (s == null) return;
    StageArm arm;
    StagePlanned plan;
    synchronized (lock) {
      arm = arms.get(new ArmKey(pipeline, position)); // the revision installed when the turn STARTS
      if (arm == null) return;
      plan = plans.get(new PlanKey(arm.getStageId(), arm.getInput(), Map.copyOf(arm.getParamsMap())));
      lastIteration.put(new Seen(seg.id(), spec.index(), pipeline, position), iteration);
    }
    Stage stage = stages.get(arm.getStageId());
    if (stage == null) return;
    try (SegmentStore.Op op = s.store().begin(seg, true)) {
      MemorySegment m = op.memory().asSlice(spec.offset(), spec.extent());
      long word = Atomics.loadAcquire(m, StageSlot.TURN);
      if (word != (iteration | ((long) pipeline << 32) | ((long) (position & 0xFFFF) << 48))) return; // moved on
      String bad = StageSlot.problem(m, spec.extent(), arm);
      if (bad != null) {
        s.violations().report(ViolationSite.VIOLATION_SITE_STAGE_SLOT_INVALID,
            "slot " + spec.index() + " of segment " + Long.toUnsignedString(seg.id()) + " at (" + pipeline + ", " + position + "): " + bad,
            new byte[0]);
        handOn(s, seg, spec, m, iteration, pipeline, position, FAILED, 1);
        return;
      }
      StageSlot slot = new StageSlot(m, arm);
      if (plan != null) {
        Set<Integer> needs = new LinkedHashSet<>(plan.getReadsList());
        for (StageChannelNeed n : plan.getModifiesList()) needs.addAll(n.getNeedsList());
        for (StageNewChannel n : plan.getAddsList()) needs.addAll(n.getNeedsList());
        List<Integer> present = slot.channels();
        for (int c : needs) {
          if (!present.contains(c)) {
            rt.log("stage " + arm.getStageId() + " refuses t " + slot.t() + ": its input channel " + c + " is absent");
            handOn(s, seg, spec, m, iteration, pipeline, position, REFUSED, Stage.Refuse.MISSING_INPUT);
            return;
          }
        }
      }
      int outcome;
      int reason;
      try {
        stage.process(slot);
        String breach = slot.planBreach();
        if (breach == null) {
          outcome = PROCESSED;
          reason = 0;
        } else {
          rt.log("stage " + arm.getStageId() + " broke its plan: " + breach);
          outcome = FAILED;
          reason = 2;
        }
      } catch (Stage.Refuse r) {
        outcome = REFUSED;
        reason = r.reason();
      } catch (Exception | Error e) {
        rt.log("stage " + arm.getStageId() + " failed on t " + slot.t() + ": " + e);
        outcome = FAILED;
        reason = 2;
      } finally {
        slot.close();
      }
      handOn(s, seg, spec, m, iteration, pipeline, position, outcome, reason);
    } catch (SegmentStore.RetiredException e) {
      s.store().countRetiredDrop(); // a normal retirement: not started, quietly
    } catch (SegmentStore.UnavailableException e) {
      rt.log("stage turn on segment " + Long.toUnsignedString(seg.id()) + " not started: " + e.getMessage());
    }
  }

  /** Writes the outcome, stores the turn with release, and rings {@code D} (§6.1, §6.3). */
  private static void handOn(Session s, Segment seg, RegionTable.Spec spec, MemorySegment m, long iteration, int pipeline,
      int position, int outcome, int reason) {
    m.set(StageSlot.U32, StageSlot.OUTCOME, outcome);
    m.set(StageSlot.U32, StageSlot.REASON, reason);
    if (outcome == PROCESSED) m.set(StageSlot.U32, StageSlot.DONE_THROUGH, position);
    int next = outcome == PROCESSED ? position + 1 : -position;
    Atomics.storeRelease(m, StageSlot.TURN, iteration | ((long) pipeline << 32) | ((long) (next & 0xFFFF) << 48));
    Doorbell d = s.doorbell().orElse(null);
    if (d != null) d.sendHint(Notification.of(seg.id(), spec.index()));
  }

  void stop() {
    worker.shutdownNow();
  }

  @Nullable StageArm arm(int pipeline, int position) {
    synchronized (lock) {
      return arms.get(new ArmKey(pipeline, position));
    }
  }
}
