//! Progress and cooperative cancellation for requests and background tasks (§8.16).
use super::{Context, Core, Error, Plugin, Result};
use crate::proto as pb;
use std::{
    any::Any,
    cell::RefCell,
    collections::BTreeMap,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex, Weak,
    },
};

pub const MAX_OPEN_TASKS: usize = 64;
pub const MAX_WORK_TEXT_BYTES: usize = 256;

pub fn clip_text(text: &str) -> String {
    let mut end = text.len().min(MAX_WORK_TEXT_BYTES);
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    text[..end].to_owned()
}
fn progress(fraction: Option<f64>) -> Option<pb::Progress> {
    Some(match fraction {
        None => pb::Progress {
            indeterminate: true,
            ..Default::default()
        },
        Some(value) => pb::Progress {
            value: if value.is_finite() {
                value.clamp(0., 1.)
            } else {
                0.
            },
            indeterminate: false,
        },
    })
}

/// A cancellation value convertible into [`Error`] for propagation with `?`.
/// Also recognized as the payload of [`cancel_unwinding`].
#[derive(Debug)]
pub struct Cancelled(String);

impl Cancelled {
    pub fn new(message: impl Into<String>) -> Self {
        Self(message.into())
    }
}
impl From<Cancelled> for Error {
    fn from(cancelled: Cancelled) -> Self {
        Self::cancelled(cancelled.0)
    }
}

/// Stop this handler by unwinding with a distinct [`Cancelled`] payload.
/// Prefer returning [`Error::cancelled`] or using [`check_cancelled`] with `?`.
///
/// Requires `panic = "unwind"`: bypasses ordinary Result adapters, runs Drops,
/// and does not invoke the panic hook. Unwinding through a mutex guard poisons
/// its mutex. Deliberate `catch_unwind` wrappers must resume this payload.
/// With `panic = "abort"`, prints an explanation to stderr and aborts the process.
pub fn cancel_unwinding(message: impl Into<String>) -> ! {
    if cfg!(panic = "abort") {
        use std::io::Write;
        let mut stderr = std::io::stderr().lock();
        let _ = writeln!(stderr, "vizi SDK: work::cancel_unwinding requires panic = \"unwind\"; this plugin uses panic = \"abort\"; aborting");
        let _ = stderr.flush();
        std::process::abort();
    }
    std::panic::resume_unwind(Box::new(Cancelled::new(message)))
}
pub(crate) fn unwind_error(payload: Box<dyn Any + Send>, fallback: &str) -> Error {
    match payload.downcast::<Cancelled>() {
        Ok(cancelled) => Error::from(*cancelled),
        Err(_) => Error::internal(fallback),
    }
}

pub(super) struct RequestWork {
    pub(super) id: String,
    core: Weak<Core>,
    cancelled: AtomicBool,
    ended: AtomicBool,
}
impl RequestWork {
    pub(super) fn cancelled(&self) -> bool {
        self.cancelled.load(Ordering::SeqCst)
    }
    fn report(&self, fraction: Option<f64>, text: &str) -> Result<()> {
        if self.ended.load(Ordering::SeqCst) {
            return Ok(());
        }
        if let Some(core) = self.core.upgrade() {
            core.send(
                String::new(),
                Plugin::WorkProgress(pb::WorkProgress {
                    subject: Some(pb::work_progress::Subject::RequestId(self.id.clone())),
                    progress: progress(fraction),
                    text: clip_text(text),
                    ..Default::default()
                }),
                4,
            )?;
        }
        Ok(())
    }
}

thread_local! {
    static CURRENT: RefCell<Option<Arc<RequestWork>>> = const { RefCell::new(None) };
}
pub(super) struct Current(Option<Arc<RequestWork>>);
impl Current {
    pub(super) fn enter(work: Arc<RequestWork>) -> Self {
        Self(CURRENT.with(|current| current.replace(Some(work))))
    }
}
impl Drop for Current {
    fn drop(&mut self) {
        CURRENT.with(|current| current.replace(self.0.take()));
    }
}
/// Report on the request served by this thread. Outside a handler, a no-op.
pub fn report_progress(fraction: Option<f64>, text: &str) -> Result<()> {
    let work = CURRENT.with(|current| current.borrow().clone());
    work.map_or(Ok(()), |work| work.report(fraction, text))
}
/// Whether the host cancelled this thread's request; false outside a handler.
pub fn cancelled() -> bool {
    CURRENT.with(|current| {
        current
            .borrow()
            .as_ref()
            .is_some_and(|work| work.cancelled())
    })
}
/// Return cancellation as an ordinary error; use `work::check_cancelled()?`.
/// Outside a request handler, returns `Ok(())`.
pub fn check_cancelled() -> Result<()> {
    if cancelled() {
        Err(Error::cancelled("cancelled"))
    } else {
        Ok(())
    }
}

