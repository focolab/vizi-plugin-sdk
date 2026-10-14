//! Plugin author API. Use Registry for common contributions, or implement
//! Plugin directly for complete access to the published protobuf messages.
use crate::{
    control::{Context, Error, HostReply, Input, Result},
    memory::{validate_descriptor, Payload},
    proto as pb,
};
use pb::{host_msg::Msg as Host, plugin_msg::Msg as Reply};
use prost::Message;
use std::{
    collections::BTreeMap,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc, Mutex,
    },
};
mod series;
pub use series::{ReaderOpen, SeriesChoice, SeriesEntry};

pub use crate::control::chunks::{Chunk, ChunkSource, LabelTable};

pub trait Plugin: Send + Sync + 'static {
    /// Shape computations, keyed by declared region type. Drag and commit use
    /// the same function (seq is zero on commit), on independent SDK workers.
    fn shapers(&self) -> BTreeMap<String, crate::shape::Shaper> {
        BTreeMap::new()
    }
    /// Called after a shape edit ends, even if this plugin never proposed for it.
    fn shape_edit_ended(&self, _ctx: &Context, _ended: pb::ShapeEditEnded) -> Result<()> {
        Ok(())
    }

    fn stages(&self) -> BTreeMap<String, Arc<dyn crate::stage::Stage>> {
        BTreeMap::new()
    }
    /// Registered sources are run by the SDK, one worker per overlay.
    fn chunk_sources(&self) -> BTreeMap<String, ChunkSource> {
        BTreeMap::new()
    }
    fn handle(&self, _ctx: &Context, _request: &mut HostReply) -> Result<Reply> {
        Err(Error::unsupported(
            "operation not implemented by this plugin",
        ))
    }
    /// Run a person-started Follow on its own worker. Return/error/panic ends it.
    fn follow(&self, _ctx: &Context, follow: &crate::control::Follow) -> Result<()> {
        eprintln!(
            "vizi SDK: no Follow driver for panel {:?}; ending Follow",
            follow.panel_id()
        );
        Ok(())
    }
    fn input(&self, _ctx: &Context, _input: Input) -> Result<()> {
        Ok(())
    }
    fn cancelled(&self, _ctx: &Context, _cancel: crate::Cancellation) -> Result<()> {
        Ok(())
    }
    /// Called once on engagement, after the binding acknowledgement is queued.
    fn activated(&self, _ctx: &Context, _binding: pb::ToolBinding) -> Result<()> {
        Ok(())
    }
    /// Called after withdrawal acknowledgement and cancellation of open input.
    fn deactivated(&self, _ctx: &Context, _binding: pb::ToolBinding) -> Result<()> {
        Ok(())
    }
    /// Coalescing view state, on a dedicated worker that may call Context.
    fn view_changed(&self, _ctx: &Context, _change: pb::ViewChanged) -> Result<()> {
        Ok(())
    }
    /// Pending updates merge by region; reset discards earlier pending state.
    /// Runs on its own worker, independently of view_changed and commands.
    fn regions_changed(&self, _ctx: &Context, _change: pb::RegionsChanged) -> Result<()> {
        Ok(())
    }
    /// Called on every open, including re-shown docks. Resending state is optional:
    /// the host retains pre-open non-image patches. Images need the panel's arena.
    fn panel_opened(&self, _ctx: &Context, _panel: pb::PanelOpened) -> Result<()> {
        Ok(())
    }
    fn pulsed(&self, _ctx: &Context, _segment: u64, _region: u32, _payload: Payload) -> Result<()> {
        Ok(())
    }
    fn shutdown(
        &self,
        _ctx: &Context,
        _shutdown: pb::Shutdown,
    ) -> Result<pb::shutdown_report::Outcome> {
        Ok(pb::shutdown_report::Outcome::NoHook)
    }
    /// Final cleanup on the runtime's background worker, including local stop
    /// and unannounced disconnect. No more messages can be sent. May overlap
    /// a timed-out shutdown hook or an application callback still in flight.
    fn session_ended(&self) {}
}

