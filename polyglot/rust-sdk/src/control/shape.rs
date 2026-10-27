//! Plugin-computed region shapes (§8.8). The SDK owns correlation and sending.
use super::{Context, Core, Error, Plugin, Result};
use crate::{manifest::Manifest, proto as pb};
use pb::pois_math::v1::{Resolution, Vec3};
use prost::Message;
use std::{
    collections::BTreeMap,
    sync::{Arc, Mutex},
};

/// A computation may overlap another drag or its own commit. Every edit carries
/// its full rest state. Return an error to reject that edit with its message.
pub type Shaper = Arc<dyn Fn(&Context, &pb::ShapeEdit) -> Result<Shape> + Send + Sync>;

/// Positions are in the input ROI's rest coordinates, one per vertex in order.
/// The SDK validates cardinality and finiteness before sending.
#[derive(Debug, Clone)]
pub struct Shape {
    pub positions: Vec<Vec3>,
    pub resolution: Resolution,
    pub message: String,
}
impl Shape {
    pub fn ok(positions: Vec<Vec3>) -> Self {
        Self {
            positions,
            resolution: Resolution::Ok,
            message: String::new(),
        }
    }
    pub fn clamped(positions: Vec<Vec3>) -> Self {
        Self {
            resolution: Resolution::Clamped,
            ..Self::ok(positions)
        }
    }
    pub fn rejected(message: impl Into<String>) -> Self {
        Self {
            positions: vec![],
            resolution: Resolution::Rejected,
            message: message.into(),
        }
    }
    fn wire(self, edit: &pb::ShapeEdit) -> pb::ShapeResult {
        let count = edit
            .r#move
            .as_ref()
            .and_then(|m| m.roi.as_ref())
            .map(|r| r.vertices.len());
        let valid = if self.resolution == Resolution::Rejected {
            self.positions.is_empty()
        } else {
            count == Some(self.positions.len())
                && self
                    .positions
                    .iter()
                    .all(|p| p.z.is_finite() && p.y.is_finite() && p.x.is_finite())
        };
        let shape = if valid {
            self
        } else {
            Self::rejected(
                "shaper must return one finite position per vertex, or no positions with REJECTED",
            )
        };
        pb::ShapeResult {
            drag_id: edit.drag_id,
            seq: edit.seq,
            positions: shape.positions,
            resolution: shape.resolution as i32,
            message: shape.message,
        }
    }
}

