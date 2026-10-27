#![cfg(windows)]

mod support;
use support::{wait_until, WaitingIo};

use std::{
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::mpsc::{self, Receiver},
    thread,
    time::{Duration, Instant},
};
use vizi_doorbell::*;

const TOKEN: &str = "0123456789abcdefABCDEF0123456789";
const WAIT: Duration = Duration::from_secs(5);
struct Peer {
    bell: Doorbell,
    host: Option<WaitingIo<TcpStream>>,
    events: Receiver<Outcome>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Peer {
    fn new() -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let (bell, doorbell) = Doorbell::connect(
            &listener.local_addr().unwrap().to_string(),
            TOKEN,
            Config::default(),
            WAIT,
        )
        .unwrap();
        let end = Instant::now() + WAIT;
        let (host, _) = wait_until(end, || listener.accept()).unwrap();
        host.set_nonblocking(true).unwrap();
        let mut host = WaitingIo::new(host, end);
        let mut token = [0; 32];
        host.read_exact(&mut token).unwrap();
        assert_eq!(&token, TOKEN.as_bytes());
        let (tx, events) = mpsc::channel();
        let thread = thread::spawn(move || {
            doorbell
                .run(|o| {
                    tx.send(std::mem::replace(
                        o,
                        Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop)),
                    ))
                    .unwrap();
                })
                .unwrap()
        });
        Self {
            bell,
            host: Some(host),
            events,
            thread: Some(thread),
        }
    }
    fn ready(&self) {
        self.bell
            .handshake_completed_with_token(1, 0, TOKEN)
            .unwrap();
    }
    fn send(&mut self, bytes: &[u8]) {
        self.host.as_mut().unwrap().write_all(bytes).unwrap();
    }
    fn next(&self) -> Outcome {
        self.events.recv_timeout(WAIT).unwrap()
    }
    fn frame(&self) -> FrameOutcome {
        match self.next() {
            Outcome::Frame(f) => f,
            o => panic!("expected frame: {o:?}"),
        }
    }
}
impl Drop for Peer {
    fn drop(&mut self) {
        self.bell.stop();
        if let Some(t) = self.thread.take() {
            t.join().unwrap();
        }
    }
}
fn hint() -> Notification {
    let mut mask = RegionMask::default();
    mask.set(9).unwrap();
    Notification {
        segment_id: 0x1_0000_0001,
        mask,
        pane: None,
    }
}
fn grant() -> MemoryGrant {
    MemoryGrant {
        segment_id: 0x1_0000_0001,
        size: 4096,
        permissions: Permissions::new(15).unwrap(),
    }
}
fn checksum(b: &mut RecordBytes) {
    let mut crc = 0xffff_ffffu32;
    for byte in &b[..108] {
        crc ^= *byte as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ if crc & 1 != 0 { 0xedb8_8320 } else { 0 };
        }
    }
    b[108..].copy_from_slice(&(!crc).to_le_bytes());
}
fn input(phase: Phase, seq: u32) -> InputEvent {
    InputEvent {
        gen: 1,
        t: 0,
        interaction: 1,
        seq,
        epoch: 1,
        target: None,
        tool: 1,
        channels: 1,
        phase,
        view_id: 0,
        level: 0,
        mods: 0,
        click_count: 0,
        miss: false,
        roll_ref_y: false,
        action: 0,
        position: Position::None,
    }
}

#[test]
fn token_is_first_bytes_and_hello_echo_gates_outbound_records() {
    let mut p = Peer::new();
    assert_eq!(
        p.bell.notify(&hint()).unwrap(),
        SendOutcome::HandshakePending
    );
    assert!(p.bell.handshake_completed(1, 0).is_err());
    p.ready();
    assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Sent);
    let mut record = [0; 112];
    p.host.as_mut().unwrap().read_exact(&mut record).unwrap();
    assert_eq!(record, hint().encode(1).unwrap());
    let info = p.bell.socket_info();
    assert!(info.nonblocking);
    assert!(info.close_on_exec);
    assert_eq!(info.socket_type, 1);
    let p = Peer::new();
    assert!(p
        .bell
        .handshake_completed_with_token(1, 0, "ffffffffffffffffffffffffffffffff")
        .is_err());
    assert!(matches!(
        p.next(),
        Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop))
    ));
    assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Closed);
}

#[test]
fn stream_chunking_and_coalescing_do_not_change_framing() {
    let mut p = Peer::new();
    p.ready();
    let first = hint().encode(1).unwrap();
    for byte in first {
        p.send(&[byte]);
    }
    assert!(matches!(
        p.frame().disposition,
        Disposition::Decoded(DecodedRecord::Notification(_))
    ));
    let second = hint().encode(2).unwrap();
    let third = hint().encode(3).unwrap();
    p.send(&[second.as_slice(), third.as_slice()].concat());
    assert_eq!(p.frame().id, Some(2));
    assert_eq!(p.frame().id, Some(3));
}

