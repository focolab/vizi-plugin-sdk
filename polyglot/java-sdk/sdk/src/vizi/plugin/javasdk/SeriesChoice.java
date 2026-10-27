package vizi.plugin.javasdk;

import java.util.List;
import java.util.Objects;
import java.util.Optional;

/**
 * A file's images, for the person to choose from (reader_writer_contract.md §1.2a). A
 * {@link SeriesReader} answers an open with one when the file holds several images.
 *
 * <p>The SDK checks it before sending: at least one entry, each shape five positive sizes when
 * given. A choice that is not valid is refused {@code INVALID_ARGUMENT} and closed. A choice of one
 * entry is opened at once rather than sent.
 *
 * <p><b>The listing opens the choice.</b> What you opened to list the file may stay open: give an
 * {@link #openingWith opener}, and the SDK holds this choice until the host's next {@code
 * OpenReader}. When that one names a series of the same file and reader, the opener opens it, and
 * whatever the dataset needs of the listing is then the dataset's to close. Any other open, ten idle
 * minutes, or the session's end retires the choice instead, running its {@link #onClose closer}
 * (which also runs when the opener fails). A closer that throws is noted, never raised. Without an
 * opener, the choice is opened by calling the reader again with the series set, and then closed.
 * The SDK runs a closer at most once.
 */
public final class SeriesChoice implements Opening, AutoCloseable {
  /** Opens one listed image on what the listing holds. */
  @FunctionalInterface
  public interface Opener {
    Dataset open(String series) throws Exception;
  }

  private final List<SeriesEntry> entries;
  private final Optional<Opener> opener;
  private final Runnable closer;

  private SeriesChoice(List<SeriesEntry> entries, Optional<Opener> opener, Runnable closer) {
    this.entries = List.copyOf(entries);
    this.opener = opener;
    this.closer = closer;
  }

  /** In the order a person is shown them. */
  public static SeriesChoice of(List<SeriesEntry> entries) {
    return new SeriesChoice(entries, Optional.empty(), () -> {});
  }

  public static SeriesChoice of(SeriesEntry... entries) {
    return of(List.of(entries));
  }

  /** The same choice, opened by {@code opener} on what the listing holds. */
  public SeriesChoice openingWith(Opener opener) {
    return new SeriesChoice(entries, Optional.of(Objects.requireNonNull(opener, "opener")), closer);
  }

  /** The same choice, with {@code closer} run when it is retired without opening anything. */
  public SeriesChoice onClose(Runnable closer) {
    return new SeriesChoice(entries, opener, Objects.requireNonNull(closer, "closer"));
  }

  public List<SeriesEntry> entries() {
    return entries;
  }

  public Optional<Opener> opener() {
    return opener;
  }

  /** Runs the closer. */
  @Override
  public void close() {
    closer.run();
  }
}
