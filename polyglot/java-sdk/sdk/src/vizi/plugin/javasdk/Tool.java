package vizi.plugin.javasdk;

/**
 * A tool's handlers (doorbell_E_input.md). Every method has a no-op default; override the ones
 * the tool needs, or {@link #event} to take every event in one exhaustive {@code switch}.
 *
 * <p>All of them run on the doorbell listener, in record order, with a cancellation always
 * before the deactivation that caused it. Return within a few milliseconds: anything longer, and
 * any host request, belongs in a job ({@code ctx.jobs().start(...)}). {@link ToolContext} has no
 * host client for that reason.
 *
 * <p>An event whose context could not be reconciled with the arranged state arrives with {@code
 * info().usable()} false (the violation is already reported); its position is still the host's,
 * but its pane is not known.
 */
public interface Tool {

  /** The tool was engaged: its binding is installed and acknowledged. */
  default void activated(ToolContext ctx) {}

  /** The tool was disengaged or withdrawn; any open gesture was canceled first. */
  default void deactivated(ToolContext ctx) {}

  default void press(ToolContext ctx, InputEvent.Press ev) {}

  default void drag(ToolContext ctx, InputEvent.Drag ev) {}

  default void release(ToolContext ctx, InputEvent.Release ev) {}

  default void fire(ToolContext ctx, InputEvent.Fire ev) {}

  default void action(ToolContext ctx, InputEvent.Action ev) {}

  default void cancel(ToolContext ctx, InputEvent.Cancel ev) {}

  /** Routes {@code ev} to its method; override to handle every event in one place. */
  default void event(ToolContext ctx, InputEvent ev) {
    switch (ev) {
      case InputEvent.Press p -> press(ctx, p);
      case InputEvent.Drag d -> drag(ctx, d);
      case InputEvent.Release r -> release(ctx, r);
      case InputEvent.Fire f -> fire(ctx, f);
      case InputEvent.Action a -> action(ctx, a);
      case InputEvent.Cancel c -> cancel(ctx, c);
    }
  }
}
