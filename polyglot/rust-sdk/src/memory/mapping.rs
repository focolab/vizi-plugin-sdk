//! The only raw mapping boundary. Payloads never escape as borrowed slices.
use super::{Failure, Result};
use crate::MemoryGrant;
use std::{ptr::NonNull, sync::atomic::AtomicU64};

#[cfg(test)]
thread_local! {
    pub(super) static BEFORE_COPY: std::cell::RefCell<Option<Box<dyn FnOnce()>>> = const { std::cell::RefCell::new(None) };
}
#[cfg(test)]
fn before_copy() {
    let hook = BEFORE_COPY.with(|h| h.borrow_mut().take());
    if let Some(hook) = hook {
        hook();
    }
}

pub(super) struct Mapping {
    ptr: NonNull<u8>,
    len: usize,
    writable: bool,
    #[cfg(windows)]
    handle: windows_sys::Win32::Foundation::HANDLE,
}
// All access is serialized by Memory's mutex; cross-process access follows
// the installed discipline. No pointers or references escape that lock.
unsafe impl Send for Mapping {}
impl Mapping {
    #[cfg(unix)]
    pub(super) fn open(grant: &MemoryGrant, fd: std::os::fd::OwnedFd) -> Result<Self> {
        use std::os::fd::AsRawFd;
        let file = std::fs::File::from(fd);
        let metadata = file.metadata().map_err(Failure::os)?;
        if !metadata.is_file() || metadata.len() != grant.size {
            return Err(Failure::malformed(
                "mapped object's size or type disagrees with grant",
            ));
        }
        #[cfg(target_os = "linux")]
        {
            // Seals are host-side containment, not a receiver admission
            // condition (§5.5). Ordinary file backing may not support them.
            let seals = unsafe { libc::fcntl(file.as_raw_fd(), libc::F_GET_SEALS) };
            if seals < 0
                || seals & (libc::F_SEAL_SHRINK | libc::F_SEAL_GROW)
                    != (libc::F_SEAL_SHRINK | libc::F_SEAL_GROW)
            {
                eprintln!(
                    "vizi SDK: Linux grant {} lacks shrink/grow seals; continuing installation",
                    grant.segment_id,
                );
            }
        }
        let len = usize::try_from(grant.size)
            .map_err(|_| Failure::too_large("mapping exceeds address space"))?;
        let prot = libc::PROT_READ
            | if grant.permissions.plugin_writable() {
                libc::PROT_WRITE
            } else {
                0
            };
        let p = unsafe {
            libc::mmap(
                std::ptr::null_mut(),
                len,
                prot,
                libc::MAP_SHARED,
                file.as_raw_fd(),
                0,
            )
        };
        if p == libc::MAP_FAILED {
            return Err(Failure::os(std::io::Error::last_os_error()));
        }
        // mmap retains the object; close the descriptor immediately, including
        // on later validation failures. Retirement then has one resource to drop.
        Ok(Self {
            ptr: NonNull::new(p.cast()).unwrap(),
            len,
            writable: grant.permissions.plugin_writable(),
        })
    }
    #[cfg(windows)]
    pub(super) fn open(grant: &MemoryGrant, name: &str) -> Result<Self> {
        use windows_sys::Win32::{Foundation::CloseHandle, System::Memory::*};
        let len = usize::try_from(grant.size)
            .map_err(|_| Failure::too_large("mapping exceeds address space"))?;
        let wide: Vec<u16> = name.encode_utf16().chain(Some(0)).collect();
        let access = if grant.permissions.plugin_writable() {
            FILE_MAP_WRITE
        } else {
            FILE_MAP_READ
        };
        let handle = unsafe { OpenFileMappingW(access, 0, wide.as_ptr()) };
        if handle.is_null() {
            return Err(Failure::os(std::io::Error::last_os_error()));
        }
        // Map the existing section in full; never create-or-open a private one.
        let view = unsafe { MapViewOfFile(handle, access, 0, 0, 0) };
        let Some(ptr) = NonNull::new(view.Value.cast::<u8>()) else {
            let e = std::io::Error::last_os_error();
            unsafe { CloseHandle(handle) };
            return Err(Failure::os(e));
        };
        let mapping = Self {
            ptr,
            len,
            writable: grant.permissions.plugin_writable(),
            handle,
        };
        let mut info: MEMORY_BASIC_INFORMATION = unsafe { std::mem::zeroed() };
        if unsafe { VirtualQuery(view.Value, &mut info, std::mem::size_of_val(&info)) } == 0
            || info.RegionSize < len
            || info.RegionSize - len >= 4096
        {
            return Err(Failure::malformed(
                "section extent disagrees with grant (page-rounded)",
            ));
        }
        Ok(mapping)
    }
    pub(super) fn read(&self, offset: u64, len: u64) -> Result<Vec<u8>> {
        let (offset, len) = self.range(offset, len)?;
        let mut out = Vec::new();
        out.try_reserve_exact(len)
            .map_err(|_| Failure::retryable("copy allocation failed"))?;
        // The caller owns this range by its discipline. No Rust reference to
        // foreign mutable memory is created; the returned allocation is private.
        #[cfg(test)]
        before_copy();
        unsafe {
            std::ptr::copy_nonoverlapping(self.ptr.as_ptr().add(offset), out.as_mut_ptr(), len);
            out.set_len(len);
        }
        Ok(out)
    }
    pub(super) fn write(&mut self, offset: u64, bytes: &[u8]) -> Result<()> {
        if !self.writable {
            return Err(Failure::malformed("mapping is read-only"));
        }
        let (offset, len) = self.range(offset, bytes.len() as u64)?;
        #[cfg(test)]
        before_copy();
        unsafe {
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), self.ptr.as_ptr().add(offset), len);
        }
        Ok(())
    }
    fn range(&self, offset: u64, len: u64) -> Result<(usize, usize)> {
        if offset
            .checked_add(len)
            .is_none_or(|end| end > self.len as u64)
        {
            return Err(Failure::malformed("mapping range out of bounds"));
        }
        Ok((offset as usize, len as usize))
    }
    pub(super) fn atomic(&self, offset: u64) -> &AtomicU64 {
        assert!(
            self.writable
                && offset.is_multiple_of(8)
                && offset.checked_add(8).is_some_and(|n| n <= self.len as u64)
        );
        // Install validated alignment/extent; only state words use this method.
        unsafe { &*self.ptr.as_ptr().add(offset as usize).cast::<AtomicU64>() }
    }
}
impl Drop for Mapping {
    fn drop(&mut self) {
        #[cfg(unix)]
        unsafe {
            libc::munmap(self.ptr.as_ptr().cast(), self.len);
        }
        #[cfg(windows)]
        unsafe {
            windows_sys::Win32::System::Memory::UnmapViewOfFile(
                windows_sys::Win32::System::Memory::MEMORY_MAPPED_VIEW_ADDRESS {
                    Value: self.ptr.as_ptr().cast(),
                },
            );
            windows_sys::Win32::Foundation::CloseHandle(self.handle);
        }
    }
}
