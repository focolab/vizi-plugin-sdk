pub use crate::input::{Cancellation, CancellationReason, Interaction, LifecycleError};
#[cfg(unix)]
pub use crate::socket::socket_pair;
pub use crate::socket::{Descriptor, SocketInfo};
use crate::{
    input::{InputDisposition, InputState},
    socket,
    wire::*,
};
#[cfg(unix)]
use std::os::fd::OwnedFd;
use std::{
    collections::{BTreeMap, VecDeque},
    io::{self, Read, Write},
    panic::{catch_unwind, AssertUnwindSafe},
    sync::{Arc, Condvar, Mutex, MutexGuard},
    time::Duration,
};

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Direction {
    HostToPlugin,
    PluginToHost,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum TransportClass {
    Seqpacket,
    Datagram,
    Stream,
}
impl TransportClass {
    pub fn current() -> Self {
        #[cfg(windows)]
        {
            Self::Stream
        }
        #[cfg(target_os = "linux")]
        {
            Self::Seqpacket
        }
        #[cfg(target_os = "macos")]
        {
            Self::Datagram
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum FailureClass {
    BadCrc,
    BadLength,
    BadVer,
    BadFlags,
    BadReserved,
    BadPadding,
    Ctrunc,
    WrongFdCount,
    ReservedOp,
    ShortWrite,
    AmbiguousSend,
    RecvError,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Header {
    pub op: u8,
    pub version: u8,
    pub flags: u8,
    pub reserved: u8,
    pub id: u32,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Fault {
    pub failure_class: FailureClass,
    pub capture: Option<Vec<u8>>,
    pub observed_len: Option<usize>,
    pub ordinal: Option<u64>,
    pub msg_ctrunc: Option<bool>,
    pub os_error: Option<i32>,
    pub expected_crc: Option<u32>,
    pub observed_crc: Option<u32>,
    /// Present only when CRC has established that these bytes are a header.
    pub header: Option<Header>,
    pub transport_class: TransportClass,
    pub direction: Direction,
    pub socket: SocketInfo,
    pub detail: String,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum TeardownReason {
    PeerGone {
        os_error: Option<i32>,
        capture: Vec<u8>,
    },
    LocallyClosed {
        os_error: i32,
    },
    LocalStop,
    ListenerDropped,
    ConsumerPanicked,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Terminal {
    Fault(Arc<Fault>),
    Teardown(TeardownReason),
}

#[derive(Debug)]
pub enum DecodedRecord {
    /// Windows capability: validated UTF-8 section name, no attached fd/handle.
    /// Opening the section and validating its identity belongs to the memory layer.
    NamedMemory {
        grant: MemoryGrant,
        name: String,
    },
    /// Take the descriptor with `descriptor.take()` to claim it. Otherwise the
    /// transport closes it when the callback returns, including during unwind.
    Memory {
        grant: MemoryGrant,
        descriptor: Option<Descriptor>,
    },
    Input {
        event: InputEvent,
        implicitly_closed: Option<u32>,
    },
    Notification(Notification),
}
#[derive(Debug)]
pub enum Disposition {
    Decoded(DecodedRecord),
    UnknownOpcode(u8),
    Rejected(Rejection),
    Ignored(DiagnosticKind),
    FramingRejected(FailureClass),
}
#[derive(Debug)]
pub struct FrameOutcome {
    /// Exact received bytes; malformed packets can have lengths other than 112.
    pub capture: Vec<u8>,
    pub ordinal: u64,
    /// Present after CRC and op-independent header checks, before opcode rules.
    pub id: Option<u32>,
    pub diagnostics: Vec<Diagnostic>,
    pub disposition: Disposition,
}
#[derive(Debug)]
pub enum Outcome {
    Frame(FrameOutcome),
    Cancellation(Cancellation),
    Terminal(Terminal),
}

// Internal fences share the outcome queue so control-plane lifecycle hooks
// cannot overtake input/cancellation outcomes already accepted by the listener.
enum QueuedOutcome {
    Event(Outcome),
    Fence(Box<dyn FnOnce() + Send>),
}

#[derive(Clone, Debug, Default)]
pub struct Statistics {
    pub frames_examined: u64,
    pub notifications_sent: u64,
    pub notifications_refused: u64,
    pub notifications_suppressed: u64,
    pub diagnostic_counts: BTreeMap<DiagnosticKind, u64>,
    pub missing_record_ids: u64,
    pub missing_samples: u64,
}
#[derive(Clone, Debug)]
pub struct Config {
    /// Local resource policy, not a wire constant. Reject activation before its
    /// gRPC acknowledgement if this many tools are already active.
    pub max_active_tools: usize,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            max_active_tools: 1024,
        }
    }
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum SendOutcome {
    Sent,
    Dropped,
    Suppressed,
    Closed,
    HandshakePending,
}
#[derive(Debug)]
pub enum RunError {
    ConsumerPanicked,
}
impl std::fmt::Display for RunError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}
impl std::error::Error for RunError {}

struct State {
    socket: Option<socket::DoorbellSocket>,
    info: SocketInfo,
    input: InputState,
    outcomes: VecDeque<QueuedOutcome>,
    terminal: Option<Terminal>,
    terminal_ready: Arc<Condvar>,
    exited: bool,
    handshake: bool,
    session_token: Option<[u8; 32]>,
    receive_window: Vec<u8>,
    next_send: u32,
    expected_receive: u32,
    stats: Statistics,
}
struct Shared {
    state: Mutex<State>,
    wake: socket::WakeStream,
    terminal_ready: Arc<Condvar>,
}
impl Shared {
    fn lock(&self) -> MutexGuard<'_, State> {
        // User code is never called under this mutex. Poison would indicate an
        // internal bug; recovering is limited to allowing Drop to release fds.
        self.state.lock().unwrap_or_else(|p| p.into_inner())
    }
    fn wake(&self) {
        let mut wake = &self.wake;
        loop {
            match wake.write(&[1]) {
                Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                // Would-block means a wake is already pending. Broken pipe
                // means the listener has exited; the terminal mailbox persists.
                _ => return,
            }
        }
    }
}

/// Cloneable producer/lifecycle/stop handle; owns no extra copy of the socket.
#[derive(Clone)]
pub struct Doorbell {
    shared: Arc<Shared>,
}
/// The sole receiver and outcome dispatcher. Move it to one dedicated thread.
pub struct Listener {
    shared: Arc<Shared>,
    wake: socket::WakeStream,
    #[cfg(test)]
    after_empty_outcomes: Option<Box<dyn FnOnce() + Send>>,
}

impl Doorbell {
    /// Connect and send the 32-byte token within one total launch deadline.
    /// No socket is exposed until authentication bytes have been sent. The host
    /// validates those bytes; its Hello token must subsequently match too.
    #[cfg(windows)]
    pub fn connect(
        address: &str,
        token: &str,
        config: Config,
        timeout: Duration,
    ) -> io::Result<(Self, Listener)> {
        let token = crate::tcp::token(token)?;
        let stream = crate::tcp::connect(address, &token, timeout)?;
        Self::build(stream, config, Some(token))
    }

    /// Read VIZI_PLUGIN_DOORBELL_ADDR and VIZI_PLUGIN_TOKEN. The default total
    /// connect/authentication deadline is 5 seconds; use connect to override it.
    /// Publishing VIZI_PLUGIN_PORT_FILE belongs to the gRPC supervisor.
    #[cfg(windows)]
    pub fn from_env(config: Config) -> io::Result<(Self, Listener)> {
        let address = std::env::var("VIZI_PLUGIN_DOORBELL_ADDR").map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "VIZI_PLUGIN_DOORBELL_ADDR is required",
            )
        })?;
        let token = std::env::var("VIZI_PLUGIN_TOKEN").map_err(|_| {
            io::Error::new(io::ErrorKind::InvalidInput, "VIZI_PLUGIN_TOKEN is required")
        })?;
        Self::connect(&address, &token, config, crate::tcp::DEFAULT_LAUNCH_TIMEOUT)
    }

    /// Adopts a connected inherited fd, immediately sets CLOEXEC/nonblocking,
    /// and validates platform socket type/family. Failure closes the adopted fd
    /// and is a launch error, never a DoorbellFault.
    #[cfg(unix)]
    pub fn new(fd: OwnedFd, config: Config) -> io::Result<(Self, Listener)> {
        Self::build(fd, config, None)
    }

    fn build(
        fd: socket::DoorbellSocket,
        config: Config,
        session_token: Option<[u8; 32]>,
    ) -> io::Result<(Self, Listener)> {
        if config.max_active_tools == 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "the active-tool limit must be nonzero",
            ));
        }
        let info = socket::setup(&fd)?;
        let (wake_tx, wake_rx) = socket::wake_pair()?;
        wake_tx.set_nonblocking(true)?;
        wake_rx.set_nonblocking(true)?;
        let terminal_ready = Arc::new(Condvar::new());
        let shared = Arc::new(Shared {
            state: Mutex::new(State {
                socket: Some(fd),
                info,
                input: InputState::new(config.max_active_tools),
                outcomes: VecDeque::new(),
                terminal: None,
                terminal_ready: terminal_ready.clone(),
                exited: false,
                handshake: false,
                session_token,
                receive_window: Vec::new(),
                next_send: 1,
                expected_receive: 1,
                stats: Statistics::default(),
            }),
            wake: wake_tx,
            terminal_ready,
        });
        Ok((
            Self {
                shared: shared.clone(),
            },
            Listener {
                shared,
                wake: wake_rx,
                #[cfg(test)]
                after_empty_outcomes: None,
            },
        ))
    }

    /// Adopt VIZI_PLUGIN_DOORBELL_FD exactly once at process startup.
    ///
    /// # Safety
    /// If the environment value names an open descriptor, it must be exclusively
    /// owned by the caller, inherited from the launcher. No other owner may
    /// close it, replace it, or access its flags during adoption; call this only
    /// once. A closed descriptor is diagnosed before ownership is established.
    /// Environment text cannot prove fd ownership, so this convenience function
    /// cannot be a safe Rust API.
    #[cfg(unix)]
    pub unsafe fn from_env(config: Config) -> io::Result<(Self, Listener)> {
        let text = std::env::var("VIZI_PLUGIN_DOORBELL_FD").map_err(|_| {
            io::Error::new(
                io::ErrorKind::InvalidInput,
                "VIZI_PLUGIN_DOORBELL_FD is required",
            )
        })?;
        if text.is_empty() || !text.bytes().all(|b| b.is_ascii_digit()) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                "invalid doorbell fd",
            ));
        }
        let fd: i32 = text
            .parse()
            .map_err(|_| io::Error::new(io::ErrorKind::InvalidInput, "invalid doorbell fd"))?;
        // SAFETY: exclusive ownership and no concurrent close/replacement are
        // the caller's explicit preconditions; adoption checks liveness.
        Self::new(unsafe { socket::adopt_inherited(fd) }?, config)
    }

    /// The supervisor calls this only after Hello/Ready agrees on compatible 1.x.
    /// A mismatch is returned as a setup error; the supervisor must fail launch.
    pub fn handshake_completed(&self, major: u32, minor: u32) -> io::Result<()> {
        self.finish_handshake(major, minor, None)
    }
    /// Complete Hello/Ready using Hello.session_token. Required on Windows;
    /// the token is compared byte-for-byte, never hex-normalized or logged.
    pub fn handshake_completed_with_token(
        &self,
        major: u32,
        minor: u32,
        token: &str,
    ) -> io::Result<()> {
        self.finish_handshake(major, minor, Some(token))
    }
    fn finish_handshake(&self, major: u32, minor: u32, token: Option<&str>) -> io::Result<()> {
        if major != PROTOCOL_VERSION.0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("doorbell requires protocol 1.0; peer declared {major}.{minor}"),
            ));
        }
        let mut s = self.shared.lock();
        if s.terminal.is_some() || s.exited {
            return Err(io::Error::new(
                io::ErrorKind::NotConnected,
                "doorbell closed",
            ));
        }
        if let Some(expected) = s.session_token {
            if token.is_none() {
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Hello.session_token is required on Windows",
                ));
            }
            if token.map(str::as_bytes) != Some(expected.as_slice()) {
                s.terminate(Terminal::Teardown(TeardownReason::LocalStop));
                drop(s);
                self.shared.wake();
                return Err(io::Error::new(
                    io::ErrorKind::PermissionDenied,
                    "Hello session token mismatch",
                ));
            }
        }
        s.handshake = true;
        Ok(())
    }
    /// Disposable hint: one nonblocking send, no queue, no retry on refusal.
    /// Sent means accepted by the socket, never observed by the receiver.
    pub fn notify(&self, hint: &Notification) -> Result<SendOutcome, EncodeError> {
        // Invalid local values and empty masks never enter writer admission.
        hint.encode(0)?;
        let mut s = self.shared.lock();
        if s.terminal.is_some() || s.exited {
            return Ok(SendOutcome::Closed);
        }
        if !s.handshake {
            return Ok(SendOutcome::HandshakePending);
        }
        if hint.mask.is_empty() {
            s.stats.notifications_suppressed = s.stats.notifications_suppressed.saturating_add(1);
            return Ok(SendOutcome::Suppressed);
        }
        let id = s.next_send;
        s.next_send = s.next_send.wrapping_add(1);
        let bytes = hint.encode(id)?;
        let result = socket::send(s.socket.as_ref().unwrap(), &bytes);
        let out = s.sent(result);
        let terminal = s.terminal.is_some();
        drop(s);
        if terminal {
            self.shared.wake();
        }
        Ok(out)
    }
    fn lifecycle(
        &self,
        apply: impl FnOnce(&mut InputState) -> Result<Vec<Cancellation>, LifecycleError>,
    ) -> Result<(), LifecycleError> {
        let mut s = self.shared.lock();
        if s.terminal.is_some() || s.exited {
            return Err(LifecycleError::Closed);
        }
        // Only cancellations enter from lifecycle calls, and each removes an
        // open interaction. Only the listener creates interactions, and it
        // drains outcomes before receiving again: the queue is structurally
        // bounded by the active-tool limit plus constant framing/terminal slack.
        let changes = apply(&mut s.input)?;
        let wake = !changes.is_empty();
        s.outcomes.extend(
            changes
                .into_iter()
                .map(|c| QueuedOutcome::Event(Outcome::Cancellation(c))),
        );
        drop(s);
        if wake {
            self.shared.wake();
        }
        Ok(())
    }
    pub(crate) fn after_outcomes(
        &self,
        callback: impl FnOnce() + Send + 'static,
    ) -> Result<(), LifecycleError> {
        let mut s = self.shared.lock();
        if s.terminal.is_some() || s.exited || s.outcomes.len() >= 256 {
            return Err(LifecycleError::Closed);
        }
        s.outcomes
            .push_back(QueuedOutcome::Fence(Box::new(callback)));
        drop(s);
        self.shared.wake();
        Ok(())
    }
    pub fn tool_activated(&self, handle: u32) -> Result<(), LifecycleError> {
        self.lifecycle(|i| {
            i.activate(handle)?;
            Ok(Vec::new())
        })
    }
    pub fn tool_retired(&self, handle: u32) -> Result<(), LifecycleError> {
        self.lifecycle(|i| i.retire(handle))
    }
    pub fn epoch_acknowledged(&self, view_id: u8, epoch: u32) -> Result<(), LifecycleError> {
        self.lifecycle(|i| i.epoch(view_id, epoch))
    }
    pub fn cancel_interaction(&self, n: u32) -> Result<(), LifecycleError> {
        self.lifecycle(|i| Ok(i.cancel(n, CancellationReason::Supervisor)))
    }
    pub fn cancel_interactions(&self, view_id: u8) -> Result<(), LifecycleError> {
        self.lifecycle(|i| Ok(i.cancel_view(Some(view_id), CancellationReason::Supervisor)))
    }

    /// Records cancellation, publishes exactly one terminal result, releases
    /// the socket, and wakes the listener. Never joins or waits for a callback.
    /// Call this on an authoritative control-plane teardown notice; datagram
    /// transports do not guarantee a durable socket EOF notification.
    pub fn stop(&self) {
        self.shared
            .lock()
            .terminate(Terminal::Teardown(TeardownReason::LocalStop));
        self.shared.wake();
    }
    /// Dedicated durable fault/teardown mailbox. Publication occurs under the
    /// state lock BEFORE releasing the socket, independently of callback drain.
    /// A supervisor can inspect this even while a consumer callback is blocked.
    /// Send a Fault over gRPC; do not wait for the ordinary listener callback.
    pub fn terminal_report(&self) -> Option<Terminal> {
        self.shared.lock().terminal.clone()
    }
    /// Wait for the dedicated terminal mailbox, independently of listener
    /// callbacks. Suitable for a supervisor/control-plane worker. A timeout
    /// means only that no terminal report is available, never peer death.
    pub fn wait_terminal(&self, timeout: Duration) -> Option<Terminal> {
        let (state, _) = self
            .shared
            .terminal_ready
            .wait_timeout_while(self.shared.lock(), timeout, |s| s.terminal.is_none())
            .unwrap_or_else(|p| p.into_inner());
        state.terminal.clone()
    }
    pub fn statistics(&self) -> Statistics {
        self.shared.lock().stats.clone()
    }
    pub fn socket_info(&self) -> SocketInfo {
        self.shared.lock().info.clone()
    }
}

