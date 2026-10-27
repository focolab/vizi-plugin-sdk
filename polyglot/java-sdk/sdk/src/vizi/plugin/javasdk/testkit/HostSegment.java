package vizi.plugin.javasdk.testkit;

import java.lang.foreign.MemorySegment;
import java.security.SecureRandom;
import java.util.List;
import vizi.plugin.javasdk.doorbell.MemoryGrant;
import vizi.plugin.javasdk.memory.Preamble;
import vizi.plugin.javasdk.nativeio.OsException;
import vizi.plugin.javasdk.nativeio.Posix;
import vizi.plugin.v1.ViziPlugin.InstallSegment;
import vizi.plugin.v1.ViziPlugin.Region;
import vizi.plugin.v1.ViziPlugin.RegionDiscipline;
import vizi.plugin.v1.ViziPlugin.RegionProducer;

/**
 * The host's side of one grant, for tests and host emulators (unix): a shared object with its
 * preamble written, the host's own writable mapping of it, the {@code M} record that grants it,
 * and helpers to build the {@code InstallSegment} that gives it meaning.
 */
public final class HostSegment implements AutoCloseable {
  private static final SecureRandom RANDOM = new SecureRandom();

  public final long id;
  public final long size;
  public final int fd;
  public final MemorySegment view;
  public final byte[] proof;

  private HostSegment(long id, long size, int fd, MemorySegment view, byte[] proof) {
    this.id = id;
    this.size = size;
    this.fd = fd;
    this.view = view;
    this.proof = proof;
  }

  /** A fresh object of {@code size} bytes with a valid preamble naming {@code id}. */
  public static HostSegment create(long id, long size) throws OsException {
    int fd = Posix.createSharedObject("vizi-host-" + Long.toUnsignedString(id), size);
    MemorySegment view = Posix.mmap(fd, size, true);
    byte[] proof = new byte[16];
    RANDOM.nextBytes(proof);
    Preamble.write(view, id, proof);
    return new HostSegment(id, size, fd, view, proof);
  }

  /** The {@code M} record granting this segment with {@code perms}. */
  public byte[] grantRecord(long recordId, int perms) {
    return MemoryGrant.unix(id, size, perms).encode(recordId);
  }

  /** Sends the {@code M} (with the descriptor) on the host end of a doorbell socketpair. */
  public void sendGrant(int hostDoorbellFd, long recordId, int perms) {
    byte[] rec = grantRecord(recordId, perms);
    Posix.Sent s = Posix.sendmsg(hostDoorbellFd, rec, fd);
    if (s.n() != rec.length) throw new IllegalStateException("sending M failed: " + s);
  }

  public InstallSegment install(String purpose, String subject, List<Region> regions) {
    return install(purpose, subject, 0, regions);
  }

  public InstallSegment install(String purpose, String subject, long supersedes, List<Region> regions) {
    return InstallSegment.newBuilder()
        .setSegmentId(id)
        .setPurpose(purpose)
        .setSubject(subject)
        .setSupersedes(supersedes)
        .addAllRegions(regions)
        .build();
  }

  public static Region loaned(int index, long offset, long extent, RegionProducer producer) {
    return Region.newBuilder()
        .setIndex(index)
        .setOffset(offset)
        .setExtent(extent)
        .setDiscipline(RegionDiscipline.REGION_DISCIPLINE_LOANED)
        .setProducer(producer)
        .setSlotBytes(64)
        .build();
  }

  public static Region pulsed(int index, long offset, int buffers, long slotBytes, RegionProducer producer, boolean paneHeader) {
    Region.Builder b = Region.newBuilder()
        .setIndex(index)
        .setOffset(offset)
        .setExtent(16L * buffers + (long) buffers * slotBytes)
        .setDiscipline(RegionDiscipline.REGION_DISCIPLINE_PULSED)
        .setProducer(producer)
        .setBuffers(buffers)
        .setSlotBytes(slotBytes);
    if (paneHeader) b.setEpochOffset(slotBytes - 8);
    return b.build();
  }

  public static Region of(int index, long offset, long extent, RegionDiscipline d, RegionProducer p) {
    return Region.newBuilder().setIndex(index).setOffset(offset).setExtent(extent).setDiscipline(d).setProducer(p).build();
  }

  public static final int READ_WRITE = MemoryGrant.PLUGIN_READ | MemoryGrant.PLUGIN_WRITE | MemoryGrant.HOST_READ | MemoryGrant.HOST_WRITE;
  public static final int READ_ONLY = MemoryGrant.PLUGIN_READ | MemoryGrant.HOST_READ | MemoryGrant.HOST_WRITE;

  @Override
  public void close() {
    Posix.munmap(view);
    Posix.close(fd);
  }
}
