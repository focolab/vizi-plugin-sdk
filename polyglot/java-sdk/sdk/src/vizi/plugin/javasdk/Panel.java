package vizi.plugin.javasdk;

import java.lang.foreign.MemorySegment;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.function.Consumer;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Outbound;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.ImageRef;
import vizi.plugin.v1.ViziPlugin.Options;
import vizi.plugin.v1.ViziPlugin.PatchOp;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * A declared panel (control_plane_protocol.md §8.4) and the SDK's mirror of its widgets.
 *
 * <p>Every set works from any thread, is sent as it happens (coalesced per widget on the way out),
 * and is remembered: when the panel is opened the SDK re-pushes the whole mirror (options before
 * values, every enablement and visibility ever set, the last image) before the author's own hook
 * runs. The host keeps a closed panel's state (§8.4); only an image waits, for the
 * panel's {@code P} segment, and goes out when that installs or the panel next opens.
 *
 * <p>An interactive widget taken as a typed handle ({@link #number}, {@link #integer}, {@link
 * #flag}, {@link #choice}, {@link #line}) OWNS the command it binds: the SDK answers that command
 * itself, parsing and checking the value. A refused value is pushed back to what it was, the
 * reason goes to the {@code reportTo} label if one is set, and the command fails with it.
 */
public final class Panel {

  private final PanelDecl decl;
  private final Plugin.Runtime rt;
  private final Map<String, WidgetDecl> widgets = new LinkedHashMap<>();
  private final Map<String, Widget> handles = new HashMap<>();
  private final Map<String, State> mirror = new LinkedHashMap<>();

  /** Which list a template widget is a cell of: its ops are addressed by row. */
  private final Map<String, String> cellOf = new HashMap<>();

  /** What was last set on one widget, or one row's cell of a list. */
  private static final class State {
    String widget = "";
    String row = "";
    @Nullable List<String> rows; // a list's set_rows
    // a table's rows, by key in the plugin's order: every set_table and patch_table applied
    @Nullable LinkedHashMap<String, vizi.plugin.v1.ViziPlugin.TableRow> table;
    @Nullable Value value;
    @Nullable List<String> options;
    @Nullable Boolean enabled;
    @Nullable Boolean visible;
    byte @Nullable [] png;
    boolean imageHeld; // png not sent yet: no P segment when it was pushed
    // sent at a P install since the last PanelOpened, so that open's re-push need not lend it again
    boolean sentAtInstall;
    vizi.plugin.v1.ViziPlugin.@Nullable Progress progress;
  }

  Panel(PanelDecl decl, Plugin.Runtime rt) {
    this.decl = decl;
    this.rt = rt;
    for (WidgetDecl w : decl.root().flatten()) {
      if (w instanceof WidgetDecl.RowList l) for (WidgetDecl c : l.row()) cellOf.put(c.id(), l.id());
    }
    for (WidgetDecl w : decl.root().flatten()) {
      widgets.put(w.id(), w);
      if (cellOf.containsKey(w.id())) continue; // a template's values are the host's until a row sets them
      // a label's declared text is its state until set: re-pushed on open like any other
      if (w instanceof WidgetDecl.Label l && l.text().isPresent()) {
        state(w.id()).value = Value.newBuilder().setS(l.text().get()).build();
      }
    }
  }

  public String id() {
    return decl.id();
  }

  PanelDecl decl() {
    return decl;
  }

  // ---- handles ----

  /** A label: {@code set(text)}. */
  public Label label(String widgetId) {
    return handle(widgetId, WidgetDecl.Label.class, "label", w -> new Label(widgetId));
  }

  /** An image widget: {@code push(png)}. */
  public Image image(String widgetId) {
    return handle(widgetId, WidgetDecl.Image.class, "image", w -> new Image(widgetId));
  }

  /** A progress bar. */
  public ProgressBar progress(String widgetId) {
    return handle(widgetId, WidgetDecl.Progress.class, "progress", w -> new ProgressBar(widgetId));
  }

  /** Any widget, for enablement and visibility only (a button, a row). */
  public Control control(String widgetId) {
    notCell(widgetId);
    WidgetDecl w = widget(widgetId);
    Widget h = handles.get(widgetId);
    if (h != null) {
      if (h instanceof Control c) return c;
      throw new IllegalStateException("widget " + widgetId + " already has a typed handle; use that");
    }
    Control c = new Control(w.id());
    handles.put(widgetId, c);
    return c;
  }

  /**
   * A number from a slider or a line edit, answered by the SDK. {@code check} returns null to
   * accept a value or the reason to refuse it.
   */
  public Number number(String widgetId, Check<Double> check) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.Slider) && !(w instanceof WidgetDecl.LineEdit)) {
      throw new IllegalArgumentException("widget " + widgetId + " is a " + w.kind().wire() + "; a number binds a slider or a line_edit");
    }
    return bind(widgetId, new Number(w, check));
  }

  public Number number(String widgetId) {
    return number(widgetId, v -> null);
  }

  /** A whole number from a slider or a line edit, answered by the SDK. */
  public Whole integer(String widgetId, Check<Long> check) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.Slider) && !(w instanceof WidgetDecl.LineEdit)) {
      throw new IllegalArgumentException("widget " + widgetId + " is a " + w.kind().wire() + "; an integer binds a slider or a line_edit");
    }
    return bind(widgetId, new Whole(w, check));
  }

  public Whole integer(String widgetId) {
    return integer(widgetId, v -> null);
  }

  /** A checkbox, answered by the SDK. */
  public Flag flag(String widgetId) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.Checkbox c)) throw new IllegalArgumentException("widget " + widgetId + " is not a checkbox");
    return bind(widgetId, new Flag(c));
  }

  /** A dropdown, answered by the SDK. */
  public Choice choice(String widgetId, Check<String> check) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.Dropdown d)) throw new IllegalArgumentException("widget " + widgetId + " is not a dropdown");
    return bind(widgetId, new Choice(d, check));
  }

  public Choice choice(String widgetId) {
    return choice(widgetId, v -> null);
  }

  /** Free text from a line edit, answered by the SDK. */
  public Text line(String widgetId, Check<String> check) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.LineEdit l)) throw new IllegalArgumentException("widget " + widgetId + " is not a line_edit");
    return bind(widgetId, new Text(l, check));
  }

  public Text line(String widgetId) {
    return line(widgetId, v -> null);
  }

  /**
   * A channel picker, answered by the SDK: the chosen channel indices, 0-based (one for a
   * single picker; any number with {@code multiple}, which fires {@code Value.ints}). The host labels
   * each entry by its name, "Channel N" 1-based when the file names none; the value is
   * still the index.
   */
  public Channels channels(String widgetId) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.ChannelPicker c)) throw new IllegalArgumentException("widget " + widgetId + " is not a channel_picker");
    return bind(widgetId, new Channels(c));
  }

  /**
   * A color, colormap or region picker, answered by the SDK: the chosen {@code "#RRGGBB"},
   * colormap name, or region id (with its {@link Picked#name() name}).
   */
  public Picked picked(String widgetId) {
    WidgetDecl w = widget(widgetId);
    if (!(w instanceof WidgetDecl.ColorPicker) && !(w instanceof WidgetDecl.ColormapPicker) && !(w instanceof WidgetDecl.RoiPicker)) {
      throw new IllegalArgumentException("widget " + widgetId + " is a " + w.kind().wire() + ", not a color, colormap or roi picker");
    }
    return bind(widgetId, new Picked(w));
  }

  /**
   * A list: its rows, set as the whole ordered key list, and each row's cells addressed by
   * {@link Rows#row(String)}. A selectable list's command is answered by this handle, which then
   * tracks the selection.
   */
  public Rows rows(String listId) {
    WidgetDecl w = widget(listId);
    if (!(w instanceof WidgetDecl.RowList l)) throw new IllegalArgumentException("widget " + listId + " is not a list");
    synchronized (this) {
      Widget h = handles.get(listId);
      if (h instanceof Rows r) return r;
    }
    Rows r = new Rows(l);
    if (l.selectable()) return bind(listId, r);
    synchronized (this) {
      handles.put(listId, r);
    }
    return r;
  }

  /**
   * A table (§3.3.1): its rows, stated whole with {@link Table#set} or changed by key with {@link
   * Table#patch}. A selectable table's command is answered by this handle, which then tracks the
   * selection; take it before {@code build()}.
   */
  public Table table(String tableId) {
    WidgetDecl.Table t = tableDecl(tableId);
    synchronized (this) {
      Widget h = handles.get(tableId);
      if (h instanceof Table x) return x;
    }
    Table x = new Table(t);
    if (t.selectable()) return bind(tableId, x);
    synchronized (this) {
      handles.put(tableId, x);
    }
    return x;
  }

  /**
   * A table's rows, whole and in order, replacing the last ({@code set_table}). Keys are
   * non-empty, distinct and at most 128 characters; a table holds at most 100,000 rows. A table
   * larger than one message (1 MiB) goes as a whole statement followed by appending patches, which
   * the host applies in order. Rows that stay keep their selection, here as on the host.
   */
  public void setTable(String tableId, List<TableRow> rows) {
    writeTable(tableDecl(tableId), rows, List.of(), true);
  }

  /**
   * Changes a table's rows by key ({@code patch_table}): every key in {@code delete} goes, then
   * each row in {@code upsert} replaces the row with its key in place or is appended. A patch
   * that would take the table past 100,000 rows is refused here, whole, as the host would refuse
   * it. A patch larger than one message goes as several, deletes first.
   */
  public void patchTable(String tableId, List<TableRow> upsert, java.util.Collection<String> delete) {
    writeTable(tableDecl(tableId), upsert, delete, false);
  }

  /** Answers a {@code moves_view} widget's cause; a throw fails the command, and the cause is declined. */
  @FunctionalInterface
  public interface CauseListener {
    void answer(ViewCause cause) throws Exception;
  }

  /** A {@code moves_view} interaction that arrived with cause 0: nothing to answer. */
  @FunctionalInterface
  public interface NoCauseListener {
    void run() throws Exception;
  }

  /** Accepts a value ({@code null}) or refuses it with the reason. */
  @FunctionalInterface
  public interface Check<T> {
    @Nullable String refuse(T value);
  }

  // ---- by-id conveniences (what a hand-written handler reaches for) ----

  /** Sets a label's text, or a widget's value, by id. */
  public void setValue(String widgetId, String text) {
    notCell(widgetId);
    widget(widgetId);
    set(widgetId, Value.newBuilder().setS(text).build());
  }

  /**
   * Sets a checkbox's state by id, for a handler that answers the checkbox's command itself (one
   * that is not bound with {@link #flag}).
   */
  public void setValue(String widgetId, boolean on) {
    notCell(widgetId);
    if (!(widget(widgetId) instanceof WidgetDecl.Checkbox)) throw new IllegalArgumentException("widget " + widgetId + " is not a checkbox");
    set(widgetId, Value.newBuilder().setB(on).build());
  }

  /** Pushes a PNG into an image widget, by id. */
  public void pushImage(String widgetId, byte[] png) {
    image(widgetId).push(png);
  }

  // ---- the widgets ----

  /** What every handle can do. */
  public abstract sealed class Widget permits Label, Image, ProgressBar, Control, Bound {
    final String widgetId;

    Widget(String widgetId) {
      this.widgetId = widgetId;
    }

    public String id() {
      return widgetId;
    }

    public void enabled(boolean on) {
      op(widgetId, PatchOp.newBuilder().setSetEnabled(on), s -> s.enabled = on);
    }

    public void visible(boolean on) {
      op(widgetId, PatchOp.newBuilder().setSetVisible(on), s -> s.visible = on);
    }
  }

  public final class Label extends Widget {
    Label(String id) {
      super(id);
    }

    public void set(String text) {
      Panel.this.set(widgetId, Value.newBuilder().setS(text).build());
    }
  }

  public final class Control extends Widget {
    Control(String id) {
      super(id);
    }
  }

  public final class Image extends Widget {
    Image(String id) {
      super(id);
    }

    /**
     * Pushes {@code png} through the panel's arena. Throws {@link ArenaFullException} when the
     * arena is short (a shortfall has gone to the host); nothing is sent then.
     */
    public void push(byte[] png) {
      byte[] copy = png.clone();
      synchronized (Panel.this) { // recorded and queued as one step: see opRow
        State st = state(widgetId);
        st.png = copy;
        st.imageHeld = true;
        st.sentAtInstall = false;
        Session s = rt.sessionOrNull();
        if (s != null) st.imageHeld = !pushImage(s, widgetId, "", copy);
      }
    }

    /**
     * How large the host draws this image, as it last said this session ({@code WidgetGeometry},
     * §8.4); empty before it has. An image pushed at {@code size().get().pixels()} is shown 1:1.
     */
    public Optional<WidgetSize> size() {
      return rt.geometry(decl.id(), widgetId);
    }
  }

  public final class ProgressBar extends Widget {
    ProgressBar(String id) {
      super(id);
    }

    /** A fraction in [0, 1]. */
    public void set(double fraction) {
      var p = vizi.plugin.v1.ViziPlugin.Progress.newBuilder().setValue(fraction).build();
      op(widgetId, PatchOp.newBuilder().setSetProgress(p), st -> st.progress = p);
    }

    /** Length unknown: the host shows activity. */
    public void indeterminate() {
      var p = vizi.plugin.v1.ViziPlugin.Progress.newBuilder().setIndeterminate(true).build();
      op(widgetId, PatchOp.newBuilder().setSetProgress(p), st -> st.progress = p);
    }
  }

  /** An interactive widget whose command the SDK answers. */
  public abstract sealed class Bound extends Widget permits Number, Whole, Flag, Choice, Text, Channels, Picked, Rows, Table {
    final WidgetDecl decl;
    @Nullable Label reportTo;

    Bound(WidgetDecl decl) {
      super(decl.id());
      this.decl = decl;
    }

    String command() {
      return ((WidgetDecl.Interactive) decl).command();
    }

    /** Where a refusal's reason is shown, besides the failed command. */
    public Bound reportTo(Label label) {
      this.reportTo = label;
      return this;
    }

    private final List<CauseListener> causeListeners = new ArrayList<>();

    /**
     * For a {@code moves_view} widget (§8.15): runs on the command worker after the value
     * listeners, for each accepted interaction, with the cause to answer. The cause carries the
     * interaction's arguments; a list's clicked row is {@link ViewCause#row()}. A cause no listener
     * answers (or {@link ViewCause#defer() defers}) is declined and logged, as is one whose value
     * was refused.
     */
    public Bound onCause(CauseListener listener) {
      rt.checkOpen("adding a listener");
      if (!(decl instanceof WidgetDecl.Interactive i) || !i.movesView()) {
        throw new IllegalStateException("widget " + widgetId + " is not declared moves_view, so its interactions carry no cause");
      }
      causeListeners.add(listener);
      return this;
    }

    void caused(ViewCause cause) throws Exception {
      for (CauseListener l : causeListeners) l.answer(cause);
    }

    private final List<NoCauseListener> noCauseListeners = new ArrayList<>();

    /**
     * For a {@code moves_view} widget: runs on the command worker after the value listeners, for an
     * accepted interaction that carries cause 0, which vizi sends when no dataset is open (§8.15).
     * Nothing can move the view then, so there is no cause to answer; the listener says why.
     */
    public Bound onNoCause(NoCauseListener listener) {
      rt.checkOpen("adding a listener");
      if (!(decl instanceof WidgetDecl.Interactive i) || !i.movesView()) {
        throw new IllegalStateException("widget " + widgetId + " is not declared moves_view, so its interactions carry no cause");
      }
      noCauseListeners.add(listener);
      return this;
    }

    void causeless() throws Exception {
      for (NoCauseListener l : noCauseListeners) l.run();
    }

    boolean movesView() {
      return decl instanceof WidgetDecl.Interactive i && i.movesView();
    }

    /** Parses and checks the widget's value; throws {@link Refusal} to refuse it. */
    abstract void accept(Map<String, Value> args) throws Refusal;

    /** The mirror's current value, for the push-back. */
    @Nullable Value current() {
      synchronized (Panel.this) {
        State st = mirror.get(widgetId);
        return st == null ? null : st.value;
      }
    }

    final void refuse(String reason) throws Refusal {
      Value old = current();
      if (old != null) pushValue(widgetId, old);
      Label r = reportTo;
      if (r != null) r.set(reason);
      throw Refusal.invalid(reason);
    }
  }

  public final class Number extends Bound {
    private final Check<Double> check;
    private final List<Consumer<Double>> listeners = new ArrayList<>();
    private double value;

    Number(WidgetDecl decl, Check<Double> check) {
      super(decl);
      this.check = check;
      if (decl instanceof WidgetDecl.Slider s) value = s.value().orElse(s.min());
      if (decl instanceof WidgetDecl.LineEdit l) value = l.value().map(Panel::parseOr0).orElse(0.0);
      remember(widgetId, wire(value));
    }

    public synchronized double get() {
      return value;
    }

    public void set(double v) {
      synchronized (this) {
        value = v;
      }
      Panel.this.set(widgetId, wire(v));
    }

    /** Runs on the command worker after each accepted value. */
    public Number onChange(Consumer<Double> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    private Value wire(double v) {
      if (decl instanceof WidgetDecl.LineEdit) return Value.newBuilder().setS(Fmt.g(v)).build();
      if (decl instanceof WidgetDecl.Slider s && !s.isFloat()) return Value.newBuilder().setI((long) Math.rint(v)).build();
      return Value.newBuilder().setF(v).build();
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      double v;
      if (a == null) {
        refuse("no value");
        return;
      } else if (a.hasF()) {
        v = a.getF();
      } else if (a.hasI()) {
        v = a.getI();
      } else if (a.hasS()) {
        try {
          v = Double.parseDouble(a.getS().trim());
        } catch (NumberFormatException e) {
          refuse("'" + a.getS() + "' is not a number");
          return;
        }
      } else {
        refuse("no value");
        return;
      }
      if (!Double.isFinite(v)) {
        refuse(Fmt.g(v) + " is not a finite number");
        return;
      }
      String why = check.refuse(v);
      if (why != null) {
        refuse(why);
        return;
      }
      synchronized (this) {
        value = v;
      }
      remember(widgetId, wire(v));
      if (decl instanceof WidgetDecl.LineEdit) pushValue(widgetId, wire(v)); // shown as the SDK reads it
      for (Consumer<Double> l : listeners) l.accept(v);
    }
  }

  public final class Whole extends Bound {
    private final Check<Long> check;
    private final List<Consumer<Long>> listeners = new ArrayList<>();
    private long value;

    Whole(WidgetDecl decl, Check<Long> check) {
      super(decl);
      this.check = check;
      if (decl instanceof WidgetDecl.Slider s) value = (long) Math.rint(s.value().orElse(s.min()));
      if (decl instanceof WidgetDecl.LineEdit l) value = (long) Math.rint(l.value().map(Panel::parseOr0).orElse(0.0));
      remember(widgetId, wire(value));
    }

    public synchronized long get() {
      return value;
    }

    public void set(long v) {
      synchronized (this) {
        value = v;
      }
      Panel.this.set(widgetId, wire(v));
    }

    public Whole onChange(Consumer<Long> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    private Value wire(long v) {
      if (decl instanceof WidgetDecl.LineEdit) return Value.newBuilder().setS(Long.toString(v)).build();
      if (decl instanceof WidgetDecl.Slider s && s.isFloat()) return Value.newBuilder().setF(v).build();
      return Value.newBuilder().setI(v).build();
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      long v;
      if (a != null && a.hasI()) {
        v = a.getI();
      } else if (a != null && (a.hasF() || a.hasS())) {
        double d;
        try {
          d = a.hasF() ? a.getF() : Double.parseDouble(a.getS().trim());
        } catch (NumberFormatException e) {
          refuse("'" + a.getS() + "' is not a whole number");
          return;
        }
        if (!Double.isFinite(d) || d != Math.rint(d) || Math.abs(d) > 9.007199254740992E15) {
          refuse((a.hasS() ? a.getS() : Fmt.g(d)) + " is not a whole number");
          return;
        }
        v = (long) d;
      } else {
        refuse("no value");
        return;
      }
      String why = check.refuse(v);
      if (why != null) {
        refuse(why);
        return;
      }
      synchronized (this) {
        value = v;
      }
      remember(widgetId, wire(v));
      if (decl instanceof WidgetDecl.LineEdit) pushValue(widgetId, wire(v));
      for (Consumer<Long> l : listeners) l.accept(v);
    }
  }

  public final class Flag extends Bound {
    private final List<Consumer<Boolean>> listeners = new ArrayList<>();
    private boolean value;

    Flag(WidgetDecl.Checkbox decl) {
      super(decl);
      value = decl.value().orElse(false);
      remember(widgetId, Value.newBuilder().setB(value).build());
    }

    public synchronized boolean get() {
      return value;
    }

    public void set(boolean v) {
      synchronized (this) {
        value = v;
      }
      Panel.this.set(widgetId, Value.newBuilder().setB(v).build());
    }

    public Flag onChange(Consumer<Boolean> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      if (a == null || !a.hasB()) {
        refuse("a checkbox value is a boolean");
        return;
      }
      synchronized (this) {
        value = a.getB();
      }
      remember(widgetId, a);
      for (Consumer<Boolean> l : listeners) l.accept(a.getB());
    }
  }

  public final class Choice extends Bound {
    private final Check<String> check;
    private final List<Consumer<String>> listeners = new ArrayList<>();
    private List<String> options;
    private @Nullable String value;

    Choice(WidgetDecl.Dropdown decl, Check<String> check) {
      super(decl);
      this.check = check;
      this.options = decl.options();
      this.value = decl.value().orElse(null);
      if (value != null) remember(widgetId, Value.newBuilder().setS(value).build());
    }

    public synchronized Optional<String> get() {
      return Optional.ofNullable(value);
    }

    public synchronized List<String> options() {
      return options;
    }

    /** The selection's index in the current options, or -1 for none. */
    public synchronized int index() {
      return value == null ? -1 : options.indexOf(value);
    }

    /** Selects {@code v}, which must be one of the current options. */
    public void set(String v) {
      synchronized (this) {
        if (!options.contains(v)) throw new IllegalArgumentException("'" + v + "' is not one of " + options);
        value = v;
      }
      Panel.this.set(widgetId, Value.newBuilder().setS(v).build());
    }

    /**
     * Replaces the option list. The host carries the selection across by text, so the SDK's
     * mirror does too: a selection not in the new list is cleared.
     */
    public void options(List<String> opts) {
      List<String> copy = List.copyOf(opts);
      if (copy.isEmpty()) throw new IllegalArgumentException("a dropdown needs at least one option");
      synchronized (this) {
        options = copy;
        if (value != null && !copy.contains(value)) value = null;
      }
      op(widgetId, PatchOp.newBuilder().setSetOptions(Options.newBuilder().addAllOptions(copy)), st -> {
        st.options = copy;
        if (st.value != null && st.value.hasS() && !copy.contains(st.value.getS())) st.value = null;
      });
    }

    public Choice onChange(Consumer<String> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      Value idx = args.get("index");
      String v;
      List<String> opts;
      synchronized (this) {
        opts = options;
      }
      if (a != null && a.hasS()) {
        v = a.getS();
      } else if (idx != null && idx.hasI() && idx.getI() >= 0 && idx.getI() < opts.size()) {
        v = opts.get((int) idx.getI());
      } else {
        refuse("no option chosen");
        return;
      }
      if (!opts.contains(v)) {
        refuse("'" + v + "' is not one of the options");
        return;
      }
      String why = check.refuse(v);
      if (why != null) {
        refuse(why);
        return;
      }
      synchronized (this) {
        value = v;
      }
      remember(widgetId, Value.newBuilder().setS(v).build());
      for (Consumer<String> l : listeners) l.accept(v);
    }
  }

  public final class Text extends Bound {
    private final Check<String> check;
    private final List<Consumer<String>> listeners = new ArrayList<>();
    private String value;

    Text(WidgetDecl.LineEdit decl, Check<String> check) {
      super(decl);
      this.check = check;
      value = decl.value().orElse("");
      if (decl.value().isPresent()) remember(widgetId, Value.newBuilder().setS(value).build());
    }

    public synchronized String get() {
      return value;
    }

    public void set(String v) {
      synchronized (this) {
        value = v;
      }
      Panel.this.set(widgetId, Value.newBuilder().setS(v).build());
    }

    public Text onChange(Consumer<String> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      if (a == null || !a.hasS()) {
        refuse("a line edit's value is text");
        return;
      }
      String why = check.refuse(a.getS());
      if (why != null) {
        refuse(why);
        return;
      }
      synchronized (this) {
        value = a.getS();
      }
      remember(widgetId, a);
      for (Consumer<String> l : listeners) l.accept(a.getS());
    }
  }

  public final class Channels extends Bound {
    private final boolean multiple;
    private final List<Consumer<List<Integer>>> listeners = new ArrayList<>();
    private List<Integer> value;

    Channels(WidgetDecl.ChannelPicker decl) {
      super(decl);
      this.multiple = decl.multiple();
      this.value = decl.value().orElse(List.of());
      if (decl.value().isPresent()) remember(widgetId, wire(value));
    }

    public synchronized List<Integer> get() {
      return value;
    }

    /** The first chosen channel, or empty. */
    public synchronized Optional<Integer> first() {
      return value.isEmpty() ? Optional.empty() : Optional.of(value.get(0));
    }

    public void set(List<Integer> channels) {
      List<Integer> copy = List.copyOf(channels);
      if (!multiple && copy.size() != 1) throw new IllegalArgumentException("a single channel picker shows one channel");
      for (int c : copy) if (c < 0) throw new IllegalArgumentException("channel " + c + " is not a channel index");
      synchronized (this) {
        value = copy;
      }
      Panel.this.set(widgetId, wire(copy));
    }

    public Channels onChange(Consumer<List<Integer>> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    private Value wire(List<Integer> v) {
      return multiple ? Values.ofInts(v) : Value.newBuilder().setI(v.isEmpty() ? 0 : v.get(0)).build();
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      List<Integer> chosen = new ArrayList<>();
      if (a != null && a.hasInts()) {
        for (long c : a.getInts().getValuesList()) {
          if (c < 0 || c > Integer.MAX_VALUE) {
            refuse("channel " + c + " is not a channel index");
            return;
          }
          chosen.add((int) c);
        }
      } else if (a != null && a.hasI() && a.getI() >= 0 && a.getI() <= Integer.MAX_VALUE) {
        chosen.add((int) a.getI());
      } else {
        refuse("a channel picker's value is a channel index" + (multiple ? " or a list of them" : ""));
        return;
      }
      if (!multiple && chosen.size() != 1) {
        refuse("a single channel picker chose " + chosen.size() + " channels");
        return;
      }
      List<Integer> copy = List.copyOf(chosen);
      synchronized (this) {
        value = copy;
      }
      remember(widgetId, wire(copy));
      for (Consumer<List<Integer>> l : listeners) l.accept(copy);
    }
  }

  public final class Picked extends Bound {
    private final boolean color;
    private final List<Consumer<String>> listeners = new ArrayList<>();
    private @Nullable String value;
    private String name = "";

    Picked(WidgetDecl decl) {
      super(decl);
      this.color = decl instanceof WidgetDecl.ColorPicker;
      Optional<String> v = switch (decl) {
        case WidgetDecl.ColorPicker c -> c.value();
        case WidgetDecl.ColormapPicker c -> c.value();
        case WidgetDecl.RoiPicker c -> c.value();
        default -> Optional.empty();
      };
      value = v.orElse(null);
      if (value != null) remember(widgetId, Value.newBuilder().setS(value).build());
    }

    /** The chosen color, colormap name or region id. */
    public synchronized Optional<String> get() {
      return Optional.ofNullable(value);
    }

    /** A region picker's chosen region's name, as the host sent it ({@code ""} otherwise). */
    public synchronized String name() {
      return name;
    }

    /**
     * Shows {@code v} in the picker: {@code "#RRGGBB"} for a color picker, a colormap name or a
     * region id otherwise. The host applies a colormap or region only if it is among the picker's
     * current choices, and refuses anything else (a ContractViolation); the SDK cannot see those
     * choices, so it refuses only what is never one: {@code ""}.
     */
    public void set(String v) {
      if (color && !WidgetDecl.ColorPicker.isColor(v)) throw new IllegalArgumentException("'" + v + "' is not #RRGGBB");
      if (v.isEmpty()) throw new IllegalArgumentException("a " + decl.kind().wire() + " shows one of its choices, and \"\" is none");
      synchronized (this) {
        value = v;
      }
      Panel.this.set(widgetId, Value.newBuilder().setS(v).build());
    }

    public Picked onChange(Consumer<String> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value a = args.get("value");
      if (a == null || !a.hasS() || a.getS().isEmpty()) {
        refuse("a picker's value is a non-empty string");
        return;
      }
      if (color && !WidgetDecl.ColorPicker.isColor(a.getS())) {
        refuse("'" + a.getS() + "' is not a #RRGGBB color");
        return;
      }
      Value n = args.get("name");
      synchronized (this) {
        value = a.getS();
        name = n != null && n.hasS() ? n.getS() : "";
      }
      remember(widgetId, a);
      for (Consumer<String> l : listeners) l.accept(a.getS());
    }
  }

  public final class Rows extends Bound {
    /** The longest row key the protocol takes (§8.4). */
    public static final int MAX_KEY = 128;

    private final WidgetDecl.RowList list;
    private final java.util.Set<String> cells = new java.util.HashSet<>();
    private final List<Consumer<String>> listeners = new ArrayList<>();
    private List<String> keys = List.of();
    private String selected = "";

    Rows(WidgetDecl.RowList list) {
      super(list);
      this.list = list;
      for (WidgetDecl c : list.row()) cells.add(c.id());
    }

    /** The rows, in display order. */
    public synchronized List<String> keys() {
      return keys;
    }

    /**
     * Sets the rows, in display order: the whole list, replacing the last. A new key's row starts
     * from the template; a key that leaves drops its row and everything set on it. Keys are
     * non-empty, distinct, at most 128 characters, and no more than the list's {@code max_rows}.
     */
    public void set(List<String> newKeys) {
      List<String> copy = List.copyOf(newKeys);
      if (copy.size() > list.maxRows()) {
        throw new IllegalArgumentException(copy.size() + " rows for list " + list.id() + ", whose max_rows is " + list.maxRows());
      }
      java.util.Set<String> seen = new java.util.HashSet<>();
      for (String k : copy) {
        if (k.isEmpty() || k.length() > MAX_KEY) throw new IllegalArgumentException("a row key is 1 to " + MAX_KEY + " characters: '" + k + "'");
        if (!seen.add(k)) throw new IllegalArgumentException("row key '" + k + "' appears twice");
      }
      // one step under the panel's lock: no cell op for a dropped row can be queued after the
      // new set_rows, whether from another thread's set or from a PanelOpened re-push
      synchronized (Panel.this) {
        synchronized (this) {
          keys = copy;
          if (!selected.isEmpty() && !seen.contains(selected)) selected = "";
        }
        // the rows that left drop what was set on them, here as on the host
        mirror.values().removeIf(st -> !st.row.isEmpty() && cells.contains(st.widget) && !seen.contains(st.row));
        Session s = rt.sessionOrNull();
        if (s != null) s.outbound().dropRows(Panel.this.decl.id(), cells, seen); // the PANEL's id: Bound.decl is the list's own
        op(list.id(), PatchOp.newBuilder().setSetRows(vizi.plugin.v1.ViziPlugin.RowKeys.newBuilder().addAllKeys(copy)), st -> st.rows = copy);
      }
    }

    /** One row's cells; {@code key} must be one of {@link #keys()}. */
    public Row row(String key) {
      synchronized (this) {
        if (!keys.contains(key)) throw new IllegalArgumentException("list " + list.id() + " holds no row '" + key + "'; set it with set(keys) first");
      }
      return new Row(this, key);
    }

    /** A selectable list's selected row, if any (not vizi's region selection). */
    public synchronized Optional<String> selected() {
      return selected.isEmpty() ? Optional.empty() : Optional.of(selected);
    }

    /** Selects {@code key}, or clears the selection with {@code ""}. */
    public void select(String key) {
      if (!list.selectable()) throw new IllegalStateException("list " + list.id() + " is not selectable");
      synchronized (this) {
        if (!key.isEmpty() && !keys.contains(key)) throw new IllegalArgumentException("list " + list.id() + " holds no row '" + key + "'");
        selected = key;
      }
      Panel.this.set(list.id(), Value.newBuilder().setS(key).build());
    }

    /** Runs on the command worker when the person selects a row. */
    public Rows onSelect(Consumer<String> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    boolean isCell(String widgetId) {
      return cells.contains(widgetId);
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value r = args.get("row");
      if (r == null || !r.hasS()) {
        refuse("a list selection names its row");
        return;
      }
      String key = r.getS();
      boolean held;
      synchronized (this) {
        held = key.isEmpty() || keys.contains(key);
        if (held) selected = key;
      }
      // refused after leaving this lock: refuse() takes the panel's, and the panel's lock is
      // always taken before this one (Rows.set, every cell op), never after
      if (!held) {
        refuse("list " + list.id() + " holds no row '" + key + "'");
        return;
      }
      remember(list.id(), Value.newBuilder().setS(key).build());
      for (Consumer<String> l : listeners) l.accept(key);
    }
  }

  /** One row of a list: every panel op, addressed to this row's cell of a template widget. */
  public final class Row {
    private final Rows rows;
    private final String key;

    Row(Rows rows, String key) {
      this.rows = rows;
      this.key = key;
    }

    public String key() {
      return key;
    }

    private void check(String widgetId) {
      if (!rows.isCell(widgetId)) throw new IllegalArgumentException("widget " + widgetId + " is not a cell of list " + rows.id());
      if (!rows.keys().contains(key)) throw new IllegalStateException("list " + rows.id() + " no longer holds row '" + key + "'");
    }

    /** A label's text, or a line edit's, dropdown's or picker's value, as text. */
    public Row set(String widgetId, String text) {
      return value(widgetId, Value.newBuilder().setS(text).build());
    }

    public Row value(String widgetId, Value v) {
      opCell(widgetId, PatchOp.newBuilder().setSetValue(v), st -> st.value = v);
      return this;
    }

    public Row enabled(String widgetId, boolean on) {
      opCell(widgetId, PatchOp.newBuilder().setSetEnabled(on), st -> st.enabled = on);
      return this;
    }

    public Row visible(String widgetId, boolean on) {
      opCell(widgetId, PatchOp.newBuilder().setSetVisible(on), st -> st.visible = on);
      return this;
    }

    public Row options(String widgetId, List<String> options) {
      List<String> copy = List.copyOf(options);
      opCell(widgetId, PatchOp.newBuilder().setSetOptions(Options.newBuilder().addAllOptions(copy)), st -> st.options = copy);
      return this;
    }

    public Row progress(String widgetId, double fraction) {
      var p = vizi.plugin.v1.ViziPlugin.Progress.newBuilder().setValue(fraction).build();
      opCell(widgetId, PatchOp.newBuilder().setSetProgress(p), st -> st.progress = p);
      return this;
    }

    /** A PNG into this row's image cell, through the panel's arena (held until its {@code P} segment installs). */
    public Row image(String widgetId, byte[] png) {
      byte[] copy = png.clone();
      synchronized (Panel.this) { // checked, recorded and queued as one step: see opRow
        check(widgetId);
        State st = cell(widgetId, key);
        st.png = copy;
        st.imageHeld = true;
        st.sentAtInstall = false;
        Session s = rt.sessionOrNull();
        if (s != null) st.imageHeld = !pushImage(s, widgetId, key, copy);
      }
      return this;
    }

    /** The row is checked, the op recorded and queued, as one step under the panel's lock. */
    private void opCell(String widgetId, PatchOp.Builder op, Consumer<State> record) {
      synchronized (Panel.this) {
        check(widgetId);
        opRow(widgetId, key, op, record);
      }
    }
  }

  /**
   * A table's rows and, when it is selectable, its selection. The rows are the SDK's mirror of what
   * the plugin sent, in the plugin's order (the host may show them sorted).
   */
  public final class Table extends Bound {
    private final WidgetDecl.Table table;
    private final List<Consumer<List<String>>> listeners = new ArrayList<>();

    Table(WidgetDecl.Table table) {
      super(table);
      this.table = table;
    }

    /** The rows, whole and in order, replacing the last: {@link Panel#setTable}. */
    public void set(List<TableRow> rows) {
      writeTable(table, rows, List.of(), true);
    }

    public void set(TableRow... rows) {
      set(List.of(rows));
    }

    /** Deletes, then upserts by key: {@link Panel#patchTable}. */
    public void patch(List<TableRow> upsert, java.util.Collection<String> delete) {
      writeTable(table, upsert, delete, false);
    }

    /** Each row replaces the row with its key in place, or is appended. */
    public void upsert(TableRow... rows) {
      patch(List.of(rows), List.of());
    }

    public void delete(String... keys) {
      patch(List.of(), List.of(keys));
    }

    /** The row keys, in the plugin's order. */
    public List<String> keys() {
      synchronized (Panel.this) {
        State st = mirror.get(widgetId);
        return st == null || st.table == null ? List.of() : List.copyOf(st.table.keySet());
      }
    }

    /** The selected row keys (none, one, or with {@code multiple} several). */
    public List<String> selected() {
      synchronized (Panel.this) {
        State st = mirror.get(widgetId);
        return selection(st == null ? null : st.value);
      }
    }

    /**
     * Selects {@code keys}, rows the table holds (none clears the selection); never fires the
     * command. A table that is not {@code multiple} selects one row at most.
     */
    public void select(List<String> keys) {
      if (!table.selectable()) throw new IllegalStateException("table " + table.id() + " is not selectable");
      List<String> copy = List.copyOf(keys);
      if (!table.multiple() && copy.size() > 1) throw new IllegalArgumentException("table " + table.id() + " is not multiple: one row at most");
      synchronized (Panel.this) { // checked and sent as one step: no set_table drops a key in between
        State st = mirror.get(widgetId);
        for (String k : copy) {
          if (st == null || st.table == null || !st.table.containsKey(k)) throw new IllegalArgumentException("table " + table.id() + " holds no row '" + k + "'");
        }
        Panel.this.set(widgetId, selectionValue(copy));
      }
    }

    public void select(String... keys) {
      select(List.of(keys));
    }

    /** Runs on the command worker when the person changes the selection, with the keys selected. */
    public Table onSelect(Consumer<List<String>> listener) {
      rt.checkOpen("adding a listener");
      listeners.add(listener);
      return this;
    }

    private Value selectionValue(List<String> keys) {
      if (table.multiple()) return Values.ofStrings(keys);
      return Value.newBuilder().setS(keys.isEmpty() ? "" : keys.get(0)).build();
    }

    @Override
    void accept(Map<String, Value> args) throws Refusal {
      Value r = args.get("rows");
      // always Value.strings; a cleared selection is one with no entries
      if (r == null || !r.hasStrings()) {
        refuse("a table selection names its rows, a list of keys");
        return;
      }
      List<String> keys = List.copyOf(r.getStrings().getValuesList());
      String missing = null;
      synchronized (Panel.this) {
        State st = mirror.get(widgetId);
        for (String k : keys) {
          if (st == null || st.table == null || !st.table.containsKey(k)) {
            missing = k;
            break;
          }
        }
        if (missing == null && (table.multiple() || keys.size() <= 1)) remember(widgetId, selectionValue(keys));
      }
      // refused outside the panel's lock, which refuse() takes again for the push-back
      if (missing != null) {
        refuse("table " + table.id() + " holds no row '" + missing + "'");
        return;
      }
      if (!table.multiple() && keys.size() > 1) {
        refuse("table " + table.id() + " is not multiple, and " + keys.size() + " rows were selected");
        return;
      }
      for (Consumer<List<String>> l : listeners) l.accept(keys);
    }
  }

  // ---- internals ----

  private WidgetDecl.Table tableDecl(String tableId) {
    WidgetDecl w = widget(tableId);
    if (!(w instanceof WidgetDecl.Table t)) throw new IllegalArgumentException("widget " + tableId + " is a " + w.kind().wire() + ", not a table");
    return t;
  }

  /** A table's selection as keys: {@code Value.s} (one key, or none for {@code ""}) or {@code Value.strings}. */
  private static List<String> selection(@Nullable Value v) {
    if (v == null) return List.of();
    if (v.hasStrings()) return List.copyOf(v.getStrings().getValuesList());
    if (v.hasS() && !v.getS().isEmpty()) return List.of(v.getS());
    return List.of();
  }

  /** The selection still held once a table's rows changed: the keys still there, as the host keeps it. */
  private static @Nullable Value keptSelection(@Nullable Value v, java.util.function.Predicate<String> kept) {
    if (v == null) return null;
    if (v.hasS() && !v.getS().isEmpty() && !kept.test(v.getS())) return Value.newBuilder().setS("").build();
    if (v.hasStrings()) return Values.ofStrings(v.getStrings().getValuesList().stream().filter(kept).toList());
    return v;
  }

  /** What one table op's rows may encode to: one message's 1 MiB, less room for its ids (as the reference). */
  long tableBudget(String tableId) {
    return WidgetDecl.Table.MAX_PATCH_BYTES - 1024L - utf8(decl.id()) - utf8(tableId);
  }

  private static long utf8(String s) {
    return s.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
  }

  /**
   * Checks and builds rows, then records them in the mirror and queues them as ONE step under the
   * panel's lock (see {@link #opRow}): a whole statement ({@code whole}) or a patch.
   */
  private void writeTable(WidgetDecl.Table t, List<TableRow> rows, java.util.Collection<String> delete, boolean whole) {
    String id = t.id();
    List<String> columns = t.columnIds();
    long budget = tableBudget(id);
    List<vizi.plugin.v1.ViziPlugin.TableRow> built = new ArrayList<>(rows.size());
    java.util.Set<String> keys = new java.util.HashSet<>();
    for (TableRow r : rows) {
      String key = r.key();
      int chars = key.codePointCount(0, key.length());
      if (chars == 0 || chars > WidgetDecl.Table.MAX_KEY) {
        throw new IllegalArgumentException("table row keys are non-empty and at most " + WidgetDecl.Table.MAX_KEY + " characters, not '" + key + "'");
      }
      if (!keys.add(key)) throw new IllegalArgumentException("table row keys must be distinct in one op: '" + key + "' appears twice");
      List<@Nullable Object> cells = r.cells(id, columns);
      if (cells.size() > columns.size()) {
        throw new IllegalArgumentException("a row of table " + id + " has " + cells.size() + " cells, and the table " + columns.size() + " columns");
      }
      var b = vizi.plugin.v1.ViziPlugin.TableRow.newBuilder().setKey(key);
      for (Object c : cells) b.addCells(TableRow.cell(c));
      var row = b.build();
      if (row.getSerializedSize() + 8L > budget) {
        throw new IllegalArgumentException("row '" + key + "' of table " + id + " encodes to " + row.getSerializedSize()
            + " bytes, past what one message (1 MiB) carries");
      }
      built.add(row);
    }
    List<String> deletes = List.copyOf(delete);
    synchronized (this) {
      State st = state(id);
      LinkedHashMap<String, vizi.plugin.v1.ViziPlugin.TableRow> now = st.table == null ? new LinkedHashMap<>() : st.table;
      if (whole) {
        if (built.size() > WidgetDecl.Table.MAX_ROWS) {
          throw new IllegalArgumentException("a table holds at most " + WidgetDecl.Table.MAX_ROWS + " rows, not " + built.size());
        }
      } else {
        java.util.Set<String> gone = new java.util.HashSet<>();
        for (String k : deletes) if (now.containsKey(k)) gone.add(k);
        java.util.Set<String> added = new java.util.HashSet<>();
        for (var r : built) if (!now.containsKey(r.getKey()) || gone.contains(r.getKey())) added.add(r.getKey());
        long after = (long) now.size() - gone.size() + added.size();
        if (after > WidgetDecl.Table.MAX_ROWS) {
          throw new IllegalArgumentException("the patch would take table " + id + " to " + after + " rows, past its "
              + WidgetDecl.Table.MAX_ROWS);
        }
      }
      List<PatchOp> ops = whole ? tableOps(id, budget, built, null) : tableOps(id, budget, built, deletes);
      // after each op the host keeps the selected keys still there; a key a patch deletes is a
      // fresh row even when the same patch upserts it again, so it leaves the selection
      java.util.Set<String> present = new java.util.HashSet<>(now.keySet());
      for (PatchOp op : ops) {
        java.util.Set<String> fresh = java.util.Set.of();
        if (op.hasSetTable()) {
          present.clear();
          for (var r : op.getSetTable().getRowsList()) present.add(r.getKey());
        } else {
          fresh = new java.util.HashSet<>(op.getPatchTable().getDeleteList());
          present.removeAll(fresh);
          for (var r : op.getPatchTable().getUpsertList()) present.add(r.getKey());
        }
        java.util.Set<String> gone = fresh;
        st.value = keptSelection(st.value, k -> present.contains(k) && !gone.contains(k));
      }
      LinkedHashMap<String, vizi.plugin.v1.ViziPlugin.TableRow> next = whole ? new LinkedHashMap<>() : now;
      for (String k : deletes) next.remove(k);
      for (var r : built) next.put(r.getKey(), r); // in place when held, appended when new or just deleted
      st.table = next;
      Session s = rt.sessionOrNull();
      if (s != null) for (PatchOp op : ops) s.outbound().patch(decl.id(), id, op, Outbound.Loans.NONE);
    }
  }

  /**
   * A table's rows as ops of at most one message each, in order: a whole statement ({@code
   * deletes} null) is a {@code set_table} and then appending {@code patch_table}s; a patch is its
   * deletes, then its upserts, split where a message fills: at its byte budget, or when its larger
   * repeated field, rows or deleted keys each counted on its own, reaches {@link
   * WidgetDecl.Table#MAX_ROWS_PER_MESSAGE}, whichever comes first. The last run of deletes shares
   * its message with the first run of upserts, as the reference splits.
   */
  static List<PatchOp> tableOps(String tableId, long budget, List<vizi.plugin.v1.ViziPlugin.TableRow> rows,
      @Nullable List<String> deletes) {
    int cap = WidgetDecl.Table.MAX_ROWS_PER_MESSAGE;
    List<PatchOp> out = new ArrayList<>();
    boolean statement = deletes == null;
    List<String> dels = new ArrayList<>();
    List<vizi.plugin.v1.ViziPlugin.TableRow> ups = new ArrayList<>();
    long size = 0;
    if (deletes != null) {
      for (String k : deletes) {
        long cost = utf8(k) + 4L;
        if (!dels.isEmpty() && (size + cost > budget || dels.size() >= cap)) {
          out.add(tablePatch(tableId, dels, ups));
          dels = new ArrayList<>();
          size = 0;
        }
        dels.add(k);
        size += cost;
      }
    }
    for (var r : rows) {
      long cost = r.getSerializedSize() + 8L;
      if ((!dels.isEmpty() || !ups.isEmpty()) && (size + cost > budget || ups.size() >= cap)) {
        out.add(statement && out.isEmpty() ? tableSet(tableId, ups) : tablePatch(tableId, dels, ups));
        dels = new ArrayList<>();
        ups = new ArrayList<>();
        size = 0;
      }
      ups.add(r);
      size += cost;
    }
    if (statement && out.isEmpty()) out.add(tableSet(tableId, ups)); // an empty table is a statement too
    else if (!dels.isEmpty() || !ups.isEmpty()) out.add(tablePatch(tableId, dels, ups));
    return out;
  }

  private static PatchOp tableSet(String tableId, List<vizi.plugin.v1.ViziPlugin.TableRow> rows) {
    return op(tableId, "").setSetTable(vizi.plugin.v1.ViziPlugin.TableRows.newBuilder().addAllRows(rows)).build();
  }

  private static PatchOp tablePatch(String tableId, List<String> deletes, List<vizi.plugin.v1.ViziPlugin.TableRow> upserts) {
    return op(tableId, "").setPatchTable(vizi.plugin.v1.ViziPlugin.TablePatch.newBuilder().addAllDelete(deletes).addAllUpsert(upserts)).build();
  }

  private static double parseOr0(String s) {
    try {
      return Double.parseDouble(s.trim());
    } catch (NumberFormatException e) {
      return 0;
    }
  }

  private WidgetDecl widget(String widgetId) {
    WidgetDecl w = widgets.get(Objects.requireNonNull(widgetId, "widgetId"));
    if (w == null) throw new IllegalArgumentException("panel " + decl.id() + " declares no widget " + widgetId);
    return w;
  }

  private <W extends Widget> W handle(String widgetId, Class<? extends WidgetDecl> kind, String what,
      java.util.function.Function<WidgetDecl, W> make) {
    notCell(widgetId);
    WidgetDecl w = widget(widgetId);
    if (!kind.isInstance(w)) throw new IllegalArgumentException("widget " + widgetId + " is a " + w.kind().wire() + ", not a " + what);
    synchronized (this) {
      Widget h = handles.get(widgetId);
      if (h != null) {
        @SuppressWarnings("unchecked")
        W same = (W) h;
        return same;
      }
      W made = make.apply(w);
      handles.put(widgetId, made);
      return made;
    }
  }

  private <B extends Bound> B bind(String widgetId, B b) {
    notCell(widgetId);
    rt.checkOpen("binding a widget");
    if (handles.containsKey(widgetId)) throw new IllegalStateException("widget " + widgetId + " is already bound");
    handles.put(widgetId, b);
    rt.bindWidgetCommand(b.command(), this, b);
    return b;
  }

  private synchronized State state(String widgetId) {
    return cell(widgetId, "");
  }

  private synchronized State cell(String widgetId, String row) {
    return mirror.computeIfAbsent(row.isEmpty() ? widgetId : widgetId + "\u0000" + row, k -> {
      State st = new State();
      st.widget = widgetId;
      st.row = row;
      return st;
    });
  }

  private void notCell(String widgetId) {
    String list = cellOf.get(widgetId);
    if (list != null) {
      throw new IllegalArgumentException("widget " + widgetId + " is a cell of list " + list + ": its values are per row;"
          + " address it with rows(\"" + list + "\").row(key), and read args[\"row\"] (CommandContext.row()) in its command");
    }
  }

  private void remember(String widgetId, Value v) {
    synchronized (this) {
      state(widgetId).value = v;
    }
  }

  /** Records a value and sends it. */
  private void set(String widgetId, Value v) {
    op(widgetId, PatchOp.newBuilder().setSetValue(v), st -> st.value = v);
  }

  private void pushValue(String widgetId, Value v) {
    op(widgetId, PatchOp.newBuilder().setSetValue(v), st -> {});
  }

  private void op(String widgetId, PatchOp.Builder op, Consumer<State> record) {
    opRow(widgetId, "", op, record);
  }

  /**
   * Records an op in the mirror and queues it, as ONE step under the panel's lock. Two steps would
   * let a {@code set_rows} (another thread's, or a {@code PanelOpened} re-push's) land between
   * them, and a cell op queued after the set_rows that dropped its row is refused by the host. The
   * outbound queue never calls back into the panel, so holding the lock across it is safe.
   */
  private void opRow(String widgetId, String row, PatchOp.Builder op, Consumer<State> record) {
    synchronized (this) {
      record.accept(cell(widgetId, row));
      Session s = rt.sessionOrNull();
      if (s != null) s.outbound().patch(decl.id(), widgetId, op.setWidgetId(widgetId).setRowKey(row).build(), Outbound.Loans.NONE);
    }
  }

  /**
   * Lends {@code png} from the panel's arena and queues the {@code set_image}; false when it is
   * HELD. With no {@code P} segment installed (it is granted when the panel opens, §8.4)
   * or only a retired one, the caller keeps it in the mirror, and it is sent when a {@code P}
   * installs ({@link #flushHeldImages}) or at the next {@code PanelOpened}, before the plugin's own
   * panel-opened hook, and only if its row is still listed. Two live {@code P} segments are the
   * host's error, not a hold: that throws.
   */
  private boolean pushImage(Session s, String widgetId, String row, byte[] png) {
    Segment seg;
    try {
      seg = s.store().live("P", decl.id());
    } catch (SegmentStore.AmbiguousException e) {
      throw new IllegalStateException("panel " + decl.id() + ": " + e.getMessage(), e);
    } catch (SegmentStore.UnavailableException e) {
      return false; // held in the mirror until the panel's segment installs
    }
    try (Loans loans = new Loans(s)) {
      SegmentRef ref;
      try {
        ref = loans.lend(seg, MemorySegment.ofArray(png), 1);
      } catch (Loans.Retired retired) {
        return false; // retired with nothing superseding it: held, sent at the next install or open
      }
      ImageRef img = ImageRef.newBuilder().setSegmentId(ref.getSegmentId()).setOffset(ref.getOffset()).setLen(ref.getLen()).build();
      s.outbound().patch(decl.id(), widgetId, PatchOp.newBuilder().setWidgetId(widgetId).setRowKey(row).setSetImage(img).build(), loans.forQueue());
      loans.admitted();
      return true;
    }
  }

  /**
   * The panel opened: re-push the whole mirror. The panel's own widgets first (a list's rows among
   * them), then the cells of rows the lists hold now, so no cell op names a row not yet set.
   */
  /** The Follow offer's label (§8.15): null never offered, {@code ""} withdrawn. */
  private @Nullable String followOffer;

  /**
   * Offers a Follow (§8.15): the host shows "Follow: {@code label}" in the panel's first row, and
   * the person may press it. Panel state: kept, and re-sent at every open; the latest offer wins.
   * The panel must declare {@code follow_targets}, and the Follow is driven by the panel's {@link
   * Plugin.Builder#follow} driver.
   */
  public void offerFollow(String label) {
    if (label.isEmpty()) throw new IllegalArgumentException("an offer has a label; withdraw with withdrawFollow()");
    sendOffer(label);
  }

  /** Withdraws the offer; a Follow running from it ends {@code WITHDRAWN}. */
  public void withdrawFollow() {
    sendOffer("");
  }

  private void sendOffer(String label) {
    if (decl.followTargets().isEmpty()) throw new IllegalStateException("panel " + decl.id() + " declares no follow_targets");
    synchronized (this) {
      followOffer = label;
      Session s = rt.sessionOrNull();
      if (s != null) s.outbound().offerFollow(vizi.plugin.v1.ViziPlugin.OfferFollow.newBuilder().setPanelId(decl.id()).setLabel(label).build());
    }
  }

  void repush() {
    Session s = rt.sessionOrNull();
    if (s == null) return;
    // the snapshot and every op it queues are one step under the panel's lock (see opRow): a
    // concurrent set_rows either precedes the snapshot or supersedes what it queued
    synchronized (this) {
      repushLocked(s);
    }
  }

  /**
   * The panel's {@code P} segment installed: send the images held for want of one. vizi grants
   * {@code P} with no {@code PanelOpened} when it relaunches a plugin whose panel is open,
   * so an install alone must deliver them. An image sent here is not sent again by
   * the re-push of the {@code PanelOpened} that follows.
   */
  void flushHeldImages() {
    Session s = rt.sessionOrNull();
    if (s == null) return;
    synchronized (this) {
      for (State st : listed()) {
        if (!st.imageHeld) continue;
        sendImage(s, st);
        // vizi grants P before it sends PanelOpened: that open's re-push would lend it a second time
        if (!st.imageHeld) st.sentAtInstall = true;
      }
    }
  }

  /** The mirror's entries whose row, if they have one, the list still holds. */
  private List<State> listed() {
    List<State> out = new ArrayList<>();
    for (State st : mirror.values()) {
      if (!st.row.isEmpty()) {
        State list = mirror.get(cellOf.getOrDefault(st.widget, ""));
        if (list == null || list.rows == null || !list.rows.contains(st.row)) continue;
      }
      out.add(st);
    }
    return out;
  }

  private void sendImage(Session s, State st) {
    byte[] png = st.png;
    if (png == null) return;
    try {
      st.imageHeld = !pushImage(s, st.widget, st.row, png);
    } catch (RuntimeException ex) {
      st.imageHeld = true;
      rt.log("panel " + decl.id() + ": the image for " + st.widget + (st.row.isEmpty() ? "" : " row " + st.row)
          + " was not sent: " + ex.getMessage());
    }
  }

  private void repushLocked(Session s) {
    String offer = followOffer;
    if (offer != null) s.outbound().offerFollow(vizi.plugin.v1.ViziPlugin.OfferFollow.newBuilder().setPanelId(decl.id()).setLabel(offer).build());
    List<PatchOp> ops = new ArrayList<>();
    List<State> images = new ArrayList<>();
    List<State> listed = listed();
    {
      for (int pass = 0; pass < 2; pass++) {
        for (State st : listed) {
          boolean isCell = !st.row.isEmpty();
          if (isCell != (pass == 1)) continue;
          String w = st.widget;
          if (st.rows != null) ops.add(op(w, st.row).setSetRows(vizi.plugin.v1.ViziPlugin.RowKeys.newBuilder().addAllKeys(st.rows)).build());
          // a table is never re-sent at open, rows or selection: the host merges a closed panel's
          // held ops under §8.4 and drops none, so what was sent stands, and re-sending 100,000
          // rows at every open would only cost bandwidth. The mirror serves the handle's own reads.
          if (widgets.get(w) instanceof WidgetDecl.Table) continue; // nothing of a table's, its enablement included
          if (st.options != null) ops.add(op(w, st.row).setSetOptions(Options.newBuilder().addAllOptions(st.options)).build());
          if (st.value != null) ops.add(op(w, st.row).setSetValue(st.value).build());
          if (st.progress != null) ops.add(op(w, st.row).setSetProgress(st.progress).build());
          if (st.enabled != null) ops.add(op(w, st.row).setSetEnabled(st.enabled).build());
          if (st.visible != null) ops.add(op(w, st.row).setSetVisible(st.visible).build());
          if (st.png != null) images.add(st);
        }
      }
    }
    for (PatchOp op : ops) s.outbound().patch(decl.id(), op.getWidgetId(), op, Outbound.Loans.NONE);
    for (State st : images) {
      if (st.sentAtInstall) {
        st.sentAtInstall = false; // went at this open's P install: once per open
        continue;
      }
      sendImage(s, st);
    }
  }

  private static PatchOp.Builder op(String widgetId, String row) {
    return PatchOp.newBuilder().setWidgetId(widgetId).setRowKey(row);
  }
}
