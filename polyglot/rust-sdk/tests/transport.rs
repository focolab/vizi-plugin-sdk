#![cfg(unix)]

use std::{
    io::{Read, Write},
    os::{
        fd::{AsRawFd, OwnedFd},
        unix::net::UnixStream,
    },
    sync::mpsc::{self, Receiver},
    thread,
    time::Duration,
};
use vizi_doorbell::*;

const WAIT: Duration = Duration::from_secs(3);
struct Peer {
    bell: Doorbell,
    host: OwnedFd,
    events: Receiver<Outcome>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Peer {
    fn new() -> Self {
        let (host, plugin) = socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
        let (tx, events) = mpsc::channel();
        let thread = thread::spawn(move || {
            listener
                .run(|o| {
                    // Moving the complete outcome also claims any OwnedFd it contains.
                    let o = std::mem::replace(
                        o,
                        Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop)),
                    );
                    tx.send(o).unwrap();
                })
                .unwrap()
        });
        Self {
            bell,
            host,
            events,
            thread: Some(thread),
        }
    }
    fn send(&self, b: &[u8]) {
        send(&self.host, b, &[]);
    }
    fn next(&self) -> Outcome {
        self.events.recv_timeout(WAIT).unwrap()
    }
    fn frame(&self) -> FrameOutcome {
        match self.next() {
            Outcome::Frame(f) => f,
            o => panic!("expected frame, got {o:?}"),
        }
    }
    fn setup_input(&self) {
        self.bell.tool_activated(1).unwrap();
        self.bell.epoch_acknowledged(0, 1).unwrap();
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

// Independent mock host. All traffic, including malformed lengths and rights,
// goes through the actual kernel socket; no Python/Go SDK participates.
fn send(fd: &OwnedFd, b: &[u8], rights: &[i32]) {
    let mut iov = libc::iovec {
        iov_base: b.as_ptr() as *mut _,
        iov_len: b.len(),
    };
    let mut control = [0usize; 128];
    // SAFETY: all pointers below refer to appropriately aligned live buffers.
    unsafe {
        let mut msg: libc::msghdr = std::mem::zeroed();
        msg.msg_iov = &mut iov;
        msg.msg_iovlen = 1;
        if !rights.is_empty() {
            msg.msg_control = control.as_mut_ptr().cast();
            msg.msg_controllen = libc::CMSG_SPACE(std::mem::size_of_val(rights) as u32) as _;
            let h = libc::CMSG_FIRSTHDR(&msg);
            (*h).cmsg_level = libc::SOL_SOCKET;
            (*h).cmsg_type = libc::SCM_RIGHTS;
            (*h).cmsg_len = libc::CMSG_LEN(std::mem::size_of_val(rights) as u32) as _;
            std::ptr::copy_nonoverlapping(
                rights.as_ptr().cast::<u8>(),
                libc::CMSG_DATA(h),
                std::mem::size_of_val(rights),
            );
        }
        #[cfg(target_os = "linux")]
        let flags = libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL;
        #[cfg(target_os = "macos")]
        let flags = libc::MSG_DONTWAIT;
        assert_eq!(
            libc::sendmsg(fd.as_raw_fd(), &msg, flags),
            b.len() as isize,
            "{}",
            std::io::Error::last_os_error()
        );
    }
}
fn checksum(b: &mut RecordBytes) {
    // A deliberately separate bit-at-a-time implementation for corrupted peer
    // fixtures, so the receiver and test peer do not share their CRC primitive.
    let mut crc = 0xffff_ffffu32;
    for byte in &b[..108] {
        crc ^= *byte as u32;
        for _ in 0..8 {
            crc = (crc >> 1) ^ if crc & 1 != 0 { 0xedb8_8320 } else { 0 };
        }
    }
    b[108..].copy_from_slice(&(!crc).to_le_bytes());
}
fn hint() -> Notification {
    let mut mask = RegionMask::default();
    mask.set(0).unwrap();
    mask.set(511).unwrap();
    Notification {
        segment_id: 0x0000_0001_0000_0001,
        mask,
        pane: Some(PaneEpoch {
            view_id: 0,
            epoch: 1,
        }),
    }
}
fn grant() -> MemoryGrant {
    MemoryGrant {
        segment_id: 0x0000_0001_0000_0001,
        size: 4096,
        permissions: Permissions::new(15).unwrap(),
    }
}
fn input(phase: Phase, n: u32, seq: u32) -> InputEvent {
    InputEvent {
        gen: 1,
        t: 0,
        interaction: n,
        seq,
        epoch: 1,
        target: None,
        tool: 1,
        channels: 3,
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
fn accepted(f: FrameOutcome) -> (InputEvent, Option<u32>) {
    match f.disposition {
        Disposition::Decoded(DecodedRecord::Input {
            event,
            implicitly_closed,
        }) => (event, implicitly_closed),
        o => panic!("expected input, got {o:?}"),
    }
}

#[test]
fn wire_layout_crc_and_mask() {
    assert_eq!(crc32(b"123456789"), 0xcbf4_3926);
    let b = hint().encode(0x10203040).unwrap();
    assert_eq!(&b[..8], &[0x44, 2, 0, 0, 0x40, 0x30, 0x20, 0x10]);
    assert_eq!(&b[8..16], &[1, 0, 0, 0, 1, 0, 0, 0]);
    assert_eq!(b[16], 1);
    assert_eq!(b[79], 128);
    assert_eq!(&b[80..84], &1u32.to_le_bytes());
    assert_eq!(b[84], 0);
    let mut independent = b;
    checksum(&mut independent);
    assert_eq!(independent, b);
    assert_eq!(hint().mask.indices().collect::<Vec<_>>(), vec![0, 511]);
    assert!(RegionMask::default().set(512).is_err());
    assert!(Permissions::new(2).is_err());
    assert!(Permissions::new(17).is_err());
}

#[test]
fn setup_rejects_stream_and_sets_flags() {
    let (wrong, _) = UnixStream::pair().unwrap();
    assert!(Doorbell::new(wrong.into(), Config::default()).is_err());
    let (host, plugin) = socket_pair().unwrap();
    let raw = plugin.as_raw_fd();
    // Simulate an inheritable launcher fd without nonblocking mode.
    unsafe {
        assert_eq!(libc::fcntl(raw, libc::F_SETFD, 0), 0);
    }
    let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
    unsafe {
        assert_ne!(libc::fcntl(raw, libc::F_GETFD) & libc::FD_CLOEXEC, 0);
        assert_ne!(libc::fcntl(raw, libc::F_GETFL) & libc::O_NONBLOCK, 0);
    }
    assert!(bell.socket_info().nonblocking);
    drop(listener);
    drop(host);
    assert_eq!(bell.tool_activated(1), Err(LifecycleError::Closed));
    assert!(matches!(
        bell.terminal_report(),
        Some(Terminal::Teardown(TeardownReason::ListenerDropped))
    ));
}

#[test]
fn unknown_opcode_feeds_ids_and_preserves_message_boundaries() {
    let p = Peer::new();
    let mut unknown = hint().encode(3).unwrap();
    unknown[0] = 0x91;
    unknown[107] = 99;
    checksum(&mut unknown);
    p.send(&unknown);
    p.send(&hint().encode(2).unwrap());
    p.send(&hint().encode(4).unwrap());
    let f = p.frame();
    assert!(matches!(f.disposition, Disposition::UnknownOpcode(0x91)));
    assert_eq!(f.diagnostics[0].kind, DiagnosticKind::IdGap);
    assert_eq!(f.diagnostics[0].value, 2);
    let f = p.frame();
    assert_eq!(f.diagnostics[0].kind, DiagnosticKind::IdRegression);
    assert!(p.frame().diagnostics.is_empty());
    assert_eq!(p.bell.statistics().missing_record_ids, 2);
}

#[test]
fn framing_faults_are_fail_stop_and_keep_diagnostics() {
    for (offset, value, class) in [
        (1, 3, FailureClass::BadVer),
        (2, 1, FailureClass::BadFlags),
        (3, 1, FailureClass::BadReserved),
        (0, 255, FailureClass::ReservedOp),
        (85, 1, FailureClass::BadPadding),
    ] {
        let p = Peer::new();
        let mut b = hint().encode(1).unwrap();
        b[offset] = value;
        checksum(&mut b);
        p.send(&b);
        assert!(matches!(p.frame().disposition,Disposition::FramingRejected(c) if c==class));
        let Outcome::Terminal(Terminal::Fault(f)) = p.next() else {
            panic!()
        };
        assert_eq!(f.failure_class, class);
        assert_eq!(f.capture.as_deref(), Some(b.as_slice()));
        assert_eq!(f.ordinal, Some(1));
        assert_eq!(f.observed_len, Some(112));
        assert_eq!(f.os_error, None);
        assert_eq!(f.expected_crc, f.observed_crc);
        assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Closed);
        p.bell.stop();
        assert!(p.events.recv_timeout(Duration::from_millis(10)).is_err());
    }
    let p = Peer::new();
    let mut b = hint().encode(8).unwrap();
    b[1] = 7;
    p.send(&b);
    let f = p.frame();
    assert_eq!(f.id, None);
    assert!(f.diagnostics.is_empty());
    let Outcome::Terminal(Terminal::Fault(f)) = p.next() else {
        panic!()
    };
    assert_eq!(f.failure_class, FailureClass::BadCrc);
    assert_eq!(f.header, None);
}

#[test]
fn all_ff_marker_is_crc_fault_not_recovery() {
    let p = Peer::new();
    p.send(&[255; 112]);
    assert!(matches!(
        p.frame().disposition,
        Disposition::FramingRejected(FailureClass::BadCrc)
    ));
}

#[test]
fn short_oversized_and_split_packets_fault_by_observed_length() {
    for len in [1, 56, 111, 113, 120, 224] {
        let p = Peer::new();
        p.send(&vec![0; len]);
        assert!(matches!(
            p.frame().disposition,
            Disposition::FramingRejected(FailureClass::BadLength)
        ));
        let Outcome::Terminal(Terminal::Fault(f)) = p.next() else {
            panic!()
        };
        assert_eq!(f.observed_len, Some(len.min(120)));
        assert_eq!(f.capture.as_ref().unwrap().len(), len.min(120));
        assert_eq!(f.expected_crc.is_some(), len >= 112);
    }
}

#[test]
fn zero_length_is_platform_specific() {
    let p = Peer::new();
    p.send(&[]);
    #[cfg(target_os = "linux")]
    assert!(matches!(
        p.next(),
        Outcome::Terminal(Terminal::Teardown(TeardownReason::PeerGone { .. }))
    ));
    #[cfg(target_os = "macos")]
    assert!(matches!(
        p.frame().disposition,
        Disposition::FramingRejected(FailureClass::BadLength)
    ));
}

#[test]
fn notification_semantics_continue_without_session_fault() {
    let p = Peer::new();
    let mut b = hint().encode(1).unwrap();
    b[8..16].fill(0);
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::ZeroId))
    ));
    let mut b = hint().encode(2).unwrap();
    b[16..80].fill(0);
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::EmptyMask)
    ));
    let mut b = hint().encode(3).unwrap();
    b[80..84].fill(0);
    b[84] = 7;
    checksum(&mut b);
    p.send(&b);
    let f = p.frame();
    assert_eq!(f.diagnostics[0].kind, DiagnosticKind::ViewWithoutEpoch);
    assert!(matches!(
        f.disposition,
        Disposition::Decoded(DecodedRecord::Notification(Notification { pane: None, .. }))
    ));
    assert!(p.bell.terminal_report().is_none());
}

