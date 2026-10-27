//! Solicited overlay state. Membership is reconciled by the dispatcher; only
//! computation and deferred publication run on the per-overlay worker.
use super::*;
use crate::plugin::Volume;
use std::{collections::BTreeSet, sync::Condvar, thread::JoinHandle};

/// A bbox-tight label volume on the wanted level's lattice.
#[derive(Clone, Debug)]
pub struct Chunk {
    pub labels: Volume,
    pub position_zyx: [u32; 3],
}
pub type ChunkSource = Arc<dyn Fn(&Context, &pb::ChunkWant) -> Result<Option<Chunk>> + Send + Sync>;

/// Overlay-wide label properties; the colormap is sent inline, keyed separately
/// from chunks. Names and visibility use label ids, not colormap row numbers.
#[derive(Clone, Debug)]
pub struct LabelTable {
    pub colormap: [[u8; 4]; 256],
    pub label_names: BTreeMap<u32, String>,
    pub label_visible: BTreeMap<u32, bool>,
    pub style: pb::OverlayStyle,
}

type Key = (u32, Option<u32>);
fn key(w: &pb::ChunkWant) -> Key {
    (w.level, w.t)
}

#[derive(Default)]
struct State {
    wants: Vec<pb::ChunkWant>,
    // Each entry gets its own ticket. Leaving and returning while a source is
    // computing must discard the old answer, even if the newest set is equal.
    tickets: BTreeMap<Key, u64>,
    next: u64,
    served: BTreeSet<Key>,
    deferred_segment: Option<u64>,
    stopped: bool,
}
impl State {
    fn restate(&mut self, wants: Vec<pb::ChunkWant>) {
        let keys: BTreeSet<_> = wants.iter().map(key).collect();
        self.served.retain(|k| keys.contains(k));
        self.tickets.retain(|k, _| keys.contains(k));
        for k in keys {
            self.tickets.entry(k).or_insert_with(|| {
                self.next += 1;
                self.next
            });
        }
        self.wants = wants;
    }
    fn current(&self, k: Key, ticket: u64) -> bool {
        !self.stopped && self.tickets.get(&k) == Some(&ticket)
    }
}
struct Overlay {
    state: Mutex<State>,
    wake: Condvar,
    source: Option<ChunkSource>,
}
pub(super) struct Chunks {
    overlays: BTreeMap<String, Arc<Overlay>>,
    workers: Mutex<Vec<JoinHandle<()>>>,
}
impl Chunks {
    pub fn new(
        manifest: &crate::manifest::Manifest,
        mut sources: BTreeMap<String, ChunkSource>,
    ) -> Result<Self> {
        let mut overlays = BTreeMap::new();
        if let Some(items) = manifest.value()["contributions"]["overlays"].as_array() {
            for item in items {
                if item["chunked"].as_bool() != Some(true) {
                    continue;
                }
                let id = item["id"].as_str().unwrap();
                overlays.insert(
                    id.to_owned(),
                    Arc::new(Overlay {
                        state: Mutex::new(State::default()),
                        wake: Condvar::new(),
                        source: sources.remove(id),
                    }),
                );
            }
        }
        if !sources.is_empty() {
            return Err(Error::protocol(
                "chunk source requires a declared chunked overlay",
            ));
        }
        Ok(Self {
            overlays,
            workers: Mutex::new(Vec::new()),
        })
    }
    fn overlay(&self, id: &str) -> Result<&Arc<Overlay>> {
        self.overlays
            .get(id)
            .ok_or_else(|| Error::new(pb::error::Kind::NotFound, "undeclared chunked overlay"))
    }
    pub fn start(&self, ctx: &Context) -> Result<()> {
        for (id, overlay) in &self.overlays {
            if let Some(source) = &overlay.source {
                let (ctx, id, overlay, source) =
                    (ctx.clone(), id.clone(), overlay.clone(), source.clone());
                let worker = std::thread::Builder::new()
                    .name(format!("vizi-chunks-{id}"))
                    .spawn(move || serve(ctx, id, overlay, source))?;
                self.workers.lock().unwrap().push(worker);
            }
        }
        Ok(())
    }
    pub fn wanted(&self, message: pb::ChunksWanted) {
        if message.wants.len() > 8 {
            eprintln!("vizi SDK: ChunksWanted exceeds 8 entries; host set ignored");
            return;
        }
        let Ok(overlay) = self.overlay(&message.overlay_id) else {
            eprintln!("vizi SDK: ChunksWanted names an undeclared chunked overlay");
            return;
        };
        let mut state = overlay.state.lock().unwrap();
        if !state.stopped {
            state.restate(message.wants);
            overlay.wake.notify_one();
        }
    }
    pub fn released(&self, segment: u64) {
        for overlay in self.overlays.values() {
            let state = overlay.state.lock().unwrap();
            if state.deferred_segment == Some(segment) {
                overlay.wake.notify_one();
            }
        }
    }
    pub fn stop(&self) {
        for overlay in self.overlays.values() {
            overlay.state.lock().unwrap().stopped = true;
            overlay.wake.notify_one();
        }
    }
    pub fn join(&self) {
        let workers = std::mem::take(&mut *self.workers.lock().unwrap());
        for worker in workers {
            if worker.join().is_err() {
                eprintln!("vizi SDK: chunk worker panicked");
            }
        }
    }
}

