package vizi.plugin.javasdk.nativeio;

import static java.lang.foreign.ValueLayout.ADDRESS;
import static java.lang.foreign.ValueLayout.JAVA_BYTE;
import static java.lang.foreign.ValueLayout.JAVA_INT;
import static java.lang.foreign.ValueLayout.JAVA_LONG;
import static java.lang.foreign.ValueLayout.JAVA_SHORT;

import java.lang.foreign.Arena;
import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.MemoryLayout;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.StructLayout;
import java.lang.foreign.SymbolLookup;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.VarHandle;
import java.util.Arrays;
import org.jspecify.annotations.Nullable;

/**
 * Every POSIX downcall the SDK makes, and the per-OS layouts and constants they need.
 *
 * <p>Nothing outside this package calls a native function. Linux and Darwin differ in {@code
 * msghdr}, {@code cmsghdr}, {@code nfds_t}, {@code SOL_SOCKET}, the {@code MSG_*} bits and
 * {@code O_NONBLOCK}; each is chosen here once, from {@link Platform#OS}. A {@code invokeExact}
 * type mismatch only fails when the call runs, so every downcall here is exercised by a test.
 *
 * <p>Calls that set up state throw {@link OsException}. The two calls on the doorbell's hot path
 * ({@link #recvmsg} and {@link #sendmsg}) return their error code instead, because the transport
 * classifies every code and an exception would be the common case on would-block.
 */
public final class Posix {
  private Posix() {}

  private static final boolean MAC = Platform.OS == Platform.DARWIN;

  // ---- constants ----
  public static final int AF_UNIX = 1;
  public static final int SOCK_STREAM = 1;
  public static final int SOCK_DGRAM = 2;
  public static final int SOCK_SEQPACKET = 5;
  public static final int SOL_SOCKET = MAC ? 0xffff : 1;
  public static final int SO_TYPE = MAC ? 0x1008 : 3;
  public static final int SO_SNDBUF = MAC ? 0x1001 : 7;
  public static final int SO_RCVBUF = MAC ? 0x1002 : 8;
  /** Darwin only; Linux refuses it with ENOPROTOOPT (doorbell_protocol.md §6 rule 4). */
  public static final int SO_NOSIGPIPE = 0x1022;

  public static final int SCM_RIGHTS = 1;

  public static final int F_GETFD = 1;
  public static final int F_SETFD = 2;
  public static final int F_GETFL = 3;
  public static final int F_SETFL = 4;
  public static final int FD_CLOEXEC = 1;
  public static final int O_NONBLOCK = MAC ? 0x4 : 0x800;

  public static final int MSG_DONTWAIT = MAC ? 0x80 : 0x40;
  /** Linux only. Darwin has SO_NOSIGPIPE instead. */
  public static final int MSG_NOSIGNAL = MAC ? 0 : 0x4000;
  /** Linux only; the one input flag a receive passes (§7.6). */
  public static final int MSG_CMSG_CLOEXEC = MAC ? 0 : 0x40000000;
  /** Read through this constant only: Linux 0x08, Darwin 0x20, colliding with each other. */
  public static final int MSG_CTRUNC = MAC ? 0x20 : 0x08;

  public static final short POLLIN = 0x1;
  public static final short POLLERR = 0x8;
  public static final short POLLHUP = 0x10;
  public static final short POLLNVAL = 0x20;

  public static final int PROT_READ = 1;
  public static final int PROT_WRITE = 2;
  public static final int MAP_SHARED = 1;
  public static final int SEEK_END = 2;

  public static final int MFD_CLOEXEC = 1;
  public static final int MFD_ALLOW_SEALING = 2;
  public static final int F_ADD_SEALS = 1033;
  public static final int F_SEAL_SHRINK = 0x2;
  public static final int F_SEAL_GROW = 0x4;

  // ---- layouts ----
  /** struct iovec: the same on both. */
  static final long IOVEC_SIZE = 16;

  /** struct msghdr: Linux 56 bytes (size_t lengths); Darwin 48 (int iovlen, socklen_t controllen). */
  static final long MSGHDR_SIZE = MAC ? 48 : 56;