impl Listener {
    /// Dispatch outcomes on this thread, in state-mutation order. No user code
    /// runs under transport serialization. Keep callbacks below 5 ms as a local
    /// SDK target; longer work delays grants and source-side DRAGs can be lost.
    /// This is a consumer budget, not an enforced runtime deadline. stop() is
    /// independent; joining remains bounded only by the callback.
    pub fn run(mut self, mut emit: impl FnMut(&mut Outcome)) -> Result<(), RunError> {
        loop {
            let outcome = { self.shared.lock().outcomes.pop_front() };
            if let Some(mut outcome) = outcome {
                let terminal = matches!(outcome, QueuedOutcome::Event(Outcome::Terminal(_)));
                if catch_unwind(AssertUnwindSafe(|| match &mut outcome {
                    QueuedOutcome::Event(event) => emit(event),
                    QueuedOutcome::Fence(callback) => {
                        std::mem::replace(callback, Box::new(|| {}))()
                    }
                }))
                .is_err()
                {
                    // Unclaimed descriptors close when outcome drops; claimed
                    // OwnedFds belong exclusively to the consumer.
                    drop(outcome);
                    let mut s = self.shared.lock();
                    s.terminate(Terminal::Teardown(TeardownReason::ConsumerPanicked));
                    s.exited = true;
                    s.outcomes.clear();
                    return Err(RunError::ConsumerPanicked);
                }
                if terminal {
                    self.shared.lock().exited = true;
                    return Ok(());
                }
                continue;
            }
            #[cfg(test)]
            if let Some(gate) = self.after_empty_outcomes.take() {
                gate();
            }
            let fd = {
                let mut s = self.shared.lock();
                // A lifecycle call may have queued outcomes since the empty
                // pop above. Drain them before observing terminal or receiving
                // again; stop() publishes cancellations before its terminal.
                if !s.outcomes.is_empty() {
                    continue;
                }
                if s.terminal.is_some() {
                    s.exited = true;
                    return Ok(());
                }
                let fd = socket::raw_socket(s.socket.as_ref().unwrap());
                // Nonblocking recvmsg and processing share serialization with
                // stop and sends: no raw descriptor is used after release.
                let state = &mut *s;
                match socket::receive(state.socket.as_ref().unwrap(), &mut state.receive_window) {
                    Ok(packet) => {
                        s.received(packet);
                        continue;
                    }
                    Err(e) if socket::would_block(&e) => {}
                    Err(e) if socket::locally_closed(&e) => {
                        s.terminate(Terminal::Teardown(TeardownReason::LocallyClosed {
                            os_error: e.raw_os_error().unwrap(),
                        }));
                        continue;
                    }
                    Err(e) if socket::peer_gone(&e) => {
                        s.peer_closed(e.raw_os_error());
                        continue;
                    }
                    Err(e) => {
                        let fault = s.io_fault(FailureClass::RecvError, &e, None);
                        s.terminate(Terminal::Fault(Arc::new(fault)));
                        continue;
                    }
                }
                fd
            };
            // A stop racing this snapshot closes the socket and writes wake.
            // poll never consumes data; I/O above always rechecks locked state.
            if let Err(e) = socket::poll(fd, socket::raw_wake(&self.wake)) {
                let mut s = self.shared.lock();
                if s.terminal.is_none() {
                    let fault = s.io_fault(FailureClass::RecvError, &e, None);
                    s.terminate(Terminal::Fault(Arc::new(fault)));
                }
            }
            // One bounded read is sufficient: coalesced wakes have no payload.
            // Remaining bytes cause another level-triggered wake, never a stall.
            let mut discard = [0u8; 256];
            match self.wake.read(&mut discard) {
                Ok(_) => {}
                Err(e)
                    if e.kind() == io::ErrorKind::WouldBlock
                        || e.kind() == io::ErrorKind::Interrupted => {}
                Err(_) => {
                    self.shared
                        .lock()
                        .terminate(Terminal::Teardown(TeardownReason::LocalStop));
                }
            }
        }
    }
}
impl Drop for Listener {
    fn drop(&mut self) {
        let mut s = self.shared.lock();
        s.terminate(Terminal::Teardown(TeardownReason::ListenerDropped));
        s.exited = true;
        // No callback can ever consume these now. This also closes any grants.
        s.outcomes.clear();
    }
}

