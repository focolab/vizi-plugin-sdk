#![cfg(target_os = "linux")]
#[path = "session_cases/channel_names.rs"]
mod channel_names_cases;
#[path = "session_cases/follow.rs"]
mod follow_cases;
#[path = "session_cases/images.rs"]
mod image_cases;
#[path = "session_cases/lists.rs"]
mod list_cases;
#[path = "session_cases/provenance.rs"]
mod provenance_cases;
#[path = "session_cases/series.rs"]
mod series_cases;
#[path = "session_cases/shape.rs"]
mod shape_cases;
#[path = "session_cases/subscribed.rs"]
mod subscribed_cases;
#[path = "session_cases/view.rs"]
mod view_cases;
#[path = "session_cases/work.rs"]
mod work_cases;
use pb::{host_msg::Msg as Host, plugin_msg::Msg as Plugin};
use std::{
    os::fd::{AsFd, AsRawFd, FromRawFd, OwnedFd},
    os::unix::fs::FileExt,
    sync::{
        atomic::{AtomicU64, AtomicUsize, Ordering},
        Arc, Mutex,
    },
    time::Duration,
};
use tokio_stream::wrappers::ReceiverStream;
use vizi_doorbell::{
    control::{Context, Runtime},
    manifest::Manifest,
    plugin::Registry,
    proto as pb, *,
};

fn manifest() -> Manifest {
    Manifest::parse(r#"{"manifest_version":1,"id":"test.plugin","name":"Test","version":"1.2.3","engines":{"vizi":"1.0"},"entry":{"command":["test"]},"contributions":{"overlays":[{"id":"overlay"},{"id":"chunks","chunked":true},{"id":"static_chunks","chunked":true,"time":"static"}],"readers":[{"id":"raw-reader","extensions":["raw"]}],"commands":[{"id":"run","title":"Run"}],"tools":[{"id":"tool","triggers":[{"key":"T"}],"actions":[{"id":"add_node","key":"N"}]}],"panels":[{"id":"panel","title":"Panel","root":{"kind":"label","id":"status"}}],"exporters":[{"id":"raw","extension":"raw"}]}}"#).unwrap()
}
struct Session {
    tx: tokio::sync::mpsc::Sender<pb::HostMsg>,
    rx: tonic::Streaming<pb::PluginMsg>,
    runtime: Runtime,
    peer: OwnedFd,
    connection: OwnedFd,
    server: tokio::task::JoinHandle<vizi_doorbell::control::Result<()>>,
}
impl Session {
    async fn new(plugin: Registry) -> Self {
        Self::version(plugin, 1, 23, "Hello from independent Rust spec fixture")
            .await
            .unwrap()
    }
    async fn version(
        plugin: Registry,
        major: u32,
        minor: u32,
        identity: &str,
    ) -> std::result::Result<Self, Box<tonic::Status>> {
        Self::with_runtime(
            |bell, listener| Runtime::new(manifest(), Arc::new(plugin), bell, listener),
            major,
            minor,
            identity,
        )
        .await
    }
    async fn with_runtime(
        make: impl FnOnce(Doorbell, Listener) -> vizi_doorbell::control::Result<Runtime>,
        major: u32,
        minor: u32,
        identity: &str,
    ) -> std::result::Result<Self, Box<tonic::Status>> {
        static ID: AtomicU64 = AtomicU64::new(0);
        let path = std::path::PathBuf::from(format!(
            "/tmp/vizi-rust-sdk-{}-{}.sock",
            std::process::id(),
            ID.fetch_add(1, Ordering::Relaxed)
        ));
        let (plugin_fd, peer) = socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(plugin_fd, Default::default()).unwrap();
        let runtime = make(bell, listener).unwrap();
        let r = runtime.clone();
        let p = path.clone();
        let server = tokio::spawn(async move { r.serve_unix(p).await });
        let endpoint = tonic::transport::Endpoint::from_static("http://[::]:50051");
        let mut connection = None;
        let transport = Arc::new(Mutex::new(None));
        for _ in 0..200 {
            let p = path.clone();
            let transport = transport.clone();
            match endpoint
                .clone()
                .connect_with_connector(tower::service_fn(move |_| {
                    let p = p.clone();
                    let transport = transport.clone();
                    async move {
                        let stream = tokio::net::UnixStream::connect(p).await?;
                        *transport.lock().unwrap() = Some(stream.as_fd().try_clone_to_owned()?);
                        Ok::<_, std::io::Error>(hyper_util::rt::TokioIo::new(stream))
                    }
                }))
                .await
            {
                Ok(c) => {
                    connection = Some(c);
                    break;
                }
                Err(_) => tokio::time::sleep(Duration::from_millis(5)).await,
            }
        }
        let channel = connection.expect("server bind deadline");
        let mut client = pb::plugin_client::PluginClient::new(channel)
            .max_decoding_message_size(control::MAX_MESSAGE_BYTES);
        let (tx, rx) = tokio::sync::mpsc::channel(64);
        tx.send(pb::HostMsg {
            id: identity.into(),
            msg: Some(Host::Hello(pb::Hello {
                protocol_major: major,
                protocol_minor: minor,
                session_token: String::new(),
            })),
        })
        .await
        .unwrap();
        let response = client.session(ReceiverStream::new(rx)).await;
        match response {
            Ok(response) => Ok(Self {
                tx,
                rx: response.into_inner(),
                runtime,
                peer,
                connection: transport.lock().unwrap().take().unwrap(),
                server,
            }),
            Err(e) => {
                runtime.stop();
                server.abort();
                Err(Box::new(e))
            }
        }
    }
    async fn send(&self, id: &str, msg: Host) {
        self.tx
            .send(pb::HostMsg {
                id: id.into(),
                msg: Some(msg),
            })
            .await
            .unwrap();
    }
    async fn recv(&mut self) -> pb::PluginMsg {
        tokio::time::timeout(Duration::from_secs(3), self.rx.message())
            .await
            .expect("response deadline")
            .unwrap()
            .expect("stream ended unexpectedly")
    }
    /// A following control reply also proves refused arrangements emitted no
    /// acknowledgements or unrelated contract reports.
    async fn barrier(&mut self) {
        self.send(
            "barrier",
            Host::GrantStateQuery(pb::GrantStateQuery { segment_id: 99 }),
        )
        .await;
        let reply = self.recv().await;
        assert_eq!(reply.id, "barrier");
        assert!(matches!(reply.msg, Some(Plugin::GrantState(_))));
    }
    fn ctx(&self) -> Context {
        self.runtime.context()
    }
    async fn stop(self) {
        self.runtime.stop();
        drop(self.rx);
        drop(self.tx);
        tokio::time::timeout(Duration::from_secs(2), self.server)
            .await
            .unwrap()
            .unwrap()
            .unwrap();
    }
}
fn done() -> pb::CommandDone {
    pb::CommandDone {
        ok: true,
        ..Default::default()
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn declared_manifest_routes_the_registered_command_and_panel() {
    use vizi_doorbell::declare::{CommandSpec, PanelSpec, PluginBuilder, Widget};
    let mut b = PluginBuilder::new("test.declared", "Declared", "3.2.1");
    let command = b.command(CommandSpec::new("declared.run", "Run"), |ctx, _| {
        ctx.patch(
            "declared.panel",
            vec![pb::PatchOp {
                row_key: String::new(),
                widget_id: "status".into(),
                op: Some(pb::patch_op::Op::SetValue(pb::Value {
                    kind: Some(pb::value::Kind::S("Called".into())),
                })),
            }],
        )?;
        Ok(done())
    });
    b.panel(PanelSpec::new(
        "declared.panel",
        "Panel",
        Widget::column(
            "root",
            [
                Widget::label("status", "Ready"),
                Widget::button("run", "Run", &command),
            ],
        ),
    ));
    let plugin = b.build().unwrap();
    #[cfg(feature = "install")]
    let published = {
        let directory = tempfile::tempdir().unwrap();
        Manifest::load(plugin.write_manifest(directory.path()).unwrap()).unwrap()
    };
    #[cfg(not(feature = "install"))]
    let published = plugin.manifest().clone();
    let command_id = published.value()["contributions"]["panels"][0]["root"]["children"][1]
        ["command"]
        .as_str()
        .unwrap()
        .to_owned();
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from declaration fixture",
    )
    .await
    .unwrap();
    let ready = s.recv().await;
    assert!(ready.id.contains("test.declared 3.2.1"));
    s.send(
        "invoke",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id,
            ..Default::default()
        }),
    )
    .await;
    let mut patched = false;
    let mut completed = false;
    for _ in 0..2 {
        match s.recv().await.msg.unwrap() {
            Plugin::PanelPatch(p) => {
                assert_eq!(p.panel_id, "declared.panel");
                patched = true;
            }
            Plugin::CommandDone(c) => {
                assert!(c.ok);
                assert_eq!(c.command_id, "declared.run");
                completed = true;
            }
            other => panic!("unexpected reply: {other:?}"),
        }
    }
    assert!(patched && completed);
    s.stop().await;
}