#[test]
fn lifecycle_sequencing_dedup_gaps_and_implicit_close() {
    let p = Peer::new();
    p.setup_input();
    let opening = input(Phase::Press, 1, 0).encode(1).unwrap();
    p.send(&opening);
    assert_eq!(accepted(p.frame()).1, None);
    p.send(&opening);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::DuplicateOpening)
    ));
    p.send(&input(Phase::Drag, 1, 3).encode(3).unwrap());
    let f = p.frame();
    assert!(f
        .diagnostics
        .iter()
        .any(|d| d.kind == DiagnosticKind::SequenceGap && d.value == 2));
    accepted(f);
    p.send(&input(Phase::Drag, 1, 2).encode(4).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::DuplicateSequence)
    ));
    p.send(&input(Phase::Fire, 2, 0).encode(5).unwrap());
    accepted(p.frame());
    p.send(&input(Phase::Press, 3, 0).encode(6).unwrap());
    assert_eq!(accepted(p.frame()).1, Some(1));
    p.send(&input(Phase::Release, 3, 1).encode(7).unwrap());
    assert_eq!(accepted(p.frame()).0.phase, Phase::Release);
    p.send(&input(Phase::Cancel, 3, 2).encode(8).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::UnknownInteraction)
    ));
    p.bell.stop();
    assert!(matches!(p.next(), Outcome::Terminal(Terminal::Teardown(_))));
}

#[test]
fn rejected_press_closes_predecessor_but_malformed_fire_does_not() {
    let p = Peer::new();
    p.setup_input();
    p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
    accepted(p.frame());
    let mut fire = input(Phase::Fire, 2, 0).encode(2).unwrap();
    fire[20] = 1;
    checksum(&mut fire);
    p.send(&fire);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::SeqOnOpen))
    ));
    let mut press = input(Phase::Press, 3, 0).encode(3).unwrap();
    press[20] = 1;
    checksum(&mut press);
    p.send(&press);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::SeqOnOpen))
    ));
    let Outcome::Cancellation(c) = p.next() else {
        panic!()
    };
    assert_eq!(c.interaction.interaction, 1);
    p.send(&input(Phase::Fire, 2, 0).encode(4).unwrap());
    accepted(p.frame()); // rejected openings never advance watermark
}