impl State {
    fn peer_closed(&mut self, os_error: Option<i32>) {
        let capture = std::mem::take(&mut self.receive_window);
        self.terminate(Terminal::Teardown(TeardownReason::PeerGone {
            os_error,
            capture,
        }));
    }
    fn terminate(&mut self, terminal: Terminal) {
        if self.terminal.is_some() {
            return;
        }
        // This latch closes admission in both directions and is the fault sink:
        // a single bounded slot, no external callback, no delivery wait, hence
        // no timer thread or abandonment of claimed descriptors is necessary.
        self.terminal = Some(terminal.clone());
        self.terminal_ready.notify_all();
        self.outcomes.extend(
            self.input
                .cancel_view(None, CancellationReason::SessionEnded)
                .into_iter()
                .map(|c| QueuedOutcome::Event(Outcome::Cancellation(c))),
        );
        self.outcomes
            .push_back(QueuedOutcome::Event(Outcome::Terminal(terminal)));
        self.socket.take();
    }
    fn sent(&mut self, result: io::Result<usize>) -> SendOutcome {
        match result {
            Ok(RECORD_LEN) => {
                self.stats.notifications_sent = self.stats.notifications_sent.saturating_add(1);
                SendOutcome::Sent
            }
            Err(e) if socket::refused(&e) => {
                self.stats.notifications_refused =
                    self.stats.notifications_refused.saturating_add(1);
                SendOutcome::Dropped
            }
            Err(e) if socket::locally_closed(&e) => {
                self.terminate(Terminal::Teardown(TeardownReason::LocallyClosed {
                    os_error: e.raw_os_error().unwrap(),
                }));
                SendOutcome::Closed
            }
            Err(e) if socket::peer_gone(&e) => {
                self.peer_closed(e.raw_os_error());
                SendOutcome::Closed
            }
            Ok(n) => {
                let f = self.io_fault(
                    FailureClass::ShortWrite,
                    &io::Error::from_raw_os_error(0),
                    Some(n),
                );
                self.terminate(Terminal::Fault(Arc::new(f)));
                SendOutcome::Closed
            }
            Err(e) => {
                let f = self.io_fault(FailureClass::AmbiguousSend, &e, None);
                self.terminate(Terminal::Fault(Arc::new(f)));
                SendOutcome::Closed
            }
        }
    }
    fn io_fault(&self, class: FailureClass, error: &io::Error, len: Option<usize>) -> Fault {
        let receive = class == FailureClass::RecvError;
        Fault {
            failure_class: class,
            capture: None,
            observed_len: len,
            ordinal: receive.then_some(self.stats.frames_examined),
            msg_ctrunc: None,
            os_error: error.raw_os_error(),
            expected_crc: None,
            observed_crc: None,
            header: None,
            transport_class: TransportClass::current(),
            direction: if receive {
                Direction::HostToPlugin
            } else {
                Direction::PluginToHost
            },
            socket: self.info.clone(),
            detail: if class == FailureClass::ShortWrite {
                format!(
                    "record send accepted {} of {RECORD_LEN} bytes",
                    len.unwrap()
                )
            } else {
                error.to_string()
            },
        }
    }
    fn frame_fault(&self, class: FailureClass, packet: &socket::Packet) -> Fault {
        let crc = (packet.bytes.len() >= RECORD_LEN)
            .then(|| (crc32(&packet.bytes[..108]), u32_at(&packet.bytes, 108)));
        let header = crc.filter(|(a, b)| a == b).map(|_| Header {
            op: packet.bytes[0],
            version: packet.bytes[1],
            flags: packet.bytes[2],
            reserved: packet.bytes[3],
            id: u32_at(&packet.bytes, 4),
        });
        Fault {
            failure_class: class,
            capture: Some(packet.bytes.clone()),
            observed_len: Some(packet.bytes.len()),
            ordinal: Some(self.stats.frames_examined),
            msg_ctrunc: Some(packet.ctrunc),
            os_error: None,
            expected_crc: crc.map(|v| v.0),
            observed_crc: crc.map(|v| v.1),
            header,
            transport_class: TransportClass::current(),
            direction: Direction::HostToPlugin,
            socket: self.info.clone(),
            detail: format!("{class:?}"),
        }
    }
    fn track_id(&mut self, id: u32, diags: &mut Vec<Diagnostic>) {
        let expected = self.expected_receive;
        let delta = id.wrapping_sub(expected);
        if delta < 0x8000_0000 {
            if delta != 0 {
                diags.push(Diagnostic::sequence(
                    DiagnosticKind::IdGap,
                    expected,
                    id,
                    delta,
                ));
            }
            self.expected_receive = id.wrapping_add(1);
        } else {
            diags.push(Diagnostic::sequence(
                DiagnosticKind::IdRegression,
                expected,
                id,
                0,
            ));
        }
    }
    fn publish_frame(&mut self, outcome: FrameOutcome) {
        for d in &outcome.diagnostics {
            let count = self.stats.diagnostic_counts.entry(d.kind).or_default();
            *count = count.saturating_add(1);
            if d.kind == DiagnosticKind::IdGap {
                self.stats.missing_record_ids =
                    self.stats.missing_record_ids.saturating_add(d.value);
            }
            if d.kind == DiagnosticKind::SequenceGap {
                self.stats.missing_samples = self.stats.missing_samples.saturating_add(d.value);
            }
        }
        self.outcomes
            .push_back(QueuedOutcome::Event(Outcome::Frame(outcome)));
    }
    fn received(&mut self, mut packet: socket::Packet) {
        // Ancillary integrity precedes even the zero-byte teardown case.
        let acquisition = if packet.ctrunc {
            Some(FailureClass::Ctrunc)
        } else if packet.descriptors.len() > 1 {
            Some(FailureClass::WrongFdCount)
        } else {
            None
        };
        if acquisition.is_none()
            && packet.descriptors.is_empty()
            && (packet.eof
                || (packet.bytes.is_empty()
                    && TransportClass::current() == TransportClass::Seqpacket))
        {
            self.receive_window = std::mem::take(&mut packet.bytes);
            drop(packet);
            self.peer_closed(None);
            return;
        }
        self.stats.frames_examined = self.stats.frames_examined.saturating_add(1);
        let mut id = None;
        let mut diagnostics = Vec::new();
        let fault = acquisition.or_else(|| {
            let b = &packet.bytes;
            if b.len() != RECORD_LEN {
                return Some(FailureClass::BadLength);
            }
            if crc32(&b[..108]) != u32_at(b, 108) {
                return Some(FailureClass::BadCrc);
            }
            if b[1] != 2 {
                return Some(FailureClass::BadVer);
            }
            if b[2] != 0 {
                return Some(FailureClass::BadFlags);
            }
            if b[3] != 0 {
                return Some(FailureClass::BadReserved);
            }
            let n = u32_at(b, 4);
            id = Some(n);
            self.track_id(n, &mut diagnostics);
            if b[0] == 0xff {
                return Some(FailureClass::ReservedOp);
            }
            let count = match b[0] {
                b'M' => usize::from(TransportClass::current() != TransportClass::Stream),
                b'D' | b'E' => 0,
                _ => return None,
            };
            if packet.descriptors.len() != count {
                return Some(FailureClass::WrongFdCount);
            }
            let p = &b[8..108];
            let padding = match b[0] {
                b'M' => p[17] != 0,
                b'D' => p[77..].iter().any(|b| *b != 0),
                b'E' => p[92..].iter().any(|b| *b != 0),
                _ => false,
            };
            padding.then_some(FailureClass::BadPadding)
        });
        if let Some(class) = fault {
            packet.descriptors.clear();
            let fault = self.frame_fault(class, &packet);
            self.publish_frame(FrameOutcome {
                capture: packet.bytes,
                ordinal: self.stats.frames_examined,
                id,
                diagnostics,
                disposition: Disposition::FramingRejected(class),
            });
            self.terminate(Terminal::Fault(Arc::new(fault)));
            return;
        }
        let raw: RecordBytes = packet.bytes.as_slice().try_into().unwrap();
        let p = &raw[8..108];
        let mut cancellations = Vec::new();
        let disposition = match raw[0] {
            b'M' => {
                #[cfg(unix)]
                if p[18..].iter().any(|b| *b != 0) {
                    diagnostics.push(Diagnostic::notice(DiagnosticKind::UnixNameIgnored, p[18]));
                }
                let segment_id = u64_at(p, 0);
                let size = u64_at(p, 8);
                let mut violations = Vec::new();
                if size < 32 {
                    violations.push(GrantViolation::SizeBelowPreamble);
                }
                if p[16] & 1 == 0 {
                    violations.push(GrantViolation::MissingPluginRead);
                }
                if p[16] & 0xf0 != 0 {
                    violations.push(GrantViolation::ReservedPermissions);
                }
                #[cfg(windows)]
                let name = match grant_name(p) {
                    Ok(name) => Some(name),
                    Err(violation) => {
                        violations.push(violation);
                        None
                    }
                };
                if segment_id == 0 {
                    Disposition::Rejected(Rejection::Contract(Violation::ZeroId))
                } else if !violations.is_empty() {
                    Disposition::Rejected(Rejection::Grant {
                        segment_id,
                        violations,
                    })
                } else {
                    #[cfg(unix)]
                    {
                        Disposition::Decoded(DecodedRecord::Memory {
                            grant: MemoryGrant {
                                segment_id,
                                size,
                                permissions: Permissions::new(p[16]).unwrap(),
                            },
                            descriptor: packet.descriptors.pop(),
                        })
                    }
                    #[cfg(windows)]
                    {
                        Disposition::Decoded(DecodedRecord::NamedMemory {
                            grant: MemoryGrant {
                                segment_id,
                                size,
                                permissions: Permissions::new(p[16]).unwrap(),
                            },
                            name: name.unwrap(),
                        })
                    }
                }
            }
            b'D' => {
                let segment_id = u64_at(p, 0);
                let mask = RegionMask::from_bytes(p[8..72].try_into().unwrap());
                let epoch = u32_at(p, 72);
                if epoch == 0 && p[76] != 0 {
                    diagnostics.push(Diagnostic::notice(DiagnosticKind::ViewWithoutEpoch, p[76]));
                }
                if segment_id == 0 {
                    Disposition::Rejected(Rejection::Contract(Violation::ZeroId))
                } else if mask.is_empty() {
                    diagnostics.push(Diagnostic::notice(DiagnosticKind::EmptyMask, 0u8));
                    Disposition::Ignored(DiagnosticKind::EmptyMask)
                } else {
                    Disposition::Decoded(DecodedRecord::Notification(Notification {
                        segment_id,
                        mask,
                        pane: (epoch != 0).then_some(PaneEpoch {
                            view_id: p[76],
                            epoch,
                        }),
                    }))
                }
            }
            b'E' => match self
                .input
                .process(&raw, &mut diagnostics, &mut cancellations)
            {
                InputDisposition::Accepted {
                    event,
                    implicitly_closed,
                } => Disposition::Decoded(DecodedRecord::Input {
                    event,
                    implicitly_closed,
                }),
                InputDisposition::Ignored(kind) => Disposition::Ignored(kind),
                InputDisposition::Rejected(v) => Disposition::Rejected(Rejection::Contract(v)),
            },
            op => {
                diagnostics.push(Diagnostic::notice(DiagnosticKind::UnknownOpcode, op));
                Disposition::UnknownOpcode(op)
            }
        };
        // Rejected/unknown records must close their resources BEFORE hand-up.
        packet.descriptors.clear();
        self.publish_frame(FrameOutcome {
            capture: packet.bytes,
            ordinal: self.stats.frames_examined,
            id,
            diagnostics,
            disposition,
        });
        self.outcomes.extend(
            cancellations
                .into_iter()
                .map(|c| QueuedOutcome::Event(Outcome::Cancellation(c))),
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    const IO_ERROR: i32 = libc::EIO;
    #[cfg(windows)]
    const IO_ERROR: i32 = windows_sys::Win32::Networking::WinSock::WSAEINVAL;

    fn pair() -> (Doorbell, Listener, socket::DoorbellSocket) {
        #[cfg(unix)]
        let (host, plugin) = socket_pair().unwrap();
        #[cfg(windows)]
        let (host, plugin) = crate::tcp::pair().unwrap();
        let (bell, listener) = Doorbell::build(plugin, Config::default(), None).unwrap();
        (bell, listener, host)
    }

    #[test]
    fn stop_after_empty_queue_check_delivers_cancellation_before_terminal() {
        use std::{sync::mpsc, thread};
        const WAIT: Duration = Duration::from_secs(3);
        let (bell, mut listener, _host) = pair();
        bell.tool_activated(1).unwrap();
        bell.epoch_acknowledged(0, 1).unwrap();
        let press = InputEvent {
            gen: 1,
            t: 0,
            interaction: 1,
            seq: 0,
            epoch: 1,
            target: None,
            tool: 1,
            channels: 3,
            phase: Phase::Press,
            view_id: 0,
            level: 0,
            mods: 0,
            click_count: 0,
            miss: false,
            roll_ref_y: false,
            action: 0,
            position: Position::None,
        };
        // Seed through the normal decoder so there is a real open interaction.
        bell.shared.lock().received(socket::Packet {
            bytes: press.encode(1).unwrap().to_vec(),
            ctrunc: false,
            descriptors: vec![],
            eof: false,
        });
        let (entered, at_gap) = mpsc::channel();
        let (release, resume) = mpsc::channel();
        listener.after_empty_outcomes = Some(Box::new(move || {
            entered.send(()).unwrap();
            resume
                .recv_timeout(WAIT)
                .expect("empty-queue gate was not released");
        }));
        let (tx, events) = mpsc::channel();
        let (done, finished) = mpsc::channel();
        let worker = thread::spawn(move || {
            let result = listener.run(|outcome| {
                let event = std::mem::replace(
                    outcome,
                    Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop)),
                );
                tx.send(event).unwrap();
            });
            done.send(result).unwrap();
        });
        assert!(matches!(
            events
                .recv_timeout(WAIT)
                .expect("press frame was not delivered"),
            Outcome::Frame(FrameOutcome {
                disposition: Disposition::Decoded(DecodedRecord::Input { .. }),
                ..
            })
        ));
        at_gap
            .recv_timeout(WAIT)
            .expect("listener did not reach empty-queue gap");
        // The host socket stays open. Termination publishes both events while
        // the listener is between its two state-lock acquisitions.
        bell.stop();
        assert!(matches!(
            bell.terminal_report(),
            Some(Terminal::Teardown(TeardownReason::LocalStop))
        ));
        release.send(()).unwrap();
        let cancellation = events
            .recv_timeout(WAIT)
            .expect("stop lost the open interaction's cancellation");
        assert!(
            matches!(
                cancellation,
                Outcome::Cancellation(Cancellation {
                    interaction: Interaction {
                        interaction: 1,
                        tool: 1,
                        ..
                    },
                    reason: CancellationReason::SessionEnded,
                })
            ),
            "{cancellation:?}"
        );
        assert!(matches!(
            events
                .recv_timeout(WAIT)
                .expect("stop lost its terminal event"),
            Outcome::Terminal(Terminal::Teardown(TeardownReason::LocalStop))
        ));
        finished
            .recv_timeout(WAIT)
            .expect("listener did not finish")
            .unwrap();
        worker.join().unwrap();
        bell.stop();
        assert!(matches!(
            events.try_recv(),
            Err(mpsc::TryRecvError::Disconnected)
        ));
    }

