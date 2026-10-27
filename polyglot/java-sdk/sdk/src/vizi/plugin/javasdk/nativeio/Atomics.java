package vizi.plugin.javasdk.nativeio;

import java.lang.foreign.MemorySegment;
import java.lang.foreign.ValueLayout;
import java.lang.invoke.VarHandle;

/**
 * 64-bit atomics on shared memory: the Pulsed state words (shared_memory_protocol.md §6) and the
 * Staged turn word (processing_stages.md §6.1). Each word is 8-aligned in a mapping another
 * process also writes, so plain Java fields cannot serve and nothing here is cached.
 *
 * <p>{@code compareAndExchange} is a full volatile CAS, stronger than the AcqRel-on-success,
 * Acquire-on-failure the protocol asks for, and that is fine.
 */
public final class Atomics {
  private Atomics() {}

  private static final VarHandle LONG = ValueLayout.JAVA_LONG.varHandle();

  public static long loadAcquire(MemorySegment seg, long offset) {
    return (long) LONG.getAcquire(seg, offset);
  }

  public static void storeRelease(MemorySegment seg, long offset, long value) {
    LONG.setRelease(seg, offset, value);
  }

  /** Returns the witness value: equal to {@code expected} exactly when the swap happened. */
  public static long compareAndExchange(MemorySegment seg, long offset, long expected, long value) {
    return (long) LONG.compareAndExchange(seg, offset, expected, value);
  }

  public static boolean compareAndSet(MemorySegment seg, long offset, long expected, long value) {
    return compareAndExchange(seg, offset, expected, value) == expected;
  }
}
