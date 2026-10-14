//! All OS/ancillary unsafe code lives here. No mapping or pointer into shared
//! memory is ever created by this crate.
use std::{
    io, mem,
    os::fd::{AsRawFd, FromRawFd, OwnedFd, RawFd},
};

pub type DoorbellSocket = OwnedFd;
pub type Descriptor = OwnedFd;
pub type WakeStream = std::os::unix::net::UnixStream;
pub fn raw_socket(fd: &DoorbellSocket) -> RawFd {
    fd.as_raw_fd()
}
pub fn raw_wake(wake: &WakeStream) -> RawFd {
    wake.as_raw_fd()
}
pub fn wake_pair() -> io::Result<(WakeStream, WakeStream)> {
    WakeStream::pair()
}
pub fn locally_closed(_: &io::Error) -> bool {
    false
}

#[cfg(target_os = "linux")]
pub const SOCKET_TYPE: libc::c_int = libc::SOCK_SEQPACKET;
#[cfg(target_os = "macos")]
pub const SOCKET_TYPE: libc::c_int = libc::SOCK_DGRAM;

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SocketInfo {
    pub socket_type: i32,
    pub receive_buffer: i32,
    pub send_buffer: i32,
    pub nonblocking: bool,
    pub close_on_exec: bool,
}

fn cvt(n: libc::c_int) -> io::Result<libc::c_int> {
    if n < 0 {
        Err(io::Error::last_os_error())
    } else {
        Ok(n)
    }
}

/// Adopt a launch-time descriptor, checking liveness before creating an owner.
///
/// # Safety
/// If open, `fd` must be exclusively owned by the caller. No other thread may
/// close or replace it during adoption.
pub unsafe fn adopt_inherited(fd: RawFd) -> io::Result<OwnedFd> {
    let explain = |error: io::Error| {
        if matches!(error.raw_os_error(), Some(libc::EBADF | libc::ENOTSOCK)) {
            io::Error::new(
                error.kind(),
                format!(
                    "cannot adopt doorbell fd {fd}: {error}; the inherited doorbell is missing or no longer a socket. \
                     A launcher may have closed non-stdio descriptors before starting the plugin. \
                     Exec the plugin or explicitly pass the descriptor named by VIZI_PLUGIN_DOORBELL_FD to the child"
                ),
            )
        } else {
            error
        }
    };
    // SAFETY: F_GETFD inspects an integer descriptor without dereferencing it;
    // EBADF is returned before an invalid descriptor can become an OwnedFd.
    cvt(unsafe { libc::fcntl(fd, libc::F_GETFD) }).map_err(explain)?;
    // SAFETY: liveness was checked above; exclusive ownership and freedom from
    // concurrent close/replacement are the caller's preconditions.
    let owned = unsafe { OwnedFd::from_raw_fd(fd) };
    get_option(&owned, libc::SO_TYPE).map_err(explain)?;
    Ok(owned)
}

