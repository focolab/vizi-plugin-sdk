package vizi.plugin.javasdk;

import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.OptionalLong;
import java.util.concurrent.TimeoutException;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * What a command handler (or a panel-opened hook) can reach: everything a tool can, plus the
 * host client and the command's arguments. It runs on the command worker, one at a time, and holds
 * the session's in-flight slot while it runs (§8.16). The host shows it to the person, who can
 * cancel it: report {@link #progress} and check {@link #cancelled} between steps. Work that should
 * not hold the slot goes to a job or a {@link Task}.
 */
public final class CommandContext implements PluginContext {

  private final Plugin.Runtime rt;
  private final String commandId;
  private final Map<String, Value> args;
  private final @Nullable ViewCause cause;

  CommandContext(Plugin.Runtime rt, String commandId, Map<String, Value> args) {
    this(rt, commandId, args, null);
  }

  CommandContext(Plugin.Runtime rt, String commandId, Map<String, Value> args, @Nullable ViewCause cause) {
    this.rt = rt;
    this.commandId = commandId;
    this.args = Map.copyOf(args);
    this.cause = cause;
  }

  /** The command's id; empty for a panel-opened hook. */
  public String commandId() {
    return commandId;
  }

  /** The row a list cell's event came from ({@code args["row"]}), or empty for any other widget. */
  public Optional<String> row() {
    return string("row");
  }

  /**
   * The set-view cause this invocation carries (§8.15): present for an interaction with a {@code
   * moves_view} widget, empty for every other. Unanswered and not {@link ViewCause#defer()
   * deferred}, it is declined when the command returns.
   */
  public Optional<ViewCause> cause() {
    return Optional.ofNullable(cause);
  }

  /**
   * Moves the view in answer to this invocation's cause. Without one it is refused here ({@link
   * IllegalStateException}): no cause, no move. The host's refusals are returned, not thrown.
   */
  public ViewResult moveView(ViewMove move) throws HostException, TimeoutException, InterruptedException {
    return requireCause().move(move);
  }

  /** Answers this invocation's cause and moves nothing; refused here without one. */
  public ViewResult declineView() throws HostException, TimeoutException, InterruptedException {
    return requireCause().decline();
  }

  private ViewCause requireCause() {
    if (cause == null) {
      throw new IllegalStateException("command " + commandId + " was not invoked with a cause (only a moves_view widget's"
          + " interaction carries one): a plugin moves the view only when the person asked it to");
    }
    return cause;
  }

  /**
   * A clickable image's click: the pixel in the image's own coordinates, (0,0) at top left;
   * {@code button} {@code "left"}, {@code "middle"} or {@code "right"}; {@code modifiers} such as
   * {@code "shift+ctrl"}, or {@code ""}; and {@code image}, the offset of the {@code ImageRef} the
   * widget was showing, so the pixel is placed on the right image even after a newer push.
   */
  public record ImageClick(int x, int y, String button, String modifiers, long image) {}

  /** This invocation as an image click, when it carries {@code x} and {@code y}. */
  public Optional<ImageClick> imageClick() {
    OptionalDouble x = number("x");
    OptionalDouble y = number("y");
    if (x.isEmpty() || y.isEmpty()) return Optional.empty();
    return Optional.of(new ImageClick((int) Math.floor(x.getAsDouble()), (int) Math.floor(y.getAsDouble()),
        string("button").orElse("left"), string("modifiers").orElse(""), (long) number("image").orElse(0)));
  }

  /** The raw arguments. */
  public Map<String, Value> args() {
    return args;
  }

  public Optional<String> string(String key) {
    Value v = args.get(key);
    return v != null && v.hasS() ? Optional.of(v.getS()) : Optional.empty();
  }

  /** A number argument, whether the host sent it as a float or an integer. */
  public OptionalDouble number(String key) {
    Value v = args.get(key);
    if (v == null) return OptionalDouble.empty();
    if (v.hasF()) return OptionalDouble.of(v.getF());
    if (v.hasI()) return OptionalDouble.of(v.getI());
    return OptionalDouble.empty();
  }

  public OptionalLong integer(String key) {
    Value v = args.get(key);
    return v != null && v.hasI() ? OptionalLong.of(v.getI()) : OptionalLong.empty();
  }

  /** A list of whole numbers ({@code Value.ints}): a multi-select channel picker's indices. */
  public Optional<List<Long>> ints(String key) {
    Value v = args.get(key);
    return v != null && v.hasInts() ? Optional.of(List.copyOf(v.getInts().getValuesList())) : Optional.empty();
  }

  /**
   * A list of strings ({@code Value.strings}): a table's selected row keys, {@code strings("rows")};
   * a cleared selection is {@code Value.strings} with no entries.
   */
  public Optional<List<String>> strings(String key) {
    Value v = args.get(key);
    return v != null && v.hasStrings() ? Optional.of(List.copyOf(v.getStrings().getValuesList())) : Optional.empty();
  }

  /** An argument as a plain Java value: String, Double, Long, Boolean, List of Long or of String, or empty. */
  public Optional<Object> value(String key) {
    Value v = args.get(key);
    return v == null ? Optional.empty() : Values.toJava(v);
  }

  public Optional<Boolean> flag(String key) {
    Value v = args.get(key);
    return v != null && v.hasB() ? Optional.of(v.getB()) : Optional.empty();
  }

  /** The host client. */
  public Host host() {
    return rt.host();
  }

  @Override
  public Optional<Appearance> appearance() {
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

  /**
   * How far along this command is, {@code fraction} in [0, 1], and what it is doing now (one line;
   * the host shows the latest). Nothing for a panel-opened hook, which answers no request.
   */
  public void progress(double fraction, String text) {
    CurrentWork.progress(fraction, text);
  }

  /** What this command is doing now, with no measure of how far along. */
  public void progress(String text) {
    CurrentWork.progress(text);
  }

  /** Whether the person canceled this command; stop by throwing {@link Cancelled}. */
  public boolean cancelled() {
    return CurrentWork.cancelled();
  }

  /** Throws {@link Cancelled} once the person canceled this command. */
  public void checkCancelled() {
    CurrentWork.checkCancelled();
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
