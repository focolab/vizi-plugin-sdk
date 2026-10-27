package vizi.plugin.javasdk;

import java.util.Objects;
import vizi.plugin.v1.ViziPlugin.WidgetGeometry;

/**
 * How large an {@code image} widget is drawn (control_plane_protocol.md §8.4): {@code widthPx} by
 * {@code heightPx} device-independent pixels, and the screen's device-pixel ratio {@code dpr}. An
 * image of {@link #pixels()} is shown one image pixel per screen pixel; any other is scaled to fit.
 * It is the space the widget is given, never the image's own size, so redrawing at it cannot grow
 * the panel. A click's {@code x} and {@code y} are in the pushed image's own pixels either way.
 */
public record WidgetSize(String panelId, String widgetId, int widthPx, int heightPx, double dpr) {

  /** An image size in pixels. */
  public record Pixels(int width, int height) {}

  public WidgetSize {
    Objects.requireNonNull(panelId, "panelId");
    Objects.requireNonNull(widgetId, "widgetId");
  }

  /**
   * The image size drawn 1:1: {@code widthPx × dpr} by {@code heightPx × dpr}, each rounded half
   * to even as the reference rounds.
   */
  public Pixels pixels() {
    return new Pixels(scaled(widthPx), scaled(heightPx));
  }

  private int scaled(int px) {
    double v = Math.rint(px * dpr);
    return v >= Integer.MAX_VALUE ? Integer.MAX_VALUE : (int) v;
  }

  /**
   * From the wire. A {@code dpr} of 0, the field unset, reads as 1, as the reference reads it; so
   * does one that is not a positive finite number, which no screen has. A size past {@code
   * Integer.MAX_VALUE} saturates.
   */
  static WidgetSize of(WidgetGeometry g) {
    double dpr = g.getDpr();
    if (!(dpr > 0) || !Double.isFinite(dpr)) dpr = 1.0;
    return new WidgetSize(g.getPanelId(), g.getWidgetId(), saturate(g.getWidthPx()), saturate(g.getHeightPx()), dpr);
  }

  private static int saturate(int unsigned) {
    long v = Integer.toUnsignedLong(unsigned);
    return v > Integer.MAX_VALUE ? Integer.MAX_VALUE : (int) v;
  }
}