#[test]
fn changed_replay_rejects_and_closes_without_merging_gestures() {
    let p = Peer::new();
    p.setup_input();
    p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
    accepted(p.frame());
    p.send(&input(Phase::Press, 1, 0).encode(2).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::InteractionMismatch))
    ));
    assert!(matches!(p.next(), Outcome::Cancellation(_)));
}

#[test]
fn epoch_install_closes_once_and_stale_press_preserves_current_gesture() {
    let p = Peer::new();
    p.setup_input();
    p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
    accepted(p.frame());
    p.bell.epoch_acknowledged(0, 4).unwrap();
    let Outcome::Cancellation(c) = p.next() else {
        panic!()
    };
    assert_eq!(c.reason, CancellationReason::EpochChanged);
    assert_eq!(c.interaction.epoch, 1);
    let mut current = input(Phase::Press, 2, 0);
    current.epoch = 4;
    p.send(&current.encode(2).unwrap());
    accepted(p.frame());
    p.bell.epoch_acknowledged(0, 4).unwrap();
    assert_eq!(
        p.bell.epoch_acknowledged(0, 2),
        Err(LifecycleError::OlderEpoch)
    );
    p.send(&input(Phase::Press, 3, 0).encode(3).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::StaleEpoch)
    ));
    current.phase = Phase::Release;
    current.seq = 1;
    p.send(&current.encode(4).unwrap());
    accepted(p.frame());
}

#[test]
fn rejection_prefers_interaction_over_wrong_tool_and_zero_falls_back() {
    let p = Peer::new();
    p.setup_input();
    p.bell.tool_activated(3).unwrap();
    p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
    accepted(p.frame());
    let mut e = input(Phase::Press, 2, 0);
    e.tool = 3;
    p.send(&e.encode(2).unwrap());
    accepted(p.frame());
    e = input(Phase::Release, 1, 1);
    e.tool = 3;
    p.send(&e.encode(3).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::InteractionMismatch))
    ));
    let Outcome::Cancellation(c) = p.next() else {
        panic!()
    };
    assert_eq!(c.interaction.tool, 1);
    e = input(Phase::Release, 2, 1);
    e.tool = 3;
    let mut b = e.encode(4).unwrap();
    b[16..20].fill(0);
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::ZeroId))
    ));
    let Outcome::Cancellation(c) = p.next() else {
        panic!()
    };
    assert_eq!(c.interaction.tool, 3);
}

#[test]
fn lifecycle_high_water_and_retirement_wake_idle_listener() {
    let p = Peer::new();
    p.bell.tool_activated(5).unwrap();
    p.bell.epoch_acknowledged(0, 1).unwrap();
    assert_eq!(p.bell.tool_activated(4), Err(LifecycleError::InvalidHandle));
    p.bell.tool_retired(4).unwrap();
    let mut e = input(Phase::Press, 1, 0);
    e.tool = 5;
    p.send(&e.encode(1).unwrap());
    accepted(p.frame());
    p.bell.tool_retired(5).unwrap();
    let Outcome::Cancellation(c) = p.next() else {
        panic!()
    };
    assert_eq!(c.reason, CancellationReason::ToolRetired);
    p.bell.tool_retired(5).unwrap();
    assert_eq!(p.bell.tool_activated(5), Err(LifecycleError::InvalidHandle));
    e.interaction = 2;
    p.send(&e.encode(2).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::RetiredTool)
    ));
    e.tool = 6;
    p.send(&e.encode(3).unwrap());
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::UnknownTool))
    ));
}

#[test]
fn unknown_enums_and_bits_keep_usable_transition() {
    let p = Peer::new();
    p.setup_input();
    let mut b = input(Phase::Press, 1, 0).encode(1).unwrap();
    b[47] = 99;
    b[48] = 99;
    b[45] = 0x81;
    b[49] = 0x81;
    b[52..96].fill(255);
    checksum(&mut b);
    p.send(&b);
    let f = p.frame();
    assert_eq!(f.diagnostics.len(), 4);
    let (e, _) = accepted(f);
    assert_eq!(e.position, Position::None);
    assert_eq!(e.target, None);
    assert_eq!(e.mods, 1);
    assert!(!e.miss);
    let mut b = input(Phase::Release, 1, 1).encode(2).unwrap();
    b[42] = 6;
    b[16..20].fill(0);
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Ignored(DiagnosticKind::UnknownPhase)
    ));
    p.send(&input(Phase::Release, 1, 1).encode(3).unwrap());
    accepted(p.frame());
}

#[test]
fn invalid_terminal_position_cancels_and_fixed_padding_faults() {
    for (offset, value, violation) in [
        (52, 1, Violation::PositionTail),
        (96, 1, Violation::PositionTail),
        (99, 1, Violation::PositionTail),
        (47, 1, Violation::InvalidTarget),
    ] {
        let p = Peer::new();
        p.setup_input();
        p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
        accepted(p.frame());
        let mut b = input(Phase::Release, 1, 1).encode(2).unwrap();
        b[offset] = value;
        checksum(&mut b);
        p.send(&b);
        assert!(
            matches!(p.frame().disposition,Disposition::Rejected(Rejection::Contract(v)) if v==violation)
        );
        assert!(matches!(p.next(), Outcome::Cancellation(_)));
        assert!(p.bell.terminal_report().is_none());
    }
    let p = Peer::new();
    p.setup_input();
    p.send(&input(Phase::Press, 1, 0).encode(1).unwrap());
    accepted(p.frame());
    let mut b = input(Phase::Release, 1, 1).encode(2).unwrap();
    b[100] = 1;
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::FramingRejected(FailureClass::BadPadding)
    ));
    assert!(matches!(p.next(), Outcome::Cancellation(_)));
    assert!(matches!(p.next(), Outcome::Terminal(Terminal::Fault(_))));
}

#[test]
fn unix_name_is_ignored_even_with_invalid_length_and_bytes() {
    let p = Peer::new();
    let (cap, _) = UnixStream::pair().unwrap();
    let mut b = grant().encode(1).unwrap();
    b[26..108].fill(255);
    checksum(&mut b);
    send(&p.host, &b, &[cap.as_raw_fd()]);
    let f = p.frame();
    assert_eq!(f.diagnostics[0].kind, DiagnosticKind::UnixNameIgnored);
    let Disposition::Decoded(DecodedRecord::Memory {
        grant: g,
        descriptor: Some(fd),
    }) = f.disposition
    else {
        panic!()
    };
    assert_eq!(g, grant());
    unsafe {
        assert_ne!(
            libc::fcntl(fd.as_raw_fd(), libc::F_GETFD) & libc::FD_CLOEXEC,
            0
        );
    }
}