async fn check_unannounced_stream_end(disconnect: bool) {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _file = grant(&s, 7, 512);
    s.send("", Host::InstallSegment(install(7))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    let ctx = s.ctx();
    assert_eq!(ctx.memory().installed().len(), 1);
    let requester = ctx.clone();
    let pending = tokio::task::spawn_blocking(move || {
        requester.request_timeout(
            Plugin::GetViewState(pb::GetViewState {}),
            Duration::from_secs(30),
        )
    });
    assert!(matches!(s.recv().await.msg, Some(Plugin::GetViewState(_))));
    let Session {
        tx,
        mut rx,
        runtime,
        peer,
        connection,
        mut server,
    } = s;
    // Keep the doorbell open: the control stream alone must end the session.
    let started = std::time::Instant::now();
    if disconnect {
        // Sever HTTP/2 with both gRPC halves open, without END_STREAM or
        // Shutdown. The duplicate controls the actual socket tonic connected.
        // SAFETY: connection is an owned, live socket; shutdown takes no pointers.
        assert_eq!(
            unsafe { libc::shutdown(connection.as_raw_fd(), libc::SHUT_RDWR) },
            0
        );
        drop(rx);
    } else {
        drop(tx);
        assert!(tokio::time::timeout(Duration::from_secs(2), rx.message())
            .await
            .unwrap()
            .unwrap()
            .is_none());
        drop(rx);
    }
    match tokio::time::timeout(Duration::from_secs(2), &mut server).await {
        Ok(result) => result.unwrap().unwrap(),
        Err(_) => {
            runtime.stop();
            server.abort();
            panic!(
                "serve_unix did not return after unannounced stream end (disconnect={disconnect})"
            );
        }
    }
    assert!(tokio::time::timeout(Duration::from_secs(2), pending)
        .await
        .unwrap()
        .unwrap()
        .is_err());
    assert!(ctx.memory().installed().is_empty());
    assert!(ctx.patch("panel", Vec::new()).is_err());
    assert!(started.elapsed() < Duration::from_secs(2));
    drop(peer);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_eof_returns_from_serve_and_releases_session_resources() {
    check_unannounced_stream_end(false).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stream_error_returns_from_serve_and_releases_session_resources() {
    check_unannounced_stream_end(true).await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn session_fault_returns_from_serve_without_host_shutdown() {
    let mut s = Session::new(Registry::new().on_panel_opened(|_, _| {
        panic!("fixture event failure");
    }))
    .await;
    s.recv().await;
    s.send(
        "",
        Host::PanelOpened(pb::PanelOpened {
            panel_id: "panel".into(),
        }),
    )
    .await;
    let fault = tokio::time::timeout(Duration::from_secs(2), s.rx.message())
        .await
        .unwrap()
        .unwrap_err();
    assert_eq!(fault.code(), tonic::Code::Internal);
    assert!(fault
        .message()
        .contains("application event callback panicked"));
    tokio::time::timeout(Duration::from_secs(2), s.server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn after_reply_orders_picker_and_keeps_dispatching_while_it_waits() {
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (gate_tx, gate_rx) = std::sync::mpsc::channel();
    let gate_rx = Mutex::new(gate_rx);
    let (finished_tx, finished_rx) = std::sync::mpsc::channel();
    let retained = Arc::new(Mutex::new(None));
    let saved = retained.clone();
    let app = Registry::new().command("run", move |ctx, command| {
        if command.args.contains_key("skip") {
            return Ok(done());
        }
        let finished = finished_tx.clone();
        ctx.after_reply(move |ctx| {
            assert!(
                ctx.after_reply(|_| Ok(())).is_err(),
                "callback is outside its handler scope"
            );
            let reply = ctx.request(Plugin::RequestAsset(pb::RequestAsset {
                slot: "pick".into(),
                force_reprompt: true,
            }))?;
            assert!(matches!(reply.message, Host::AssetResult(_)));
            finished.send(()).unwrap();
            Ok(())
        })?;
        assert!(
            ctx.after_reply(|_| Ok(())).is_err(),
            "only one action per reply"
        );
        *saved.lock().unwrap() = Some(ctx.clone());
        entered_tx.send(()).unwrap();
        gate_rx.lock().unwrap().recv().unwrap();
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    assert!(s.ctx().after_reply(|_| Ok(())).is_err());
    s.send(
        "pick",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    // The handler is still held; no sleep-based ordering in the application.
    s.barrier().await;
    gate_tx.send(()).unwrap();
    let ack = s.recv().await;
    assert_eq!(ack.id, "pick");
    assert!(matches!(ack.msg, Some(Plugin::CommandDone(c)) if c.ok));
    let picker = s.recv().await;
    assert!(matches!(picker.msg, Some(Plugin::RequestAsset(_))));
    assert!(retained
        .lock()
        .unwrap()
        .as_ref()
        .unwrap()
        .after_reply(|_| Ok(()))
        .is_err());
    s.send(
        "next",
        Host::InvokeCommand(pb::InvokeCommand {
            cause: 0,
            command_id: "run".into(),
            args: [("skip".into(), pb::Value::default())].into(),
        }),
    )
    .await;
    assert_eq!(s.recv().await.id, "next"); // picker still awaiting its human
    s.send(
        &picker.id,
        Host::AssetResult(pb::AssetResult {
            declined: true,
            ..Default::default()
        }),
    )
    .await;
    finished_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    s.stop().await;
}

struct DeferredDrop(std::sync::mpsc::Sender<()>);
impl Drop for DeferredDrop {
    fn drop(&mut self) {
        let _ = self.0.send(());
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn after_reply_discards_actions_for_failures_and_rejected_output() {
    for mode in ["error", "false", "panic", "oversized"] {
        let calls = Arc::new(AtomicUsize::new(0));
        let called = calls.clone();
        let (dropped_tx, dropped_rx) = std::sync::mpsc::channel();
        let app = Registry::new().command("run", move |ctx, _| {
            let guard = DeferredDrop(dropped_tx.clone());
            let called = called.clone();
            ctx.after_reply(move |_| {
                let _guard = guard;
                called.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })?;
            match mode {
                "error" => Err(control::Error::internal("intentional failure")),
                "false" => Ok(pb::CommandDone {
                    ok: false,
                    ..Default::default()
                }),
                "panic" => panic!("intentional handler panic"),
                _ => Ok(pb::CommandDone {
                    ok: true,
                    message: "x".repeat(control::MAX_INLINE_BYTES + 1),
                    ..Default::default()
                }),
            }
        });
        let mut s = Session::new(app).await;
        s.recv().await;
        s.send(
            "run",
            Host::InvokeCommand(pb::InvokeCommand {
                command_id: "run".into(),
                ..Default::default()
            }),
        )
        .await;
        let reply = s.recv().await;
        assert!(matches!(
            reply.msg,
            Some(Plugin::Error(_)) | Some(Plugin::CommandDone(pb::CommandDone { ok: false, .. }))
        ));
        dropped_rx.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(calls.load(Ordering::SeqCst), 0, "{mode}");
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn supervised_command_outlives_old_budget_and_keeps_its_slot_and_after_reply() {
    let (entered_tx, entered_rx) = std::sync::mpsc::channel();
    let (gate_tx, gate_rx) = std::sync::mpsc::channel();
    let gate_rx = Mutex::new(gate_rx);
    let (after_tx, after_rx) = std::sync::mpsc::channel();
    let app = Registry::new().command("run", move |ctx, _| {
        let after_tx = after_tx.clone();
        ctx.after_reply(move |_| {
            after_tx.send(()).unwrap();
            Ok(())
        })?;
        entered_tx.send(()).unwrap();
        gate_rx
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(30))
            .unwrap();
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "slow",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    entered_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    assert!(
        tokio::time::timeout(Duration::from_millis(10_300), s.rx.message())
            .await
            .is_err()
    );
    s.send(
        "concurrent",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let refused = s.recv().await;
    assert_eq!(refused.id, "concurrent");
    assert!(matches!(refused.msg, Some(Plugin::Error(e))
        if e.kind == pb::error::Kind::Protocol as i32 && e.message.contains("concurrent")));
    gate_tx.send(()).unwrap();
    let reply = s.recv().await;
    assert_eq!(reply.id, "slow");
    assert!(matches!(reply.msg, Some(Plugin::CommandDone(c)) if c.ok));
    after_rx.recv_timeout(Duration::from_secs(2)).unwrap();
    s.stop().await;
}

fn binding(revision: u32) -> pb::ToolBinding {
    pb::ToolBinding {
        tool: 1,
        tool_id: "tool".into(),
        triggers: vec![pb::Trigger {
            key: "T".into(),
            hold: pb::TriggerHold::Toggle as i32,
            ..Default::default()
        }],
        actions: vec![pb::ToolAction {
            action: 1,
            action_id: "add_node".into(),
            key: "N".into(),
        }],
        mode: 1,
        revision,
        ..Default::default()
    }
}
fn epoch(epoch: u32, time_revision: u32) -> pb::EpochChange {
    pb::EpochChange {
        datasets: vec![pb::DatasetState {
            gen: 4,
            geometry: Some(pb::Geometry {
                gen: 4,
                levels: vec![pb::LevelScale {
                    z: 1.,
                    y: 1.,
                    x: 1.,
                }],
                ..Default::default()
            }),
            time_mapping_revision: time_revision,
        }],
        panes: vec![pb::PaneEpoch {
            view_id: 0,
            epoch,
            gen: 4,
            ..Default::default()
        }],
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn handshake_versions_and_identity_form() {
    for (major, minor, id) in [
        (0, 12, "hello"),
        (0, 12, "Hello from old"),
        (2, 0, "Hello from new"),
        (1, 0, "wrong"),
        (1, 0, "Hello from bad\nlog"),
        (1, 0, "Hello from "),
    ] {
        let error = match Session::version(Registry::new(), major, minor, id).await {
            Err(e) => e,
            Ok(mut s) => {
                // Headers precede Hello validation; a rejected handshake is
                // now a status on the response stream, never a Ready message.
                let error = tokio::time::timeout(Duration::from_secs(3), s.rx.message())
                    .await
                    .expect("handshake failure deadline")
                    .expect_err("invalid handshake accepted");
                s.stop().await;
                Box::new(error)
            }
        };
        assert_eq!(error.code(), tonic::Code::FailedPrecondition);
        if major != 1 {
            assert!(error.message().contains("incompatible"), "{error}");
        }
    }
    let mut s = Session::new(Registry::new()).await;
    let ready = s.recv().await;
    assert!(ready.id.starts_with("Ready; I am test.plugin 1.2.3"));
    assert!(matches!(ready.msg,Some(Plugin::Ready(r)) if r.protocol_major==1&&r.protocol_minor==0));
    assert_eq!(
        s.ctx().host_identity().as_deref(),
        Some("Hello from independent Rust spec fixture")
    );
    s.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn blocked_command_does_not_block_arrangements_or_host_replies() {
    let app = Registry::new().command("run", |ctx, _| {
        let reply = ctx.request(Plugin::GetViewState(pb::GetViewState {}))?;
        assert!(matches!(reply.message, Host::ViewState(_)));
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "r1",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let request = s.recv().await;
    assert!(matches!(request.msg, Some(Plugin::GetViewState(_))));
    s.send(&request.id, Host::ToolBinding(binding(1))).await; // push must classify before matching this id
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    s.send("", Host::EpochChange(epoch(8, 0))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    s.send(&request.id, Host::ViewState(pb::ViewState::default()))
        .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "r1");
    assert!(matches!(reply.msg,Some(Plugin::CommandDone(r)) if r.ok));
    s.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn epochs_require_mapping_and_duplicate_state_is_idempotent() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    s.send("", Host::EpochChange(epoch(8, 1))).await;
    let missing = s.recv().await;
    assert!(matches!(missing.msg,Some(Plugin::ContractViolation(v)) if v.r#where==3));
    assert!(s.ctx().pane(0).is_none());
    s.send(
        "",
        Host::TimeMapping(pb::TimeMapping {
            gen: 4,
            revision: 1,
            t: vec![0., 0.5],
        }),
    )
    .await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::TimeMappingAck(_))
    ));
    s.send("", Host::EpochChange(epoch(8, 1))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    let original = s.ctx().pane(0).unwrap();
    assert_eq!(original.time_at(1), Some(0.5));
    assert_eq!(original.physical_spacing(), None);
    s.send("", Host::EpochChange(epoch(8, 1))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    s.send("", Host::EpochChange(epoch(7, 1))).await;
    let mut changed = epoch(8, 1);
    changed.datasets[0].geometry.as_mut().unwrap().spacing_x = 2.;
    s.send("", Host::EpochChange(changed)).await;
    let mut sites = Vec::new();
    while sites.len() < 2 {
        if let Some(Plugin::ContractViolation(v)) = s.recv().await.msg {
            sites.push(v.r#where);
        }
    }
    assert!(sites.contains(&19) && sites.contains(&20));
    assert_eq!(s.ctx().pane(0).unwrap(), original);
    s.stop().await;
}
fn grant(s: &Session, id: u64, size: u64) -> std::fs::File {
    let fd = unsafe {
        libc::memfd_create(
            c"session-grant".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        )
    };
    assert!(fd >= 0);
    let file = unsafe { std::fs::File::from_raw_fd(fd) };
    file.set_len(size).unwrap();
    let mut p = [0; 32];
    p[..8].copy_from_slice(&id.to_le_bytes());
    p[12..28].copy_from_slice(b"spec-proof-12345");
    let crc = crc32(&p[..28]);
    p[28..].copy_from_slice(&crc.to_le_bytes());
    file.write_all_at(&p, 0).unwrap();
    file.write_all_at(b"hello", 64).unwrap();
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
    let bytes = MemoryGrant {
        segment_id: id,
        size,
        permissions: Permissions::new(15).unwrap(),
    }
    .encode(id as u32)
    .unwrap();
    unsafe {
        let mut iov = libc::iovec {
            iov_base: bytes.as_ptr().cast_mut().cast(),
            iov_len: bytes.len(),
        };
        let mut control = [0usize; 8];
        let mut message: libc::msghdr = std::mem::zeroed();
        message.msg_iov = &mut iov;
        message.msg_iovlen = 1;
        message.msg_control = control.as_mut_ptr().cast();
        message.msg_controllen = libc::CMSG_SPACE(4) as usize;
        let header = libc::CMSG_FIRSTHDR(&message);
        (*header).cmsg_level = libc::SOL_SOCKET;
        (*header).cmsg_type = libc::SCM_RIGHTS;
        (*header).cmsg_len = libc::CMSG_LEN(4) as usize;
        std::ptr::write_unaligned(libc::CMSG_DATA(header).cast::<i32>(), fd);
        assert_eq!(
            libc::sendmsg(s.peer.as_raw_fd(), &message, libc::MSG_NOSIGNAL),
            112
        );
    }
    file
}
fn install(id: u64) -> pb::InstallSegment {
    pb::InstallSegment {
        segment_id: id,
        purpose: "V".into(),
        regions: vec![pb::Region {
            index: 0,
            offset: 64,
            extent: 256,
            discipline: 4,
            producer: 1,
            ..Default::default()
        }],
        ..Default::default()
    }
}
fn data(id: u64) -> pb::DataVolume {
    pb::DataVolume {
        segment: Some(pb::SegmentRef {
            segment_id: id,
            offset: 64,
            len: 5,
            shape_zyx: vec![1, 1, 5],
            dtype: 1,
            ..Default::default()
        }),
        install: Some(install(id)),
        ..Default::default()
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn late_reply_before_capability_is_held_then_released_without_delivery() {
    let app = Registry::new().command("run", |ctx, _| {
        assert!(ctx
            .request_timeout(
                Plugin::ReadDataVolume(pb::ReadDataVolume::default()),
                Duration::from_millis(40)
            )
            .is_err());
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "r1",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let request = s.recv().await;
    assert!(matches!(request.msg, Some(Plugin::ReadDataVolume(_))));
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    s.send(&request.id, Host::DataVolume(data(7))).await;
    tokio::time::sleep(Duration::from_millis(20)).await;
    assert_eq!(s.ctx().memory().query(7).state, 2);
    grant(&s, 7, 512);
    let mut installed = false;
    let mut released = false;
    while !installed || !released {
        match s.recv().await.msg {
            Some(Plugin::GrantAck(a)) => {
                assert!(
                    matches!(a.outcome,Some(pb::grant_ack::Outcome::Installed(p)) if p.identity_proof==b"spec-proof-12345")
                );
                installed = true;
            }
            Some(Plugin::ReleaseDeposit(r)) => {
                assert_eq!((r.segment_id, r.region, r.offset, r.len), (7, 0, 64, 5));
                released = true;
            }
            m => panic!("unexpected {m:?}"),
        }
    }
    s.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_hook_runs_during_blocked_command_and_refuses_new_work() {
    let (release, blocked) = std::sync::mpsc::channel();
    let blocked = Mutex::new(blocked);
    let entered = Arc::new(AtomicUsize::new(0));
    let count = entered.clone();
    let hook = Arc::new(AtomicUsize::new(0));
    let h = hook.clone();
    let app = Registry::new()
        .command("run", move |_, _| {
            count.fetch_add(1, Ordering::SeqCst);
            blocked
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(2))
                .unwrap();
            Ok(done())
        })
        .on_shutdown(move |_, _| {
            h.fetch_add(1, Ordering::SeqCst);
            std::thread::sleep(Duration::from_millis(200));
            Ok(())
        });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "r1",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    for _ in 0..100 {
        if entered.load(Ordering::SeqCst) == 1 {
            break;
        }
        tokio::time::sleep(Duration::from_millis(2)).await;
    }
    let shutdown = pb::Shutdown {
        deadline_ms: Some(60),
        ..Default::default()
    };
    s.send("", Host::Shutdown(shutdown.clone())).await;
    s.send("", Host::Shutdown(shutdown)).await;
    s.send(
        "r2",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let refusal = s.recv().await;
    assert_eq!(refusal.id, "r2");
    assert!(matches!(refusal.msg,Some(Plugin::Error(e)) if e.kind==6));
    assert!(matches!(s.recv().await.msg,Some(Plugin::ShutdownReport(r)) if r.outcome==4));
    assert_eq!(hook.load(Ordering::SeqCst), 1);
    assert_eq!(entered.load(Ordering::SeqCst), 1);
    release.send(()).unwrap();
    s.stop().await;
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_request_and_panicking_handler_both_answer() {
    let app = Registry::new().command("run", |_, _| panic!("fixture panic"));
    let mut s = Session::new(app).await;
    s.recv().await;
    s.tx.send(pb::HostMsg {
        id: "unknown".into(),
        msg: None,
    })
    .await
    .unwrap();
    let refusal = s.recv().await;
    assert_eq!(refusal.id, "unknown");
    assert!(matches!(refusal.msg,Some(Plugin::Error(e)) if e.kind==3));
    s.send(
        "panic",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let error = s.recv().await;
    assert_eq!(error.id, "panic");
    assert!(matches!(error.msg,Some(Plugin::Error(e)) if e.kind==7));
    s.send(
        "",
        Host::GrantStateQuery(pb::GrantStateQuery { segment_id: 99 }),
    )
    .await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::GrantState(q)) if q.state==1));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unknown_reply_completes_its_waiter_and_unknown_push_is_not_answered() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let pending = s
        .ctx()
        .start_request(Plugin::GetViewState(pb::GetViewState {}))
        .unwrap();
    let request = s.recv().await;
    assert_eq!(request.id, pending.id());
    // Prost drops unrecognized oneof arms, leaving msg = None.
    s.tx.send(pb::HostMsg {
        id: request.id,
        msg: None,
    })
    .await
    .unwrap();
    let error = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(2)))
        .await
        .unwrap()
        .err()
        .expect("unknown reply must complete with an error");
    assert_eq!(error.kind, pb::error::Kind::Unsupported);
    assert_eq!(error.message, "unknown host reply kind");
    s.barrier().await; // No unsolicited Error back to the host.
    s.tx.send(pb::HostMsg {
        id: String::new(),
        msg: None,
    })
    .await
    .unwrap();
    s.barrier().await; // Unknown push neither answers nor faults.
    let next = s
        .ctx()
        .start_request(Plugin::GetViewState(pb::GetViewState {}))
        .unwrap();
    let request = s.recv().await;
    s.send(&request.id, Host::ViewState(pb::ViewState::default()))
        .await;
    let reply = tokio::task::spawn_blocking(move || next.wait(Duration::from_secs(2)))
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(reply.message, Host::ViewState(_)));
    s.stop().await;
}

struct TestReader;
impl vizi_doorbell::plugin::Reader for TestReader {
    fn info(&self) -> vizi_doorbell::control::Result<pb::ReaderOpened> {
        Ok(pb::ReaderOpened {
            shape_tczyx: vec![1, 1, 1, 1, 5],
            dtype: 1,
            little_endian: true,
            ..Default::default()
        })
    }
    fn read_volume(
        &self,
        _: &pb::ReadVolume,
    ) -> vizi_doorbell::control::Result<vizi_doorbell::plugin::Volume> {
        Ok(vizi_doorbell::plugin::Volume {
            bytes: b"reader"[..5].to_vec(),
            shape_zyx: [1, 1, 5],
            dtype: pb::DType::DtypeU8,
        })
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_factory_routes_and_deposits_into_granted_arena() {
    let app = Registry::new().reader("raw-reader", |_, _| Ok(Arc::new(TestReader)));
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenReader(pb::OpenReader {
            uri: "test.raw".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    let handle = match s.recv().await.msg {
        Some(Plugin::ReaderOpened(r)) => r.handle,
        other => panic!("{other:?}"),
    };
    // G is sized from ReaderOpened and acknowledged before the first read.
    let file = grant(&s, 11, 512);
    let mut i = install(11);
    i.purpose = "G".into();
    i.regions[0].producer = 2;
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.send(
        "read",
        Host::ReadVolume(pb::ReadVolume {
            handle: handle.clone(),
            ..Default::default()
        }),
    )
    .await;
    let d = match s.recv().await.msg {
        Some(Plugin::Segment(d)) => d,
        other => panic!("{other:?}"),
    };
    assert_eq!((d.segment_id, d.len), (11, 5));
    let mut bytes = [0; 5];
    file.read_exact_at(&mut bytes, d.offset).unwrap();
    assert_eq!(&bytes, b"reade");
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: 11,
            region: 0,
            offset: d.offset,
            len: d.len,
        }),
    )
    .await;
    s.send(
        "plane",
        Host::ReadPlane(pb::ReadPlane {
            handle: handle.clone(),
            ..Default::default()
        }),
    )
    .await;
    let plane = match s.recv().await.msg {
        Some(Plugin::Segment(d)) => d,
        other => panic!("{other:?}"),
    };
    assert_eq!(plane.shape_zyx, [1, 1, 5]);
    file.read_exact_at(&mut bytes, plane.offset).unwrap();
    assert_eq!(&bytes, b"reade");
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: plane.segment_id,
            region: 0,
            offset: plane.offset,
            len: plane.len,
        }),
    )
    .await;
    s.send("close", Host::CloseReader(pb::CloseReader { handle }))
        .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Closed(_))));
    s.stop().await;
}
struct TestExporter(Arc<AtomicUsize>);
impl vizi_doorbell::plugin::Exporter for TestExporter {
    fn write(&self, _: u32, channels: &[u32], bytes: &[u8]) -> vizi_doorbell::control::Result<u64> {
        assert_eq!(channels, &[0, 1]);
        assert_eq!(bytes.len(), 10);
        self.0.fetch_add(bytes.len(), Ordering::SeqCst);
        Ok(bytes.len() as u64)
    }
    fn finish(&self) -> vizi_doorbell::control::Result<pb::ExportDone> {
        Ok(Default::default())
    }
    fn abort(&self) -> vizi_doorbell::control::Result<()> {
        Err(vizi_doorbell::control::Error::internal(
            "fixture cleanup failure",
        ))
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn file_state_follows_each_view_reader_and_export_source_without_creating_folders() {
    use std::path::Path;
    let temp = tempfile::tempdir().unwrap();
    let a = temp
        .path()
        .join("dataset-a")
        .join("plugins")
        .join("test.plugin");
    let b = temp
        .path()
        .join("dataset-b")
        .join("plugins")
        .join("test.plugin");
    let seen = Arc::new(Mutex::new(Vec::new()));
    let readers = seen.clone();
    let exporters = seen.clone();
    let app = Registry::new()
        .reader("raw-reader", move |_, open| {
            readers
                .lock()
                .unwrap()
                .push(("reader", open.file_state_dir().map(Path::to_path_buf)));
            Ok(Arc::new(TestReader))
        })
        .exporter("raw", move |_, open| {
            assert_eq!(open.locator, "elsewhere/output.raw");
            exporters
                .lock()
                .unwrap()
                .push(("export", open.file_state_dir().map(Path::to_path_buf)));
            Ok(Arc::new(TestExporter(Arc::new(AtomicUsize::new(0)))))
        });
    let mut s = Session::new(app).await;
    s.recv().await;
    for (index, path) in [Some(a.as_path()), Some(b.as_path()), None]
        .into_iter()
        .enumerate()
    {
        let wire = path.map_or_else(String::new, |p| p.to_str().unwrap().to_owned());
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.view_state());
        let request = s.recv().await;
        assert!(matches!(request.msg, Some(Plugin::GetViewState(_))));
        s.send(
            &request.id,
            Host::ViewState(pb::ViewState {
                file_state_dir: wire.clone(),
                dataset_series: Some(format!("opaque/image/{index}")),
                ..Default::default()
            }),
        )
        .await;
        let view = task.await.unwrap().unwrap();
        assert_eq!(view.file_state_dir(), path);
        assert_eq!(view.dataset_series, Some(format!("opaque/image/{index}")));

        s.send(
            "reader",
            Host::OpenReader(pb::OpenReader {
                uri: format!("source-{index}.raw"),
                reader_id: "raw-reader".into(),
                file_state_dir: wire.clone(),
                ..Default::default()
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::ReaderOpened(_))));
        s.send(
            "export",
            Host::OpenExport(pb::OpenExport {
                exporter_id: "raw".into(),
                locator: "elsewhere/output.raw".into(),
                file_state_dir: wire,
                shape_tczyx: vec![1, 1, 1, 1, 5],
                dtype: 1,
                little_endian: true,
                ..Default::default()
            }),
        )
        .await;
        let handle = match s.recv().await.msg {
            Some(Plugin::ExportOpened(e)) => e.handle,
            other => panic!("{other:?}"),
        };
        s.send("finish", Host::FinishExport(pb::FinishExport { handle }))
            .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::ExportDone(_))));
        assert_eq!(
            seen.lock().unwrap().drain(..).collect::<Vec<_>>(),
            vec![
                ("reader", path.map(Path::to_path_buf)),
                ("export", path.map(Path::to_path_buf)),
            ]
        );
        assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 0);
    }
    s.stop().await;
    assert_eq!(std::fs::read_dir(temp.path()).unwrap().count(), 0);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn exporter_checks_multichannel_length_and_abort_always_answers() {
    let written = Arc::new(AtomicUsize::new(0));
    let w = written.clone();
    let app = Registry::new().exporter("raw", move |_, _| Ok(Arc::new(TestExporter(w.clone()))));
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1, 2, 1, 1, 5],
            dtype: 1,
            little_endian: true,
            ..Default::default()
        }),
    )
    .await;
    let handle = match s.recv().await.msg {
        Some(Plugin::ExportOpened(e)) => e.handle,
        other => panic!("{other:?}"),
    };
    s.send(
        "chunk1",
        Host::ExportChunk(pb::ExportChunk {
            handle: handle.clone(),
            channels: vec![0, 1],
            source: Some(pb::export_chunk::Source::Inline(vec![3; 10])),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg,Some(Plugin::ChunkWritten(c)) if c.total_bytes==10));
    let _file = grant(&s, 12, 512);
    let mut i = install(12);
    i.purpose = "X".into();
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.send(
        "chunk2",
        Host::ExportChunk(pb::ExportChunk {
            handle: handle.clone(),
            channels: vec![0, 1],
            source: Some(pb::export_chunk::Source::Segment(pb::SegmentRef {
                segment_id: 12,
                offset: 64,
                len: 10,
                shape_zyx: vec![1, 1, 5],
                dtype: 1,
                ..Default::default()
            })),
            ..Default::default()
        }),
    )
    .await;
    let mut released = false;
    let mut chunk = false;
    while !released || !chunk {
        match s.recv().await.msg {
            Some(Plugin::ReleaseDeposit(r)) => {
                assert_eq!(r.len, 10);
                released = true;
            }
            Some(Plugin::ChunkWritten(c)) => {
                assert_eq!(c.total_bytes, 10);
                chunk = true;
            }
            m => panic!("{m:?}"),
        }
    }
    s.send(
        "bad",
        Host::ExportChunk(pb::ExportChunk {
            handle: handle.clone(),
            channels: vec![0, 1],
            source: Some(pb::export_chunk::Source::Inline(vec![0; 9])),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Error(_))));
    assert_eq!(written.load(Ordering::SeqCst), 20);
    s.send("abort", Host::AbortExport(pb::AbortExport { handle }))
        .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "abort");
    assert!(matches!(reply.msg, Some(Plugin::ExportDone(_))));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_zero_epoch_costs_only_its_pane() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let mut change = epoch(0, 0);
    let mut good = change.panes[0].clone();
    good.view_id = 1;
    good.epoch = 5;
    change.panes.push(good.clone());
    s.send("", Host::EpochChange(change)).await;
    let mut ack = false;
    let mut report = false;
    while !ack || !report {
        match s.recv().await.msg {
            Some(Plugin::EpochAck(a)) => {
                assert_eq!(a.panes, vec![good.clone()]);
                ack = true;
            }
            Some(Plugin::ContractViolation(v)) => {
                assert_eq!(v.r#where, 4);
                assert_eq!(v.count, 1);
                report = true;
            }
            other => panic!("{other:?}"),
        }
    }
    assert!(s.ctx().pane(0).is_none());
    assert_eq!(s.ctx().pane(1).unwrap().pane, good);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_geometry_metadata_does_not_gate_epoch_acknowledgement() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let mut change = epoch(8, 0);
    change.datasets[0].geometry = None;
    change.datasets.push(pb::DatasetState {
        gen: 7,
        geometry: Some(pb::Geometry {
            gen: 0,
            channels: vec![pb::ChannelInfo {
                index: 0,
                name: "one named channel".into(),
                source: pb::ChannelNameSource::File as i32,
            }],
            ..Default::default()
        }),
        time_mapping_revision: 0,
    });
    change.panes.push(pb::PaneEpoch {
        view_id: 1,
        epoch: 5,
        gen: 7,
        channels_high: vec![16],
    });
    // A missing time arrangement still costs only the pane referencing it.
    change.datasets.push(pb::DatasetState {
        gen: 9,
        geometry: None,
        time_mapping_revision: 1,
    });
    change.panes.push(pb::PaneEpoch {
        view_id: 2,
        epoch: 1,
        gen: 9,
        ..Default::default()
    });
    s.send("", Host::EpochChange(change.clone())).await;
    let mut accepted = false;
    let mut missing = false;
    while !accepted || !missing {
        match s.recv().await.msg {
            Some(Plugin::EpochAck(a)) => {
                assert_eq!(a.panes, change.panes[..2]);
                accepted = true;
            }
            Some(Plugin::ContractViolation(v)) => {
                assert_eq!(v.r#where, 3);
                assert!(v.detail.contains("time mapping"), "{v:?}");
                missing = true;
            }
            other => panic!("{other:?}"),
        }
    }
    for view in 0..2 {
        assert_eq!(
            s.ctx().pane(view).unwrap().dataset,
            change.datasets[view as usize]
        );
    }
    assert!(s.ctx().pane(2).is_none());
    change.panes.pop();
    change.datasets.pop();
    s.send("", Host::EpochChange(change.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(a)) if a.panes == change.panes));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_inline_export_ignores_stray_install_and_writes_payload() {
    let written = Arc::new(AtomicUsize::new(0));
    let w = written.clone();
    let app = Registry::new().exporter("raw", move |_, _| Ok(Arc::new(TestExporter(w.clone()))));
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1, 2, 1, 1, 5],
            dtype: 1,
            little_endian: true,
            ..Default::default()
        }),
    )
    .await;
    let handle = match s.recv().await.msg {
        Some(Plugin::ExportOpened(e)) => e.handle,
        other => panic!("{other:?}"),
    };
    s.send(
        "chunk",
        Host::ExportChunk(pb::ExportChunk {
            handle,
            channels: vec![0, 1],
            source: Some(pb::export_chunk::Source::Inline(vec![3; 10])),
            install: Some(install(99)),
            ..Default::default()
        }),
    )
    .await;
    let mut reported = false;
    let mut consumed = false;
    while !reported || !consumed {
        let message = s.recv().await;
        match message.msg {
            Some(Plugin::ContractViolation(v)) => {
                assert_eq!(v.r#where, 17);
                assert_eq!(v.count, 1);
                reported = true;
            }
            Some(Plugin::ChunkWritten(c)) => {
                assert_eq!(message.id, "chunk");
                assert_eq!(c.total_bytes, 10);
                consumed = true;
            }
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(written.load(Ordering::SeqCst), 10);
    assert_eq!(
        s.ctx().memory().query(99).state,
        1,
        "stray installation must not enter the grant manager"
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_doorbell_fault_includes_known_supervisor_health() {
    let app = Registry::new().command("run", |_, _| Ok(done()));
    let mut s = Session::new(app).await;
    s.recv().await;
    let bad_crc = [0u8; 112];
    assert_eq!(
        unsafe {
            libc::send(
                s.peer.as_raw_fd(),
                bad_crc.as_ptr().cast(),
                bad_crc.len(),
                libc::MSG_NOSIGNAL,
            )
        },
        112
    );
    match s.recv().await.msg {
        Some(Plugin::DoorbellFault(f)) => {
            assert_eq!(f.failure_class, 1);
            assert_eq!(f.capture, Some(bad_crc.to_vec()));
            assert_eq!(f.sdk_version.as_deref(), Some(env!("CARGO_PKG_VERSION")));
            assert_eq!(f.peer_protocol_major, Some(1));
            assert_eq!(f.peer_protocol_minor, Some(23));
            assert_eq!(f.process_healthy_at_detection, Some(true));
            assert_eq!(f.stream_healthy_at_detection, Some(true));
            assert_eq!(f.manifest_source, None); // Manifest::parse has no file source.
        }
        other => panic!("{other:?}"),
    }
    s.send(
        "still-live",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "still-live");
    assert!(matches!(reply.msg, Some(Plugin::CommandDone(_))));
    // A doorbell fault alone leaves control usable (§8.13); the host follows
    // with Shutdown to end the session (§3.3). No explicit Runtime::stop here.
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(1000),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ShutdownReport(_))
    ));
    assert!(tokio::time::timeout(Duration::from_secs(2), s.rx.message())
        .await
        .unwrap()
        .unwrap()
        .is_none());
    tokio::time::timeout(Duration::from_secs(2), s.server)
        .await
        .unwrap()
        .unwrap()
        .unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_over_bound_host_reply_wakes_its_waiter_with_protocol_error() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    let waiter = tokio::task::spawn_blocking(move || {
        ctx.request_timeout(
            Plugin::GetRois(pb::GetRois::default()),
            Duration::from_secs(2),
        )
    });
    let request = s.recv().await;
    assert!(matches!(request.msg, Some(Plugin::GetRois(_))));
    s.send(
        &request.id,
        Host::Rois(pb::RoiList {
            rois: vec![pb::RoiRecord::default(); 65537],
        }),
    )
    .await;
    let error = match waiter.await.unwrap() {
        Err(e) => e,
        Ok(_) => panic!("oversized reply accepted"),
    };
    assert_eq!(error.kind, pb::error::Kind::Protocol);
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 21));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn review_held_request_answers_not_found_when_grant_retires() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    s.send(
        "held",
        Host::ExportChunk(pb::ExportChunk {
            handle: "unopened".into(),
            source: Some(pb::export_chunk::Source::Segment(pb::SegmentRef {
                segment_id: 99,
                offset: 64,
                len: 5,
                shape_zyx: vec![1, 1, 5],
                dtype: 1,
                ..Default::default()
            })),
            ..Default::default()
        }),
    )
    .await;
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 99 }),
    )
    .await;
    let mut released = false;
    let mut refused = false;
    while !released || !refused {
        let message = s.recv().await;
        match message.msg {
            Some(Plugin::SegmentReleased(r)) => {
                assert_eq!(r.segment_id, 99);
                released = true;
            }
            Some(Plugin::Error(e)) => {
                assert_eq!(message.id, "held");
                assert_eq!(e.kind, pb::error::Kind::NotFound as i32);
                refused = true;
            }
            other => panic!("{other:?}"),
        }
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn unusual_time_mappings_are_installed_and_replayed_without_losing_nan() {
    for values in [vec![2., 1.], vec![f64::NAN], vec![f64::INFINITY]] {
        let mut s = Session::new(Registry::new()).await;
        s.recv().await;
        let mapping = pb::TimeMapping {
            gen: 4,
            revision: 1,
            t: values.clone(),
        };
        for _ in 0..2 {
            s.send("", Host::TimeMapping(mapping.clone())).await;
            assert!(
                matches!(s.recv().await.msg, Some(Plugin::TimeMappingAck(a)) if a.gen == 4 && a.revision == 1)
            );
            s.send("", Host::EpochChange(epoch(8, 1))).await;
            assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
            let snapshot = s.ctx().pane(0).unwrap();
            for (index, value) in values.iter().enumerate() {
                assert_eq!(
                    snapshot.time_at(index as u32).unwrap().to_bits(),
                    value.to_bits()
                );
            }
        }
        s.send(
            "",
            Host::TimeMapping(pb::TimeMapping {
                t: vec![7.],
                ..mapping
            }),
        )
        .await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 19)
        );
        assert_eq!(
            s.ctx().pane(0).unwrap().time_at(0).unwrap().to_bits(),
            values[0].to_bits()
        );
        s.barrier().await;
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retired_tools_still_cannot_be_reactivated() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    s.send("", Host::ToolBinding(binding(1))).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    let mut retired = binding(2);
    retired.retired = true;
    s.send("", Host::ToolBinding(retired)).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    s.send("", Host::ToolBinding(binding(3))).await;
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn duplicate_entries_and_wide_pane_ids_are_installed_without_unrelated_sites() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let mut change = epoch(1, 0);
    change.panes.push(change.panes[0].clone());
    let mut valid = change.panes[0].clone();
    valid.view_id = 1;
    change.panes.push(valid.clone());
    let mut wide = valid.clone();
    wide.view_id = 256;
    change.panes.push(wide.clone());
    let mut dataset = change.datasets[0].clone();
    dataset.geometry = None;
    change.datasets.push(dataset.clone());
    let expected = vec![change.panes[0].clone(), valid, wide];
    for _ in 0..2 {
        s.send("", Host::EpochChange(change.clone())).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(a)) if a.panes == expected));
    }
    s.barrier().await;
    for view in [0, 1, 256] {
        assert_eq!(s.ctx().pane_by_id(view).unwrap().dataset, dataset);
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn repeated_doorbell_shutdown_reports_once_and_lifecycle_refusals_stay_local() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let shutdown = pb::DoorbellShutdown {
        reason: 3,
        failure_class: Some(1),
        detail: "fixture framing fault".into(),
        deadline_ms: 5000,
    };
    s.send("", Host::DoorbellShutdown(shutdown.clone())).await;
    match s.recv().await.msg {
        Some(Plugin::DoorbellFinalReport(r)) => {
            assert!(r.detail.contains("FramingFault"), "{r:?}");
            assert!(r.detail.contains("failure_class=Some(1)"), "{r:?}");
            assert!(r.detail.contains("fixture framing fault"), "{r:?}");
        }
        other => panic!("{other:?}"),
    }
    s.send("", Host::DoorbellShutdown(shutdown)).await;
    s.send("", Host::ToolBinding(binding(1))).await;
    s.send("", Host::EpochChange(epoch(1, 0))).await;
    s.barrier().await;
    assert!(s.ctx().pane(0).is_none());
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn doorbell_shutdown_accepts_peer_closed_and_unknown_reasons() {
    for reason in [7, 999, -1] {
        let mut s = Session::new(Registry::new()).await;
        s.recv().await;
        s.send(
            "",
            Host::DoorbellShutdown(pb::DoorbellShutdown {
                reason,
                deadline_ms: 5000,
                ..Default::default()
            }),
        )
        .await;
        match s.recv().await.msg {
            Some(Plugin::DoorbellFinalReport(r)) => {
                assert!(r.detail.contains(&format!("reason={reason} ")), "{r:?}");
                if reason == 7 {
                    assert!(r.detail.contains("PeerClosed"));
                }
            }
            other => panic!("{other:?}"),
        }
        s.barrier().await;
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn mismatched_ride_along_refuses_message_and_wakes_reply_waiter() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = grant(&s, 7, 512);
    s.send("", Host::InstallSegment(install(7))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    let ctx = s.ctx();
    let waiter = tokio::task::spawn_blocking(move || {
        ctx.request_timeout(
            Plugin::ReadDataVolume(pb::ReadDataVolume::default()),
            Duration::from_secs(2),
        )
    });
    let request = s.recv().await;
    let mut volume = data(7);
    volume.install = Some(install(8));
    s.send(&request.id, Host::DataVolume(volume.clone())).await;
    assert!(matches!(waiter.await.unwrap(), Err(e) if e.kind == pb::error::Kind::Protocol));
    assert!(matches!(s.recv().await.msg, Some(Plugin::ReleaseDeposit(r)) if r.segment_id == 7));
    s.send(
        "bad-chunk",
        Host::ExportChunk(pb::ExportChunk {
            source: Some(pb::export_chunk::Source::Segment(volume.segment.unwrap())),
            install: volume.install,
            ..Default::default()
        }),
    )
    .await;
    let mut error = false;
    let mut release = false;
    while !error || !release {
        let message = s.recv().await;
        match message.msg {
            Some(Plugin::Error(e)) => {
                assert_eq!(message.id, "bad-chunk");
                assert_eq!(e.kind, pb::error::Kind::Protocol as i32);
                error = true;
            }
            Some(Plugin::ReleaseDeposit(r)) => {
                assert_eq!(r.segment_id, 7);
                release = true;
            }
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(s.ctx().memory().query(8).state, 1);
    s.barrier().await;
    drop(file);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn concurrent_request_with_unresolvable_deposit_does_not_end_session() {
    let app = Registry::new().command("run", |ctx, _| {
        ctx.request(Plugin::GetViewState(pb::GetViewState {}))?;
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let _file = grant(&s, 7, 512);
    s.send("", Host::InstallSegment(install(7))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.send(
        "active",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let waiting = s.recv().await;
    let mut descriptor = data(7).segment.unwrap();
    descriptor.offset = 500; // installed segment, outside its loan arena
    s.send(
        "concurrent",
        Host::ExportChunk(pb::ExportChunk {
            source: Some(pb::export_chunk::Source::Segment(descriptor)),
            ..Default::default()
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "concurrent");
    assert!(
        matches!(reply.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::Protocol as i32)
    );
    s.barrier().await;
    s.send(&waiting.id, Host::ViewState(Default::default()))
        .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "active");
    assert!(matches!(reply.msg, Some(Plugin::CommandDone(_))));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_honors_host_window_longer_than_five_seconds() {
    let app = Registry::new().on_shutdown(|_, s| {
        assert_eq!(s.deadline_ms, Some(8000));
        std::thread::sleep(Duration::from_millis(5250));
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(8000),
            ..Default::default()
        }),
    )
    .await;
    let reply = tokio::time::timeout(Duration::from_secs(9), s.rx.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert!(
        matches!(reply.msg, Some(Plugin::ShutdownReport(r)) if r.outcome == pb::shutdown_report::Outcome::Completed as i32)
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn input_matches_low_32_bits_of_control_generation() {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let app = Registry::new().on_input(move |_, input| {
        tx.send(input).unwrap();
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send("", Host::ToolBinding(binding(1))).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    let generation = (1u64 << 32) + 4;
    let mut change = epoch(1, 0);
    change.panes[0].gen = generation;
    change.datasets[0].gen = generation;
    change.datasets[0].geometry.as_mut().unwrap().gen = generation;
    s.send("", Host::EpochChange(change)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    for (interaction, gen, usable) in [(1, 4, true), (2, 5, false)] {
        let event = InputEvent {
            gen,
            t: 0,
            interaction,
            seq: 0,
            epoch: 1,
            target: None,
            tool: 1,
            channels: 0,
            phase: Phase::Fire,
            view_id: 0,
            level: 0,
            mods: 0,
            click_count: 1,
            miss: true,
            roll_ref_y: false,
            action: 0,
            position: Position::None,
        };
        let record = event.encode(interaction).unwrap();
        assert_eq!(
            unsafe {
                libc::send(
                    s.peer.as_raw_fd(),
                    record.as_ptr().cast(),
                    record.len(),
                    libc::MSG_NOSIGNAL,
                )
            },
            record.len() as isize
        );
        let input = tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(input.usable_context, usable);
        assert_eq!(
            input.state.as_ref().map(|s| s.pane.gen),
            usable.then_some(generation)
        );
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn undeclared_tool_is_acknowledged_but_its_input_is_not_dispatched() {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let app = Registry::new().on_input(move |_, input| {
        tx.send(input.binding.unwrap().tool_id).unwrap();
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let mut unknown = binding(1);
    unknown.tool_id = "nope".into();
    for _ in 0..2 {
        s.send("", Host::ToolBinding(unknown.clone())).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ToolBindingAck(a)) if a.tool == 1 && a.revision == 1)
        );
    }
    // Same identity, different content is still a real contract violation.
    s.send("", Host::ToolBinding(binding(1))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 19));
    let mut known = binding(1);
    known.tool = 2;
    s.send("", Host::ToolBinding(known)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ToolBindingAck(a)) if a.tool == 2));
    s.send("", Host::EpochChange(epoch(1, 0))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    for tool in [1, 2] {
        let record = InputEvent {
            gen: 4,
            t: 0,
            interaction: tool,
            seq: 0,
            epoch: 1,
            target: None,
            tool,
            channels: 0,
            phase: Phase::Fire,
            view_id: 0,
            level: 0,
            mods: 0,
            click_count: 1,
            miss: true,
            roll_ref_y: false,
            action: 0,
            position: Position::None,
        }
        .encode(tool)
        .unwrap();
        assert_eq!(
            unsafe {
                libc::send(
                    s.peer.as_raw_fd(),
                    record.as_ptr().cast(),
                    record.len(),
                    libc::MSG_NOSIGNAL,
                )
            },
            record.len() as isize
        );
    }
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap(),
        "tool"
    );
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_applies_standalone_install_but_not_refused_request_ride_along() {
    let (release, blocked) = std::sync::mpsc::channel();
    let blocked = Mutex::new(blocked);
    let (entered, mut started) = tokio::sync::mpsc::unbounded_channel();
    let app = Registry::new().on_shutdown(move |_, _| {
        entered.send(()).unwrap();
        blocked
            .lock()
            .unwrap()
            .recv_timeout(Duration::from_secs(5))
            .unwrap();
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let _first = grant(&s, 7, 512);
    let _second = grant(&s, 8, 512);
    s.send("", Host::InstallSegment(install(8))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == 8));
    tokio::time::timeout(Duration::from_secs(2), async {
        while s.ctx().memory().query(7).state != 2 {
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    })
    .await
    .unwrap();
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(10000),
            ..Default::default()
        }),
    )
    .await;
    tokio::time::timeout(Duration::from_secs(2), started.recv())
        .await
        .unwrap()
        .unwrap();
    for _ in 0..2 {
        s.send("", Host::InstallSegment(install(7))).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == 7 && matches!(a.outcome, Some(pb::grant_ack::Outcome::Installed(_))))
        );
    }
    s.send(
        "refused",
        Host::ExportChunk(pb::ExportChunk {
            source: Some(pb::export_chunk::Source::Segment(data(8).segment.unwrap())),
            install: Some(install(66)),
            ..Default::default()
        }),
    )
    .await;
    let mut refusal = false;
    let mut disposed = false;
    while !refusal || !disposed {
        let message = s.recv().await;
        match message.msg {
            Some(Plugin::Error(e)) => {
                assert_eq!(message.id, "refused");
                assert_eq!(e.kind, pb::error::Kind::Protocol as i32);
                refusal = true;
            }
            Some(Plugin::ReleaseDeposit(r)) => {
                assert_eq!(r.segment_id, 8);
                disposed = true;
            }
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(s.ctx().memory().query(7).state, 3);
    assert_eq!(s.ctx().memory().query(66).state, 1);
    release.send(()).unwrap();
    assert!(matches!(s.recv().await.msg, Some(Plugin::ShutdownReport(r)) if r.outcome == 2));
    s.stop().await;
}

struct LifecycleReader {
    name: String,
    events: Arc<Mutex<Vec<String>>>,
}
impl vizi_doorbell::plugin::Reader for LifecycleReader {
    fn info(&self) -> control::Result<pb::ReaderOpened> {
        if self.name == "bad-info" {
            return Err(control::Error::internal("fixture info failure"));
        }
        TestReader.info()
    }
    fn read_volume(&self, _: &pb::ReadVolume) -> control::Result<plugin::Volume> {
        panic!("invalid read reached reader")
    }
    fn read_plane(&self, _: &pb::ReadPlane) -> control::Result<plugin::Volume> {
        panic!("invalid plane reached reader")
    }
    fn close(&self) -> control::Result<()> {
        self.events
            .lock()
            .unwrap()
            .push(format!("close:{}", self.name));
        if self.name == "close-error" {
            return Err(control::Error::internal("fixture close failure"));
        }
        Ok(())
    }
}
async fn open_reader(s: &mut Session, uri: &str) -> String {
    s.send(
        "open",
        Host::OpenReader(pb::OpenReader {
            uri: uri.into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    match s.recv().await.msg {
        Some(Plugin::ReaderOpened(r)) => r.handle,
        other => panic!("{other:?}"),
    }
}
async fn assert_reader_present(s: &mut Session, handle: &str) {
    s.send(
        "metadata",
        Host::Metadata(pb::Metadata {
            handle: handle.into(),
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Metadata(_))));
}
async fn assert_reader_absent(s: &mut Session, handle: &str) {
    for request in [
        Host::ReadVolume(pb::ReadVolume {
            handle: handle.into(),
            ..Default::default()
        }),
        Host::ReadPlane(pb::ReadPlane {
            handle: handle.into(),
            ..Default::default()
        }),
        Host::Metadata(pb::Metadata {
            handle: handle.into(),
        }),
    ] {
        s.send("absent", request).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::Error(e))
            if e.kind == pb::error::Kind::NotFound as i32 && e.message.contains(handle)));
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_replacement_opens_before_close_and_failed_open_preserves_previous() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let e = events.clone();
    let app = Registry::new().reader("raw-reader", move |_, open| {
        e.lock().unwrap().push(format!("open:{}", open.uri));
        if open.uri == "fail" {
            return Err(control::Error::new(
                pb::error::Kind::NotFound,
                "fixture open failure",
            ));
        }
        Ok(Arc::new(LifecycleReader {
            name: open.uri.clone(),
            events: e.clone(),
        }))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    assert_reader_absent(&mut s, "never-opened").await;
    let first = open_reader(&mut s, "close-error").await;
    let second = open_reader(&mut s, "second").await;
    assert_ne!(first, second);
    assert_eq!(
        *events.lock().unwrap(),
        ["open:close-error", "open:second", "close:close-error"]
    );
    assert_reader_absent(&mut s, &first).await;
    // A stale close must not close the replacement.
    s.send(
        "stale-close",
        Host::CloseReader(pb::CloseReader {
            handle: first.clone(),
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Closed(c)) if c.handle == first));
    s.send(
        "metadata",
        Host::Metadata(pb::Metadata {
            handle: second.clone(),
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Metadata(_))));
    s.send(
        "fail",
        Host::OpenReader(pb::OpenReader {
            uri: "fail".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::NotFound as i32)
    );
    assert_reader_present(&mut s, &second).await;
    s.send(
        "bad-info",
        Host::OpenReader(pb::OpenReader {
            uri: "bad-info".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Error(_))));
    assert_reader_present(&mut s, &second).await;
    let third = open_reader(&mut s, "third").await;
    for _ in 0..2 {
        s.send(
            "close",
            Host::CloseReader(pb::CloseReader {
                handle: third.clone(),
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::Closed(c)) if c.handle == third));
    }
    assert_reader_absent(&mut s, &third).await;
    open_reader(&mut s, "last").await;
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(2000),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ShutdownReport(r))
        if r.outcome == pb::shutdown_report::Outcome::NoHook as i32));
    assert_eq!(
        *events.lock().unwrap(),
        [
            "open:close-error",
            "open:second",
            "close:close-error",
            "open:fail",
            "open:bad-info",
            "close:bad-info",
            "open:third",
            "close:second",
            "close:third",
            "open:last",
            "close:last",
        ]
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_levels_and_axes_are_rejected_before_dispatch() {
    let app = Registry::new().reader("raw-reader", |_, _| {
        Ok(Arc::new(LifecycleReader {
            name: "bounds".into(),
            events: Arc::default(),
        }))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "undeclared",
        Host::OpenReader(pb::OpenReader {
            reader_id: "unknown".into(),
            uri: String::new(),
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::NotFound as i32)
    );
    let handle = open_reader(&mut s, "bounds").await;
    for (request, axis) in [
        (
            Host::ReadVolume(pb::ReadVolume {
                handle: handle.clone(),
                level: 2,
                ..Default::default()
            }),
            "level",
        ),
        (
            Host::ReadPlane(pb::ReadPlane {
                handle: handle.clone(),
                level: 2,
                z: 9,
                ..Default::default()
            }),
            "level",
        ),
        (
            Host::ReadVolume(pb::ReadVolume {
                handle: handle.clone(),
                t: 1,
                ..Default::default()
            }),
            "t index",
        ),
        (
            Host::ReadVolume(pb::ReadVolume {
                handle: handle.clone(),
                c: 1,
                ..Default::default()
            }),
            "c index",
        ),
        (
            Host::ReadPlane(pb::ReadPlane {
                handle,
                z: 1,
                ..Default::default()
            }),
            "z index",
        ),
    ] {
        s.send("bounds", request).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::Error(e))
            if e.kind == pb::error::Kind::OutOfBounds as i32 && e.message.contains(axis)));
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_factory_finishing_during_shutdown_closes_its_reader() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let e = events.clone();
    let (release, blocked) = std::sync::mpsc::channel();
    let blocked = Mutex::new(blocked);
    let (entered, ready) = tokio::sync::oneshot::channel();
    let entered = Mutex::new(Some(entered));
    let app = Registry::new()
        .reader("raw-reader", move |_, _| {
            entered.lock().unwrap().take().unwrap().send(()).unwrap();
            blocked
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(2))
                .unwrap();
            Ok(Arc::new(LifecycleReader {
                name: "late".into(),
                events: e.clone(),
            }))
        })
        .on_shutdown(move |_, _| {
            release.send(()).unwrap();
            Ok(())
        });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenReader(pb::OpenReader {
            uri: "late".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    tokio::time::timeout(Duration::from_secs(2), ready)
        .await
        .unwrap()
        .unwrap();
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(2000),
            ..Default::default()
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "open");
    assert!(matches!(reply.msg, Some(Plugin::Error(e)) if e.message.contains("shutdown")));
    assert!(matches!(s.recv().await.msg, Some(Plugin::ShutdownReport(r))
        if r.outcome == pb::shutdown_report::Outcome::Completed as i32));
    assert_eq!(*events.lock().unwrap(), ["close:late"]);
    s.stop().await;
}

struct SelectionExporter(Arc<AtomicUsize>);
impl vizi_doorbell::plugin::Exporter for SelectionExporter {
    fn write(&self, t: u32, channels: &[u32], bytes: &[u8]) -> control::Result<u64> {
        assert_eq!(t, 7); // The contract has not settled the time index space.
        assert_eq!(channels, &[3]);
        assert_eq!(bytes, &[42; 5]);
        self.0.fetch_add(1, Ordering::SeqCst);
        Ok(bytes.len() as u64)
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        Ok(Default::default())
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn export_source_channel_indices_are_independent_of_artifact_counts() {
    let writes = Arc::new(AtomicUsize::new(0));
    let w = writes.clone();
    let app = Registry::new().exporter("raw", move |_, _| {
        Ok(Arc::new(SelectionExporter(w.clone())))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1, 1, 1, 1, 5],
            dtype: 1,
            little_endian: true,
            ..Default::default()
        }),
    )
    .await;
    let handle = match s.recv().await.msg {
        Some(Plugin::ExportOpened(e)) => e.handle,
        other => panic!("{other:?}"),
    };
    for channels in [vec![3], vec![], vec![3, 4]] {
        let valid = channels.len() == 1;
        s.send(
            "chunk",
            Host::ExportChunk(pb::ExportChunk {
                handle: handle.clone(),
                t: 7,
                source: Some(pb::export_chunk::Source::Inline(vec![
                    42;
                    channels.len() * 5
                ])),
                channels,
                ..Default::default()
            }),
        )
        .await;
        let reply = s.recv().await;
        if valid {
            assert!(matches!(reply.msg, Some(Plugin::ChunkWritten(c)) if c.total_bytes == 5));
        } else {
            assert!(
                matches!(reply.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::OutOfBounds as i32)
            );
        }
    }
    assert_eq!(writes.load(Ordering::SeqCst), 1);
    s.stop().await;
}

fn loan_item(d: pb::SegmentRef) -> pb::OverlayItem {
    pb::OverlayItem {
        geometry: Some(pb::overlay_item::Geometry::LabelVolume(
            pb::OverlayLabelVolume {
                labels: Some(pb::GeomBuffer {
                    source: Some(pb::geom_buffer::Source::Segment(d)),
                    ..Default::default()
                }),
                ..Default::default()
            },
        )),
        ..Default::default()
    }
}
fn loan_push(items: Vec<pb::OverlayItem>) -> Plugin {
    Plugin::OverlayUpdate(pb::OverlayUpdate {
        overlay_id: "overlay".into(),
        items,
        ..Default::default()
    })
}
fn compile(item: pb::OverlayItem) -> Plugin {
    Plugin::CompileLabelVolume(pb::CompileLabelVolume {
        overlay_id: "overlay".into(),
        item: Some(item),
        ..Default::default()
    })
}
async fn admit_compile(s: &mut Session, item: pb::OverlayItem) {
    let ctx = s.ctx();
    let request = tokio::task::spawn_blocking(move || ctx.request(compile(item)));
    let message = s.recv().await;
    assert!(matches!(message.msg, Some(Plugin::CompileLabelVolume(_))));
    s.send(
        &message.id,
        Host::LabelVolumeCompiled(pb::LabelVolumeCompiled::default()),
    )
    .await;
    request.await.unwrap().unwrap();
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn sender_counts_loan_uses_in_both_orders_and_retains_failed_later_sends() {
    for request_first in [false, true] {
        let mut s = Session::new(Registry::new()).await;
        s.recv().await;
        let _file = grant(&s, 71, 512);
        let mut i = install(71);
        i.purpose = "O".into();
        i.subject = "overlay".into();
        i.regions[0].producer = 2;
        s.send("", Host::InstallSegment(i)).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
        let ctx = s.ctx();
        let loan = ctx.memory().reserve(71, b"labels").unwrap();
        let d = pb::SegmentRef {
            segment_id: 71,
            offset: loan.offset,
            len: loan.len,
            uses: 2,
            ..Default::default()
        };
        let item = loan_item(d.clone());
        if request_first {
            admit_compile(&mut s, item.clone()).await;
        } else {
            ctx.push(loan_push(vec![item.clone()])).unwrap();
            assert!(matches!(s.recv().await.msg, Some(Plugin::OverlayUpdate(_))));
        }
        // A refused request must not count as the second use or reclaim the first.
        let mut bad = item.clone();
        bad.item_id = "x".repeat(control::MAX_INLINE_BYTES + 1);
        assert!(ctx.request(compile(bad)).is_err());
        assert!(ctx.memory().cancel_unsent(loan.clone()).is_err());
        // Whole-message failure must not partially count or reclaim either loan.
        let fresh = ctx.memory().reserve(71, b"fresh").unwrap();
        let fresh_item = loan_item(pb::SegmentRef {
            offset: fresh.offset,
            len: fresh.len,
            uses: 1,
            ..d.clone()
        });
        let mismatch = loan_item(pb::SegmentRef {
            uses: 3,
            ..d.clone()
        });
        let error = ctx
            .push(loan_push(vec![fresh_item.clone(), mismatch]))
            .unwrap_err();
        assert!(error.message.contains("disagreement"));
        let error = ctx
            .push_with_loans(
                loan_push(vec![fresh_item, item.clone(), item.clone()]),
                vec![loan.clone(), fresh.clone()],
            )
            .unwrap_err();
        assert!(error.message.contains("twice"));
        // Cleanup proceeds past the retained, previously sent loan to the fresh one.
        assert!(ctx
            .memory()
            .cancel_unsent(fresh)
            .unwrap_err()
            .detail
            .contains("no outstanding"));
        assert!(ctx.memory().cancel_unsent(loan.clone()).is_err());
        if request_first {
            ctx.push(loan_push(vec![item.clone()])).unwrap();
            assert!(matches!(s.recv().await.msg, Some(Plugin::OverlayUpdate(_))));
        } else {
            admit_compile(&mut s, item.clone()).await;
        }
        for result in [
            ctx.push(loan_push(vec![item.clone()])),
            ctx.request(compile(item)).map(|_| ()),
        ] {
            assert!(result.unwrap_err().message.contains("uses exceeded"));
        }
        // Nothing refused reached the stream; only the receiver's release permits reuse.
        s.barrier().await;
        s.send(
            "",
            Host::ReleaseLoan(pb::ReleaseLoan {
                segment_id: 71,
                region: loan.region,
                offset: loan.offset,
                len: loan.len,
            }),
        )
        .await;
        s.barrier().await;
        let reused = ctx.memory().reserve(71, b"labels").unwrap();
        assert_eq!(reused.offset, loan.offset);
        ctx.push(loan_push(vec![loan_item(pb::SegmentRef { uses: 0, ..d })]))
            .unwrap();
        assert!(matches!(s.recv().await.msg, Some(Plugin::OverlayUpdate(_))));
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn image_references_have_one_use_and_concurrent_sends_cannot_exceed_it() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _file = grant(&s, 72, 512);
    let mut i = install(72);
    i.purpose = "P".into();
    i.subject = "panel".into();
    i.regions[0].producer = 2;
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    let ctx = s.ctx();
    let loan = ctx.memory().reserve(72, b"png").unwrap();
    let patch = Plugin::PanelPatch(pb::PanelPatch {
        panel_id: "panel".into(),
        ops: vec![pb::PatchOp {
            row_key: String::new(),
            widget_id: "image".into(),
            op: Some(pb::patch_op::Op::SetImage(pb::ImageRef {
                segment_id: 72,
                offset: loan.offset,
                len: loan.len,
            })),
        }],
    });
    let mut tasks = Vec::new();
    for _ in 0..8 {
        let ctx = ctx.clone();
        let patch = patch.clone();
        tasks.push(tokio::task::spawn_blocking(move || ctx.push(patch)));
    }
    let mut accepted = 0;
    for task in tasks {
        match task.await.unwrap() {
            Ok(()) => accepted += 1,
            Err(e) => assert!(e.message.contains("uses exceeded")),
        }
    }
    assert_eq!(accepted, 1);
    assert!(ctx.memory().cancel_unsent(loan).is_err());
    assert!(matches!(s.recv().await.msg, Some(Plugin::PanelPatch(_))));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn input_context_reports_failed_clauses_and_exempts_ray_endpoints() {
    let ray = Ray {
        camera: [-100.; 3],
        far: [100.; 3],
        entry_frac: 0.,
        roll: 0.,
    };
    for (level, t, mapped, miss, position, reason) in [
        (2, 0, false, false, Position::Voxel([2.; 3]), Some("level")),
        (1, 1, true, false, Position::Voxel([2.; 3]), Some("time")),
        (1, 999, false, false, Position::Voxel([2.; 3]), None),
        (
            1,
            0,
            false,
            false,
            Position::Voxel([2., 2., 4.]),
            Some("coordinate"),
        ),
        (
            1,
            0,
            false,
            false,
            Position::Picked([2., 4., 2.]),
            Some("coordinate"),
        ),
        (1, 0, false, true, Position::Voxel([100.; 3]), None),
        (1, 0, false, true, Position::None, None),
        (1, 0, false, false, Position::Ray(ray.clone()), None),
        (
            1,
            0,
            false,
            false,
            Position::RayAndPicked {
                ray: ray.clone(),
                picked: [2.; 3],
                back_frac: 0.5,
            },
            None,
        ),
        (
            1,
            0,
            false,
            false,
            Position::RayAndPicked {
                ray: ray.clone(),
                picked: [100.; 3],
                back_frac: 0.5,
            },
            Some("coordinate"),
        ),
    ] {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut s = Session::new(Registry::new().on_input(move |_, input| {
            tx.send(input).unwrap();
            Ok(())
        }))
        .await;
        s.recv().await;
        s.send("", Host::ToolBinding(binding(1))).await;
        assert!(matches!(
            s.recv().await.msg,
            Some(Plugin::ToolBindingAck(_))
        ));
        if mapped {
            s.send(
                "",
                Host::TimeMapping(pb::TimeMapping {
                    gen: 4,
                    revision: 1,
                    t: vec![0.],
                }),
            )
            .await;
            assert!(matches!(
                s.recv().await.msg,
                Some(Plugin::TimeMappingAck(_))
            ));
        }
        let mut change = epoch(1, u32::from(mapped));
        let g = change.datasets[0].geometry.as_mut().unwrap();
        g.levels.push(pb::LevelScale {
            z: 2.,
            y: 4.,
            x: 8.,
        });
        g.extent = Some(pb::Extent {
            z_n: 32,
            y_n: 32,
            x_n: 32,
            ..Default::default()
        });
        g.crop = Some(pb::EdgeBox {
            z0: 3.5,
            z1: 11.5,
            y0: 7.5,
            y1: 15.5,
            x0: 15.5,
            x1: 31.5,
        });
        s.send("", Host::EpochChange(change)).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
        let event = InputEvent {
            gen: 4,
            t,
            interaction: 1,
            seq: 0,
            epoch: 1,
            target: None,
            tool: 1,
            channels: 0,
            phase: Phase::Fire,
            view_id: 0,
            level,
            mods: 0,
            click_count: 1,
            miss,
            roll_ref_y: false,
            action: 0,
            position,
        };
        for interaction in 1..=2 {
            let mut event = event.clone();
            event.interaction = interaction;
            if interaction == 2 {
                event.level = 1;
                event.t = 0;
                event.miss = false;
                event.position = Position::Voxel([2.; 3]);
            }
            let record = event.encode(interaction).unwrap();
            assert_eq!(
                unsafe {
                    libc::send(
                        s.peer.as_raw_fd(),
                        record.as_ptr().cast(),
                        record.len(),
                        libc::MSG_NOSIGNAL,
                    )
                },
                record.len() as isize
            );
            let input = tokio::time::timeout(Duration::from_secs(2), rx.recv())
                .await
                .unwrap()
                .unwrap();
            assert_eq!(input.usable_context, interaction == 2 || reason.is_none());
            assert_eq!(input.event.interaction, interaction);
            if interaction == 1 {
                if let Some(reason) = reason {
                    let report = s.recv().await;
                    assert!(
                        matches!(report.msg, Some(Plugin::ContractViolation(v)) if v.r#where == pb::ViolationSite::UnusableContext as i32 && v.count == 1 && v.detail.contains(reason))
                    );
                }
            }
        }
        s.barrier().await;
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn action_fire_resolves_captured_binding_without_disturbing_a_drag() {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let mut s = Session::new(Registry::new().on_input(move |_, input| {
        tx.send(input).unwrap();
        Ok(())
    }))
    .await;
    s.recv().await;
    s.send("", Host::ToolBinding(binding(1))).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    s.send("", Host::EpochChange(epoch(1, 0))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    let mut captured = None;
    for (id, phase, interaction, seq, action, expected) in [
        (1, Phase::Press, 1, 0, 0, None),
        (2, Phase::Fire, 2, 0, 1, Some("add_node")),
        (3, Phase::Drag, 1, 1, 0, None),
        (4, Phase::Fire, 3, 0, 65535, None),
        (5, Phase::Release, 1, 2, 0, None),
        (6, Phase::Fire, 4, 0, 1, Some("add_node")),
        (7, Phase::Fire, 5, 0, 0, None),
    ] {
        let event = InputEvent {
            gen: 4,
            t: 0,
            interaction,
            seq,
            epoch: 1,
            target: None,
            tool: 1,
            channels: 0,
            phase,
            view_id: 0,
            level: 0,
            mods: 0,
            click_count: 1,
            miss: false,
            roll_ref_y: false,
            action,
            position: Position::None,
        };
        let record = event.encode(id).unwrap();
        assert_eq!(
            unsafe {
                libc::send(
                    s.peer.as_raw_fd(),
                    record.as_ptr().cast(),
                    record.len(),
                    libc::MSG_NOSIGNAL,
                )
            },
            record.len() as isize
        );
        let input = tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap()
            .unwrap();
        assert_eq!(input.event, event);
        assert!(input.usable_context);
        assert_eq!(input.implicitly_closed, None);
        assert_eq!(input.action(), expected);
        assert_eq!(
            input.binding.as_ref().unwrap().triggers,
            binding(1).triggers
        );
        if action == 1 {
            captured = Some(input);
        }
        s.barrier().await; // No violation or fault accompanies any action.
    }
    let mut changed = binding(2);
    changed.triggers.clear();
    changed.actions[0].key.clear();
    changed.actions[0].action_id = "updated".into();
    s.send("", Host::ToolBinding(changed.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ToolBindingAck(a)) if a.revision == 2));
    s.send("", Host::ToolBinding(changed)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ToolBindingAck(a)) if a.revision == 2));
    assert_eq!(captured.unwrap().action(), Some("add_node"));
    s.stop().await;
}

fn example_event(position: Position) -> InputEvent {
    InputEvent {
        gen: 4,
        t: 0,
        interaction: 1,
        seq: 0,
        epoch: 1,
        target: None,
        tool: 1,
        channels: 1,
        phase: Phase::Fire,
        view_id: 0,
        level: 0,
        mods: 0,
        click_count: 1,
        miss: false,
        roll_ref_y: false,
        action: 0,
        position,
    }
}
fn send_input(s: &Session, event: &InputEvent, id: u32) {
    let bytes = event.encode(id).unwrap();
    assert_eq!(
        unsafe {
            libc::send(
                s.peer.as_raw_fd(),
                bytes.as_ptr().cast(),
                bytes.len(),
                libc::MSG_NOSIGNAL,
            )
        },
        bytes.len() as isize
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn columns_exempt_half_open_endpoints_but_voxels_still_check_crop() {
    for (lo, hi) in [(0., 12.), (4., 8.)] {
        let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
        let mut s = Session::new(Registry::new().on_input(move |_, input| {
            tx.send(input).unwrap();
            Ok(())
        }))
        .await;
        s.recv().await;
        s.send("", Host::ToolBinding(binding(1))).await;
        assert!(matches!(
            s.recv().await.msg,
            Some(Plugin::ToolBindingAck(_))
        ));
        let mut change = epoch(1, 0);
        let g = change.datasets[0].geometry.as_mut().unwrap();
        g.extent = Some(pb::Extent {
            z_n: 12,
            y_n: 12,
            x_n: 12,
            ..Default::default()
        });
        g.crop = Some(pb::EdgeBox {
            z0: lo - 0.5,
            z1: hi - 0.5,
            y0: lo - 0.5,
            y1: hi - 0.5,
            x0: lo - 0.5,
            x1: hi - 0.5,
        });
        s.send("", Host::EpochChange(change)).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
        for (n, position) in [
            Position::ColumnYx {
                y: 6.,
                x: 6.,
                z_min: lo as f32,
                z_max: hi as f32,
            },
            Position::ColumnZx {
                z: 6.,
                x: 6.,
                y_min: lo as f32,
                y_max: hi as f32,
            },
            Position::ColumnZy {
                z: 6.,
                y: 6.,
                x_min: lo as f32,
                x_max: hi as f32,
            },
            Position::Voxel([12., 6., 6.]),
        ]
        .into_iter()
        .enumerate()
        {
            let mut e = example_event(position);
            e.interaction = n as u32 + 1;
            send_input(&s, &e, n as u32 + 1);
            let input = tokio::time::timeout(Duration::from_secs(2), rx.recv())
                .await
                .unwrap()
                .unwrap();
            assert_eq!(input.usable_context, n < 3);
        }
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == pb::ViolationSite::UnusableContext as i32 && v.count == 1)
        );
        s.barrier().await;
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn tool_hooks_follow_ack_and_cancel_without_replay_or_refusal_hooks() {
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let activated = tx.clone();
    let input = tx.clone();
    let cancel = tx.clone();
    let app = Registry::new()
        .on_activated(move |ctx, b| {
            assert_eq!(b.actions[0].action_id, "add_node");
            ctx.panel("panel").set_value("status", "active")?;
            activated.send("activated").unwrap();
            Ok(())
        })
        .on_input(move |_, _| {
            input.send("input").unwrap();
            Ok(())
        })
        .on_cancel(move |_, c| {
            assert_eq!(c.reason, CancellationReason::ToolRetired);
            cancel.send("cancelled").unwrap();
            Ok(())
        })
        .on_deactivated(move |_, b| {
            assert!(b.retired);
            tx.send("deactivated").unwrap();
            Ok(())
        });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send("", Host::ToolBinding(binding(1))).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    assert!(matches!(s.recv().await.msg, Some(Plugin::PanelPatch(_))));
    assert_eq!(rx.recv().await, Some("activated"));
    for revision in [1, 2] {
        s.send("", Host::ToolBinding(binding(revision))).await;
        assert!(matches!(
            s.recv().await.msg,
            Some(Plugin::ToolBindingAck(_))
        ));
    }
    s.send("", Host::EpochChange(epoch(1, 0))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    let mut e = example_event(Position::None);
    e.phase = Phase::Press;
    send_input(&s, &e, 1);
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap(),
        Some("input")
    );
    let mut retired = binding(3);
    retired.retired = true;
    s.send("", Host::ToolBinding(retired.clone())).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap(),
        Some("cancelled")
    );
    assert_eq!(
        tokio::time::timeout(Duration::from_secs(2), rx.recv())
            .await
            .unwrap(),
        Some("deactivated")
    );
    s.send("", Host::ToolBinding(retired)).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ToolBindingAck(_))
    ));
    let mut refused = binding(1);
    refused.tool = 0;
    s.send("", Host::ToolBinding(refused)).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ContractViolation(_))
    ));
    s.barrier().await;
    assert!(tokio::time::timeout(Duration::from_millis(50), rx.recv())
        .await
        .is_err());
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_drains_deferred_violation_counts_within_the_rate_and_deadline() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let memory = s.ctx().memory().clone();
    let site = pb::ViolationSite::UnusableContext as i32;
    memory.report(site, "first", vec![]);
    let mut total = match s.recv().await.msg {
        Some(Plugin::ContractViolation(v)) => v.count,
        other => panic!("{other:?}"),
    };
    for _ in 0..2 {
        memory.report(site, "repeat", vec![]);
    }
    total += match s.recv().await.msg {
        Some(Plugin::ContractViolation(v)) => v.count,
        other => panic!("{other:?}"),
    };
    assert_eq!(total, 3);
    for _ in 0..2 {
        memory.report(site, "deferred", vec![]);
    }
    let started = std::time::Instant::now();
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(2000),
            ..Default::default()
        }),
    )
    .await;
    loop {
        match s.recv().await.msg {
            Some(Plugin::ContractViolation(v)) => {
                assert_eq!(v.r#where, site);
                total += v.count;
            }
            Some(Plugin::ShutdownReport(_)) => break,
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(total, 5);
    assert!(started.elapsed() < Duration::from_secs(2));
    assert!(started.elapsed() >= Duration::from_millis(900));
    s.stop().await;
}

struct FailedFinish;
impl vizi_doorbell::plugin::Exporter for FailedFinish {
    fn write(&self, _: u32, _: &[u32], _: &[u8]) -> control::Result<u64> {
        panic!("closed exporter called")
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        Err(control::Error::internal("finalization failed"))
    }
}
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_finish_consumes_export_handle() {
    let mut s =
        Session::new(Registry::new().exporter("raw", |_, _| Ok(Arc::new(FailedFinish)))).await;
    s.recv().await;
    s.send(
        "open",
        Host::OpenExport(pb::OpenExport {
            exporter_id: "raw".into(),
            shape_tczyx: vec![1; 5],
            dtype: 1,
            little_endian: true,
            ..Default::default()
        }),
    )
    .await;
    let handle = match s.recv().await.msg {
        Some(Plugin::ExportOpened(e)) => e.handle,
        other => panic!("{other:?}"),
    };
    s.send(
        "finish",
        Host::FinishExport(pb::FinishExport {
            handle: handle.clone(),
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.message == "finalization failed")
    );
    for request in [
        Host::ExportChunk(pb::ExportChunk {
            handle: handle.clone(),
            channels: vec![0],
            source: Some(pb::export_chunk::Source::Inline(vec![1])),
            ..Default::default()
        }),
        Host::FinishExport(pb::FinishExport { handle }),
    ] {
        s.send("closed", request).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::NotFound as i32)
        );
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn typed_volume_owns_values_and_releases_deposits_on_success_and_refusal() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = grant(&s, 91, 512);
    file.write_all_at(&[0, 1, 255, 255], 64).unwrap();
    for (raw, wrong_coordinates) in [(false, false), (false, true), (true, false), (true, true)] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || {
            if raw {
                ctx.read_volume_raw(2, 3, 0, Some(([1, 2, 3], [2, 3, 5])))
            } else {
                ctx.read_volume(2, 3, 0, Some(([1, 2, 3], [2, 3, 5])))
            }
        });
        let request = s.recv().await;
        assert!(
            matches!(request.msg, Some(Plugin::ReadDataVolume(v)) if (v.t, v.c, v.level, v.raw) == (2, 3, 0, raw) && v.roi_min_zyx == [1, 2, 3] && v.roi_max_zyx == [2, 3, 5])
        );
        let mut volume = data(91);
        volume.t = if wrong_coordinates { 1 } else { 2 };
        volume.c = 3;
        volume.open_generation = 42;
        let d = volume.segment.as_mut().unwrap();
        d.len = 4;
        d.shape_zyx = vec![1, 1, 2];
        d.dtype = pb::DType::DtypeU16 as i32;
        s.send(&request.id, Host::DataVolume(volume)).await;
        loop {
            match s.recv().await.msg {
                Some(Plugin::GrantAck(_)) => {}
                Some(Plugin::ReleaseDeposit(r)) => {
                    assert_eq!((r.segment_id, r.offset, r.len), (91, 64, 4));
                    break;
                }
                other => panic!("{other:?}"),
            }
        }
        let result = task.await.unwrap();
        if wrong_coordinates {
            assert!(result.unwrap_err().message.contains("coordinates"));
        } else {
            let result = result.unwrap();
            assert_eq!(result.open_generation, 42);
            assert_eq!(result.shape_zyx, [1, 1, 2]);
            assert_eq!(result.data, control::VoxelData::U16(vec![256, 65535]));
        }
    }
    s.stop().await;
}

fn chunk_answer(value: u8) -> vizi_doorbell::plugin::Chunk {
    vizi_doorbell::plugin::Chunk {
        labels: vizi_doorbell::plugin::Volume {
            bytes: vec![value; 192],
            shape_zyx: [2, 8, 12],
            dtype: pb::DType::DtypeU8,
        },
        position_zyx: [3, 4, 5],
    }
}
fn want(t: u32) -> pb::ChunkWant {
    pb::ChunkWant {
        level: 0,
        t: Some(t),
    }
}
async fn chunk_arena(s: &mut Session, id: u64, supersedes: u64) -> std::fs::File {
    let file = grant(s, id, 512);
    let mut i = install(id);
    i.purpose = "O".into();
    i.subject = "chunks".into();
    i.supersedes = supersedes;
    i.regions[0].producer = 2;
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == id));
    file
}
async fn wants(s: &Session, values: Vec<pb::ChunkWant>) {
    s.send(
        "",
        Host::ChunksWanted(pb::ChunksWanted {
            overlay_id: "chunks".into(),
            wants: values,
        }),
    )
    .await;
}
async fn received_chunk(s: &mut Session) -> pb::OverlayChunk {
    match s.recv().await.msg {
        Some(Plugin::OverlayChunk(c)) => c,
        other => panic!("expected chunk, got {other:?}"),
    }
}
fn chunk_descriptor(c: &pb::OverlayChunk) -> &pb::SegmentRef {
    match c.labels.as_ref().unwrap().source.as_ref().unwrap() {
        pb::geom_buffer::Source::Segment(d) => d,
        _ => panic!("chunk must be segment-backed"),
    }
}
async fn release_chunk(s: &Session, c: &pb::OverlayChunk) {
    let d = chunk_descriptor(c);
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: d.segment_id,
            region: 0,
            offset: d.offset,
            len: d.len,
        }),
    )
    .await;
}
async fn no_chunk(s: &mut Session) {
    assert!(
        tokio::time::timeout(Duration::from_millis(100), s.rx.message())
            .await
            .is_err(),
        "unexpected output (including shortfall/violation)"
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn chunk_sources_serve_in_order_defer_without_shortfall_and_reconcile_every_set() {
    let calls = Arc::new(Mutex::new(Vec::new()));
    let record = calls.clone();
    let mut s = Session::new(Registry::new().chunk_source("chunks", move |_, w| {
        record.lock().unwrap().push(*w);
        Ok(Some(chunk_answer(w.t.unwrap() as u8)))
    }))
    .await;
    s.recv().await;
    let file = chunk_arena(&mut s, 80, 0).await;
    wants(&s, vec![want(2), want(0), want(3)]).await;
    for t in [2, 0, 3] {
        let c = received_chunk(&mut s).await;
        assert_eq!((c.level, c.t), (0, Some(t)));
        assert_eq!(c.position_zyx, [3, 4, 5]);
        assert_eq!(c.labels.as_ref().unwrap().shape, [2, 8, 12]);
        let d = chunk_descriptor(&c);
        assert_eq!((d.segment_id, d.dtype, d.uses), (80, 1, 1));
        let mut bytes = [0; 192];
        file.read_exact_at(&mut bytes, d.offset).unwrap();
        assert_eq!(bytes, [t as u8; 192]);
        // Each chunk fills this small arena. The next answer must wait without
        // any shortfall or refused-push report and without recomputation.
        no_chunk(&mut s).await;
        release_chunk(&s, &c).await;
    }
    assert_eq!(*calls.lock().unwrap(), [want(2), want(0), want(3)]);
    wants(&s, vec![want(2), want(0), want(3)]).await;
    no_chunk(&mut s).await;
    wants(&s, vec![]).await;
    wants(&s, vec![want(0), want(3)]).await;
    for t in [0, 3] {
        let c = received_chunk(&mut s).await;
        assert_eq!(c.t, Some(t));
        release_chunk(&s, &c).await;
    }
    assert_eq!(
        *calls.lock().unwrap(),
        [want(2), want(0), want(3), want(0), want(3)]
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn chunk_invalidate_and_departure_discard_deferred_answers() {
    let calls = Arc::new(AtomicUsize::new(0));
    let record = calls.clone();
    let mut s = Session::new(Registry::new().chunk_source("chunks", move |_, _| {
        Ok(Some(chunk_answer(
            (record.fetch_add(1, Ordering::SeqCst) + 1) as u8,
        )))
    }))
    .await;
    s.recv().await;
    let file = chunk_arena(&mut s, 81, 0).await;
    wants(&s, vec![want(1), want(2)]).await;
    let first = received_chunk(&mut s).await;
    no_chunk(&mut s).await;
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    s.ctx().invalidate_chunks("chunks").unwrap();
    no_chunk(&mut s).await;
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    release_chunk(&s, &first).await;
    let refreshed = received_chunk(&mut s).await;
    assert_eq!(refreshed.t, Some(1));
    let mut bytes = [0; 192];
    file.read_exact_at(&mut bytes, chunk_descriptor(&refreshed).offset)
        .unwrap();
    assert_eq!(
        bytes, [3; 192],
        "must not publish answer computed before invalidate"
    );
    no_chunk(&mut s).await;
    assert_eq!(calls.load(Ordering::SeqCst), 4);
    wants(&s, vec![want(1)]).await;
    release_chunk(&s, &refreshed).await;
    no_chunk(&mut s).await;
    wants(&s, vec![want(1), want(2)]).await;
    let returned = received_chunk(&mut s).await;
    assert_eq!(returned.t, Some(2));
    file.read_exact_at(&mut bytes, chunk_descriptor(&returned).offset)
        .unwrap();
    assert_eq!(bytes, [5; 192]);
    release_chunk(&s, &returned).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn static_empty_chunks_errors_and_manual_publication() {
    let mut s = Session::new(Registry::new().chunk_source("static_chunks", |_, w| {
        assert_eq!(w.t, None);
        if w.level == 1 {
            return Err(vizi_doorbell::control::Error::internal(
                "source fixture failure",
            ));
        }
        Ok(None)
    }))
    .await;
    s.recv().await;
    s.send(
        "",
        Host::ChunksWanted(pb::ChunksWanted {
            overlay_id: "static_chunks".into(),
            wants: vec![
                pb::ChunkWant { level: 1, t: None },
                pb::ChunkWant { level: 2, t: None },
            ],
        }),
    )
    .await;
    let c = received_chunk(&mut s).await;
    assert_eq!(
        (c.level, c.t, c.labels, c.position_zyx),
        (2, None, None, vec![])
    );
    s.ctx().invalidate_chunks("static_chunks").unwrap();
    assert_eq!(received_chunk(&mut s).await.level, 2);
    wants(&s, vec![want(0)]).await;
    s.barrier().await;
    assert_eq!(s.ctx().wants("chunks").unwrap(), [want(0)]);
    assert!(s.ctx().publish_chunk("chunks", &want(1), None).is_err());
    s.ctx().publish_chunk("chunks", &want(0), None).unwrap();
    assert_eq!(received_chunk(&mut s).await.t, Some(0));
    wants(&s, (0..9).map(want).collect()).await;
    s.barrier().await;
    assert_eq!(
        s.ctx().wants("chunks").unwrap(),
        [want(0)],
        "oversize sets are diagnosed, not applied or faulted"
    );
    let table = vizi_doorbell::plugin::LabelTable {
        colormap: [[1, 2, 3, 4]; 256],
        label_names: [(300, "cell".into())].into(),
        label_visible: [(300, false)].into(),
        style: pb::OverlayStyle {
            opacity: 0.6,
            ..Default::default()
        },
    };
    s.ctx().set_label_table("chunks", &table).unwrap();
    let Some(Plugin::OverlayLabelTable(t)) = s.recv().await.msg else {
        panic!("table expected")
    };
    assert_eq!(t.label_names.get(&300).unwrap(), "cell");
    assert_eq!(t.label_visible.get(&300), Some(&false));
    assert_eq!(t.style.unwrap().opacity, 0.6);
    let lut = t.colormap.unwrap();
    assert_eq!((lut.dtype, lut.shape), (1, vec![256, 4]));
    assert!(
        matches!(lut.source, Some(pb::geom_buffer::Source::Inline(bytes)) if bytes == [1,2,3,4].repeat(256))
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn chunk_supersede_releases_old_arena_replays_retirement_and_uses_new_arena() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _old = chunk_arena(&mut s, 82, 0).await;
    let _new = chunk_arena(&mut s, 83, 82).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 82));
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 82 }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 82));
    wants(&s, vec![want(0)]).await;
    s.barrier().await;
    let ctx = s.ctx();
    ctx.publish_chunk("chunks", &want(0), Some(&chunk_answer(7)))
        .unwrap();
    let c = received_chunk(&mut s).await;
    assert_eq!(chunk_descriptor(&c).segment_id, 83);
    assert!(ctx
        .publish_chunk("chunks", &want(0), Some(&chunk_answer(7)))
        .is_err());
    no_chunk(&mut s).await;
    release_chunk(&s, &c).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn chunk_shutdown_cancels_pending_reads_and_joins_before_reporting() {
    let exited = Arc::new(AtomicUsize::new(0));
    let record = exited.clone();
    let hook = exited.clone();
    let mut s = Session::new(
        Registry::new()
            .chunk_source("chunks", move |ctx, _| {
                let result = ctx.view_state();
                record.fetch_add(1, Ordering::SeqCst);
                result?;
                Ok(None)
            })
            .on_shutdown(move |_, _| {
                assert_eq!(
                    hook.load(Ordering::SeqCst),
                    1,
                    "source joined before shutdown hook"
                );
                Ok(())
            }),
    )
    .await;
    s.recv().await;
    wants(&s, vec![want(0)]).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GetViewState(_))));
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(1000),
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::ShutdownReport(r)) if r.outcome == pb::shutdown_report::Outcome::Completed as i32)
    );
    assert_eq!(exited.load(Ordering::SeqCst), 1);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn withdrawn_want_while_source_runs_discards_answer_and_recomputes_returning_key() {
    let calls = Arc::new(AtomicUsize::new(0));
    let record = calls.clone();
    let (release, blocked) = std::sync::mpsc::channel();
    let blocked = Mutex::new(blocked);
    let (entered, ready) = tokio::sync::oneshot::channel();
    let entered = Mutex::new(Some(entered));
    let app = Registry::new().chunk_source("chunks", move |_, _| {
        let call = record.fetch_add(1, Ordering::SeqCst) + 1;
        if call == 1 {
            entered.lock().unwrap().take().unwrap().send(()).unwrap();
            blocked
                .lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(3))
                .unwrap();
        }
        Ok(Some(chunk_answer(call as u8)))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let file = chunk_arena(&mut s, 90, 0).await;
    wants(&s, vec![want(0)]).await;
    tokio::time::timeout(Duration::from_secs(2), ready)
        .await
        .unwrap()
        .unwrap();
    wants(&s, vec![]).await;
    wants(&s, vec![want(0)]).await;
    s.barrier().await; // both sets applied while computation is still blocked
    assert_eq!(calls.load(Ordering::SeqCst), 1);
    release.send(()).unwrap();
    let chunk = received_chunk(&mut s).await;
    assert_eq!(chunk.t, Some(0));
    let mut bytes = [0; 192];
    file.read_exact_at(&mut bytes, chunk_descriptor(&chunk).offset)
        .unwrap();
    assert_eq!(
        bytes, [2; 192],
        "old computation must never be published for a new membership"
    );
    assert_eq!(calls.load(Ordering::SeqCst), 2);
    release_chunk(&s, &chunk).await;
    no_chunk(&mut s).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn same_uri_reload_closes_before_factory_and_failure_leaves_no_reader() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let record = events.clone();
    let attempts = AtomicUsize::new(0);
    let app = Registry::new().reader("raw-reader", move |_, open| {
        record.lock().unwrap().push(format!("open:{}", open.uri));
        if attempts.fetch_add(1, Ordering::SeqCst) == 2 {
            return Err(control::Error::internal("reload failed"));
        }
        Ok(Arc::new(LifecycleReader {
            name: open.uri.clone(),
            events: record.clone(),
        }))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let first = open_reader(&mut s, "same").await;
    let second = open_reader(&mut s, "same").await;
    assert_ne!(first, second);
    assert_eq!(
        *events.lock().unwrap(),
        ["open:same", "close:same", "open:same"]
    );
    assert_reader_absent(&mut s, &first).await;
    assert_reader_present(&mut s, &second).await;
    s.send(
        "failed-reload",
        Host::OpenReader(pb::OpenReader {
            uri: "same".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.message == "reload failed"));
    assert_reader_absent(&mut s, &second).await;
    assert_eq!(
        *events.lock().unwrap(),
        [
            "open:same",
            "close:same",
            "open:same",
            "close:same",
            "open:same"
        ]
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn refused_open_keeps_previous_readable_and_reader_arena_supersedes() {
    let app = Registry::new().reader("raw-reader", |_, open| {
        if open.uri == "refused" {
            Err(control::Error::new(
                pb::error::Kind::NotFound,
                "cannot open",
            ))
        } else {
            Ok(Arc::new(TestReader))
        }
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let handle = open_reader(&mut s, "good").await;
    let file = grant(&s, 91, 512);
    let mut arena = install(91);
    arena.purpose = "G".into();
    arena.regions[0].producer = 2;
    s.send("", Host::InstallSegment(arena.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.send(
        "refused",
        Host::OpenReader(pb::OpenReader {
            uri: "refused".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Error(_))));
    s.send(
        "still-readable",
        Host::ReadVolume(pb::ReadVolume {
            handle: handle.clone(),
            ..Default::default()
        }),
    )
    .await;
    let Some(Plugin::Segment(d)) = s.recv().await.msg else {
        panic!("previous reader was lost")
    };
    assert_eq!(d.segment_id, 91);
    let mut bytes = [0; 5];
    file.read_exact_at(&mut bytes, d.offset).unwrap();
    assert_eq!(&bytes, b"reade");
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: d.segment_id,
            region: 0,
            offset: d.offset,
            len: d.len,
        }),
    )
    .await;
    let next = open_reader(&mut s, "next").await;
    assert_ne!(handle, next);
    let _next_file = grant(&s, 92, 512);
    arena.segment_id = 92;
    arena.supersedes = 91;
    s.send("", Host::InstallSegment(arena)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == 92));
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 91));
    assert_reader_absent(&mut s, &handle).await;
    s.send(
        "new-read",
        Host::ReadVolume(pb::ReadVolume {
            handle: next,
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Segment(d)) if d.segment_id == 92));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn two_installed_reader_arenas_refuse_reads_until_one_retires() {
    let mut s =
        Session::new(Registry::new().reader("raw-reader", |_, _| Ok(Arc::new(TestReader)))).await;
    s.recv().await;
    let handle = open_reader(&mut s, "good").await;
    let mut files = Vec::new();
    for id in [93, 94] {
        files.push(grant(&s, id, 512));
        let mut arena = install(id);
        arena.purpose = "G".into();
        arena.regions[0].producer = 2;
        s.send("", Host::InstallSegment(arena)).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    }
    for message in [
        Host::ReadVolume(pb::ReadVolume {
            handle: handle.clone(),
            ..Default::default()
        }),
        Host::ReadPlane(pb::ReadPlane {
            handle: handle.clone(),
            ..Default::default()
        }),
    ] {
        s.send("ambiguous", message).await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.message.contains("2 installed segments carry purpose 'G'"))
        );
    }
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 93 }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 93));
    s.send(
        "read",
        Host::ReadVolume(pb::ReadVolume {
            handle,
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::Segment(d)) if d.segment_id == 94));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn subscribed_toolless_session_acknowledges_epochs_and_publishes_progress() {
    use vizi_doorbell::declare::{PanelSpec, PluginBuilder, Subscription, Widget};
    let mut b = PluginBuilder::new("progress", "Progress", "1");
    b.subscription(Subscription::Epochs);
    b.panel(PanelSpec::new(
        "p",
        "Progress",
        Widget::progress("bar", 0.0, false),
    ));
    let plugin = b.build().unwrap();
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from tool-less fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    s.send(
        "",
        Host::TimeMapping(pb::TimeMapping {
            gen: 4,
            revision: 1,
            t: vec![0., 0.5],
        }),
    )
    .await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::TimeMappingAck(_))
    ));
    let mut change = epoch(1, 1);
    let g = change.datasets[0].geometry.as_mut().unwrap();
    g.spacing_z = 3.;
    g.spacing_y = 2.;
    g.spacing_x = 1.;
    g.spacing_unit = "um".into();
    g.spacing_provenance = 1;
    s.send("", Host::EpochChange(change)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::EpochAck(_))));
    let pane = s.ctx().pane(0).unwrap();
    assert_eq!(pane.time_at(1), Some(0.5));
    assert_eq!(pane.physical_spacing(), Some([3., 2., 1.]));
    for (value, indeterminate) in [(0.5, false), (1.5, true)] {
        s.ctx()
            .panel("p")
            .set_progress("bar", value, indeterminate)
            .unwrap();
        let Some(Plugin::PanelPatch(p)) = s.recv().await.msg else {
            panic!("missing progress patch")
        };
        assert_eq!(p.panel_id, "p");
        assert_eq!(p.ops[0].widget_id, "bar");
        assert_eq!(
            p.ops[0].op,
            Some(pb::patch_op::Op::SetProgress(pb::Progress {
                value,
                indeterminate
            }))
        );
    }
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn request_status_keeps_original_waiter_and_unknown_is_not_failure() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let mut pick = s
        .ctx()
        .start_request(Plugin::RequestAsset(pb::RequestAsset {
            slot: "weights".into(),
            force_reprompt: false,
        }))
        .unwrap();
    let request = s.recv().await;
    assert_eq!(request.id, pick.id());
    assert!(pick.wait_timeout(Duration::ZERO).unwrap().is_none());
    for disposition in [
        pb::request_status::Disposition::Pending,
        pb::request_status::Disposition::Unknown,
    ] {
        let ctx = s.ctx();
        let subject = pick.id().to_owned();
        let task = tokio::task::spawn_blocking(move || {
            ctx.request_status(&subject, Duration::from_secs(2))
        });
        let query = s.recv().await;
        assert_ne!(query.id, pick.id());
        assert!(
            matches!(query.msg, Some(Plugin::RequestStatusQuery(ref q)) if q.subject_id == pick.id())
        );
        let status = pb::RequestStatus {
            subject_id: pick.id().into(),
            disposition: disposition as i32,
            awaiting_user: disposition == pb::request_status::Disposition::Pending,
        };
        s.send(&query.id, Host::RequestStatus(status.clone())).await;
        assert_eq!(task.await.unwrap().unwrap(), status);
        assert!(pick.wait_timeout(Duration::ZERO).unwrap().is_none());
    }
    s.send(
        pick.id(),
        Host::AssetResult(pb::AssetResult {
            slot: "weights".into(),
            declined: true,
            ..Default::default()
        }),
    )
    .await;
    let reply = tokio::task::spawn_blocking(move || pick.wait(Duration::from_secs(2)))
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(&reply.message, Host::AssetResult(a) if a.declined));
    drop(reply);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn abandoned_asset_reply_is_dropped_and_request_capacity_is_reclaimed() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let mut pending = Vec::new();
    for _ in 0..32 {
        pending.push(
            s.ctx()
                .start_request(Plugin::RequestAsset(pb::RequestAsset::default()))
                .unwrap(),
        );
        s.recv().await;
    }
    assert!(s
        .ctx()
        .start_request(Plugin::GetViewState(pb::GetViewState {}))
        .is_err());
    let request = pending.pop().unwrap();
    let id = request.id().to_owned();
    assert!(request.wait(Duration::ZERO).is_err());
    s.send(&id, Host::AssetResult(pb::AssetResult::default()))
        .await;
    s.barrier().await;
    let next = s
        .ctx()
        .start_request(Plugin::GetViewState(pb::GetViewState {}))
        .unwrap();
    let request = s.recv().await;
    assert_eq!(request.id, next.id());
    s.send(&request.id, Host::ViewState(pb::ViewState::default()))
        .await;
    let reply = tokio::task::spawn_blocking(move || next.wait(Duration::from_secs(2)))
        .await
        .unwrap()
        .unwrap();
    assert!(matches!(&reply.message, Host::ViewState(_)));
    drop(reply);
    drop(pending);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn roi_windows_preserve_full_domain_and_extensions_preserve_host_outcomes() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    for (start, end) in [
        (None, None),
        (Some(2), None),
        (None, Some(4)),
        (Some(2), Some(4)),
        (Some(0), Some(10)),
    ] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || {
            if start.is_none() && end.is_none() {
                ctx.get_rois()
            } else {
                ctx.get_rois_window(start, end)
            }
        });
        let q = s.recv().await;
        assert!(
            matches!(q.msg, Some(Plugin::GetRois(ref w)) if (w.pose_t_start, w.pose_t_end) == (start, end))
        );
        let record = pb::RoiRecord {
            contrib_id: "track".into(),
            pose_domain_t_start: Some(0),
            pose_domain_t_end: Some(10),
            roi: Some(pb::pois_math::v1::Roi {
                t_start: Some(start.unwrap_or(0)),
                t_end: Some(end.unwrap_or(10)),
                // Unwindowed reads are posed with rows cleared, even though
                // their declared span still matches the full domain.
                pose_translation: if start.is_none() && end.is_none() {
                    vec![]
                } else {
                    vec![1.; 3 * (end.unwrap_or(10) - start.unwrap_or(0) + 1) as usize]
                },
                ..Default::default()
            }),
            ..Default::default()
        };
        s.send(
            &q.id,
            Host::Rois(pb::RoiList {
                rois: vec![record.clone()],
            }),
        )
        .await;
        let records = task.await.unwrap().unwrap();
        assert_eq!(records, vec![record]);
        let r = &records[0];
        let roi = r.roi.as_ref().unwrap();
        let whole_domain = (r.pose_domain_t_start, r.pose_domain_t_end) == (roi.t_start, roi.t_end);
        assert_eq!(
            whole_domain,
            start.unwrap_or(0) == 0 && end.unwrap_or(10) == 10
        );
    }
    let extension = pb::RoiPoseExtension {
        contrib_id: "track".into(),
        t_start: 3,
        span: 2,
        pose_translation: vec![1., 2., 3., 4., 5., 6.],
        ..Default::default()
    };
    for error in [
        None,
        Some(pb::error::Kind::NotFound),
        Some(pb::error::Kind::InvalidArgument),
    ] {
        let ctx = s.ctx();
        let sent = extension.clone();
        let task = tokio::task::spawn_blocking(move || ctx.extend_roi_poses(vec![sent]));
        let q = s.recv().await;
        assert!(
            matches!(q.msg, Some(Plugin::ExtendRoiPoses(ref e)) if e.extensions == [extension.clone()])
        );
        let response = if let Some(kind) = error {
            Host::HostError(pb::Error {
                kind: kind as i32,
                message: "host validation refused batch".into(),
            })
        } else {
            Host::RoiPosesExtended(pb::RoiPosesExtended {
                region_count: 1,
                frame_count: 2,
            })
        };
        s.send(&q.id, response).await;
        let result = task.await.unwrap();
        if let Some(kind) = error {
            assert_eq!(result.unwrap_err().kind, kind);
        } else {
            assert_eq!(
                result.unwrap(),
                pb::RoiPosesExtended {
                    region_count: 1,
                    frame_count: 2
                }
            );
        }
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn supervised_command_can_wait_for_its_asset_request() {
    let mut s = Session::new(Registry::new().command("run", |ctx, _| {
        let asset = ctx.request_asset("weights", false, Duration::from_secs(30))?;
        assert!(asset.declined);
        Ok(done())
    }))
    .await;
    s.recv().await;
    s.send(
        "invoke",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let pick = s.recv().await;
    assert!(matches!(pick.msg, Some(Plugin::RequestAsset(_))));
    tokio::time::sleep(Duration::from_millis(10_200)).await;
    // Control dispatch stays responsive while the command waits on its picker.
    // A picker uses the host call timeout; the command has no local budget.
    s.barrier().await;
    s.send(
        &pick.id,
        Host::AssetResult(pb::AssetResult {
            slot: "weights".into(),
            declined: true,
            ..Default::default()
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "invoke");
    assert!(matches!(reply.msg, Some(Plugin::CommandDone(c)) if c.ok));
    s.stop().await;
}

fn inclusion(id: u64) -> pb::RoiInclusion {
    pb::RoiInclusion {
        segment: data(id).segment,
        origin_zyx: vec![1, 2, 3],
        region_id: "region".into(),
        t: 2,
        open_generation: 42,
        present: true,
        voxel_count: 3,
    }
}
async fn inclusion_release(s: &mut Session, id: u64) {
    assert!(matches!(s.recv().await.msg, Some(Plugin::ReleaseDeposit(r))
        if (r.segment_id, r.offset, r.len) == (id, 64, 5)));
    // Also detects double releases or spurious contract violations.
    s.barrier().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn roi_inclusion_copies_membership_and_distinguishes_empty_states() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = grant(&s, 92, 512);
    file.write_all_at(&[0, 1, 1, 0, 1], 64).unwrap();
    s.send("", Host::InstallSegment(install(92))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    for (present, voxel_count) in [(true, 3), (true, 0), (false, 0)] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.read_roi_inclusion("region", 2));
        let request = s.recv().await;
        assert!(
            matches!(request.msg, Some(Plugin::ReadRoiInclusion(r)) if r.region_id == "region" && r.t == 2)
        );
        let mut result = inclusion(92);
        result.present = present;
        result.voxel_count = voxel_count;
        if voxel_count == 0 {
            result.segment = None;
            result.origin_zyx.clear();
        }
        s.send(&request.id, Host::RoiInclusion(result)).await;
        let result = task.await.unwrap().unwrap();
        assert_eq!(
            (result.region_id.as_str(), result.t, result.open_generation),
            ("region", 2, 42)
        );
        assert_eq!((result.present, result.voxel_count), (present, voxel_count));
        if voxel_count > 0 {
            assert_eq!(
                result.volume.unwrap(),
                control::InclusionVolume {
                    data: vec![0, 1, 1, 0, 1],
                    shape_zyx: [1, 1, 5],
                    origin_zyx: [1, 2, 3],
                }
            );
            inclusion_release(&mut s, 92).await;
        } else {
            assert_eq!(result.volume, None);
            s.barrier().await;
        }
    }
    for kind in [
        pb::error::Kind::NotFound,
        pb::error::Kind::OutOfBounds,
        pb::error::Kind::Unsupported,
    ] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.read_roi_inclusion("region", 2));
        let request = s.recv().await;
        s.send(
            &request.id,
            Host::HostError(pb::Error {
                kind: kind as i32,
                message: "host refused inclusion".into(),
            }),
        )
        .await;
        let error = task.await.unwrap().unwrap_err();
        assert_eq!(error.kind, kind);
        assert_eq!(error.message, "host refused inclusion");
        s.barrier().await;
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn roi_inclusion_refusals_return_their_deposits() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = grant(&s, 93, 512);
    s.send("", Host::InstallSegment(install(93))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    for invalid in [
        "region",
        "frame",
        "presence",
        "dtype",
        "shape",
        "origin",
        "count",
        "membership",
    ] {
        file.write_all_at(&[0, 1, 1, 0, 1], 64).unwrap();
        let mut result = inclusion(93);
        match invalid {
            "region" => result.region_id = "other".into(),
            "frame" => result.t = 1,
            "presence" => result.present = false,
            "dtype" => result.segment.as_mut().unwrap().dtype = pb::DType::DtypeU16 as i32,
            "shape" => result.segment.as_mut().unwrap().shape_zyx = vec![1, 1, 4],
            "origin" => result.origin_zyx.clear(),
            "count" => result.voxel_count = 2,
            "membership" => file.write_all_at(&[2], 64).unwrap(),
            _ => unreachable!(),
        }
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.read_roi_inclusion("region", 2));
        let request = s.recv().await;
        s.send(&request.id, Host::RoiInclusion(result)).await;
        assert!(task.await.unwrap().is_err(), "{invalid}");
        inclusion_release(&mut s, 93).await;
    }
    // A positive count without a deposit is a local refusal, not an attempted
    // copy from a missing segment and not a session fault.
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.read_roi_inclusion("region", 2));
    let request = s.recv().await;
    let mut result = inclusion(93);
    result.segment = None;
    s.send(&request.id, Host::RoiInclusion(result)).await;
    assert!(task.await.unwrap().is_err());
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn roi_inclusion_clip_boxes_are_sent_and_deposits_outside_them_are_released() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = grant(&s, 93, 512);
    file.write_all_at(&[0, 1, 1, 0, 1], 64).unwrap();
    s.send("", Host::InstallSegment(install(93))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    for (lo, hi) in [
        ([1, 2, 3], [2, 3, 8]),
        ([1, 2, 4], [2, 3, 9]),
        ([1, 2, 3], [2, 3, 7]),
    ] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || {
            ctx.read_roi_inclusion_clipped("region", 2, Some((lo, hi)))
        });
        let request = s.recv().await;
        assert!(
            matches!(request.msg, Some(Plugin::ReadRoiInclusion(r)) if r.roi_min_zyx == lo && r.roi_max_zyx == hi)
        );
        s.send(&request.id, Host::RoiInclusion(inclusion(93))).await;
        let result = task.await.unwrap();
        if lo == [1, 2, 3] && hi == [2, 3, 8] {
            assert_eq!(result.unwrap().volume.unwrap().shape_zyx, [1, 1, 5]);
        } else {
            assert_eq!(result.unwrap_err().kind, pb::error::Kind::Protocol);
        }
        inclusion_release(&mut s, 93).await;
    }
    let error = s
        .ctx()
        .read_roi_inclusion_clipped("region", 2, Some(([0; 3], [0, 1, 1])))
        .unwrap_err();
    assert_eq!(error.kind, pb::error::Kind::OutOfBounds);
    s.barrier().await; // Invalid local boxes send no request.
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn abandoned_inclusion_deposits_wait_for_install_and_release_at_zero_uses() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let request = s
        .ctx()
        .start_request(Plugin::ReadRoiInclusion(pb::ReadRoiInclusion {
            region_id: "region".into(),
            t: 2,
            ..Default::default()
        }))
        .unwrap();
    let sent = s.recv().await;
    assert_eq!(sent.id, request.id());
    assert!(request.wait(Duration::ZERO).is_err());
    // Both the segment capability and this late reply may arrive later.
    s.send("", Host::InstallSegment(install(94))).await;
    let mut result = inclusion(94);
    result.segment.as_mut().unwrap().uses = 2;
    s.send(&sent.id, Host::RoiInclusion(result.clone())).await;
    s.barrier().await;
    let _file = grant(&s, 94, 512);
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.barrier().await; // one discarded use, so no release yet
    s.send(&sent.id, Host::RoiInclusion(result)).await;
    inclusion_release(&mut s, 94).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn oversized_roi_window_refusal_keeps_the_session_usable_for_a_smaller_read() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.get_rois_window(Some(0), Some(1000)));
    let request = s.recv().await;
    let message = "GetRois reply is 33554433 bytes, past the 33554432 byte limit";
    s.send(
        &request.id,
        Host::HostError(pb::Error {
            kind: pb::error::Kind::OutOfBounds as i32,
            message: message.into(),
        }),
    )
    .await;
    let error = task.await.unwrap().unwrap_err();
    assert_eq!(error.kind, pb::error::Kind::OutOfBounds);
    assert_eq!(error.message, message);
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.get_rois_window(Some(0), Some(1)));
    let request = s.recv().await;
    assert!(
        matches!(request.msg, Some(Plugin::GetRois(w)) if w.pose_t_start == Some(0) && w.pose_t_end == Some(1))
    );
    // A nonposed region carries neither original-domain bound.
    let record = pb::RoiRecord {
        region_id: "static".into(),
        ..Default::default()
    };
    s.send(
        &request.id,
        Host::Rois(pb::RoiList {
            rois: vec![record.clone()],
        }),
    )
    .await;
    assert_eq!(task.await.unwrap().unwrap(), [record]);
    s.stop().await;
}

struct AuditReader {
    events: Arc<Mutex<Vec<String>>>,
    name: String,
    slow_read: bool,
}
impl plugin::Reader for AuditReader {
    fn info(&self) -> control::Result<pb::ReaderOpened> {
        TestReader.info()
    }
    fn read_volume(&self, request: &pb::ReadVolume) -> control::Result<plugin::Volume> {
        if self.slow_read {
            std::thread::sleep(Duration::from_secs(12));
        }
        TestReader.read_volume(request)
    }
    fn close(&self) -> control::Result<()> {
        self.events
            .lock()
            .unwrap()
            .push(format!("close:{}", self.name));
        Ok(())
    }
}
async fn audit_reader_arena(s: &mut Session) -> std::fs::File {
    let file = grant(s, 95, 512);
    let mut arena = install(95);
    arena.purpose = "G".into();
    arena.regions[0].producer = 2;
    s.send("", Host::InstallSegment(arena)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    file
}
async fn audit_read(s: &mut Session, handle: &str, file: &std::fs::File) {
    s.send(
        "read",
        Host::ReadVolume(pb::ReadVolume {
            handle: handle.into(),
            ..Default::default()
        }),
    )
    .await;
    let reply = tokio::time::timeout(Duration::from_secs(15), s.rx.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(reply.id, "read");
    let Some(Plugin::Segment(d)) = reply.msg else {
        panic!("reader did not answer with data")
    };
    let mut bytes = [0; 5];
    file.read_exact_at(&mut bytes, d.offset).unwrap();
    assert_eq!(&bytes, b"reade");
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: d.segment_id,
            region: 0,
            offset: d.offset,
            len: d.len,
        }),
    )
    .await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn slow_reader_replacement_publishes_its_handle_before_following_reads() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let record = events.clone();
    let app = Registry::new().reader("raw-reader", move |_, open| {
        record.lock().unwrap().push(format!("open:{}", open.uri));
        if open.uri == "B" {
            std::thread::sleep(Duration::from_secs(12));
        }
        Ok(Arc::new(AuditReader {
            events: record.clone(),
            name: open.uri.clone(),
            slow_read: false,
        }))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let first = open_reader(&mut s, "A").await;
    let file = audit_reader_arena(&mut s).await;
    s.send(
        "slow-open",
        Host::OpenReader(pb::OpenReader {
            uri: "B".into(),
            reader_id: String::new(),
            ..Default::default()
        }),
    )
    .await;
    let reply = tokio::time::timeout(Duration::from_secs(15), s.rx.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(reply.id, "slow-open");
    let Some(Plugin::ReaderOpened(second)) = reply.msg else {
        panic!("SDK cut off a factory within the host's 30-second wait")
    };
    assert_ne!(first, second.handle);
    assert_eq!(*events.lock().unwrap(), ["open:A", "open:B", "close:A"]);
    audit_read(&mut s, &second.handle, &file).await;
    assert_reader_present(&mut s, &second.handle).await;
    assert_reader_absent(&mut s, &first).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn slow_volume_read_is_not_cut_off_at_ten_seconds() {
    let app = Registry::new().reader("raw-reader", |_, _| {
        Ok(Arc::new(AuditReader {
            events: Default::default(),
            name: "slow".into(),
            slow_read: true,
        }))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let handle = open_reader(&mut s, "slow").await;
    let file = audit_reader_arena(&mut s).await;
    audit_read(&mut s, &handle, &file).await;
    assert_reader_present(&mut s, &handle).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_same_uri_reader_selection_preserves_the_open_reader() {
    let events = Arc::new(Mutex::new(Vec::new()));
    let record = events.clone();
    let registry = Registry::new().reader("raw-reader", move |_, open| {
        record.lock().unwrap().push(format!("open:{}", open.uri));
        Ok(Arc::new(AuditReader {
            events: record.clone(),
            name: open.uri.clone(),
            slow_read: false,
        }))
    });
    let mut value = manifest().value().clone();
    value["contributions"]["readers"]
        .as_array_mut()
        .unwrap()
        .push(serde_json::json!({"id":"unregistered", "extensions":["other"]}));
    let manifest = Manifest::parse(&value.to_string()).unwrap();
    let mut s = Session::with_runtime(
        |bell, listener| Runtime::new(manifest, Arc::new(registry), bell, listener),
        1,
        0,
        "Hello from audit fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    s.send(
        "open",
        Host::OpenReader(pb::OpenReader {
            uri: "same".into(),
            reader_id: "raw-reader".into(),
            ..Default::default()
        }),
    )
    .await;
    let Some(Plugin::ReaderOpened(reader)) = s.recv().await.msg else {
        panic!("open failed")
    };
    let file = audit_reader_arena(&mut s).await;
    for (reader_id, kind) in [
        ("unknown", pb::error::Kind::NotFound),
        ("", pb::error::Kind::InvalidArgument),
        ("unregistered", pb::error::Kind::Unsupported),
    ] {
        s.send(
            "bad-reload",
            Host::OpenReader(pb::OpenReader {
                uri: "same".into(),
                reader_id: reader_id.into(),
                ..Default::default()
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.kind == kind as i32));
        assert_eq!(*events.lock().unwrap(), ["open:same"]);
        audit_read(&mut s, &reader.handle, &file).await;
    }
    s.stop().await;
}

#[path = "stage_cases/mod.rs"]
mod stage_cases;

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn deferred_violation_reports_flush_without_inbound_traffic() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let site = pb::ViolationSite::UnusableContext as i32;
    let memory = s.ctx().memory().clone();
    memory.report(site, "first", vec![]);
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == site && v.count == 1)
    );
    for _ in 0..3 {
        memory.report(site, "repeat", vec![]);
    }
    // No barrier, host request, doorbell input, or shutdown to drive reporting.
    // Assert eventual counts, not a narrow time window: scheduling can split batches.
    let mut count = 0;
    while count < 3 {
        let Some(Plugin::ContractViolation(v)) = s.recv().await.msg else {
            panic!("expected report")
        };
        assert_eq!(v.r#where, site);
        count += v.count;
    }
    assert_eq!(count, 3);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn shutdown_cancels_application_read_before_hook_join_and_refuses_retry() {
    let worker = Arc::new(Mutex::new(None::<std::thread::JoinHandle<()>>));
    let stopped = Arc::new(AtomicUsize::new(0));
    let join = worker.clone();
    let observed = stopped.clone();
    let app = Registry::new().on_shutdown(move |_, _| {
        join.lock().unwrap().take().unwrap().join().unwrap();
        assert_eq!(observed.load(Ordering::SeqCst), 1);
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let ctx = s.ctx();
    *worker.lock().unwrap() = Some(std::thread::spawn(move || {
        assert!(ctx.read_volume(0, 0, 0, None).is_err());
        assert!(ctx.is_shutting_down());
        assert!(ctx.view_state().is_err());
        stopped.store(1, Ordering::SeqCst);
    }));
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ReadDataVolume(_))
    ));
    // Keep the read unanswered, as if host storage I/O is still running.
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(5000),
            ..Default::default()
        }),
    )
    .await;
    // The cancelled worker's retry must not become another wire request.
    assert!(matches!(s.recv().await.msg, Some(Plugin::ShutdownReport(r))
        if r.outcome == pb::shutdown_report::Outcome::Completed as i32));
    s.stop().await;
}
