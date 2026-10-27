//! Portable TCP mechanics used by Windows and exercised on every test platform.
use crate::RECORD_LEN;
use std::{
    io::{self, Read, Write},
    net::{Ipv4Addr, SocketAddrV4, TcpListener, TcpStream},
    time::{Duration, Instant},
};

pub const DEFAULT_LAUNCH_TIMEOUT: Duration = Duration::from_secs(5);

pub fn token(text: &str) -> io::Result<[u8; 32]> {
    if text.len() != 32 || !text.bytes().all(|b| b.is_ascii_hexdigit()) {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "doorbell token must contain 32 ASCII hex characters",
        ));
    }
    Ok(text.as_bytes().try_into().unwrap())
}
fn deadline(timeout: Duration) -> io::Result<Instant> {
    if timeout.is_zero() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "launch timeout must be positive",
        ));
    }
    Instant::now()
        .checked_add(timeout)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "launch timeout is too large"))
}
fn remaining(deadline: Instant) -> io::Result<Duration> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or_else(|| io::Error::new(io::ErrorKind::TimedOut, "doorbell launch deadline expired"))
}
fn write_token(
    bytes: &[u8; 32],
    deadline: Instant,
    mut send: impl FnMut(&[u8], Duration) -> io::Result<usize>,
) -> io::Result<()> {
    let mut written = 0;
    while written < bytes.len() {
        let budget = remaining(deadline)?;
        match send(&bytes[written..], budget) {
            Ok(0) => {
                return Err(io::Error::new(
                    io::ErrorKind::WriteZero,
                    "doorbell closed during authentication",
                ))
            }
            Ok(n) => written += n,
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
    // Also bound the final write, including a scheduler delay on return.
    remaining(deadline)?;
    Ok(())
}

/// A single total deadline covers connect and the complete token, not each byte.
/// The host-to-plugin direction begins with records: there is no token echo here.
pub fn connect(address: &str, token: &[u8; 32], timeout: Duration) -> io::Result<TcpStream> {
    let address: SocketAddrV4 = address.parse().map_err(|_| {
        io::Error::new(
            io::ErrorKind::InvalidInput,
            "doorbell address must be 127.0.0.1:<port>",
        )
    })?;
    if *address.ip() != Ipv4Addr::LOCALHOST || address.port() == 0 {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            "doorbell address must be 127.0.0.1 with a nonzero port",
        ));
    }
    let deadline = deadline(timeout)?;
    let mut stream = TcpStream::connect_timeout(&address.into(), remaining(deadline)?)?;
    stream.set_nodelay(true)?;
    write_token(token, deadline, |bytes, budget| {
        stream.set_write_timeout(Some(budget))?;
        stream.write(bytes)
    })?;
    // Record writes must never inherit the token's blocking send timeout.
    stream.set_write_timeout(None)?;
    stream.set_nonblocking(true)?;
    Ok(stream)
}

/// Socket-based wake endpoint, because Windows polling cannot wait on a pipe.
pub fn pair() -> io::Result<(TcpStream, TcpStream)> {
    let end = deadline(DEFAULT_LAUNCH_TIMEOUT)?;
    let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0))?;
    listener.set_nonblocking(true)?;
    let writer = TcpStream::connect_timeout(&listener.local_addr()?, remaining(end)?)?;
    let writer_address = writer.local_addr()?;
    let reader = loop {
        remaining(end)?;
        match listener.accept() {
            Ok((stream, address)) if address == writer_address => break stream,
            Ok(_) => {}
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                std::thread::sleep(Duration::from_millis(1))
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    };
    writer.set_nodelay(true)?;
    reader.set_nodelay(true)?;
    Ok((writer, reader))
}

pub enum ReadResult {
    Frame(Vec<u8>),
    Eof(Vec<u8>),
}

