package vizi.plugin.javasdk.manifest;

import com.google.gson.Strictness;
import com.google.gson.stream.JsonWriter;
import java.io.IOException;
import java.io.StringWriter;
import java.io.UncheckedIOException;
import java.util.List;

/**
 * Writes a {@link Manifest} as {@code vizi-plugin.json}: two-space indent, keys in the order the
 * spec's tables list them, a trailing newline. A field equal to its default is omitted, so the
 * output is canonical: equal manifests write equal text, and {@link ManifestParser} reads it back
 * to an equal model.
 */
public final class ManifestWriter {
  private ManifestWriter() {}

  /**
   * Writes the manifest's canonical JSON.
   *
   * @throws IllegalArgumentException if a number in the model is not finite (JSON has no NaN)
   */
  public static String write(Manifest m) {
    StringWriter out = new StringWriter();
    try (JsonWriter w = new JsonWriter(out)) {
      w.setIndent("  ");
      w.setSerializeNulls(true);
      w.setHtmlSafe(false);
      w.setStrictness(Strictness.STRICT);
      manifest(w, m);
    } catch (IOException e) {
      throw new UncheckedIOException(e); // a StringWriter does not fail
    }
    return out + "\n";
  }

  private static void manifest(JsonWriter w, Manifest m) throws IOException {
    w.beginObject();
    w.name("manifest_version").value(m.manifestVersion());
    w.name("id").value(m.id());
    w.name("name").value(m.name());
    w.name("version").value(m.version());
    w.name("engines").beginObject().name("vizi").value(m.engine().text()).endObject();
    w.name("entry").beginObject().name("command");
    strings(w, m.entryCommand());
    if (m.startupS().isPresent()) {
      double s = m.startupS().getAsDouble();
      w.name("startup_s");
      // whole seconds as an integer, as a person writes them ({@code 90}, not {@code 90.0})
      if (s == Math.rint(s) && Math.abs(s) < 1e15) w.value((long) s);
      else number(w, s);
    }
    w.endObject();

    Contributions c = m.contributions();
    if (!c.isEmpty()) {
      w.name("contributions").beginObject();
      if (!c.readers().isEmpty()) {
        w.name("readers").beginArray();
        for (ReaderDecl r : c.readers()) reader(w, r);
        w.endArray();
      }
      if (!c.commands().isEmpty()) {
        w.name("commands").beginArray();
        for (CommandDecl x : c.commands()) command(w, x);
        w.endArray();
      }
      if (!c.panels().isEmpty()) {
        w.name("panels").beginArray();
        for (PanelDecl p : c.panels()) panel(w, p);
        w.endArray();
      }
      if (!c.exporters().isEmpty()) {
        w.name("exporters").beginArray();
        for (ExporterDecl e : c.exporters()) exporter(w, e);
        w.endArray();
      }
      if (!c.overlays().isEmpty()) {
        w.name("overlays").beginArray();
        for (OverlayDecl o : c.overlays()) overlay(w, o);
        w.endArray();
      }
      if (!c.tools().isEmpty()) {
        w.name("tools").beginArray();
        for (ToolDecl t : c.tools()) tool(w, t);
        w.endArray();
      }
      if (!c.stages().isEmpty()) {
        w.name("stages").beginArray();
        for (StageDecl s : c.stages()) stage(w, s);
        w.endArray();
      }
      if (!c.regionTypes().isEmpty()) {
        w.name("region_types").beginArray();
        for (RegionTypeDecl r : c.regionTypes()) {
          w.beginObject();
          idAndName(w, r.id(), r.name());
          w.endObject();
        }
        w.endArray();
      }
      w.endObject();
    }

    if (!m.assets().isEmpty()) {
      w.name("assets").beginArray();
      for (AssetDecl a : m.assets()) asset(w, a);
      w.endArray();
    }
    if (!m.permissions().isEmpty()) {
      strings(w.name("permissions"), m.permissions().stream().map(Permission::wire).toList());
    }
    if (!m.subscriptions().isEmpty()) {
      strings(w.name("subscriptions"), m.subscriptions().stream().map(Subscription::wire).toList());
    }
    w.endObject();
  }