#[derive(Clone, Debug)]
pub struct Volume {
    pub bytes: Vec<u8>,
    pub shape_zyx: [u32; 3],
    pub dtype: pb::DType,
}
pub trait Reader: Send + Sync + 'static {
    /// Existing readers return metadata after their factory has opened the file.
    fn info(&self) -> Result<pb::ReaderOpened> {
        Err(Error::unsupported("reader metadata not implemented"))
    }
    /// Override for multi-image files. None asks to open or list; Some("") is
    /// a real series id. A held listing is reopened here without another factory
    /// call. The default preserves readers whose factory already opened a file.
    fn open(&self, _request: &pb::OpenReader) -> Result<ReaderOpen> {
        self.info().map(ReaderOpen::Opened)
    }
    fn read_volume(&self, request: &pb::ReadVolume) -> Result<Volume>;
    /// By default, extract the requested plane from a level-0 volume.
    fn read_plane(&self, request: &pb::ReadPlane) -> Result<Volume> {
        level_zero(request.level)?;
        let mut volume = self.read_volume(&pb::ReadVolume {
            handle: request.handle.clone(),
            t: request.t,
            c: request.c,
            level: 0,
        })?;
        validate_descriptor(&pb::SegmentRef {
            len: volume.bytes.len() as u64,
            shape_zyx: volume.shape_zyx.to_vec(),
            dtype: volume.dtype as i32,
            ..Default::default()
        })?;
        if request.z >= volume.shape_zyx[0] {
            return Err(Error::new(
                pb::error::Kind::OutOfBounds,
                "reader z index out of bounds",
            ));
        }
        let stride = volume.bytes.len() / volume.shape_zyx[0] as usize;
        let start = request.z as usize * stride;
        volume.bytes = volume.bytes[start..start + stride].to_vec();
        volume.shape_zyx[0] = 1;
        Ok(volume)
    }
    fn metadata(&self) -> Result<pb::MetadataResult> {
        Ok(Default::default())
    }
    /// Release file resources. Registry calls this on replacement and shutdown,
    /// including while another callback may still be running during shutdown.
    fn close(&self) -> Result<()> {
        Ok(())
    }
}
/// One export write, with provenance determined per channel for THIS chunk.
/// `bytes` holds packed channel volumes in `channels` order. The layout and
/// calibration are those supplied to the factory in OpenExport.
pub struct ExportChunk<'a> {
    pub t: u32,
    pub channels: &'a [u32],
    pub bytes: &'a [u8],
    /// Channels explicitly marked filtered by this chunk, not by OpenExport.
    pub filtered_channels: &'a [u32],
    filter: Option<&'a pb::AppliedFilter>,
}
impl ExportChunk<'_> {
    /// Provenance of this channel's volume. None means the file's own voxels
    /// (or a channel absent from this chunk). The filter named at OpenExport
    /// is used only when this chunk explicitly marks the channel as filtered.
    pub fn filter_for(&self, channel: u32) -> Option<&pb::AppliedFilter> {
        if self.channels.contains(&channel) && self.filtered_channels.contains(&channel) {
            self.filter
        } else {
            None
        }
    }
}

pub trait Exporter: Send + Sync + 'static {
    /// Return the bytes actually consumed. Partial success is reflected in
    /// ChunkWritten.total_bytes, never silently reported as full success.
    /// Implement write_chunk instead when the artifact records provenance.
    fn write(&self, _t: u32, _channels: &[u32], _bytes: &[u8]) -> Result<u64> {
        Err(Error::unsupported("export write not implemented"))
    }
    /// Called for every valid chunk, including raw fallback when the user
    /// allowed it. Contradictory filter metadata is refused before this callback.
    /// Existing exporters implementing write continue to receive the same bytes.
    fn write_chunk(&self, chunk: &ExportChunk<'_>) -> Result<u64> {
        self.write(chunk.t, chunk.channels, chunk.bytes)
    }
    fn finish(&self) -> Result<pb::ExportDone>;
    /// Abandon the export and clean up its partial artifact, including after
    /// successful writes. A failed filter timepoint normally aborts the export.
    fn abort(&self) -> Result<()> {
        Ok(())
    }
}
type FollowHook = Arc<dyn Fn(&Context, &crate::control::Follow) -> Result<()> + Send + Sync>;
type Command = Arc<dyn Fn(&Context, &pb::InvokeCommand) -> Result<pb::CommandDone> + Send + Sync>;
type ReaderFactory =
    Arc<dyn Fn(&Context, &pb::OpenReader) -> Result<Arc<dyn Reader>> + Send + Sync>;
type ExportFactory =
    Arc<dyn Fn(&Context, &pb::OpenExport) -> Result<Arc<dyn Exporter>> + Send + Sync>;
