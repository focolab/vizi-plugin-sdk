package vizi.plugin.javasdk;

/**
 * What a {@link SeriesReader} answers an open with (reader_writer_contract.md §1.2a): the
 * {@link Dataset} it opened, or a {@link SeriesChoice} of the images the file holds.
 */
public sealed interface Opening permits Dataset, SeriesChoice {}