  static final long MSG_IOV = 16;
  static final long MSG_IOVLEN = 24;
  static final long MSG_CONTROL = 32;
  static final long MSG_CONTROLLEN = 40;
  static final long MSG_FLAGS = MAC ? 44 : 48;

  /** cmsghdr: Linux {size_t len; int level; int type} data at 16, 8-aligned; Darwin 12, 4-aligned. */
  static final long CMSG_HDR = MAC ? 12 : 16;

  static final long CMSG_ALIGN_TO = MAC ? 4 : 8;

  static long cmsgAlign(long n) {
    return (n + CMSG_ALIGN_TO - 1) & ~(CMSG_ALIGN_TO - 1);
  }

  static long cmsgLen(long n) {
    return CMSG_HDR + n;
  }

  static long cmsgSpace(long n) {
    return CMSG_HDR + cmsgAlign(n);
  }

  /**
   * The receive control buffer holds this many descriptors. The protocol carries at most one, but
   * XNU installs every descriptor before it truncates the copy, so a small buffer leaks the rest
   * with no way to recover their numbers; and room for two turns a peer sending two from {@code
   * ctrunc} into the more precise {@code wrong_fd_count} (§7 rule 3).
   */
  public static final int RECV_FD_CAPACITY = 253; // Linux SCM_MAX_FD: a short control buffer on Darwin can leave extra descriptors installed and unseen

  // ---- downcalls ----
  private static final Linker LINKER = Linker.nativeLinker();
  private static final SymbolLookup LIBC = LINKER.defaultLookup();
  private static final StructLayout CAPTURE = Linker.Option.captureStateLayout();
  private static final VarHandle ERRNO =
      CAPTURE.varHandle(MemoryLayout.PathElement.groupElement("errno"));

  private static @Nullable MethodHandle optional(String name, FunctionDescriptor fd, Linker.Option... extra) {
    var sym = LIBC.find(name);
    if (sym.isEmpty()) return null;
    Linker.Option[] opts = Arrays.copyOf(extra, extra.length + 1);
    opts[extra.length] = Linker.Option.captureCallState("errno");
    return LINKER.downcallHandle(sym.get(), fd, opts);
  }

  private static MethodHandle required(String name, FunctionDescriptor fd, Linker.Option... extra) {
    MethodHandle h = optional(name, fd, extra);
    if (h == null) throw new UnsatisfiedLinkError("libc has no " + name);
    return h;
  }

