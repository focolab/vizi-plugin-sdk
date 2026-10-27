package vizi.plugin.javasdk;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import vizi.plugin.v1.ViziPlugin.HostAppearance;

/**
 * The host's theme and the colors it draws panels with (control_plane_protocol.md §8.4,
 * "Appearance"): what a plugin that renders its own pictures (a plot pushed to an {@code image})
 * needs to match them. {@code theme} is {@code "dark"} or {@code "light"}; each color is an
 * {@link Rgba} of 0–255 channels. {@link #hex(String)} gives one as {@code "#rrggbb"}, and
 * {@link #hexColors()} all of them.
 */
public record Appearance(String theme, Rgba windowBg, Rgba panelBg, Rgba text, Rgba textDisabled, Rgba accent, Rgba border) {

  /** The colors' names, in the order the message numbers them. */
  public static final List<String> COLORS = List.of("window_bg", "panel_bg", "text", "text_disabled", "accent", "border");

  /** One color: red, green, blue and alpha, each 0–255. */
  public record Rgba(int r, int g, int b, int a) {

    /** From the wire's {@code 0xRRGGBBAA}. */
    public static Rgba of(int rrggbbaa) {
      return new Rgba((rrggbbaa >>> 24) & 0xFF, (rrggbbaa >>> 16) & 0xFF, (rrggbbaa >>> 8) & 0xFF, rrggbbaa & 0xFF);
    }

    /** {@code "#rrggbb"}, alpha dropped. */
    public String hex() {
      return String.format("#%02x%02x%02x", r, g, b);
    }
  }

  public Appearance {
    Objects.requireNonNull(theme, "theme");
    Objects.requireNonNull(windowBg, "windowBg");
    Objects.requireNonNull(panelBg, "panelBg");
    Objects.requireNonNull(text, "text");
    Objects.requireNonNull(textDisabled, "textDisabled");
    Objects.requireNonNull(accent, "accent");
    Objects.requireNonNull(border, "border");
  }

  /** True when the theme is {@code "dark"}. */
  public boolean dark() {
    return theme.equals("dark");
  }

  /**
   * One color by its wire name ({@link #COLORS}), e.g. {@code color("text_disabled")}.
   *
   * @throws IllegalArgumentException for a name not in {@link #COLORS}
   */
  public Rgba color(String name) {
    return switch (name) {
      case "window_bg" -> windowBg;
      case "panel_bg" -> panelBg;
      case "text" -> text;
      case "text_disabled" -> textDisabled;
      case "accent" -> accent;
      case "border" -> border;
      default -> throw new IllegalArgumentException("no appearance color " + name + "; one of " + COLORS);
    };
  }

  /** One color as {@code "#rrggbb"} (alpha dropped), e.g. {@code hex("text")}. */
  public String hex(String name) {
    return color(name).hex();
  }

  /** Every color as {@code "#rrggbb"}, by wire name, in {@link #COLORS}' order. */
  public Map<String, String> hexColors() {
    var out = new LinkedHashMap<String, String>();
    for (String name : COLORS) out.put(name, hex(name));
    return java.util.Collections.unmodifiableMap(out);
  }

  /** From the wire. */
  static Appearance of(HostAppearance m) {
    return new Appearance(m.getTheme(), Rgba.of(m.getWindowBg()), Rgba.of(m.getPanelBg()), Rgba.of(m.getText()),
        Rgba.of(m.getTextDisabled()), Rgba.of(m.getAccent()), Rgba.of(m.getBorder()));
  }
}
