package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Optional;

/**
 * A tool's own control-pane button (§3.6.3).
 *
 * @param icon a path relative to the plugin directory, {@code .svg} or {@code .png}, at most 64 KiB
 * @param tooltip at most 128 characters
 * @param hold {@link Hold#TOGGLE} (the default) is sticky; {@link Hold#MOMENTARY} engages the tool
 *     for one gesture
 */
public record ToolPane(String icon, Optional<String> tooltip, Hold hold) {
  public ToolPane {
    Objects.requireNonNull(icon, "icon");
    Objects.requireNonNull(tooltip, "tooltip");
    Objects.requireNonNull(hold, "hold");
  }

  public static ToolPane of(String icon) {
    return new ToolPane(icon, Optional.empty(), Hold.TOGGLE);
  }

  public ToolPane withTooltip(String tooltip) {
    return new ToolPane(icon, Optional.of(tooltip), hold);
  }

  public ToolPane withHold(Hold hold) {
    return new ToolPane(icon, tooltip, hold);
  }
}
