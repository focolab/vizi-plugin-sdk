package vizi.plugin.javasdk;

import java.lang.foreign.MemorySegment;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalDouble;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.OpenExport;

/**
 * Starts an export (reader_writer_contract.md §2): the host writes voxels, a timepoint at a
 * time, and this plugin writes the artifact. Runs on the work worker. Throw {@link Refusal} to
 * choose the reply's kind; any other exception answers {@code IO_ERROR}.
 */
@FunctionalInterface
public interface Exporter {

  Export open(Request request) throws Exception;

  /** An export in progress. */
  interface Export {
    /** One chunk; its bytes are the SDK's and are invalid after the call returns. */
    void chunk(Chunk chunk) throws Exception;

    /** Finalize the artifact. */
    void finish() throws Exception;

    /** Abandon and clean up; never finalize. */
    default void abort() {}
  }

  /**
   * What to write, and where (reader_writer_contract.md §2.2, control_plane_protocol.md §8.6).
   * {@code littleEndian} is the byte order of the chunks the host will send. Each calibration
   * value is present only when the host knows it (a field of 0 is unknown, per value), in {@code
   * spacingUnit} or {@code timeUnit}; write no measurement you were not given.
   *
   * <p>What the artifact IS: the shape, the calibration and {@code channels}, the artifact's
   * channels in its C order, each with its SOURCE index, its name and whose the name is. Write a
   * channel's name into the artifact only when it is {@link ChannelName#declared declared}: a
   * placeholder written there would read back as a name the file gave.
   *
   * <p>What it CAME FROM: {@code sourceMetadata}, the SOURCE's own document ({@link
   * Dataset.Metadata#NONE} when it has none), and {@code provenanceJson}, the host's record
   * ({@code ""} when it keeps none), parsed by {@link #provenance}. The source's document describes
   * the source, whose sizes, channels and pixel sizes a subset, crop or projection no longer has:
   * embed it only as provenance, never as the artifact's own description, and read no measurement
   * out of it.
   */
  record Request(
      String exporterId,
      String locator,
      int t,
      int c,
      int z,
      int y,
      int x,
      DType dtype,
      OptionalDouble spacingZ,
      OptionalDouble spacingY,
      OptionalDouble spacingX,
      String spacingUnit,
      OptionalDouble timeIncrement,
      String timeUnit,
      Path fileStateDir,
      Dataset.Metadata sourceMetadata,
      List<ChannelName> channels,
      String provenanceJson,
      boolean littleEndian,
      OpenExport raw) {

    public Request {
      channels = List.copyOf(channels);
    }

    /**
     * The provenance record as a JSON object ({@link JsonValues}' plain values), or empty when
     * there is none or it is not one: provenance never fails an export.
     */
    public Optional<Map<String, @Nullable Object>> provenance() {
      return JsonValues.object(provenanceJson);
    }
  }

  /**
   * One chunk: {@code channels.length} volumes of the artifact's {@code (Z, Y, X)}, concatenated in
   * channel order.
   *
   * @param tRank the artifact time index: the rank of {@code t} among the export's distinct t's
   * @param cRanks each channel's artifact index
   */
  record Chunk(long t, int tRank, List<Integer> channels, List<Integer> cRanks, List<Integer> filteredChannels,
      int z, int y, int x, DType dtype, MemorySegment bytes) {

    /** The {@code k}-th channel's volume (a view of {@link #bytes}). */
    public Volume volume(int k) {
      long per = (long) z * y * x * dtype.size;
      return Volume.of(z, y, x, dtype, bytes.asSlice(k * per, per));
    }
  }
}
