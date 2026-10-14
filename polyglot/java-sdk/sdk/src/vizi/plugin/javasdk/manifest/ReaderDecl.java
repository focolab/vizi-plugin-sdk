package vizi.plugin.javasdk.manifest;

import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Optional;
import java.util.stream.Stream;

/**
 * A reader: "I open files with these suffixes" (§3.1). A reader declares at least one suffix across
 * {@link #extensions()} and {@link #fallbackExtensions()}; the host picks among the readers whose
 * suffixes a file name ends with, and never reads the file to decide.
 *
 * @param id unique among readers; the {@code reader_id} {@code OpenReader} is routed by
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param extensions normalised to lower case with one leading dot; may be compound
 *     ({@code .ome.tif})
 * @param fallbackExtensions suffixes this reader can read but should not be picked for unasked (a
 *     generic HDF5 reader beside an NWB one): offered, and can be made the default, but never chosen
 *     automatically over a reader that declared the suffix in {@code extensions}; normalised the same
 *     way
 */
public record ReaderDecl(String id, String name, List<String> extensions, List<String> fallbackExtensions) {
  public ReaderDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    extensions = extensions.stream().map(ReaderDecl::normaliseExtension).toList();
    fallbackExtensions = fallbackExtensions.stream().map(ReaderDecl::normaliseExtension).toList();
  }

  public ReaderDecl(String id, String name, List<String> extensions) {
    this(id, name, extensions, List.of());
  }

  public static ReaderDecl of(String id, String... extensions) {
    return new ReaderDecl(id, id, List.of(extensions));
  }

  public ReaderDecl withName(String name) {
    return new ReaderDecl(id, name, extensions, fallbackExtensions);
  }

  /** The same reader with these fallback suffixes ({@code fallback_extensions}). */
  public ReaderDecl withFallback(String... fallbackExtensions) {
    return new ReaderDecl(id, name, extensions, List.of(fallbackExtensions));
  }

  /**
   * {@code "TIF"}, {@code ".tif"} and {@code "tif"} are all {@code ".tif"} (§3.1). Leading dots
   * collapse to one.
   */
  public static String normaliseExtension(String extension) {
    int i = 0;
    while (i < extension.length() && extension.charAt(i) == '.') i++;
    return "." + extension.substring(i).toLowerCase(Locale.ROOT);
  }

  /**
   * The longest declared suffix (from either list) that {@code path}'s file name ends with,
   * case-insensitively, or empty (§3.1). The name must be longer than the suffix: a file named
   * {@code .tif} is not a {@code .tif} file.
   */
  public Optional<String> match(String path) {
    int slash = Math.max(path.lastIndexOf('/'), path.lastIndexOf('\\'));
    String name = path.substring(slash + 1).toLowerCase(Locale.ROOT);
    return Stream.concat(extensions.stream(), fallbackExtensions.stream())
        .filter(s -> name.length() > s.length() && name.endsWith(s))
        .max((a, b) -> Integer.compare(a.length(), b.length()));
  }

  /** Whether {@code path} names a file this reader is a candidate for: {@link #match} finds a suffix. */
  public boolean claims(String path) {
    return match(path).isPresent();
  }
}