#[derive(Default)]
pub(super) struct Registry {
    requests: BTreeMap<String, Arc<RequestWork>>,
    tasks: BTreeMap<String, Arc<Task>>,
    next_task: u64,
    closed: bool,
}

// This token follows a request through descriptor holds, the worker, and its
// completion. Every refusal/drop path retires it, including session loss.
pub(super) struct RequestRegistration {
    pub(super) work: Arc<RequestWork>,
}
impl Drop for RequestRegistration {
    fn drop(&mut self) {
        self.work.ended.store(true, Ordering::SeqCst);
        if let Some(core) = self.work.core.upgrade() {
            let mut registry = core.work.lock().unwrap();
            if registry
                .requests
                .get(&self.work.id)
                .is_some_and(|w| Arc::ptr_eq(w, &self.work))
            {
                registry.requests.remove(&self.work.id);
            }
        }
    }
}
pub(super) fn register_request(core: &Arc<Core>, id: &str) -> Result<RequestRegistration> {
    let mut registry = core.work.lock().unwrap();
    if registry.closed {
        return Err(Error::protocol("new request during shutdown"));
    }
    if registry.requests.contains_key(id) {
        return Err(Error::protocol(
            "host sent concurrent requests with the same id",
        ));
    }
    let work = Arc::new(RequestWork {
        id: id.into(),
        core: Arc::downgrade(core),
        cancelled: AtomicBool::new(false),
        ended: AtomicBool::new(false),
    });
    registry.requests.insert(id.into(), work.clone());
    Ok(RequestRegistration { work })
}
pub(super) fn cancel_subject(core: &Core, cancel: &pb::CancelWork) -> bool {
    let registry = core.work.lock().unwrap();
    match &cancel.subject {
        Some(pb::cancel_work::Subject::RequestId(id)) => registry
            .requests
            .get(id)
            .map(|work| {
                work.cancelled.store(true, Ordering::SeqCst);
            })
            .is_some(),
        Some(pb::cancel_work::Subject::TaskId(id)) => registry
            .tasks
            .get(id)
            .map(|task| {
                task.cancelled.store(true, Ordering::SeqCst);
            })
            .is_some(),
        None => false,
    }
}
pub(super) fn same_key(a: &pb::PluginMsg, b: &pb::PluginMsg) -> bool {
    matches!((&a.msg, &b.msg), (Some(Plugin::WorkProgress(a)), Some(Plugin::WorkProgress(b)))
        if a.subject.is_some() && a.subject == b.subject)
}

struct Task {
    id: String,
    title: String,
    cancellable: bool,
    core: Weak<Core>,
    cancelled: AtomicBool,
    // Serialize reports with the terminal report: no update may resurrect it.
    ended: Mutex<bool>,
}
impl Task {
    fn report(&self, fraction: Option<f64>, text: &str) -> Result<()> {
        let ended = self.ended.lock().unwrap();
        if *ended {
            return Ok(());
        }
        self.send(fraction, text, false)
    }
    fn send(&self, fraction: Option<f64>, text: &str, done: bool) -> Result<()> {
        if let Some(core) = self.core.upgrade() {
            core.send(
                String::new(),
                Plugin::WorkProgress(pb::WorkProgress {
                    subject: Some(pb::work_progress::Subject::TaskId(self.id.clone())),
                    title: self.title.clone(),
                    progress: if done { None } else { progress(fraction) },
                    text: clip_text(text),
                    done,
                    cancellable: !done && self.cancellable,
                }),
                4,
            )?;
        }
        Ok(())
    }
    fn end(&self, text: &str, force: bool) -> Result<()> {
        let mut ended = self.ended.lock().unwrap();
        if *ended {
            return Ok(());
        }
        let result = self.send(None, text, true);
        if result.is_ok() || force {
            *ended = true;
            if let Some(core) = self.core.upgrade() {
                core.work.lock().unwrap().tasks.remove(&self.id);
            }
        }
        result
    }
}

