package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.List;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.Cancelled;
import vizi.plugin.javasdk.CurrentWork;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Dataset;
import vizi.plugin.javasdk.Exporter;
import vizi.plugin.javasdk.Jobs;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.Task;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.ExporterDecl;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.CancelWork;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.InvokeCommand;
import vizi.plugin.v1.ViziPlugin.OpenExport;
import vizi.plugin.v1.ViziPlugin.OpenReader;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.WorkProgress;

/**
 * §8.16: a request reports its progress and can be canceled, and background
 * tasks are reported alongside. Canceling is cooperative and arrives on the dispatch thread, which
 * is free while the work runs on its worker. The shapes of the reference's {@code LongRunningWork}.
 */
class LongRunningWorkTest {
  static final Duration T = Duration.ofSeconds(10);

  TestHost host;
  final CountDownLatch started = new CountDownLatch(1);
  final CountDownLatch step = new CountDownLatch(1);
  final CountDownLatch release = new CountDownLatch(1);
  final AtomicBoolean secondRan = new AtomicBoolean();
  final AtomicReference<Task> task = new AtomicReference<>();
  final AtomicReference<Jobs.Job> job = new AtomicReference<>();
  final AtomicReference<Throwable> escaped = new AtomicReference<>();
  final java.util.concurrent.atomic.AtomicLong afterWait = new java.util.concurrent.atomic.AtomicLong();

  @AfterEach
  void down() {
    release.countDown();
    step.countDown();
    if (host != null) host.close();
  }

  static boolean await(CountDownLatch l) {
    try {
      return l.await(5, TimeUnit.SECONDS);
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      return false;
    }
  }

  /** Waits (up to 5 s) for {@code cancelled} to be true, as a handler polling between steps would. */
  static void untilCancelled(java.util.function.BooleanSupplier cancelled) {
    long end = System.nanoTime() + TimeUnit.SECONDS.toNanos(5);
    while (!cancelled.getAsBoolean() && System.nanoTime() < end) Thread.onSpinWait();
  }

  Plugin plugin() {
    var b = Plugin.builder("org.test.work", "Work", "0.1").entry("./run");
    b.command(CommandDecl.of("slow", "Slow"), ctx -> {
      ctx.progress(0.25, "a quarter");
      await(step);
      ctx.progress("x".repeat(400));
      await(release);
      return "done";
    });
    b.command(CommandDecl.of("stubborn", "Stubborn"), ctx -> {
      started.countDown();
      untilCancelled(ctx::cancelled);
      try {
        throw new Cancelled("stopped at step 3");
      } catch (Exception e) {
        throw new IllegalStateException("a catch (Exception) must not see Cancelled", e);
      }
    });
    b.command(CommandDecl.of("first", "First"), ctx -> {
      started.countDown();
      await(release);
      return "first";
    });
    b.command(CommandDecl.of("second", "Second"), ctx -> {
      secondRan.set(true);
      return "second";
    });
    b.command(CommandDecl.of("begin", "Begin a task"), ctx -> {
      task.set(ctx.startTask("Index the file"));
      return "";
    });
    b.command(CommandDecl.of("fit", "Fit"), ctx -> {
      try {
        ctx.startTask("Fit").run(t -> {
          task.set(t);
          throw new IllegalStateException("no convergence");
        });
      } catch (IllegalStateException e) {
        escaped.set(e);
      }
      return "";
    });
    b.command(CommandDecl.of("job", "Job"), ctx -> ctx.jobs().start("Crunch", j -> {
      job.set(j);
      j.progress(0.5, "halfway");
      started.countDown();
      while (true) {
        j.checkpoint();
        Thread.sleep(2);
      }
    }));
    b.command(CommandDecl.of("save", "Save"), ctx -> {
      try {
        return ctx.host().requestSavePath("out.csv", "csv", "Save").map(Object::toString).orElse("declined");
      } finally {
        afterWait.set(System.nanoTime());
      }
    });
    b.command(CommandDecl.of("wait_job", "Wait for a job"), ctx -> ctx.jobs().await("Crunch", j -> {
      job.set(j);
      started.countDown();
      while (true) {
        j.checkpoint();
        Thread.sleep(2);
      }
    }, Duration.ofMinutes(5)));
    b.jobs(o -> {});
    b.reader(ReaderDecl.of("slow", ".slow"), (uri, dir) -> {
      CurrentWork.progress(0.5, "decompressing");
      started.countDown();
      untilCancelled(CurrentWork::cancelled);
      CurrentWork.checkCancelled();
      return new Dataset() {
        public Info info() {
          return Info.of(1, 1, 1, 1, 1, DType.U8);
        }

        public Volume volume(int t, int c) {
          return Volume.u8(1, 1, 1, new byte[1]);
        }
      };
    });
    b.exporter(ExporterDecl.of("slow", ".slow"), req -> {
      CurrentWork.progress("preparing");
      started.countDown();
      untilCancelled(CurrentWork::cancelled);
      throw new Cancelled();
    });
    return b.build();
  }