  private static void strings(JsonWriter w, List<String> xs) throws IOException {
    w.beginArray();
    for (String x : xs) w.value(x);
    w.endArray();
  }

  /** {@code name} is written only when it differs from the id it defaults to. */
  private static void idAndName(JsonWriter w, String id, String name) throws IOException {
    w.name("id").value(id);
    if (!name.equals(id)) w.name("name").value(name);
  }

  private static void reader(JsonWriter w, ReaderDecl r) throws IOException {
    w.beginObject();
    idAndName(w, r.id(), r.name());
    if (!r.extensions().isEmpty() || r.fallbackExtensions().isEmpty()) strings(w.name("extensions"), r.extensions());
    if (!r.fallbackExtensions().isEmpty()) strings(w.name("fallback_extensions"), r.fallbackExtensions());
    w.endObject();
  }

  private static void command(JsonWriter w, CommandDecl c) throws IOException {
    w.beginObject();
    w.name("id").value(c.id());
    w.name("title").value(c.title());
    if (!c.description().isEmpty()) w.name("description").value(c.description());
    if (c.defaultKeybinding().isPresent()) {
      w.name("default_keybinding").value(c.defaultKeybinding().get());
    }
    if (c.menu() == Menu.NONE) w.name("menu").nullValue();
    if (c.pane().isPresent()) paneButton(w, c.pane().get());
    w.endObject();
  }

  private static void paneButton(JsonWriter w, PaneButton p) throws IOException {
    w.name("pane").beginObject();
    w.name("icon").value(p.icon());
    if (p.tooltip().isPresent()) w.name("tooltip").value(p.tooltip().get());
    w.endObject();
  }

  private static void panel(JsonWriter w, PanelDecl p) throws IOException {
    w.beginObject();
    w.name("id").value(p.id());
    w.name("title").value(p.title());
    if (p.menuLabel().isPresent()) w.name("menu_label").value(p.menuLabel().get());
    if (!p.viewTargets().isEmpty()) strings(w.name("view_targets"), p.viewTargets().stream().map(ViewTarget::wire).toList());
    if (!p.followTargets().isEmpty()) strings(w.name("follow_targets"), p.followTargets().stream().map(ViewTarget::wire).toList());
    w.name("root");
    widget(w, p.root());
    w.endObject();
  }