    #[test]
    fn diagnostic_id_wrap_and_half_range_boundary() {
        let (bell, _listener, _host) = pair();
        let mut s = bell.shared.lock();
        let mut d = Vec::new();
        s.expected_receive = u32::MAX;
        for id in [u32::MAX, 0, 1] {
            s.track_id(id, &mut d);
        }
        assert!(d.is_empty());
        assert_eq!(s.expected_receive, 2);
        s.track_id(0x8000_0002, &mut d);
        assert_eq!(d[0].kind, DiagnosticKind::IdRegression);
        assert_eq!(s.expected_receive, 2);
        s.track_id(4, &mut d);
        assert_eq!(d[1].value, 2);
        assert_eq!(s.expected_receive, 5);
        s.track_id(0, &mut d);
        assert_eq!(s.expected_receive, 5);
    }

    #[test]
    fn synthetic_short_send_is_terminal_and_not_a_receive_ordinal() {
        // Message sockets cannot legitimately produce short sends. Inject the
        // syscall result at the same classifier production sends call.
        for n in [0, 1, 111] {
            let (bell, listener, _host) = pair();
            assert_eq!(bell.shared.lock().sent(Ok(n)), SendOutcome::Closed);
            let Some(Terminal::Fault(f)) = bell.terminal_report() else {
                panic!()
            };
            assert_eq!(f.failure_class, FailureClass::ShortWrite);
            assert_eq!(f.observed_len, Some(n));
            assert_eq!(f.ordinal, None);
            assert_eq!(f.capture, None);
            assert_eq!(f.msg_ctrunc, None);
            assert_eq!(f.os_error, Some(0));
            assert_eq!(f.expected_crc, None);
            bell.stop();
            let mut count = 0;
            listener
                .run(|o| {
                    if matches!(o, Outcome::Terminal(_)) {
                        count += 1;
                    }
                })
                .unwrap();
            assert_eq!(count, 1);
        }
    }