#[test]
fn rejected_and_unknown_grants_close_rights_before_dispatch() {
    for mode in 0..4 {
        let p = Peer::new();
        let (cap, mut observer) = UnixStream::pair().unwrap();
        observer.set_read_timeout(Some(WAIT)).unwrap();
        let mut b = grant().encode(1).unwrap();
        match mode {
            0 => b[8..16].fill(0),
            1 => b[16..24].fill(0),
            2 => b[0] = 0x99,
            3 => b[25] = 1,
            _ => unreachable!(),
        };
        checksum(&mut b);
        send(&p.host, &b, &[cap.as_raw_fd()]);
        drop(cap);
        let f = p.frame();
        match mode {
            0 => assert!(matches!(
                f.disposition,
                Disposition::Rejected(Rejection::Contract(Violation::ZeroId))
            )),
            1 => assert!(matches!(
                f.disposition,
                Disposition::Rejected(Rejection::Grant { .. })
            )),
            2 => assert!(matches!(f.disposition, Disposition::UnknownOpcode(0x99))),
            3 => assert!(matches!(
                f.disposition,
                Disposition::FramingRejected(FailureClass::BadPadding)
            )),
            _ => unreachable!(),
        };
        assert_eq!(
            observer.read(&mut [0]).unwrap(),
            0,
            "received descriptor leaked"
        );
    }
}

#[test]
fn ancillary_count_and_truncation_close_all_received_rights() {
    for (op, count, expected) in [
        (b'M', 0, FailureClass::WrongFdCount),
        (b'D', 1, FailureClass::WrongFdCount),
        (0x99, 2, FailureClass::WrongFdCount),
        #[cfg(target_os = "linux")]
        (b'M', 16, FailureClass::Ctrunc),
        // Darwin uses a larger control buffer to prevent the kernel from
        // installing descriptors whose numbers it then omits during copyout.
        // Keep the EOF assertion: every surplus capability must be closed.
        #[cfg(target_os = "macos")]
        (b'M', 16, FailureClass::WrongFdCount),
        #[cfg(target_os = "macos")]
        (b'M', 64, FailureClass::WrongFdCount),
    ] {
        let p = Peer::new();
        let (cap, mut observer) = UnixStream::pair().unwrap();
        observer.set_read_timeout(Some(WAIT)).unwrap();
        let mut b = grant().encode(1).unwrap();
        b[0] = op;
        checksum(&mut b);
        send(&p.host, &b, &vec![cap.as_raw_fd(); count]);
        drop(cap);
        assert!(matches!(p.frame().disposition,Disposition::FramingRejected(c) if c==expected));
        let Outcome::Terminal(Terminal::Fault(fault)) = p.next() else {
            panic!("expected terminal ancillary fault")
        };
        assert_eq!(fault.msg_ctrunc, Some(expected == FailureClass::Ctrunc));
        assert_eq!(observer.read(&mut [0]).unwrap(), 0);
    }
}

#[test]
fn zero_length_with_descriptors_is_a_frame_and_closes_all_rights() {
    for (count, expected) in [
        (1, FailureClass::BadLength),
        (2, FailureClass::WrongFdCount),
        #[cfg(target_os = "linux")]
        (16, FailureClass::Ctrunc),
        #[cfg(target_os = "macos")]
        (16, FailureClass::WrongFdCount),
    ] {
        let p = Peer::new();
        let (cap, mut observer) = UnixStream::pair().unwrap();
        observer.set_read_timeout(Some(WAIT)).unwrap();
        send(&p.host, &[], &vec![cap.as_raw_fd(); count]);
        drop(cap);
        assert!(matches!(p.frame().disposition,Disposition::FramingRejected(c) if c==expected));
        let Outcome::Terminal(Terminal::Fault(fault)) = p.next() else {
            panic!("expected terminal empty-frame fault")
        };
        assert_eq!(fault.observed_len, Some(0));
        assert_eq!(fault.ordinal, Some(1));
        assert_eq!(fault.msg_ctrunc, Some(expected == FailureClass::Ctrunc));
        assert_eq!(observer.read(&mut [0]).unwrap(), 0, "descriptor leaked");
    }
}

#[test]
fn unclaimed_descriptor_closes_after_callback_and_claimed_one_survives_stop() {
    for claim in [false, true] {
        let (host, plugin) = socket_pair().unwrap();
        let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
        let (tx, rx) = mpsc::channel();
        let (done_tx, done_rx) = mpsc::channel();
        let thread = thread::spawn(move || {
            listener
                .run(|o| {
                    if let Outcome::Frame(f) = o {
                        if let Disposition::Decoded(DecodedRecord::Memory { descriptor, .. }) =
                            &mut f.disposition
                        {
                            tx.send(if claim { descriptor.take() } else { None })
                                .unwrap();
                        } else {
                            done_tx.send(()).unwrap();
                        }
                    }
                })
                .unwrap()
        });
        let (cap, mut observer) = UnixStream::pair().unwrap();
        observer.set_read_timeout(Some(WAIT)).unwrap();
        send(&host, &grant().encode(1).unwrap(), &[cap.as_raw_fd()]);
        drop(cap);
        let owned = rx.recv_timeout(WAIT).unwrap();
        send(&host, &hint().encode(2).unwrap(), &[]);
        done_rx.recv_timeout(WAIT).unwrap(); // callback returned
        bell.stop();
        thread.join().unwrap();
        if let Some(fd) = owned {
            let mut retained = UnixStream::from(fd);
            retained.write_all(b"x").unwrap();
            let mut byte = [0];
            observer.read_exact(&mut byte).unwrap();
            assert_eq!(byte, [b'x']);
        }
        assert_eq!(observer.read(&mut [0]).unwrap(), 0);
    }
}

#[test]
fn stop_is_independent_of_blocked_callback_and_preserves_emission_order() {
    let (host, plugin) = socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
    bell.tool_activated(1).unwrap();
    bell.epoch_acknowledged(0, 1).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let (tx, rx) = mpsc::channel();
    let thread = thread::spawn(move || {
        listener
            .run(|o| match o {
                Outcome::Frame(_) => {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(WAIT).unwrap();
                    tx.send("frame").unwrap();
                }
                Outcome::Cancellation(_) => tx.send("cancel").unwrap(),
                Outcome::Terminal(_) => tx.send("terminal").unwrap(),
            })
            .unwrap()
    });
    send(&host, &input(Phase::Press, 1, 0).encode(1).unwrap(), &[]);
    entered_rx.recv_timeout(WAIT).unwrap();
    bell.tool_retired(1).unwrap();
    let stopper = bell.clone();
    let (done_tx, done_rx) = mpsc::channel();
    let stop = thread::spawn(move || {
        stopper.stop();
        done_tx.send(()).unwrap();
    });
    done_rx.recv_timeout(Duration::from_secs(1)).unwrap();
    assert!(bell.terminal_report().is_some());
    assert_eq!(bell.cancel_interaction(1), Err(LifecycleError::Closed));
    release_tx.send(()).unwrap();
    stop.join().unwrap();
    thread.join().unwrap();
    assert_eq!(
        rx.into_iter().collect::<Vec<_>>(),
        ["frame", "cancel", "terminal"]
    );
}