pub fn cloexec(fd: &OwnedFd) -> io::Result<()> {
    // SAFETY: borrowed live fd; F_GETFD/F_SETFD take integer arguments only.
    unsafe {
        let flags = cvt(libc::fcntl(fd.as_raw_fd(), libc::F_GETFD))?;
        cvt(libc::fcntl(
            fd.as_raw_fd(),
            libc::F_SETFD,
            flags | libc::FD_CLOEXEC,
        ))?;
    }
    Ok(())
}
fn get_option(fd: &OwnedFd, option: i32) -> io::Result<i32> {
    let mut value: libc::c_int = 0;
    let mut len = mem::size_of_val(&value) as libc::socklen_t;
    // SAFETY: pointer and length describe a writable c_int for getsockopt.
    unsafe {
        cvt(libc::getsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            option,
            (&mut value as *mut libc::c_int).cast(),
            &mut len,
        ))?;
    }
    Ok(value)
}
#[cfg(target_os = "macos")]
fn set_option(fd: &OwnedFd, option: i32, value: i32) -> io::Result<()> {
    // SAFETY: pointer and length describe the live integer value.
    unsafe {
        cvt(libc::setsockopt(
            fd.as_raw_fd(),
            libc::SOL_SOCKET,
            option,
            (&value as *const i32).cast(),
            mem::size_of_val(&value) as libc::socklen_t,
        ))?;
    }
    Ok(())
}
pub fn setup(fd: &OwnedFd) -> io::Result<SocketInfo> {
    cloexec(fd)?;
    // SAFETY: fd is live and flags do not use pointers.
    unsafe {
        let flags = cvt(libc::fcntl(fd.as_raw_fd(), libc::F_GETFL))?;
        cvt(libc::fcntl(
            fd.as_raw_fd(),
            libc::F_SETFL,
            flags | libc::O_NONBLOCK,
        ))?;
    }
    let socket_type = get_option(fd, libc::SO_TYPE)?;
    if socket_type != SOCKET_TYPE {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "doorbell SO_TYPE disagrees with the platform protocol",
        ));
    }
    // Check connection and family without redeclaring the inherited object.
    // SAFETY: zeroed sockaddr_storage is a valid output buffer for getpeername.
    let mut address: libc::sockaddr_storage = unsafe { mem::zeroed() };
    let mut len = mem::size_of_val(&address) as libc::socklen_t;
    // SAFETY: len and address are writable and sized for any socket address.
    unsafe {
        cvt(libc::getpeername(
            fd.as_raw_fd(),
            (&mut address as *mut libc::sockaddr_storage).cast(),
            &mut len,
        ))?;
    }
    if address.ss_family as i32 != libc::AF_UNIX {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "doorbell must be a connected AF_UNIX socket",
        ));
    }
    #[cfg(target_os = "macos")]
    {
        set_option(fd, libc::SO_NOSIGPIPE, 1)?;
        if get_option(fd, libc::SO_RCVBUF)? < 65536 {
            set_option(fd, libc::SO_RCVBUF, 65536)?;
        }
    }
    Ok(SocketInfo {
        socket_type,
        receive_buffer: get_option(fd, libc::SO_RCVBUF)?,
        send_buffer: get_option(fd, libc::SO_SNDBUF)?,
        nonblocking: true,
        close_on_exec: true,
    })
}

