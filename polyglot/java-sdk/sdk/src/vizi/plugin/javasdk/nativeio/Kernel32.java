package vizi.plugin.javasdk.nativeio;

import static java.lang.foreign.ValueLayout.ADDRESS;
import static java.lang.foreign.ValueLayout.JAVA_CHAR;
import static java.lang.foreign.ValueLayout.JAVA_INT;
import static java.lang.foreign.ValueLayout.JAVA_LONG;

import java.lang.foreign.Arena;
import java.lang.foreign.FunctionDescriptor;
import java.lang.foreign.Linker;
import java.lang.foreign.MemoryLayout;
import java.lang.foreign.MemorySegment;
import java.lang.foreign.StructLayout;
import java.lang.foreign.SymbolLookup;
import java.lang.invoke.MethodHandle;
import java.lang.invoke.VarHandle;

/**
 * kernel32 downcalls for Windows shared memory: a named section the host created (doorbell_M
 * _memory.md §1, §5). The plugin OPENS the section and never creates it: a create-or-open call
 * would silently hand back a fresh zero-filled private section when the name is gone, which only
 * the preamble's checksum and identity proof would catch.
 *
 * <p><b>Not yet run anywhere</b>; see {@link Winsock}.
 */
public final class Kernel32 {
  private Kernel32() {}

  public static final int FILE_MAP_WRITE = 0x0002;
  public static final int FILE_MAP_READ = 0x0004;

  private static final Linker LINKER = Linker.nativeLinker();
  private static final StructLayout CAPTURE = Linker.Option.captureStateLayout();
  private static final VarHandle LAST_ERROR =
      CAPTURE.varHandle(MemoryLayout.PathElement.groupElement("GetLastError"));

  private static final class H {
    static final SymbolLookup K32 = SymbolLookup.libraryLookup("kernel32", Arena.global());

    static MethodHandle fn(String name, FunctionDescriptor fd) {
      return LINKER.downcallHandle(K32.findOrThrow(name), fd, Linker.Option.captureCallState("GetLastError"));
    }

    static final MethodHandle OPEN_FILE_MAPPING_W =
        fn("OpenFileMappingW", FunctionDescriptor.of(ADDRESS, JAVA_INT, JAVA_INT, ADDRESS));
    static final MethodHandle MAP_VIEW_OF_FILE =
        fn("MapViewOfFile", FunctionDescriptor.of(ADDRESS, ADDRESS, JAVA_INT, JAVA_INT, JAVA_INT, JAVA_LONG));
    static final MethodHandle UNMAP_VIEW_OF_FILE = fn("UnmapViewOfFile", FunctionDescriptor.of(JAVA_INT, ADDRESS));
    static final MethodHandle CLOSE_HANDLE = fn("CloseHandle", FunctionDescriptor.of(JAVA_INT, ADDRESS));
    static final MethodHandle SET_HANDLE_INFORMATION =
        fn("SetHandleInformation", FunctionDescriptor.of(JAVA_INT, ADDRESS, JAVA_INT, JAVA_INT));
    static final MethodHandle VIRTUAL_QUERY =
        fn("VirtualQuery", FunctionDescriptor.of(JAVA_LONG, ADDRESS, ADDRESS, JAVA_LONG));
  }

  private static int lastError(MemorySegment cap) {
    return (int) LAST_ERROR.get(cap, 0L);
  }

  private static RuntimeException linkage(Throwable t) {
    if (t instanceof RuntimeException r) return r;
    if (t instanceof Error e) throw e;
    return new IllegalStateException("native call failed", t);
  }

  public static final int HANDLE_FLAG_INHERIT = 0x1;

  /** Makes {@code handle} (a SOCKET is one) non-inheritable. */
  public static void clearInherit(long handle) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int ok = (int) H.SET_HANDLE_INFORMATION.invokeExact(cap, MemorySegment.ofAddress(handle), HANDLE_FLAG_INHERIT, 0);
      if (ok == 0) throw new OsException("SetHandleInformation", lastError(cap));
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** An open section and one view of it. */
  public record View(MemorySegment handle, MemorySegment base, long regionSize) {}

  /**
   * Opens the named section and maps {@code size} bytes of it with exactly the access asked for.
   * {@code regionSize} is what VirtualQuery reports for the view, which the caller compares with
   * the grant's {@code size} (page-rounded: the exact section size is not queryable through the
   * documented API).
   */
  public static View openAndMap(String name, long size, boolean writable) throws OsException {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      char[] chars = (name + "\0").toCharArray();
      MemorySegment wname = a.allocate(JAVA_CHAR, chars.length);
      for (int i = 0; i < chars.length; i++) wname.setAtIndex(JAVA_CHAR, i, chars[i]);
      // FILE_MAP_WRITE alone maps read/write; asking for READ as well requests a right not needed
      int access = writable ? FILE_MAP_WRITE : FILE_MAP_READ;
      MemorySegment h = (MemorySegment) H.OPEN_FILE_MAPPING_W.invokeExact(cap, access, 0, wname);
      if (h.address() == 0) throw new OsException("OpenFileMappingW", lastError(cap));
      MemorySegment base = (MemorySegment) H.MAP_VIEW_OF_FILE.invokeExact(cap, h, access, 0, 0, size);
      if (base.address() == 0) {
        int e = lastError(cap);
        int ignored = (int) H.CLOSE_HANDLE.invokeExact(a.allocate(CAPTURE), h);
        throw new OsException("MapViewOfFile", e);
      }
      MemorySegment mbi = a.allocate(48, 8);
      long got = (long) H.VIRTUAL_QUERY.invokeExact(cap, base, mbi, 48L);
      long regionSize = got == 0 ? -1 : mbi.get(JAVA_LONG, 24);
      return new View(h, base.reinterpret(size), regionSize);
    } catch (OsException e) {
      throw e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }

  /** Unmaps the view and closes the handle; returns 0 or the first error code. */
  public static int release(View v) {
    try (Arena a = Arena.ofConfined()) {
      MemorySegment cap = a.allocate(CAPTURE);
      int e = 0;
      if ((int) H.UNMAP_VIEW_OF_FILE.invokeExact(cap, v.base()) == 0) e = lastError(cap);
      if ((int) H.CLOSE_HANDLE.invokeExact(cap, v.handle()) == 0 && e == 0) e = lastError(cap);
      return e;
    } catch (Throwable t) {
      throw linkage(t);
    }
  }
}
