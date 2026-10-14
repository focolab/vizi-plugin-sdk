package vizi.plugin.javasdk.manifest;

import java.util.Objects;

/**
 * An exporter: "I write this artifact format" (§3.4).
 *
 * @param id unique among exporters
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param extension normalised like a reader's: lower case, one leading dot
 */
public record ExporterDecl(String id, String name, String extension) {
  public ExporterDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    extension = ReaderDecl.normaliseExtension(extension);
  }

  public static ExporterDecl of(String id, String extension) {
    return new ExporterDecl(id, id, extension);
  }

  public ExporterDecl withName(String name) {
    return new ExporterDecl(id, name, extension);
  }
}