#[derive(Default)]
struct Drag {
    pending: Option<pb::ShapeEdit>,
    latest: u32,
    running: bool,
    committed: bool,
    ended: bool,
}
// Retain closed IDs as merged intervals: sequential gestures occupy one entry,
// while delayed messages from older gestures remain harmless for the session.
#[derive(Default)]
struct Closed(BTreeMap<u64, u64>);
impl Closed {
    fn contains(&self, id: u64) -> bool {
        self.0
            .range(..=id)
            .next_back()
            .is_some_and(|(_, end)| id <= *end)
    }
    fn insert(&mut self, id: u64) {
        let (mut start, mut end) = (id, id);
        if let Some((&s, &e)) = self.0.range(..=id).next_back() {
            if e.saturating_add(1) >= id {
                start = s;
                end = end.max(e);
                self.0.remove(&s);
            }
        }
        if let Some((&s, &e)) = self.0.range(start..).next() {
            if s <= end.saturating_add(1) {
                end = end.max(e);
                self.0.remove(&s);
            }
        }
        self.0.insert(start, end);
    }
}
#[derive(Default)]
struct State {
    drags: BTreeMap<u64, Arc<Mutex<Drag>>>,
    closed: Closed,
    stopped: bool,
}
pub(super) struct Shapes {
    handlers: BTreeMap<String, Shaper>,
    state: Mutex<State>,
}
impl Shapes {
    pub(super) fn new(manifest: &Manifest, handlers: BTreeMap<String, Shaper>) -> Result<Self> {
        for id in handlers.keys() {
            if !manifest.value()["contributions"]["region_types"]
                .as_array()
                .is_some_and(|types| types.iter().any(|t| t["id"].as_str() == Some(id)))
            {
                return Err(Error::protocol(format!(
                    "shaper '{id}' has no declared region type"
                )));
            }
        }
        Ok(Self {
            handlers,
            state: Mutex::new(State::default()),
        })
    }
    pub(super) fn drag(&self, ctx: &Context, edit: pb::ShapeEdit) -> Result<()> {
        let mut state = self.state.lock().unwrap();
        if state.stopped || state.closed.contains(edit.drag_id) {
            return Ok(());
        }
        let drag = state.drags.entry(edit.drag_id).or_default().clone();
        let mut d = drag.lock().unwrap();
        if edit.seq <= d.latest {
            return Ok(());
        }
        d.latest = edit.seq;
        d.pending = Some(edit);
        if d.running {
            return Ok(());
        }
        d.running = true;
        let ctx = ctx.clone();
        let running = drag.clone();
        if let Err(error) = std::thread::Builder::new()
            .name("vizi-shape-drag".into())
            .spawn(move || {
                loop {
                    let edit = {
                        let mut d = running.lock().unwrap();
                        if d.ended || d.committed {
                            d.pending = None;
                        }
                        match d.pending.take() {
                            Some(edit) => edit,
                            None => {
                                d.running = false;
                                return;
                            }
                        }
                    };
                    let result = ctx.core.shapes.compute(&ctx, &edit);
                    let d = running.lock().unwrap();
                    if !d.ended && !d.committed && d.latest == edit.seq {
                        // Preview loss under pressure is harmless; commits use the
                        // reliable queue. Superseded results never become proposals.
                        let _ = ctx
                            .core
                            .send(String::new(), Plugin::ShapeProposal(result), 5);
                    }
                }
            })
        {
            d.running = false;
            d.pending = None;
            return Err(error.into());
        }
        Ok(())
    }
    pub(super) fn commit(&self, ctx: &Context, mut edit: pb::ShapeEdit) -> Result<()> {
        let mut state = self.state.lock().unwrap();
        if state.stopped || state.closed.contains(edit.drag_id) {
            return Ok(());
        }
        state.closed.insert(edit.drag_id);
        let drag = state.drags.entry(edit.drag_id).or_default().clone();
        {
            let mut d = drag.lock().unwrap();
            d.committed = true;
            d.pending = None;
        }
        discard_proposals(&ctx.core, edit.drag_id);
        drop(state);
        edit.seq = 0;
        let worker_edit = edit.clone();
        let worker_ctx = ctx.clone();
        let running = drag.clone();
        let spawn = std::thread::Builder::new()
            .name("vizi-shape-commit".into())
            .spawn(move || {
                let result = worker_ctx.core.shapes.compute(&worker_ctx, &worker_edit);
                let d = running.lock().unwrap();
                if !d.ended {
                    if let Err(e) = worker_ctx.core.push(Plugin::ShapeAnswer(result)) {
                        if !worker_ctx.is_shutting_down() {
                            worker_ctx.core.fault(e.message);
                        }
                    }
                }
            });
        if let Err(error) = spawn {
            let d = drag.lock().unwrap();
            if !d.ended {
                ctx.core.push(Plugin::ShapeAnswer(
                    Shape::rejected(format!("cannot start shaper: {error}")).wire(&edit),
                ))?;
            }
        }
        Ok(())
    }
    fn compute(&self, ctx: &Context, edit: &pb::ShapeEdit) -> pb::ShapeResult {
        let shape = match self.handlers.get(&edit.type_name) {
            None => Shape::rejected(format!(
                "no shaper registered for region type '{}'",
                edit.type_name
            )),
            Some(handler) => {
                match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handler(ctx, edit)))
                {
                    Ok(Ok(shape)) => shape,
                    Ok(Err(error)) => Shape::rejected(error.message),
                    Err(payload) => Shape::rejected(
                        super::work::unwind_error(payload, "shaper panicked").message,
                    ),
                }
            }
        };
        let result = shape.wire(edit);
        let msg = if edit.seq == 0 {
            Plugin::ShapeAnswer(result.clone())
        } else {
            Plugin::ShapeProposal(result.clone())
        };
        if super::bounds::plugin(&pb::PluginMsg {
            id: String::new(),
            msg: Some(msg),
        })
        .is_err()
        {
            Shape::rejected("shaper result exceeds control-plane bounds").wire(edit)
        } else {
            result
        }
    }
    pub(super) fn end(&self, core: &Core, id: u64) {
        let mut state = self.state.lock().unwrap();
        state.closed.insert(id);
        if let Some(drag) = state.drags.remove(&id) {
            let mut d = drag.lock().unwrap();
            d.ended = true;
            d.pending = None;
        }
        discard_proposals(core, id);
    }
    pub(super) fn stop(&self, core: &Core) {
        let mut state = self.state.lock().unwrap();
        state.stopped = true;
        for drag in std::mem::take(&mut state.drags).into_values() {
            let mut d = drag.lock().unwrap();
            d.ended = true;
            d.pending = None;
        }
        let mut q = core.queues.lock().unwrap();
        q.bytes -= q.shapes.iter().map(Message::encoded_len).sum::<usize>();
        q.shapes.clear();
    }
    // Called after reserving gRPC capacity, so end/commit and the final handoff
    // are serialized even when the writer already removed a proposal's batch.
    pub(super) fn if_current(&self, result: &pb::ShapeResult, commit: bool, send: impl FnOnce()) {
        let state = self.state.lock().unwrap();
        if state.stopped {
            return;
        }
        if let Some(drag) = state.drags.get(&result.drag_id) {
            let d = drag.lock().unwrap();
            if !d.ended
                && if commit {
                    d.committed
                } else {
                    !d.committed && d.latest == result.seq
                }
            {
                send();
            }
        }
    }
}
fn discard_proposals(core: &Core, id: u64) {
    let mut q = core.queues.lock().unwrap();
    let before = q.shapes.iter().map(Message::encoded_len).sum::<usize>();
    q.shapes
        .retain(|m| !matches!(&m.msg, Some(Plugin::ShapeProposal(p)) if p.drag_id == id));
    q.bytes -= before - q.shapes.iter().map(Message::encoded_len).sum::<usize>();
}
pub(super) fn same_key(a: &pb::PluginMsg, b: &pb::PluginMsg) -> bool {
    matches!((&a.msg, &b.msg), (Some(Plugin::ShapeProposal(a)), Some(Plugin::ShapeProposal(b))) if a.drag_id == b.drag_id)
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{future::Future, task::Poll, time::Duration};
    fn runtime() -> (super::super::Runtime, std::os::fd::OwnedFd, Context) {
        let manifest = Manifest::parse(r#"{"manifest_version":1,"id":"org.example.shape","name":"Shape","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["shape"]}}"#).unwrap();
        let (fd, peer) = crate::socket_pair().unwrap();
        let (bell, listener) = crate::Doorbell::new(fd, Default::default()).unwrap();
        let rt = super::super::Runtime::new(
            manifest,
            Arc::new(crate::plugin::Registry::new()),
            bell,
            listener,
        )
        .unwrap();
        let ctx = rt.context();
        *ctx.core.peer.lock().unwrap() = Some((1, 0));
        (rt, peer, ctx)
    }
    fn proposal(id: u64, seq: u32) -> Plugin {
        Plugin::ShapeProposal(pb::ShapeResult {
            drag_id: id,
            seq,
            ..Default::default()
        })
    }
    #[test]
    fn proposal_queue_has_four_coalescing_keys_and_retires_them_at_commit_or_end() {
        let (rt, _peer, ctx) = runtime();
        for id in 1..=4 {
            ctx.core.send(String::new(), proposal(id, 1), 5).unwrap();
        }
        for seq in 2..100 {
            ctx.core.send(String::new(), proposal(1, seq), 5).unwrap();
        }
        assert!(ctx.core.send(String::new(), proposal(5, 1), 5).is_err());
        {
            let q = ctx.core.queues.lock().unwrap();
            assert_eq!(q.shapes.len(), 4);
            assert!(matches!(&q.shapes[0].msg, Some(Plugin::ShapeProposal(p)) if p.seq == 99));
            assert_eq!(
                q.bytes,
                q.shapes.iter().map(Message::encoded_len).sum::<usize>()
            );
        }
        ctx.core.shapes.end(&ctx.core, 1);
        ctx.core.send(String::new(), proposal(5, 1), 5).unwrap();
        ctx.core
            .shapes
            .commit(
                &ctx,
                pb::ShapeEdit {
                    drag_id: 2,
                    ..Default::default()
                },
            )
            .unwrap();
        assert!(!ctx
            .core
            .queues
            .lock()
            .unwrap()
            .shapes
            .iter()
            .any(|m| matches!(&m.msg, Some(Plugin::ShapeProposal(p)) if p.drag_id == 2)));
        rt.stop();
        assert!(ctx.core.queues.lock().unwrap().shapes.is_empty());
    }
    #[tokio::test]
    async fn blocked_writer_rechecks_supersession_commit_end_and_shutdown_at_handoff() {
        for action in ["newer", "commit", "end", "shutdown", "ended-answer"] {
            let (rt, _peer, ctx) = runtime();
            let d = Arc::new(Mutex::new(Drag {
                latest: 1,
                committed: action == "ended-answer",
                ..Default::default()
            }));
            ctx.core
                .shapes
                .state
                .lock()
                .unwrap()
                .drags
                .insert(1, d.clone());
            let (tx, mut rx) = tokio::sync::mpsc::channel(1);
            tx.send(Ok(pb::PluginMsg::default())).await.unwrap();
            let message = pb::PluginMsg {
                id: String::new(),
                msg: Some(if action == "ended-answer" {
                    Plugin::ShapeAnswer(pb::ShapeResult {
                        drag_id: 1,
                        ..Default::default()
                    })
                } else {
                    proposal(1, 1)
                }),
            };
            let write = ctx.core.write_outbound(
                &tx,
                message,
                tokio::time::Instant::now() + Duration::from_secs(5),
            );
            tokio::pin!(write);
            std::future::poll_fn(|cx| {
                assert!(matches!(write.as_mut().poll(cx), Poll::Pending));
                Poll::Ready(())
            })
            .await;
            match action {
                "newer" => d.lock().unwrap().latest = 2,
                "commit" => {
                    ctx.core
                        .shapes
                        .commit(
                            &ctx,
                            pb::ShapeEdit {
                                drag_id: 1,
                                ..Default::default()
                            },
                        )
                        .unwrap();
                }
                "end" | "ended-answer" => ctx.core.shapes.end(&ctx.core, 1),
                _ => rt.stop(),
            }
            rx.recv().await.unwrap().unwrap();
            assert!(write.await);
            assert!(rx.try_recv().is_err(), "obsolete output after {action}");
            rt.stop();
        }
    }
    #[test]
    fn closed_drag_intervals_merge_without_hiding_unseen_older_ids() {
        let mut closed = Closed::default();
        for id in [5, 2, 4, 3, 5] {
            closed.insert(id);
        }
        assert!(!closed.contains(1));
        assert!(!closed.contains(6));
        assert_eq!(closed.0, BTreeMap::from([(2, 5)]));
        closed.insert(1);
        assert_eq!(closed.0, BTreeMap::from([(1, 5)]));
        closed.insert(u64::MAX);
        assert!(closed.contains(u64::MAX));
    }
}
