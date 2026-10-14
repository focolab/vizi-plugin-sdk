//! A single-session gRPC server. The dispatcher never invokes application code.
mod arrangements;
mod bounds;
pub(crate) mod chunks;
mod host;
mod images;
pub mod shape;
pub mod work;
pub use host::{HostRoiInclusion, HostVolume, InclusionVolume, Panel, VoxelData};
mod follow;
mod runtime;
mod stage_runtime;
mod subscriptions;
mod view;
use crate::{
    memory::{Loan, Memory, Payload},
    proto as pb, Doorbell,
};
pub use arrangements::{edge_at_level, Input, PaneState};
pub use follow::Follow;
use pb::{host_msg::Msg as Host, plugin_msg::Msg as Plugin};
use prost::Message;
pub use runtime::Runtime;
use std::{
    collections::{BTreeMap, VecDeque},
    sync::{
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio::sync::Notify;
pub use view::{ViewControl, ViewMove, ViewRequestHandle};

pub const MAX_MESSAGE_BYTES: usize = 32 * 1024 * 1024;
pub const MAX_SERIES: usize = 1 << 22;
pub const MAX_INLINE_BYTES: usize = 8 * 1024 * 1024;
#[derive(Clone, Debug)]
pub struct Error {
    pub kind: pb::error::Kind,
    pub message: String,
}
pub type Result<T> = std::result::Result<T, Error>;
impl Error {
    pub fn new(kind: pb::error::Kind, message: impl Into<String>) -> Self {
        Self {
            kind,
            message: message.into(),
        }
    }
    pub fn protocol(s: impl Into<String>) -> Self {
        Self::new(pb::error::Kind::Protocol, s)
    }
    /// An ordinary cooperative cancellation, reported without a plugin fault.
    pub fn cancelled(message: impl Into<String>) -> Self {
        Self::new(pb::error::Kind::Cancelled, message)
    }
    pub fn internal(s: impl Into<String>) -> Self {
        Self::new(pb::error::Kind::Internal, s)
    }
    pub fn unsupported(s: impl Into<String>) -> Self {
        Self::new(pb::error::Kind::Unsupported, s)
    }
    pub fn wire(self) -> pb::Error {
        pb::Error {
            kind: self.kind as i32,
            message: self.message,
        }
    }
}
impl std::fmt::Display for Error {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.message)
    }
}
impl std::error::Error for Error {}
impl From<crate::memory::Failure> for Error {
    fn from(e: crate::memory::Failure) -> Self {
        Self::internal(e.detail)
    }
}
impl From<std::io::Error> for Error {
    fn from(e: std::io::Error) -> Self {
        Self::new(pb::error::Kind::IoError, e.to_string())
    }
}