#[test]
fn callback_can_reenter_transport_and_panic_does_not_leak_grant() {
    let (host, plugin) = socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
    let reentrant = bell.clone();
    let thread = thread::spawn(move || {
        listener.run(|_| {
            reentrant.tool_activated(1).unwrap();
            panic!("consumer failed");
        })
    });
    let (cap, mut observer) = UnixStream::pair().unwrap();
    observer.set_read_timeout(Some(WAIT)).unwrap();
    send(&host, &grant().encode(1).unwrap(), &[cap.as_raw_fd()]);
    drop(cap);
    assert!(thread.join().unwrap().is_err());
    assert_eq!(observer.read(&mut [0]).unwrap(), 0);
    assert!(matches!(
        bell.terminal_report(),
        Some(Terminal::Teardown(TeardownReason::ConsumerPanicked))
    ));
}

#[test]
fn refusal_burns_ids_and_never_escalates() {
    let p = Peer::new();
    assert_eq!(
        p.bell.notify(&hint()).unwrap(),
        SendOutcome::HandshakePending
    );
    assert!(p.bell.handshake_completed(0, 12).is_err());
    p.bell.handshake_completed(1, 0).unwrap();
    let mut attempts = 0;
    loop {
        attempts += 1;
        let r = p.bell.notify(&hint()).unwrap();
        if r == SendOutcome::Dropped {
            break;
        }
        assert_eq!(r, SendOutcome::Sent);
        assert!(attempts < 100_000);
    }
    for _ in 0..10 {
        assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Dropped);
        attempts += 1;
    }
    assert!(p.bell.terminal_report().is_none());
    loop {
        let mut b = [0u8; 120];
        let n = unsafe {
            libc::recv(
                p.host.as_raw_fd(),
                b.as_mut_ptr().cast(),
                b.len(),
                libc::MSG_DONTWAIT,
            )
        };
        if n < 0 {
            assert_eq!(
                std::io::Error::last_os_error().kind(),
                std::io::ErrorKind::WouldBlock
            );
            break;
        }
        assert_eq!(n, 112);
    }
    assert_eq!(p.bell.notify(&hint()).unwrap(), SendOutcome::Sent);
    let mut b = [0u8; 120];
    let n = unsafe {
        libc::recv(
            p.host.as_raw_fd(),
            b.as_mut_ptr().cast(),
            b.len(),
            libc::MSG_DONTWAIT,
        )
    };
    assert_eq!(n, 112);
    assert_eq!(
        u32::from_le_bytes(b[4..8].try_into().unwrap()),
        attempts + 1
    );
    assert_eq!(p.bell.statistics().notifications_refused, 11);
}

#[test]
fn concurrent_senders_allocate_ids_in_wire_order() {
    let p = Peer::new();
    p.bell.handshake_completed(1, 0).unwrap();
    let threads: Vec<_> = (0..4)
        .map(|_| {
            let b = p.bell.clone();
            thread::spawn(move || {
                for _ in 0..4 {
                    assert_eq!(b.notify(&hint()).unwrap(), SendOutcome::Sent);
                }
            })
        })
        .collect();
    for t in threads {
        t.join().unwrap();
    }
    for id in 1u32..=16 {
        let mut bytes = [0u8; 120];
        assert_eq!(
            unsafe {
                libc::recv(
                    p.host.as_raw_fd(),
                    bytes.as_mut_ptr().cast(),
                    120,
                    libc::MSG_DONTWAIT,
                )
            },
            112
        );
        assert_eq!(&bytes[4..8], &id.to_le_bytes());
    }
}

#[test]
#[cfg(target_os = "linux")]
fn peer_close_emits_teardown_and_cancels_open_interactions() {
    check_teardown_cancels_open_interactions(false);
}

#[test]
fn control_plane_stop_cancels_open_interactions_without_socket_eof() {
    // Protocol §8: the control plane announces teardown. In particular, a
    // Darwin datagram close need not give the listener a durable EOF signal.
    check_teardown_cancels_open_interactions(true);
}

fn check_teardown_cancels_open_interactions(control_plane_stop: bool) {
    let (host, plugin) = socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
    bell.tool_activated(1).unwrap();
    bell.epoch_acknowledged(0, 1).unwrap();
    let (tx, rx) = mpsc::channel();
    let thread = thread::spawn(move || {
        listener
            .run(|o| tx.send(format!("{o:?}")).unwrap())
            .unwrap()
    });
    send(&host, &input(Phase::Press, 1, 0).encode(1).unwrap(), &[]);
    assert!(rx.recv_timeout(WAIT).unwrap().starts_with("Frame"));
    if control_plane_stop {
        // Keep the peer socket alive: no socket-close notification is needed.
        bell.stop();
    } else {
        drop(host);
    }
    assert!(rx.recv_timeout(WAIT).unwrap().starts_with("Cancellation"));
    let terminal = rx.recv_timeout(WAIT).unwrap();
    let expected = if control_plane_stop {
        "Terminal(Teardown(LocalStop"
    } else {
        "Terminal(Teardown(PeerGone"
    };
    assert!(terminal.starts_with(expected), "{terminal}");
    bell.stop(); // A later stop must not emit another terminal or cancellation.
    thread.join().unwrap();
    assert!(matches!(
        rx.try_recv(),
        Err(mpsc::TryRecvError::Disconnected)
    ));
}

#[test]
fn every_position_layout_roundtrips_and_nonfinite_rays_are_rejected() {
    let p = Peer::new();
    p.setup_input();
    let ray = Ray {
        camera: [-1.0, 2.0, 3.0],
        far: [10.0, 20.0, 30.0],
        entry_frac: 0.25,
        roll: 0.5,
    };
    let positions = vec![
        Position::None,
        Position::Voxel([1.0, 2.0, 3.0]),
        Position::Picked([4.0, 5.0, 6.0]),
        Position::ColumnYx {
            y: 1.0,
            x: 2.0,
            z_min: 3.0,
            z_max: 4.0,
        },
        Position::ColumnZx {
            z: 1.0,
            x: 2.0,
            y_min: 3.0,
            y_max: 4.0,
        },
        Position::ColumnZy {
            z: 1.0,
            y: 2.0,
            x_min: 3.0,
            x_max: 4.0,
        },
        Position::Ray(ray.clone()),
        Position::RayAndPicked {
            ray,
            picked: [7.0, 8.0, 9.0],
            back_frac: 0.75,
        },
    ];
    for (i, position) in positions.into_iter().enumerate() {
        let mut e = input(Phase::Fire, i as u32 + 1, 0);
        e.position = position;
        e.roll_ref_y = true;
        e.t = 123;
        e.channels = 0x8001;
        e.level = 2;
        e.mods = 15;
        let b = e.encode(i as u32 + 1).unwrap();
        assert_eq!(b[48], i as u8); // independent numeric kind mapping
        p.send(&b);
        assert_eq!(accepted(p.frame()).0, e);
    }
    for case in 0..5 {
        let mut e = input(Phase::Fire, 9, 0);
        e.position = Position::Ray(Ray {
            camera: [0.0; 3],
            far: [1.0; 3],
            entry_frac: 0.5,
            roll: 0.0,
        });
        let mut b = e.encode(case + 9).unwrap();
        match case {
            0 => b[52..56].copy_from_slice(&f32::NAN.to_le_bytes()),
            1 => b[80..84].copy_from_slice(&f32::INFINITY.to_le_bytes()),
            2 => b[76..80].copy_from_slice(&1.1f32.to_le_bytes()),
            3 => b[64..76].fill(0),
            4 => b[84] = 1,
            _ => unreachable!(),
        }
        checksum(&mut b);
        p.send(&b);
        let expected = if case == 4 {
            Violation::PositionTail
        } else {
            Violation::NonfinitePosition
        };
        assert!(
            matches!(p.frame().disposition,Disposition::Rejected(Rejection::Contract(v)) if v==expected)
        );
    }
    let mut b = input(Phase::Fire, 9, 0).encode(14).unwrap();
    b[48] = 2;
    b[49] = 1;
    checksum(&mut b);
    p.send(&b);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Rejected(Rejection::Contract(Violation::MissWithPicked))
    ));
}