#[test]
fn named_grants_have_no_descriptor_and_malformed_names_do_not_fault() {
    let mut p = Peer::new();
    p.ready();
    for name in [
        "Local\\example",
        "not-a-fixed-pattern",
        "é",
        &"x".repeat(80),
    ] {
        p.send(&grant().encode_named(1, name).unwrap());
        let Disposition::Decoded(DecodedRecord::NamedMemory { grant: g, name: n }) =
            p.frame().disposition
        else {
            panic!()
        };
        assert_eq!(g, grant());
        assert_eq!(n, name);
    }
    for (offset, value, violation) in [
        (26, 0, GrantViolation::EmptyName),
        (26, 255, GrantViolation::NameTooLong),
        (30, 1, GrantViolation::NameTerminator),
        (28, 0, GrantViolation::EmbeddedNul),
        (27, 255, GrantViolation::InvalidUtf8),
        (107, 1, GrantViolation::NamePadding),
    ] {
        let mut b = grant().encode_named(2, "abc").unwrap();
        b[offset] = value;
        checksum(&mut b);
        p.send(&b);
        let Disposition::Rejected(Rejection::Grant { violations, .. }) = p.frame().disposition
        else {
            panic!()
        };
        assert!(violations.contains(&violation));
        assert!(p.bell.terminal_report().is_none());
    }
    let mut b = grant().encode_named(3, "abc").unwrap();
    b[25] = 1;
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::FramingRejected(FailureClass::BadPadding)
    ));
    assert!(matches!(p.next(), Outcome::Terminal(Terminal::Fault(_))));
}

#[test]
fn eof_mid_record_is_teardown_with_partial_capture() {
    let mut p = Peer::new();
    p.ready();
    p.send(&hint().encode(1).unwrap());
    p.send(b"partial");
    p.host.take();
    assert_eq!(p.frame().ordinal, 1);
    let Outcome::Terminal(Terminal::Teardown(TeardownReason::PeerGone { capture, os_error })) =
        p.next()
    else {
        panic!()
    };
    assert_eq!(capture, b"partial");
    assert_eq!(os_error, None);
    assert_eq!(p.bell.statistics().frames_examined, 1);
}

#[test]
fn bad_crc_is_immediate_and_does_not_consume_a_second_frame() {
    let mut p = Peer::new();
    p.ready();
    let mut b = hint().encode(1).unwrap();
    b[1] = 99;
    p.send(&[b.as_slice(), hint().encode(2).unwrap().as_slice()].concat());
    let f = p.frame();
    assert!(matches!(
        f.disposition,
        Disposition::FramingRejected(FailureClass::BadCrc)
    ));
    assert_eq!(f.id, None);
    let Outcome::Terminal(Terminal::Fault(f)) = p.next() else {
        panic!()
    };
    assert_eq!(f.transport_class, TransportClass::Stream);
    assert_eq!(f.capture.as_deref(), Some(b.as_slice()));
    assert_eq!(p.bell.statistics().frames_examined, 1);
    assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Closed);
}

#[test]
fn unknown_opcode_skips_exactly_one_stream_window() {
    let mut p = Peer::new();
    p.ready();
    let mut b = hint().encode(1).unwrap();
    b[0] = 0xa1;
    b[107] = 1;
    checksum(&mut b);
    p.send(&b);
    p.send(&hint().encode(2).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::UnknownOpcode(0xa1)
    ));
    assert_eq!(p.frame().id, Some(2));
}

#[test]
fn windows_input_uses_the_same_lifecycle_and_cancellation_order() {
    let mut p = Peer::new();
    p.ready();
    p.bell.tool_activated(1).unwrap();
    p.bell.epoch_acknowledged(0, 1).unwrap();
    p.send(&input(Phase::Press, 0).encode(1).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Decoded(DecodedRecord::Input { .. })
    ));
    p.send(&input(Phase::Drag, 3).encode(2).unwrap());
    assert!(p
        .frame()
        .diagnostics
        .iter()
        .any(|d| d.kind == DiagnosticKind::SequenceGap && d.value == 2));
    p.bell.epoch_acknowledged(0, 2).unwrap();
    assert!(matches!(
        p.next(),
        Outcome::Cancellation(Cancellation {
            reason: CancellationReason::EpochChanged,
            ..
        })
    ));
    p.send(&input(Phase::Release, 4).encode(3).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::StaleEpoch)
    ));
}

#[test]
fn windows_wake_endpoint_stops_an_idle_listener() {
    let p = Peer::new();
    p.bell.stop();
    assert!(matches!(
        p.next(),
        Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop))
    ));
    assert_eq!(p.bell.tool_activated(1), Err(LifecycleError::Closed));
}

