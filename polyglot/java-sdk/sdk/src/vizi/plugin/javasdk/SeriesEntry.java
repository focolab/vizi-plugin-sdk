package vizi.plugin.javasdk;

import java.util.List;
import java.util.Objects;
import java.util.Optional;

/**
 * One image of a file, as a {@link SeriesChoice} lists it. Shape and dtype are optional:
 * leave them out when learning them would mean reading more than a listing should.
 *
 * @param id yours, and opaque to the host, which sends it back as the series to open: an HDF5
 *     path, an index, a member name
 * @param label what a person is shown ({@code ""} shows the id)
 * @param shapeTczyx five positive sizes, or empty when unknown
 * @param description one line, or empty
 */
public record SeriesEntry(String id, String label, List<Integer> shapeTczyx, Optional<DType> dtype, String description) {
  public SeriesEntry {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(label, "label");
    shapeTczyx = List.copyOf(shapeTczyx);
    Objects.requireNonNull(dtype, "dtype");
    Objects.requireNonNull(description, "description");
  }

  public static SeriesEntry of(String id, String label) {
    return new SeriesEntry(id, label, List.of(), Optional.empty(), "");
  }

  public SeriesEntry shape(int t, int c, int z, int y, int x) {
    return new SeriesEntry(id, label, List.of(t, c, z, y, x), dtype, description);
  }

  public SeriesEntry dtype(DType d) {
    return new SeriesEntry(id, label, shapeTczyx, Optional.of(d), description);
  }

  public SeriesEntry description(String line) {
    return new SeriesEntry(id, label, shapeTczyx, dtype, line);
  }
}