type InputHook = Arc<dyn Fn(&Context, Input) -> Result<()> + Send + Sync>;
type CancelHook = Arc<dyn Fn(&Context, crate::Cancellation) -> Result<()> + Send + Sync>;
type ToolHook = Arc<dyn Fn(&Context, pb::ToolBinding) -> Result<()> + Send + Sync>;
type PanelHook = Arc<dyn Fn(&Context, pb::PanelOpened) -> Result<()> + Send + Sync>;
type ViewHook = Arc<dyn Fn(&Context, pb::ViewChanged) -> Result<()> + Send + Sync>;
type RegionsHook = Arc<dyn Fn(&Context, pb::RegionsChanged) -> Result<()> + Send + Sync>;
type ShutdownHook = Arc<dyn Fn(&Context, pb::Shutdown) -> Result<()> + Send + Sync>;
struct OpenedReader {
    reader: Arc<dyn Reader>,
    info: pb::ReaderOpened,
    uri: String,
}
#[derive(Default)]
struct ReaderState {
    open: Option<OpenedReader>,
    pending: Option<series::PendingReader>,
    shutting_down: bool,
}
struct OpenedExport {
    exporter: Arc<dyn Exporter>,
    info: pb::OpenExport,
}
type ShapeEndedHook = Arc<dyn Fn(&Context, pb::ShapeEditEnded) -> Result<()> + Send + Sync>;
#[derive(Default)]
pub struct Registry {
    shapers: BTreeMap<String, crate::shape::Shaper>,
    shape_ended: Option<ShapeEndedHook>,
    stages: BTreeMap<String, Arc<dyn crate::stage::Stage>>,
    commands: BTreeMap<String, Command>,
    follows: BTreeMap<String, FollowHook>,
    chunks: BTreeMap<String, ChunkSource>,
    readers: BTreeMap<String, ReaderFactory>,
    exporters: BTreeMap<String, ExportFactory>,
    reader_state: Arc<Mutex<ReaderState>>,
    // A new open must wait for an expired listing to finish releasing its file.
    pending_expiry: Arc<Mutex<()>>,
    open_exports: Mutex<BTreeMap<String, OpenedExport>>,
    sequence: AtomicU64,
    input: Option<InputHook>,
    cancel: Option<CancelHook>,
    panel: Option<PanelHook>,
    view: Option<ViewHook>,
    regions: Option<RegionsHook>,
    activated: Option<ToolHook>,
    deactivated: Option<ToolHook>,
    shutdown: Option<ShutdownHook>,
}
impl Registry {
    /// Register a computation for a manifest-declared region type.
    /// Commits can overlap previews; use the complete ShapeEdit as input.
    pub fn shaper(
        mut self,
        type_name: impl Into<String>,
        handler: impl Fn(&Context, &pb::ShapeEdit) -> Result<crate::shape::Shape>
            + Send
            + Sync
            + 'static,
    ) -> Self {
        self.shapers.insert(type_name.into(), Arc::new(handler));
        self
    }
    pub fn on_shape_edit_ended(
        mut self,
        hook: impl Fn(&Context, pb::ShapeEditEnded) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.shape_ended = Some(Arc::new(hook));
        self
    }

