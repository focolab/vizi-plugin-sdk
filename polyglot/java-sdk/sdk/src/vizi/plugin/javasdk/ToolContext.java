package vizi.plugin.javasdk;

import java.util.List;
import java.util.Optional;
import vizi.plugin.v1.ViziPlugin.ToolAction;
import vizi.plugin.v1.ViziPlugin.ToolBinding;

/**
 * What a tool handler can reach. There is deliberately no host client here: a host request from
 * the doorbell listener stalls every tool's input. Start a job instead.
 */
public final class ToolContext implements PluginContext {

  private final Plugin.Runtime rt;
  private final String toolId;
  private final long handle;
  volatile ToolBinding binding = ToolBinding.getDefaultInstance();

  ToolContext(Plugin.Runtime rt, String toolId, long handle) {
    this.rt = rt;
    this.toolId = toolId;
    this.handle = handle;
  }

  /** The manifest id of the tool. */
  public String toolId() {
    return toolId;
  }

  /** The host's handle for this binding (the {@code E} record's {@code tool}). */
  public long handle() {
    return handle;
  }

  /**
   * The host's binding for this tool, as installed: its triggers (what engages it), its mode, and
   * its actions with the keys the host kept for them ({@code ToolAction.key}, empty for none).
   */
  public ToolBinding binding() {
    return binding;
  }

  /** The key the host kept for action {@code actionId}, or empty when it kept none. */
  public java.util.Optional<String> actionKey(String actionId) {
    for (ToolAction a : binding.getActionsList()) {
      if (a.getActionId().equals(actionId) && !a.getKey().isEmpty()) return java.util.Optional.of(a.getKey());
    }
    return java.util.Optional.empty();
  }

  /**
   * Moves the view in answer to a FIRE of this tool (§8.15): a {@code mode: fire} tool's {@link
   * InputEvent.Fire}, or a declared action's {@link InputEvent.Action}. Its cause is the record's
   * {@code (tool, interaction)}. A press, drag, release or cancel is never a cause, since a view
   * that moves under a drag fights the hand, so it is refused here ({@link
   * IllegalArgumentException}). The targets must be in this tool's {@code view_targets}.
   *
   * <p>It does not block (a tool handler runs on the doorbell listener): the future completes on
   * the dispatch thread with the host's answer, where no host request may be made. The host judges
   * the cause as it judges a panel's (2 s expiry, one answer, superseded by the person's input).
   * The person's switch is the first row of the plugin's panel: the move applies only while that
   * panel is open with the switch on, and is refused {@code NOT_ENABLED} otherwise. Nothing on the
   * wire reports the switch.
   */
  public java.util.concurrent.CompletableFuture<ViewResult> moveView(InputEvent fire, ViewMove move) {
    var r = vizi.plugin.v1.ViziPlugin.ViewRequest.newBuilder().setToolCause(toolCause(fire));
    move.into(r);
    return rt.host().requestViewLater(r.build());
  }

  /** Answers a FIRE's cause and moves nothing; see {@link #moveView}. */
  public java.util.concurrent.CompletableFuture<ViewResult> declineView(InputEvent fire) {
    return rt.host().requestViewLater(
        vizi.plugin.v1.ViziPlugin.ViewRequest.newBuilder().setToolCause(toolCause(fire)).setDecline(true).build());
  }

  private vizi.plugin.v1.ViziPlugin.ToolCause toolCause(InputEvent ev) {
    InputEvent.Info info = switch (ev) {
      case InputEvent.Fire f -> f.info();
      case InputEvent.Action a -> a.info();
      default -> throw new IllegalArgumentException("only a FIRE is a cause (a " + ev.getClass().getSimpleName().toLowerCase(java.util.Locale.ROOT)
          + " is not: a view that moves during a gesture fights the hand doing it)");
    };
    if (info.tool() != handle) {
      throw new IllegalArgumentException("that FIRE is tool handle " + info.tool() + "'s, not this tool's (" + handle + ")");
    }
    return vizi.plugin.v1.ViziPlugin.ToolCause.newBuilder().setTool((int) info.tool()).setInteraction((int) info.interaction()).build();
  }

  @Override
  public java.util.Optional<Appearance> appearance() {
    return rt.appearance();
  }

  @Override
  public Overlay overlay(String id) {
    return rt.overlay(id);
  }

  @Override
  public ChunkedOverlay chunked(String id) {
    return rt.chunked(id);
  }

  @Override
  public Optional<java.nio.file.Path> asset(String assetId) {
    return rt.asset(assetId);
  }

  @Override
  public Optional<java.nio.file.Path> settingsDir() throws java.io.IOException {
    return rt.settingsDir();
  }

  @Override
  public Panel panel(String id) {
    return rt.panel(id);
  }

  @Override
  public Jobs jobs() {
    return rt.jobs();
  }

  @Override
  public Task startTask(String title, boolean cancellable) {
    return rt.startTask(title, cancellable, null);
  }

  @Override
  public Optional<PaneContext> pane(int viewId) {
    return rt.pane(viewId);
  }

  @Override
  public List<PaneContext> panes() {
    return rt.panes();
  }

  @Override
  public void log(String text) {
    rt.log(text);
  }
}
