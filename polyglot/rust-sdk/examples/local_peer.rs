//! Local socket peer demo. This substitutes in-process arrangements for the
//! unavailable gRPC host; it is not a production handshake implementation.
#[cfg(unix)]
use std::fs::File;
use std::{
    io::{self, Write},
    sync::mpsc,
    thread,
    time::Duration,
};
use vizi_doorbell::*;

fn demo() -> Result<(), Box<dyn std::error::Error>> {
    #[cfg(unix)]
    let (host, plugin) = socket_pair()?;
    #[cfg(unix)]
    let (bell, listener) = Doorbell::new(plugin, Config::default())?;
    #[cfg(windows)]
    let (mut host, (bell, listener)) = {
        use std::{io::Read, net::TcpListener};
        let host_listener = TcpListener::bind("127.0.0.1:0")?;
        let pair = Doorbell::connect(
            &host_listener.local_addr()?.to_string(),
            "0123456789abcdef0123456789abcdef",
            Config::default(),
            Duration::from_secs(5),
        )?;
        let (mut host, _) = host_listener.accept()?;
        host.set_read_timeout(Some(Duration::from_secs(5)))?;
        let mut token = [0; 32];
        host.read_exact(&mut token)?;
        assert_eq!(&token, b"0123456789abcdef0123456789abcdef");
        (host, pair)
    };
    let (processed_tx, processed_rx) = mpsc::channel();
    let listener_thread = thread::spawn(move || {
        listener.run(|outcome| match outcome {
            Outcome::Frame(frame) => {
                println!("frame {}: {:?}", frame.ordinal, frame.disposition);
                processed_tx.send(frame.id).expect("demo receiver dropped");
            }
            Outcome::Cancellation(c) => {
                println!("canceled {}: {:?}", c.interaction.interaction, c.reason)
            }
            Outcome::Terminal(t) => println!("session ended: {t:?}"),
        })
    });

    // Stand-ins for successful, exact-version and arrangement exchanges. In a
    // real plugin these calls precede the corresponding gRPC acknowledgements.
    #[cfg(unix)]
    bell.handshake_completed(1, 0)?;
    #[cfg(windows)]
    bell.handshake_completed_with_token(1, 0, "0123456789abcdef0123456789abcdef")?;
    bell.tool_activated(1)?;
    bell.epoch_acknowledged(0, 1)?;
    let mut event = InputEvent {
        gen: 1,
        t: 0,
        interaction: 1,
        seq: 0,
        epoch: 1,
        target: None,
        tool: 1,
        channels: 1,
        phase: Phase::Press,
        view_id: 0,
        level: 0,
        mods: 0,
        click_count: 1,
        miss: false,
        roll_ref_y: false,
        action: 0,
        position: Position::Voxel([2.5, 3.5, 4.5]),
    };
    // Two tiny sends into an otherwise empty mock-host socket. Production
    // host sending also needs its own nonblocking/retry policy; this is a demo.
    #[cfg(unix)]
    let mut host = File::from(host);
    for id in 1..=2 {
        let frame = event.encode(id)?;
        let sent = host.write(&frame)?; // one syscall, never write_all
        if sent != RECORD_LEN {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "mock peer short send").into());
        }
        event.phase = Phase::Release;
        event.seq = 1;
        event.click_count = 0;
    }
    for id in 1..=2 {
        assert_eq!(processed_rx.recv_timeout(Duration::from_secs(5))?, Some(id));
    }
    // Stand-in for control-plane teardown after both demo events were handled.
    // A datagram socket close does not provide a durable EOF notification.
    bell.stop();
    drop(host);
    listener_thread.join().expect("listener thread panicked")?;
    assert!(matches!(
        bell.terminal_report(),
        Some(Terminal::Teardown(_))
    ));
    Ok(())
}
fn main() -> Result<(), Box<dyn std::error::Error>> {
    demo()
}

#[test]
fn local_peer_demo() {
    demo().unwrap();
}