    fn close_readers(&self) {
        let (reader, pending) = {
            let mut state = self.reader_state.lock().unwrap();
            state.shutting_down = true;
            (state.open.take(), state.pending.take())
        };
        drop(pending);
        if let Some(reader) = reader {
            close_reader(&reader.reader);
        }
    }
    pub fn stage(mut self, id: impl Into<String>, stage: Arc<dyn crate::stage::Stage>) -> Self {
        self.stages.insert(id.into(), stage);
        self
    }
    pub fn new() -> Self {
        Self::default()
    }
    /// Serve each continuously wanted key once, off the dispatcher. None is an
    /// empty chunk. The SDK retains an answer while its arena is full.
    pub fn chunk_source(
        mut self,
        overlay: impl Into<String>,
        source: impl Fn(&Context, &pb::ChunkWant) -> Result<Option<Chunk>> + Send + Sync + 'static,
    ) -> Self {
        self.chunks.insert(overlay.into(), Arc::new(source));
        self
    }
    /// Drive a Follow offered by this panel. Use Follow::wait for interruptible dwell.
    pub fn follow(
        mut self,
        panel: impl Into<String>,
        handler: impl Fn(&Context, &crate::control::Follow) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.follows.insert(panel.into(), Arc::new(handler));
        self
    }
    pub fn command(
        mut self,
        id: impl Into<String>,
        handler: impl Fn(&Context, &pb::InvokeCommand) -> Result<pb::CommandDone>
            + Send
            + Sync
            + 'static,
    ) -> Self {
        self.commands.insert(id.into(), Arc::new(handler));
        self
    }
    pub fn reader(
        mut self,
        id: impl Into<String>,
        factory: impl Fn(&Context, &pb::OpenReader) -> Result<Arc<dyn Reader>> + Send + Sync + 'static,
    ) -> Self {
        self.readers.insert(id.into(), Arc::new(factory));
        self
    }
    pub fn exporter(
        mut self,
        id: impl Into<String>,
        factory: impl Fn(&Context, &pb::OpenExport) -> Result<Arc<dyn Exporter>> + Send + Sync + 'static,
    ) -> Self {
        self.exporters.insert(id.into(), Arc::new(factory));
        self
    }
    pub fn on_input(
        mut self,
        hook: impl Fn(&Context, Input) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.input = Some(Arc::new(hook));
        self
    }
    pub fn on_cancel(
        mut self,
        hook: impl Fn(&Context, crate::Cancellation) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.cancel = Some(Arc::new(hook));
        self
    }
    pub fn on_activated(
        mut self,
        hook: impl Fn(&Context, pb::ToolBinding) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.activated = Some(Arc::new(hook));
        self
    }
    pub fn on_deactivated(
        mut self,
        hook: impl Fn(&Context, pb::ToolBinding) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.deactivated = Some(Arc::new(hook));
        self
    }
    /// Requires the view subscription and data_access permission.
    pub fn on_view_changed(
        mut self,
        hook: impl Fn(&Context, pb::ViewChanged) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.view = Some(Arc::new(hook));
        self
    }
    /// Requires the regions subscription and data_access permission.
    pub fn on_regions_changed(
        mut self,
        hook: impl Fn(&Context, pb::RegionsChanged) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.regions = Some(Arc::new(hook));
        self
    }
    pub fn on_panel_opened(
        mut self,
        hook: impl Fn(&Context, pb::PanelOpened) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.panel = Some(Arc::new(hook));
        self
    }
    pub fn on_shutdown(
        mut self,
        hook: impl Fn(&Context, pb::Shutdown) -> Result<()> + Send + Sync + 'static,
    ) -> Self {
        self.shutdown = Some(Arc::new(hook));
        self
    }
    fn handle_id(&self, prefix: &str) -> Result<String> {
        // Rust 1.99 renamed this to try_update, unavailable on our 1.88 MSRV.
        #[allow(deprecated)]
        let n = self
            .sequence
            .fetch_update(Ordering::SeqCst, Ordering::SeqCst, |v| v.checked_add(1))
            .map_err(|_| Error::internal("handle space exhausted"))?;
        Ok(format!("{prefix}{n}"))
    }
    fn reader_for(&self, handle: &str) -> Result<(Arc<dyn Reader>, pb::ReaderOpened)> {
        self.reader_state
            .lock()
            .unwrap()
            .open
            .as_ref()
            .filter(|r| r.info.handle == handle)
            .map(|r| (r.reader.clone(), r.info.clone()))
            .ok_or_else(|| {
                eprintln!(
                    "vizi SDK: host out of step: request names unknown reader handle '{handle}'"
                );
                Error::new(
                    pb::error::Kind::NotFound,
                    format!("unknown reader handle '{handle}'"),
                )
            })
    }
    fn export_for(&self, handle: &str) -> Result<(Arc<dyn Exporter>, pb::OpenExport)> {
        self.open_exports
            .lock()
            .unwrap()
            .get(handle)
            .map(|r| (r.exporter.clone(), r.info.clone()))
            .ok_or_else(|| Error::new(pb::error::Kind::NotFound, "unknown export handle"))
    }
    fn deposit(&self, ctx: &Context, v: Volume) -> Result<Reply> {
        let mut d = pb::SegmentRef {
            len: v.bytes.len() as u64,
            shape_zyx: v.shape_zyx.to_vec(),
            dtype: v.dtype as i32,
            uses: 1,
            ..Default::default()
        };
        validate_descriptor(&d)?;
        let arenas: Vec<_> = ctx
            .memory()
            .installed()
            .into_iter()
            .filter(|i| i.purpose == "G")
            .collect();
        let id = match arenas.as_slice() {
            [arena] => arena.segment_id,
            [] => return Err(Error::protocol("no installed segment carries purpose 'G'")),
            _ => {
                return Err(Error::protocol(format!(
                    "{} installed segments carry purpose 'G'; host out of step",
                    arenas.len()
                )))
            }
        };
        let loan = ctx.memory().reserve(id, &v.bytes)?;
        d.segment_id = loan.segment_id;
        d.offset = loan.offset;
        Ok(Reply::Segment(d))
    }
}
impl Plugin for Registry {
    fn shapers(&self) -> BTreeMap<String, crate::shape::Shaper> {
        self.shapers.clone()
    }
    fn shape_edit_ended(&self, ctx: &Context, ended: pb::ShapeEditEnded) -> Result<()> {
        self.shape_ended
            .as_ref()
            .map_or(Ok(()), |hook| hook(ctx, ended))
    }

