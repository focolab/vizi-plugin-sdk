package vizi.plugin.javasdk;

import java.nio.file.Path;
import java.util.Optional;

/**
 * A reader whose files may hold several images (reader_writer_contract.md §1.2a): an NWB
 * file's image series, an OME-Zarr's numbered series, an OME-TIFF's images. Runs on the work
 * worker, like {@link Reader}, and refuses the same way.
 *
 * <p>With {@code series} empty, open the file when it holds one image, or answer a {@link
 * SeriesChoice} listing them; the host asks the person and opens again with the chosen id. (A
 * reader may instead open a default of its own when one is honestly the image a person means.) A
 * choice of one entry is not sent: the SDK opens that entry at once. With {@code series} present,
 * open that one: an id you do not have is {@link Refusal#notFound}, which is how a stale
 * recent-files entry learns its image is gone, and answering a named series with a choice is
 * refused {@code INVALID_ARGUMENT}. A named series can arrive with no listing before it.
 */
@FunctionalInterface
public interface SeriesReader {
  /**
   * Opens {@code uri}, lists its images, or opens the one {@code series} names.
   *
   * @param series the id from one of your {@link SeriesEntry entries}, or empty to open or list;
   *     {@code ""} is an id like any other
   */
  Opening open(String uri, Path fileStateDir, Optional<String> series) throws Exception;
}
