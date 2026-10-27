package vizi.plugin.javasdk;

import java.util.List;
import java.util.Optional;

/** What every handler can reach: the plugin's overlays, panels, jobs and the arranged panes. */
public sealed interface PluginContext permits CommandContext, ToolContext {

  /** A declared overlay; throws if {@code id} is not declared. */
  Overlay overlay(String id);

  /** A declared chunked overlay; throws if {@code id} is not one. */
  ChunkedOverlay chunked(String id);

  /**
   * A manifest-declared asset's file, as the host provisioned it at launch
   * ({@code VIZI_PLUGIN_ASSETS}); empty when it provided none. A runtime slot is
   * {@code Host.requestAsset} instead.
   */
  Optional<java.nio.file.Path> asset(String assetId);

  /**
   * The plugin's settings folder ({@code VIZI_PLUGIN_SETTINGS_DIR}), created on first use; empty
   * when the host provided none.
   *
   * @throws java.io.IOException when the folder cannot be created (a file stands there, say)
   */
  Optional<java.nio.file.Path> settingsDir() throws java.io.IOException;

  /** A declared panel; throws if {@code id} is not declared. */
  Panel panel(String id);

  /** The plugin's job runner; throws if the plugin registered none. */
  Jobs jobs();

  /**
   * Begins a background task (§8.16), shown to the person at once with a Cancel: work that should
   * not hold the session's slot, run on a thread of the plugin's own. A {@link Jobs} job is one
   * already. At most 64 are open; the 65th throws {@link IllegalStateException}.
   */
  default Task startTask(String title) {
    return startTask(title, true);
  }

  /** As {@link #startTask(String)}; {@code cancellable} false shows no Cancel. */
  Task startTask(String title, boolean cancellable);

  /** A pane's installed arrangement, if one is. */
  Optional<PaneContext> pane(int viewId);

  /** Every installed pane. */
  List<PaneContext> panes();

  /**
   * The host's theme and colors as it last said this session (§8.4); empty before the first,
   * which the host sends after the session starts and before any panel opens.
   */
  Optional<Appearance> appearance();

  /** A diagnostic line on stderr, which the host keeps in the plugin's log. */
  void log(String text);
}
