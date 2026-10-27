package vizi.plugin.javasdk.manifest;

/**
 * A manifest name that arrived with a protocol minor (§5, §5.1). A manifest's name is honored only
 * when its {@code engines.vizi} minor is at least the name's; otherwise it is warned about and
 * ignored, like an unknown name. The minors are append-only within 1.x.
 */
public interface Introduced {
  /** The protocol minor that introduced this name. */
  int introducedMinor();
}