  private static void widget(JsonWriter w, WidgetDecl x) throws IOException {
    w.beginObject();
    w.name("kind").value(x.kind().wire());
    w.name("id").value(x.id());
    switch (x) {
      case WidgetDecl.Label l -> {
        if (l.text().isPresent()) w.name("text").value(l.text().get());
        if (l.textFormat() != WidgetDecl.TextFormat.PLAIN) w.name("text_format").value(l.textFormat().wire());
      }
      case WidgetDecl.Button b -> {
        w.name("command").value(b.command());
        if (b.text().isPresent()) w.name("text").value(b.text().get());
      }
      case WidgetDecl.Slider s -> {
        w.name("command").value(s.command());
        if (Double.compare(s.min(), WidgetDecl.Slider.DEFAULT_MIN) != 0) {
          number(w.name("min"), s.min());
        }
        if (Double.compare(s.max(), WidgetDecl.Slider.DEFAULT_MAX) != 0) {
          number(w.name("max"), s.max());
        }
        if (Double.compare(s.step(), WidgetDecl.Slider.DEFAULT_STEP) != 0) {
          number(w.name("step"), s.step());
        }
        if (s.value().isPresent()) number(w.name("value"), s.value().getAsDouble());
        if (s.isFloat()) w.name("float").value(true);
      }
      case WidgetDecl.Dropdown d -> {
        w.name("command").value(d.command());
        strings(w.name("options"), d.options());
        if (d.value().isPresent()) w.name("value").value(d.value().get());
      }
      case WidgetDecl.Checkbox cb -> {
        w.name("command").value(cb.command());
        if (cb.text().isPresent()) w.name("text").value(cb.text().get());
        if (cb.value().isPresent()) w.name("value").value(cb.value().get().booleanValue());
      }
      case WidgetDecl.LineEdit le -> {
        w.name("command").value(le.command());
        if (le.placeholder().isPresent()) w.name("placeholder").value(le.placeholder().get());
        if (le.value().isPresent()) w.name("value").value(le.value().get());
      }
      case WidgetDecl.ChannelPicker cp -> {
        w.name("command").value(cp.command());
        if (cp.multiple()) w.name("multiple").value(true);
        if (cp.value().isPresent()) {
          if (cp.multiple()) {
            w.name("value").beginArray();
            for (int i : cp.value().get()) w.value(i);
            w.endArray();
          } else {
            w.name("value").value(cp.value().get().get(0));
          }
        }
      }
      case WidgetDecl.ColorPicker cp -> {
        w.name("command").value(cp.command());
        if (cp.value().isPresent()) w.name("value").value(cp.value().get());
      }
      case WidgetDecl.ColormapPicker cp -> {
        w.name("command").value(cp.command());
        if (cp.value().isPresent()) w.name("value").value(cp.value().get());
      }
      case WidgetDecl.RoiPicker rp -> {
        w.name("command").value(rp.command());
        if (rp.value().isPresent()) w.name("value").value(rp.value().get());
        if (rp.followsSelection()) w.name("follows_selection").value(true);
        if (rp.filter() != WidgetDecl.RoiFilter.ALL) w.name("filter").value(rp.filter().wire());
      }
      case WidgetDecl.RowList l -> {
        w.name("row").beginArray();
        for (WidgetDecl cell : l.row()) widget(w, cell);
        w.endArray();
        if (l.maxRows() != WidgetDecl.RowList.DEFAULT_MAX_ROWS) w.name("max_rows").value(l.maxRows());
        if (l.visibleRows() != WidgetDecl.RowList.DEFAULT_VISIBLE_ROWS) w.name("visible_rows").value(l.visibleRows());
        if (l.selectable()) {
          w.name("selectable").value(true);
          w.name("command").value(l.command());
        }
      }
      case WidgetDecl.Image i -> {
        if (!i.command().isEmpty()) w.name("command").value(i.command());
      }
      case WidgetDecl.Progress p -> {
        if (Double.compare(p.value(), 0) != 0) number(w.name("value"), p.value());
        if (p.indeterminate()) w.name("indeterminate").value(true);
      }
      case WidgetDecl.Row r -> children(w, r.children());
      case WidgetDecl.Column c -> children(w, c.children());
    }
    if (x.movesView()) w.name("moves_view").value(true);
    w.endObject();
  }

  private static void children(JsonWriter w, List<WidgetDecl> children) throws IOException {
    w.name("children").beginArray();
    for (WidgetDecl c : children) widget(w, c);
    w.endArray();
  }

  private static void exporter(JsonWriter w, ExporterDecl e) throws IOException {
    w.beginObject();
    idAndName(w, e.id(), e.name());
    w.name("extension").value(e.extension());
    w.endObject();
  }

  private static void overlay(JsonWriter w, OverlayDecl o) throws IOException {
    w.beginObject();
    idAndName(w, o.id(), o.name());
    switch (o.kind()) {
      case OverlayDecl.Drawn d -> {
        if (d.segmentMib() != OverlayDecl.DEFAULT_SEGMENT_MIB) {
          w.name("segment_mib").value(d.segmentMib());
        }
      }
      case OverlayDecl.Chunked c -> {
        w.name("chunked").value(true);
        if (c.time() != OverlayDecl.TimeMode.PER_T) w.name("time").value(c.time().wire());
      }
    }
    w.endObject();
  }