pub struct Packet {
    pub bytes: Vec<u8>,
    pub ctrunc: bool,
    pub descriptors: Vec<OwnedFd>,
    pub eof: bool,
}
pub fn receive(fd: &OwnedFd, _partial: &mut [u8]) -> io::Result<Packet> {
    let mut bytes = [0u8; 120];
    // usize alignment is at least cmsghdr alignment on Linux and Darwin.
    #[cfg(target_os = "linux")]
    let mut control = [0usize; 16];
    // Darwin installs all rights before copying control data out. A short
    // buffer leaks the fds whose numbers are omitted. Its sockargs path caps
    // incoming control allocation at MCLBYTES (2048); leave headroom here too.
    // This is receive capacity, NOT permission to accept more than one fd.
    #[cfg(target_os = "macos")]
    let mut control = [0usize; 4096 / mem::size_of::<usize>()];
    let mut iov = libc::iovec {
        iov_base: bytes.as_mut_ptr().cast(),
        iov_len: bytes.len(),
    };
    // SAFETY: zero is valid for optional msghdr pointers/counts.
    let mut msg: libc::msghdr = unsafe { mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = control.as_mut_ptr().cast();
    #[cfg(target_os = "linux")]
    {
        // SAFETY: CMSG_SPACE is pure size arithmetic on a small constant.
        msg.msg_controllen = unsafe { libc::CMSG_SPACE((2 * mem::size_of::<RawFd>()) as u32) } as _;
    }
    #[cfg(target_os = "macos")]
    {
        msg.msg_controllen = mem::size_of_val(&control) as _;
    }
    #[cfg(target_os = "linux")]
    let flags = libc::MSG_CMSG_CLOEXEC;
    #[cfg(target_os = "macos")]
    let flags = 0;
    let n = loop {
        // SAFETY: every msghdr pointer refers to a live writable buffer above.
        let n = unsafe { libc::recvmsg(fd.as_raw_fd(), &mut msg, flags) };
        if n >= 0 {
            break n as usize;
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    };
    // SAFETY: recvmsg returned native headers and newly owned SCM_RIGHTS fds in
    // the aligned control buffer. msg_controllen bounds the returned bytes.
    let descriptors = unsafe { received_rights(&msg) };
    // Linux already did this atomically. The redundant assertion also checks
    // failures explicitly; on Darwin this is the non-atomic fallback.
    for descriptor in &descriptors {
        cloexec(descriptor)?;
    }
    Ok(Packet {
        bytes: bytes[..n].to_vec(),
        ctrunc: msg.msg_flags & libc::MSG_CTRUNC != 0,
        descriptors,
        eof: false,
    })
}

/// Adopt only complete descriptors within recvmsg's returned control length.
///
/// SAFETY: msg must describe a live, aligned, kernel-produced control buffer;
/// its reported length must fit that buffer. Complete SCM_RIGHTS entries must
/// be distinct, newly owned descriptors that have not already been adopted.
unsafe fn received_rights(msg: &libc::msghdr) -> Vec<OwnedFd> {
    // The native field is size_t on Linux and socklen_t (u32) on Darwin.
    #[cfg(target_os = "linux")]
    let control_len = msg.msg_controllen;
    #[cfg(target_os = "macos")]
    let control_len = msg.msg_controllen as usize;
    let mut descriptors = Vec::new();
    // SAFETY: the caller supplies native headers and ownership of their rights.
    // Count is bounded by both the header and the actual returned byte extent,
    // including for a truncated final header. No partial fd is ever read.
    unsafe {
        let mut cmsg = libc::CMSG_FIRSTHDR(msg);
        while !cmsg.is_null() {
            let header_len = libc::CMSG_LEN(0) as usize;
            #[cfg(target_os = "linux")]
            let declared_len = (*cmsg).cmsg_len;
            #[cfg(target_os = "macos")]
            let declared_len = (*cmsg).cmsg_len as usize;
            if declared_len < header_len {
                break;
            }
            let data = libc::CMSG_DATA(cmsg);
            let data_offset = data as usize - msg.msg_control as usize;
            let available = control_len.saturating_sub(data_offset);
            let declared = declared_len - header_len;
            if (*cmsg).cmsg_level == libc::SOL_SOCKET && (*cmsg).cmsg_type == libc::SCM_RIGHTS {
                // Darwin may retain the original cmsg_len after truncation.
                // Reading its claim would adopt unrelated bytes as owned fds.
                let count = declared.min(available) / mem::size_of::<RawFd>();
                let data = data.cast::<RawFd>();
                for i in 0..count {
                    descriptors.push(OwnedFd::from_raw_fd(data.add(i).read_unaligned()));
                }
            }
            if declared > available {
                break;
            }
            cmsg = libc::CMSG_NXTHDR(msg, cmsg);
        }
    }
    descriptors
}

fn send_flags() -> i32 {
    #[cfg(target_os = "linux")]
    {
        libc::MSG_DONTWAIT | libc::MSG_NOSIGNAL
    }
    #[cfg(target_os = "macos")]
    {
        libc::MSG_DONTWAIT
    }
}
pub fn send(fd: &OwnedFd, bytes: &[u8]) -> io::Result<usize> {
    loop {
        // SAFETY: fd and the complete byte buffer remain live for this call.
        let n = unsafe {
            libc::send(
                fd.as_raw_fd(),
                bytes.as_ptr().cast(),
                bytes.len(),
                send_flags(),
            )
        };
        if n >= 0 {
            return Ok(n as usize);
        }
        let e = io::Error::last_os_error();
        if e.kind() != io::ErrorKind::Interrupted {
            return Err(e);
        }
    }
}
pub fn peer_gone(error: &io::Error) -> bool {
    error.raw_os_error().is_some_and(|n| {
        [
            libc::EPIPE,
            libc::ECONNREFUSED,
            libc::ECONNRESET,
            libc::EDESTADDRREQ,
            libc::ENOTCONN,
        ]
        .contains(&n)
    })
}
pub fn would_block(error: &io::Error) -> bool {
    error
        .raw_os_error()
        .is_some_and(|n| n == libc::EAGAIN || n == libc::EWOULDBLOCK)
}
pub fn refused(error: &io::Error) -> bool {
    if would_block(error) || error.raw_os_error() == Some(libc::ENOBUFS) {
        return true;
    }
    #[cfg(target_os = "linux")]
    if error.raw_os_error() == Some(libc::ETOOMANYREFS) {
        return true;
    }
    false
}

/// Level-triggered poll. The doorbell number is only used for readiness; the
/// caller rechecks the owned socket under its state lock before any I/O.
pub fn poll(doorbell: RawFd, wake: RawFd) -> io::Result<()> {
    let mut fds = [
        libc::pollfd {
            fd: doorbell,
            events: libc::POLLIN,
            revents: 0,
        },
        libc::pollfd {
            fd: wake,
            events: libc::POLLIN,
            revents: 0,
        },
    ];
    loop {
        // SAFETY: the two-element pollfd array is live and writable.
        let n = unsafe { libc::poll(fds.as_mut_ptr(), 2, -1) };
        if n >= 0 {
            return Ok(());
        }
        let error = io::Error::last_os_error();
        if error.kind() != io::ErrorKind::Interrupted {
            return Err(error);
        }
    }
}

/// Creates the production platform's socket type, useful to independent peers.
pub fn socket_pair() -> io::Result<(OwnedFd, OwnedFd)> {
    let mut fds = [-1; 2];
    #[cfg(target_os = "linux")]
    let kind = SOCKET_TYPE | libc::SOCK_CLOEXEC;
    #[cfg(target_os = "macos")]
    let kind = SOCKET_TYPE;
    // SAFETY: fds has room for both descriptors socketpair initializes.
    unsafe {
        cvt(libc::socketpair(libc::AF_UNIX, kind, 0, fds.as_mut_ptr()))?;
    }
    // SAFETY: successful socketpair returned two distinct new owned fds.
    let pair = unsafe { (OwnedFd::from_raw_fd(fds[0]), OwnedFd::from_raw_fd(fds[1])) };
    cloexec(&pair.0)?;
    cloexec(&pair.1)?;
    Ok(pair)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{fs::File, os::fd::IntoRawFd};

    #[test]
    fn truncated_rights_never_adopt_bytes_outside_returned_extent() {
        check_rights_extents(false);
    }

    #[test]
    fn later_rights_header_uses_remaining_control_bytes() {
        check_rights_extents(true);
    }

    fn check_rights_extents(preceding_header: bool) {
        let fd_size = mem::size_of::<RawFd>();
        // SAFETY: CMSG helpers perform size arithmetic on small constants.
        let header_len = unsafe { libc::CMSG_LEN(0) as usize };
        let prefix = if preceding_header {
            unsafe { libc::CMSG_SPACE(fd_size as u32) as usize }
        } else {
            0
        };
        let source = File::open("/dev/null").unwrap();
        // Exercise missing headers, every partial fd boundary, and bytes beyond
        // the declared payload. A fifth fd acts as a canary in trailing storage.
        for returned_len in 0..=header_len + 5 * fd_size {
            let mut control = [0usize; 32];
            // SAFETY: zero initializes optional msghdr fields.
            let mut msg: libc::msghdr = unsafe { mem::zeroed() };
            msg.msg_control = control.as_mut_ptr().cast();
            msg.msg_controllen = (prefix + returned_len) as _;
            msg.msg_flags = libc::MSG_CTRUNC;
            let raw: Vec<_> = (0..5)
                .map(|_| source.try_clone().unwrap().into_raw_fd())
                .collect();
            // SAFETY: the buffer has alignment/space for both native headers
            // and all five fd values. The prefix contains no owned rights.
            unsafe {
                if preceding_header {
                    let first = msg.msg_control.cast::<libc::cmsghdr>();
                    (*first).cmsg_len = libc::CMSG_LEN(fd_size as u32) as _;
                    (*first).cmsg_level = 0;
                    (*first).cmsg_type = 0;
                }
                let rights = msg
                    .msg_control
                    .cast::<u8>()
                    .add(prefix)
                    .cast::<libc::cmsghdr>();
                (*rights).cmsg_len = libc::CMSG_LEN((4 * fd_size) as u32) as _;
                (*rights).cmsg_level = libc::SOL_SOCKET;
                (*rights).cmsg_type = libc::SCM_RIGHTS;
                let data = libc::CMSG_DATA(rights).cast::<RawFd>();
                for (i, fd) in raw.iter().enumerate() {
                    data.add(i).write_unaligned(*fd);
                }
            }
            // SAFETY: the fixture represents native control headers with newly
            // owned, distinct fds. Bytes outside msg_controllen are initialized
            // too, so a regression can fail an assertion without closing a
            // random descriptor or creating duplicate ownership.
            let received = unsafe { received_rights(&msg) };
            let adopted: Vec<_> = received.iter().map(AsRawFd::as_raw_fd).collect();
            let _remaining: Vec<_> = raw
                .iter()
                .filter(|fd| !adopted.contains(fd))
                // SAFETY: these unique fixture fds were not adopted by parsing.
                .map(|fd| unsafe { OwnedFd::from_raw_fd(*fd) })
                .collect();
            let expected = (returned_len.saturating_sub(header_len) / fd_size).min(4);
            assert_eq!(adopted, raw[..expected], "returned_len={returned_len}");
        }
    }
}
