//! Raw clients deliberately wait for response headers before sending Hello.
#![cfg(unix)]

use std::{os::fd::OwnedFd, sync::Arc, time::Duration};
use tokio_stream::wrappers::ReceiverStream;
use vizi_doorbell::{
    control::Runtime,
    manifest::Manifest,
    plugin::Registry,
    proto::{self as pb, host_msg::Msg as Host, plugin_msg::Msg as Plugin},
    socket_pair, Doorbell,
};

struct Server {
    client: pb::plugin_client::PluginClient<tonic::transport::Channel>,
    runtime: Runtime,
    task: tokio::task::JoinHandle<vizi_doorbell::control::Result<()>>,
    _peer: OwnedFd,
    _directory: tempfile::TempDir,
}
impl Server {
    async fn new() -> Self {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("control.sock");
        let (fd, peer) = socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
        let manifest = Manifest::parse(r#"{"manifest_version":1,"id":"test.headers","name":"Headers","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]}}"#).unwrap();
        let runtime = Runtime::new(manifest, Arc::new(Registry::new()), bell, listener).unwrap();
        let serving = runtime.clone();
        let endpoint_path = path.clone();
        let task = tokio::spawn(async move { serving.serve_unix(endpoint_path).await });
        let endpoint = tonic::transport::Endpoint::from_static("http://localhost");
        let channel = tokio::time::timeout(Duration::from_secs(3), async {
            loop {
                let path = path.clone();
                let connection = endpoint
                    .clone()
                    .connect_with_connector(tower::service_fn(move |_| {
                        let path = path.clone();
                        async move {
                            tokio::net::UnixStream::connect(path)
                                .await
                                .map(hyper_util::rt::TokioIo::new)
                        }
                    }))
                    .await;
                if let Ok(channel) = connection {
                    break channel;
                }
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .expect("bind/connect deadline");
        Self {
            client: pb::plugin_client::PluginClient::new(channel),
            runtime,
            task,
            _peer: peer,
            _directory: directory,
        }
    }
    async fn open(
        &mut self,
    ) -> (
        tokio::sync::mpsc::Sender<pb::HostMsg>,
        tonic::Streaming<pb::PluginMsg>,
    ) {
        let (tx, rx) = tokio::sync::mpsc::channel(4);
        // No request message has been sent. This await resolves on response
        // HEADERS, not on the first response message or a stream half-close.
        let response = tokio::time::timeout(
            Duration::from_secs(3),
            self.client.session(ReceiverStream::new(rx)),
        )
        .await
        .expect("response headers must precede Hello")
        .unwrap();
        (tx, response.into_inner())
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.runtime.stop();
        self.task.abort();
    }
}

fn hello(major: u32) -> pb::HostMsg {
    pb::HostMsg {
        id: "Hello from headers fixture".into(),
        msg: Some(Host::Hello(pb::Hello {
            protocol_major: major,
            protocol_minor: 0,
            session_token: String::new(),
        })),
    }
}

async fn next(
    stream: &mut tonic::Streaming<pb::PluginMsg>,
) -> Result<Option<pb::PluginMsg>, Box<tonic::Status>> {
    tokio::time::timeout(Duration::from_secs(3), stream.message())
        .await
        .expect("stream response deadline")
        .map_err(Box::new)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn headers_precede_hello_and_ready_admits_requests_immediately() {
    let mut server = Server::new().await;
    let (tx, mut rx) = server.open().await;
    assert!(server.runtime.context().host_identity().is_none());
    assert!(
        tokio::time::timeout(Duration::from_millis(50), rx.message())
            .await
            .is_err(),
        "Ready must wait for a validated Hello"
    );
    tx.send(hello(1)).await.unwrap();
    assert!(matches!(
        next(&mut rx).await.unwrap().unwrap().msg,
        Some(Plugin::Ready(_))
    ));
    // No extra handshake event or notification between Ready and this request.
    tx.send(pb::HostMsg {
        id: "immediate".into(),
        msg: Some(Host::GrantStateQuery(pb::GrantStateQuery {
            segment_id: 99,
        })),
    })
    .await
    .unwrap();
    let reply = next(&mut rx).await.unwrap().unwrap();
    assert_eq!(reply.id, "immediate");
    assert!(matches!(reply.msg, Some(Plugin::GrantState(_))));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn headers_do_not_accept_a_second_session_or_an_invalid_hello() {
    let mut server = Server::new().await;
    let (tx, mut rx) = server.open().await;
    let (_second_tx, second_rx) = tokio::sync::mpsc::channel::<pb::HostMsg>(1);
    let status = tokio::time::timeout(
        Duration::from_secs(3),
        server.client.session(ReceiverStream::new(second_rx)),
    )
    .await
    .unwrap()
    .unwrap_err();
    assert_eq!(status.code(), tonic::Code::AlreadyExists);
    tx.send(hello(2)).await.unwrap();
    assert_eq!(
        next(&mut rx).await.unwrap_err().code(),
        tonic::Code::FailedPrecondition
    );
    assert!(server.runtime.context().host_identity().is_none());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn eof_before_hello_is_a_stream_error() {
    let mut server = Server::new().await;
    let (tx, mut rx) = server.open().await;
    drop(tx);
    assert_eq!(
        next(&mut rx).await.unwrap_err().code(),
        tonic::Code::InvalidArgument
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn missing_hello_still_has_a_ten_second_deadline() {
    let mut server = Server::new().await;
    let (_tx, mut rx) = server.open().await;
    // Keep the request stream open but silent, as a headers-waiting host would.
    let status = tokio::time::timeout(Duration::from_secs(13), rx.message())
        .await
        .expect("Hello timeout must terminate the stream")
        .unwrap_err();
    assert_eq!(status.code(), tonic::Code::InvalidArgument);
    assert!(status.message().contains("10 seconds"));
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn stopping_before_hello_ends_the_open_stream() {
    let mut server = Server::new().await;
    let (_tx, mut rx) = server.open().await;
    server.runtime.stop();
    assert!(next(&mut rx).await.unwrap().is_none());
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn dropping_call_before_hello_ends_the_session() {
    let mut server = Server::new().await;
    let (tx, rx) = server.open().await;
    // Close both halves: dropping only the response leaves the client's
    // request body alive, which is not a cancellation of the entire RPC.
    drop(tx);
    drop(rx);
    tokio::time::timeout(Duration::from_secs(3), &mut server.task)
        .await
        .expect("cancelled call must not wait for the Hello timeout")
        .unwrap()
        .unwrap();
}
