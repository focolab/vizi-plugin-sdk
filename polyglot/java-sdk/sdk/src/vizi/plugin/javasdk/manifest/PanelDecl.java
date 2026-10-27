package vizi.plugin.javasdk.manifest;

import java.util.List;
import java.util.Objects;
import java.util.Optional;
import org.jspecify.annotations.Nullable;

/**
 * A declarative side panel the host builds (§3.3).
 *
 * @param id unique among panels; also a {@code P} grant's subject
 * @param title the dock title
 * @param menuLabel non-blank, at most 128 characters
 * @param root the widget tree
 * @param viewTargets what its {@code moves_view} widgets may move (set-view, §8.15); distinct, and
 *     required when any widget is {@code moves_view}
 * @param followTargets what a Follow the person starts from this panel may drive (§8.15); distinct
 */
public record PanelDecl(
    String id, String title, Optional<String> menuLabel, WidgetDecl root, List<ViewTarget> viewTargets, List<ViewTarget> followTargets) {
  public PanelDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(title, "title");
    Objects.requireNonNull(menuLabel, "menuLabel");
    Objects.requireNonNull(root, "root");
    viewTargets = List.copyOf(viewTargets);
    if (viewTargets.stream().distinct().count() != viewTargets.size()) {
      throw new IllegalArgumentException("panel " + id + "'s view_targets must be distinct");
    }
    followTargets = List.copyOf(followTargets);
    if (followTargets.stream().distinct().count() != followTargets.size()) {
      throw new IllegalArgumentException("panel " + id + "'s follow_targets must be distinct");
    }
    String cells = listCellsProblem(id, root);
    if (cells != null) throw new IllegalArgumentException(cells);
  }

  /**
   * Why the panel's lists TOGETHER could realize more than {@value WidgetDecl.RowList#MAX_CELLS}
   * cells (§8.4), or null: the host builds every row one patch asks for on its UI
   * thread, and one patch may fill every list at once. Each list alone is bounded by {@link
   * WidgetDecl.RowList}.
   */
  public static @Nullable String listCellsProblem(String id, WidgetDecl root) {
    long total = 0;
    for (WidgetDecl w : root.flatten()) {
      if (w instanceof WidgetDecl.RowList l) total += (long) l.maxRows() * l.row().size();
    }
    if (total <= WidgetDecl.RowList.MAX_CELLS) return null;
    // the reference's wording, which a §8 fixture pins
    return "panel " + Wire.quote(id) + "'s lists may realize at most " + WidgetDecl.RowList.MAX_CELLS
        + " cells together (each list's max_rows × row cells), not " + total;
  }

  public PanelDecl(String id, String title, Optional<String> menuLabel, WidgetDecl root) {
    this(id, title, menuLabel, root, List.of(), List.of());
  }

  public PanelDecl(String id, String title, Optional<String> menuLabel, WidgetDecl root, List<ViewTarget> viewTargets) {
    this(id, title, menuLabel, root, viewTargets, List.of());
  }

  public static PanelDecl of(String id, String title, WidgetDecl root) {
    return new PanelDecl(id, title, Optional.empty(), root);
  }

  public PanelDecl withMenuLabel(String menuLabel) {
    return new PanelDecl(id, title, Optional.of(menuLabel), root, viewTargets, followTargets);
  }

  /** What this panel's {@code moves_view} widgets may move. */
  public PanelDecl withViewTargets(ViewTarget... targets) {
    return new PanelDecl(id, title, menuLabel, root, List.of(targets), followTargets);
  }

  /** What a Follow the person starts from this panel may drive. */
  public PanelDecl withFollowTargets(ViewTarget... targets) {
    return new PanelDecl(id, title, menuLabel, root, viewTargets, List.of(targets));
  }

  /** Every widget of the tree, root first, in document order. */
  public List<WidgetDecl> widgets() {
    return root.flatten();
  }

  public Optional<WidgetDecl> widget(String widgetId) {
    return widgets().stream().filter(w -> w.id().equals(widgetId)).findFirst();
  }

  /** The widgets that fire a command, in document order. */
  public List<WidgetDecl.Interactive> interactiveWidgets() {
    return widgets().stream()
        .filter(w -> w instanceof WidgetDecl.Interactive)
        .map(w -> (WidgetDecl.Interactive) w)
        .toList();
  }

  /**
   * The widget count the host sizes its queue from (§3.3.1): each widget once, and a list's
   * template widgets once per possible row.
   */
  public int widgetCount() {
    int n = 0;
    for (WidgetDecl w : root.flatten()) {
      if (w instanceof WidgetDecl.RowList l) n += l.widgetCount() - l.row().size(); // its cells were counted once already
      else n += 1;
    }
    return n;
  }
}
