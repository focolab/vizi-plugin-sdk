package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Optional;

/**
 * What the engaged tool can be told mid-gesture (§3.6.2). It reaches the tool as an {@code E}
 * {@code FIRE} record whose {@code action} is this entry's 1-based index in {@link
 * ToolDecl#actions()}. Needs a key or a pane button.
 *
 * @param id unique among the tool's actions
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param key live only while the tool is engaged
 */
public record ActionDecl(String id, String name, Optional<String> key, Optional<PaneButton> pane) {
  public ActionDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(key, "key");
    Objects.requireNonNull(pane, "pane");
  }

  public static ActionDecl onKey(String id, String key) {
    return new ActionDecl(id, id, Optional.of(key), Optional.empty());
  }

  public static ActionDecl onPane(String id, PaneButton pane) {
    return new ActionDecl(id, id, Optional.empty(), Optional.of(pane));
  }

  public ActionDecl withName(String name) {
    return new ActionDecl(id, name, key, pane);
  }

  public ActionDecl withKey(String key) {
    return new ActionDecl(id, name, Optional.of(key), pane);
  }

  public ActionDecl withPane(PaneButton pane) {
    return new ActionDecl(id, name, key, Optional.of(pane));
  }
}
