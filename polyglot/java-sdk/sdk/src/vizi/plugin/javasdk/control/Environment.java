package vizi.plugin.javasdk.control;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.file.Path;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Optional;

/**
 * The launch environment beyond the two channels (control_plane_protocol.md §2.1, §2.3):
 * manifest-declared assets and the settings folder.
 *
 * <p>{@code VIZI_PLUGIN_ASSETS} is a JSON object {@code {asset_id: absolute path}}, not a
 * directory: an id absent from it is not declared for this plugin, which is a different answer
 * from declared-but-missing. {@code VIZI_PLUGIN_SETTINGS_DIR} names a folder the host did not
 * create; the SDK creates it on first use, and when the variable is absent there is none.
 */
public final class Environment {

  private final Map<String, Path> assets;
  private final Optional<Path> settingsDir;
  private final String assetsProblem;

  private Environment(Map<String, Path> assets, Optional<Path> settingsDir, String assetsProblem) {
    this.assets = Map.copyOf(assets);
    this.settingsDir = settingsDir;
    this.assetsProblem = assetsProblem;
  }

  public static Environment of(Map<String, String> env) {
    Map<String, Path> assets = new LinkedHashMap<>();
    String problem = "";
    String raw = env.get("VIZI_PLUGIN_ASSETS");
    if (raw != null && !raw.isBlank()) {
      try {
        JsonElement e = JsonParser.parseString(raw);
        if (!e.isJsonObject()) {
          problem = "VIZI_PLUGIN_ASSETS is not a JSON object";
        } else {
          JsonObject o = e.getAsJsonObject();
          for (Map.Entry<String, JsonElement> kv : o.entrySet()) {
            if (kv.getValue().isJsonPrimitive() && kv.getValue().getAsJsonPrimitive().isString()) {
              assets.put(kv.getKey(), Path.of(kv.getValue().getAsString()));
            }
          }
        }
      } catch (RuntimeException ex) {
        problem = "VIZI_PLUGIN_ASSETS is not valid JSON: " + ex.getMessage();
      }
    }
    String settings = env.get("VIZI_PLUGIN_SETTINGS_DIR");
    Optional<Path> dir = settings == null || settings.isBlank() ? Optional.empty() : Optional.of(Path.of(settings));
    return new Environment(assets, dir, problem);
  }

  /** The published path of a manifest asset, or empty when the host did not publish it. */
  public Optional<Path> asset(String assetId) {
    return Optional.ofNullable(assets.get(assetId));
  }

  public Map<String, Path> assets() {
    return assets;
  }

  /** Why the assets variable could not be read, or empty. */
  public String assetsProblem() {
    return assetsProblem;
  }

  /**
   * The settings folder, created on first use. Empty when the host provided none: the SDK says
   * so rather than inventing one.
   */
  public Optional<Path> settingsDir() throws java.io.IOException {
    if (settingsDir.isEmpty()) return Optional.empty();
    java.nio.file.Files.createDirectories(settingsDir.get());
    return settingsDir;
  }

  /** The settings folder as named, without creating it. */
  public Optional<Path> settingsDirNamed() {
    return settingsDir;
  }
}