struct Pending {
    expected: ReplyKind,
    tx: std::sync::mpsc::SyncSender<Result<HostReply>>,
}
#[derive(Clone, Copy, PartialEq, Eq)]
enum ReplyKind {
    View,
    Volume,
    Rois,
    Compiled,
    Mask,
    Activation,
    Asset,
    SavePath,
    Colormap,
    ViewRequest,
    Contributed,
    RequestStatus,
    RoiPosesExtended,
    RoiInclusion,
}
fn request_kind(msg: &Plugin) -> Option<ReplyKind> {
    Some(match msg {
        Plugin::GetViewState(_) => ReplyKind::View,
        Plugin::ReadDataVolume(_) => ReplyKind::Volume,
        Plugin::GetRois(_) => ReplyKind::Rois,
        Plugin::CompileLabelVolume(_) => ReplyKind::Compiled,
        Plugin::ReadMaskVoxels(_) => ReplyKind::Mask,
        Plugin::ActivateTool(_) => ReplyKind::Activation,
        Plugin::RequestAsset(_) => ReplyKind::Asset,
        Plugin::RequestSavePath(_) => ReplyKind::SavePath,
        Plugin::GetColormap(_) => ReplyKind::Colormap,
        Plugin::ViewRequest(_) => ReplyKind::ViewRequest,
        Plugin::ContributeRois(_) => ReplyKind::Contributed,
        Plugin::RequestStatusQuery(_) => ReplyKind::RequestStatus,
        Plugin::ExtendRoiPoses(_) => ReplyKind::RoiPosesExtended,
        Plugin::ReadRoiInclusion(_) => ReplyKind::RoiInclusion,
        Plugin::Ready(_)
        | Plugin::ReaderOpened(_)
        | Plugin::SeriesList(_)
        | Plugin::Segment(_)
        | Plugin::Metadata(_)
        | Plugin::Closed(_)
        | Plugin::Error(_)
        | Plugin::CommandDone(_)
        | Plugin::OfferFollow(_)
        | Plugin::FollowState(_)
        | Plugin::EndFollow(_)
        | Plugin::PanelPatch(_)
        | Plugin::ExportOpened(_)
        | Plugin::ChunkWritten(_)
        | Plugin::ExportDone(_)
        | Plugin::OverlayUpdate(_)
        | Plugin::OverlayRemove(_)
        | Plugin::ReleaseDeposit(_)
        | Plugin::GrantAck(_)
        | Plugin::ToolBindingAck(_)
        | Plugin::EpochAck(_)
        | Plugin::TimeMappingAck(_)
        | Plugin::GrantState(_)
        | Plugin::SegmentReleased(_)
        | Plugin::ReleaseRequest(_)
        | Plugin::DoorbellFault(_)
        | Plugin::DoorbellFinalReport(_)
        | Plugin::ContractViolation(_)
        | Plugin::SegmentShortfall(_)
        | Plugin::ShapeProposal(_)
        | Plugin::ShapeAnswer(_)
        | Plugin::WorkProgress(_)
        | Plugin::ShutdownReport(_)
        | Plugin::OverlayChunk(_)
        | Plugin::StagePlanned(_)
        | Plugin::StageArmAck(_)
        | Plugin::OverlayLabelTable(_) => return None,
    })
}
fn reply_kind(msg: &Host) -> Option<ReplyKind> {
    Some(match msg {
        Host::ViewState(_) => ReplyKind::View,
        Host::DataVolume(_) => ReplyKind::Volume,
        Host::Rois(_) => ReplyKind::Rois,
        Host::LabelVolumeCompiled(_) => ReplyKind::Compiled,
        Host::MaskVoxels(_) => ReplyKind::Mask,
        Host::ToolActivationChanged(_) => ReplyKind::Activation,
        Host::AssetResult(_) => ReplyKind::Asset,
        Host::SavePathResult(_) => ReplyKind::SavePath,
        Host::ColormapTable(_) => ReplyKind::Colormap,
        Host::ViewRequestResult(_) => ReplyKind::ViewRequest,
        Host::RoisContributed(_) => ReplyKind::Contributed,
        Host::RequestStatus(_) => ReplyKind::RequestStatus,
        Host::RoiPosesExtended(_) => ReplyKind::RoiPosesExtended,
        Host::RoiInclusion(_) => ReplyKind::RoiInclusion,
        Host::Hello(_)
        | Host::PlanStage(_)
        | Host::StageArm(_)
        | Host::OpenReader(_)
        | Host::ReadVolume(_)
        | Host::ReadPlane(_)
        | Host::Metadata(_)
        | Host::CloseReader(_)
        | Host::ShapeDrag(_)
        | Host::ShapeCommit(_)
        | Host::ShapeEditEnded(_)
        | Host::CancelWork(_)
        | Host::Shutdown(_)
        | Host::InvokeCommand(_)
        | Host::OpenExport(_)
        | Host::ExportChunk(_)
        | Host::FinishExport(_)
        | Host::AbortExport(_)
        | Host::HostError(_)
        | Host::InstallSegment(_)
        | Host::ToolBinding(_)
        | Host::EpochChange(_)
        | Host::GrantStateQuery(_)
        | Host::RetireSegment(_)
        | Host::PanelOpened(_)
        | Host::TimeMapping(_)
        | Host::DoorbellShutdown(_)
        | Host::ReleaseLoan(_)
        | Host::ContractViolation(_)
        | Host::ChunksWanted(_)
        | Host::ViewChanged(_)
        | Host::RegionsChanged(_)
        | Host::FollowStarted(_)
        | Host::FollowEnded(_) => return None,
    })
}
pub(crate) fn descriptor(msg: &Host) -> Option<&pb::SegmentRef> {
    match msg {
        Host::DataVolume(v) => v.segment.as_ref(),
        Host::MaskVoxels(v) => v.segment.as_ref(),
        Host::RoiInclusion(v) => v.segment.as_ref(),
        Host::ExportChunk(v) => match &v.source {
            Some(pb::export_chunk::Source::Segment(d)) => Some(d),
            _ => None,
        },
        _ => None,
    }
}

