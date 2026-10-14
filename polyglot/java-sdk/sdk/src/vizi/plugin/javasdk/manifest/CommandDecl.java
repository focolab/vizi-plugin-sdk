package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Optional;

/**
 * A command: an addressable behaviour that panel widgets, the Tools menu and a pane button fire
 * (§3.2).
 *
 * @param id unique among commands
 * @param title the menu / palette caption
 * @param description empty when the manifest gives none
 * @param defaultKeybinding a request; the host resolves it and a core binding wins
 * @param menu {@link Menu#TOOLS} unless the manifest says {@code null}
 * @param pane a momentary button on the control pane; needs {@code manifest_version} 1
 */
public record CommandDecl(
    String id,
    String title,
    String description,
    Optional<String> defaultKeybinding,
    Menu menu,
    Optional<PaneButton> pane) {
  public CommandDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(title, "title");
    Objects.requireNonNull(description, "description");
    Objects.requireNonNull(defaultKeybinding, "defaultKeybinding");
    Objects.requireNonNull(menu, "menu");
    Objects.requireNonNull(pane, "pane");
  }

  public static CommandDecl of(String id, String title) {
    return new CommandDecl(id, title, "", Optional.empty(), Menu.TOOLS, Optional.empty());
  }

  public CommandDecl withDescription(String description) {
    return new CommandDecl(id, title, description, defaultKeybinding, menu, pane);
  }

  public CommandDecl withKeybinding(String keybinding) {
    return new CommandDecl(id, title, description, Optional.of(keybinding), menu, pane);
  }

  public CommandDecl withMenu(Menu menu) {
    return new CommandDecl(id, title, description, defaultKeybinding, menu, pane);
  }

  /** Registers the command without listing it in the Tools menu ({@code "menu": null}). */
  public CommandDecl unlisted() {
    return withMenu(Menu.NONE);
  }

  public CommandDecl withPane(PaneButton pane) {
    return new CommandDecl(id, title, description, defaultKeybinding, menu, Optional.of(pane));
  }
}
