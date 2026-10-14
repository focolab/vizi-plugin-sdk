package vizi.plugin.javasdk.manifest;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.OptionalInt;

/**
 * A live interaction mode (§3.6). While engaged it owns the primary button in its {@code views}.
 * {@code mode}, {@code cancel_on_camera_move}, {@code wants_pick}, {@code triggers}, {@code actions}
 * and {@code pane} need {@code manifest_version} 1 when they differ from their defaults.
 *
 * @param id unique among tools
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param views de-duplicated, in {@link View} order; default both
 * @param mode {@link Mode#GESTURE} (default) or {@link Mode#FIRE}
 * @param cancelOnCameraMove default true
 * @param wantsPick default true, which is every tool before the field existed: the host computes
 *     its surface pick for this tool's 3D presses (doorbell_E_input.md §3). False
 *     saves the host a trace per press for a tool that resolves its own anchor from the ray. It is
 *     a REQUEST about what the host computes, not a promise about what arrives: a tool takes a
 *     record with or without a pick whichever it declared
 * @param triggers requests for what engages the tool
 * @param actions at most 64; an action's wire index is its position here plus one
 * @param viewTargets what a FIRE of this tool may move (set-view, §8.15); distinct
 */
public record ToolDecl(
    String id,
    String name,
    List<View> views,
    Menu menu,
    Mode mode,
    boolean cancelOnCameraMove,
    boolean wantsPick,
    List<TriggerDecl> triggers,
    List<ActionDecl> actions,
    Optional<ToolPane> pane,
    List<ViewTarget> viewTargets) {

  /** The most actions one tool may declare (§3.6.2). */
  public static final int MAX_ACTIONS = 64;

  public static final List<View> DEFAULT_VIEWS = List.of(View.TWO_D, View.THREE_D);

  public ToolDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    views = views.stream().distinct().sorted().toList();
    Objects.requireNonNull(menu, "menu");
    Objects.requireNonNull(mode, "mode");
    triggers = List.copyOf(triggers);
    actions = List.copyOf(actions);
    Objects.requireNonNull(pane, "pane");
    viewTargets = List.copyOf(viewTargets);
    if (viewTargets.stream().distinct().count() != viewTargets.size()) {
      throw new IllegalArgumentException("tool " + id + "'s view_targets must be distinct");
    }
  }

  public ToolDecl(String id, String name, List<View> views, Menu menu, Mode mode, boolean cancelOnCameraMove,
      List<TriggerDecl> triggers, List<ActionDecl> actions, Optional<ToolPane> pane) {
    this(id, name, views, menu, mode, cancelOnCameraMove, true, triggers, actions, pane, List.of());
  }

  /** As declared before {@code wants_pick} existed: the tool wants the host's pick. */
  public ToolDecl(String id, String name, List<View> views, Menu menu, Mode mode, boolean cancelOnCameraMove,
      List<TriggerDecl> triggers, List<ActionDecl> actions, Optional<ToolPane> pane, List<ViewTarget> viewTargets) {
    this(id, name, views, menu, mode, cancelOnCameraMove, true, triggers, actions, pane, viewTargets);
  }

  public enum View implements WireName {
    TWO_D("2d"),
    THREE_D("3d");

    private final String wire;

    View(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  public enum Mode implements WireName {
    /** Press / drag / release reach the plugin as {@code E} records. */
    GESTURE("gesture"),
    /** Each press reaches the plugin as one {@code FIRE}. */
    FIRE("fire");

    private final String wire;

    Mode(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  /** A gesture tool in both views, listed in the Tools menu, with nothing else declared. */
  public static ToolDecl of(String id) {
    return new ToolDecl(
        id, id, DEFAULT_VIEWS, Menu.TOOLS, Mode.GESTURE, true, true, List.of(), List.of(), Optional.empty(),
        List.of());
  }

  public ToolDecl withName(String name) {
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, viewTargets);
  }

  public ToolDecl withViews(View... views) {
    return new ToolDecl(
        id, name, Arrays.asList(views), menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, viewTargets);
  }

  public ToolDecl withMenu(Menu menu) {
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, viewTargets);
  }

  public ToolDecl withMode(Mode mode) {
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, viewTargets);
  }

  public ToolDecl withCancelOnCameraMove(boolean cancel) {
    return new ToolDecl(id, name, views, menu, mode, cancel, wantsPick, triggers, actions, pane, viewTargets);
  }

  /**
   * {@code false} tells the host not to compute its surface pick for this tool's 3D presses (E
   * §3): for a tool that resolves its own anchor from the ray, the pick is a trace per press nobody
   * reads. The default wants it. Either way a handler must take what arrives, a ray with a pick or
   * without one, because the declaration is a request about what the host computes, not a filter
   * on the wire: a host that predates the field always picks, and a host that picks may still find
   * nothing and send the bare ray. {@code InputEvent.Info.ray()} is the ray under either, and
   * {@code InputEvent.Info.pick()} the pick or empty.
   */
  public ToolDecl withWantsPick(boolean wantsPick) {
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, viewTargets);
  }

  public ToolDecl withTrigger(TriggerDecl trigger) {
    List<TriggerDecl> t = new ArrayList<>(triggers);
    t.add(trigger);
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, t, actions, pane, viewTargets);
  }

  public ToolDecl withAction(ActionDecl action) {
    List<ActionDecl> a = new ArrayList<>(actions);
    a.add(action);
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, a, pane, viewTargets);
  }

  public ToolDecl withPane(ToolPane pane) {
    return new ToolDecl(
        id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, Optional.of(pane), viewTargets);
  }

  /** What a FIRE of this tool may move. */
  public ToolDecl withViewTargets(ViewTarget... targets) {
    return new ToolDecl(id, name, views, menu, mode, cancelOnCameraMove, wantsPick, triggers, actions, pane, List.of(targets));
  }

  /** The action a {@code FIRE} record names by its 1-based {@code action} index. */
  public Optional<ActionDecl> action(int oneBasedIndex) {
    return oneBasedIndex >= 1 && oneBasedIndex <= actions.size()
        ? Optional.of(actions.get(oneBasedIndex - 1))
        : Optional.empty();
  }

  /** The 1-based wire index of the action with this id. */
  public OptionalInt actionIndex(String actionId) {
    for (int i = 0; i < actions.size(); i++) {
      if (actions.get(i).id().equals(actionId)) return OptionalInt.of(i + 1);
    }
    return OptionalInt.empty();
  }

  /** Whether any field needs {@code manifest_version} 1 (§3.6). */
  public boolean needsVersion1() {
    return mode != Mode.GESTURE
        || !cancelOnCameraMove
        || !wantsPick
        || !triggers.isEmpty()
        || !actions.isEmpty()
        || pane.isPresent();
  }
}
