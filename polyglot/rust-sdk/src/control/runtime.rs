use super::*;
use crate::{plugin::Plugin as Application, DecodedRecord, Disposition, Outcome, Terminal};
use std::{pin::Pin, time::Instant};
use tokio_stream::{wrappers::ReceiverStream, Stream};

enum Event {
    Input(Input),
    ShapeEnded(pb::ShapeEditEnded),
    Cancellation(crate::Cancellation),
    Panel(pb::PanelOpened),
    PanelImages(String),
    Tool(pb::ToolBinding),
    Pulsed(u64, u32, Payload),
}
/// Owns the doorbell listener, independent application workers, and one stream.
#[derive(Clone)]
pub struct Runtime {
    core: Arc<Core>,
    application: Arc<dyn Application>,
    events: std::sync::mpsc::SyncSender<Event>,
}
impl Runtime {
    pub fn new(
        manifest: crate::manifest::Manifest,
        application: Arc<dyn Application>,
        bell: Doorbell,
        listener: crate::Listener,
    ) -> Result<Self> {
        let identity = format!(
            "Ready; I am {} {}; rust-sdk {}",
            manifest.id(),
            manifest.version(),
            env!("CARGO_PKG_VERSION")
        );
        validate_identity(&identity, "Ready; I am ")?;
        let chunks = chunks::Chunks::new(&manifest, application.chunk_sources())?;
        let stages = stage_runtime::Stages::new(&manifest, application.stages())?;
        let shapes = shape::Shapes::new(&manifest, application.shapers())?;
        let session_token = Mutex::new(bell.session_token());
        let core = Arc::new(Core {
            session_token,
            shapes,
            images: Mutex::new(Default::default()),
            tables: Mutex::new(Default::default()),
            follows: Mutex::new(BTreeMap::new()),
            stages,
            chunks,
            subscriptions: subscriptions::Subscriptions::new(&manifest),
            memory_flush: Mutex::new(()),
            memory: Memory::new(Default::default(), Some(&manifest)),
            manifest,
            bell,
            queues: Mutex::new(Queues::default()),
            wake: Notify::new(),
            pending: Mutex::new(BTreeMap::new()),
            work: Mutex::new(work::Registry::default()),
            sequence: AtomicU64::new(1),
            arrangements: Mutex::new(Default::default()),
            host_identity: Mutex::new(None),
            peer: Mutex::new(None),
            shutting_down: AtomicBool::new(false),
            end_reason: Mutex::new(None),
            shutdown_started: Notify::new(),
            doorbell_shutdown: AtomicBool::new(false),
            started: AtomicBool::new(false),
            ended: AtomicBool::new(false),
            done: Notify::new(),
            unusable_inputs: AtomicU64::new(0),
            deferred_actions: AtomicUsize::new(0),
        });
        let (tx, rx) = std::sync::mpsc::sync_channel(256);
        let worker_core = core.clone();
        let app = application.clone();
        std::thread::Builder::new()
            .name("vizi-events".into())
            .spawn(move || {
                let ctx = Context {
                    core: worker_core.clone(),
                    after_reply: None,
                    work: None,
                };
                while let Ok(event) = rx.recv() {
                    if worker_core.ended.load(Ordering::SeqCst) {
                        break;
                    }
                    let result =
                        std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| match event {
                            Event::ShapeEnded(v) => app.shape_edit_ended(&ctx, v),
                            Event::Input(v) => app.input(&ctx, v),
                            Event::Cancellation(v) => app.cancelled(&ctx, v),
                            Event::Panel(v) => {
                                // Old held state precedes any update from the hook.
                                ctx.flush_panel_images(&v.panel_id);
                                app.panel_opened(&ctx, v)
                            }
                            Event::PanelImages(panel) => {
                                ctx.flush_panel_images(&panel);
                                Ok(())
                            }
                            Event::Tool(v) if v.retired => app.deactivated(&ctx, v),
                            Event::Tool(v) => app.activated(&ctx, v),
                            Event::Pulsed(id, r, p) => app.pulsed(&ctx, id, r, p),
                        }));
                    match result {
                        Ok(Ok(())) => {}
                        // A host call made by an already-running callback may
                        // be canceled by teardown. Its author can use `?`
                        // without turning normal shutdown into a diagnostic.
                        Ok(Err(_)) if ctx.is_shutting_down() => {}
                        Ok(Err(e)) => eprintln!("vizi SDK callback error: {e}"),
                        Err(_) => {
                            worker_core.fault("application event callback panicked");
                            break;
                        }
                    }
                }
            })?;
        let runtime = Self {
            core,
            application,
            events: tx,
        };
        if let Err(error) = runtime.core.chunks.start(&runtime.context()) {
            runtime.core.finish();
            return Err(error);
        }
        if let Err(error) = runtime.core.stages.start(&runtime.context()) {
            runtime.core.finish();
            return Err(error);
        }
        if let Err(error) = runtime
            .core
            .subscriptions
            .start(&runtime.context(), &runtime.application)
        {
            runtime.core.finish();
            return Err(error);
        }
        let recv = runtime.clone();
        // Spawn before bind/listen can expose control-plane readiness.
        std::thread::Builder::new()
            .name("vizi-doorbell".into())
            .spawn(move || {
                let result = listener.run(|outcome| recv.doorbell(outcome));
                if let Err(e) = result {
                    recv.core.fault(format!("doorbell listener failed: {e:?}"));
                }
            })?;
        let poller = runtime.clone();
        std::thread::Builder::new()
            .name("vizi-memory-poll".into())
            .spawn(move || {
                while !poller.core.ended.load(Ordering::SeqCst) {
                    if !poller.core.shutting_down.load(Ordering::SeqCst) {
                        for segment in poller.core.memory.installed() {
                            for region in segment.regions {
                                if region.discipline == 3 && region.producer == 1 {
                                    if let Ok(Some(payload)) = poller
                                        .core
                                        .memory
                                        .read_pulsed(segment.segment_id, region.index)
                                    {
                                        poller.event(Event::Pulsed(
                                            segment.segment_id,
                                            region.index,
                                            payload,
                                        ));
                                    }
                                }
                            }
                        }
                    }
                    // Receiver-owned rate limit, independent of all D traffic.
                    std::thread::sleep(Duration::from_millis(10));
                }
                if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    poller.application.session_ended()
                }))
                .is_err()
                {
                    eprintln!("vizi SDK: session-end cleanup panicked");
                }
            })?;
        Ok(runtime)
    }
    pub fn context(&self) -> Context {
        Context {
            core: self.core.clone(),
            after_reply: None,
            work: None,
        }
    }
    /// One-time adoption of the launcher's inherited descriptor.
    ///
    /// # Safety
    /// On Unix, if VIZI_PLUGIN_DOORBELL_FD names an open descriptor, it must be
    /// owned exclusively by this call and not closed or replaced concurrently.
    /// A closed descriptor produces a launch diagnostic. Do not call this twice
    /// for the same launch.
    pub unsafe fn from_env(
        manifest: crate::manifest::Manifest,
        application: Arc<dyn Application>,
    ) -> Result<Self> {
        #[cfg(unix)]
        let (bell, listener) = unsafe { Doorbell::from_env(Default::default()) }?;
        #[cfg(windows)]
        let (bell, listener) = Doorbell::from_env(Default::default())?;
        Self::new(manifest, application, bell, listener)
    }
    pub fn service(&self) -> pb::plugin_server::PluginServer<Self> {
        pb::plugin_server::PluginServer::new(self.clone())
            .max_decoding_message_size(MAX_MESSAGE_BYTES)
            .max_encoding_message_size(MAX_MESSAGE_BYTES)
    }
    pub async fn serve_env(self) -> Result<()> {
        let _cleanup = Cleanup(self.core.clone());
        #[cfg(unix)]
        {
            let path = std::env::var_os("VIZI_PLUGIN_UDS")
                .ok_or_else(|| Error::protocol("VIZI_PLUGIN_UDS missing"))?;
            self.serve_unix(std::path::PathBuf::from(path)).await
        }
        #[cfg(windows)]
        {
            let path = std::env::var_os("VIZI_PLUGIN_PORT_FILE")
                .ok_or_else(|| Error::protocol("VIZI_PLUGIN_PORT_FILE missing"))?;
            let listener =
                tokio::net::TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).await?;
            let path = std::path::PathBuf::from(path);
            let temp = path.with_extension(format!("{}.tmp", std::process::id()));
            tokio::fs::write(&temp, listener.local_addr()?.port().to_string()).await?;
            tokio::fs::rename(temp, &path).await?;
            let result = self.serve_tcp(listener).await;
            let _ = tokio::fs::remove_file(path).await;
            result
        }
    }
    /// Requires every stream to prove `token` before it becomes the session:
    /// in call metadata under [`super::SESSION_TOKEN_METADATA_KEY`], checked
    /// before the stream is accepted, or else in `Hello.session_token`. A
    /// Windows runtime takes its doorbell's token without this call.
    pub fn require_session_token(&self, token: impl Into<String>) {
        *self.core.session_token.lock().unwrap() = Some(token.into());
    }
    /// Serves the session on an already bound loopback TCP listener, the
    /// Windows transport. `serve_env` publishes the port file and calls this.
    pub async fn serve_tcp(self, listener: tokio::net::TcpListener) -> Result<()> {
        let _cleanup = Cleanup(self.core.clone());
        let core = self.core.clone();
        let result = tonic::transport::Server::builder()
            .add_service(self.service())
            .serve_with_incoming_shutdown(
                tokio_stream::wrappers::TcpListenerStream::new(listener),
                async move { core.wait_done().await },
            )
            .await;
        self.core.finish();
        result.map_err(|e| Error::internal(e.to_string()))
    }
    #[cfg(unix)]
    pub async fn serve_unix(self, path: impl AsRef<std::path::Path>) -> Result<()> {
        let _cleanup = Cleanup(self.core.clone());
        use std::os::unix::fs::PermissionsExt;
        let path = path.as_ref().to_path_buf();
        // Never unlink an existing endpoint that might belong to another run.
        let listener = tokio::net::UnixListener::bind(&path)?;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600))?;
        let core = self.core.clone();
        let result = tonic::transport::Server::builder()
            .add_service(self.service())
            .serve_with_incoming_shutdown(
                tokio_stream::wrappers::UnixListenerStream::new(listener),
                async move { core.wait_done().await },
            )
            .await;
        self.core.finish();
        let _ = tokio::fs::remove_file(path).await;
        result.map_err(|e| Error::internal(e.to_string()))
    }
    /// Authoritative local teardown; useful to embedders and tests.
    pub fn stop(&self) {
        self.core.finish();
    }
    fn event(&self, event: Event) {
        if self.events.try_send(event).is_err() {
            self.core
                .fault("application event queue is not draining (capacity 256)");
        }
    }
    fn panel_images_ready(&self, id: u64) {
        if let Some(segment) = self
            .core
            .memory
            .installed()
            .into_iter()
            .find(|i| i.segment_id == id && i.purpose == "P")
        {
            self.event(Event::PanelImages(segment.subject));
        }
    }
    fn doorbell(&self, outcome: &mut Outcome) {
        if matches!(outcome, Outcome::Frame(_)) && self.core.peer.lock().unwrap().is_none() {
            self.core.fault("doorbell record before Hello/Ready");
            return;
        }
        match outcome {
            Outcome::Frame(frame) => {
                match &mut frame.disposition {
                    #[cfg(unix)]
                    Disposition::Decoded(DecodedRecord::Memory { grant, descriptor }) => {
                        if let Some(fd) = descriptor.take() {
                            if let Err(e) = self.core.memory.grant(
                                grant.clone(),
                                frame.capture.as_slice().try_into().unwrap(),
                                fd,
                            ) {
                                self.core.fault(e.detail);
                            }
                        }
                        self.core.flush_memory();
                        self.panel_images_ready(grant.segment_id);
                    }
                    #[cfg(windows)]
                    Disposition::Decoded(DecodedRecord::NamedMemory { grant, name }) => {
                        if let Err(e) = self.core.memory.grant_named(
                            grant.clone(),
                            frame.capture.as_slice().try_into().unwrap(),
                            name,
                        ) {
                            self.core.fault(e.detail);
                        }
                        self.core.flush_memory();
                        self.panel_images_ready(grant.segment_id);
                    }
                    Disposition::Decoded(DecodedRecord::Input {
                        event,
                        implicitly_closed,
                    }) => {
                        let input = self.core.input(event.clone(), *implicitly_closed);
                        if input.binding.as_ref().is_some_and(|b| {
                            !self
                                .core
                                .manifest
                                .contribution_ids("tools")
                                .contains(&b.tool_id)
                        }) {
                            eprintln!("vizi SDK: input for undeclared tool {} ignored", event.tool);
                            return;
                        }
                        if !input.usable_context {
                            let count =
                                self.core.unusable_inputs.fetch_add(1, Ordering::Relaxed) + 1;
                            if count.is_power_of_two() {
                                eprintln!("vizi SDK: {count} inputs with unusable context; latest pane {}",event.view_id);
                            }
                        }
                        self.event(Event::Input(input));
                    }
                    Disposition::Decoded(DecodedRecord::Notification(n)) => {
                        self.core
                            .memory
                            .notification_record(n, frame.capture.clone());
                        self.core.stages.hint(n.segment_id);
                    }
                    Disposition::Rejected(crate::Rejection::Grant {
                        segment_id,
                        violations,
                    }) => {
                        if let Err(e) = self.core.memory.reject_grant_record(
                            *segment_id,
                            format!("invalid M: {violations:?}"),
                            frame.capture.as_slice().try_into().ok(),
                        ) {
                            self.core.fault(e.detail);
                        }
                        self.core.flush_memory();
                    }
                    Disposition::Rejected(crate::Rejection::Contract(v)) => {
                        use crate::Violation::*;
                        let site = match v {
                            ZeroId => 4,
                            SeqOnOpen => 13,
                            PositionTail => 14,
                            NonfinitePosition => 15,
                            MissWithPicked => 5,
                            InvalidTarget => 16,
                            InteractionMismatch => 2,
                            UnknownTool => 6,
                            EpochMismatch => 1,
                        };
                        if matches!(v, ZeroId) && frame.capture.first() == Some(&b'M') {
                            if let Err(e) =
                                self.core
                                    .push(Plugin::ContractViolation(pb::ContractViolation {
                                        r#where: 4,
                                        detail: "M has zero segment id".into(),
                                        record: frame.capture.clone(),
                                        count: 1,
                                    }))
                            {
                                self.core.fault(e.message);
                            }
                        } else {
                            self.core.memory.report(
                                site,
                                format!("input contract violation: {v:?}"),
                                frame.capture.clone(),
                            );
                        }
                    }
                    _ => {}
                }
            }
            Outcome::Cancellation(c) => self.event(Event::Cancellation(c.clone())),
            Outcome::Terminal(_) => {} // reported once through the terminal mailbox
        }
    }
}
impl Core {
    async fn wait_shutdown(&self) {
        let notified = self.shutdown_started.notified();
        if !self.shutting_down.load(Ordering::SeqCst) {
            notified.await;
        }
    }
    /// False means the output channel stopped making progress. A canceled
    /// request is successfully discarded, including while output is blocked.
    pub(super) async fn write_outbound(
        &self,
        output: &tokio::sync::mpsc::Sender<std::result::Result<pb::PluginMsg, tonic::Status>>,
        message: pb::PluginMsg,
        deadline: tokio::time::Instant,
    ) -> bool {
        if let Some(Plugin::ShapeProposal(result) | Plugin::ShapeAnswer(result)) = &message.msg {
            let permit = tokio::select! {
                biased;
                _ = self.wait_shutdown() => return true,
                result = tokio::time::timeout_at(deadline, output.reserve()) => match result {
                    Ok(Ok(permit)) => permit,
                    _ => return false,
                },
            };
            let commit = matches!(&message.msg, Some(Plugin::ShapeAnswer(_)));
            self.shapes.if_current(result, commit, || {
                permit.send(Ok(message.clone()));
            });
            return true;
        }
        let loans = bounds::plugin(&message).unwrap_or_default();
        if message.msg.as_ref().and_then(request_kind).is_none() {
            let sent = matches!(
                tokio::time::timeout_at(deadline, output.send(Ok(message))).await,
                Ok(Ok(()))
            );
            if sent && !loans.is_empty() {
                self.memory.loans_sent(&loans);
                self.flush_memory();
            }
            return sent;
        }
        // Never hold the queue mutex across an await. Waiting for capacity
        // must itself be cancellable, rather than costing another five seconds.
        let permit = tokio::select! {
            biased;
            _ = self.wait_shutdown() => return true,
            result = tokio::time::timeout_at(deadline, output.reserve()) => match result {
                Ok(Ok(permit)) => permit,
                _ => return false,
            },
        };
        let admission = self.queues.lock().unwrap();
        if !admission.closed && !self.shutting_down.load(Ordering::SeqCst) {
            permit.send(Ok(message));
        }
        drop(admission);
        if !loans.is_empty() {
            self.memory.loans_sent(&loans);
            self.flush_memory();
        }
        true
    }
    async fn wait_done(&self) {
        let notified = self.done.notified();
        if !self.ended.load(Ordering::SeqCst) {
            notified.await;
        }
    }
    fn fault_report(&self, f: &crate::Fault) -> pb::DoorbellFault {
        use crate::FailureClass::*;
        let class = match f.failure_class {
            BadCrc => 1,
            BadLength => 2,
            BadVer => 3,
            BadFlags => 4,
            BadReserved => 5,
            BadPadding => 6,
            Ctrunc => 7,
            WrongFdCount => 8,
            ReservedOp => 9,
            ShortWrite => 11,
            AmbiguousSend => 12,
            RecvError => 13,
        };
        let peer = *self.peer.lock().unwrap();
        pb::DoorbellFault {
            failure_class: class,
            capture: f.capture.clone(),
            observed_len: f.observed_len.map(|n| n as u64),
            msg_ctrunc: f.msg_ctrunc,
            ordinal: f.ordinal,
            os_error: f.os_error.map(|n| n as u32),
            expected_crc: f.expected_crc,
            observed_crc: f.observed_crc,
            transport_class: match f.transport_class {
                crate::TransportClass::Seqpacket => "seqpacket",
                crate::TransportClass::Datagram => "dgram",
                crate::TransportClass::Stream => "tcp",
            }
            .into(),
            direction: format!("{:?}", f.direction),
            detail: f.detail.clone(),
            plugin_id: Some(self.manifest.id().into()),
            sdk_version: Some(env!("CARGO_PKG_VERSION").into()),
            peer_protocol_major: peer.map(|p| p.0),
            peer_protocol_minor: peer.map(|p| p.1),
            // Local supervisor observations when it detects the terminal
            // mailbox report; neither asserts that the host listener is alive.
            process_healthy_at_detection: Some(true),
            stream_healthy_at_detection: Some(
                peer.is_some()
                    && !self.ended.load(Ordering::SeqCst)
                    && !self.queues.lock().unwrap().closed,
            ),
            ..Default::default()
        }
    }
}
fn validate_identity(id: &str, prefix: &str) -> Result<()> {
    if id.len() > 2048
        || !id.starts_with(prefix)
        || id.len() == prefix.len()
        || id.chars().any(char::is_control)
    {
        return Err(Error::protocol(format!(
            "invalid handshake identity {:?}",
            id.chars().take(160).collect::<String>()
        )));
    }
    Ok(())
}
pub(super) fn is_request(msg: &Host) -> bool {
    matches!(
        msg,
        Host::PlanStage(_)
            | Host::OpenReader(_)
            | Host::ReadVolume(_)
            | Host::ReadPlane(_)
            | Host::Metadata(_)
            | Host::CloseReader(_)
            | Host::InvokeCommand(_)
            | Host::OpenExport(_)
            | Host::ExportChunk(_)
            | Host::FinishExport(_)
            | Host::AbortExport(_)
    )
}
fn valid_reply(request: &Host, reply: &Plugin) -> bool {
    matches!(reply, Plugin::Error(_))
        || matches!(
            (request, reply),
            (Host::PlanStage(_), Plugin::StagePlanned(_))
                | (Host::OpenReader(_), Plugin::ReaderOpened(_))
                | (Host::OpenReader(_), Plugin::SeriesList(_))
                | (Host::ReadVolume(_) | Host::ReadPlane(_), Plugin::Segment(_))
                | (Host::Metadata(_), Plugin::Metadata(_))
                | (Host::CloseReader(_), Plugin::Closed(_))
                | (Host::InvokeCommand(_), Plugin::CommandDone(_))
                | (Host::OpenExport(_), Plugin::ExportOpened(_))
                | (Host::ExportChunk(_), Plugin::ChunkWritten(_))
                | (
                    Host::FinishExport(_) | Host::AbortExport(_),
                    Plugin::ExportDone(_)
                )
        )
}
// Work is registered on dispatch, including while a descriptor waits for its grant.
struct Incoming {
    message: pb::HostMsg,
    work: Option<work::RequestRegistration>,
}
struct Job {
    work: work::RequestRegistration,
    request: HostReply,
}
struct Completed {
    work: work::RequestRegistration,
    result: Result<Plugin>,
    after_reply: Option<DeferredAction>,
}
struct Shutdown {
    deadline: Instant,
    result: std::sync::mpsc::Receiver<pb::ShutdownReport>,
    report: Option<pb::ShutdownReport>,
}
struct Cleanup(Arc<Core>);
impl Drop for Cleanup {
    fn drop(&mut self) {
        self.0.finish();
    }
}

