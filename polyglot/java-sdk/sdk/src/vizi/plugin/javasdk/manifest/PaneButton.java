package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Optional;

/**
 * A control-pane button on a command or a tool action (§3.6.3). It fires once on click; only a
 * tool's own button ({@link ToolPane}) has a hold.
 *
 * @param icon a path relative to the plugin directory, {@code .svg} or {@code .png}, at most 64 KiB
 * @param tooltip at most 128 characters
 */
public record PaneButton(String icon, Optional<String> tooltip) {
  public PaneButton {
    Objects.requireNonNull(icon, "icon");
    Objects.requireNonNull(tooltip, "tooltip");
  }

  public static PaneButton of(String icon) {
    return new PaneButton(icon, Optional.empty());
  }

  public PaneButton withTooltip(String tooltip) {
    return new PaneButton(icon, Optional.of(tooltip));
  }
}
