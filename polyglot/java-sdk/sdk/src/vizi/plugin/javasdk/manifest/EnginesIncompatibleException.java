package vizi.plugin.javasdk.manifest;

/**
 * A manifest whose {@code engines.vizi} this SDK's protocol does not run: a different major, or a
 * newer minor (§2.1).
 *
 * <p>It is thrown as soon as {@code engines} is read, before anything else in the manifest is, so
 * that a plugin built for a later 1.x is refused for that and not for the first thing that 1.x
 * added (a widget kind this SDK has never heard of). The message is the sentence every
 * implementation says: {@code "<name> <version> needs vizi protocol <engines>; this vizi speaks
 * <major>.<minor>"}, naming the id when the name is missing.
 */
public final class EnginesIncompatibleException extends ManifestException {
  private static final long serialVersionUID = 1L;

  private final String pluginId;
  private final String name;
  private final String version;
  private final String engines;

  public EnginesIncompatibleException(String pluginId, String name, String version, String engines) {
    super(message(name.isEmpty() ? pluginId : name, version, engines));
    this.pluginId = pluginId;
    this.name = name;
    this.version = version;
    this.engines = engines;
  }

  /** The sentence for {@code plugin} (its name, or its id) at {@code version}. */
  public static String message(String plugin, String version, String engines) {
    return plugin
        + " "
        + version
        + " needs vizi protocol "
        + engines
        + "; this vizi speaks "
        + EngineRequirement.PROTOCOL_MAJOR
        + "."
        + EngineRequirement.PROTOCOL_MINOR;
  }

  /** The manifest's {@code id}, or {@code ""}. */
  public String pluginId() {
    return pluginId;
  }

  /** The manifest's {@code name}, or {@code ""}. */
  public String name() {
    return name;
  }

  /** The manifest's {@code version}, or {@code ""}. */
  public String version() {
    return version;
  }

  /** {@code engines.vizi} as written. */
  public String engines() {
    return engines;
  }
}
