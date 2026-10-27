use super::*;
use vizi_doorbell::{
    plugin::{Exporter, Reader, Volume},
    work,
};

async fn invoke(s: &Session, id: &str) {
    s.send(
        id,
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
}
async fn cancel_request(s: &Session, id: &str) {
    s.send(
        "",
        Host::CancelWork(pb::CancelWork {
            subject: Some(pb::cancel_work::Subject::RequestId(id.into())),
        }),
    )
    .await;
}
async fn report(s: &mut Session, id: &str) -> pb::WorkProgress {
    let message = s.recv().await;
    assert!(message.id.is_empty());
    let Some(Plugin::WorkProgress(report)) = message.msg else {
        panic!("{message:?}")
    };
    assert_eq!(
        report.subject,
        Some(pb::work_progress::Subject::RequestId(id.into()))
    );
    report
}
async fn cancelled_reply(s: &mut Session, id: &str) -> String {
    let message = s.recv().await;
    assert_eq!(message.id, id);
    let Some(Plugin::Error(error)) = message.msg else {
        panic!("{message:?}")
    };
    assert_eq!(error.kind, pb::error::Kind::Cancelled as i32);
    error.message
}
#[derive(Clone, Copy)]
enum Cancellation {
    Result,
    Unwinding,
}
impl Cancellation {
    fn message(self) -> &'static str {
        match self {
            Self::Result => "canceled",
            Self::Unwinding => "stopped at step 3",
        }
    }
}
fn await_cancel(mode: Cancellation) -> control::Result<()> {
    let until = std::time::Instant::now() + Duration::from_secs(5);
    while !work::cancelled() {
        assert!(
            std::time::Instant::now() < until,
            "cancellation never reached worker"
        );
        std::thread::sleep(Duration::from_millis(1));
    }
    match mode {
        Cancellation::Result => work::check_cancelled(),
        Cancellation::Unwinding => work::cancel_unwinding("stopped at step 3"),
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn command_reports_fraction_indeterminate_and_clipped_utf8() {
    let (step, wait) = std::sync::mpsc::channel();
    let wait = Mutex::new(wait);
    let mut s = Session::new(Registry::new().command("run", move |ctx, _| {
        ctx.report_progress(Some(0.25), "a quarter")?;
        wait.lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        ctx.report_progress(None, &"€".repeat(100))?;
        wait.lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        Ok(done())
    }))
    .await;
    s.recv().await;
    invoke(&s, "c1").await;
    let first = report(&mut s, "c1").await;
    assert_eq!(first.progress.unwrap().value, 0.25);
    assert_eq!(first.text, "a quarter");
    step.send(()).unwrap();
    let second = report(&mut s, "c1").await;
    assert!(second.progress.unwrap().indeterminate);
    assert_eq!(second.text, "€".repeat(85));
    step.send(()).unwrap();
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unwinding_command_bypasses_result_mapping_and_discards_after_reply() {
    let called = Arc::new(AtomicUsize::new(0));
    let calls = called.clone();
    let mut s = Session::new(Registry::new().command("run", move |ctx, _| {
        let calls = calls.clone();
        ctx.after_reply(move |_| {
            calls.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })?;
        ctx.report_progress(None, "started")?;
        let operation = || -> control::Result<pb::CommandDone> {
            while !ctx.cancelled() {
                std::thread::sleep(Duration::from_millis(1));
            }
            work::cancel_unwinding("stopped at step 3")
        };
        operation().map_err(|e| control::Error::internal(format!("ordinary failure: {e}")))
    }))
    .await;
    s.recv().await;
    invoke(&s, "c1").await;
    report(&mut s, "c1").await;
    cancel_request(&s, "c1").await;
    assert_eq!(cancelled_reply(&mut s, "c1").await, "stopped at step 3");
    assert_eq!(called.load(Ordering::SeqCst), 0);
    // No fault, diagnostic, or cancellation reply beyond the original request's answer.
    s.barrier().await;
    work::report_progress(Some(0.9), "outside").unwrap();
    assert!(!work::cancelled());
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn ignored_cancel_keeps_request_slot_until_normal_answer() {
    let (release, wait) = std::sync::mpsc::channel();
    let wait = Mutex::new(wait);
    let mut s = Session::new(Registry::new().command("run", move |ctx, _| {
        ctx.report_progress(None, "started")?;
        wait.lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        assert!(ctx.cancelled());
        Ok(done())
    }))
    .await;
    s.recv().await;
    invoke(&s, "c1").await;
    report(&mut s, "c1").await;
    cancel_request(&s, "c1").await;
    s.barrier().await;
    invoke(&s, "c2").await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.message.contains("concurrent"))
    );
    release.send(()).unwrap();
    let reply = s.recv().await;
    assert_eq!(reply.id, "c1");
    assert!(matches!(reply.msg, Some(Plugin::CommandDone(_))));
    cancel_request(&s, "c1").await;
    cancel_request(&s, "unknown").await;
    s.send("", Host::CancelWork(pb::CancelWork::default()))
        .await;
    s.send(
        "",
        Host::CancelWork(pb::CancelWork {
            subject: Some(pb::cancel_work::Subject::TaskId("gone".into())),
        }),
    )
    .await;
    s.barrier().await;
    s.stop().await;
}

struct SlowReader {
    mode: Cancellation,
    slow_open: bool,
    closed: Arc<AtomicUsize>,
}
impl Reader for SlowReader {
    fn info(&self) -> control::Result<pb::ReaderOpened> {
        if self.slow_open {
            work::report_progress(None, "opening")?;
            await_cancel(self.mode)?;
        }
        Ok(pb::ReaderOpened {
            shape_tczyx: vec![1; 5],
            dtype: 1,
            ..Default::default()
        })
    }
    fn read_volume(&self, _: &pb::ReadVolume) -> control::Result<Volume> {
        work::report_progress(Some(0.5), "decompressing")?;
        await_cancel(self.mode)?;
        unreachable!()
    }
    fn metadata(&self) -> control::Result<pb::MetadataResult> {
        work::report_progress(None, "metadata")?;
        await_cancel(self.mode)?;
        unreachable!()
    }
    fn close(&self) -> control::Result<()> {
        self.closed.fetch_add(1, Ordering::SeqCst);
        Ok(())
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unwinding_reader_open_read_and_metadata() {
    reader_cancellation(Cancellation::Unwinding).await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn result_reader_open_read_and_metadata() {
    reader_cancellation(Cancellation::Result).await;
}
async fn reader_cancellation(mode: Cancellation) {
    let closed = Arc::new(AtomicUsize::new(0));
    let count = closed.clone();
    let mut s = Session::new(Registry::new().reader("raw-reader", move |_, req| {
        Ok(Arc::new(SlowReader {
            mode,
            slow_open: req.uri == "slow",
            closed: count.clone(),
        }))
    }))
    .await;
    s.recv().await;
    s.send(
        "fast",
        Host::OpenReader(pb::OpenReader {
            uri: "fast".into(),
            ..Default::default()
        }),
    )
    .await;
    let Some(Plugin::ReaderOpened(opened)) = s.recv().await.msg else {
        panic!()
    };
    for (id, request, text) in [
        (
            "open",
            Host::OpenReader(pb::OpenReader {
                uri: "slow".into(),
                ..Default::default()
            }),
            "opening",
        ),
        (
            "read",
            Host::ReadVolume(pb::ReadVolume {
                handle: opened.handle.clone(),
                ..Default::default()
            }),
            "decompressing",
        ),
        (
            "metadata",
            Host::Metadata(pb::Metadata {
                handle: opened.handle.clone(),
            }),
            "metadata",
        ),
    ] {
        s.send(id, request).await;
        assert_eq!(report(&mut s, id).await.text, text);
        cancel_request(&s, id).await;
        assert_eq!(cancelled_reply(&mut s, id).await, mode.message());
        assert_eq!(
            closed.load(Ordering::SeqCst),
            1,
            "only failed candidate closes"
        );
        s.barrier().await;
    }
    s.stop().await;
}

struct SlowExporter {
    mode: Cancellation,
}
impl Exporter for SlowExporter {
    fn write(&self, _: u32, _: &[u32], _: &[u8]) -> control::Result<u64> {
        work::report_progress(Some(0.5), "writing")?;
        await_cancel(self.mode)?;
        unreachable!()
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        Ok(Default::default())
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unwinding_exporter_chunk_preserves_handle() {
    exporter_cancellation(Cancellation::Unwinding).await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn result_exporter_chunk_preserves_handle() {
    exporter_cancellation(Cancellation::Result).await;
}
async fn exporter_cancellation(mode: Cancellation) {
    let mut s = Session::new(
        Registry::new().exporter("raw", move |_, _| Ok(Arc::new(SlowExporter { mode }))),
    )
    .await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1; 5],
            dtype: 1,
            ..Default::default()
        }),
    )
    .await;
    let Some(Plugin::ExportOpened(opened)) = s.recv().await.msg else {
        panic!()
    };
    s.send(
        "chunk",
        Host::ExportChunk(pb::ExportChunk {
            handle: opened.handle.clone(),
            channels: vec![0],
            source: Some(pb::export_chunk::Source::Inline(vec![42])),
            ..Default::default()
        }),
    )
    .await;
    assert_eq!(report(&mut s, "chunk").await.text, "writing");
    cancel_request(&s, "chunk").await;
    assert_eq!(cancelled_reply(&mut s, "chunk").await, mode.message());
    s.send(
        "abort",
        Host::AbortExport(pb::AbortExport {
            handle: opened.handle,
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ExportDone(_))));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn request_cancelled_before_handler_starts_is_not_run_and_deposit_is_released() {
    let writes = Arc::new(AtomicUsize::new(0));
    let count = writes.clone();
    let mut s = Session::new(
        Registry::new().exporter("raw", move |_, _| Ok(Arc::new(TestExporter(count.clone())))),
    )
    .await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1, 1, 1, 1, 5],
            dtype: 1,
            ..Default::default()
        }),
    )
    .await;
    let Some(Plugin::ExportOpened(opened)) = s.recv().await.msg else {
        panic!()
    };
    let mut install = install(7);
    install.purpose = "X".into();
    s.send(
        "held",
        Host::ExportChunk(pb::ExportChunk {
            handle: opened.handle,
            channels: vec![0],
            install: Some(install),
            source: Some(pb::export_chunk::Source::Segment(data(7).segment.unwrap())),
            ..Default::default()
        }),
    )
    .await;
    s.barrier().await;
    cancel_request(&s, "held").await;
    s.barrier().await;
    let _file = grant(&s, 7, 512);
    let (mut ack, mut released, mut cancelled) = (false, false, false);
    while !ack || !released || !cancelled {
        let reply = s.recv().await;
        match reply.msg {
            Some(Plugin::GrantAck(_)) => ack = true,
            Some(Plugin::ReleaseDeposit(d)) => {
                assert_eq!(d.segment_id, 7);
                released = true;
            }
            Some(Plugin::Error(e)) => {
                assert_eq!(reply.id, "held");
                assert_eq!(e.kind, pb::error::Kind::Cancelled as i32);
                cancelled = true;
            }
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(writes.load(Ordering::SeqCst), 0);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn background_task_begins_updates_cancels_and_ends_without_holding_request_slot() {
    let mut s = Session::new(Registry::new().command("run", |_, _| Ok(done()))).await;
    s.recv().await;
    let task = s.ctx().start_task(&"é".repeat(200)).unwrap();
    let Some(Plugin::WorkProgress(first)) = s.recv().await.msg else {
        panic!()
    };
    assert_eq!(
        first.subject,
        Some(pb::work_progress::Subject::TaskId(task.id().into()))
    );
    assert_eq!(first.title, "é".repeat(128));
    assert!(first.cancellable && first.progress.unwrap().indeterminate);
    task.progress(Some(0.5), "half").unwrap();
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::WorkProgress(p)) if p.progress.unwrap().value == 0.5)
    );
    invoke(&s, "free").await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    s.send(
        "",
        Host::CancelWork(pb::CancelWork {
            subject: Some(pb::cancel_work::Subject::TaskId(task.id().into())),
        }),
    )
    .await;
    s.barrier().await;
    assert!(task.cancelled());
    task.done("stopped early").unwrap();
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::WorkProgress(p)) if p.done && p.text == "stopped early")
    );
    task.progress(Some(1.), "late").unwrap();
    task.done("again").unwrap();
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn user_stopped_ends_tasks_and_runs_shutdown_while_command_ignores_cancel() {
    let (release, wait) = std::sync::mpsc::channel();
    let wait = Mutex::new(wait);
    let (stopped, received) = std::sync::mpsc::channel();
    let mut s = Session::new(
        Registry::new()
            .command("run", move |ctx, _| {
                ctx.report_progress(None, "ignoring cancel")?;
                wait.lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap();
                Ok(done())
            })
            .on_shutdown(move |_, notice| {
                stopped.send(notice.reason).unwrap();
                Ok(())
            }),
    )
    .await;
    s.recv().await;
    let task = s.ctx().start_task("background").unwrap();
    s.recv().await;
    invoke(&s, "slow").await;
    report(&mut s, "slow").await;
    cancel_request(&s, "slow").await;
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            reason: pb::ShutdownReason::UserStopped as i32,
            deadline_ms: Some(100),
            ..Default::default()
        }),
    )
    .await;
    assert_eq!(
        received.recv_timeout(Duration::from_secs(2)).unwrap(),
        pb::ShutdownReason::UserStopped as i32
    );
    let (mut ended, mut report) = (false, false);
    while !ended || !report {
        match s.recv().await.msg {
            Some(Plugin::WorkProgress(p)) => {
                assert!(p.done);
                ended = true;
            }
            Some(Plugin::ShutdownReport(_)) => report = true,
            other => panic!("{other:?}"),
        }
    }
    assert!(task.cancelled());
    assert!(s.ctx().start_task("too late").is_err());
    release.send(()).unwrap();
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn result_command_discards_after_reply_without_poisoning_mutex() {
    let calls = Arc::new(AtomicUsize::new(0));
    let called = calls.clone();
    let lock = Arc::new(Mutex::new(()));
    let guard_lock = lock.clone();
    let mut s = Session::new(Registry::new().command("run", move |ctx, _| {
        ctx.check_cancelled()?;
        work::check_cancelled()?;
        let called = called.clone();
        ctx.after_reply(move |_| {
            called.fetch_add(1, Ordering::SeqCst);
            Ok(())
        })?;
        let _guard = guard_lock.lock().unwrap();
        ctx.report_progress(None, "started")?;
        let until = std::time::Instant::now() + Duration::from_secs(5);
        while !ctx.cancelled() {
            assert!(std::time::Instant::now() < until);
            std::thread::sleep(Duration::from_millis(1));
        }
        ctx.check_cancelled()?;
        Ok(done())
    }))
    .await;
    s.recv().await;
    invoke(&s, "cancel").await;
    report(&mut s, "cancel").await;
    cancel_request(&s, "cancel").await;
    assert_eq!(cancelled_reply(&mut s, "cancel").await, "canceled");
    assert_eq!(calls.load(Ordering::SeqCst), 0);
    assert!(!lock.is_poisoned());
    s.barrier().await;
    s.ctx().check_cancelled().unwrap();
    work::check_cancelled().unwrap();
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn result_task_run_ends_as_cancelled() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let task = s.ctx().start_task("background").unwrap();
    task.check_cancelled().unwrap();
    s.recv().await;
    s.send(
        "",
        Host::CancelWork(pb::CancelWork {
            subject: Some(pb::cancel_work::Subject::TaskId(task.id().into())),
        }),
    )
    .await;
    s.barrier().await;
    let error = task
        .run(|task| {
            task.check_cancelled()?;
            Ok(())
        })
        .unwrap_err();
    assert_eq!(error.kind, pb::error::Kind::Cancelled);
    assert_eq!(error.message, "canceled");
    assert!(matches!(s.recv().await.msg, Some(Plugin::WorkProgress(p))
        if p.done && p.text == "canceled"));
    // A typed cancellation also works without a host cancellation flag.
    let task = s.ctx().start_task("typed cancellation").unwrap();
    s.recv().await;
    let error = task
        .run(|_| -> control::Result<()> {
            Err(work::Cancelled::new("stopped early"))?;
            Ok(())
        })
        .unwrap_err();
    assert_eq!(error.kind, pb::error::Kind::Cancelled);
    assert_eq!(error.message, "stopped early");
    assert!(matches!(s.recv().await.msg, Some(Plugin::WorkProgress(p))
        if p.done && p.text == "canceled"));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn result_task_with_cancel_ui_disabled_accepts_and_may_ignore_cancel() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let task = s
        .ctx()
        .start_task_with_cancellation("cannot stop", false)
        .unwrap();
    let subject = Some(pb::work_progress::Subject::TaskId(task.id().into()));
    assert!(matches!(s.recv().await.msg, Some(Plugin::WorkProgress(p))
        if p.subject == subject && !p.cancellable && !p.done));
    assert!(!task.cancelled());
    s.send(
        "",
        Host::CancelWork(pb::CancelWork {
            subject: Some(pb::cancel_work::Subject::TaskId(task.id().into())),
        }),
    )
    .await;
    // No refusal, cancellation reply, or contract report may precede the barrier.
    // The subprocess wrapper below also checks this case's stderr is empty.
    s.barrier().await;
    assert!(task.cancelled());
    assert_eq!(
        task.check_cancelled().unwrap_err().kind,
        pb::error::Kind::Cancelled
    );
    // The flag is advisory: application work can still finish successfully.
    let value = task
        .run(|task| {
            task.progress(Some(0.5), "continuing")?;
            Ok(42)
        })
        .unwrap();
    assert_eq!(value, 42);
    loop {
        let Some(Plugin::WorkProgress(p)) = s.recv().await.msg else {
            panic!()
        };
        assert_eq!(p.subject, subject);
        assert!(!p.cancellable);
        if p.done {
            assert!(p.text.is_empty());
            break;
        }
        assert_eq!(p.text, "continuing");
    }
    s.barrier().await;
    s.stop().await;
}

#[test]
fn cancellation_boundaries_emit_no_stderr_diagnostic() {
    // Capture real SDK and panic-hook diagnostics, in addition to each test's
    // wire barrier. Filter excludes this wrapper to avoid recursive execution.
    for filter in ["work_cases::result_", "work_cases::unwinding_"] {
        let output = std::process::Command::new(std::env::current_exe().unwrap())
            .args([filter, "--nocapture"])
            .output()
            .unwrap();
        assert!(
            output.status.success(),
            "{}\n{}",
            String::from_utf8_lossy(&output.stdout),
            String::from_utf8_lossy(&output.stderr)
        );
        assert!(
            output.stderr.is_empty(),
            "{}",
            String::from_utf8_lossy(&output.stderr)
        );
    }
}