  private static void tool(JsonWriter w, ToolDecl t) throws IOException {
    w.beginObject();
    idAndName(w, t.id(), t.name());
    // written even at the default: a reader seeing the list need not know the default
    strings(w.name("views"), t.views().stream().map(ToolDecl.View::wire).toList());
    if (t.menu() == Menu.NONE) w.name("menu").nullValue();
    if (t.mode() != ToolDecl.Mode.GESTURE) w.name("mode").value(t.mode().wire());
    if (!t.cancelOnCameraMove()) w.name("cancel_on_camera_move").value(false);
    if (!t.wantsPick()) w.name("wants_pick").value(false);
    if (!t.viewTargets().isEmpty()) strings(w.name("view_targets"), t.viewTargets().stream().map(ViewTarget::wire).toList());
    if (t.pane().isPresent()) {
      ToolPane p = t.pane().get();
      w.name("pane").beginObject();
      w.name("icon").value(p.icon());
      if (p.tooltip().isPresent()) w.name("tooltip").value(p.tooltip().get());
      if (p.hold() != Hold.TOGGLE) w.name("hold").value(p.hold().wire());
      w.endObject();
    }
    if (!t.triggers().isEmpty()) {
      w.name("triggers").beginArray();
      for (TriggerDecl tr : t.triggers()) {
        w.beginObject();
        switch (tr) {
          case TriggerDecl.Key k -> {
            w.name("key").value(k.key());
            if (k.hold() != Hold.TOGGLE) w.name("hold").value(k.hold().wire());
          }
          case TriggerDecl.Mouse m -> {
            w.name("button").value(m.button().wire());
            if (!m.modifiers().isEmpty()) {
              strings(w.name("modifiers"), m.modifiers().stream().map(TriggerDecl.Modifier::wire).toList());
            }
          }
        }
        w.endObject();
      }
      w.endArray();
    }
    if (!t.actions().isEmpty()) {
      w.name("actions").beginArray();
      for (ActionDecl a : t.actions()) {
        w.beginObject();
        idAndName(w, a.id(), a.name());
        if (a.key().isPresent()) w.name("key").value(a.key().get());
        if (a.pane().isPresent()) paneButton(w, a.pane().get());
        w.endObject();
      }
      w.endArray();
    }
    w.endObject();
  }

  private static void stage(JsonWriter w, StageDecl s) throws IOException {
    w.beginObject();
    idAndName(w, s.id(), s.name());
    if (!s.params().isEmpty()) {
      w.name("params").beginArray();
      for (StageParam p : s.params()) param(w, p);
      w.endArray();
    }
    w.endObject();
  }

  private static void param(JsonWriter w, StageParam p) throws IOException {
    w.beginObject();
    w.name("key").value(p.key());
    if (!p.name().equals(p.key())) w.name("name").value(p.name());
    w.name("type").value(p.type().wire());
    switch (p) {
      case StageParam.FloatParam f -> {
        number(w.name("value"), f.value());
        if (f.min().isPresent()) number(w.name("min"), f.min().getAsDouble());
        if (f.max().isPresent()) number(w.name("max"), f.max().getAsDouble());
      }
      case StageParam.IntParam i -> {
        w.name("value").value(i.value());
        if (i.min().isPresent()) w.name("min").value(i.min().getAsLong());
        if (i.max().isPresent()) w.name("max").value(i.max().getAsLong());
      }
      case StageParam.BoolParam b -> w.name("value").value(b.value());
      case StageParam.ChoiceParam c -> {
        strings(w.name("options"), c.options());
        w.name("value").value(c.value());
      }
      case StageParam.StringParam s -> {
        w.name("value").value(s.value());
        if (s.maxLength() != StageParam.StringParam.MAX_LENGTH) {
          w.name("max_length").value(s.maxLength());
        }
      }
    }
    w.endObject();
  }

  private static void asset(JsonWriter w, AssetDecl a) throws IOException {
    w.beginObject();
    idAndName(w, a.id(), a.name());
    w.name("filename").value(a.filename());
    w.name("size").value(a.size());
    w.name("sha256").value(a.sha256());
    w.name("source").beginObject();
    switch (a.source()) {
      case AssetDecl.Manual m -> w.name("kind").value("manual");
      case AssetDecl.Https h -> {
        w.name("kind").value("https");
        w.name("url").value(h.url());
      }
    }
    w.endObject();
    w.endObject();
  }

  /** A double, always with a fraction or exponent ({@code 2.0}, {@code 1.0E20}), and finite. */
  private static void number(JsonWriter w, double d) throws IOException {
    if (!Double.isFinite(d)) throw new IllegalArgumentException("not a finite number: " + d);
    w.value(d);
  }
}