    #[test]
    #[cfg(unix)]
    fn errno_classifier_distinguishes_refusal_close_and_fault() {
        for errno in [libc::EAGAIN, libc::EWOULDBLOCK, libc::ENOBUFS] {
            let (bell, _listener, _host) = pair();
            assert_eq!(
                bell.shared
                    .lock()
                    .sent(Err(io::Error::from_raw_os_error(errno))),
                SendOutcome::Dropped
            );
            assert!(bell.terminal_report().is_none());
        }
        #[cfg(target_os = "linux")]
        assert!(socket::refused(&io::Error::from_raw_os_error(
            libc::ETOOMANYREFS
        )));
        for errno in [
            libc::EPIPE,
            libc::ECONNRESET,
            libc::ECONNREFUSED,
            libc::EDESTADDRREQ,
            libc::ENOTCONN,
        ] {
            let (bell, _listener, _host) = pair();
            bell.shared
                .lock()
                .sent(Err(io::Error::from_raw_os_error(errno)));
            assert!(matches!(
                bell.terminal_report(),
                Some(Terminal::Teardown(_))
            ));
        }
        let (bell, _listener, _host) = pair();
        bell.shared
            .lock()
            .sent(Err(io::Error::from_raw_os_error(libc::EIO)));
        let Some(Terminal::Fault(f)) = bell.terminal_report() else {
            panic!()
        };
        assert_eq!(f.failure_class, FailureClass::AmbiguousSend);
        assert_eq!(f.os_error, Some(libc::EIO));
        assert_eq!(f.observed_len, None);
        assert_eq!(f.ordinal, None);
        assert_eq!(f.direction, Direction::PluginToHost);
    }