  void start() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().application()).open();
    host.hello();
  }

  void invoke(String id, String command) {
    host.send(HostMsg.newBuilder().setId(id).setInvokeCommand(InvokeCommand.newBuilder().setCommandId(command)).build());
  }

  void cancelRequest(String id) {
    host.send(HostMsg.newBuilder().setCancelWork(CancelWork.newBuilder().setRequestId(id)).build());
  }

  void cancelTask(String id) {
    host.send(HostMsg.newBuilder().setCancelWork(CancelWork.newBuilder().setTaskId(id)).build());
  }

  WorkProgress progress(java.util.function.Predicate<WorkProgress> p) throws InterruptedException {
    return host.expect(m -> m.hasWorkProgress() && p.test(m.getWorkProgress()), T).getWorkProgress();
  }

  WorkRegistry work() {
    return host.serving.session().work();
  }

  static void cancelled(PluginMsg m, String id, String message) {
    assertTrue(m.hasError(), m.toString());
    assertEquals(id, m.getId());
    assertEquals(Error.Kind.CANCELLED, m.getError().getKind());
    if (message != null) assertEquals(message, m.getError().getMessage());
  }

  @Test
  void aCommandReportsItsProgressUnderItsRequestId() throws Exception {
    start();
    invoke("c1", "slow");
    WorkProgress first = progress(w -> w.getRequestId().equals("c1"));
    assertEquals(0.25, first.getProgress().getValue());
    assertEquals("a quarter", first.getText());
    step.countDown();
    WorkProgress second = progress(w -> w.getRequestId().equals("c1") && w.getProgress().getIndeterminate());
    assertEquals(WorkRegistry.MAX_TEXT_BYTES, second.getText().getBytes(StandardCharsets.UTF_8).length, "clipped");
    release.countDown();
    assertEquals("done", host.reply("c1", T).getCommandDone().getMessage());
  }

  @Test
  void aCancelReachesACommandWhileItsWorkerIsBusy() throws Exception {
    start();
    invoke("c1", "stubborn");
    assertTrue(await(started));
    cancelRequest("c1");
    cancelled(host.reply("c1", T), "c1", "stopped at step 3");
  }

  /**
   * A cancel wakes a command blocked on the host. vizi offers Stop plugin 10 s
   * after a cancel goes unanswered, with its own dialog still up; the command must answer CANCELLED
   * at once rather than when the person closes the picker. The host's late answer is disposed of.
   */
  @Test
  void aCancelWakesACommandWaitingOnAPicker() throws Exception {
    start();
    invoke("c1", "save");
    PluginMsg asked = host.expect(PluginMsg::hasRequestSavePath, T);
    long cancelledAt = System.nanoTime();
    cancelRequest("c1");
    cancelled(host.reply("c1", T), "c1", null);
    long woke = afterWait.get();
    assertTrue(woke != 0 && TimeUnit.NANOSECONDS.toMillis(woke - cancelledAt) < 2000, "woken promptly, not by the picker's answer");
    // the person's answer, after: it matches no waiter, and nothing is sent for it
    host.send(HostMsg.newBuilder().setId(asked.getId()).setSavePathResult(
        vizi.plugin.v1.ViziPlugin.SavePathResult.newBuilder().setPath("/tmp/out.csv")).build());
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream().filter(m -> m.hasError() || m.hasCommandDone()).toList());
    // and the session goes on: the next command runs
    invoke("c2", "second");
    assertEquals("second", host.reply("c2", T).getCommandDone().getMessage());
  }

  /** A command awaiting a job is woken by its cancel, and the job is canceled too. */
  @Test
  void aCancelWakesACommandAwaitingAJobAndCancelsTheJob() throws Exception {
    start();
    invoke("c1", "wait_job");
    assertTrue(await(started));
    cancelRequest("c1");
    cancelled(host.reply("c1", T), "c1", "Crunch was canceled with its request");
    untilCancelled(() -> job.get().cancelled());
    assertTrue(job.get().cancelled());
  }

  @Test
  void aRequestCancelledBeforeItStartsIsNeverRun() throws Exception {
    start();
    invoke("c1", "first");
    assertTrue(await(started));
    invoke("c2", "second");
    cancelRequest("c2");
    Thread.sleep(50);
    release.countDown();
    assertTrue(host.reply("c1", T).hasCommandDone());
    cancelled(host.reply("c2", T), "c2", null);
    assertFalse(secondRan.get());
  }

  @Test
  void aReaderAndAnExporterReportAndNoticeThroughCurrentWork() throws Exception {
    start();
    host.send(HostMsg.newBuilder().setId("r1").setOpenReader(OpenReader.newBuilder().setUri("/d/a.slow").setReaderId("slow")).build());
    WorkProgress p = progress(w -> w.getRequestId().equals("r1"));
    assertEquals("decompressing", p.getText());
    assertEquals(0.5, p.getProgress().getValue());
    assertTrue(await(started));
    cancelRequest("r1");
    cancelled(host.reply("r1", T), "r1", "canceled");

    host.send(HostMsg.newBuilder().setId("x1").setOpenExport(OpenExport.newBuilder().setExporterId("slow").setLocator("/out/a.slow")
        .addShapeTczyx(1).addShapeTczyx(1).addShapeTczyx(1).addShapeTczyx(1).addShapeTczyx(1)
        .setDtype(vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8).setLittleEndian(true)).build());
    assertTrue(progress(w -> w.getRequestId().equals("x1")).getProgress().getIndeterminate());
    cancelRequest("x1");
    cancelled(host.reply("x1", T), "x1", "canceled");

    // outside a handler: nothing, and no error
    CurrentWork.progress(0.9, "nowhere");
    assertFalse(CurrentWork.cancelled());
    assertNull(CurrentWork.requestId());
  }

  @Test
  void aBackgroundTaskIsBegunReportedCancelledAndEnded() throws Exception {
    start();
    invoke("c1", "begin");
    assertTrue(host.reply("c1", T).hasCommandDone());
    Task t = task.get();
    WorkProgress begun = progress(w -> w.getTaskId().equals(t.id()));
    assertEquals("Index the file", begun.getTitle());
    assertTrue(begun.getCancellable() && begun.getProgress().getIndeterminate());
    t.progress(0.5, "half");
    assertEquals(0.5, progress(w -> w.getTaskId().equals(t.id()) && w.getText().equals("half")).getProgress().getValue());
    cancelTask(t.id());
    untilCancelled(t::cancelled);
    assertTrue(t.cancelled());
    t.done("stopped early");
    WorkProgress ended = progress(w -> w.getTaskId().equals(t.id()) && w.getDone());
    assertEquals("stopped early", ended.getText());
    assertEquals(List.of(), work().openTasks());
    t.progress(0.9, "late"); // after its end: nothing
    t.done("again");
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream().filter(PluginMsg::hasWorkProgress).toList());
  }

  /**
   * {@code cancellable} is advice to the host's UI. A cancel naming a task
   * begun {@code cancellable = false} sets its flag as for any task, and is never refused or
   * reported: no error, no violation, no diagnostic. The task may ignore it.
   */
  @Test
  void aCancelForANonCancellableTaskSetsTheFlagAndIsNeverRefusedOrReported() throws Exception {
    start();
    WorkRegistry.Task t = work().startTask("Commit the index", false, null);
    WorkProgress begun = progress(w -> w.getTaskId().equals(t.id()));
    assertFalse(begun.getCancellable());
    int diagnosticsBefore = host.serving.session().diagnosticCount();
    cancelTask(t.id());
    untilCancelled(t::cancelled);
    assertTrue(t.cancelled(), "the flag is set");
    assertFalse(t.ended(), "and nothing ends it but the task");
    assertEquals(List.of(), host.quiet(Duration.ofMillis(200)).stream()
        .filter(m -> m.hasError() || m.hasContractViolation() || m.hasWorkProgress()).toList(), "nothing refused or reported");
    assertEquals(diagnosticsBefore, host.serving.session().diagnosticCount(), "and no diagnostic");
    t.done("committed anyway");
    assertEquals("committed anyway", progress(w -> w.getTaskId().equals(t.id()) && w.getDone()).getText());
  }

  @Test
  void aTaskRunEndsWithWhatEscaped() throws Exception {
    start();
    invoke("c1", "fit");
    assertTrue(host.reply("c1", T).hasCommandDone());
    assertEquals("no convergence", escaped.get().getMessage(), "rethrown");
    WorkProgress ended = progress(w -> w.getTaskId().equals(task.get().id()) && w.getDone());
    assertEquals("failed: no convergence", ended.getText());
  }

  @Test
  void openTasksAreBounded() throws Exception {
    start();
    WorkRegistry w = work();
    for (int i = 0; i < WorkRegistry.MAX_OPEN_TASKS; i++) w.startTask("t" + i, true, null);
    IllegalStateException e = assertThrows(IllegalStateException.class, () -> w.startTask("one too many", true, null));
    assertTrue(e.getMessage().contains("already open"), e.getMessage());
    w.openTasks().get(0).done("done");
    w.startTask("room again", true, null);
  }

  @Test
  void cancellingWhatIsNotOpenIsANoOp() {
    WorkRegistry w = new WorkRegistry(new Outbound(new OutboundTest.Held(), s -> {}));
    assertFalse(w.cancel(CancelWork.newBuilder().setRequestId("gone").build()));
    assertFalse(w.cancel(CancelWork.newBuilder().setTaskId("t99").build()));
    assertFalse(w.cancel(CancelWork.getDefaultInstance()));
    WorkRegistry.Request r = w.register("c1");
    w.finished(r);
    assertFalse(w.cancel(CancelWork.newBuilder().setRequestId("c1").build()), "finished work");
  }

  @Test
  void progressCoalescesPerPieceOfWork() throws Exception {
    OutboundTest.Held ch = new OutboundTest.Held();
    Outbound out = new Outbound(ch, s -> {});
    out.workProgress(WorkProgress.newBuilder().setRequestId("r3").setText("a").build());
    out.workProgress(WorkProgress.newBuilder().setTaskId("r3").setText("task").build());
    out.workProgress(WorkProgress.newBuilder().setRequestId("r3").setText("b").build());
    out.workProgress(WorkProgress.newBuilder().setRequestId("r4").setText("c").build());
    out.dropRequestProgress("r4");
    ch.ready.set(true);
    out.onReady();
    assertTrue(out.drain(2000));
    assertEquals(List.of("b", "task"), ch.got().stream().map(m -> m.getWorkProgress().getText()).sorted().toList(),
        "the latest per (request or task, id); an answered request's report dropped");
  }

  @Test
  void aJobIsABackgroundTaskThePersonCanCancel() throws Exception {
    start();
    invoke("c1", "job");
    assertTrue(host.reply("c1", T).hasCommandDone());
    assertTrue(await(started));
    WorkProgress p = progress(w -> w.getTitle().equals("Crunch") && w.getText().equals("halfway"));
    String id = p.getTaskId();
    cancelTask(id);
    WorkProgress ended = progress(w -> w.getTaskId().equals(id) && w.getDone());
    assertEquals("Crunch: canceled", ended.getText());
    assertTrue(job.get().cancelled());
  }

  @Test
  void textIsCutAtACharacterBoundary() {
    assertEquals("short", WorkRegistry.clip("short"));
    String s = "é".repeat(200); // 400 bytes
    String c = WorkRegistry.clip(s);
    assertEquals(256, c.getBytes(StandardCharsets.UTF_8).length);
    String e = "a" + "€".repeat(100); // 1 + 300 bytes: cut to 1 + 85*3 = 256
    assertEquals(256, WorkRegistry.clip(e).getBytes(StandardCharsets.UTF_8).length);
    String f = "ab" + "€".repeat(100); // 2 + 3k: 254 fits, 257 does not
    assertEquals(254, WorkRegistry.clip(f).getBytes(StandardCharsets.UTF_8).length);
    String g = "😀".repeat(100); // four bytes each: 64 of them
    assertEquals(64 * 2, WorkRegistry.clip(g).length());
  }
}