/// Reply ownership is explicit: dropping it returns any unread deposit.
pub struct HostReply {
    pub message: Host,
    descriptor: Option<pb::SegmentRef>,
    deposit: Option<crate::memory::DepositLease>,
    memory: Memory,
}
impl HostReply {
    pub fn copy_payload(&mut self) -> Result<Payload> {
        if matches!(self.message, Host::ExportChunk(_)) {
            return self.copy_export_payload();
        }
        let d = self
            .descriptor
            .take()
            .ok_or_else(|| Error::protocol("reply has no unread payload"))?;
        match self.deposit.take() {
            Some(lease) => lease.copy(&d, false),
            None => self.memory.copy_deposit(&d),
        }
        .map_err(Into::into)
    }
    pub(crate) fn copy_export_payload(&mut self) -> Result<Payload> {
        let d = self
            .descriptor
            .take()
            .ok_or_else(|| Error::protocol("request has no unread payload"))?;
        match self.deposit.take() {
            Some(lease) => lease.copy(&d, true),
            None => self.memory.copy_deposit_bytes(&d),
        }
        .map_err(Into::into)
    }
}
impl Drop for HostReply {
    fn drop(&mut self) {
        if let Some(d) = self.descriptor.take() {
            if let Err(e) = self.memory.discard_deposit(&d) {
                eprintln!("vizi SDK: abandoned deposit: {e}");
            }
        }
    }
}

