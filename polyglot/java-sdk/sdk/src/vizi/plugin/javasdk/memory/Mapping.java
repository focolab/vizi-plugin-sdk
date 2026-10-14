package vizi.plugin.javasdk.memory;

import java.lang.foreign.MemorySegment;
import vizi.plugin.javasdk.nativeio.Errno;
import vizi.plugin.javasdk.nativeio.Kernel32;
import vizi.plugin.javasdk.nativeio.OsException;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.v1.ViziPlugin.GrantFailureClass;

/**
 * One granted mapping and the handle that keeps its object alive. {@link #release} unmaps AND
 * closes (shared_memory_protocol.md §9.2): an open descriptor keeps the object alive after the
 * last unmap, so a release that only unmapped would be false.
 */
abstract sealed class Mapping permits Mapping.Unix, Mapping.Windows {

  final long size;
  final boolean writable;

  Mapping(long size, boolean writable) {
    this.size = size;
    this.writable = writable;
  }

  /** The whole mapping, read-only when the grant is; a stray write then throws, not SIGSEGVs. */
  abstract MemorySegment segment();

  /** Unmaps and closes. Returns an error description, or null. Idempotent. */
  abstract @org.jspecify.annotations.Nullable String release();

  /** A mapping failure, classified by its typed code (doorbell_M_memory.md §7). */
  record Failure(GrantFailureClass failureClass, String detail) {}

  static Failure classify(String what, OsException e) {
    GrantFailureClass c = Errno.isRetryableMappingError(e.code())
        ? GrantFailureClass.GRANT_FAILURE_RETRYABLE
        : GrantFailureClass.GRANT_FAILURE_FAILED;
    return new Failure(c, what + ": " + e.getMessage());
  }

  /** unix: a received descriptor, mapped with exactly the declared access. */
  static final class Unix extends Mapping {
    private int fd;
    private MemorySegment base;
    private final MemorySegment view;

    private Unix(int fd, MemorySegment base, long size, boolean writable) {
      super(size, writable);
      this.fd = fd;
      this.base = base;
      this.view = writable ? base : base.asReadOnly();
    }

    /**
     * Takes ownership of {@code fd}: on every failure it is closed before this returns, so the
     * caller never has a descriptor to leak.
     */
    static Unix map(int fd, long size, boolean writable) throws MappingException {
      long actual;
      try {
        actual = Posix.objectSize(fd);
      } catch (OsException e) {
        Posix.close(fd);
        throw new MappingException(classify("cannot size the granted object", e));
      }
      if (actual != size) {
        Posix.close(fd);
        throw new MappingException(new Failure(GrantFailureClass.GRANT_FAILURE_MALFORMED,
            "the granted object is " + actual + " bytes, the grant says " + Long.toUnsignedString(size)));
      }
      try {
        MemorySegment base = Posix.mmap(fd, size, writable);
        return new Unix(fd, base, size, writable);
      } catch (OsException e) {
        Posix.close(fd);
        Failure f = e.code() == Errno.EACCES
            ? new Failure(GrantFailureClass.GRANT_FAILURE_MALFORMED,
                "mapping with the declared perms was refused (the access obtained does not match perms): " + e.getMessage())
            : classify("mmap", e);
        throw new MappingException(f);
      }
    }

    @Override
    MemorySegment segment() {
      return view;
    }

    @Override
    synchronized @org.jspecify.annotations.Nullable String release() {
      String err = null;
      if (base.address() != 0) {
        int e = Posix.munmap(base);
        if (e != 0) err = "munmap: " + Errno.describe(e);
        base = MemorySegment.NULL;
      }
      if (fd >= 0) {
        int e = Posix.close(fd);
        if (e != 0 && err == null) err = "close: " + Errno.describe(e);
        fd = -1;
      }
      return err;
    }
  }

  /** Windows: a named section, opened (never created) and viewed. Not yet run anywhere. */
  static final class Windows extends Mapping {
    private Kernel32.@org.jspecify.annotations.Nullable View view;
    private final MemorySegment seg;

    private Windows(Kernel32.View view, long size, boolean writable) {
      super(size, writable);
      this.view = view;
      this.seg = writable ? view.base() : view.base().asReadOnly();
    }

    static Windows map(String name, long size, boolean writable) throws MappingException {
      Kernel32.View v;
      try {
        v = Kernel32.openAndMap(name, size, writable);
      } catch (OsException e) {
        throw new MappingException(classify("opening the section " + name, e));
      }
      long region = v.regionSize();
      if (region >= 0 && (region < size || region >= size + 65536)) {
        Kernel32.release(v);
        throw new MappingException(new Failure(GrantFailureClass.GRANT_FAILURE_MALFORMED,
            "the section view is " + region + " bytes, the grant says " + size));
      }
      return new Windows(v, size, writable);
    }

    @Override
    MemorySegment segment() {
      return seg;
    }

    @Override
    synchronized @org.jspecify.annotations.Nullable String release() {
      Kernel32.View v = view;
      if (v == null) return null;
      view = null;
      int e = Kernel32.release(v);
      return e == 0 ? null : "UnmapViewOfFile/CloseHandle: error " + e;
    }
  }

  static boolean windows() {
    return Platform.OS == Platform.WINDOWS;
  }

  /** A failed mapping, already cleaned up. */
  static final class MappingException extends Exception {
    private static final long serialVersionUID = 1L;
    final transient Failure failure;

    MappingException(Failure failure) {
      super(failure.detail());
      this.failure = failure;
    }
  }
}