#[tonic::async_trait]
impl pb::plugin_server::Plugin for Runtime {
    type SessionStream =
        Pin<Box<dyn Stream<Item = std::result::Result<pb::PluginMsg, tonic::Status>> + Send>>;
    async fn session(
        &self,
        request: tonic::Request<tonic::Streaming<pb::HostMsg>>,
    ) -> std::result::Result<tonic::Response<Self::SessionStream>, tonic::Status> {
        // The token in call metadata is checked before the stream is accepted.
        // Only an authenticated stream claims the session; a refused one never
        // ends it, and the plugin keeps accepting streams for the real host.
        let expected = self.core.session_token.lock().unwrap().clone();
        let claimed =
            match token::check_metadata(expected.as_deref(), request.metadata()).map_err(|s| *s)? {
                token::MetadataAuth::Authenticated => {
                    self.claim().map_err(|s| *s)?;
                    true
                }
                token::MetadataAuth::Absent => false,
            };
        let (output_tx, output_rx) = tokio::sync::mpsc::channel(1);
        let runtime = self.clone();
        tokio::spawn(async move {
            if let Err((status, claimed)) = runtime
                .start_session(request.into_inner(), output_tx.clone(), claimed)
                .await
            {
                let _ = output_tx.send(Err(*status)).await;
                if claimed {
                    runtime.core.finish();
                }
            }
        });
        // Return headers before reading Hello. Ready and application admission
        // still require the checked handshake in start_session (§3.1).
        Ok(tonic::Response::new(Box::pin(ReceiverStream::new(
            output_rx,
        ))))
    }
}
impl Runtime {
    fn claim(&self) -> std::result::Result<(), Box<tonic::Status>> {
        if self.core.started.swap(true, Ordering::SeqCst) {
            return Err(Box::new(tonic::Status::already_exists(
                "one session per plugin process",
            )));
        }
        Ok(())
    }
    /// An error carries whether this stream had claimed the session: only a
    /// claimed stream's failure ends it.
    async fn start_session(
        &self,
        mut stream: tonic::Streaming<pb::HostMsg>,
        output_tx: tokio::sync::mpsc::Sender<std::result::Result<pb::PluginMsg, tonic::Status>>,
        claimed: bool,
    ) -> std::result::Result<(), (Box<tonic::Status>, bool)> {
        // End a pending handshake when the session stops or the RPC is
        // canceled, rather than waiting for the plugin's Hello timeout. This
        // timeout on the host is independent of the host's launch deadline.
        let first = tokio::select! {
            result = self.read_hello(&mut stream) => result.map_err(|s| (s, claimed))?,
            _ = self.core.wait_done() => return Ok(()),
            _ = output_tx.closed() => {
                if claimed {
                    self.core.finish();
                }
                return Ok(());
            }
        };
        if !claimed {
            // No token in metadata: a host that sends it only in Hello.
            let expected = self.core.session_token.lock().unwrap().clone();
            let offered = match &first.msg {
                Some(Host::Hello(hello)) => hello.session_token.as_str(),
                _ => "",
            };
            if !expected.is_some_and(|e| token::tokens_equal(offered.as_bytes(), e.as_bytes())) {
                return Err((Box::new(token::unauthenticated()), false));
            }
            self.claim().map_err(|s| (s, false))?;
        }
        self.handshake(first).map_err(|s| (s, true))?;
        self.start_writer(output_tx);
        let runtime = self.clone();
        tokio::spawn(async move {
            runtime.dispatch(stream).await;
        });
        Ok(())
    }
    async fn read_hello(
        &self,
        stream: &mut tonic::Streaming<pb::HostMsg>,
    ) -> std::result::Result<pb::HostMsg, Box<tonic::Status>> {
        let first = match tokio::time::timeout(Duration::from_secs(10), stream.message()).await {
            Ok(Ok(Some(m))) => m,
            _ => {
                return Err(Box::new(tonic::Status::invalid_argument(
                    "Hello was not received within 10 seconds",
                )));
            }
        };
        Ok(first)
    }
    fn handshake(&self, first: pb::HostMsg) -> std::result::Result<(), Box<tonic::Status>> {
        let ready_id = format!(
            "Ready; I am {} {}; rust-sdk {}",
            self.core.manifest.id(),
            self.core.manifest.version(),
            env!("CARGO_PKG_VERSION")
        );
        let handshake = (|| -> Result<()> {
            let Some(Host::Hello(hello)) = &first.msg else {
                return Err(Error::protocol("first message must be Hello"));
            };
            if hello.protocol_major != features::PROTOCOL_MAJOR {
                return Err(Error::protocol(format!(
                    "SDK protocol 1.0 incompatible with host {}.{}",
                    hello.protocol_major, hello.protocol_minor
                )));
            }
            // Ready carries the lower minor, and the session runs at it; a
            // manifest asking for more than that is refused here.
            let ready_minor = features::ready_minor(hello.protocol_minor);
            if let Some(refusal) =
                features::engines_refusal(self.core.manifest.minimum_protocol.1, ready_minor)
            {
                return Err(Error::protocol(refusal));
            }
            validate_identity(&first.id, "Hello from ")
                .map_err(|e| Error::protocol(format!("{ready_id}: {e}")))?;
            #[cfg(unix)]
            self.core
                .bell
                .handshake_completed(hello.protocol_major, hello.protocol_minor)?;
            // Only an authenticated stream gets here; the doorbell's gate
            // receives the token that stream proved, in metadata or Hello.
            #[cfg(windows)]
            self.core.bell.handshake_completed_with_token(
                hello.protocol_major,
                hello.protocol_minor,
                self.core
                    .session_token
                    .lock()
                    .unwrap()
                    .as_deref()
                    .unwrap_or(&hello.session_token),
            )?;
            *self.core.host_identity.lock().unwrap() = Some(first.id.clone());
            self.core.send(
                ready_id,
                Plugin::Ready(pb::Ready {
                    protocol_major: features::PROTOCOL_MAJOR,
                    protocol_minor: ready_minor,
                    plugin_id: self.core.manifest.id().into(),
                }),
                0,
            )?;
            // Open application admission only after Ready is the first queued message.
            *self.core.peer.lock().unwrap() = Some((hello.protocol_major, hello.protocol_minor));
            Ok(())
        })();
        if let Err(e) = handshake {
            return Err(Box::new(tonic::Status::failed_precondition(e.message)));
        }
        Ok(())
    }
    fn start_writer(
        &self,
        output_tx: tokio::sync::mpsc::Sender<std::result::Result<pb::PluginMsg, tonic::Status>>,
    ) {
        let writer = self.core.clone();
        tokio::spawn(async move {
            let mut drain_deadline = None;
            loop {
                let notified = writer.wake.notified();
                let (batch, closed, fault) = {
                    let mut q = writer.queues.lock().unwrap();
                    let mut batch = Vec::new();
                    for _ in 0..16 {
                        if let Some(m) = q.reliable.pop_front() {
                            let action = q.after_reply.remove(&m.id);
                            batch.push((m, action));
                        }
                    }
                    for _ in 0..8 {
                        if let Some(m) = q.requests.pop_front() {
                            batch.push((m, None));
                        }
                    }
                    for _ in 0..8 {
                        if let Some(m) = q.pushes.pop_front() {
                            batch.push((m, None));
                        }
                    }
                    for _ in 0..8 {
                        if let Some(m) = q.work.pop_front() {
                            batch.push((m, None));
                        }
                    }
                    for _ in 0..4 {
                        if let Some(m) = q.shapes.pop_front() {
                            batch.push((m, None));
                        }
                    }
                    q.bytes -= batch.iter().map(|(m, _)| m.encoded_len()).sum::<usize>();
                    (batch, q.closed, q.fault.take())
                };
                if closed && drain_deadline.is_none() {
                    drain_deadline = Some(tokio::time::Instant::now() + Duration::from_secs(5));
                }
                if batch.is_empty() {
                    if let Some(f) = fault {
                        let _ = tokio::time::timeout(
                            Duration::from_secs(5),
                            output_tx.send(Err(tonic::Status::internal(f))),
                        )
                        .await;
                        break;
                    }
                    if closed {
                        break;
                    }
                    notified.await;
                    continue;
                }
                for (m, action) in batch {
                    let deadline = drain_deadline
                        .unwrap_or_else(|| tokio::time::Instant::now() + Duration::from_secs(5));
                    if !writer.write_outbound(&output_tx, m, deadline).await {
                        writer.finish();
                        return;
                    }
                    if let Some(action) = action {
                        action.start();
                    }
                }
                if let Some(f) = fault {
                    let _ = tokio::time::timeout(
                        Duration::from_secs(5),
                        output_tx.send(Err(tonic::Status::internal(f))),
                    )
                    .await;
                    break;
                }
            }
            writer.finish();
        });
    }
}
impl Runtime {
    async fn dispatch(self, mut stream: tonic::Streaming<pb::HostMsg>) {
        let (jobs, work) = std::sync::mpsc::sync_channel::<Job>(1);
        let (completed, mut completions) = tokio::sync::mpsc::channel::<Completed>(1);
        let app = self.application.clone();
        let ctx = self.context();
        let spawn = std::thread::Builder::new()
            .name("vizi-requests".into())
            .spawn(move || {
                while let Ok(mut job) = work.recv() {
                    if ctx.core.ended.load(Ordering::SeqCst) {
                        break;
                    }
                    let _current = work::Current::enter(job.work.work.clone());
                    let slot = Arc::new(Mutex::new(DeferredSlot::default()));
                    let request_ctx = Context {
                        core: ctx.core.clone(),
                        after_reply: Some(slot.clone()),
                        work: Some(job.work.work.clone()),
                    };
                    let mut result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                        if job.work.work.cancelled() {
                            return Err(Error::cancelled("canceled before it started"));
                        }
                        if let Host::PlanStage(request) = &job.request.message {
                            request_ctx
                                .core
                                .stages
                                .plan(&request_ctx, request)
                                .map(Plugin::StagePlanned)
                        } else {
                            app.handle(&request_ctx, &mut job.request)
                        }
                    }))
                    .unwrap_or_else(|payload| {
                        Err(work::unwind_error(payload, "request handler panicked"))
                    });
                    if result
                        .as_ref()
                        .is_ok_and(|reply| !valid_reply(&job.request.message, reply))
                    {
                        if let Ok(Plugin::Segment(d)) = &result {
                            let _ = ctx.memory().cancel_unsent_descriptor(d);
                        }
                        result = Err(Error::protocol("handler returned the wrong reply kind"));
                    }
                    let after_reply = {
                        let mut slot = slot.lock().unwrap();
                        slot.closed = true;
                        slot.action.take()
                    };
                    // Drop application captures outside the registration lock.
                    let after_reply = if matches!(&result, Ok(reply)
                        if !matches!(reply, Plugin::Error(_))
                        && !matches!(reply, Plugin::CommandDone(done) if !done.ok))
                    {
                        after_reply
                    } else {
                        None
                    };
                    drop(job.request); // Settle any unread deposit before the reply handoff.
                    if completed
                        .blocking_send(Completed {
                            work: job.work,
                            result,
                            after_reply,
                        })
                        .is_err()
                    {
                        break;
                    }
                }
            });
        if let Err(e) = spawn {
            self.core.fault(e.to_string());
            return;
        }
        let mut active: Option<Arc<work::RequestWork>> = None;
        let mut deferred: VecDeque<Incoming> = VecDeque::new();
        let mut shutdown: Option<Shutdown> = None;
        let mut terminal_reported = false;
        let mut ids = super::ids::Ledger::default();
        let mut tick = tokio::time::interval(Duration::from_millis(10));
        tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        loop {
            tokio::select! {
                incoming=stream.message()=>match incoming{
                    Ok(Some(message))=>{
                        // Taken before any await: the decoder of this message ran
                        // inside this poll, on this thread.
                        match ids.observe(&message, super::codec::take_digest()) {
                            super::ids::Seen::New => {}
                            super::ids::Seen::Resent => {
                                // What a re-send names belongs to the first copy: nothing
                                // is released for it.
                                eprintln!("vizi SDK: re-sent message under id {:?} deduplicated", message.id);
                                continue;
                            }
                            super::ids::Seen::Reused => {
                                self.core.fault(format!("id {:?} reused with different content", message.id));
                                break;
                            }
                        }
                        if let Err(e)=bounds::host(&message){
                            self.core.memory.report(21,e.message.clone(),Vec::new());
                            if let Some(msg) = &message.msg {
                                if is_request(msg) {
                                    let _ = self.core.send(message.id, Plugin::Error(e.wire()), 0);
                                } else if reply_kind(msg).is_some() || matches!(msg, Host::HostError(_)) {
                                    // The reply exists but is unusable. Wake its
                                    // waiter now, rather than leave it to expire.
                                    self.core.deliver(message.id, Host::HostError(e.wire()));
                                }
                                if let Some(d) = descriptor(msg) {
                                    let _ = self.core.memory.discard_deposit(d);
                                }
                            }
                            self.core.flush_memory();
                            continue;
                        }
                        let registration = if message.msg.as_ref().is_some_and(is_request) && !message.id.is_empty() {
                            match work::register_request(&self.core, &message.id) {
                                Ok(work) => Some(work),
                                Err(e) => {
                                    let _ = self.refuse(message.id, message.msg.as_ref().unwrap(), e);
                                    continue;
                                }
                            }
                        } else { None };
                        if let Err(e)=self.route(Incoming { message, work: registration },&jobs,&mut active,&mut deferred,&mut shutdown){self.core.fault(e.message);break;}
                    },
                    Ok(None)=>{self.core.end_requests("the host closed its side of the stream");self.core.finish();break;},Err(e)=>{self.core.fault(format!("gRPC stream error: {e}"));break;}
                },
                completion=completions.recv()=>{
                    if let Some(c)=completion {
                        if active.as_ref().is_some_and(|a| Arc::ptr_eq(a, &c.work.work)) {
                            active = None;
                            let reply = c.result.unwrap_or_else(|e| Plugin::Error(e.wire()));
                            let loan = if let Plugin::Segment(d) = &reply { Some(d.clone()) } else { None };
                            if let Err(e) = self.core.send_with_action(c.work.work.id.clone(), reply, 0, c.after_reply) {
                                if let Some(d) = loan { let _ = self.core.memory.cancel_unsent_descriptor(&d); }
                                let _ = self.core.send(c.work.work.id.clone(), Plugin::Error(e.wire()), 0);
                            }
                        } else if let Ok(Plugin::Segment(d)) = &c.result {
                            let _ = self.core.memory.cancel_unsent_descriptor(d);
                        }
                    } else { self.core.fault("request worker ended"); break; }
                },
                _=tick.tick()=>{},
            }
            if self.core.ended.load(Ordering::SeqCst) {
                break;
            }
            if self.core.queues.lock().unwrap().closed {
                break;
            }
            // Holding descriptors does not occupy the dispatcher or wait on M.
            let n = deferred.len();
            for _ in 0..n {
                let message = deferred.pop_front().unwrap();
                let state = descriptor(message.message.msg.as_ref().unwrap())
                    .map(|d| self.core.memory.query(d.segment_id).state)
                    .unwrap_or(3);
                if matches!(state, 1 | 2) {
                    deferred.push_back(message);
                } else if let Err(e) =
                    self.route(message, &jobs, &mut active, &mut deferred, &mut shutdown)
                {
                    self.core.fault(e.message);
                }
            }
            if !terminal_reported {
                if let Some(terminal) = self.core.bell.terminal_report() {
                    terminal_reported = true;
                    if let Terminal::Fault(f) = terminal {
                        if let Err(e) = self
                            .core
                            .push(Plugin::DoorbellFault(self.core.fault_report(&f)))
                        {
                            self.core.fault(e.message);
                        }
                    }
                }
            }
            self.core.flush_memory();
            if let Some(shutdown) = &mut shutdown {
                if shutdown.report.is_none() {
                    match shutdown.result.try_recv() {
                        Ok(report) => shutdown.report = Some(report),
                        Err(std::sync::mpsc::TryRecvError::Disconnected) => {
                            shutdown.report = Some(pb::ShutdownReport {
                                outcome: 3,
                                detail: "shutdown worker disconnected".into(),
                            })
                        }
                        _ => {}
                    }
                }
                if Instant::now() >= shutdown.deadline
                    || shutdown.report.is_some()
                        && active.is_none()
                        && !self.core.memory.has_pending_reports()
                {
                    let report = shutdown.report.take().unwrap_or(pb::ShutdownReport {
                        outcome: 4,
                        detail: "shutdown hook exceeded window".into(),
                    });
                    let _ = self.core.push(Plugin::ShutdownReport(report));
                    self.core.queues.lock().unwrap().closed = true;
                    self.core.wake.notify_one();
                    break;
                }
            }
        }
        // Requests wait only on this session; stop callers even if output is blocked.
        // Publish cancellation before waking callers on fault paths too.
        self.core.close_request_admission();
        let fault = self.core.queues.lock().unwrap().fault.clone();
        self.core
            .end_requests(fault.as_deref().unwrap_or("the session closed"));
        self.core.bell.stop();
    }
    /// A content refusal disposes any resolvable deposit and answers a request
    /// or reply waiter. Only failure to queue the answer can fault the session.
    fn refuse(&self, id: String, msg: &Host, err: Error) -> Result<()> {
        eprintln!("vizi SDK: {err}");
        if let Some(d) = descriptor(msg) {
            if let Err(e) = self.core.memory.discard_deposit(d) {
                eprintln!("vizi SDK: refused descriptor could not be released: {e}");
            }
        }
        if is_request(msg) {
            self.core.send(id, Plugin::Error(err.wire()), 0)
        } else {
            self.core.deliver(id, Host::HostError(err.wire()));
            Ok(())
        }
    }
    fn route(
        &self,
        incoming: Incoming,
        jobs: &std::sync::mpsc::SyncSender<Job>,
        active: &mut Option<Arc<work::RequestWork>>,
        deferred: &mut VecDeque<Incoming>,
        shutdown: &mut Option<Shutdown>,
    ) -> Result<()> {
        let Incoming { message, work } = incoming;
        let id = message.id;
        let Some(mut msg) = message.msg else {
            // An unknown kind under the id of one of this end's pending
            // requests stands where that request's reply belongs: it faults
            // the session (§4 rule 8). Under any other id it is answered
            // UNSUPPORTED, and without an id it is dropped.
            if id.is_empty() {
                eprintln!("vizi SDK: unknown host push kind dropped");
            } else if let Some(detail) = self.core.unknown_under_pending(&id) {
                return Err(Error::protocol(detail));
            } else {
                self.core.send(
                    id,
                    Plugin::Error(Error::unsupported("unknown host message kind").wire()),
                    0,
                )?;
            }
            return Ok(());
        };
        if is_request(&msg) && shutdown.is_some() {
            if let Some(d) = descriptor(&msg) {
                let _ = self.core.memory.discard_deposit(d);
            }
            return self.core.send(
                id,
                Plugin::Error(Error::protocol("new request during shutdown").wire()),
                0,
            );
        }
        let install = match &msg {
            Host::DataVolume(v) => v.install.as_ref(),
            Host::ExportChunk(v)
                if matches!(v.source, Some(pb::export_chunk::Source::Segment(_))) =>
            {
                v.install.as_ref()
            }
            _ => None,
        };
        if install.is_some_and(|i| descriptor(&msg).is_none_or(|d| d.segment_id != i.segment_id)) {
            return self.refuse(
                id,
                &msg,
                Error::protocol("ride-along segment identity mismatch"),
            );
        }
        match &mut msg {
            Host::DataVolume(v) => {
                if let Some(i) = &v.install {
                    if let Err(e) = self.core.memory.install(i.clone()) {
                        return self.refuse(id, &msg, e.into());
                    }
                }
            }
            Host::ExportChunk(v) => {
                if let Some(i) = v.install.take() {
                    if let Some(pb::export_chunk::Source::Segment(_)) = &v.source {
                        if let Err(e) = self.core.memory.install(i) {
                            return self.refuse(id, &msg, e.into());
                        }
                    } else {
                        self.core
                            .memory
                            .report(17, "inline chunk carries InstallSegment", vec![]);
                        // §8.12 rejects this installation, not the inline
                        // payload. The handler receives the plain chunk.
                    }
                }
            }
            _ => {}
        }
        if let Some(d) = descriptor(&msg) {
            let state = self.core.memory.query(d.segment_id);
            if matches!(state.state, 1 | 2) {
                if deferred.len() >= 32 {
                    return Err(Error::internal("pending descriptor queue exceeded 32"));
                }
                deferred.push_back(Incoming {
                    message: pb::HostMsg { id, msg: Some(msg) },
                    work,
                });
                return Ok(());
            }
            if state.state != 3 {
                let err = Error::new(
                    pb::error::Kind::NotFound,
                    format!("descriptor segment unavailable: {:?}", state.detail),
                );
                if is_request(&msg) {
                    return self.core.send(id, Plugin::Error(err.wire()), 0);
                } else {
                    self.core.deliver(id, Host::HostError(err.wire()));
                    return Ok(());
                }
            }
        }
        if reply_kind(&msg).is_some() || matches!(msg, Host::HostError(_)) {
            self.core.deliver(id, msg);
            return Ok(());
        }
        if is_request(&msg) {
            if id.is_empty() {
                return self.refuse(id, &msg, Error::protocol("host request id is empty"));
            }
            if active.is_some() {
                return self.refuse(id, &msg, Error::protocol("host sent concurrent requests"));
            }
            let work = work.expect("request registered on dispatch");
            let running = work.work.clone();
            let d = descriptor(&msg).cloned();
            if let Err(e) = jobs.try_send(Job {
                work,
                request: HostReply {
                    deposit: d
                        .as_ref()
                        .and_then(|d| self.core.memory.pin_deposit(d).ok()),
                    message: msg,
                    descriptor: d,
                    memory: self.core.memory.clone(),
                },
            }) {
                // Dropping the unscheduled job disposes its descriptor.
                drop(e);
                return self.core.send(
                    id,
                    Plugin::Error(Error::internal("request worker unavailable").wire()),
                    0,
                );
            }
            *active = Some(running);
            return Ok(());
        }
        match msg {
            Host::ShapeDrag(edit) => self.core.shapes.drag(&self.context(), edit)?,
            Host::ShapeCommit(edit) => self.core.shapes.commit(&self.context(), edit)?,
            Host::ShapeEditEnded(ended) => {
                self.core.shapes.end(&self.core, ended.drag_id);
                self.event(Event::ShapeEnded(ended));
            }
            Host::CancelWork(cancel) => {
                work::cancel_subject(&self.core, &cancel);
            }
            Host::Hello(_) => eprintln!("vizi SDK: second Hello ignored"),
            Host::InstallSegment(i) => {
                if shutdown.is_some() {
                    eprintln!("vizi SDK: applying standalone installation during shutdown");
                }
                let segment_id = i.segment_id;
                if let Err(e) = self.core.memory.install(i) {
                    eprintln!("vizi SDK: installation refused: {e}");
                }
                self.core.flush_memory();
                self.panel_images_ready(segment_id);
            }
            Host::ToolBinding(b) => {
                if let Some(binding) = self.core.tool_binding(b)? {
                    let runtime = self.clone();
                    self.core
                        .bell
                        .after_outcomes(move || runtime.event(Event::Tool(binding)))
                        .map_err(|e| {
                            Error::internal(format!(
                                "tool lifecycle callback could not be queued: {e:?}"
                            ))
                        })?;
                }
            }
            Host::StageArm(arm) => self.core.stages.arm(&self.core, id, arm)?,
            Host::EpochChange(e) => self.core.epoch(e)?,
            Host::TimeMapping(t) => self.core.time_mapping(t)?,
            Host::GrantStateQuery(q) => self.core.send(
                id,
                Plugin::GrantState(self.core.memory.query(q.segment_id)),
                0,
            )?,
            Host::RetireSegment(r) => {
                if let Err(e) = self.core.memory.retire(r.segment_id) {
                    eprintln!("vizi SDK: retirement refused: {e}");
                }
            }
            Host::ReleaseLoan(r) => {
                let segment = r.segment_id;
                if let Err(e) = self.core.memory.release_loan(r) {
                    eprintln!("vizi SDK: {e}");
                } else {
                    self.core.chunks.released(segment);
                }
            }
            Host::ChunksWanted(w) => self.core.chunks.wanted(w),
            Host::PanelOpened(p) => self.event(Event::Panel(p)),
            Host::FollowStarted(v) => follow::start(&self.context(), self.application.clone(), v)?,
            Host::FollowEnded(v) => self.core.follow_ended(v),
            Host::ViewChanged(v) => self.core.subscriptions.view(v),
            Host::RegionsChanged(v) => self.core.subscriptions.regions(v),
            Host::WidgetGeometry(v) => self.core.subscriptions.geometry(v),
            Host::Appearance(v) => self.core.subscriptions.appearance(v),
            Host::ContractViolation(v) => eprintln!("{}", host_violation_message(&v)),
            Host::DoorbellShutdown(s) => {
                if self.core.doorbell_shutdown.swap(true, Ordering::SeqCst) {
                    eprintln!("vizi SDK: duplicate DoorbellShutdown ignored");
                    return Ok(());
                }
                self.core.bell.stop();
                self.core
                    .push(Plugin::DoorbellFinalReport(pb::DoorbellFinalReport {
                        detail: format!(
                            "reason={} ({:?}) failure_class={:?} detail={:?}; {:?}",
                            s.reason,
                            pb::DoorbellShutdownReason::try_from(s.reason).ok(),
                            s.failure_class,
                            s.detail,
                            self.core.bell.statistics()
                        ),
                    }))?;
            }
            Host::Shutdown(s) => {
                if shutdown.is_some() {
                    eprintln!("vizi SDK: duplicate Shutdown ignored");
                    return Ok(());
                }
                // §3.3: the host answers no request in its window, so every
                // request still pending fails now, before the hook runs; this
                // also unblocks sources waiting on a host read before joining.
                let reason = pb::ShutdownReason::try_from(s.reason)
                    .map_or_else(|_| s.reason.to_string(), |r| r.as_str_name().to_string());
                self.core.end_requests(&format!(
                    "shutdown began ({reason}): the host answers no request in its window"
                ));
                self.core.close_request_admission();
                self.core.chunks.stop();
                self.core.subscriptions.stop();
                self.core.stop_follows();
                self.core.bell.stop();
                if s.deadline_ms.is_none() {
                    self.core
                        .memory
                        .report(25, "Shutdown omitted deadline; using 5 s", vec![]);
                }
                let until = Instant::now()
                    + Duration::from_millis(u64::from(s.deadline_ms.unwrap_or(5000)));
                let (tx, rx) = std::sync::mpsc::sync_channel(1);
                let app = self.application.clone();
                let ctx = self.context();
                std::thread::Builder::new()
                    .name("vizi-shutdown".into())
                    .spawn(move || {
                        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                            ctx.core.chunks.join();
                            app.shutdown(&ctx, s)
                        }));
                        let report = match result {
                            Ok(Ok(outcome)) => pb::ShutdownReport {
                                outcome: outcome as i32,
                                detail: String::new(),
                            },
                            Ok(Err(e)) => pb::ShutdownReport {
                                outcome: 3,
                                detail: e.message,
                            },
                            Err(_) => pb::ShutdownReport {
                                outcome: 3,
                                detail: "shutdown hook panicked".into(),
                            },
                        };
                        let _ = tx.send(report);
                    })?;
                *shutdown = Some(Shutdown {
                    deadline: until,
                    result: rx,
                    report: None,
                });
            }
            _ => eprintln!("vizi SDK: unclassified host message ignored"),
        }
        Ok(())
    }
}