#[derive(Default)]
struct Queues {
    reliable: VecDeque<pb::PluginMsg>,
    requests: VecDeque<pb::PluginMsg>,
    pushes: VecDeque<pb::PluginMsg>,
    work: VecDeque<pb::PluginMsg>,
    shapes: VecDeque<pb::PluginMsg>,
    after_reply: BTreeMap<String, DeferredAction>,
    closed: bool,
    fault: Option<String>,
    bytes: usize,
}
pub(crate) struct Core {
    images: Mutex<images::Images>,
    follows: Mutex<BTreeMap<u64, Arc<follow::Running>>>,
    shapes: shape::Shapes,
    stages: stage_runtime::Stages,
    chunks: chunks::Chunks,
    subscriptions: subscriptions::Subscriptions,
    queues: Mutex<Queues>,
    wake: Notify,
    pending: Mutex<BTreeMap<String, Pending>>,
    sequence: AtomicU64,
    work: Mutex<work::Registry>,
    memory_flush: Mutex<()>,
    pub memory: Memory,
    pub bell: Doorbell,
    pub manifest: crate::manifest::Manifest,
    arrangements: Mutex<arrangements::Arrangements>,
    host_identity: Mutex<Option<String>>,
    peer: Mutex<Option<(u32, u32)>>,
    shutting_down: AtomicBool,
    shutdown_started: Notify,
    doorbell_shutdown: AtomicBool,
    started: AtomicBool,
    ended: AtomicBool,
    done: Notify,
    unusable_inputs: AtomicU64,
    deferred_actions: AtomicUsize,
}
impl Core {
    fn send(&self, id: String, msg: Plugin, class: u8) -> Result<()> {
        self.send_with_action(id, msg, class, None)
    }
    fn send_with_action(
        &self,
        id: String,
        mut msg: Plugin,
        class: u8,
        action: Option<DeferredAction>,
    ) -> Result<()> {
        if self.peer.lock().unwrap().is_none() && !matches!(msg, Plugin::Ready(_)) {
            return Err(Error::protocol("Hello/Ready handshake is pending"));
        }
        if let Plugin::WorkProgress(report) = &mut msg {
            report.title = work::clip_text(&report.title);
            report.text = work::clip_text(&report.text);
        }
        let id_for_action = id.clone();
        let message = pb::PluginMsg { id, msg: Some(msg) };
        let loans = bounds::plugin(&message)?;
        let result = (|| {
            let mut q = self.queues.lock().unwrap();
            if q.closed {
                return Err(Error::internal("session closed"));
            }
            // Serialize the final admission check with shutdown, not just the
            // caller's earlier check before it registered its reply waiter.
            if request_kind(message.msg.as_ref().unwrap()).is_some()
                && self.shutting_down.load(Ordering::SeqCst)
            {
                return Err(Error::protocol("new requests are refused during shutdown"));
            }
            let len = message.encoded_len();
            // Follow states/offers share the terminal FIFO so EndFollow cannot
            // overtake the last state. They still coalesce and refuse overflow
            // without ending the session, unlike one-shot reliable messages.
            if class == 3 {
                if let Some(index) = q
                    .reliable
                    .iter()
                    .position(|old| follow::same_key(old, &message))
                {
                    let old_len = q.reliable[index].encoded_len();
                    let bytes = q.bytes - old_len + len;
                    if bytes > 64 * 1024 * 1024 {
                        return Err(Error::internal(
                            "outbound byte budget exceeded; message not admitted",
                        ));
                    }
                    q.reliable[index] = message;
                    q.bytes = bytes;
                    drop(q);
                    self.wake.notify_one();
                    return Ok(());
                }
            }
            if class == 5 {
                if let Some(index) = q
                    .shapes
                    .iter()
                    .position(|old| shape::same_key(old, &message))
                {
                    let bytes = q.bytes - q.shapes[index].encoded_len() + len;
                    if bytes > 64 * 1024 * 1024 {
                        return Err(Error::internal(
                            "outbound byte budget exceeded; message not admitted",
                        ));
                    }
                    q.shapes[index] = message;
                    q.bytes = bytes;
                    drop(q);
                    self.wake.notify_one();
                    return Ok(());
                }
            }
            if class == 4 {
                if let Some(index) = q.work.iter().position(|old| work::same_key(old, &message)) {
                    let bytes = q.bytes - q.work[index].encoded_len() + len;
                    if bytes > 64 * 1024 * 1024 {
                        return Err(Error::internal(
                            "outbound byte budget exceeded; message not admitted",
                        ));
                    }
                    q.work[index] = message;
                    q.bytes = bytes;
                    drop(q);
                    self.wake.notify_one();
                    return Ok(());
                }
            }
            if q.bytes.saturating_add(len) > 64 * 1024 * 1024 {
                if class == 0 {
                    q.fault = Some("reliable outbound byte budget exceeded".into());
                    q.closed = true;
                    self.wake.notify_one();
                }
                return Err(Error::internal(
                    "outbound byte budget exceeded; message not admitted",
                ));
            }
            let (queue, capacity) = match class {
                0 | 3 => (&mut q.reliable, 256),
                1 => (&mut q.requests, 32),
                4 => (&mut q.work, 68),
                5 => (&mut q.shapes, 4),
                _ => (&mut q.pushes, 256),
            };
            if queue.len() >= capacity {
                if class == 0 {
                    q.fault = Some("reliable outbound queue overflow".into());
                    q.closed = true;
                }
                self.wake.notify_one();
                return Err(Error::internal("outbound queue full; message not admitted"));
            }
            self.memory
                .admit_loans(&loans)
                .map_err(|e| Error::protocol(e.detail))?;
            queue.push_back(message);
            if let Some(action) = action {
                q.after_reply.insert(id_for_action, action);
            }
            q.bytes += len;
            drop(q);
            self.wake.notify_one();
            Ok(())
        })();
        if result.is_err() {
            self.memory.strand_loans(&loans);
        }
        result
    }
    fn push(&self, msg: Plugin) -> Result<()> {
        self.send(String::new(), msg, 0)
    }
    fn close_request_admission(&self) {
        let mut q = self.queues.lock().unwrap();
        self.shutting_down.store(true, Ordering::SeqCst);
        // The writer may already hold a batch; it performs the same check at
        // the final handoff to gRPC. Replies and cleanup messages still drain.
        let bytes = q.requests.iter().map(Message::encoded_len).sum::<usize>();
        q.requests.clear();
        q.bytes -= bytes;
        drop(q);
        work::end_session(self);
        self.shapes.stop(self);
        self.shutdown_started.notify_waiters();
    }
    fn fault(&self, detail: impl Into<String>) {
        let detail = detail.into();
        eprintln!("vizi SDK session fault: {detail}");
        let mut q = self.queues.lock().unwrap();
        if q.fault.is_none() {
            q.fault = Some(detail);
        }
        q.closed = true;
        drop(q);
        self.wake.notify_one();
    }
    fn finish(&self) {
        if self.ended.swap(true, Ordering::SeqCst) {
            return;
        }
        self.close_request_admission();
        self.chunks.stop();
        self.subscriptions.stop();
        self.stop_follows();
        self.bell.stop();
        self.memory.close();
        for (_, p) in std::mem::take(&mut *self.pending.lock().unwrap()) {
            let _ = p.tx.send(Err(Error::internal("session ended")));
        }
        let mut queues = self.queues.lock().unwrap();
        let abandoned = std::mem::take(&mut queues.after_reply);
        queues.closed = true;
        drop(queues);
        // Captures may have application-defined destructors; never run those
        // under the outbound queue lock.
        drop(abandoned);
        self.wake.notify_one();
        self.done.notify_waiters();
    }
    fn flush_memory(&self) {
        let _flush = self.memory_flush.lock().unwrap();
        match self.memory.drain_events() {
            Ok(events) => {
                for e in events {
                    if let Err(error) = self.push(e) {
                        self.fault(error.message);
                        break;
                    }
                }
            }
            Err(e) => self.fault(e.detail),
        }
    }
    fn deliver_unknown(&self, id: &str) -> bool {
        let pending = self.pending.lock().unwrap().remove(id);
        if let Some(p) = pending {
            let _ =
                p.tx.send(Err(Error::unsupported("unknown host reply kind")));
            true
        } else {
            false
        }
    }
    fn deliver(&self, id: String, msg: Host) {
        let d = descriptor(&msg).cloned();
        let reply = HostReply {
            deposit: d.as_ref().and_then(|d| self.memory.pin_deposit(d).ok()),
            message: msg,
            descriptor: d,
            memory: self.memory.clone(),
        };
        if let Some(p) = self.pending.lock().unwrap().remove(&id) {
            let result = if let Host::HostError(e) = &reply.message {
                Err(Error::new(
                    pb::error::Kind::try_from(e.kind).unwrap_or(pb::error::Kind::Internal),
                    e.message.clone(),
                ))
            } else if reply_kind(&reply.message) != Some(p.expected) {
                Err(Error::protocol("host reply kind differs from request"))
            } else {
                Ok(reply)
            };
            // If the caller expired between removal and send, Err owns and
            // drops the reply, returning its reservation exactly once.
            let _ = p.tx.send(result);
        } else if !self.shutting_down.load(Ordering::SeqCst) {
            eprintln!(
                "vizi SDK: late or unmatched host reply dropped, id {:?}",
                id.chars().take(128).collect::<String>()
            );
        }
    }
}