#[test]
fn picked_back_roundtrips_in_every_phase_and_point_picks_have_no_back() {
    let p = Peer::new();
    p.setup_input();
    for (i, (phase, interaction, seq, back_frac, back)) in [
        // The back is measured from the picked front, half way along the
        // ray: 0.0 is the front itself, 0.5 half of the half that is left.
        (Phase::Press, 1, 0, 0.0, Some([2.0, 8.0, 16.0])),
        (Phase::Drag, 1, 1, 0.5, Some([4.0, 10.0, 20.0])),
        (Phase::Release, 1, 2, 1.0, Some([6.0, 12.0, 24.0])),
        (Phase::Fire, 2, 0, f32::NAN, None),
        (Phase::Press, 3, 0, f32::NAN, None),
        (Phase::Cancel, 3, 1, 0.5, Some([4.0, 10.0, 20.0])),
    ]
    .into_iter()
    .enumerate()
    {
        let mut e = input(phase, interaction, seq);
        e.position = Position::RayAndPicked {
            ray: Ray {
                camera: [-2.0, 4.0, 8.0],
                far: [6.0, 12.0, 24.0],
                entry_frac: 0.75, // back_frac has no ordering rule against entry_frac.
                roll: 0.0,
            },
            picked: [2.0, 8.0, 16.0],
            back_frac,
        };
        let id = i as u32 + 1;
        let b = e.encode(id).unwrap();
        assert_eq!(&b[96..100], &back_frac.to_le_bytes());
        assert_eq!(&b[100..108], &[0; 8]);
        p.send(&b);
        let decoded = accepted(p.frame()).0;
        assert_eq!(decoded.position.is_point(), back_frac.is_nan());
        assert_eq!(decoded.position.back(), back);
        // The front is the voxel's, whether or not anything stood out.
        assert_eq!(decoded.position.front_frac(), Some(0.5));
        assert_eq!(decoded.position.front(), Some([2.0, 8.0, 16.0]));
        assert_eq!(decoded.ray(), decoded.pick().and_then(|_| decoded.ray()));
        assert!(matches!(
            decoded.pick(),
            Some(Position::RayAndPicked { picked, .. }) if *picked == [2.0, 8.0, 16.0]
        ));
        // Compare bytes because a NaN point pick is not equal to itself.
        assert_eq!(decoded.encode(id).unwrap(), b);
    }
    for position in [Position::None, Position::Picked([1.0; 3])] {
        assert!(!position.is_point());
        assert_eq!(position.back(), None);
        assert_eq!(position.front(), None);
        assert_eq!(position.front_frac(), None);
    }
    let wide = Position::RayAndPicked {
        ray: Ray {
            camera: [-f32::MAX; 3],
            far: [f32::MAX; 3],
            entry_frac: 0.0,
            roll: 0.0,
        },
        picked: [0.0; 3],
        back_frac: 0.5,
    };
    assert_eq!(wide.front(), Some([0.0; 3]));
    assert_eq!(wide.back(), Some([f32::MAX / 2.0; 3]));
}

fn pick(camera: [f32; 3], far: [f32; 3], picked: [f32; 3], back_frac: f32) -> Position {
    Position::RayAndPicked {
        ray: Ray {
            camera,
            far,
            entry_frac: 0.0,
            roll: 0.0,
        },
        picked,
        back_frac,
    }
}

fn near(got: Option<[f32; 3]>, want: [f32; 3]) {
    let got = got.unwrap();
    for i in 0..3 {
        assert!((got[i] - want[i]).abs() < 1e-4, "{got:?} != {want:?}");
    }
}

/// The back is measured from the front: back = front + back_frac * (far - front).
#[test]
fn the_back_is_measured_from_the_picked_front() {
    // vizi's own vector (test_the_sdk_puts_the_back_where_the_host_measured_it):
    // a front at t = 0.2 and a back at t = 0.5, encoded as 0.3 / 0.8.
    let host = pick([1.0, 2.0, 3.0], [9.0, 6.0, 43.0], [2.6, 2.8, 11.0], 0.375);
    assert!((host.front_frac().unwrap() - 0.2).abs() < 1e-6);
    near(host.front(), [2.6, 2.8, 11.0]);
    near(host.back(), [5.0, 4.0, 23.0]);

    let (camera, far) = ([0.0; 3], [0.0, 0.0, 10.0]);
    // 0.0 is NOT a point: a real pick of no depth, its back at its front and
    // not at the camera -- what a sender that predates the field leaves.
    let zeros = pick(camera, far, [0.0, 0.0, 4.0], 0.0);
    assert!(!zeros.is_point());
    assert_eq!(zeros.front(), Some([0.0, 0.0, 4.0]));
    assert_eq!(zeros.back(), zeros.front());
    // NaN is the other answer: nothing stood out, and there is no back.
    let nothing = pick(camera, far, [0.0, 0.0, 4.0], f32::NAN);
    assert!(nothing.is_point());
    assert_eq!(nothing.back(), None);
    assert_eq!(nothing.front(), Some([0.0, 0.0, 4.0]));

    // A voxel center off the ray is projected onto it.
    let off = pick(camera, far, [0.0, 3.0, 2.0], 0.5);
    assert!((off.front_frac().unwrap() - 0.2).abs() < 1e-12);
    near(off.front(), [0.0, 0.0, 2.0]);
    near(off.back(), [0.0, 0.0, 6.0]);
    assert_eq!(pick(camera, far, [0.0, 0.0, 2.0], 1.0).back(), Some(far));
    // The front is clamped to the segment; at far no ray is left.
    let behind = pick(camera, far, [0.0, 0.0, -3.0], 0.5);
    assert_eq!(behind.front_frac(), Some(0.0));
    assert_eq!(behind.back(), Some([0.0, 0.0, 5.0]));
    let past = pick(camera, far, [0.0, 0.0, 12.0], 0.5);
    assert_eq!(past.front_frac(), Some(1.0));
    assert_eq!(past.back(), Some(far));
    // camera == far has no direction: the front fraction is 0. (The receiver
    // refuses such a record; the accessor still answers.)
    let flat = pick([1.0; 3], [1.0; 3], [0.0; 3], 0.5);
    assert_eq!(flat.front_frac(), Some(0.0));
    assert_eq!(flat.front(), Some([1.0; 3]));
    assert_eq!(flat.back(), Some([1.0; 3]));
}

