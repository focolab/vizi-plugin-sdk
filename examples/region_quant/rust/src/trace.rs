//! Background traces. No host read holds the cache lock or runs in a callback.
use crate::{
    app::measure,
    table::{self, Measure, Row},
};
use std::{
    collections::BTreeMap,
    sync::{Arc, Condvar, Mutex},
    thread::JoinHandle,
    time::{Duration, Instant},
};
use vizi::{
    control::{Context, Error, Result},
    proto as pb,
};

struct Trace {
    name: String,
    revision: Arc<()>,
    values: Vec<Measure>,
    total: Option<u32>,
    best: Option<u32>,
    ready: bool,
    abandoned: bool,
    retry_at: Option<Instant>,
    retry_delay: Duration,
}
impl Trace {
    fn new(name: String, ready: bool) -> Self {
        Self {
            name,
            revision: Arc::new(()),
            values: Vec::new(),
            total: None,
            best: None,
            ready,
            abandoned: false,
            retry_at: None,
            retry_delay: Duration::from_millis(100),
        }
    }
    fn retry(&mut self) {
        self.values.clear();
        self.best = None;
        self.retry_at = Some(Instant::now() + self.retry_delay);
        self.retry_delay = (self.retry_delay * 2).min(Duration::from_secs(5));
    }
    fn complete(&self) -> bool {
        self.total.is_some_and(|n| self.values.len() == n as usize)
    }
}
#[derive(Default)]
struct Book {
    rows: BTreeMap<String, Trace>,
    c: u32,
    selected: String,
    frames: u32,
    generation: u64,
    float: bool,
    running: Option<Job>,
    stopped: bool,
}
#[derive(Clone)]
struct Job {
    generation: u64,
    id: String,
    revision: Arc<()>,
    c: u32,
}
impl Book {
    fn current(&self, job: &Job) -> bool {
        !self.stopped
            && self.generation == job.generation
            && self.c == job.c
            && self
                .rows
                .get(&job.id)
                .is_some_and(|r| Arc::ptr_eq(&r.revision, &job.revision))
    }
    fn next(&mut self) -> Option<Job> {
        let (id, row) = self.rows.iter().find(|(_, r)| {
            r.ready
                && !r.abandoned
                && !r.complete()
                && r.retry_at.is_none_or(|at| at <= Instant::now())
        })?;
        let job = Job {
            generation: self.generation,
            id: id.clone(),
            revision: row.revision.clone(),
            c: self.c,
        };
        self.running = Some(job.clone());
        Some(job)
    }
    fn progress(&self) -> String {
        let active = self
            .running
            .as_ref()
            .filter(|j| self.current(j))
            .and_then(|j| self.rows.get(&j.id))
            .filter(|r| !r.complete() && !r.abandoned);
        active
            .or_else(|| self.rows.values().find(|r| !r.complete() && !r.abandoned))
            .map(|r| {
                format!(
                    "Measuring {}: {} of {} frames",
                    r.name,
                    r.values.len(),
                    r.total.unwrap_or(self.frames)
                )
            })
            .unwrap_or_default()
    }
}
pub struct Selection {
    pub name: String,
    pub progress: Option<(usize, u32)>,
    pub best: Option<u32>,
}
#[derive(Default)]
pub struct Traces {
    book: Mutex<Book>,
    wake: Condvar,
    worker: Mutex<Option<JoinHandle<()>>>,
}
// A queued trace may reach the host before the new-generation push gets the
// model lock. Check the host's current generation and dtype before any reads.
fn trace_frames(view: &pb::ViewState, generation: u64) -> Result<u32> {
    if view.open_generation != generation || view.dtype == pb::DType::DtypeF32 as i32 {
        return Err(Error::new(
            pb::error::Kind::NotFound,
            "trace dataset changed",
        ));
    }
    view.shape_tczyx
        .first()
        .copied()
        .ok_or_else(|| Error::protocol("view has no dataset shape"))
}
trait TraceHost {
    fn is_shutting_down(&self) -> bool;
    fn frames(&self, generation: u64) -> Result<u32>;
    fn measure(&self, id: &str, t: u32, c: u32, generation: u64) -> Result<Measure>;
    fn progress(&self, text: String) -> Result<()>;
}
impl TraceHost for Context {
    fn is_shutting_down(&self) -> bool {
        Context::is_shutting_down(self)
    }
    fn frames(&self, generation: u64) -> Result<u32> {
        trace_frames(&self.view_state()?, generation)
    }
    fn measure(&self, id: &str, t: u32, c: u32, generation: u64) -> Result<Measure> {
        measure(self, id, t, c, generation)
    }
    fn progress(&self, text: String) -> Result<()> {
        self.panel("quant").set_value("tracing", text)
    }
}
impl Traces {
    pub fn start(self: &Arc<Self>, ctx: Context) -> std::io::Result<()> {
        let mut worker = self.worker.lock().unwrap();
        let traces = self.clone();
        *worker = Some(
            std::thread::Builder::new()
                .name("region-quant-traces".into())
                .spawn(move || traces.run(&ctx))?,
        );
        Ok(())
    }
    pub fn stop(&self) {
        self.book.lock().unwrap().stopped = true;
        self.wake.notify_all();
    }
    pub fn stop_and_join(&self) -> Result<()> {
        self.stop();
        // Serialize the shutdown hook and main's fallback cleanup. The trace
        // worker never takes this mutex, and host calls wake on SDK shutdown.
        let mut worker = self.worker.lock().unwrap();
        if let Some(worker) = worker.take() {
            worker
                .join()
                .map_err(|_| Error::internal("trace worker panicked"))?;
        }
        Ok(())
    }
    pub fn select(&self, id: &str) {
        self.book.lock().unwrap().selected = id.into();
    }
    pub fn selection(&self) -> Option<Selection> {
        let book = self.book.lock().unwrap();
        let row = book.rows.get(&book.selected)?;
        Some(Selection {
            name: row.name.clone(),
            progress: (!row.complete())
                .then_some((row.values.len(), row.total.unwrap_or(book.frames))),
            best: row.best,
        })
    }
    // Invalidate before the table's slow reads. Keep pending entries but do not
    // start them until updated row metadata/channel has been committed by sync.
    pub fn invalidate(&self, ctx: &Context, change: &vizi::proto::RegionsChanged) -> Result<()> {
        let mut book = self.book.lock().unwrap();
        if change.reset {
            book.rows.clear();
        }
        for changed in &change.changes {
            if changed.deleted {
                book.rows.remove(&changed.region_id);
            } else if let Some(row) = book.rows.get_mut(&changed.region_id) {
                *row = Trace::new(row.name.clone(), false);
            }
        }
        ctx.progress(book.progress())
    }
    pub fn dataset(
        &self,
        ctx: &Context,
        generation: u64,
        frames: u32,
        c: u32,
        float: bool,
    ) -> Result<()> {
        let mut book = self.book.lock().unwrap();
        book.rows.clear();
        book.frames = frames;
        book.generation = generation;
        book.c = c;
        book.float = float;
        ctx.progress(String::new())?;
        self.wake.notify_all();
        Ok(())
    }
    pub fn channel(&self, ctx: &Context, c: u32) -> Result<()> {
        let mut book = self.book.lock().unwrap();
        if book.c != c {
            book.c = c;
            for row in book.rows.values_mut() {
                *row = Trace::new(row.name.clone(), false);
            }
        }
        ctx.progress(book.progress())
    }
    pub fn sync<'a>(
        &self,
        ctx: &Context,
        rows: impl Iterator<Item = &'a Row>,
        selected: &str,
    ) -> Result<()> {
        let mut book = self.book.lock().unwrap();
        if book.float {
            book.rows.clear();
            return ctx.progress(String::new());
        }
        let rows: BTreeMap<_, _> = rows
            .map(|r| (r.id.clone(), r.display_name().to_owned()))
            .collect();
        book.rows.retain(|id, _| rows.contains_key(id));
        for (id, name) in rows {
            let row = book
                .rows
                .entry(id)
                .or_insert_with(|| Trace::new(name.clone(), true));
            if !row.ready {
                *row = Trace::new(name, true);
            }
        }
        book.selected = selected.into();
        ctx.progress(book.progress())?;
        self.wake.notify_one();
        Ok(())
    }
    fn publish(host: &impl TraceHost, book: &Book) {
        if book.stopped || host.is_shutting_down() {
            return;
        }
        if let Err(error) = host.progress(book.progress()) {
            if !host.is_shutting_down() {
                eprintln!("trace progress: {error}");
            }
        }
    }
    fn run(&self, host: &impl TraceHost) {
        loop {
            let job = {
                let mut book = self.book.lock().unwrap();
                loop {
                    if book.stopped || host.is_shutting_down() {
                        return;
                    }
                    if let Some(job) = book.next() {
                        break job;
                    }
                    let delay = book
                        .rows
                        .values()
                        .filter_map(|r| r.retry_at)
                        .map(|at| at.saturating_duration_since(Instant::now()))
                        .min()
                        .unwrap_or(Duration::from_secs(1));
                    book = self.wake.wait_timeout(book, delay).unwrap().0;
                }
            };
            // Every read, including T, runs outside the cache lock.
            let result = self.trace_one(host, &job);
            let mut book = self.book.lock().unwrap();
            book.running = None;
            // The SDK wakes a blocked read before the application shutdown
            // hook necessarily runs. Do not diagnose or retry that cancellation.
            if book.stopped || host.is_shutting_down() {
                return;
            }
            if book.current(&job) {
                let row = book.rows.get_mut(&job.id).unwrap();
                match result {
                    Err(error) if error.kind == vizi::proto::error::Kind::NotFound => {
                        row.abandoned = true;
                        row.retry_at = None;
                    }
                    Err(error) => {
                        row.retry();
                        eprintln!("trace {} will retry: {error}", job.id);
                    }
                    Ok(()) => {
                        row.retry_at = None;
                        row.retry_delay = Duration::from_millis(100);
                    }
                }
            }
            Self::publish(host, &book);
        }
    }
    fn trace_one(&self, host: &impl TraceHost, job: &Job) -> Result<()> {
        if host.is_shutting_down() || !self.book.lock().unwrap().current(job) {
            return Ok(());
        }
        let total = host.frames(job.generation)?;
        {
            let mut book = self.book.lock().unwrap();
            if host.is_shutting_down() || !book.current(job) {
                return Ok(());
            }
            book.frames = total;
            book.rows.get_mut(&job.id).unwrap().total = Some(total);
            Self::publish(host, &book);
        }
        for t in 0..total {
            if host.is_shutting_down() || !self.book.lock().unwrap().current(job) {
                return Ok(());
            }
            let value = host.measure(&job.id, t, job.c, job.generation)?;
            let mut book = self.book.lock().unwrap();
            if host.is_shutting_down() || !book.current(job) {
                return Ok(());
            }
            let row = book.rows.get_mut(&job.id).unwrap();
            row.values.push(value);
            // Compare only the previous winner and this frame, keeping even
            // very long traces from making the command's snapshot lock slow.
            row.best = table::best_frame(
                row.best
                    .map(|best| (best, row.values[best as usize]))
                    .into_iter()
                    .chain(std::iter::once((t, value))),
            );
            Self::publish(host, &book);
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        sync::{
            atomic::{AtomicBool, Ordering},
            mpsc,
        },
        time::Duration,
    };

    struct Read {
        id: String,
        t: u32,
        c: u32,
        reply: mpsc::SyncSender<Result<Measure>>,
    }
    struct Host {
        view: pb::ViewState,
        reads: mpsc::Sender<Read>,
        progress: mpsc::Sender<String>,
        shutdown: Arc<AtomicBool>,
    }
    impl TraceHost for Host {
        fn is_shutting_down(&self) -> bool {
            self.shutdown.load(Ordering::SeqCst)
        }
        fn frames(&self, generation: u64) -> Result<u32> {
            trace_frames(&self.view, generation)
        }
        fn measure(&self, id: &str, t: u32, c: u32, _generation: u64) -> Result<Measure> {
            let (reply, result) = mpsc::sync_channel(1);
            self.reads
                .send(Read {
                    id: id.into(),
                    t,
                    c,
                    reply,
                })
                .unwrap();
            result
                .recv_timeout(Duration::from_secs(5))
                .expect("test read was not released")
        }
        fn progress(&self, text: String) -> Result<()> {
            self.progress.send(text).unwrap();
            Ok(())
        }
    }
    type Running = (
        Arc<Traces>,
        JoinHandle<()>,
        mpsc::Receiver<Read>,
        mpsc::Receiver<String>,
        Arc<AtomicBool>,
    );
    fn running(ids: &[&str]) -> Running {
        running_dataset(
            ids,
            pb::ViewState {
                dtype: pb::DType::DtypeU16 as i32,
                shape_tczyx: vec![2, 1, 1, 1, 1],
                ..Default::default()
            },
        )
    }
    fn running_dataset(ids: &[&str], view: pb::ViewState) -> Running {
        let traces = Arc::new(Traces::default());
        for id in ids {
            traces
                .book
                .lock()
                .unwrap()
                .rows
                .insert((*id).into(), Trace::new((*id).into(), true));
        }
        traces.select(ids[0]);
        let (send, reads) = mpsc::channel();
        let (progress, labels) = mpsc::channel();
        let copy = traces.clone();
        let shutdown = Arc::new(AtomicBool::new(false));
        let host_shutdown = shutdown.clone();
        let worker = std::thread::spawn(move || {
            copy.run(&Host {
                view,
                reads: send,
                progress,
                shutdown: host_shutdown,
            })
        });
        (traces, worker, reads, labels, shutdown)
    }
    fn read(reads: &mpsc::Receiver<Read>, id: &str, t: u32, c: u32) -> Read {
        let read = reads.recv_timeout(Duration::from_secs(5)).unwrap();
        assert_eq!((read.id.as_str(), read.t, read.c), (id, t, c));
        read
    }
    fn idle(labels: &mpsc::Receiver<String>) {
        while !labels
            .recv_timeout(Duration::from_secs(5))
            .unwrap()
            .is_empty()
        {}
    }
    #[test]
    fn slow_read_does_not_block_snapshot_and_channel_restart_discards_old_result() {
        let (traces, worker, reads, labels, _) = running(&["a"]);
        let old = read(&reads, "a", 0, 0);
        // The read is parked: taking a brightest snapshot must not wait for it.
        let snapshot = traces.selection().unwrap();
        assert_eq!(snapshot.progress, Some((0, 2)));
        {
            let mut book = traces.book.lock().unwrap();
            book.c = 1;
            book.rows
                .insert("a".into(), Trace::new("renamed".into(), true));
        }
        old.reply.send(Ok(Measure { count: 1, sum: 999 })).unwrap();
        read(&reads, "a", 0, 1)
            .reply
            .send(Ok(Measure { count: 1, sum: 2 }))
            .unwrap();
        let last = read(&reads, "a", 1, 1);
        assert_eq!(traces.selection().unwrap().progress, Some((1, 2)));
        last.reply.send(Ok(Measure { count: 1, sum: 3 })).unwrap();
        idle(&labels);
        let snapshot = traces.selection().unwrap();
        assert_eq!(snapshot.name, "renamed");
        assert_eq!(snapshot.progress, None);
        assert_eq!(snapshot.best, Some(1));
        assert_eq!(traces.book.lock().unwrap().rows["a"].values.len(), 2);
        traces.stop();
        worker.join().unwrap();
    }
    #[test]
    fn missing_region_abandons_one_trace_and_worker_finishes_the_next() {
        let (traces, worker, reads, labels, _) = running(&["a", "b"]);
        read(&reads, "a", 0, 0)
            .reply
            .send(Ok(Measure { count: 1, sum: 100 }))
            .unwrap();
        read(&reads, "a", 1, 0)
            .reply
            .send(Err(Error::new(
                vizi::proto::error::Kind::NotFound,
                "unknown region",
            )))
            .unwrap();
        let b = read(&reads, "b", 0, 0);
        assert!(traces.book.lock().unwrap().rows["a"].abandoned);
        assert_eq!(traces.selection().unwrap().progress, Some((1, 2)));
        traces.book.lock().unwrap().rows.remove("a");
        traces.select("b");
        b.reply.send(Ok(Measure::default())).unwrap();
        read(&reads, "b", 1, 0)
            .reply
            .send(Ok(Measure::default()))
            .unwrap();
        idle(&labels);
        let snapshot = traces.selection().unwrap();
        assert!(snapshot.progress.is_none());
        assert!(snapshot.best.is_none());
        traces.stop();
        worker.join().unwrap();
    }
    #[test]
    fn redraw_and_delete_recreate_restart_at_zero_even_on_the_same_channel() {
        let (traces, worker, reads, labels, _) = running(&["a"]);
        read(&reads, "a", 0, 0)
            .reply
            .send(Ok(Measure { count: 1, sum: 100 }))
            .unwrap();
        let old = read(&reads, "a", 1, 0);
        traces
            .book
            .lock()
            .unwrap()
            .rows
            .insert("a".into(), Trace::new("redrawn".into(), true));
        old.reply.send(Ok(Measure { count: 1, sum: 100 })).unwrap();
        let redrawn = read(&reads, "a", 0, 0);
        {
            let mut book = traces.book.lock().unwrap();
            book.rows.clear();
            book.rows
                .insert("a".into(), Trace::new("recreated".into(), true));
        }
        redrawn
            .reply
            .send(Ok(Measure { count: 1, sum: 100 }))
            .unwrap();
        for t in 0..2 {
            read(&reads, "a", t, 0)
                .reply
                .send(Ok(Measure { count: 1, sum: 2 }))
                .unwrap();
        }
        idle(&labels);
        let snapshot = traces.selection().unwrap();
        assert_eq!(snapshot.name, "recreated");
        assert_eq!(snapshot.best, Some(0));
        assert_eq!(snapshot.progress, None);
        traces.stop();
        worker.join().unwrap();
    }

    #[test]
    fn trace_does_not_read_a_replaced_or_float_dataset_before_push_handlers_catch_up() {
        for (generation, dtype) in [(1, pb::DType::DtypeU16), (0, pb::DType::DtypeF32)] {
            let (traces, worker, reads, labels, _) = running_dataset(
                &["a"],
                pb::ViewState {
                    open_generation: generation,
                    dtype: dtype as i32,
                    shape_tczyx: vec![2, 1, 1, 1, 1],
                    ..Default::default()
                },
            );
            idle(&labels);
            assert!(traces.book.lock().unwrap().rows["a"].abandoned);
            assert!(reads.try_recv().is_err());
            traces.stop();
            worker.join().unwrap();
        }
    }

    #[test]
    fn transient_failure_retries_from_zero_after_backoff_and_success_resets_it() {
        let (traces, worker, reads, labels, _) = running(&["a"]);
        read(&reads, "a", 0, 0)
            .reply
            .send(Ok(Measure { count: 1, sum: 99 }))
            .unwrap();
        let failed = read(&reads, "a", 1, 0);
        let start = Instant::now();
        failed
            .reply
            .send(Err(Error::internal("temporary")))
            .unwrap();
        let retry = read(&reads, "a", 0, 0);
        assert!(start.elapsed() >= Duration::from_millis(100));
        assert_eq!(traces.selection().unwrap().progress, Some((0, 2)));
        retry.reply.send(Ok(Measure { count: 1, sum: 1 })).unwrap();
        read(&reads, "a", 1, 0)
            .reply
            .send(Ok(Measure { count: 1, sum: 2 }))
            .unwrap();
        idle(&labels);
        assert_eq!(traces.selection().unwrap().best, Some(1));
        assert_eq!(
            traces.book.lock().unwrap().rows["a"].retry_delay,
            Duration::from_millis(100)
        );
        traces.stop();
        worker.join().unwrap();
    }

    #[test]
    fn retry_doubles_to_five_seconds_and_can_be_cancelled_during_the_wait() {
        let mut row = Trace::new("a".into(), true);
        for millis in [100, 200, 400, 800, 1600, 3200, 5000, 5000] {
            let start = Instant::now();
            row.retry();
            let delay = row.retry_at.unwrap().duration_since(start);
            assert!(delay >= Duration::from_millis(millis));
            assert!(delay < Duration::from_millis(millis + 100));
        }
        let (traces, worker, reads, labels, _) = running(&["a"]);
        let failed = read(&reads, "a", 0, 0);
        traces
            .book
            .lock()
            .unwrap()
            .rows
            .get_mut("a")
            .unwrap()
            .retry_delay = Duration::from_secs(5);
        failed
            .reply
            .send(Err(Error::internal("temporary")))
            .unwrap();
        while traces.book.lock().unwrap().rows["a"].retry_at.is_none() {
            labels.recv_timeout(Duration::from_secs(1)).unwrap();
        }
        let start = Instant::now();
        traces.stop();
        worker.join().unwrap();
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(reads.try_recv().is_err());
    }

    #[test]
    fn shutdown_during_read_exits_without_retrying_or_publishing() {
        // SDK shutdown can wake the read before the hook gets CPU time. Also
        // cover an explicit local stop and a successful read arriving late.
        for sdk_shutdown in [false, true] {
            for successful_reply in [false, true] {
                let (traces, worker, reads, labels, shutdown) = running(&["a", "b"]);
                let blocked = read(&reads, "a", 0, 0);
                while labels.try_recv().is_ok() {}
                if sdk_shutdown {
                    shutdown.store(true, Ordering::SeqCst);
                } else {
                    traces.stop();
                }
                blocked
                    .reply
                    .send(if successful_reply {
                        Ok(Measure { count: 1, sum: 100 })
                    } else {
                        Err(Error::internal("session is shutting down"))
                    })
                    .unwrap();
                worker.join().unwrap();
                assert!(matches!(
                    reads.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                ));
                assert!(matches!(
                    labels.try_recv(),
                    Err(mpsc::TryRecvError::Disconnected)
                ));
                let book = traces.book.lock().unwrap();
                assert!(book.running.is_none());
                assert!(!book.rows["a"].abandoned);
                assert!(book.rows.values().all(|r| r.values.is_empty()));
            }
        }
    }

    #[test]
    fn shutdown_wakes_and_joins_idle_worker_and_cleanup_is_repeatable() {
        let (traces, worker, reads, labels, _) = running(&["a"]);
        *traces.worker.lock().unwrap() = Some(worker);
        for t in 0..2 {
            read(&reads, "a", t, 0)
                .reply
                .send(Ok(Measure::default()))
                .unwrap();
        }
        idle(&labels);
        traces.stop_and_join().unwrap();
        traces.stop_and_join().unwrap();
        assert!(traces.worker.lock().unwrap().is_none());
        assert!(matches!(
            reads.try_recv(),
            Err(mpsc::TryRecvError::Disconnected)
        ));
    }
}
