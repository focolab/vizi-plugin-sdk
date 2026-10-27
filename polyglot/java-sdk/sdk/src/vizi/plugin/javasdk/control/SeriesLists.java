package vizi.plugin.javasdk.control;

import com.google.protobuf.CodedOutputStream;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.SeriesEntry;
import vizi.plugin.v1.ViziPlugin.SeriesList;

/**
 * A reader's list of the images one file holds (control_plane_protocol.md §8.2,
 * reader_writer_contract.md §1.2a): checked before it is sent, and trimmed to the message
 * ceiling.
 */
public final class SeriesLists {
  private SeriesLists() {}

  /** What a sent list may occupy: the message ceiling less 64 bytes for the envelope around it. */
  public static final int BUDGET = Bounds.MAX_MESSAGE - 64;

  /** The refusal of a list that is not valid, which also closes what listed it. */
  public static HandlerFailure invalid(String detail) {
    return new HandlerFailure(Error.Kind.INVALID_ARGUMENT, "open failed: the reader's SeriesChoice is not valid: " + detail);
  }

  /** The refusal of a list answered to an {@code OpenReader} that named a series. */
  public static HandlerFailure listForNamedSeries(String series) {
    return new HandlerFailure(Error.Kind.INVALID_ARGUMENT,
        "open failed: asked for series '" + series + "', the reader answered with a list");
  }

  /** Why a list with no entries is refused. */
  public static final String EMPTY = "a SeriesChoice lists no series; a file with no image is refused";

  /**
   * What is wrong with one entry, or null: a shape, when given, is five positive sizes; a dtype,
   * when given, is one a reader serves.
   *
   * @param shape as signed values, so that a caller holding {@code int}s can report a negative one
   */
  public static @Nullable String entryProblem(String id, List<Long> shape, DType dtype) {
    if (!shape.isEmpty() && (shape.size() != 5 || shape.stream().anyMatch(n -> n <= 0))) {
      return "series '" + id + "': shape_tczyx must be five positive sizes, not " + shape;
    }
    if (dtype != DType.DTYPE_UNSPECIFIED && Application.itemSize(dtype) == 0) {
      return "series '" + id + "': dtype " + dtype + " is not one a reader serves";
    }
    return null;
  }

  /** Why a list naming one id twice is refused: the host answers with the id alone. */
  public static String listedTwice(String id) {
    return "series id '" + id + "' is listed twice";
  }

  /** The first problem in {@code entries} as received from an application, or null. */
  static @Nullable String problem(List<SeriesEntry> entries) {
    if (entries.isEmpty()) return EMPTY;
    Set<String> ids = new HashSet<>();
    for (SeriesEntry e : entries) {
      String p = entryProblem(e.getId(), e.getShapeTczyxList().stream().map(Integer::toUnsignedLong).toList(), e.getDtype());
      if (p != null) return p;
      if (!ids.add(e.getId())) return listedTwice(e.getId());
    }
    return null;
  }

  /**
   * The entries, in order, while they fit in {@link #BUDGET} bytes, each costing its encoded size
   * plus its tag and length; {@code truncated} when some did not.
   */
  public static SeriesList trim(List<SeriesEntry> entries) {
    return trim(entries, BUDGET);
  }

  static SeriesList trim(List<SeriesEntry> entries, long budget) {
    SeriesList.Builder b = SeriesList.newBuilder();
    long used = 0;
    for (SeriesEntry e : entries) {
      int size = e.getSerializedSize();
      long cost = (long) CodedOutputStream.computeTagSize(SeriesList.ENTRIES_FIELD_NUMBER)
          + CodedOutputStream.computeUInt32SizeNoTag(size) + size;
      if (used + cost > budget) {
        return b.setTruncated(true).build();
      }
      used += cost;
      b.addEntries(e);
    }
    return b.build();
  }
}