    #[test]
    fn receive_error_does_not_invent_capture_or_crc() {
        let (bell, _listener, _host) = pair();
        let mut s = bell.shared.lock();
        s.stats.frames_examined = 4;
        let f = s.io_fault(
            FailureClass::RecvError,
            &io::Error::from_raw_os_error(IO_ERROR),
            None,
        );
        assert_eq!(f.ordinal, Some(4));
        assert_eq!(f.capture, None);
        assert_eq!(f.observed_crc, None);
        assert_eq!(f.observed_len, None);
        assert_eq!(f.msg_ctrunc, None);
        assert_eq!(f.direction, Direction::HostToPlugin);
    }

    #[cfg(unix)]
    #[test]
    fn control_truncation_is_terminal_before_record_validation() {
        // Darwin's production buffer avoids rights truncation on current XNU.
        // Still exercise MSG_CTRUNC explicitly: a future kernel or other
        // ancillary data must never turn a truncated capability into success.
        for len in [0, RECORD_LEN] {
            let (bell, _listener, _host) = pair();
            bell.shared.lock().received(socket::Packet {
                bytes: vec![0; len], // also has invalid length or CRC/header
                ctrunc: true,
                descriptors: Vec::new(),
                eof: false,
            });
            let Some(Terminal::Fault(f)) = bell.terminal_report() else {
                panic!("expected terminal truncation fault")
            };
            assert_eq!(f.failure_class, FailureClass::Ctrunc);
            assert_eq!(f.msg_ctrunc, Some(true));
            assert_eq!(f.observed_len, Some(len));
            assert_eq!(f.ordinal, Some(1));
        }
    }

