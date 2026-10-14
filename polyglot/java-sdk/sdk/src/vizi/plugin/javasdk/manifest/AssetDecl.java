package vizi.plugin.javasdk.manifest;

import java.util.Locale;
import java.util.Objects;

/**
 * An immutable file the host provisions and verifies (§4); the plugin asks for it at run time
 * with {@code RequestAsset}.
 *
 * @param id starts alphanumeric, then letters, digits, {@code .}, {@code _}, {@code -}
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param filename one plain file name
 * @param size the exact byte count, positive
 * @param sha256 64 hex characters, stored lower-case
 */
public record AssetDecl(
    String id, String name, String filename, long size, String sha256, Source source) {
  public AssetDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(filename, "filename");
    sha256 = sha256.toLowerCase(Locale.ROOT);
    Objects.requireNonNull(source, "source");
  }

  /** Where the host obtains the file. */
  public sealed interface Source permits Manual, Https {}

  /** The user supplies the file. */
  public record Manual() implements Source {}

  /** The host downloads it from an {@code https://} URL with a host and no credentials. */
  public record Https(String url) implements Source {
    public Https {
      Objects.requireNonNull(url, "url");
    }
  }

  public static AssetDecl manual(String id, String filename, long size, String sha256) {
    return new AssetDecl(id, id, filename, size, sha256, new Manual());
  }

  public static AssetDecl https(String id, String filename, long size, String sha256, String url) {
    return new AssetDecl(id, id, filename, size, sha256, new Https(url));
  }

  public AssetDecl withName(String name) {
    return new AssetDecl(id, name, filename, size, sha256, source);
  }
}
