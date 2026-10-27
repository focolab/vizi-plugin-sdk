package vizi.plugin.javasdk.manifest;

import java.util.List;
import java.util.Objects;

/**
 * An accepted manifest and what the parser warned about on the way (unknown permissions and
 * subscriptions, duplicate JSON keys). Warnings are data here, never printed.
 */
public record ParseResult(Manifest manifest, List<String> warnings) {
  public ParseResult {
    Objects.requireNonNull(manifest, "manifest");
    warnings = List.copyOf(warnings);
  }
}