    #[cfg(windows)]
    #[test]
    fn winsock_refusal_local_shutdown_and_peer_death_are_distinct() {
        use windows_sys::Win32::Networking::WinSock::*;
        for errno in [WSAEWOULDBLOCK, WSAENOBUFS] {
            let (bell, _listener, _host) = pair();
            assert_eq!(
                bell.shared
                    .lock()
                    .sent(Err(io::Error::from_raw_os_error(errno))),
                SendOutcome::Dropped
            );
            assert!(bell.terminal_report().is_none());
        }
        for errno in [
            WSAECONNRESET,
            WSAECONNABORTED,
            WSAECONNREFUSED,
            WSAEDESTADDRREQ,
            WSAENOTCONN,
        ] {
            let (bell, _listener, _host) = pair();
            bell.shared
                .lock()
                .sent(Err(io::Error::from_raw_os_error(errno)));
            assert!(matches!(
                bell.terminal_report(),
                Some(Terminal::Teardown(TeardownReason::PeerGone { .. }))
            ));
        }
        let (bell, _listener, _host) = pair();
        bell.shared
            .lock()
            .sent(Err(io::Error::from_raw_os_error(WSAESHUTDOWN)));
        assert!(matches!(
            bell.terminal_report(),
            Some(Terminal::Teardown(TeardownReason::LocallyClosed {
                os_error: WSAESHUTDOWN
            }))
        ));
        let (bell, _listener, _host) = pair();
        bell.shared
            .lock()
            .sent(Err(io::Error::from_raw_os_error(WSAEINVAL)));
        assert!(
            matches!(bell.terminal_report(),Some(Terminal::Fault(f)) if f.failure_class==FailureClass::AmbiguousSend)
        );
    }

    #[test]
    fn send_counter_wraps_through_zero() {
        let (bell, _listener, host) = pair();
        bell.handshake_completed(1, 0).unwrap();
        bell.shared.lock().next_send = u32::MAX;
        let mut mask = RegionMask::default();
        mask.set(1).unwrap();
        let hint = Notification {
            segment_id: 1,
            mask,
            pane: None,
        };
        for expected in [u32::MAX, 0, 1] {
            assert_eq!(bell.notify(&hint).unwrap(), SendOutcome::Sent);
            let packet = socket::receive(&host, &mut Vec::new()).unwrap();
            assert_eq!(u32_at(&packet.bytes, 4), expected);
        }
    }
}