/// An owned background task. Moving it to a worker keeps it open. `done`
/// ends it explicitly; dropping it also ends it. Use `run` to record a Result's
/// outcome automatically, reporting a Cancelled error as `"cancelled"`.
pub struct TaskHandle {
    task: Arc<Task>,
}
impl TaskHandle {
    pub fn id(&self) -> &str {
        &self.task.id
    }
    pub fn cancelled(&self) -> bool {
        self.task.cancelled.load(Ordering::SeqCst)
    }
    /// Return cancellation as an ordinary error; use `task.check_cancelled()?`.
    pub fn check_cancelled(&self) -> Result<()> {
        if self.cancelled() {
            Err(Error::cancelled("cancelled"))
        } else {
            Ok(())
        }
    }
    pub fn progress(&self, fraction: Option<f64>, text: &str) -> Result<()> {
        self.task.report(fraction, text)
    }
    pub fn done(&self, message: &str) -> Result<()> {
        self.task.end(message, false)
    }
    pub fn run<T>(self, action: impl FnOnce(&Self) -> Result<T>) -> Result<T> {
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| action(&self))) {
            Ok(Ok(value)) => {
                self.done("")?;
                Ok(value)
            }
            Ok(Err(error)) => {
                let message = if error.kind == pb::error::Kind::Cancelled {
                    "cancelled".into()
                } else {
                    format!("failed: {error}")
                };
                let _ = self.done(&message);
                Err(error)
            }
            Err(payload) => {
                let message = if payload.is::<Cancelled>() {
                    "cancelled"
                } else {
                    "failed: task panicked"
                };
                let _ = self.done(message);
                std::panic::resume_unwind(payload)
            }
        }
    }
}
impl Drop for TaskHandle {
    fn drop(&mut self) {
        let message = if self.cancelled() {
            "cancelled"
        } else if std::thread::panicking() {
            "failed: task unwound"
        } else {
            ""
        };
        let _ = self.task.end(message, true);
    }
}
impl Context {
    /// Report on this context's request, even from a plugin-owned worker.
    pub fn report_progress(&self, fraction: Option<f64>, text: &str) -> Result<()> {
        self.work
            .as_ref()
            .map_or(Ok(()), |work| work.report(fraction, text))
    }
    pub fn cancelled(&self) -> bool {
        self.work.as_ref().is_some_and(|work| work.cancelled())
    }
    /// Return cancellation as an ordinary error; use `ctx.check_cancelled()?`.
    pub fn check_cancelled(&self) -> Result<()> {
        if self.cancelled() {
            Err(Error::cancelled("cancelled"))
        } else {
            Ok(())
        }
    }
    /// Begin a cancellable task, independent of the current request's slot.
    pub fn start_task(&self, title: &str) -> Result<TaskHandle> {
        self.start_task_with_cancellation(title, true)
    }
    /// `cancellable` advises the host's UI whether to offer Cancel. A CancelWork
    /// still sets the flag when false, without a fault; the task may ignore it.
    pub fn start_task_with_cancellation(
        &self,
        title: &str,
        cancellable: bool,
    ) -> Result<TaskHandle> {
        let mut registry = self.core.work.lock().unwrap();
        if registry.closed || self.is_shutting_down() {
            return Err(Error::protocol("session is closing"));
        }
        if registry.tasks.len() >= MAX_OPEN_TASKS {
            return Err(Error::internal(
                "64 background tasks are already open; end one first",
            ));
        }
        registry.next_task = registry
            .next_task
            .checked_add(1)
            .ok_or_else(|| Error::internal("task id space exhausted"))?;
        let task = Arc::new(Task {
            id: format!("t{}", registry.next_task),
            title: clip_text(title),
            cancellable,
            core: Arc::downgrade(&self.core),
            cancelled: AtomicBool::new(false),
            ended: Mutex::new(false),
        });
        registry.tasks.insert(task.id.clone(), task.clone());
        drop(registry);
        let handle = TaskHandle { task };
        handle.progress(None, "")?;
        Ok(handle)
    }
}
pub(super) fn end_session(core: &Core) {
    let tasks = {
        let mut registry = core.work.lock().unwrap();
        if registry.closed {
            return;
        }
        registry.closed = true;
        for work in registry.requests.values() {
            work.cancelled.store(true, Ordering::SeqCst);
            work.ended.store(true, Ordering::SeqCst);
        }
        std::mem::take(&mut registry.tasks)
    };
    for task in tasks.values() {
        task.cancelled.store(true, Ordering::SeqCst);
        let _ = task.end("session ended", true);
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use crate::{control::Runtime, manifest::Manifest, plugin, Doorbell};
    use std::{sync::mpsc, time::Duration};

    // Keep the observer independent of the session's locks and destructors.
    // In particular, neither thread::scope nor join may wait for a stuck worker
    // after this deadline has expired. The scenario runs once, not in a retry loop.
    fn bounded_scenario(
        timeout: Duration,
        scenario: impl FnOnce(&dyn Fn(&'static str)) + Send + 'static,
    ) {
        enum Event {
            Phase(&'static str),
            Finished(std::thread::Result<()>),
        }
        let (tx, rx) = mpsc::channel();
        let deadline = std::time::Instant::now() + timeout;
        std::thread::spawn(move || {
            let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                scenario(&|phase| {
                    let _ = tx.send(Event::Phase(phase));
                });
            }));
            // The scenario's locals, including its runtime, have been dropped.
            let _ = tx.send(Event::Finished(outcome));
        });
        let mut phase = "starting worker";
        loop {
            match rx.recv_timeout(deadline.saturating_duration_since(std::time::Instant::now())) {
                Ok(Event::Phase(next)) => phase = next,
                Ok(Event::Finished(Ok(()))) => return,
                Ok(Event::Finished(Err(payload))) => std::panic::resume_unwind(payload),
                Err(error) => panic!("task scope scenario did not finish within {timeout:?}; phase: {phase}; {error}"),
            }
        }
    }
    fn session() -> (Runtime, std::os::fd::OwnedFd, Context) {
        let manifest = Manifest::parse(r#"{"manifest_version":1,"id":"work.test","name":"Work","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#).unwrap();
        let (fd, peer) = crate::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime =
            Runtime::new(manifest, Arc::new(plugin::Registry::new()), bell, listener).unwrap();
        let ctx = runtime.context();
        *ctx.core.peer.lock().unwrap() = Some((1, 0));
        (runtime, peer, ctx)
    }
    fn reports(ctx: &Context) -> Vec<pb::WorkProgress> {
        let mut q = ctx.core.queues.lock().unwrap();
        let messages = std::mem::take(&mut q.work);
        q.bytes -= messages
            .iter()
            .map(prost::Message::encoded_len)
            .sum::<usize>();
        messages
            .into_iter()
            .map(|m| match m.msg {
                Some(Plugin::WorkProgress(p)) => p,
                _ => panic!(),
            })
            .collect()
    }
    #[test]
    fn task_scope_watchdog_bounds_a_gated_worker() {
        let (release, gate) = mpsc::channel::<()>();
        let (stopped, finished) = mpsc::channel();
        let failure = std::panic::catch_unwind(|| {
            bounded_scenario(Duration::from_millis(50), move |phase| {
                phase("waiting on test gate");
                let result = gate.recv_timeout(Duration::from_secs(5));
                let _ = stopped.send(result);
            });
        })
        .expect_err("the observer must time out while the worker is gated");
        // Only release the worker after the observer has returned. Joining the
        // worker on timeout would deadlock this ordering. Every gate is bounded.
        drop(release);
        assert_eq!(
            finished
                .recv_timeout(Duration::from_secs(5))
                .expect("watchdog worker did not exit after gate release"),
            Err(mpsc::RecvTimeoutError::Disconnected)
        );
        let message = failure.downcast_ref::<String>().unwrap();
        assert!(message.contains("task scope scenario did not finish within 50ms; phase:"));
        // If the worker was not scheduled within 50 ms, the startup phase is
        // also a correct diagnostic; the scenario's five-second bound covers it.
        assert!(message.contains("waiting on test gate") || message.contains("starting worker"));
    }
    #[test]
    fn task_limit_reuses_capacity_and_session_end_retires_retained_handles() {
        let (runtime, _peer, ctx) = session();
        let tasks: Vec<_> = (0..64).map(|_| ctx.start_task("task").unwrap()).collect();
        assert!(ctx
            .start_task("too many")
            .err()
            .unwrap()
            .message
            .contains("already open"));
        tasks[0].done("finished").unwrap();
        let next = ctx.start_task_with_cancellation("next", false).unwrap();
        assert_eq!(next.id(), "t65");
        let first = reports(&ctx);
        assert_eq!(first.len(), 65);
        assert!(first[0].done);
        assert!(!first.last().unwrap().cancellable);
        runtime.stop();
        assert!(next.cancelled());
        assert!(tasks[1..].iter().all(TaskHandle::cancelled));
        assert!(ctx.core.work.lock().unwrap().tasks.is_empty());
        assert!(ctx.start_task("ended").is_err());
        assert!(reports(&ctx).iter().all(|p| p.done));
        next.progress(Some(1.), "late").unwrap();
        next.done("late").unwrap();
        assert!(reports(&ctx).is_empty());
    }
    #[test]
    fn task_scope_records_success_failure_and_cancellation_outcomes() {
        bounded_scenario(Duration::from_secs(5), |phase| {
            phase("creating session");
            let (runtime, _peer, ctx) = session();
            phase("running successful task");
            ctx.start_task("success").unwrap().run(|_| Ok(())).unwrap();
            phase("collecting successful task report");
            assert_eq!(reports(&ctx)[0].text, "");
            phase("running failing task");
            let result: Result<()> = ctx
                .start_task("failure")
                .unwrap()
                .run(|_| Err(Error::internal("no convergence")));
            assert!(result.is_err());
            phase("collecting failing task report");
            let report = reports(&ctx).pop().unwrap();
            assert!(report.done);
            assert_eq!(report.text, "failed: no convergence");
            phase("running cancellation unwind");
            let signal = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                ctx.start_task("cancel").unwrap().run::<()>(|_| {
                    phase("unwinding cancellation and ending task");
                    cancel_unwinding("stop")
                })
            }))
            .unwrap_err();
            assert!(signal.is::<Cancelled>());
            phase("collecting cancelled task report");
            assert_eq!(reports(&ctx)[0].text, "cancelled");
            phase("starting dropped task");
            {
                let _task = ctx.start_task("drop").unwrap();
                phase("dropping task handle");
            }
            phase("collecting dropped task report");
            assert!(reports(&ctx)[0].done);
            phase("checking empty task registry");
            assert!(ctx.core.work.lock().unwrap().tasks.is_empty());
            phase("stopping runtime");
            runtime.stop();
            phase("dropping session");
        });
    }
    #[test]
    fn reports_coalesce_by_subject_type_and_id_and_terminal_report_wins() {
        let (runtime, _peer, ctx) = session();
        let registration = register_request(&ctx.core, "t1").unwrap();
        let task = ctx.start_task("task").unwrap();
        for i in 0..1000 {
            registration
                .work
                .report(Some(i as f64 / 1000.), &i.to_string())
                .unwrap();
            task.progress(None, &i.to_string()).unwrap();
        }
        task.done("finished").unwrap();
        task.progress(Some(1.), "too late").unwrap();
        let reports = reports(&ctx);
        assert_eq!(reports.len(), 2);
        assert!(reports.iter().any(|p| p.subject
            == Some(pb::work_progress::Subject::TaskId("t1".into()))
            && p.done
            && p.text == "finished"));
        assert!(reports.iter().any(|p| p.subject
            == Some(pb::work_progress::Subject::RequestId("t1".into()))
            && !p.done
            && p.text == "999"));
        assert_eq!(ctx.core.queues.lock().unwrap().bytes, 0);
        drop(registration);
        assert!(!cancel_subject(
            &ctx.core,
            &pb::CancelWork {
                subject: Some(pb::cancel_work::Subject::RequestId("t1".into()))
            }
        ));
        assert!(!cancel_subject(
            &ctx.core,
            &pb::CancelWork {
                subject: Some(pb::cancel_work::Subject::TaskId("t1".into()))
            }
        ));
        runtime.stop();
    }
    #[test]
    fn utf8_clipping_and_fraction_normalization_also_apply_to_raw_pushes() {
        assert_eq!(clip_text(&format!("{}€", "x".repeat(255))), "x".repeat(255));
        assert_eq!(clip_text(&"🦀".repeat(65)), "🦀".repeat(64));
        for (value, expected) in [(-1., 0.), (2., 1.), (f64::NAN, 0.), (f64::INFINITY, 0.)] {
            assert_eq!(progress(Some(value)).unwrap().value, expected);
        }
        let (runtime, _peer, ctx) = session();
        ctx.push(Plugin::WorkProgress(pb::WorkProgress {
            subject: Some(pb::work_progress::Subject::TaskId("raw".into())),
            title: "é".repeat(200),
            text: "€".repeat(200),
            ..Default::default()
        }))
        .unwrap();
        let report = reports(&ctx).pop().unwrap();
        assert_eq!(report.title.len(), 256);
        assert_eq!(report.text.len(), 255);
        runtime.stop();
    }
    #[test]
    fn task_progress_racing_done_cannot_resurrect_the_task() {
        let (runtime, _peer, ctx) = session();
        let task = ctx.start_task("race").unwrap();
        std::thread::scope(|scope| {
            scope.spawn(|| {
                for _ in 0..1000 {
                    task.progress(Some(0.5), "working").unwrap();
                }
            });
            task.done("finished").unwrap();
        });
        let report = reports(&ctx).pop().unwrap();
        assert!(report.done);
        assert_eq!(report.text, "finished");
        runtime.stop();
    }
}
