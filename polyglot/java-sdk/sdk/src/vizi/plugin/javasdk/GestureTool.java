package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.List;

/**
 * A tool that follows one gesture at a time: press → drag… → release, or a cancel. It keeps the
 * gesture's usable events in order, so a subclass sees the whole stroke at its end.
 *
 * <p>Events whose context the SDK could not reconcile ({@code usable} false) are left out of the
 * stroke but still end it when they are its terminal. Runs on the doorbell listener, like any
 * tool.
 */
public abstract class GestureTool implements Tool {

  private long open;
  private final List<InputEvent.Info> stroke = new ArrayList<>();

  /** The gesture began. */
  protected void started(ToolContext ctx, InputEvent.Info at) {}

  /** The gesture moved. */
  protected void moved(ToolContext ctx, InputEvent.Info at, List<InputEvent.Info> soFar) {}

  /** The gesture completed; {@code stroke} is every usable event of it, press first. */
  protected abstract void completed(ToolContext ctx, List<InputEvent.Info> stroke);

  /** The gesture ended any other way; nothing it began should stand. */
  protected void abandoned(ToolContext ctx, List<InputEvent.Info> soFar, String reason) {}

  @Override
  public final void press(ToolContext ctx, InputEvent.Press ev) {
    if (open != 0 && ev.implicitlyClosed() == open) abandoned(ctx, List.copyOf(stroke), "a new gesture closed it");
    stroke.clear();
    open = ev.interaction();
    if (ev.info().usable()) stroke.add(ev.info());
    started(ctx, ev.info());
  }

  @Override
  public final void drag(ToolContext ctx, InputEvent.Drag ev) {
    if (ev.interaction() != open) return;
    if (ev.info().usable()) stroke.add(ev.info());
    moved(ctx, ev.info(), List.copyOf(stroke));
  }

  @Override
  public final void release(ToolContext ctx, InputEvent.Release ev) {
    if (ev.interaction() != open) return;
    if (ev.info().usable()) stroke.add(ev.info());
    List<InputEvent.Info> done = List.copyOf(stroke);
    reset();
    completed(ctx, done);
  }

  @Override
  public final void cancel(ToolContext ctx, InputEvent.Cancel ev) {
    if (ev.interaction() != open) return;
    List<InputEvent.Info> soFar = List.copyOf(stroke);
    reset();
    abandoned(ctx, soFar, ev.reason());
  }

  @Override
  public void deactivated(ToolContext ctx) {
    reset();
  }

  private void reset() {
    open = 0;
    stroke.clear();
  }
}
