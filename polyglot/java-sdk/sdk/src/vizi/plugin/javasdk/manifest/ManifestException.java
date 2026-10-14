package vizi.plugin.javasdk.manifest;

/**
 * A manifest the validator refuses. The message is the whole diagnostic: it names the manifest
 * file, the location within it, and the rule broken ({@code manifest_schema.md} §1: "every
 * rejection is a diagnostic naming the manifest and the field").
 */
public final class ManifestException extends Exception {
  private static final long serialVersionUID = 1L;

  public ManifestException(String diagnostic) {
    super(diagnostic);
  }

  public ManifestException(String diagnostic, Throwable cause) {
    super(diagnostic, cause);
  }
}
