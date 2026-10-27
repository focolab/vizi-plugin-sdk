//! Test-only waiting adapter. The underlying socket remains nonblocking.
use std::{
    io::{self, Read, Write},
    thread,
    time::{Duration, Instant},
};

pub fn wait_until<T>(end: Instant, mut operation: impl FnMut() -> io::Result<T>) -> io::Result<T> {
    loop {
        if Instant::now() >= end {
            return Err(io::Error::new(
                io::ErrorKind::TimedOut,
                "test I/O deadline expired",
            ));
        }
        match operation() {
            Err(e) if e.kind() == io::ErrorKind::WouldBlock => {
                thread::sleep(
                    end.saturating_duration_since(Instant::now())
                        .min(Duration::from_millis(1)),
                );
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => {}
            result => return result,
        }
    }
}

/// One deadline covers all calls, including partial reads/writes in read_exact
/// and write_all. This adapter never changes a socket's flags or timeouts.
pub struct WaitingIo<T> {
    inner: T,
    end: Instant,
}
impl<T> WaitingIo<T> {
    pub fn new(inner: T, end: Instant) -> Self {
        Self { inner, end }
    }
}
impl<T: Read> Read for WaitingIo<T> {
    fn read(&mut self, bytes: &mut [u8]) -> io::Result<usize> {
        wait_until(self.end, || self.inner.read(bytes))
    }
}
impl<T: Write> Write for WaitingIo<T> {
    fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
        wait_until(self.end, || self.inner.write(bytes))
    }
    fn flush(&mut self) -> io::Result<()> {
        wait_until(self.end, || self.inner.flush())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn waiting_adapter_preserves_partial_reads_across_would_block() {
        struct Fragmented(usize);
        impl Read for Fragmented {
            fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
                self.0 += 1;
                match self.0 {
                    1 | 4 => Err(io::ErrorKind::WouldBlock.into()),
                    3 => Err(io::ErrorKind::Interrupted.into()),
                    2 => {
                        b[..2].copy_from_slice(b"ab");
                        Ok(2)
                    }
                    5 => {
                        b[..2].copy_from_slice(b"cd");
                        Ok(2)
                    }
                    _ => Ok(0),
                }
            }
        }
        let mut reader = WaitingIo::new(Fragmented(0), Instant::now() + Duration::from_secs(1));
        let mut bytes = [0; 4];
        reader.read_exact(&mut bytes).unwrap();
        assert_eq!(&bytes, b"abcd");
        assert_eq!(
            reader.read_exact(&mut bytes).unwrap_err().kind(),
            io::ErrorKind::UnexpectedEof
        );
    }

    #[test]
    fn expired_deadline_does_not_attempt_io() {
        let error =
            wait_until::<()>(Instant::now(), || panic!("must not attempt I/O")).unwrap_err();
        assert_eq!(error.kind(), io::ErrorKind::TimedOut);
    }
}