// Keep unknown future sites visible too: their detail is often the only reason
// an author receives for an overlay that did not appear.
fn host_violation_message(v: &pb::ContractViolation) -> String {
    let name = pb::ViolationSite::try_from(v.r#where)
        .map_or("UNKNOWN_VIOLATION_SITE", |site| site.as_str_name());
    format!(
        "vizi host reports violation {} {} ({}): {}",
        v.r#where, name, v.count, v.detail
    )
}

#[cfg(test)]
mod host_violation_tests {
    use super::*;

    #[test]
    fn refused_chunk_names_site_30() {
        let report = pb::ContractViolation {
            r#where: 30,
            count: 2,
            detail: "chunk box outside level".into(),
            ..Default::default()
        };
        assert_eq!(
            pb::ViolationSite::try_from(30).unwrap(),
            pb::ViolationSite::OverlayChunkRefused
        );
        assert_eq!(host_violation_message(&report),
            "vizi host reports violation 30 VIOLATION_SITE_OVERLAY_CHUNK_REFUSED (2): chunk box outside level");
    }

    #[test]
    fn refused_overlay_diagnostic_names_site_count_and_reason() {
        let report = pb::ContractViolation {
            r#where: pb::ViolationSite::OverlayItemRefused as i32,
            count: 3,
            detail: "overlay labels, item cells: position is not integral".into(),
            ..Default::default()
        };
        assert_eq!(report.r#where, 28);
        assert_eq!(host_violation_message(&report),
            "vizi host reports violation 28 VIOLATION_SITE_OVERLAY_ITEM_REFUSED (3): overlay labels, item cells: position is not integral");
    }

    #[test]
    fn refused_panel_op_decodes_and_names_site_count_and_reason() {
        let message = pb::HostMsg {
            msg: Some(Host::ContractViolation(pb::ContractViolation {
                r#where: 29,
                count: 2,
                detail: "panel probe, op set_image, widget plot: PNG did not decode".into(),
                ..Default::default()
            })),
            ..Default::default()
        };
        let decoded = pb::HostMsg::decode(message.encode_to_vec().as_slice()).unwrap();
        bounds::host(&decoded).unwrap();
        let Some(Host::ContractViolation(report)) = decoded.msg else {
            panic!("expected a host violation report");
        };
        assert_eq!(
            pb::ViolationSite::try_from(report.r#where).unwrap(),
            pb::ViolationSite::PanelPatchRefused
        );
        assert_eq!(host_violation_message(&report),
            "vizi host reports violation 29 VIOLATION_SITE_PANEL_PATCH_REFUSED (2): panel probe, op set_image, widget plot: PNG did not decode");
    }

    #[test]
    fn future_host_violation_is_still_visible() {
        let report = pb::ContractViolation {
            r#where: 1000,
            count: 2,
            detail: "new host refusal".into(),
            ..Default::default()
        };
        assert_eq!(
            host_violation_message(&report),
            "vizi host reports violation 1000 UNKNOWN_VIOLATION_SITE (2): new host refusal"
        );
    }
}

#[cfg(all(test, target_os = "linux"))]
mod shutdown_admission_tests {
    use super::*;
    use std::{future::Future, task::Poll};

    fn runtime() -> (Runtime, std::os::fd::OwnedFd) {
        let manifest = crate::manifest::Manifest::parse(
            r#"{"manifest_version":1,"id":"test.shutdown","name":"Shutdown","version":"1.0.0","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#,
        ).unwrap();
        let (fd, peer) = crate::socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let runtime = Runtime::new(
            manifest,
            Arc::new(crate::plugin::Registry::new()),
            bell,
            listener,
        )
        .unwrap();
        *runtime.core.peer.lock().unwrap() = Some((1, 0));
        (runtime, peer)
    }

    fn read() -> Plugin {
        Plugin::GetViewState(pb::GetViewState {})
    }

    #[test]
    fn shutdown_removes_unsent_requests_and_closes_the_final_admission_gate() {
        let (runtime, _peer) = runtime();
        let ctx = runtime.context();
        let pending = ctx.start_request(read()).unwrap();
        let reply = pb::PluginMsg {
            id: "finished-command".into(),
            msg: Some(Plugin::CommandDone(pb::CommandDone::default())),
        };
        ctx.core
            .send(reply.id.clone(), reply.msg.clone().unwrap(), 0)
            .unwrap();
        ctx.core.close_request_admission();
        assert!(ctx.is_shutting_down());
        {
            let q = ctx.core.queues.lock().unwrap();
            assert!(q.requests.is_empty());
            assert_eq!(q.reliable.front(), Some(&reply));
            assert_eq!(q.bytes, reply.encoded_len());
        }
        assert!(ctx.start_request(read()).is_err());
        // Model a caller paused AFTER its early shutdown check and waiter
        // registration. The queue's own admission gate must still reject it.
        assert!(ctx.core.send("racing-read".into(), read(), 1).is_err());
        ctx.core
            .push(Plugin::ShutdownReport(pb::ShutdownReport::default()))
            .unwrap();
        runtime.stop();
        assert!(pending.wait(Duration::from_secs(5)).is_err());
    }

    #[tokio::test]
    async fn shutdown_cancels_a_writer_batch_waiting_for_capacity_but_allows_cleanup() {
        let (runtime, _peer) = runtime();
        let (output, mut received) = tokio::sync::mpsc::channel(1);
        output.send(Ok(pb::PluginMsg::default())).await.unwrap();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(60);
        let message = pb::PluginMsg {
            id: "held-read".into(),
            msg: Some(read()),
        };
        let write = runtime
            .core
            .write_outbound(&output, message.clone(), deadline);
        tokio::pin!(write);
        // Poll to the capacity wait before starting shutdown; no scheduler or
        // timing assumption determines which side of this boundary is tested.
        std::future::poll_fn(|cx| {
            assert!(matches!(write.as_mut().poll(cx), Poll::Pending));
            Poll::Ready(())
        })
        .await;
        runtime.core.close_request_admission();
        assert!(tokio::time::timeout(Duration::from_secs(5), write)
            .await
            .unwrap());
        received.try_recv().unwrap().unwrap();
        assert!(received.try_recv().is_err());
        // A message already removed from the SDK queue is also dropped when
        // output has capacity, if shutdown won the race to final handoff.
        assert!(
            runtime
                .core
                .write_outbound(&output, message, deadline)
                .await
        );
        assert!(received.try_recv().is_err());
        let cleanup = pb::PluginMsg {
            msg: Some(Plugin::ShutdownReport(pb::ShutdownReport::default())),
            ..Default::default()
        };
        assert!(
            runtime
                .core
                .write_outbound(&output, cleanup.clone(), deadline)
                .await
        );
        assert_eq!(received.try_recv().unwrap().unwrap(), cleanup);
        runtime.stop();
    }
}