  private static final class H {
    static final MethodHandle SOCKETPAIR =
        required("socketpair", FunctionDescriptor.of(JAVA_INT, JAVA_INT, JAVA_INT, JAVA_INT, ADDRESS));
    static final MethodHandle CLOSE = required("close", FunctionDescriptor.of(JAVA_INT, JAVA_INT));
    static final MethodHandle DUP = required("dup", FunctionDescriptor.of(JAVA_INT, JAVA_INT));
    static final MethodHandle FCNTL =
        required(
            "fcntl",
            FunctionDescriptor.of(JAVA_INT, JAVA_INT, JAVA_INT, JAVA_INT),
            Linker.Option.firstVariadicArg(2));
    static final MethodHandle GETSOCKOPT =
        required(
            "getsockopt",
            FunctionDescriptor.of(JAVA_INT, JAVA_INT, JAVA_INT, JAVA_INT, ADDRESS, ADDRESS));
    static final MethodHandle SETSOCKOPT =
        required(
            "setsockopt",
            FunctionDescriptor.of(JAVA_INT, JAVA_INT, JAVA_INT, JAVA_INT, ADDRESS, JAVA_INT));
    static final MethodHandle SENDMSG =
        required("sendmsg", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, ADDRESS, JAVA_INT));
    static final MethodHandle RECVMSG =
        required("recvmsg", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, ADDRESS, JAVA_INT));
    static final MethodHandle SEND =
        required("send", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, ADDRESS, JAVA_LONG, JAVA_INT));
    static final MethodHandle RECV =
        required("recv", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, ADDRESS, JAVA_LONG, JAVA_INT));
    static final MethodHandle POLL =
        required(
            "poll",
            MAC
                ? FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_INT, JAVA_INT)
                : FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_LONG, JAVA_INT));
    static final MethodHandle LSEEK =
        required("lseek", FunctionDescriptor.of(JAVA_LONG, JAVA_INT, JAVA_LONG, JAVA_INT));
    static final MethodHandle FTRUNCATE =
        required("ftruncate", FunctionDescriptor.of(JAVA_INT, JAVA_INT, JAVA_LONG));
    static final MethodHandle MMAP =
        required(
            "mmap",
            FunctionDescriptor.of(ADDRESS, ADDRESS, JAVA_LONG, JAVA_INT, JAVA_INT, JAVA_INT, JAVA_LONG));
    static final MethodHandle MUNMAP =
        required("munmap", FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_LONG));
    static final MethodHandle MKSTEMP = required("mkstemp", FunctionDescriptor.of(JAVA_INT, ADDRESS));
    static final MethodHandle UNLINK = required("unlink", FunctionDescriptor.of(JAVA_INT, ADDRESS));
    static final @Nullable MethodHandle MEMFD_CREATE =
        MAC ? null : optional("memfd_create", FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_INT));
  }

  private static int errno(MemorySegment cap) {
    return (int) ERRNO.get(cap, 0L);
  }

  private static RuntimeException linkage(Throwable t) {
    if (t instanceof RuntimeException r) return r;
    if (t instanceof Error e) throw e;
    return new IllegalStateException("native call failed", t);
  }

  // ---- descriptors ----

  /** A connected pair of unix sockets of the given type, both close-on-exec. */
  public static int[] socketpair(int type) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment sv = a.allocate(JAVA_INT, 2);
      int r = (int) H.SOCKETPAIR.invokeExact(cap, AF_UNIX, type, 0, sv);
      if (r != 0) throw new OsException("socketpair", errno(cap));
      int[] fds = {sv.getAtIndex(JAVA_INT, 0), sv.getAtIndex(JAVA_INT, 1)};
      setCloexec(fds[0]);
      setCloexec(fds[1]);
      return fds;
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** close(2). Returns 0 or the errno; a descriptor is never closed twice by retrying. */
  public static int close(int fd) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.CLOSE.invokeExact(cap, fd);
      return r == 0 ? 0 : errno(cap);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static int dup(int fd) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.DUP.invokeExact(cap, fd);
      if (r < 0) throw new OsException("dup", errno(cap));
      return r;
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static int fcntl(int fd, int cmd, int arg) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.FCNTL.invokeExact(cap, fd, cmd, arg);
      if (r < 0) throw new OsException("fcntl", errno(cap));
      return r;
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void setCloexec(int fd) throws OsException {
    int flags = fcntl(fd, F_GETFD, 0);
    if ((flags & FD_CLOEXEC) == 0) fcntl(fd, F_SETFD, flags | FD_CLOEXEC);
  }

  public static boolean isCloexec(int fd) throws OsException {
    return (fcntl(fd, F_GETFD, 0) & FD_CLOEXEC) != 0;
  }

  public static void setNonBlocking(int fd) throws OsException {
    int flags = fcntl(fd, F_GETFL, 0);
    if ((flags & O_NONBLOCK) == 0) fcntl(fd, F_SETFL, flags | O_NONBLOCK);
  }

  public static boolean isNonBlocking(int fd) throws OsException {
    return (fcntl(fd, F_GETFL, 0) & O_NONBLOCK) != 0;
  }

  public static int getsockoptInt(int fd, int level, int name) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment val = a.allocate(JAVA_INT);
      MemorySegment len = a.allocate(JAVA_INT);
      len.set(JAVA_INT, 0, 4);
      int r = (int) H.GETSOCKOPT.invokeExact(cap, fd, level, name, val, len);
      if (r != 0) throw new OsException("getsockopt", errno(cap));
      return val.get(JAVA_INT, 0);
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void setsockoptInt(int fd, int level, int name, int value) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment val = a.allocate(JAVA_INT);
      val.set(JAVA_INT, 0, value);
      int r = (int) H.SETSOCKOPT.invokeExact(cap, fd, level, name, val, 4);
      if (r != 0) throw new OsException("setsockopt", errno(cap));
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  // ---- the doorbell's hot path ----

  /**
   * One {@code recvmsg}. {@code n} is the byte count, or -1 with {@code errno} set. Every {@code
   * SCM_RIGHTS} descriptor that arrived is in {@code fds}, already close-on-exec (atomically via
   * {@code MSG_CMSG_CLOEXEC} on Linux, immediately after the call on Darwin).
   */
  public record Received(long n, int errno, boolean ctrunc, int[] fds, byte[] data) {}

  public static Received recvmsg(int fd, int bufferSize) {
    return recvmsg(fd, bufferSize, RECV_FD_CAPACITY);
  }

  /** With room for {@code fdCapacity} descriptors: tests use a small one to provoke {@code MSG_CTRUNC}. */
  static Received recvmsg(int fd, int bufferSize, int fdCapacity) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(bufferSize, 8);
      MemorySegment iov = a.allocate(IOVEC_SIZE, 8);
      iov.set(ADDRESS, 0, buf);
      iov.set(JAVA_LONG, 8, bufferSize);
      long ctlSize = cmsgSpace(4L * fdCapacity);
      MemorySegment ctl = a.allocate(ctlSize, 8);
      MemorySegment msg = a.allocate(MSGHDR_SIZE, 8);
      msg.set(ADDRESS, MSG_IOV, iov);
      if (MAC) {
        msg.set(JAVA_INT, MSG_IOVLEN, 1);
        msg.set(ADDRESS, MSG_CONTROL, ctl);
        msg.set(JAVA_INT, MSG_CONTROLLEN, (int) ctlSize);
      } else {
        msg.set(JAVA_LONG, MSG_IOVLEN, 1L);
        msg.set(ADDRESS, MSG_CONTROL, ctl);
        msg.set(JAVA_LONG, MSG_CONTROLLEN, ctlSize);
      }
      long n = (long) H.RECVMSG.invokeExact(cap, fd, msg, MSG_CMSG_CLOEXEC);
      if (n < 0) return new Received(-1, errno(cap), false, new int[0], new byte[0]);
      int flags = msg.get(JAVA_INT, MSG_FLAGS);
      long controllen = MAC ? Integer.toUnsignedLong(msg.get(JAVA_INT, MSG_CONTROLLEN)) : msg.get(JAVA_LONG, MSG_CONTROLLEN);
      controllen = Math.min(controllen, ctlSize);
      int[] fds = parseRights(ctl, controllen);
      if (MAC) {
        for (int f : fds) {
          try {
            setCloexec(f);
          } catch (OsException ignored) {
            // The descriptor is still returned; the transport closes it on every path.
          }
        }
      }
      byte[] data = buf.asSlice(0, Math.min(n, bufferSize)).toArray(JAVA_BYTE);
      return new Received(n, 0, (flags & MSG_CTRUNC) != 0, fds, data);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * Walks the control buffer. Each header's {@code cmsg_len} is clamped to what the kernel says
   * it wrote: Darwin can report a length past {@code msg_controllen}, and trusting it would adopt
   * stack bytes as descriptors.
   */
  static int[] parseRights(MemorySegment ctl, long controllen) {
    int[] out = new int[(int) Math.max(0, controllen / 4)];
    int count = 0;
    long off = 0;
    while (off + CMSG_HDR <= controllen) {
      long len = MAC ? Integer.toUnsignedLong(ctl.get(JAVA_INT, off)) : ctl.get(JAVA_LONG, off);
      if (len < CMSG_HDR) break;
      len = Math.min(len, controllen - off);
      int level = ctl.get(JAVA_INT, off + (MAC ? 4 : 8));
      int type = ctl.get(JAVA_INT, off + (MAC ? 8 : 12));
      if (level == SOL_SOCKET && type == SCM_RIGHTS) {
        long nfd = (len - CMSG_HDR) / 4;
        for (long i = 0; i < nfd && count < out.length; i++) {
          out[count++] = ctl.get(JAVA_INT, off + CMSG_HDR + 4 * i);
        }
      }
      off += cmsgAlign(len);
    }
    return Arrays.copyOf(out, count);
  }

  /** {@code n} bytes accepted, or -1 with {@code errno}. */
  public record Sent(long n, int errno) {}

  /**
   * One {@code sendmsg}, never looped (§6 rule 2), carrying {@code passFds} as one {@code
   * SCM_RIGHTS} message (the protocol sends at most one; tests send more to exercise the
   * receiver's count rule). The flags are the platform's: {@code MSG_NOSIGNAL|MSG_DONTWAIT} on
   * Linux, {@code MSG_DONTWAIT} on Darwin, where {@code SO_NOSIGPIPE} is set at setup instead.
   */
  public static Sent sendmsg(int fd, byte[] data, int... passFds) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(Math.max(1, data.length));
      MemorySegment.copy(data, 0, buf, JAVA_BYTE, 0, data.length);
      MemorySegment iov = a.allocate(IOVEC_SIZE, 8);
      iov.set(ADDRESS, 0, buf);
      iov.set(JAVA_LONG, 8, data.length);
      MemorySegment msg = a.allocate(MSGHDR_SIZE, 8);
      msg.set(ADDRESS, MSG_IOV, iov);
      if (MAC) msg.set(JAVA_INT, MSG_IOVLEN, 1);
      else msg.set(JAVA_LONG, MSG_IOVLEN, 1L);
      if (passFds.length > 0) {
        long payload = 4L * passFds.length;
        long space = cmsgSpace(payload);
        MemorySegment ctl = a.allocate(space, 8);
        if (MAC) {
          ctl.set(JAVA_INT, 0, (int) cmsgLen(payload));
          ctl.set(JAVA_INT, 4, SOL_SOCKET);
          ctl.set(JAVA_INT, 8, SCM_RIGHTS);
        } else {
          ctl.set(JAVA_LONG, 0, cmsgLen(payload));
          ctl.set(JAVA_INT, 8, SOL_SOCKET);
          ctl.set(JAVA_INT, 12, SCM_RIGHTS);
        }
        for (int i = 0; i < passFds.length; i++) ctl.set(JAVA_INT, CMSG_HDR + 4L * i, passFds[i]);
        msg.set(ADDRESS, MSG_CONTROL, ctl);
        if (MAC) msg.set(JAVA_INT, MSG_CONTROLLEN, (int) space);
        else msg.set(JAVA_LONG, MSG_CONTROLLEN, space);
      }
      long n = (long) H.SENDMSG.invokeExact(cap, fd, msg, MSG_NOSIGNAL | MSG_DONTWAIT);
      return n < 0 ? new Sent(-1, errno(cap)) : new Sent(n, 0);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** send(2) with the given flags; used on stream sockets and the wake endpoint. */
  public static Sent send(int fd, byte[] data, int off, int len, int flags) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(Math.max(1, len));
      MemorySegment.copy(data, off, buf, JAVA_BYTE, 0, len);
      long n = (long) H.SEND.invokeExact(cap, fd, buf, (long) len, flags);
      return n < 0 ? new Sent(-1, errno(cap)) : new Sent(n, 0);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** recv(2) into {@code out[off..off+len)}. Returns the count, or -1 with {@code errno}. */
  public static Sent recv(int fd, byte[] out, int off, int len, int flags) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment buf = a.allocate(Math.max(1, len));
      long n = (long) H.RECV.invokeExact(cap, fd, buf, (long) len, flags);
      if (n < 0) return new Sent(-1, errno(cap));
      MemorySegment.copy(buf, JAVA_BYTE, 0, out, off, (int) n);
      return new Sent(n, 0);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * poll(2) on {@code fds} for {@code POLLIN}. Returns the ready count (0 on timeout) and fills
   * {@code revents}; a failure returns the errno negated, so {@code r < 0} means {@code -r}.
   */
  public static int poll(int[] fds, short[] revents, int timeoutMs) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment pfds = a.allocate(8L * fds.length, 8);
      for (int i = 0; i < fds.length; i++) {
        pfds.set(JAVA_INT, 8L * i, fds[i]);
        pfds.set(JAVA_SHORT, 8L * i + 4, POLLIN);
        pfds.set(JAVA_SHORT, 8L * i + 6, (short) 0);
      }
      int r =
          MAC
              ? (int) H.POLL.invokeExact(cap, pfds, fds.length, timeoutMs)
              : (int) H.POLL.invokeExact(cap, pfds, (long) fds.length, timeoutMs);
      if (r < 0) return -errno(cap);
      for (int i = 0; i < fds.length; i++) revents[i] = pfds.get(JAVA_SHORT, 8L * i + 6);
      return r;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  // ---- memory ----

  /** The size of the object behind {@code fd}, by seeking to its end. */
  public static long objectSize(int fd) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      long r = (long) H.LSEEK.invokeExact(cap, fd, 0L, SEEK_END);
      if (r < 0) throw new OsException("lseek", errno(cap));
      return r;
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  public static void ftruncate(int fd, long size) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.FTRUNCATE.invokeExact(cap, fd, size);
      if (r != 0) throw new OsException("ftruncate", errno(cap));
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * Maps {@code size} bytes of {@code fd} shared, with exactly the protection asked for and never
   * more. The returned segment is valid until {@link #munmap} and is not tied to any arena: the
   * memory layer owns its lifetime, because it must unmap before it reports a release.
   */
  public static MemorySegment mmap(int fd, long size, boolean writable) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int prot = PROT_READ | (writable ? PROT_WRITE : 0);
      MemorySegment p =
          (MemorySegment) H.MMAP.invokeExact(cap, MemorySegment.NULL, size, prot, MAP_SHARED, fd, 0L);
      if (p.address() == -1L) throw new OsException("mmap", errno(cap));
      return p.reinterpret(size);
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** munmap(2). Returns 0 or the errno. */
  public static int munmap(MemorySegment mapping) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int r = (int) H.MUNMAP.invokeExact(cap, mapping, mapping.byteSize());
      return r == 0 ? 0 : errno(cap);
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /**
   * A fresh shared-memory object of {@code size} bytes: a sealed memfd on Linux (sealed against
   * shrink and grow, as the host's are), an unlinked temporary file on Darwin. The host side of
   * the testkit and the tests use this; a plugin only ever receives descriptors.
   */
  public static int createSharedObject(String name, long size) throws OsException {
    if (!MAC && H.MEMFD_CREATE != null) {
      try (Arena a = Arena.ofConfined()) {
        MemorySegment cap = a.allocate(CAPTURE);
        int fd =
            (int) H.MEMFD_CREATE.invokeExact(cap, a.allocateFrom(name), MFD_CLOEXEC | MFD_ALLOW_SEALING);
        if (fd < 0) throw new OsException("memfd_create", errno(cap));
        try {
          ftruncate(fd, size);
          fcntl(fd, F_ADD_SEALS, F_SEAL_SHRINK | F_SEAL_GROW);
        } catch (OsException e) {
          close(fd);
          throw e;
        }
        return fd;
      } catch (OsException e) {
        throw e;
      } catch (Throwable t) {
        throw linkage(t);
      }
    }
    return unlinkedTempFile(size);
  }

  /** mkstemp + unlink: the Darwin backing, and the fallback anywhere memfd is missing. */
  public static int unlinkedTempFile(long size) throws OsException {
    NamedFile f = namedTempFile(size);
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int u = (int) H.UNLINK.invokeExact(cap, a.allocateFrom(f.path()));
      if (u != 0) {
        int e = errno(cap);
        close(f.fd());
        throw new OsException("unlink", e);
      }
      return f.fd();
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** A descriptor and the path it was created at. */
  public record NamedFile(int fd, String path) {}

  /**
   * A temporary file of {@code size} bytes that keeps its name, close-on-exec. Tests map it from a
   * second process by path, which a descriptor cannot reach through {@code ProcessBuilder}.
   */
  public static NamedFile namedTempFile(long size) throws OsException {
    String dir = System.getProperty("java.io.tmpdir", "/tmp");
    if (!dir.endsWith("/")) dir += "/";
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      MemorySegment template = a.allocateFrom(dir + "vizi-seg-XXXXXX");
      int fd = (int) H.MKSTEMP.invokeExact(cap, template);
      if (fd < 0) throw new OsException("mkstemp", errno(cap));
      String path = template.getString(0);
      try {
        setCloexec(fd);
        ftruncate(fd, size);
      } catch (OsException e) {
        close(fd);
        throw e;
      }
      return new NamedFile(fd, path);
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }
}