fn serve(ctx: Context, id: String, overlay: Arc<Overlay>, source: ChunkSource) {
    let mut state = overlay.state.lock().unwrap();
    loop {
        if state.stopped {
            return;
        }
        let want = state
            .wants
            .iter()
            .find(|w| !state.served.contains(&key(w)))
            .cloned();
        let Some(want) = want else {
            state = overlay.wake.wait(state).unwrap();
            continue;
        };
        let k = key(&want);
        let ticket = state.tickets[&k];
        drop(state);
        let answer = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| source(&ctx, &want)))
            .unwrap_or_else(|_| Err(Error::internal("chunk source panicked")));
        state = overlay.state.lock().unwrap();
        while state.current(k, ticket) {
            match &answer {
                Err(error) => {
                    if reported(ctx.is_shutting_down(), error) {
                        eprintln!("vizi SDK: chunk source {id} {k:?}: {error}");
                    }
                    state.served.insert(k);
                    break;
                }
                Ok(chunk) => match publish(&ctx, &id, &want, chunk.as_ref()) {
                    Ok(None) => {
                        state.served.insert(k);
                        break;
                    }
                    Ok(Some(segment)) => {
                        // Holding this lock through reserve and condvar wait
                        // makes a release racing the failed allocation visible.
                        state.deferred_segment = Some(segment);
                        state = overlay.wake.wait(state).unwrap();
                    }
                    Err(error) => {
                        if reported(ctx.is_shutting_down(), &error) {
                            eprintln!("vizi SDK: chunk publish {id} {k:?}: {error}");
                        }
                        state.served.insert(k);
                        break;
                    }
                },
            }
        }
        state.deferred_segment = None;
    }
}

/// Whether a source's or a publication's failure goes to stderr: not once the
/// session is shutting down, and never for a request that met the session's
/// end (§3.3, [`Error::is_session_ended`]) -- that is the host's doing, and the
/// source ends quietly, as a Follow handler does. The core marks itself
/// shutting down before it fails the pending requests, so the second test
/// backs up the first rather than replacing it.
fn reported(shutting_down: bool, error: &Error) -> bool {
    !shutting_down && !error.is_session_ended()
}

// Some(segment) means allocation must wait for that arena's ReleaseLoan.
fn publish(
    ctx: &Context,
    id: &str,
    want: &pb::ChunkWant,
    chunk: Option<&Chunk>,
) -> Result<Option<u64>> {
    let mut message = pb::OverlayChunk {
        overlay_id: id.into(),
        level: want.level,
        t: want.t,
        ..Default::default()
    };
    let mut loans = Vec::new();
    if let Some(chunk) = chunk {
        let volume = &chunk.labels;
        if !matches!(volume.dtype, pb::DType::DtypeU8 | pb::DType::DtypeU16) {
            return Err(Error::protocol("chunk labels must be u8 or u16"));
        }
        let mut descriptor = pb::SegmentRef {
            len: volume.bytes.len() as u64,
            shape_zyx: volume.shape_zyx.to_vec(),
            dtype: volume.dtype as i32,
            uses: 1,
            ..Default::default()
        };
        crate::memory::validate_descriptor(&descriptor)?;
        let Some(loan) = ctx
            .memory()
            .reserve_for_subject("O", id, &volume.bytes, false)?
        else {
            // A retired arena without a successor drops work quietly. A live
            // full arena waits for ReleaseLoan as before.
            return Ok(ctx
                .memory()
                .installed()
                .into_iter()
                .find(|i| i.purpose == "O" && i.subject == id)
                .map(|i| i.segment_id));
        };
        descriptor.segment_id = loan.segment_id;
        descriptor.offset = loan.offset;
        message.labels = Some(pb::GeomBuffer {
            dtype: descriptor.dtype,
            shape: volume.shape_zyx.to_vec(),
            source: Some(pb::geom_buffer::Source::Segment(descriptor)),
        });
        message.position_zyx = chunk.position_zyx.to_vec();
        loans.push(loan);
    }
    ctx.push_with_loans(Plugin::OverlayChunk(message), loans)?;
    Ok(None)
}