    fn follow(&self, ctx: &Context, follow: &crate::control::Follow) -> Result<()> {
        match self.follows.get(follow.panel_id()) {
            Some(handler) => handler(ctx, follow),
            None => {
                eprintln!(
                    "vizi SDK: no Follow driver for panel {:?}; ending Follow",
                    follow.panel_id()
                );
                Ok(()) // Returning ends an unhandled Follow immediately.
            }
        }
    }
    fn stages(&self) -> BTreeMap<String, Arc<dyn crate::stage::Stage>> {
        self.stages.clone()
    }
    fn chunk_sources(&self) -> BTreeMap<String, ChunkSource> {
        self.chunks.clone()
    }
    fn handle(&self, ctx: &Context, request: &mut HostReply) -> Result<Reply> {
        match request.message.clone() {
            Host::InvokeCommand(c) => {
                if !ctx
                    .core
                    .manifest
                    .contribution_ids("commands")
                    .contains(&c.command_id)
                {
                    return Err(Error::new(pb::error::Kind::NotFound, "undeclared command"));
                }
                let f = self
                    .commands
                    .get(&c.command_id)
                    .ok_or_else(|| Error::unsupported("command not registered"))?;
                let mut result = f(ctx, &c)?;
                result.command_id = c.command_id;
                Ok(Reply::CommandDone(result))
            }
            Host::OpenReader(mut open) => {
                let declared = ctx.core.manifest.contribution_ids("readers");
                if open.reader_id.is_empty() && declared.len() == 1 {
                    open.reader_id = declared.first().unwrap().clone();
                }
                let held = self.take_pending(&open);
                if !declared.contains(&open.reader_id) {
                    return Err(Error::new(
                        if open.reader_id.is_empty() {
                            pb::error::Kind::InvalidArgument
                        } else {
                            pb::error::Kind::NotFound
                        },
                        "reader_id is absent, ambiguous or undeclared",
                    ));
                }
                let factory = self
                    .readers
                    .get(&open.reader_id)
                    .ok_or_else(|| Error::unsupported("reader not registered"))?;
                // H2: a different URI is opened before replacing the current
                // reader. Only a same-URI reload closes first (exclusive locks).
                let previous = {
                    let mut state = self.reader_state.lock().unwrap();
                    if state.shutting_down {
                        drop(state);
                        if let Some(reader) = held {
                            close_reader(&reader);
                        }
                        return Err(Error::protocol("reader open during shutdown"));
                    }
                    if state.open.as_ref().is_some_and(|r| r.uri == open.uri) {
                        state.open.take()
                    } else {
                        None
                    }
                };
                if let Some(previous) = previous {
                    close_reader(&previous.reader);
                }
                let reader = match held {
                    Some(reader) => reader,
                    None => factory(ctx, &open)?,
                };
                let opened = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    series::open_series(&reader, &open)
                }))
                .unwrap_or_else(|payload| {
                    Err(crate::work::unwind_error(payload, "reader open panicked"))
                });
                let opened = match opened {
                    Ok(opened) => opened,
                    Err(error) => {
                        close_reader(&reader);
                        return Err(error);
                    }
                };
                let mut info = match opened {
                    ReaderOpen::Opened(info) => info,
                    ReaderOpen::Series(choice) => {
                        let list = match choice.into_list() {
                            Ok(list) => list,
                            Err(error) => {
                                close_reader(&reader);
                                return Err(error);
                            }
                        };
                        self.hold_pending(reader, &open, std::time::Duration::from_secs(600))?;
                        return Ok(Reply::SeriesList(list));
                    }
                };
                let info = (|| {
                    if info.shape_tczyx.len() != 5 || info.shape_tczyx.contains(&0) {
                        return Err(Error::protocol(
                            "reader shape must contain five positive TCZYX dimensions",
                        ));
                    }
                    if info.channel_names.len() as u64 > u64::from(info.shape_tczyx[1]) {
                        return Err(Error::new(pb::error::Kind::InvalidArgument, format!(
                            "open failed: the reader's ReaderInfo is not valid: channel_names names {} channels of {}",
                            info.channel_names.len(), info.shape_tczyx[1])));
                    }
                    crate::memory::dtype_bytes(info.dtype)?;
                    info.handle = self.handle_id("reader")?;
                    Ok(info)
                })();
                let info = match info {
                    Ok(info) => info,
                    Err(error) => {
                        close_reader(&reader);
                        return Err(error);
                    }
                };
                let mut state = self.reader_state.lock().unwrap();
                // A factory can finish after the concurrent shutdown worker.
                if state.shutting_down {
                    drop(state);
                    close_reader(&reader);
                    return Err(Error::protocol("reader open completed during shutdown"));
                }
                let previous = state.open.replace(OpenedReader {
                    reader,
                    info: info.clone(),
                    uri: open.uri,
                });
                // Application cleanup must not run under the registry lock.
                // Install B first so closing A cannot take B's reader slot.
                drop(state);
                if let Some(previous) = previous {
                    close_reader(&previous.reader);
                }
                Ok(Reply::ReaderOpened(info))
            }
            Host::ReadVolume(read) => {
                let (reader, info) = self.reader_for(&read.handle)?;
                level_zero(read.level)?;
                indices(&info, read.t, read.c, None)?;
                let volume = reader.read_volume(&read)?;
                reader_geometry(&volume, &info, info.shape_tczyx[2])?;
                self.deposit(ctx, volume)
            }
            Host::ReadPlane(read) => {
                let (reader, info) = self.reader_for(&read.handle)?;
                level_zero(read.level)?;
                indices(&info, read.t, read.c, Some(read.z))?;
                let volume = reader.read_plane(&read)?;
                reader_geometry(&volume, &info, 1)?;
                self.deposit(ctx, volume)
            }
            Host::Metadata(m) => Ok(Reply::Metadata(self.reader_for(&m.handle)?.0.metadata()?)),
            Host::CloseReader(c) => {
                let reader = {
                    let mut state = self.reader_state.lock().unwrap();
                    if state
                        .open
                        .as_ref()
                        .is_some_and(|r| r.info.handle == c.handle)
                    {
                        state.open.take()
                    } else {
                        None
                    }
                };
                if let Some(reader) = reader {
                    reader.reader.close()?;
                }
                Ok(Reply::Closed(pb::Closed { handle: c.handle }))
            }
            Host::OpenExport(open) => {
                if !ctx
                    .core
                    .manifest
                    .contribution_ids("exporters")
                    .contains(&open.exporter_id)
                {
                    return Err(Error::new(pb::error::Kind::NotFound, "undeclared exporter"));
                }
                if open.shape_tczyx.len() != 5 || open.shape_tczyx.contains(&0) {
                    return Err(Error::protocol(
                        "export shape must contain five positive TCZYX dimensions",
                    ));
                }
                crate::memory::dtype_bytes(open.dtype)?;
                if self.open_exports.lock().unwrap().len() >= 1024 {
                    return Err(Error::internal("open export limit exceeded"));
                }
                let f = self
                    .exporters
                    .get(&open.exporter_id)
                    .ok_or_else(|| Error::unsupported("exporter not registered"))?;
                let exporter = f(ctx, &open)?;
                let handle = self.handle_id("export")?;
                self.open_exports.lock().unwrap().insert(
                    handle.clone(),
                    OpenedExport {
                        exporter,
                        info: open,
                    },
                );
                Ok(Reply::ExportOpened(pb::ExportOpened { handle }))
            }
            Host::ExportChunk(chunk) => {
                let (exporter, info) = self.export_for(&chunk.handle)?;
                if !chunk.filtered_channels.is_empty() && info.filter.is_none() {
                    return Err(Error::new(
                        pb::error::Kind::InvalidArgument,
                        "ExportChunk.filtered_channels is nonempty but OpenExport.filter is absent",
                    ));
                }
                if let Some(channel) = chunk
                    .filtered_channels
                    .iter()
                    .find(|channel| !chunk.channels.contains(channel))
                {
                    return Err(Error::new(
                        pb::error::Kind::InvalidArgument,
                        format!(
                            "ExportChunk.filtered_channels names channel {channel} absent from ExportChunk.channels"
                        ),
                    ));
                }
                // T/C are artifact selection counts; t and channel values name
                // the source. Exporters map distinct ascending t values by rank.
                if chunk.channels.is_empty()
                    || chunk.channels.len() as u64 > u64::from(info.shape_tczyx[1])
                {
                    return Err(Error::new(
                        pb::error::Kind::OutOfBounds,
                        "export channel count out of bounds",
                    ));
                }
                let expected = info.shape_tczyx[2..]
                    .iter()
                    .fold(
                        crate::memory::dtype_bytes(info.dtype)?
                            .checked_mul(chunk.channels.len() as u64),
                        |n, d| n.and_then(|n| n.checked_mul(*d as u64)),
                    )
                    .ok_or_else(|| Error::protocol("export size overflow"))?;
                let bytes = match chunk.source {
                    Some(pb::export_chunk::Source::Inline(v)) => v,
                    Some(pb::export_chunk::Source::Segment(d)) => {
                        if ctx
                            .memory()
                            .installed()
                            .iter()
                            .find(|i| i.segment_id == d.segment_id)
                            .is_none_or(|i| i.purpose != "X")
                        {
                            return Err(Error::protocol("export descriptor must name purpose X"));
                        }
                        // A slab contains channels × volumes; SegmentRef's
                        // shape describes one volume, so validate at slab scope.
                        if d.dtype != info.dtype
                            || d.shape_zyx != info.shape_tczyx[2..]
                            || d.len != expected
                        {
                            return Err(Error::protocol("export descriptor geometry mismatch"));
                        }
                        request.copy_export_payload()?.bytes
                    }
                    None => return Err(Error::protocol("export chunk has no source")),
                };
                if bytes.len() as u64 != expected {
                    return Err(Error::protocol(
                        "export length does not equal channels × shape × dtype",
                    ));
                }
                let written = exporter.write_chunk(&ExportChunk {
                    t: chunk.t,
                    channels: &chunk.channels,
                    bytes: &bytes,
                    filtered_channels: &chunk.filtered_channels,
                    filter: info.filter.as_ref(),
                })?;
                if written > expected {
                    return Err(Error::protocol(
                        "exporter reported more bytes than supplied",
                    ));
                }
                Ok(Reply::ChunkWritten(pb::ChunkWritten {
                    handle: chunk.handle,
                    total_bytes: written,
                }))
            }
            Host::FinishExport(f) => {
                // Finishing consumes the handle even if finalization fails or
                // panics. Do not retain a now-closed exporter in the registry.
                let opened = self
                    .open_exports
                    .lock()
                    .unwrap()
                    .remove(&f.handle)
                    .ok_or_else(|| {
                        Error::new(
                            pb::error::Kind::NotFound,
                            format!("unknown export handle '{}'", f.handle),
                        )
                    })?;
                let mut done = opened.exporter.finish()?;
                done.handle = f.handle.clone();
                Ok(Reply::ExportDone(done))
            }
            Host::AbortExport(a) => {
                let (exporter, _) = self.export_for(&a.handle)?;
                if let Err(e) = exporter.abort() {
                    eprintln!("vizi SDK: export abandoned; cleanup error: {e}");
                }
                self.open_exports.lock().unwrap().remove(&a.handle);
                Ok(Reply::ExportDone(pb::ExportDone { handle: a.handle }))
            }
            _ => Err(Error::unsupported("not a contribution request")),
        }
    }
    fn input(&self, ctx: &Context, input: Input) -> Result<()> {
        if let Some(f) = &self.input {
            f(ctx, input)
        } else {
            Ok(())
        }
    }
    fn cancelled(&self, ctx: &Context, c: crate::Cancellation) -> Result<()> {
        if let Some(f) = &self.cancel {
            f(ctx, c)
        } else {
            Ok(())
        }
    }
    fn activated(&self, ctx: &Context, binding: pb::ToolBinding) -> Result<()> {
        self.activated.as_ref().map_or(Ok(()), |f| f(ctx, binding))
    }
    fn deactivated(&self, ctx: &Context, binding: pb::ToolBinding) -> Result<()> {
        self.deactivated
            .as_ref()
            .map_or(Ok(()), |f| f(ctx, binding))
    }
    fn view_changed(&self, ctx: &Context, change: pb::ViewChanged) -> Result<()> {
        self.view.as_ref().map_or(Ok(()), |hook| hook(ctx, change))
    }
    fn regions_changed(&self, ctx: &Context, change: pb::RegionsChanged) -> Result<()> {
        self.regions
            .as_ref()
            .map_or(Ok(()), |hook| hook(ctx, change))
    }
    fn panel_opened(&self, ctx: &Context, p: pb::PanelOpened) -> Result<()> {
        if let Some(f) = &self.panel {
            f(ctx, p)
        } else {
            Ok(())
        }
    }
    fn shutdown(&self, ctx: &Context, s: pb::Shutdown) -> Result<pb::shutdown_report::Outcome> {
        self.close_readers();
        if let Some(f) = &self.shutdown {
            f(ctx, s)?;
            Ok(pb::shutdown_report::Outcome::Completed)
        } else {
            Ok(pb::shutdown_report::Outcome::NoHook)
        }
    }
    fn session_ended(&self) {
        self.close_readers();
    }
}
fn close_reader(reader: &Arc<dyn Reader>) {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| reader.close())) {
        Ok(Ok(())) => {}
        Ok(Err(error)) if error.kind == pb::error::Kind::Cancelled => {}
        Ok(Err(error)) => eprintln!("vizi SDK: reader closed; cleanup error: {error}"),
        Err(payload) if payload.is::<crate::work::Cancelled>() => {}
        Err(_) => eprintln!("vizi SDK: reader closed; cleanup panicked"),
    }
}
fn reader_geometry(volume: &Volume, info: &pb::ReaderOpened, depth: u32) -> Result<()> {
    if volume.shape_zyx != [depth, info.shape_tczyx[3], info.shape_tczyx[4]]
        || volume.dtype as i32 != info.dtype
    {
        return Err(Error::protocol(
            "reader payload geometry differs from ReaderOpened",
        ));
    }
    Ok(())
}
fn level_zero(level: u32) -> Result<()> {
    if level != 0 {
        return Err(Error::new(
            pb::error::Kind::OutOfBounds,
            format!("reader level {level} out of bounds; only level 0 is supported"),
        ));
    }
    Ok(())
}
fn indices(info: &pb::ReaderOpened, t: u32, c: u32, z: Option<u32>) -> Result<()> {
    for (axis, index, bound) in [
        ("t", t, info.shape_tczyx[0]),
        ("c", c, info.shape_tczyx[1]),
        ("z", z.unwrap_or(0), info.shape_tczyx[2]),
    ] {
        if index >= bound {
            return Err(Error::new(
                pb::error::Kind::OutOfBounds,
                format!("reader {axis} index {index} out of bounds (size {bound})"),
            ));
        }
    }
    Ok(())
}
impl Context {
    pub fn segment(&self, purpose: &str, subject: &str) -> Result<pb::InstallSegment> {
        self.memory()
            .installed()
            .into_iter()
            .filter(|i| i.purpose == purpose && i.subject == subject)
            .max_by_key(|i| i.segment_id)
            .ok_or_else(|| {
                Error::internal(format!("no installed {purpose} segment for '{subject}'"))
            })
    }
    pub fn patch(&self, panel: &str, ops: Vec<pb::PatchOp>) -> Result<()> {
        if !self
            .core
            .manifest
            .contribution_ids("panels")
            .contains(panel)
        {
            return Err(Error::new(pb::error::Kind::NotFound, "undeclared panel"));
        }
        self.push(Reply::PanelPatch(pb::PanelPatch {
            panel_id: panel.into(),
            ops,
        }))
    }
    pub fn push_image(&self, panel: &str, widget: &str, png: &[u8]) -> Result<()> {
        self.push_image_row(panel, widget, "", png)
    }
    /// Publish an image in a list cell; an empty row_key addresses an ordinary widget.
    pub fn push_image_row(
        &self,
        panel: &str,
        widget: &str,
        row_key: &str,
        png: &[u8],
    ) -> Result<()> {
        self.hold_or_push_image(panel, widget, row_key, png)
    }

