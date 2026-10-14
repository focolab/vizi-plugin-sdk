package vizi.plugin.javasdk.manifest;

import java.util.Arrays;
import java.util.List;
import java.util.Locale;
import java.util.Objects;

/**
 * What engages a tool (§3.6.1): a request the host honours or reports, never an assignment.
 * Exactly one of a key or a mouse button.
 */
public sealed interface TriggerDecl permits TriggerDecl.Key, TriggerDecl.Mouse {

  Hold hold();

  /**
   * A key trigger; the portable key name carries its own modifiers ({@code "Ctrl+J"}).
   *
   * @param hold {@link Hold#TOGGLE} unless the manifest says momentary
   */
  record Key(String key, Hold hold) implements TriggerDecl {
    public Key {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(hold, "hold");
    }
  }

  /**
   * A mouse chord; always momentary. The bare left button, and Alt, are never triggers.
   *
   * @param modifiers de-duplicated and kept in {@link Modifier} order
   */
  record Mouse(MouseButton button, List<Modifier> modifiers) implements TriggerDecl {
    public Mouse {
      Objects.requireNonNull(button, "button");
      modifiers = modifiers.stream().distinct().sorted().toList();
    }

    @Override
    public Hold hold() {
      return Hold.MOMENTARY;
    }
  }

  enum MouseButton implements WireName {
    RIGHT("right"),
    MIDDLE("middle"),
    /** Only with modifiers: the bare left button is the primary. */
    LEFT("left");

    private final String wire;

    MouseButton(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  enum Modifier implements WireName {
    SHIFT("shift"),
    CTRL("ctrl"),
    META("meta");

    private final String wire;

    Modifier(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  static Key key(String key) {
    return new Key(key, Hold.TOGGLE);
  }

  static Key momentaryKey(String key) {
    return new Key(key, Hold.MOMENTARY);
  }

  static Mouse mouse(MouseButton button, Modifier... modifiers) {
    return new Mouse(button, Arrays.asList(modifiers));
  }

  /**
   * How two key names are compared for duplicates and for action/trigger clashes: without regard
   * to case (§8's {@code "T"} / {@code "t"} fixture). The spec says nothing about modifier order
   * within a name, so {@code "Ctrl+Shift+J"} and {@code "Shift+Ctrl+J"} are different here.
   */
  static String keyIdentity(String key) {
    return key.toLowerCase(Locale.ROOT);
  }
}
