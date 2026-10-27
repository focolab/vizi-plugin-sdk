//! The Windows session token on loopback TCP: a stream with no token or a
//! wrong one is refused UNAUTHENTICATED and the session stays open for the
//! host. The doorbell here is a unix socket pair; the control stream is TCP.
#![cfg(unix)]

use std::{os::fd::OwnedFd, sync::Arc, time::Duration};
use tokio_stream::wrappers::ReceiverStream;
use vizi_doorbell::{
    control::{Runtime, SESSION_TOKEN_METADATA_KEY},
    manifest::Manifest,
    plugin::Registry,
    proto::{self as pb, host_msg::Msg as Host, plugin_msg::Msg as Plugin},
    socket_pair, Doorbell,
};

const TOKEN: &str = "0123456789abcdef0123456789abcdef";
const WRONG: &str = "fedcba9876543210fedcba9876543210";

struct Server {
    client: pb::plugin_client::PluginClient<tonic::transport::Channel>,
    runtime: Runtime,
    task: tokio::task::JoinHandle<vizi_doorbell::control::Result<()>>,
    _peer: OwnedFd,
    /// The request halves of accepted streams, kept open so their session
    /// lives on while the test opens the next stream.
    held: Vec<tokio::sync::mpsc::Sender<pb::HostMsg>>,
}
impl Server {
    async fn new() -> Self {
        let (fd, peer) = socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let manifest = Manifest::parse(r#"{"manifest_version":1,"id":"test.token","name":"Token","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#).unwrap();
        let runtime = Runtime::new(manifest, Arc::new(Registry::new()), bell, listener).unwrap();
        runtime.require_session_token(TOKEN);
        let tcp = tokio::net::TcpListener::bind(("127.0.0.1", 0))
            .await
            .unwrap();
        let port = tcp.local_addr().unwrap().port();
        let task = tokio::spawn(runtime.clone().serve_tcp(tcp));
        let channel = tonic::transport::Endpoint::from_shared(format!("http://127.0.0.1:{port}"))
            .unwrap()
            .origin("http://localhost".parse().unwrap())
            .connect()
            .await
            .unwrap();
        Self {
            client: pb::plugin_client::PluginClient::new(channel),
            runtime,
            task,
            _peer: peer,
            held: Vec::new(),
        }
    }
    /// Opens a stream, sends Hello, and returns the first response or status.
    #[allow(clippy::result_large_err)] // a test helper returning tonic's own Status
    async fn attempt(
        &mut self,
        metadata: Option<&str>,
        hello_token: &str,
    ) -> Result<pb::PluginMsg, tonic::Status> {
        let (tx, rx) = tokio::sync::mpsc::channel(4);
        tx.send(pb::HostMsg {
            id: "Hello from token fixture".into(),
            msg: Some(Host::Hello(pb::Hello {
                protocol_major: 1,
                protocol_minor: 0,
                session_token: hello_token.into(),
            })),
        })
        .await
        .unwrap();
        let mut request = tonic::Request::new(ReceiverStream::new(rx));
        if let Some(token) = metadata {
            request
                .metadata_mut()
                .insert(SESSION_TOKEN_METADATA_KEY, token.parse().unwrap());
        }
        let result = tokio::time::timeout(Duration::from_secs(3), async {
            let mut stream = self.client.session(request).await?.into_inner();
            stream
                .message()
                .await?
                .ok_or_else(|| tonic::Status::unknown("stream ended without a message"))
        })
        .await
        .expect("stream deadline");
        // An accepted stream keeps its request half open, so its session
        // does not end before the next attempt; a refused one is dropped.
        if result.is_ok() {
            self.held.push(tx);
        }
        result
    }
    fn assert_waiting(&self) {
        assert!(self.runtime.context().host_identity().is_none());
        assert!(
            !self.task.is_finished(),
            "a refused stream ended the session"
        );
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.runtime.stop();
        self.task.abort();
    }
}

fn is_ready(m: &pb::PluginMsg) -> bool {
    matches!(m.msg, Some(Plugin::Ready(_)))
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn metadata_token_refuses_bad_streams_without_ending_the_session() {
    let mut server = Server::new().await;
    let status = server.attempt(None, "").await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::Unauthenticated);
    server.assert_waiting();
    // A wrong metadata token is refused even when Hello carries the right one.
    let status = server.attempt(Some(WRONG), TOKEN).await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::Unauthenticated);
    server.assert_waiting();
    let ready = server.attempt(Some(TOKEN), "").await.unwrap();
    assert!(is_ready(&ready), "{ready:?}");
    let status = server.attempt(Some(TOKEN), "").await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::AlreadyExists);
}

/// The form every 1.0 host sends: the token in Hello, no metadata key.
#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_hello_only_token_is_accepted() {
    let mut server = Server::new().await;
    let status = server.attempt(None, WRONG).await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::Unauthenticated);
    server.assert_waiting();
    let status = server.attempt(Some(""), TOKEN).await.unwrap_err();
    assert_eq!(status.code(), tonic::Code::Unauthenticated);
    server.assert_waiting();
    let ready = server.attempt(None, TOKEN).await.unwrap();
    assert!(is_ready(&ready), "{ready:?}");
}