impl Context {
    /// Latest host set, in priority order; None and Some(0) are distinct times.
    pub fn wants(&self, overlay: &str) -> Result<Vec<pb::ChunkWant>> {
        Ok(self
            .core
            .chunks
            .overlay(overlay)?
            .state
            .lock()
            .unwrap()
            .wants
            .clone())
    }
    /// Recompute all current wants, discarding any in-flight or deferred answer.
    pub fn invalidate_chunks(&self, overlay: &str) -> Result<()> {
        let overlay = self.core.chunks.overlay(overlay)?;
        let mut state = overlay.state.lock().unwrap();
        state.served.clear();
        state.tickets.clear();
        let wants = state.wants.clone();
        state.restate(wants);
        overlay.wake.notify_one();
        Ok(())
    }
    /// Publish manually for a current want. Arena exhaustion returns an error
    /// without a shortfall; registered chunk sources handle the wait for you.
    pub fn publish_chunk(
        &self,
        overlay: &str,
        want: &pb::ChunkWant,
        chunk: Option<&Chunk>,
    ) -> Result<()> {
        let state = self.core.chunks.overlay(overlay)?.state.lock().unwrap();
        if state.stopped || !state.tickets.contains_key(&key(want)) {
            return Err(Error::protocol("chunk is no longer wanted"));
        }
        match publish(self, overlay, want, chunk)? {
            None => Ok(()),
            Some(_) => Err(Error::internal("chunk arena full; wait for ReleaseLoan")),
        }
    }
    pub fn set_label_table(&self, overlay: &str, table: &LabelTable) -> Result<()> {
        self.core.chunks.overlay(overlay)?;
        self.push(Plugin::OverlayLabelTable(pb::OverlayLabelTable {
            overlay_id: overlay.into(),
            colormap: Some(pb::GeomBuffer {
                dtype: pb::DType::DtypeU8 as i32,
                shape: vec![256, 4],
                source: Some(pb::geom_buffer::Source::Inline(table.colormap.concat())),
            }),
            label_names: table
                .label_names
                .iter()
                .map(|(&k, v)| (k, v.clone()))
                .collect(),
            label_visible: table.label_visible.iter().map(|(&k, &v)| (k, v)).collect(),
            style: Some(table.style.clone()),
        }))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_source_ending_with_the_session_is_not_reported() {
        let ended =
            Error::session_ended("the session ended: the host closed its side of the stream");
        assert!(!reported(false, &ended));
        assert!(!reported(true, &ended));
        let failed = Error::internal("chunk source panicked");
        assert!(reported(false, &failed));
        assert!(!reported(true, &failed));
    }

    #[test]
    fn every_set_changes_membership_even_when_worker_has_not_run() {
        let want = pb::ChunkWant {
            level: 0,
            t: Some(0),
        };
        let mut state = State::default();
        state.restate(vec![want]);
        let ticket = state.tickets[&(0, Some(0))];
        state.served.insert((0, Some(0)));
        state.restate(vec![]);
        state.restate(vec![want]);
        assert!(!state.current((0, Some(0)), ticket));
        assert!(state.served.is_empty());
        let ticket = state.tickets[&(0, Some(0))];
        state.restate(vec![pb::ChunkWant { level: 0, t: None }, want]);
        assert!(state.current((0, Some(0)), ticket));
        assert_ne!(state.tickets[&(0, None)], ticket);
    }
}
