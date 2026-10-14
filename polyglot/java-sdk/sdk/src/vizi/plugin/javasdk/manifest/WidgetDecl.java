package vizi.plugin.javasdk.manifest;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Optional;
import java.util.OptionalDouble;

/**
 * A node of a panel's widget tree (§3.3.1). Ids are unique within one panel. Optional fields the
 * spec gives a default for are stored resolved; the rest are {@code Optional}.
 */
public sealed interface WidgetDecl
    permits WidgetDecl.Label,
        WidgetDecl.Image,
        WidgetDecl.Progress,
        WidgetDecl.Container,
        WidgetDecl.Interactive {

  /** The deepest tree a panel may declare, the root at depth 1 (§3.3.1). */
  int MAX_DEPTH = 8;

  /** The most widgets one panel may declare, the root included (§3.3.1). */
  int MAX_WIDGETS = 256;

  String id();

  Kind kind();

  /**
   * Whether an interaction with this widget may move the view (§8.15): each one carries a cause
   * the plugin answers. Allowed on an interactive widget, a selectable list, and an image with a
   * command; the panel must then declare {@code view_targets}.
   */
  default boolean movesView() {
    return false;
  }

  /** The command this widget fires, if it binds one (an interactive widget, a selectable list, a clickable image). */
  default Optional<String> boundCommand() {
    return Optional.empty();
  }

  /** The {@code kind} strings of §3.3.1. */
  enum Kind implements WireName {
    LABEL("label"),
    BUTTON("button"),
    SLIDER("slider"),
    DROPDOWN("dropdown"),
    CHECKBOX("checkbox"),
    LINE_EDIT("line_edit"),
    CHANNEL_PICKER("channel_picker"),
    COLOR_PICKER("color_picker"),
    COLORMAP_PICKER("colormap_picker"),
    ROI_PICKER("roi_picker"),
    LIST("list"),
    IMAGE("image"),
    PROGRESS("progress"),
    ROW("row"),
    COLUMN("column");

    private final String wire;

    Kind(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }

    /** Whether a widget of this kind must bind a declared command. */
    public boolean interactive() {
      return switch (this) {
        case BUTTON, SLIDER, DROPDOWN, CHECKBOX, LINE_EDIT, CHANNEL_PICKER, COLOR_PICKER, COLORMAP_PICKER, ROI_PICKER -> true;
        case LABEL, IMAGE, PROGRESS, ROW, COLUMN, LIST -> false; // a list binds a command only when selectable
      };
    }
  }

  /** A widget the user drives; it fires {@link #command()}, a command this manifest declares. */
  sealed interface Interactive extends WidgetDecl
      permits Button, Slider, Dropdown, Checkbox, LineEdit, ChannelPicker, ColorPicker, ColormapPicker, RoiPicker, RowList {
    String command();

    @Override
    default Optional<String> boundCommand() {
      return command().isEmpty() ? Optional.empty() : Optional.of(command());
    }
  }

  /** {@code row} or {@code column}: a non-empty list of children. */
  sealed interface Container extends WidgetDecl permits Row, Column {
    List<WidgetDecl> children();
  }

  /** This widget and every descendant, in document (pre-)order. */
  default List<WidgetDecl> flatten() {
    List<WidgetDecl> out = new ArrayList<>();
    flattenInto(this, out);
    return List.copyOf(out);
  }

  private static void flattenInto(WidgetDecl w, List<WidgetDecl> out) {
    out.add(w);
    if (w instanceof Container c) {
      for (WidgetDecl child : c.children()) flattenInto(child, out);
    }
    if (w instanceof RowList l) out.addAll(l.row()); // template ids share the panel's id space
  }

  // ---- the kinds ----

  /**
   * A label. Its text, declared and every later {@code set_value}, is shown as written unless
   * {@code textFormat} is {@link TextFormat#RICH} (§3.3.1).
   */
  record Label(String id, Optional<String> text, TextFormat textFormat) implements WidgetDecl {
    public Label {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(text, "text");
      Objects.requireNonNull(textFormat, "textFormat");
    }

    public Label(String id, Optional<String> text) {
      this(id, text, TextFormat.PLAIN);
    }

    @Override
    public Kind kind() {
      return Kind.LABEL;
    }

    public Label textFormat(TextFormat f) {
      return new Label(id, text, f);
    }

    /** Reads the text as Qt's rich-text subset; a link is styled but never opened. */
    public Label rich() {
      return textFormat(TextFormat.RICH);
    }
  }

  record Button(String id, String command, Optional<String> text, boolean movesView) implements Interactive {
    public Button {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(text, "text");
    }

    public Button(String id, String command, Optional<String> text) {
      this(id, command, text, false);
    }

    public Button movesView(boolean on) {
      return new Button(id, command, text, on);
    }

    @Override
    public Kind kind() {
      return Kind.BUTTON;
    }
  }

  /**
   * A slider; {@code min} (default 0) {@literal <} {@code max} (default 1), {@code step} (default
   * 1) {@literal >} 0.
   *
   * @param isFloat the manifest's {@code float}: whether values are continuous (default false)
   */
  record Slider(
      String id,
      String command,
      double min,
      double max,
      double step,
      OptionalDouble value,
      boolean isFloat,
      boolean movesView)
      implements Interactive {
    public static final double DEFAULT_MIN = 0;
    public static final double DEFAULT_MAX = 1;
    public static final double DEFAULT_STEP = 1;

    public Slider {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(value, "value");
    }

    public Slider(String id, String command, double min, double max, double step, OptionalDouble value, boolean isFloat) {
      this(id, command, min, max, step, value, isFloat, false);
    }

    /**
     * Held, it streams a new cause per value change (at most 30 Hz). The host sends at most one
     * command at a time, and while a slow handler runs it keeps only the newest value, so the next
     * command carries that value and a fresh cause.
     */
    public Slider movesView(boolean on) {
      return new Slider(id, command, min, max, step, value, isFloat, on);
    }

    @Override
    public Kind kind() {
      return Kind.SLIDER;
    }

    public Slider range(double min, double max) {
      return new Slider(id, command, min, max, step, value, isFloat, movesView);
    }

    public Slider step(double step) {
      return new Slider(id, command, min, max, step, value, isFloat, movesView);
    }

    public Slider value(double value) {
      return new Slider(id, command, min, max, step, OptionalDouble.of(value), isFloat, movesView);
    }

    public Slider continuous(boolean isFloat) {
      return new Slider(id, command, min, max, step, value, isFloat, movesView);
    }
  }

  /** A dropdown; {@code value}, if present, is one of the non-empty {@code options}. */
  record Dropdown(String id, String command, List<String> options, Optional<String> value, boolean movesView)
      implements Interactive {
    public Dropdown {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      options = List.copyOf(options);
      Objects.requireNonNull(value, "value");
    }

    public Dropdown(String id, String command, List<String> options, Optional<String> value) {
      this(id, command, options, value, false);
    }

    public Dropdown movesView(boolean on) {
      return new Dropdown(id, command, options, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.DROPDOWN;
    }

    public Dropdown value(String value) {
      return new Dropdown(id, command, options, Optional.of(value), movesView);
    }
  }

  record Checkbox(String id, String command, Optional<String> text, Optional<Boolean> value, boolean movesView)
      implements Interactive {
    public Checkbox {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(text, "text");
      Objects.requireNonNull(value, "value");
    }

    public Checkbox(String id, String command, Optional<String> text, Optional<Boolean> value) {
      this(id, command, text, value, false);
    }

    public Checkbox movesView(boolean on) {
      return new Checkbox(id, command, text, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.CHECKBOX;
    }

    public Checkbox text(String text) {
      return new Checkbox(id, command, Optional.of(text), value, movesView);
    }

    public Checkbox value(boolean value) {
      return new Checkbox(id, command, text, Optional.of(value), movesView);
    }
  }

  record LineEdit(String id, String command, Optional<String> placeholder, Optional<String> value, boolean movesView)
      implements Interactive {
    public LineEdit {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(placeholder, "placeholder");
      Objects.requireNonNull(value, "value");
    }

    public LineEdit(String id, String command, Optional<String> placeholder, Optional<String> value) {
      this(id, command, placeholder, value, false);
    }

    public LineEdit movesView(boolean on) {
      return new LineEdit(id, command, placeholder, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.LINE_EDIT;
    }

    public LineEdit placeholder(String placeholder) {
      return new LineEdit(id, command, Optional.of(placeholder), value, movesView);
    }

    public LineEdit value(String value) {
      return new LineEdit(id, command, placeholder, Optional.of(value), movesView);
    }
  }

  /**
   * A host picker of the open dataset's channels, each shown by the name the person sees
   * elsewhere in the host (the file's name for it, else "Channel N", 1-based): fires {@code
   * {"value": i}} with the 0-based index, or with {@code multiple} the indices chosen as {@code
   * Value.ints}. {@code value}, if present, is one 0-based index, or with {@code multiple} a list of
   * them.
   */
  record ChannelPicker(String id, String command, boolean multiple, Optional<List<Integer>> value, boolean movesView)
      implements Interactive {
    public ChannelPicker {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      value = value.map(List::copyOf);
      if (!multiple && value.isPresent() && value.get().size() != 1) {
        throw new IllegalArgumentException("a single channel picker's value is one index");
      }
    }

    public ChannelPicker(String id, String command, boolean multiple, Optional<List<Integer>> value) {
      this(id, command, multiple, value, false);
    }

    public ChannelPicker movesView(boolean on) {
      return new ChannelPicker(id, command, multiple, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.CHANNEL_PICKER;
    }

    public ChannelPicker multiple(boolean m) {
      return new ChannelPicker(id, command, m, value, movesView);
    }

    public ChannelPicker value(Integer... indices) {
      return new ChannelPicker(id, command, multiple, Optional.of(List.of(indices)), movesView);
    }
  }

  /** A host colour picker: fires {@code "#RRGGBB"} (opaque); {@code value} is one if present. */
  record ColorPicker(String id, String command, Optional<String> value, boolean movesView) implements Interactive {
    public ColorPicker {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(value, "value");
    }

    public ColorPicker(String id, String command, Optional<String> value) {
      this(id, command, value, false);
    }

    public ColorPicker movesView(boolean on) {
      return new ColorPicker(id, command, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.COLOR_PICKER;
    }

    public ColorPicker value(String rgb) {
      return new ColorPicker(id, command, Optional.of(rgb), movesView);
    }

    /** Whether {@code s} is {@code #RRGGBB}. */
    public static boolean isColor(String s) {
      return s.matches("#[0-9A-Fa-f]{6}");
    }
  }

  /** A host colormap picker: fires a colormap name ({@code GetColormap} gives its table). */
  record ColormapPicker(String id, String command, Optional<String> value, boolean movesView) implements Interactive {
    public ColormapPicker {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(value, "value");
    }

    public ColormapPicker(String id, String command, Optional<String> value) {
      this(id, command, value, false);
    }

    public ColormapPicker movesView(boolean on) {
      return new ColormapPicker(id, command, value, on);
    }

    @Override
    public Kind kind() {
      return Kind.COLORMAP_PICKER;
    }

    public ColormapPicker value(String name) {
      return new ColormapPicker(id, command, Optional.of(name), movesView);
    }
  }

  /**
   * A host region picker: fires a region id with {@code "name"} beside it. With {@code
   * followsSelection} it shows vizi's selection, and choosing a region in it selects that region in
   * vizi, as the Regions panel does (the person's choice, in a control vizi draws: no cause is
   * involved); without, vizi's selection is untouched. {@code filter} {@link RoiFilter#MINE} lists
   * only the regions this plugin contributed.
   */
  record RoiPicker(String id, String command, Optional<String> value, boolean movesView, boolean followsSelection, RoiFilter filter)
      implements Interactive {
    public RoiPicker {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      Objects.requireNonNull(value, "value");
      Objects.requireNonNull(filter, "filter");
    }

    public RoiPicker(String id, String command, Optional<String> value) {
      this(id, command, value, false, false, RoiFilter.ALL);
    }

    public RoiPicker movesView(boolean on) {
      return new RoiPicker(id, command, value, on, followsSelection, filter);
    }

    public RoiPicker followsSelection(boolean on) {
      return new RoiPicker(id, command, value, movesView, on, filter);
    }

    public RoiPicker filter(RoiFilter f) {
      return new RoiPicker(id, command, value, movesView, followsSelection, f);
    }

    @Override
    public Kind kind() {
      return Kind.ROI_PICKER;
    }

    public RoiPicker value(String regionId) {
      return new RoiPicker(id, command, Optional.of(regionId), movesView, followsSelection, filter);
    }
  }

  /**
   * A list: rows the plugin supplies at runtime ({@code set_rows}), each built from {@code
   * row}, a template of leaf widgets whose cells are addressed by row key. {@code maxRows} and
   * {@code visibleRows} are 1–1024 (defaults 256 and 8). Only a {@code selectable} list binds a
   * command, which then fires {@code {"row": key}}; {@link #command()} is {@code ""} otherwise.
   */
  record RowList(String id, List<WidgetDecl> row, int maxRows, int visibleRows, boolean selectable, String command, boolean movesView)
      implements Interactive {
    public static final int DEFAULT_MAX_ROWS = 256;
    public static final int DEFAULT_VISIBLE_ROWS = 8;
    public static final int MAX = 1024;
    /** The most widgets one list may realise, {@code max_rows} × its row's cells (§3.3). */
    public static final int MAX_CELLS = 4096;

    public RowList {
      Objects.requireNonNull(id, "id");
      row = List.copyOf(row);
      Objects.requireNonNull(command, "command");
      if (row.isEmpty()) throw new IllegalArgumentException("list " + id + " has an empty row");
      for (WidgetDecl w : row) {
        if (w instanceof Container || w instanceof RowList) throw new IllegalArgumentException("a list's row holds leaf widgets only");
      }
      if (maxRows < 1 || maxRows > MAX || visibleRows < 1 || visibleRows > MAX) {
        throw new IllegalArgumentException("max_rows and visible_rows are in [1, 1024]");
      }
      if ((long) maxRows * row.size() > MAX_CELLS) {
        throw new IllegalArgumentException("list '" + id + "' may realise at most " + MAX_CELLS + " cells (max_rows × row cells), not "
            + maxRows + " × " + row.size());
      }
      if (selectable == command.isEmpty()) throw new IllegalArgumentException("a list binds a command exactly when it is selectable");
      if (movesView && !selectable) throw new IllegalArgumentException("list " + id + " moves the view only when it is selectable");
    }

    public RowList(String id, List<WidgetDecl> row, int maxRows, int visibleRows, boolean selectable, String command) {
      this(id, row, maxRows, visibleRows, selectable, command, false);
    }

    /** A selection may move the view (a selectable list only). */
    public RowList movesView(boolean on) {
      return new RowList(id, row, maxRows, visibleRows, selectable, command, on);
    }

    @Override
    public Kind kind() {
      return Kind.LIST;
    }

    public RowList maxRows(int n) {
      return new RowList(id, row, n, visibleRows, selectable, command, movesView);
    }

    public RowList visibleRows(int n) {
      return new RowList(id, row, maxRows, n, selectable, command, movesView);
    }

    /** Selectable, firing {@code command} with {@code {"row": key}}. */
    public RowList selectable(String command) {
      return new RowList(id, row, maxRows, visibleRows, true, command, movesView);
    }

    /** What the host's queue sizing counts: the list, and each template widget once per possible row. */
    public int widgetCount() {
      return 1 + row.size() * maxRows;
    }
  }

  /**
   * The target of {@code push_image}. With a {@code command} ({@code ""} for none) it is
   * clickable: a click fires {@code {"x", "y", "button", "modifiers", "image"}}. Only a clickable
   * image may be {@code movesView}.
   */
  record Image(String id, String command, boolean movesView) implements WidgetDecl {
    public Image {
      Objects.requireNonNull(id, "id");
      Objects.requireNonNull(command, "command");
      if (movesView && command.isEmpty()) throw new IllegalArgumentException("image " + id + " moves the view only when it binds a command");
    }

    public Image(String id) {
      this(id, "", false);
    }

    @Override
    public Kind kind() {
      return Kind.IMAGE;
    }

    @Override
    public Optional<String> boundCommand() {
      return command.isEmpty() ? Optional.empty() : Optional.of(command);
    }

    /** Clickable: a click fires {@code command}. */
    public Image clickable(String command) {
      return new Image(id, command, movesView);
    }

    public Image movesView(boolean on) {
      return new Image(id, command, on);
    }
  }

  /**
   * A label's {@code text_format} (§3.3.1): {@code plain}, the default, shows markup as written,
   * which is what text relayed from data needs; {@code rich} reads it as Qt's rich-text subset.
   */
  enum TextFormat implements WireName {
    PLAIN("plain"),
    RICH("rich");

    private final String wire;

    TextFormat(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  /** A {@code roi_picker}'s {@code filter}. */
  enum RoiFilter implements WireName {
    ALL("all"),
    MINE("mine");

    private final String wire;

    RoiFilter(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  /**
   * A progress bar the host drives and the user cannot (§3.3.1); binds nothing.
   *
   * @param value in [0, 1] (default 0)
   * @param indeterminate wins over {@code value} (default false)
   */
  record Progress(String id, double value, boolean indeterminate) implements WidgetDecl {
    public Progress {
      Objects.requireNonNull(id, "id");
    }

    @Override
    public Kind kind() {
      return Kind.PROGRESS;
    }
  }

  record Row(String id, List<WidgetDecl> children) implements Container {
    public Row {
      Objects.requireNonNull(id, "id");
      children = List.copyOf(children);
    }

    @Override
    public Kind kind() {
      return Kind.ROW;
    }
  }

  record Column(String id, List<WidgetDecl> children) implements Container {
    public Column {
      Objects.requireNonNull(id, "id");
      children = List.copyOf(children);
    }

    @Override
    public Kind kind() {
      return Kind.COLUMN;
    }
  }

  // ---- factories ----

  static Label label(String id) {
    return new Label(id, Optional.empty());
  }

  static Label label(String id, String text) {
    return new Label(id, Optional.of(text));
  }

  static Button button(String id, String command, String text) {
    return new Button(id, command, Optional.of(text));
  }

  static Slider slider(String id, String command) {
    return new Slider(
        id,
        command,
        Slider.DEFAULT_MIN,
        Slider.DEFAULT_MAX,
        Slider.DEFAULT_STEP,
        OptionalDouble.empty(),
        false,
        false);
  }

  static Dropdown dropdown(String id, String command, List<String> options) {
    return new Dropdown(id, command, options, Optional.empty());
  }

  static Checkbox checkbox(String id, String command) {
    return new Checkbox(id, command, Optional.empty(), Optional.empty());
  }

  static LineEdit lineEdit(String id, String command) {
    return new LineEdit(id, command, Optional.empty(), Optional.empty());
  }

  static ChannelPicker channelPicker(String id, String command) {
    return new ChannelPicker(id, command, false, Optional.empty());
  }

  static ColorPicker colorPicker(String id, String command) {
    return new ColorPicker(id, command, Optional.empty());
  }

  static ColormapPicker colormapPicker(String id, String command) {
    return new ColormapPicker(id, command, Optional.empty());
  }

  static RoiPicker roiPicker(String id, String command) {
    return new RoiPicker(id, command, Optional.empty());
  }

  /** A list of rows built from {@code row}: 256 rows at most, 8 shown, not selectable. */
  static RowList list(String id, WidgetDecl... row) {
    return new RowList(id, List.of(row), RowList.DEFAULT_MAX_ROWS, RowList.DEFAULT_VISIBLE_ROWS, false, "");
  }

  static Image image(String id) {
    return new Image(id);
  }

  static Progress progress(String id) {
    return new Progress(id, 0, false);
  }

  static Row row(String id, WidgetDecl... children) {
    return new Row(id, List.of(children));
  }

  static Column column(String id, WidgetDecl... children) {
    return new Column(id, List.of(children));
  }
}
