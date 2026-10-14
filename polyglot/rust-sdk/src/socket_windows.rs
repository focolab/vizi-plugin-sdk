//! Windows ABI calls only; stream framing/authentication lives in tcp.rs.
use std::{
    io::{self, Write},
    mem,
    net::TcpStream,
    os::windows::io::{AsRawSocket, RawSocket},
};
use windows_sys::Win32::{
    Foundation::{SetHandleInformation, HANDLE, HANDLE_FLAG_INHERIT},
    Networking::WinSock::*,
};

pub type DoorbellSocket = TcpStream;
pub type WakeStream = TcpStream;
/// Windows M records carry a name, never an ancillary resource. An uninhabited
/// type makes a claimed Windows descriptor unrepresentable.
#[derive(Debug)]
pub enum Descriptor {}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SocketInfo {
    pub socket_type: i32,
    pub receive_buffer: i32,
    pub send_buffer: i32,
    pub nonblocking: bool,
    /// On Windows this asserts non-inheritance by CreateProcess.
    pub close_on_exec: bool,
}
pub struct Packet {
    pub bytes: Vec<u8>,
    pub ctrunc: bool,
    pub descriptors: Vec<Descriptor>,
    pub eof: bool,
}
pub fn raw_socket(socket: &TcpStream) -> RawSocket {
    socket.as_raw_socket()
}
pub fn raw_wake(socket: &TcpStream) -> RawSocket {
    socket.as_raw_socket()
}
fn last_error() -> io::Error {
    // SAFETY: WSAGetLastError has no arguments or preconditions.
    io::Error::from_raw_os_error(unsafe { WSAGetLastError() })
}
fn non_inheritable(stream: &TcpStream) -> io::Result<()> {
    // SAFETY: the live Winsock socket is also a kernel handle; only its
    // inheritance flag changes. TcpStream remains its sole owner.
    if unsafe { SetHandleInformation(stream.as_raw_socket() as HANDLE, HANDLE_FLAG_INHERIT, 0) }
        == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}
fn get_option(stream: &TcpStream, option: i32) -> io::Result<i32> {
    let mut value = 0i32;
    let mut size = mem::size_of_val(&value) as i32;
    // SAFETY: the option buffer is a writable, correctly sized integer.
    if unsafe {
        getsockopt(
            stream.as_raw_socket() as SOCKET,
            SOL_SOCKET,
            option,
            (&mut value as *mut i32).cast(),
            &mut size,
        )
    } == SOCKET_ERROR
    {
        return Err(last_error());
    }
    Ok(value)
}
pub fn setup(stream: &TcpStream) -> io::Result<SocketInfo> {
    non_inheritable(stream)?;
    let socket_type = get_option(stream, SO_TYPE)?;
    if socket_type != SOCK_STREAM || !stream.peer_addr()?.ip().is_loopback() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "Windows doorbell must be a connected loopback TCP stream",
        ));
    }
    stream.set_nodelay(true)?;
    stream.set_read_timeout(None)?;
    stream.set_write_timeout(None)?;
    stream.set_nonblocking(true)?;
    Ok(SocketInfo {
        socket_type,
        receive_buffer: get_option(stream, SO_RCVBUF)?,
        send_buffer: get_option(stream, SO_SNDBUF)?,
        nonblocking: true,
        close_on_exec: true,
    })
}
pub fn wake_pair() -> io::Result<(TcpStream, TcpStream)> {
    let pair = crate::tcp::pair()?;
    non_inheritable(&pair.0)?;
    non_inheritable(&pair.1)?;
    Ok(pair)
}
pub fn receive(stream: &TcpStream, partial: &mut Vec<u8>) -> io::Result<Packet> {
    let (bytes, eof) = match crate::tcp::receive(stream, partial)? {
        crate::tcp::ReadResult::Frame(b) => (b, false),
        crate::tcp::ReadResult::Eof(b) => (b, true),
    };
    Ok(Packet {
        bytes,
        eof,
        ctrunc: false,
        descriptors: Vec::new(),
    })
}
pub fn send(mut stream: &TcpStream, bytes: &[u8]) -> io::Result<usize> {
    loop {
        match stream.write(bytes) {
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            result => return result, // a short record write is NEVER continued
        }
    }
}
pub fn would_block(error: &io::Error) -> bool {
    error.raw_os_error() == Some(WSAEWOULDBLOCK)
}
pub fn refused(error: &io::Error) -> bool {
    would_block(error) || error.raw_os_error() == Some(WSAENOBUFS)
}
pub fn locally_closed(error: &io::Error) -> bool {
    error.raw_os_error() == Some(WSAESHUTDOWN)
}
pub fn peer_gone(error: &io::Error) -> bool {
    error.raw_os_error().is_some_and(|e| {
        [
            WSAECONNRESET,
            WSAECONNABORTED,
            WSAECONNREFUSED,
            WSAEDESTADDRREQ,
            WSAENOTCONN,
        ]
        .contains(&e)
    })
}
pub fn poll(socket: RawSocket, wake: RawSocket) -> io::Result<()> {
    let mut fds = [
        WSAPOLLFD {
            fd: socket as SOCKET,
            events: POLLIN,
            revents: 0,
        },
        WSAPOLLFD {
            fd: wake as SOCKET,
            events: POLLIN,
            revents: 0,
        },
    ];
    loop {
        // SAFETY: both pollfd entries are live and writable; readiness does not
        // consume bytes. The transport rechecks ownership before subsequent I/O.
        if unsafe { WSAPoll(fds.as_mut_ptr(), 2, -1) } != SOCKET_ERROR {
            return Ok(());
        }
        let e = last_error();
        if e.raw_os_error() != Some(WSAEINTR) {
            return Err(e);
        }
    }
}