/// An outstanding host request. Dropping it abandons the local waiter, not
/// the host's work. A late reply is discarded without ending the session.
pub struct HostRequest {
    core: Arc<Core>,
    id: String,
    rx: std::sync::mpsc::Receiver<Result<HostReply>>,
    completed: bool,
}
impl HostRequest {
    pub fn id(&self) -> &str {
        &self.id
    }
    /// A timeout returns None and retains the waiter for another poll or a
    /// status query. UNKNOWN status does not establish that the work failed.
    pub fn wait_timeout(&mut self, timeout: Duration) -> Result<Option<HostReply>> {
        if self.completed {
            return Err(Error::protocol("host request already completed"));
        }
        match self.rx.recv_timeout(timeout) {
            Ok(result) => {
                self.completed = true;
                result.map(Some)
            }
            Err(std::sync::mpsc::RecvTimeoutError::Timeout) => Ok(None),
            Err(std::sync::mpsc::RecvTimeoutError::Disconnected) => {
                self.completed = true;
                Err(Error::internal("session ended"))
            }
        }
    }
    /// Consume the waiter. Timeout abandons it; the host outcome is unknown.
    pub fn wait(mut self, timeout: Duration) -> Result<HostReply> {
        self.wait_timeout(timeout)?
            .ok_or_else(|| Error::internal("host request timed out; outcome is unknown"))
    }
}
impl Drop for HostRequest {
    fn drop(&mut self) {
        self.core.pending.lock().unwrap().remove(&self.id);
    }
}

/// Cloneable API for handlers and plugin-owned worker threads. Calls to the
/// host have finite deadlines and never run on the dispatcher.
#[derive(Clone)]
pub struct Context {
    pub(crate) core: Arc<Core>,
    after_reply: Option<Arc<Mutex<DeferredSlot>>>,
    work: Option<Arc<work::RequestWork>>,
}

