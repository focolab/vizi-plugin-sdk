package vizi.plugin.javasdk;

import java.nio.file.Path;

/**
 * Opens a URI as a {@link Dataset} (reader_writer_contract.md §1). Runs on the work worker. Throw
 * {@link Refusal} to choose the reply's kind ({@code NOT_FOUND}, {@code UNSUPPORTED}, …); any
 * other exception answers {@code IO_ERROR} with its message. A reader whose files may hold several
 * images is a {@link SeriesReader}; this one opens every file as its one image, whatever series
 * the host names.
 */
@FunctionalInterface
public interface Reader {
  /**
   * @param fileStateDir where this plugin may keep what it remembers about this file (it may
   *     not exist yet; create it if you write there)
   */
  Dataset open(String uri, Path fileStateDir) throws Exception;
}