    pub fn set_overlay(&self, update: pb::OverlayUpdate) -> Result<()> {
        if !self
            .core
            .manifest
            .contribution_ids("overlays")
            .contains(&update.overlay_id)
        {
            return Err(Error::new(pb::error::Kind::NotFound, "undeclared overlay"));
        }
        self.push(Reply::OverlayUpdate(update))
    }
    /// The caller supplies a monotonic overlay revision shared with control-plane
    /// updates. It is distinct from the Pulsed lane's internal publication count.
    pub fn set_overlay_live(
        &self,
        update: &pb::OverlayUpdate,
        pane: Option<crate::PaneEpoch>,
    ) -> Result<bool> {
        if !self
            .core
            .manifest
            .contribution_ids("overlays")
            .contains(&update.overlay_id)
        {
            return Err(Error::new(pb::error::Kind::NotFound, "undeclared overlay"));
        }
        for item in &update.items {
            for buffer in buffers(item) {
                if !matches!(&buffer.source,Some(pb::geom_buffer::Source::Inline(v)) if v.len()<=crate::control::MAX_INLINE_BYTES)
                {
                    return Err(Error::protocol(
                        "live overlay geometry must be inline and at most 8 MiB",
                    ));
                }
            }
        }
        let published =
            self.memory()
                .publish_overlay(&update.overlay_id, &update.encode_to_vec(), pane)?;
        if let Some((segment_id, region)) = published {
            let mut mask = crate::RegionMask::default();
            mask.set(region as usize)
                .map_err(|e| Error::protocol(e.to_string()))?;
            self.notify(&crate::Notification {
                segment_id,
                mask,
                pane,
            })?;
        }
        Ok(published.is_some())
    }
}
fn buffers(item: &pb::OverlayItem) -> Vec<&pb::GeomBuffer> {
    use pb::overlay_item::Geometry::*;
    match item.geometry.as_ref() {
        Some(LineSet(v)) => v.positions_zyx.iter().chain(v.colors_rgba.iter()).collect(),
        Some(Marker(v)) => v.positions_zyx.iter().collect(),
        Some(LabelVolume(v)) => v.labels.iter().chain(v.colormap.iter()).collect(),
        _ => vec![],
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct VolumeReader(Volume);
    impl Reader for VolumeReader {
        fn info(&self) -> Result<pb::ReaderOpened> {
            unreachable!()
        }
        fn read_volume(&self, request: &pb::ReadVolume) -> Result<Volume> {
            assert_eq!(
                (&*request.handle, request.t, request.c, request.level),
                ("fixture", 3, 4, 0)
            );
            Ok(self.0.clone())
        }
    }

    #[test]
    fn default_plane_slices_unpadded_multibyte_volume_and_checks_bounds() {
        let mut reader = VolumeReader(Volume {
            bytes: (0..24).collect(),
            shape_zyx: [2, 2, 3],
            dtype: pb::DType::DtypeU16,
        });
        let request = pb::ReadPlane {
            handle: "fixture".into(),
            t: 3,
            c: 4,
            z: 1,
            level: 0,
        };
        let plane = reader.read_plane(&request).unwrap();
        assert_eq!(plane.shape_zyx, [1, 2, 3]);
        assert_eq!(plane.dtype, pb::DType::DtypeU16);
        assert_eq!(plane.bytes, (12..24).collect::<Vec<u8>>());
        assert_eq!(
            reader
                .read_plane(&pb::ReadPlane {
                    z: 2,
                    ..request.clone()
                })
                .unwrap_err()
                .kind,
            pb::error::Kind::OutOfBounds
        );
        assert_eq!(
            reader
                .read_plane(&pb::ReadPlane {
                    level: 1,
                    ..request.clone()
                })
                .unwrap_err()
                .kind,
            pb::error::Kind::OutOfBounds
        );
        reader.0.bytes.pop();
        assert!(
            reader.read_plane(&request).is_err(),
            "malformed volume must not be sliced"
        );
    }
}