#[derive(Default)]
struct DeferredSlot {
    closed: bool,
    action: Option<DeferredAction>,
}
type DeferredCallback = Box<dyn FnOnce(Context) -> Result<()> + Send>;
struct DeferredAction {
    callback: Option<DeferredCallback>,
    core: Arc<Core>,
}
impl Drop for DeferredAction {
    fn drop(&mut self) {
        self.core.deferred_actions.fetch_sub(1, Ordering::SeqCst);
    }
}
impl DeferredAction {
    fn start(mut self) {
        let core = self.core.clone();
        let result = std::thread::Builder::new()
            .name("vizi-after-reply".into())
            .spawn(move || {
                if self.core.shutting_down.load(Ordering::SeqCst) {
                    return;
                }
                let ctx = Context {
                    core: self.core.clone(),
                    after_reply: None,
                    work: None,
                };
                let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    self.callback.take().unwrap()(ctx)
                }));
                match result {
                    Ok(Ok(())) => {}
                    Ok(Err(_)) if self.core.shutting_down.load(Ordering::SeqCst) => {}
                    Ok(Err(e)) if e.kind == pb::error::Kind::Cancelled => {}
                    Ok(Err(e)) => eprintln!("vizi SDK after-reply callback: {e}"),
                    Err(payload) if payload.is::<work::Cancelled>() => {}
                    Err(_) => eprintln!("vizi SDK after-reply callback panicked"),
                }
            });
        if let Err(e) = result {
            core.fault(format!("cannot start after-reply callback: {e}"));
        }
    }
}
impl Context {
    /// Run one action on a separate worker after this handler's successful reply
    /// has entered the outgoing stream. Messages sent by the action cannot
    /// overtake that reply, even across the runtime's priority queues.
    ///
    /// Only request-handler contexts support this. Registration closes when the
    /// handler returns. Error replies, CommandDone{ok:false}, invalid replies,
    /// expired requests, failed sends and shutdown discard the action. At most
    /// 32 registered/running actions are allowed per session. A running action
    /// cannot be forcibly cancelled; its host requests end with the session.
    /// Errors and panics are diagnosed locally since the reply is already sent.
    pub fn after_reply(
        &self,
        action: impl FnOnce(Context) -> Result<()> + Send + 'static,
    ) -> Result<()> {
        if self.is_shutting_down() {
            return Err(Error::protocol("session is shutting down"));
        }
        let slot = self
            .after_reply
            .as_ref()
            .ok_or_else(|| Error::protocol("after_reply requires a request-handler context"))?;
        let mut slot = slot.lock().unwrap();
        if slot.closed || slot.action.is_some() {
            return Err(Error::protocol(
                "after_reply already registered or handler returned",
            ));
        }
        // Rust 1.99 renamed this to try_update, unavailable on our 1.88 MSRV.
        #[allow(deprecated)]
        self.core
            .deferred_actions
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| {
                (n < 32).then_some(n + 1)
            })
            .map_err(|_| Error::internal("32 after-reply actions already pending"))?;
        slot.action = Some(DeferredAction {
            callback: Some(Box::new(action)),
            core: self.core.clone(),
        });
        Ok(())
    }
    pub fn memory(&self) -> &Memory {
        &self.core.memory
    }
    pub fn host_identity(&self) -> Option<String> {
        self.core.host_identity.lock().unwrap().clone()
    }
    pub fn unusable_input_count(&self) -> u64 {
        self.core.unusable_inputs.load(Ordering::Relaxed)
    }
    /// True after host shutdown, local stop, or loss of the session. Pending
    /// host calls are woken with errors and new calls are refused. Workers
    /// should treat an error while this is true as cancellation, not retry it.
    /// Wake application-owned idle waits in `on_shutdown`; also stop/join your
    /// workers when serving returns, which covers a host crash without a hook.
    pub fn is_shutting_down(&self) -> bool {
        self.core.shutting_down.load(Ordering::SeqCst)
    }
    pub fn request(&self, msg: Plugin) -> Result<HostReply> {
        self.request_timeout(msg, Duration::from_secs(30))
    }
    pub fn request_timeout(&self, msg: Plugin, timeout: Duration) -> Result<HostReply> {
        self.start_request(msg)?.wait(timeout)
    }
    /// Submit a request without blocking. Retain its ID for solicited status
    /// queries and poll it with finite waits on an application worker.
    pub fn start_request(&self, msg: Plugin) -> Result<HostRequest> {
        if self.is_shutting_down() {
            return Err(Error::protocol("new requests are refused during shutdown"));
        }
        let expected =
            request_kind(&msg).ok_or_else(|| Error::protocol("message is not a plugin request"))?;
        // Rust 1.99 renamed this to try_update, unavailable on our 1.88 MSRV.
        #[allow(deprecated)]
        let n = self
            .core
            .sequence
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |n| n.checked_add(1))
            .map_err(|_| Error::internal("request id space exhausted"))?;
        let id = format!("q{n}");
        let (tx, rx) = std::sync::mpsc::sync_channel(1);
        {
            let mut pending = self.core.pending.lock().unwrap();
            if self.is_shutting_down() {
                return Err(Error::protocol("new requests are refused during shutdown"));
            }
            if pending.len() >= 32 {
                return Err(Error::internal("32 plugin requests already pending"));
            }
            pending.insert(id.clone(), Pending { expected, tx });
        }
        if let Err(e) = self.core.send(id.clone(), msg, 1) {
            self.core.pending.lock().unwrap().remove(&id);
            return Err(e);
        }
        Ok(HostRequest {
            core: self.core.clone(),
            id,
            rx,
            completed: false,
        })
    }
    /// Send an application push. Full queues refuse the caller without ending
    /// the session. Descriptor-bearing pushes are kept in order, never merged.
    pub fn push(&self, msg: Plugin) -> Result<()> {
        if !matches!(
            msg,
            Plugin::PanelPatch(_)
                | Plugin::OverlayUpdate(_)
                | Plugin::OverlayRemove(_)
                | Plugin::OverlayChunk(_)
                | Plugin::OverlayLabelTable(_)
                | Plugin::OfferFollow(_)
                | Plugin::FollowState(_)
                | Plugin::EndFollow(_)
                | Plugin::WorkProgress(_)
        ) {
            return Err(Error::protocol("not an application push"));
        }
        let class = if matches!(msg, Plugin::WorkProgress(_)) {
            4
        } else if matches!(msg, Plugin::OfferFollow(_) | Plugin::FollowState(_)) {
            3
        } else if matches!(msg, Plugin::OverlayRemove(_) | Plugin::EndFollow(_)) {
            0
        } else {
            2
        };
        if let Plugin::PanelPatch(patch) = &msg {
            for op in &patch.ops {
                if let Some(pb::patch_op::Op::SetRows(rows)) = &op.op {
                    let max = self
                        .core
                        .manifest
                        .list_max_rows(&patch.panel_id, &op.widget_id)
                        .ok_or_else(|| Error::protocol("set_rows requires a declared list"))?;
                    if !op.row_key.is_empty() || rows.keys.len() > max {
                        return Err(Error::protocol(
                            "set_rows must address a list and fit max_rows",
                        ));
                    }
                }
            }
            let mut images = self.core.images.lock().unwrap();
            self.core.send(String::new(), msg.clone(), class)?;
            images.prune(&self.core.manifest, patch);
            Ok(())
        } else {
            self.core.send(String::new(), msg, class)
        }
    }
    /// On admission failure, reclaim only loans no admitted message has named.
    /// Previously admitted loans remain the host's to release.
    pub fn push_with_loans(&self, msg: Plugin, loans: Vec<Loan>) -> Result<()> {
        let result = self.push(msg);
        if result.is_err() {
            for loan in loans {
                // Bounds failures happen before Core's admission check too.
                self.memory().strand_loans(&[pb::SegmentRef {
                    segment_id: loan.segment_id,
                    offset: loan.offset,
                    len: loan.len,
                    ..Default::default()
                }]);
                if let Err(e) = self.memory().cancel_unsent(loan) {
                    eprintln!("vizi SDK: loan retained after failed push: {e}");
                }
            }
        }
        result
    }
    pub fn notify(&self, n: &crate::Notification) -> Result<crate::SendOutcome> {
        self.core
            .bell
            .notify(n)
            .map_err(|e| Error::protocol(e.to_string()))
    }
    pub fn pane(&self, view: u8) -> Option<Arc<PaneState>> {
        self.pane_by_id(u32::from(view))
    }
    /// Control-plane panes above 255 can be arranged but receive no doorbell E.
    pub fn pane_by_id(&self, view: u32) -> Option<Arc<PaneState>> {
        self.core
            .arrangements
            .lock()
            .unwrap()
            .panes
            .get(&view)
            .cloned()
    }
    pub fn asset(&self, id: &str) -> Result<std::path::PathBuf> {
        let values = std::env::var("VIZI_PLUGIN_ASSETS").unwrap_or_else(|_| "{}".into());
        let values: serde_json::Value =
            serde_json::from_str(&values).map_err(|e| Error::protocol(e.to_string()))?;
        let map = values
            .as_object()
            .ok_or_else(|| Error::protocol("VIZI_PLUGIN_ASSETS must be an object"))?;
        let value = map.get(id).ok_or_else(|| {
            Error::new(
                pb::error::Kind::NotFound,
                format!("asset '{id}' was not declared/published for this plugin"),
            )
        })?;
        let path = std::path::PathBuf::from(
            value
                .as_str()
                .ok_or_else(|| Error::protocol("asset path must be a string"))?,
        );
        if !path.is_absolute() {
            return Err(Error::protocol("asset path must be absolute"));
        }
        if !path.is_file() {
            return Err(Error::new(
                pb::error::Kind::NotFound,
                format!("declared asset '{id}' is missing"),
            ));
        }
        Ok(path)
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use std::{fs::File, os::fd::FromRawFd, os::unix::fs::FileExt};

    #[test]
    fn queue_refusals_leave_loan_counts_unchanged() {
        let manifest = crate::manifest::Manifest::parse(
            r#"{"manifest_version":1,"id":"test.admission","name":"Admission","version":"1.0.0","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#,
        ).unwrap();
        let (fd, _peer) = crate::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(
            manifest,
            Arc::new(crate::plugin::Registry::new()),
            bell,
            listener,
        )
        .unwrap();
        let ctx = runtime.context();
        *ctx.core.peer.lock().unwrap() = Some((1, 0));
        let fd = unsafe {
            libc::memfd_create(
                c"admission".as_ptr(),
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            )
        };
        assert!(fd >= 0);
        let file = unsafe { File::from_raw_fd(fd) };
        file.set_len(512).unwrap();
        let mut preamble = [0; 32];
        preamble[..8].copy_from_slice(&1u64.to_le_bytes());
        let crc = crate::crc32(&preamble[..28]);
        preamble[28..].copy_from_slice(&crc.to_le_bytes());
        file.write_all_at(&preamble, 0).unwrap();
        assert_eq!(
            unsafe {
                libc::fcntl(
                    fd,
                    libc::F_ADD_SEALS,
                    libc::F_SEAL_GROW | libc::F_SEAL_SHRINK,
                )
            },
            0
        );
        let grant = crate::MemoryGrant {
            segment_id: 1,
            size: 512,
            permissions: crate::Permissions::new(15).unwrap(),
        };
        ctx.memory()
            .grant(grant.clone(), grant.encode(1).unwrap(), file.into())
            .unwrap();
        ctx.memory()
            .install(pb::InstallSegment {
                segment_id: 1,
                purpose: "G".into(),
                regions: vec![pb::Region {
                    index: 0,
                    offset: 64,
                    extent: 256,
                    discipline: 4,
                    producer: 2,
                    ..Default::default()
                }],
                ..Default::default()
            })
            .unwrap();
        let loan = ctx.memory().reserve(1, b"loan").unwrap();
        let message = Plugin::Segment(pb::SegmentRef {
            segment_id: 1,
            offset: loan.offset,
            len: loan.len,
            uses: 2,
            ..Default::default()
        });
        ctx.core.send("first".into(), message.clone(), 0).unwrap();
        for class in [1, 2] {
            {
                let mut q = ctx.core.queues.lock().unwrap();
                let (queue, capacity) = if class == 1 {
                    (&mut q.requests, 32)
                } else {
                    (&mut q.pushes, 256)
                };
                queue.resize_with(capacity, pb::PluginMsg::default);
            }
            assert!(ctx
                .core
                .send("refused".into(), message.clone(), class)
                .unwrap_err()
                .message
                .contains("queue full"));
            assert!(ctx.memory().cancel_unsent(loan.clone()).is_err());
            let mut q = ctx.core.queues.lock().unwrap();
            q.requests.clear();
            q.pushes.clear();
        }
        let bytes = ctx.core.queues.lock().unwrap().bytes;
        ctx.core.queues.lock().unwrap().bytes = 64 * 1024 * 1024;
        assert!(ctx
            .core
            .send("refused".into(), message.clone(), 2)
            .unwrap_err()
            .message
            .contains("byte budget"));
        ctx.core.queues.lock().unwrap().bytes = bytes;
        ctx.core.send("second".into(), message.clone(), 1).unwrap();
        assert!(ctx
            .core
            .send("third".into(), message, 2)
            .unwrap_err()
            .message
            .contains("uses exceeded"));
        assert!(ctx.core.queues.lock().unwrap().pushes.is_empty());
        assert!(ctx.memory().cancel_unsent(loan.clone()).is_err());
        let first = pb::SegmentRef {
            segment_id: 1,
            offset: loan.offset,
            len: loan.len,
            uses: 2,
            ..Default::default()
        };
        ctx.memory().loans_sent(&[first.clone(), first]);
        ctx.core.queues.lock().unwrap().requests.clear();
        ctx.core.queues.lock().unwrap().reliable.clear();
        ctx.memory().drain_events().unwrap();

        // One use reached transport, but the second cannot enter the queue.
        // That real admission failure must strand the reservation and release
        // its retirement claim without reclaiming bytes still lent to the host.
        let loan = ctx.memory().reserve(1, b"stranded").unwrap();
        let descriptor = pb::SegmentRef {
            segment_id: 1,
            offset: loan.offset,
            len: loan.len,
            uses: 2,
            ..Default::default()
        };
        ctx.core
            .send("landed".into(), Plugin::Segment(descriptor.clone()), 1)
            .unwrap();
        ctx.memory().loans_sent(std::slice::from_ref(&descriptor));
        ctx.core.queues.lock().unwrap().requests.clear();
        ctx.core
            .queues
            .lock()
            .unwrap()
            .pushes
            .resize_with(256, pb::PluginMsg::default);
        assert!(ctx
            .core
            .send("refused".into(), Plugin::Segment(descriptor), 2)
            .is_err());
        ctx.memory().retire(1).unwrap();
        assert!(matches!(
            ctx.memory().drain_events().unwrap().as_slice(),
            [Plugin::SegmentReleased(_)]
        ));
    }
}
