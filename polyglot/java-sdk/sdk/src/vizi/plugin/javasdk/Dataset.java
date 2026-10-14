package vizi.plugin.javasdk;

import java.util.List;
import java.util.Objects;
import java.util.OptionalDouble;

/**
 * An open dataset (reader_writer_contract.md §1): a {@code (T, C, Z, Y, X)} array read one
 * {@code (t, c)} volume at a time. Calls arrive on the work worker, one at a time. The SDK checks
 * every volume against {@link #info()}'s shape and dtype before depositing it.
 */
public non-sealed interface Dataset extends AutoCloseable, Opening {

  /** The shape, dtype and whatever calibration the file KNOWS: unknown stays unknown. */
  Info info();

  /** The level-0 volume at {@code (t, c)}. */
  Volume volume(int t, int c) throws Exception;

  /** The plane {@code (1, Y, X)} at {@code (t, z, c)}; sliced from {@link #volume} unless overridden. */
  default Volume plane(int t, int z, int c) throws Exception {
    Volume v = volume(t, c);
    long bytes = (long) v.y() * v.x() * v.dtype().size;
    return Volume.of(1, v.y(), v.x(), v.dtype(), v.data().asSlice(z * bytes, bytes));
  }

  /** The metadata document, or {@link Metadata#NONE}. */
  default Metadata metadata() throws Exception {
    return Metadata.NONE;
  }

  @Override
  default void close() {}

  /** A metadata document: {@code form} is {@code "ome-xml"}, {@code "ome-ngff"} or empty. */
  record Metadata(String form, String text) {
    public static final Metadata NONE = new Metadata("", "");

    public Metadata {
      Objects.requireNonNull(form, "form");
      Objects.requireNonNull(text, "text");
    }
  }

  /**
   * What a dataset is. Spacing and time are absent unless the file says: a reader never invents
   * calibration (a 1.0 that is not a measurement is worse than none). {@code channelNames} are the
   * channels' names by index as the file gives them, {@code ""} for one it does not name; the list
   * may stop short of {@code c}, and is empty for a reader that names none (§8.2). A name
   * given here wins over the metadata document's for that channel, and is the file's from then on.
   */
  record Info(
      int t,
      int c,
      int z,
      int y,
      int x,
      DType dtype,
      String axisInference,
      OptionalDouble spacingZ,
      OptionalDouble spacingY,
      OptionalDouble spacingX,
      String spacingUnit,
      OptionalDouble timeIncrement,
      String timeUnit,
      List<String> channelNames) {

    public Info {
      if (t < 1 || c < 1 || z < 1 || y < 1 || x < 1) throw new IllegalArgumentException("every extent is at least 1");
      Objects.requireNonNull(dtype, "dtype");
      Objects.requireNonNull(axisInference, "axisInference");
      Objects.requireNonNull(spacingUnit, "spacingUnit");
      Objects.requireNonNull(timeUnit, "timeUnit");
      channelNames = List.copyOf(channelNames); // no null names; more than c is refused at the open, by the SDK
    }

    public static Info of(int t, int c, int z, int y, int x, DType dtype) {
      return new Info(t, c, z, y, x, dtype, "", OptionalDouble.empty(), OptionalDouble.empty(), OptionalDouble.empty(), "",
          OptionalDouble.empty(), "", List.of());
    }

    /** The voxel pitch, as the file records it, in {@code unit} ({@code "um"}). */
    public Info spacing(double z, double y, double x, String unit) {
      return new Info(t, c, this.z, this.y, this.x, dtype, axisInference, OptionalDouble.of(z), OptionalDouble.of(y),
          OptionalDouble.of(x), unit, timeIncrement, timeUnit, channelNames);
    }

    /** The interval between consecutive t, in {@code unit} ({@code "s"}). */
    public Info timeIncrement(double increment, String unit) {
      return new Info(t, c, z, y, x, dtype, axisInference, spacingZ, spacingY, spacingX, spacingUnit,
          OptionalDouble.of(increment), unit, channelNames);
    }

    /** How the axes were decided: {@code "ome"}, {@code "fallback"}, or the plugin's own word. */
    public Info axisInference(String how) {
      return new Info(t, c, z, y, x, dtype, how, spacingZ, spacingY, spacingX, spacingUnit, timeIncrement, timeUnit, channelNames);
    }

    /** The channels' names by index, {@code ""} for one the file does not name; at most {@code c}. */
    public Info channelNames(List<String> names) {
      return new Info(t, c, z, y, x, dtype, axisInference, spacingZ, spacingY, spacingX, spacingUnit, timeIncrement, timeUnit, names);
    }

    /** As {@link #channelNames(List)}. */
    public Info channelNames(String... names) {
      return channelNames(List.of(names));
    }
  }
}