/// A tool takes either record whichever it declared.
#[test]
fn a_tool_reads_the_ray_the_same_way_with_or_without_a_pick() {
    let ray = Ray {
        camera: [0.0; 3],
        far: [10.0, 0.0, 0.0],
        entry_frac: 0.25,
        roll: 1.5,
    };
    let mut bare = input(Phase::Fire, 1, 0);
    bare.position = Position::Ray(ray.clone());
    let mut picked = input(Phase::Fire, 2, 0);
    picked.position = Position::RayAndPicked {
        ray: ray.clone(),
        picked: [5.0, 0.0, 0.0],
        back_frac: 0.5,
    };
    let mut voxel = input(Phase::Fire, 3, 0);
    voxel.position = Position::Voxel([1.0, 2.0, 3.0]);

    assert_eq!(bare.ray(), Some(&ray));
    assert_eq!(bare.ray(), picked.ray());
    assert!(bare.pick().is_none());
    assert_eq!(picked.pick().unwrap().back(), Some([7.5, 0.0, 0.0]));
    assert!(voxel.ray().is_none());
    assert!(voxel.pick().is_none());
}

#[test]
fn picked_back_validation_allows_nan_only_in_back_frac_and_forbids_miss() {
    let p = Peer::new();
    p.setup_input();
    let mut e = input(Phase::Fire, 1, 0);
    e.position = Position::RayAndPicked {
        ray: Ray {
            camera: [0.0; 3],
            far: [1.0; 3],
            entry_frac: 0.25,
            roll: 0.0,
        },
        picked: [0.5; 3],
        back_frac: f32::NAN,
    };
    let valid = e.encode(1).unwrap();
    for value in [-0.5, 1.25, f32::INFINITY, f32::NEG_INFINITY] {
        if let Position::RayAndPicked { back_frac, .. } = &mut e.position {
            *back_frac = value;
        }
        assert!(e.encode(1).is_err(), "back_frac {value}");
    }
    // Independent wire corruption checks the receiver, including every float
    // that must remain finite while back_frac carries its valid NaN sentinel.
    for (offset, value) in (0..11)
        .map(|i| (52 + i * 4, f32::NAN))
        .chain([-0.5, 1.25, f32::INFINITY, f32::NEG_INFINITY].map(|v| (96, v)))
    {
        let mut b = valid;
        b[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
        checksum(&mut b);
        p.send(&b);
        assert!(matches!(
            p.frame().disposition,
            Disposition::Rejected(Rejection::Contract(Violation::NonfinitePosition))
        ));
        assert!(p.bell.terminal_report().is_none());
    }
    for value in [0.5, f32::NAN] {
        if let Position::RayAndPicked { back_frac, .. } = &mut e.position {
            *back_frac = value;
        }
        e.miss = true;
        assert!(e.encode(1).is_err());
        let mut b = valid;
        b[96..100].copy_from_slice(&value.to_le_bytes());
        b[49] = 1; // MISS
        checksum(&mut b);
        p.send(&b);
        assert!(matches!(
            p.frame().disposition,
            Disposition::Rejected(Rejection::Contract(Violation::MissWithPicked))
        ));
    }
}

#[test]
fn crc_all_ones_is_an_ordinary_valid_checksum() {
    // Derived directly with stdlib zlib and GF(2) elimination on four mask
    // bytes, independently of this crate's checksum/encoder implementation.
    let hex="440200000100000001000000010000000100000000000000000000000000000093e84ab4000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000000ffffffff";
    let bytes: Vec<_> = (0..hex.len())
        .step_by(2)
        .map(|i| u8::from_str_radix(&hex[i..i + 2], 16).unwrap())
        .collect();
    assert_eq!(bytes.len(), 112);
    assert_eq!(crc32(&bytes[..108]), u32::MAX);
    let p = Peer::new();
    p.send(&bytes);
    assert!(matches!(
        p.frame().disposition,
        Disposition::Decoded(DecodedRecord::Notification(_))
    ));
}

#[test]
fn terminal_monitor_wakes_while_callback_is_blocked() {
    let (host, plugin) = socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(plugin, Config::default()).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let thread = thread::spawn(move || {
        listener
            .run(|o| {
                if matches!(o, Outcome::Frame(_)) {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(WAIT).unwrap();
                }
            })
            .unwrap()
    });
    send(&host, &hint().encode(1).unwrap(), &[]);
    entered_rx.recv_timeout(WAIT).unwrap();
    assert!(bell.wait_terminal(Duration::ZERO).is_none());
    let monitor = bell.clone();
    let (tx, rx) = mpsc::channel();
    let monitor_thread = thread::spawn(move || tx.send(monitor.wait_terminal(WAIT)).unwrap());
    bell.stop();
    assert!(matches!(
        rx.recv_timeout(WAIT).unwrap(),
        Some(Terminal::Teardown(TeardownReason::LocalStop))
    ));
    release_tx.send(()).unwrap();
    thread.join().unwrap();
    monitor_thread.join().unwrap();
}

#[test]
fn resource_limits_fail_before_mutation_and_noops_still_succeed() {
    let (host, plugin) = socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(
        plugin,
        Config {
            max_active_tools: 1,
        },
    )
    .unwrap();
    bell.tool_activated(1).unwrap();
    assert_eq!(bell.tool_activated(2), Err(LifecycleError::Capacity));
    bell.epoch_acknowledged(0, 1).unwrap();
    let (entered_tx, entered_rx) = mpsc::channel();
    let (release_tx, release_rx) = mpsc::channel();
    let thread = thread::spawn(move || {
        listener
            .run(|o| {
                if matches!(o, Outcome::Frame(_)) {
                    entered_tx.send(()).unwrap();
                    release_rx.recv_timeout(WAIT).unwrap();
                }
            })
            .unwrap()
    });
    send(&host, &input(Phase::Press, 1, 0).encode(1).unwrap(), &[]);
    entered_rx.recv_timeout(WAIT).unwrap();
    bell.epoch_acknowledged(0, 2).unwrap(); // queues one cancellation
    bell.epoch_acknowledged(0, 2).unwrap(); // repeat must still succeed
    bell.cancel_interaction(999).unwrap();
    bell.tool_retired(1).unwrap();
    bell.tool_activated(2).unwrap(); // failed activation did not burn high-water
    bell.stop();
    release_tx.send(()).unwrap();
    thread.join().unwrap();
}

#[test]
fn child_process_reports_stranded_doorbell() {
    let Ok(mode) = std::env::var("VIZI_RUST_TEST_STRANDED_DOORBELL") else {
        return;
    };
    if mode == "closed" {
        // SAFETY: the parent supplies /dev/null at stdin exclusively for this
        // test; no stdin owner is used by the child. Model a launcher closing it.
        assert_eq!(unsafe { libc::close(0) }, 0);
    }
    // SAFETY: this isolated child has exclusive ownership of stdin, or closed
    // it above, and nothing replaces or accesses it during adoption.
    let error = match unsafe { Doorbell::from_env(Config::default()) } {
        Err(error) => error,
        Ok(_) => panic!("invalid inherited descriptor was accepted"),
    };
    let message = error.to_string();
    if mode == "stream" {
        assert!(message.contains("SO_TYPE"), "{message}");
        assert!(!message.contains("launcher"), "{message}");
    } else {
        assert!(message.contains("cannot adopt doorbell fd 0"), "{message}");
        assert!(message.contains("launcher"), "{message}");
        assert!(message.contains("Exec the plugin"), "{message}");
        assert!(message.contains("VIZI_PLUGIN_DOORBELL_FD"), "{message}");
        let errno = if mode == "closed" {
            libc::EBADF
        } else {
            libc::ENOTSOCK
        };
        assert!(
            message.contains(&std::io::Error::from_raw_os_error(errno).to_string()),
            "{message}"
        );
    }
    // SAFETY: F_GETFD only inspects the number. Failed adoption must not leak
    // the non-socket or wrong-type descriptor; a closed fd must remain closed.
    assert_eq!(unsafe { libc::fcntl(0, libc::F_GETFD) }, -1);
    assert_eq!(
        std::io::Error::last_os_error().raw_os_error(),
        Some(libc::EBADF)
    );
    eprintln!("{message}");
}

#[test]
fn inherited_doorbell_failures_explain_launcher_without_aborting() {
    use std::process::{Command, Stdio};
    for mode in ["closed", "file", "stream"] {
        let (stream, _peer) = UnixStream::pair().unwrap();
        let stdin = if mode == "stream" {
            Stdio::from(OwnedFd::from(stream))
        } else {
            Stdio::null()
        };
        let output = Command::new(std::env::current_exe().unwrap())
            .args([
                "--exact",
                "child_process_reports_stranded_doorbell",
                "--nocapture",
            ])
            .env("VIZI_RUST_TEST_STRANDED_DOORBELL", mode)
            .env("VIZI_PLUGIN_DOORBELL_FD", "0")
            .stdin(stdin)
            .output()
            .unwrap();
        let stderr = String::from_utf8_lossy(&output.stderr);
        assert!(
            output.status.success(),
            "{mode}: {}\n{stderr}",
            output.status
        );
        let expected = if mode == "stream" {
            "SO_TYPE"
        } else {
            "cannot adopt doorbell fd 0"
        };
        assert!(stderr.contains(expected), "{mode}: {stderr}");
    }
}

#[test]
fn child_process_receives_shared_file_and_notifies() {
    if std::env::var_os("VIZI_RUST_TEST_CHILD").is_none() {
        return;
    }
    // SAFETY: this test's parent installed the exclusively owned doorbell at
    // stdin through Command::stdin; no stdin object is used in the child.
    let (bell, listener) = unsafe { Doorbell::from_env(Config::default()) }.unwrap();
    bell.handshake_completed(1, 0).unwrap();
    listener
        .run(|o| {
            if let Outcome::Frame(f) = o {
                let Disposition::Decoded(DecodedRecord::Memory { descriptor, .. }) =
                    &mut f.disposition
                else {
                    panic!("expected grant")
                };
                let file = std::fs::File::from(descriptor.take().unwrap());
                use std::os::unix::fs::FileExt;
                let mut bytes = [0u8; 8];
                file.read_exact_at(&mut bytes, 32).unwrap();
                assert_eq!(&bytes, b"shared!!");
                file.write_all_at(b"updated!", 32).unwrap();
                assert_eq!(bell.notify(&hint()).unwrap(), SendOutcome::Sent);
                bell.stop();
            }
        })
        .unwrap();
}

#[test]
fn descriptor_and_shared_backing_object_cross_an_exec_boundary() {
    use std::{
        os::unix::fs::FileExt,
        process::{Command, Stdio},
    };
    let (host, plugin) = socket_pair().unwrap();
    let mut child = Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "child_process_receives_shared_file_and_notifies",
            "--nocapture",
        ])
        .env("VIZI_RUST_TEST_CHILD", "1")
        .env("VIZI_PLUGIN_DOORBELL_FD", "0")
        .stdin(Stdio::from(plugin))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let path = std::env::temp_dir().join(format!("vizi-rust-doorbell-{}", child.id()));
    let file = std::fs::OpenOptions::new()
        .read(true)
        .write(true)
        .create_new(true)
        .open(&path)
        .unwrap();
    std::fs::remove_file(path).unwrap();
    file.set_len(4096).unwrap();
    file.write_all_at(b"shared!!", 32).unwrap();
    send(&host, &grant().encode(1).unwrap(), &[file.as_raw_fd()]);
    let deadline = std::time::Instant::now() + WAIT;
    while child.try_wait().unwrap().is_none() {
        if std::time::Instant::now() > deadline {
            child.kill().unwrap();
            panic!("child transport timed out");
        }
        thread::sleep(Duration::from_millis(5));
    }
    let output = child.wait_with_output().unwrap();
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
    let mut b = [0u8; 120];
    assert_eq!(
        unsafe {
            libc::recv(
                host.as_raw_fd(),
                b.as_mut_ptr().cast(),
                120,
                libc::MSG_DONTWAIT,
            )
        },
        112
    );
    assert_eq!(&b[..112], &hint().encode(1).unwrap());
    let mut value = [0; 8];
    file.read_exact_at(&mut value, 32).unwrap();
    assert_eq!(&value, b"updated!");
}

#[test]
fn action_fire_keeps_the_open_gesture_and_decodes_little_endian() {
    let p = Peer::new();
    p.setup_input();
    for (id, phase, interaction, seq, action) in [
        (1, Phase::Press, 1, 0, 0),
        (2, Phase::Fire, 2, 0, 0x1234),
        (3, Phase::Drag, 1, 1, 0),
        (4, Phase::Fire, 3, 0, u16::MAX),
        (5, Phase::Release, 1, 2, 0),
        (6, Phase::Fire, 4, 0, 1),
    ] {
        let mut event = input(phase, interaction, seq);
        event.action = action;
        let encoded = event.encode(id).unwrap();
        assert_eq!(&encoded[50..52], &action.to_le_bytes());
        // Independently put the field in an otherwise ordinary event record.
        let mut bytes = input(phase, interaction, seq).encode(id).unwrap();
        bytes[50] = action as u8;
        bytes[51] = (action >> 8) as u8;
        checksum(&mut bytes);
        p.send(&bytes);
        let frame = p.frame();
        assert!(frame.diagnostics.is_empty());
        let (decoded, closed) = accepted(frame);
        assert_eq!(decoded, event);
        assert_eq!(closed, None);
        assert!(p.bell.terminal_report().is_none());
    }
}