/// Retains at most 111 bytes across would-blocks. Each read asks only for the
/// remaining window, so coalesced later records remain in the kernel queue.
pub fn receive(mut reader: impl Read, partial: &mut Vec<u8>) -> io::Result<ReadResult> {
    assert!(partial.len() < RECORD_LEN);
    let mut buffer = [0u8; RECORD_LEN];
    loop {
        match reader.read(&mut buffer[..RECORD_LEN - partial.len()]) {
            Ok(0) => return Ok(ReadResult::Eof(std::mem::take(partial))),
            Ok(n) => {
                partial.extend_from_slice(&buffer[..n]);
                if partial.len() == RECORD_LEN {
                    return Ok(ReadResult::Frame(std::mem::take(partial)));
                }
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            Err(e) => return Err(e),
        }
    }
}

#[cfg(test)]
#[path = "../tests/support/mod.rs"]
mod test_io;

#[cfg(test)]
mod tests {
    use super::test_io::{wait_until, WaitingIo};
    use super::*;
    use std::{collections::VecDeque, thread};
    const TOKEN: &str = "0123456789abcdefABCDEF0123456789";

    #[test]
    fn token_validation_and_bounded_partial_writes() {
        let t = token(TOKEN).unwrap();
        for s in [
            "",
            "0123456789abcdef",
            "x123456789abcdefABCDEF0123456789",
            "é123456789abcdefABCDEF012345678",
        ] {
            assert!(token(s).is_err());
        }
        let mut result = Vec::new();
        let mut calls = 0;
        write_token(&t, deadline(Duration::from_secs(1)).unwrap(), |bytes, _| {
            calls += 1;
            if calls == 1 {
                return Err(io::ErrorKind::Interrupted.into());
            }
            let n = bytes.len().min(3);
            result.extend_from_slice(&bytes[..n]);
            Ok(n)
        })
        .unwrap();
        assert_eq!(result, t);
        assert_eq!(
            write_token(&t, Instant::now(), |_, _| panic!(
                "expired deadline must not write"
            ))
            .unwrap_err()
            .kind(),
            io::ErrorKind::TimedOut
        );
        assert_eq!(
            write_token(&t, deadline(Duration::from_secs(1)).unwrap(), |_, _| Ok(0))
                .unwrap_err()
                .kind(),
            io::ErrorKind::WriteZero
        );
        assert_eq!(
            write_token(&t, deadline(Duration::from_secs(1)).unwrap(), |_, _| Err(
                io::ErrorKind::BrokenPipe.into()
            ))
            .unwrap_err()
            .kind(),
            io::ErrorKind::BrokenPipe
        );
        let end = deadline(Duration::from_millis(10)).unwrap();
        assert_eq!(
            write_token(&t, end, |_, _| {
                thread::sleep(Duration::from_millis(20));
                Ok(1)
            })
            .unwrap_err()
            .kind(),
            io::ErrorKind::TimedOut
        );
    }

    #[test]
    fn actual_connect_writes_only_token_and_clears_timeout() {
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap().to_string();
        let server = thread::spawn(move || {
            let end = Instant::now() + Duration::from_secs(2);
            let (stream, _) = wait_until(end, || listener.accept()).unwrap();
            stream.set_nonblocking(true).unwrap();
            let mut stream = WaitingIo::new(stream, end);
            let mut b = [0u8; 32];
            stream.read_exact(&mut b).unwrap();
            assert_eq!(&b, TOKEN.as_bytes());
            stream.write_all(&[b'D'; RECORD_LEN]).unwrap();
        });
        let stream = connect(&address, &token(TOKEN).unwrap(), Duration::from_secs(2)).unwrap();
        assert!(stream.nodelay().unwrap());
        assert_eq!(stream.write_timeout().unwrap(), None);
        server.join().unwrap();
        let end = Instant::now() + Duration::from_secs(2);
        let mut partial = Vec::new();
        let b = loop {
            match receive(&stream, &mut partial) {
                Ok(ReadResult::Frame(b)) => break b,
                Err(e) if e.kind() == io::ErrorKind::WouldBlock && Instant::now() < end => {
                    thread::sleep(Duration::from_millis(1))
                }
                _ => panic!("server did not deliver its frame"),
            }
        };
        assert_eq!(b, [b'D'; RECORD_LEN]);
        for addr in ["localhost:123", "10.0.0.1:123", "127.0.0.1:0", "[::1]:123"] {
            assert!(connect(addr, &token(TOKEN).unwrap(), Duration::from_secs(1)).is_err());
        }
        assert!(connect(&address, &token(TOKEN).unwrap(), Duration::ZERO).is_err());
    }

    #[test]
    fn wake_pair_transfers_bytes() {
        let (tx, mut rx) = pair().unwrap();
        tx.set_nonblocking(true).unwrap();
        rx.set_nonblocking(true).unwrap();
        assert_eq!(
            rx.read(&mut [0]).unwrap_err().kind(),
            io::ErrorKind::WouldBlock
        );
        let end = Instant::now() + Duration::from_secs(1);
        let mut tx = WaitingIo::new(tx, end);
        let mut rx = WaitingIo::new(rx, end);
        tx.write_all(b"w").unwrap();
        let mut b = [0];
        rx.read_exact(&mut b).unwrap();
        assert_eq!(b, [b'w']);
    }

    struct Chunked {
        data: VecDeque<u8>,
        chunk: usize,
        blocked: bool,
    }
    impl Read for Chunked {
        fn read(&mut self, buf: &mut [u8]) -> io::Result<usize> {
            if self.blocked {
                self.blocked = false;
                return Err(io::ErrorKind::WouldBlock.into());
            }
            let n = buf.len().min(self.chunk).min(self.data.len());
            for byte in &mut buf[..n] {
                *byte = self.data.pop_front().unwrap();
            }
            self.blocked = n != 0;
            Ok(n)
        }
    }
    #[test]
    fn every_chunk_size_and_would_block_preserve_record_boundaries() {
        for chunk in 1..=224 {
            let data: Vec<_> = (0..224).map(|n| n as u8).collect();
            let mut reader = Chunked {
                data: data.clone().into(),
                chunk,
                blocked: false,
            };
            let mut partial = Vec::new();
            for expected in data.as_chunks::<RECORD_LEN>().0 {
                loop {
                    match receive(&mut reader, &mut partial) {
                        Ok(ReadResult::Frame(b)) => {
                            assert_eq!(b, expected);
                            break;
                        }
                        Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                            assert!(partial.len() < 112)
                        }
                        _ => panic!("lost framing"),
                    }
                }
            }
        }
    }
    #[test]
    fn partial_eof_and_interruption_do_not_invent_a_frame() {
        let mut partial = Vec::new();
        let ReadResult::Eof(bytes) = receive(&b"partial"[..], &mut partial).unwrap() else {
            panic!()
        };
        assert_eq!(bytes, b"partial");
        assert!(partial.is_empty());
        struct Interrupted(bool);
        impl Read for Interrupted {
            fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
                if !self.0 {
                    self.0 = true;
                    Err(io::ErrorKind::Interrupted.into())
                } else {
                    b.fill(9);
                    Ok(b.len())
                }
            }
        }
        assert!(matches!(
            receive(Interrupted(false), &mut partial),
            Ok(ReadResult::Frame(_))
        ));
    }
}