#[test]
fn windows_stop_and_terminal_sink_do_not_wait_for_a_callback() {
    let host_listener = TcpListener::bind("127.0.0.1:0").unwrap();
    host_listener.set_nonblocking(true).unwrap();
    let (bell, listener) = Doorbell::connect(
        &host_listener.local_addr().unwrap().to_string(),
        TOKEN,
        Config::default(),
        WAIT,
    )
    .unwrap();
    let end = Instant::now() + WAIT;
    let (host, _) = wait_until(end, || host_listener.accept()).unwrap();
    host.set_nonblocking(true).unwrap();
    let mut host = WaitingIo::new(host, end);
    let mut token = [0; 32];
    host.read_exact(&mut token).unwrap();
    bell.handshake_completed_with_token(1, 0, TOKEN).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let worker = thread::spawn(move || {
        listener
            .run(|o| {
                if matches!(o, Outcome::Frame(_)) {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(WAIT).unwrap();
                }
            })
            .unwrap()
    });
    host.write_all(&hint().encode(1).unwrap()).unwrap();
    entered_rx.recv_timeout(WAIT).unwrap();
    bell.stop();
    assert!(bell.wait_terminal(Duration::ZERO).is_some());
    release_tx.send(()).unwrap();
    worker.join().unwrap();
}

#[test]
fn concurrent_windows_senders_keep_record_ids_in_wire_order() {
    let mut p = Peer::new();
    p.ready();
    let workers: Vec<_> = (0..4)
        .map(|_| {
            let b = p.bell.clone();
            thread::spawn(move || {
                for _ in 0..4 {
                    assert_eq!(b.notify(&hint()).unwrap(), SendOutcome::Sent);
                }
            })
        })
        .collect();
    for w in workers {
        w.join().unwrap();
    }
    for id in 1u32..=16 {
        let mut bytes = [0u8; 112];
        p.host.as_mut().unwrap().read_exact(&mut bytes).unwrap();
        assert_eq!(bytes, hint().encode(id).unwrap());
    }
}

#[test]
fn windows_backpressure_is_disposable_or_a_detected_short_write() {
    let p = Peer::new();
    p.ready();
    for _ in 0..100_000 {
        match p.bell.notify(&hint()).unwrap() {
            SendOutcome::Sent => {}
            SendOutcome::Dropped => {
                assert!(p.bell.terminal_report().is_none());
                assert!(p.bell.statistics().notifications_refused > 0);
                return;
            }
            SendOutcome::Closed => {
                // The protocol retains this assertion because Winsock permits
                // short writes even though the specification's probes saw none.
                assert!(
                    matches!(p.bell.terminal_report(),Some(Terminal::Fault(f)) if f.failure_class==FailureClass::ShortWrite)
                );
                assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Closed);
                return;
            }
            other => panic!("unexpected send outcome {other:?}"),
        }
    }
    panic!("bounded probe did not saturate the socket");
}

#[test]
fn windows_child_adopts_launch_environment() {
    if std::env::var_os("VIZI_RUST_WINDOWS_TEST_CHILD").is_none() {
        return;
    }
    let (bell, listener) = Doorbell::from_env(Config::default()).unwrap();
    bell.handshake_completed_with_token(1, 0, TOKEN).unwrap();
    listener
        .run(|o| {
            if let Outcome::Frame(f) = o {
                let Disposition::Decoded(DecodedRecord::NamedMemory { grant: g, name }) =
                    &f.disposition
                else {
                    panic!("expected named grant")
                };
                assert_eq!(g, &grant());
                assert_eq!(name, "Local\\test-section");
                assert_eq!(bell.notify(&hint()).unwrap(), SendOutcome::Sent);
                bell.stop();
            }
        })
        .unwrap();
}

#[test]
fn windows_launch_and_named_capability_cross_a_process_boundary() {
    use std::process::{Command, Stdio};
    let listener = TcpListener::bind("127.0.0.1:0").unwrap();
    listener.set_nonblocking(true).unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "windows_child_adopts_launch_environment",
            "--nocapture",
        ])
        .env("VIZI_RUST_WINDOWS_TEST_CHILD", "1")
        .env(
            "VIZI_PLUGIN_DOORBELL_ADDR",
            listener.local_addr().unwrap().to_string(),
        )
        .env("VIZI_PLUGIN_TOKEN", TOKEN)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let end = std::time::Instant::now() + WAIT;
    let host = loop {
        match listener.accept() {
            Ok((host, _)) => break host,
            Err(e)
                if e.kind() == std::io::ErrorKind::WouldBlock
                    && std::time::Instant::now() < end =>
            {
                thread::sleep(Duration::from_millis(1))
            }
            result => {
                child.kill().ok();
                child.wait().ok();
                panic!("child did not connect: {result:?}");
            }
        }
    };
    host.set_nonblocking(true).unwrap();
    let mut host = WaitingIo::new(host, end);
    let mut token = [0; 32];
    host.read_exact(&mut token).unwrap();
    assert_eq!(&token, TOKEN.as_bytes());
    host.write_all(&grant().encode_named(1, "Local\\test-section").unwrap())
        .unwrap();
    let mut b = [0; 112];
    host.read_exact(&mut b).unwrap();
    assert_eq!(b, hint().encode(1).unwrap());
    while child.try_wait().unwrap().is_none() {
        if std::time::Instant::now() > end {
            child.kill().ok();
            child.wait().ok();
            panic!("child did not exit");
        }
        thread::sleep(Duration::from_millis(1));
    }
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
