package vizi.plugin.javasdk.manifest;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import java.io.IOException;
import java.math.BigInteger;
import java.net.URI;
import java.net.URISyntaxException;
import java.nio.ByteBuffer;
import java.nio.charset.CharacterCodingException;
import java.nio.charset.CodingErrorAction;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.InvalidPathException;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.List;
import java.util.Locale;
import java.util.Objects;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.OptionalLong;
import java.util.Set;
import java.util.regex.Pattern;
import java.util.stream.Collectors;
import java.util.stream.Stream;
import org.jspecify.annotations.Nullable;

/**
 * The manifest validator: {@code manifest_schema.md}, checked against its §8 fixtures.
 *
 * <p>JSON is read into a tree and every field is checked explicitly: types are checked, not
 * coerced (a boolean is not a number; {@code 1.0} is not an integer where the schema wants one),
 * required fields are required, and unknown fields are ignored except where §7 says otherwise.
 * The first rule broken ends the parse with a {@link ManifestException} naming the manifest, the
 * location and the rule. Warnings (§5's unknown permissions, §5.1's unknown subscriptions,
 * duplicate JSON keys) are returned in the {@link ParseResult}.
 */
public final class ManifestParser {
  /** The manifest's file name, at the plugin directory's root (§1). */
  public static final String FILE_NAME = "vizi-plugin.json";

  /** The largest pane icon, in bytes (§3.6.3). */
  public static final int MAX_ICON_BYTES = 64 * 1024;

  /** The longest {@code menu_label} and {@code pane.tooltip}, in characters (§3.3, §3.6.3). */
  public static final int MAX_CAPTION_CHARS = 128;

  private static final byte[] PNG_SIGNATURE = {
    (byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'
  };
  private static final Pattern ASSET_ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]*");
  private static final Pattern SHA256 = Pattern.compile("[0-9A-Fa-f]{64}");
  private static final List<String> CONTRIBUTION_KINDS =
      List.of("readers", "commands", "panels", "exporters", "overlays", "tools", "stages", "region_types");

  private ManifestParser() {}

  /**
   * Parses and validates manifest text read from {@code pluginDir}, whose files (pane icons) are
   * checked too.
   */
  public static ParseResult parse(String json, Path pluginDir) throws ManifestException {
    Objects.requireNonNull(pluginDir, "pluginDir");
    return new Run(pluginDir, pluginDir.resolve(FILE_NAME).toString()).manifest(json);
  }

  /**
   * Parses and validates manifest text with no plugin directory: every rule is checked except
   * that pane icons are not looked for (their paths and suffixes still are).
   */
  public static ParseResult parseDetached(String json) throws ManifestException {
    return new Run(null, FILE_NAME).manifest(json);
  }

  /** Reads {@code <pluginDir>/vizi-plugin.json} as strict UTF-8, then {@link #parse}s it. */
  public static ParseResult read(Path pluginDir) throws ManifestException {
    Path file = pluginDir.resolve(FILE_NAME);
    byte[] bytes;
    try {
      bytes = Files.readAllBytes(file);
    } catch (IOException e) {
      throw new ManifestException(file + ": cannot be read: " + e, e);
    }
    String text;
    try {
      text =
          StandardCharsets.UTF_8
              .newDecoder()
              .onMalformedInput(CodingErrorAction.REPORT)
              .onUnmappableCharacter(CodingErrorAction.REPORT)
              .decode(ByteBuffer.wrap(bytes))
              .toString();
    } catch (CharacterCodingException e) {
      throw new ManifestException(file + ": is not UTF-8 text", e);
    }
    return parse(text, pluginDir);
  }

  /**
   * Checks a manifest built in code against the same rules as a file: it is written, read back,
   * and must come back equal. With a {@code pluginDir}, pane icons are looked for there.
   */
  public static ParseResult validate(Manifest manifest, @Nullable Path pluginDir)
      throws ManifestException {
    String json = ManifestWriter.write(manifest);
    ParseResult r = pluginDir == null ? parseDetached(json) : parse(json, pluginDir);
    if (!r.manifest().equals(manifest)) {
      List<String> d = ManifestDiff.differences(manifest, r.manifest());
      throw new ManifestException(
          FILE_NAME
              + ": the manifest does not read back as written"
              + (d.isEmpty() ? "" : ": " + String.join("; ", d)));
    }
    return r;
  }

  // ---------------------------------------------------------------------------------------------

  /** One parse: the file label for diagnostics, the directory for icons, the warnings so far. */
  private static final class Run {
    private final @Nullable Path dir;
    private final String file;
    private final List<String> warnings = new ArrayList<>();

    Run(@Nullable Path dir, String file) {
      this.dir = dir;
      this.file = file;
    }

    private ManifestException err(String loc, String msg) {
      return new ManifestException(file + ": " + (loc.isEmpty() ? "" : loc + ": ") + msg);
    }

    private void warn(String msg) {
      warnings.add(file + ": " + msg);
    }

    // ---- top level (§2) ----

    /**
     * {@code engines.vizi} first, whatever else the manifest holds: a malformed one is refused, and
     * one this protocol does not run is an {@link EnginesIncompatibleException}. Only a string
     * {@code engines.vizi} is read here; a missing or non-string one is refused in the normal order.
     */
    private void enginesFirst(JsonObject o) throws ManifestException {
      JsonElement engines = o.get("engines");
      if (engines == null || !engines.isJsonObject()) return;
      JsonElement vizi = engines.getAsJsonObject().get("vizi");
      if (vizi == null || !vizi.isJsonPrimitive() || !vizi.getAsJsonPrimitive().isString()) return;
      String text = vizi.getAsString();
      EngineRequirement engine;
      try {
        engine = EngineRequirement.parse(text);
      } catch (IllegalArgumentException e) {
        throw err("", String.valueOf(e.getMessage()));
      }
      if (!engine.admitsCurrentProtocol()) {
        throw new EnginesIncompatibleException(
            topString(o, "id"), topString(o, "name"), topString(o, "version"), text);
      }
    }

    private static String topString(JsonObject o, String key) {
      JsonElement e = o.get(key);
      return e != null && e.isJsonPrimitive() && e.getAsJsonPrimitive().isString()
          ? e.getAsString()
          : "";
    }

    ParseResult manifest(String json) throws ManifestException {
      List<String> jsonWarnings = new ArrayList<>();
      JsonElement root;
      try {
        root = StrictJson.parse(json, jsonWarnings);
      } catch (StrictJson.NotJson e) {
        throw new ManifestException(file + ": is not valid JSON: " + e.getMessage(), e);
      }
      jsonWarnings.forEach(this::warn);
      if (!root.isJsonObject()) throw err("", "the top level must be a JSON object");
      JsonObject o = root.getAsJsonObject();

      enginesFirst(o);

      if (!o.has("manifest_version")) throw err("", "missing required field 'manifest_version'");
      BigInteger mv = integer(o.get("manifest_version"));
      if (mv == null) throw err("", "field 'manifest_version' must be int");
      if (!mv.equals(BigInteger.valueOf(Manifest.VERSION))) {
        throw err(
            "",
            "manifest_version " + mv + " unsupported (this host reads [1]; a 1.x addition raises"
                + " engines.vizi, which this one declares as " + declaredEngines(o)
                + ", never manifest_version)");
      }

      String id = reqString(o, "id", "");
      if (id.isBlank()) throw err("", "id must be non-empty");
      if (!Manifest.validId(id)) throw err("", Manifest.idMessage(id));
      String name = reqString(o, "name", "");
      String ver = reqString(o, "version", "");

      // An omitted engines reads as {"vizi": "1.0"}, the lowest floor.
      String vizi = "1.0";
      if (o.has("engines")) {
        JsonObject engines = reqObject(o, "engines", "");
        vizi = reqString(engines, "vizi", "engines");
      }
      EngineRequirement engine;
      try {
        engine = EngineRequirement.parse(vizi);
      } catch (IllegalArgumentException e) {
        throw err("", String.valueOf(e.getMessage()));
      }

      Optional<String> minVizi = Optional.empty();
      if (o.has("min_vizi")) {
        if (!isString(o.get("min_vizi"))) throw err("", "min_vizi must be a string");
        String v = o.get("min_vizi").getAsString();
        if (!Manifest.validMinVizi(v)) {
          throw err("", "min_vizi " + Wire.quote(v) + " is not a PEP 440 version");
        }
        minVizi = Optional.of(v);
      }
      Optional<String> sdkVersion = Optional.empty();
      if (o.has("sdk_version")) {
        JsonElement v = o.get("sdk_version");
        if (!isString(v) || v.getAsString().isEmpty()) {
          throw err("", "sdk_version must be a non-empty string");
        }
        sdkVersion = Optional.of(v.getAsString());
      }

      JsonObject entry = reqObject(o, "entry", "");
      List<String> command = stringList(entry.get("command"));
      if (command == null || command.isEmpty()) {
        throw err("", "entry.command must be a non-empty list of strings");
      }
      OptionalDouble startupS = OptionalDouble.empty();
      if (entry.has("startup_s")) {
        // §2.3: a positive number of seconds, never a boolean or a string
        StrictJson.Num n = num(entry.get("startup_s"));
        double d = n == null ? Double.NaN : n.doubleValue();
        if (!(d > 0) || !Double.isFinite(d)) {
          throw err("", "entry.startup_s must be a positive number of seconds");
        }
        startupS = OptionalDouble.of(d);
      }

      Contributions contributions = Contributions.NONE;
      if (o.has("contributions")) {
        JsonElement c = o.get("contributions");
        if (!c.isJsonObject()) throw err("", "contributions must be an object");
        contributions = contributions(c.getAsJsonObject());
      }

      List<AssetDecl> assets = List.of();
      if (o.has("assets")) {
        JsonElement a = o.get("assets");
        if (!a.isJsonArray()) throw err("", "assets must be a list");
        assets = assets(a.getAsJsonArray());
      }

      List<Permission> permissions =
          names(o, "permissions", Permission.class, "permission", "is ignored and grants nothing",
              engine.minor());
      if (!contributions.regionTypes().isEmpty() && !permissions.contains(Permission.CONTRIBUTE_ROIS)) {
        // §3.8: a type exists to be the type of regions the plugin contributes
        throw err(at("region_types", 0), "region_types need the contribute_rois permission");
      }
      List<Subscription> subscriptions =
          names(o, "subscriptions", Subscription.class, "subscription", "is ignored", engine.minor());
      if (!permissions.contains(Permission.DATA_ACCESS)) {
        // §5.1: view and regions push what data_access gates; without it they are warned and ignored
        List<Subscription> kept = new ArrayList<>();
        for (Subscription sub : subscriptions) {
          if (sub.needsDataAccess()) {
            warn("subscription " + Wire.quote(sub.wire()) + " needs the data_access permission; it is ignored");
          } else {
            kept.add(sub);
          }
        }
        subscriptions = kept;
      }

      Manifest m =
          new Manifest(
              id, name, ver, engine, minVizi, sdkVersion, command, startupS, contributions, assets,
              permissions, subscriptions);
      return new ParseResult(m, warnings);
    }

    /**
     * §5, §5.1: a list of strings; the known ones introduced at or before the manifest's {@code
     * engines.vizi} minor are kept, the rest warned about.
     */
    private <E extends Enum<E> & WireName & Introduced> List<E> names(
        JsonObject o, String field, Class<E> type, String what, String fate, long minor)
        throws ManifestException {
      if (!o.has(field)) return List.of();
      List<String> raw = stringList(o.get(field));
      if (raw == null) throw err("", field + " must be a list of strings");
      List<E> out = new ArrayList<>();
      for (String s : raw) {
        Optional<E> e = Wire.lookup(type, s).filter(n -> n.introducedMinor() <= minor);
        if (e.isPresent()) out.add(e.get());
        else warn("unknown " + what + " " + Wire.quote(s) + " " + fate);
      }
      return out;
    }

    /** {@code engines.vizi} as the unsupported-version diagnostic quotes it. */
    private static String declaredEngines(JsonObject o) {
      JsonElement engines = o.get("engines");
      JsonElement vizi = engines != null && engines.isJsonObject() ? engines.getAsJsonObject().get("vizi") : null;
      if (vizi == null) return "None";
      if (vizi.isJsonPrimitive() && vizi.getAsJsonPrimitive().isString()) return Wire.quote(vizi.getAsString());
      return vizi.toString();
    }

    // ---- contributions (§3) ----

    private Contributions contributions(JsonObject c) throws ManifestException {
      for (String kind : CONTRIBUTION_KINDS) {
        if (c.has(kind) && !c.get(kind).isJsonArray()) {
          throw err("", "contributions." + kind + " must be a list");
        }
      }
      List<ReaderDecl> readers = new ArrayList<>();
      Set<String> seen = new HashSet<>();
      int i = 0;
      for (JsonObject e : entries(c, "readers")) readers.add(reader(e, at("readers", i++), seen));

      List<CommandDecl> commands = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "commands")) {
        commands.add(command(e, at("commands", i++), seen));
      }
      Set<String> commandIds = new HashSet<>(seen);

      List<PanelDecl> panels = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "panels")) {
        panels.add(panel(e, at("panels", i++), seen, commandIds));
      }

      List<ExporterDecl> exporters = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "exporters")) {
        exporters.add(exporter(e, at("exporters", i++), seen));
      }

      List<OverlayDecl> overlays = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "overlays")) {
        overlays.add(overlay(e, at("overlays", i++), seen));
      }

      List<ToolDecl> tools = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "tools")) tools.add(tool(e, at("tools", i++), seen));
      // set-view's switch is the first row of a panel declaring view_targets (§3.3.1, §3.6)
      boolean switchRow = panels.stream().anyMatch(p -> !p.viewTargets().isEmpty());
      for (int t = 0; t < tools.size() && !switchRow; t++) {
        if (!tools.get(t).viewTargets().isEmpty()) {
          throw err(named(at("tools", t), tools.get(t).id()), "tool " + Wire.quote(tools.get(t).id())
              + " declares view_targets but no panel does; the move-the-view row lives on the plugin's panel");
        }
      }

      List<StageDecl> stages = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "stages")) stages.add(stage(e, at("stages", i++), seen));

      List<RegionTypeDecl> regionTypes = new ArrayList<>();
      seen = new HashSet<>();
      i = 0;
      for (JsonObject e : entries(c, "region_types")) regionTypes.add(regionType(e, at("region_types", i++), seen));

      // an overlay draws only while the plugin runs, and the host starts it only for what it always
      // offers (§3.5): a panel, a reader, an exporter, a stage, or a command or tool in the
      // Tools menu or with a pane button. A keybinding or a trigger alone can be lost to another binding
      boolean starter = !panels.isEmpty() || !readers.isEmpty() || !exporters.isEmpty() || !stages.isEmpty()
          || commands.stream().anyMatch(d -> d.menu() != Menu.NONE || d.pane().isPresent())
          || tools.stream().anyMatch(t -> t.menu() != Menu.NONE || t.pane().isPresent());
      if (!overlays.isEmpty() && !starter) {
        throw err(at("overlays", 0), "overlays are drawn only while the plugin runs, and nothing declared here starts it;"
            + " declare a panel (or a command or tool in the Tools menu or on the pane, a reader, an exporter or a stage)");
      }

      return new Contributions(readers, commands, panels, exporters, overlays, tools, stages, regionTypes);
    }

    private static String at(String kind, int i) {
      return "contributions." + kind + "[" + i + "]";
    }

    private List<JsonObject> entries(JsonObject c, String kind) throws ManifestException {
      if (!c.has(kind)) return List.of();
      List<JsonObject> out = new ArrayList<>();
      JsonArray a = c.get(kind).getAsJsonArray();
      for (int i = 0; i < a.size(); i++) {
        if (!a.get(i).isJsonObject()) throw err(at(kind, i), "each entry must be an object");
        out.add(a.get(i).getAsJsonObject());
      }
      return out;
    }

    /** A contribution's {@code id}: a non-blank string, unique within its kind (§3). */
    private String entryId(JsonObject o, String loc, String singular, Set<String> seen)
        throws ManifestException {
      String id = reqString(o, "id", loc);
      if (id.isBlank()) throw err(loc, "id must be non-empty");
      if (!seen.add(id)) throw err(loc, "duplicate " + singular + " id " + Wire.quote(id));
      return id;
    }

    private static String named(String loc, String id) {
      return loc + " " + Wire.quote(id);
    }

    // §3.1
    private ReaderDecl reader(JsonObject o, String loc0, Set<String> seen)
        throws ManifestException {
      String id = entryId(o, loc0, "reader", seen);
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      // extensions completely, then fallback_extensions (§3.1): a fault in the first is
      // reported even when the second is not a list
      List<String> primary = suffixes(suffixList(o, "extensions", loc), loc, "each extension must be a non-empty string");
      List<String> fallbacks = suffixes(suffixList(o, "fallback_extensions", loc), loc, "each fallback extension must be a non-empty string");
      ReaderDecl r = new ReaderDecl(id, name, primary, fallbacks);
      if (r.extensions().isEmpty() && r.fallbackExtensions().isEmpty()) throw err(loc, "extensions must be non-empty");
      var both = new java.util.TreeSet<>(r.extensions());
      both.retainAll(r.fallbackExtensions());
      if (!both.isEmpty()) {
        throw err(loc, "a suffix is in both extensions and fallback_extensions: "
            + both.stream().map(Wire::quote).collect(java.util.stream.Collectors.joining(", ", "[", "]")));
      }
      return r;
    }

    /** {@code extensions} or {@code fallback_extensions}: absent is empty, present must be a list (§3.1). */
    private JsonArray suffixList(JsonObject o, String field, String loc) throws ManifestException {
      if (!o.has(field)) return new JsonArray();
      JsonElement e = o.get(field);
      if (!e.isJsonArray()) throw err(loc, field + " must be a list");
      return e.getAsJsonArray();
    }

    /**
     * Each a string that leaves something other than ASCII whitespace after its leading dots, as an
     * exporter's {@code extension} must too ({@code
     * "."} and {@code ". \t"} do not; §3.1). Unicode whitespace is a suffix like any other.
     */
    private List<String> suffixes(JsonArray a, String loc, String refusal) throws ManifestException {
      List<String> out = new ArrayList<>();
      for (JsonElement x : a) {
        if (!isString(x) || asciiBlank(extensionCore(x.getAsString()))) throw err(loc, refusal);
        out.add(x.getAsString());
      }
      return out;
    }

    /** Empty, or only space, tab, CR, LF, VT and FF. */
    private static boolean asciiBlank(String s) {
      for (int i = 0; i < s.length(); i++) {
        char c = s.charAt(i);
        if (c != ' ' && c != '\t' && c != '\r' && c != '\n' && c != 0x0b && c != '\f') return false;
      }
      return true;
    }

    /** An extension without its leading dots, which must leave something (§3.1, §3.4). */
    private static String extensionCore(String ext) {
      return ReaderDecl.normaliseExtension(ext).substring(1);
    }

    // §3.2
    private CommandDecl command(JsonObject o, String loc0, Set<String> seen)
        throws ManifestException {
      String id = entryId(o, loc0, "command", seen);
      String loc = named(loc0, id);
      String title = reqString(o, "title", loc);
      String description = optString(o, "description", loc).orElse("");
      Optional<String> key = Optional.empty();
      if (o.has("default_keybinding") && !o.get("default_keybinding").isJsonNull()) {
        JsonElement k = o.get("default_keybinding");
        if (!isString(k) || k.getAsString().isBlank()) {
          throw err(loc, "default_keybinding must be a non-empty string");
        }
        key = Optional.of(k.getAsString());
      }
      Menu menu = menu(o, loc);
      Optional<PaneButton> pane = Optional.empty();
      if (o.has("pane")) pane = Optional.of(paneButton(o.get("pane"), loc));
      return new CommandDecl(id, title, description, key, menu, pane);
    }

    /** {@code menu}: absent is {@code "tools"}, {@code null} is unlisted (§3.2, §7). */
    private Menu menu(JsonObject o, String loc) throws ManifestException {
      if (!o.has("menu")) return Menu.TOOLS;
      JsonElement m = o.get("menu");
      if (m.isJsonNull()) return Menu.NONE;
      if (isString(m) && m.getAsString().equals("tools")) return Menu.TOOLS;
      // typed before the set-membership test, with the reference's wording
      throw err(loc, "menu " + repr(m) + " not allowed (expected one of ['tools'])");
    }

    // §3.3
    private PanelDecl panel(JsonObject o, String loc0, Set<String> seen, Set<String> commandIds)
        throws ManifestException {
      String id = entryId(o, loc0, "panel", seen);
      String loc = named(loc0, id);
      String title = reqString(o, "title", loc);
      Optional<String> menuLabel = Optional.empty();
      if (o.has("menu_label")) {
        JsonElement l = o.get("menu_label");
        if (!isString(l) || l.getAsString().isBlank()) {
          throw err(loc, "menu_label must be a non-empty string");
        }
        if (Wire.length(l.getAsString()) > MAX_CAPTION_CHARS) {
          throw err(loc, "menu_label must be at most " + MAX_CAPTION_CHARS + " characters");
        }
        menuLabel = Optional.of(l.getAsString());
      }
      if (!o.has("root")) throw err(loc, "missing required field 'root'");
      List<ViewTarget> viewTargets = viewTargets(o, loc, "view_targets");
      List<ViewTarget> followTargets = viewTargets(o, loc, "follow_targets");
      WidgetDecl root = widget(o.get("root"), 1, new Tree(loc, commandIds));
      if (viewTargets.isEmpty() && root.flatten().stream().anyMatch(WidgetDecl::movesView)) {
        throw err(loc, "panel " + Wire.quote(id) + " has moves_view widgets and declares no view_targets");
      }
      String cells = PanelDecl.listCellsProblem(id, root);
      if (cells != null) throw err(loc, cells);
      return new PanelDecl(id, title, menuLabel, root, viewTargets, followTargets);
    }

    /**
     * {@code view_targets} or {@code follow_targets} (set-view, §3.3, §3.3.1, §3.6): distinct names
     * from {@link ViewTarget}; absent is none.
     */
    private List<ViewTarget> viewTargets(JsonObject o, String loc, String field) throws ManifestException {
      if (!o.has(field)) return List.of();
      List<String> raw = stringList(o.get(field));
      List<ViewTarget> out = new ArrayList<>();
      if (raw != null) for (String s : raw) Wire.lookup(ViewTarget.class, s).ifPresent(out::add);
      if (raw == null || out.size() != raw.size() || out.stream().distinct().count() != out.size()) {
        throw err(loc, field + " must be distinct names from " + Wire.choices(ViewTarget.class));
      }
      return out;
    }

    /** The per-panel state of a widget-tree walk. */
    private static final class Tree {
      final String loc;
      final Set<String> commandIds;
      final Set<String> ids = new HashSet<>();
      int count;

      Tree(String loc, Set<String> commandIds) {
        this.loc = loc;
        this.commandIds = commandIds;
      }
    }

    // §3.3.1
    private WidgetDecl widget(JsonElement e, int depth, Tree t) throws ManifestException {
      String loc = t.loc;
      if (depth > WidgetDecl.MAX_DEPTH) {
        throw err(loc, "panel tree deeper than " + WidgetDecl.MAX_DEPTH);
      }
      if (++t.count > WidgetDecl.MAX_WIDGETS) {
        throw err(loc, "panel has more than " + WidgetDecl.MAX_WIDGETS + " widgets");
      }
      if (!e.isJsonObject()) throw err(loc, "each widget must be an object");
      JsonObject w = e.getAsJsonObject();

      if (!w.has("kind")) throw err(loc, "a widget is missing required field 'kind'");
      JsonElement k = w.get("kind");
      Optional<WidgetDecl.Kind> kindOpt =
          isString(k) ? Wire.lookup(WidgetDecl.Kind.class, k.getAsString()) : Optional.empty();
      if (kindOpt.isEmpty()) throw err(loc, "unknown widget kind " + repr(k));
      WidgetDecl.Kind kind = kindOpt.get();

      if (!w.has("id")) throw err(loc, "a " + kind.wire() + " widget is missing required field 'id'");
      if (!isString(w.get("id"))) throw err(loc, "widget id must be a string");
      String id = w.get("id").getAsString();
      if (id.isBlank()) throw err(loc, "widget id must be non-empty");
      if (!t.ids.add(id)) throw err(loc, "duplicate widget id " + Wire.quote(id));
      String who = kind.wire() + " " + Wire.quote(id);

      // text_format (§3.3.1): its value first, whatever the kind, then whether the kind takes it
      WidgetDecl.TextFormat textFormat = WidgetDecl.TextFormat.PLAIN;
      if (w.has("text_format")) {
        JsonElement f = w.get("text_format");
        Optional<WidgetDecl.TextFormat> tf = isString(f) ? Wire.lookup(WidgetDecl.TextFormat.class, f.getAsString()) : Optional.empty();
        if (tf.isEmpty()) throw err(loc, who + " text_format must be one of " + Wire.choices(WidgetDecl.TextFormat.class));
        if (kind != WidgetDecl.Kind.LABEL) throw err(loc, who + " does not take text_format; only a label does");
        textFormat = tf.get();
      }

      // Fields with one type whatever the kind are checked on every kind that carries them: a
      // value that would have crashed the reference parser is not an unknown to pass over.
      for (String f : List.of("text", "placeholder", "command")) {
        if (w.has(f) && !isString(w.get(f))) throw err(loc, who + " " + f + " must be a string");
      }
      for (String f : List.of("float", "indeterminate", "multiple", "selectable", "moves_view", "follows_selection", "sortable")) {
        if (w.has(f) && !isBool(w.get(f))) throw err(loc, who + " " + f + " must be a boolean");
      }
      boolean rowKind = kind == WidgetDecl.Kind.LIST || kind == WidgetDecl.Kind.TABLE;
      for (String f : List.of("max_rows", "row")) {
        if (w.has(f) && kind != WidgetDecl.Kind.LIST) throw err(loc, who + " does not take " + f);
      }
      if (w.has("visible_rows") && !rowKind) throw err(loc, who + " does not take visible_rows");
      // the reference refuses these two by presence on any kind but a table
      if ((w.has("columns") || w.has("sortable")) && kind != WidgetDecl.Kind.TABLE) {
        throw err(loc, who + " does not take columns or sortable");
      }
      // the boolean keys are refused on the wrong kind only when TRUE, as the host and the
      // reference refuse them; filter, like text_format, is refused by presence
      if (isTrue(w, "selectable") && !rowKind) throw err(loc, who + " does not take selectable");
      if (isTrue(w, "multiple") && kind != WidgetDecl.Kind.CHANNEL_PICKER && kind != WidgetDecl.Kind.TABLE) {
        throw err(loc, who + " does not take multiple");
      }
      if (isTrue(w, "multiple") && kind == WidgetDecl.Kind.TABLE && !isTrue(w, "selectable")) {
        throw err(loc, who + " takes multiple only when selectable");
      }
      if ((isTrue(w, "follows_selection") || w.has("filter")) && kind != WidgetDecl.Kind.ROI_PICKER) {
        throw err(loc, who + " does not take follows_selection or filter");
      }
      for (String f : List.of("min", "max", "step")) {
        if (w.has(f)) widgetNumber(w, f, who, loc);
      }
      if (w.has("options") && stringList(w.get("options")) == null) {
        throw err(loc, who + " options must be a list of strings");
      }
      if (w.has("children") && !w.get("children").isJsonArray()) {
        throw err(loc, who + " children must be a list");
      }

      String command = "";
      if (rowKind) {
        boolean selectable = w.has("selectable") && w.get("selectable").getAsBoolean();
        if (!selectable && w.has("command")) throw err(loc, who + " binds a command only when selectable");
      }
      boolean bindsCommand = switch (kind) {
        case LIST, TABLE -> w.has("selectable") && w.get("selectable").getAsBoolean();
        case IMAGE -> w.has("command"); // a clickable image
        default -> kind.interactive();
      };
      boolean movesView = w.has("moves_view") && w.get("moves_view").getAsBoolean();
      if (movesView && !bindsCommand) {
        throw err(loc, who + " cannot be moves_view: only an interactive widget, a selectable list or table, or an image with a command can");
      }
      if (bindsCommand) {
        Optional<String> c = optString(w, "command", loc);
        if (c.isEmpty() || !t.commandIds.contains(c.get())) {
          throw err(
              loc,
              kind.wire()
                  + " widget "
                  + Wire.quote(id)
                  + " must bind a declared command"
                  + c.map(s -> " (" + Wire.quote(s) + " is not one)").orElse(" (it names none)"));
        }
        command = c.get();
      }

      return switch (kind) {
        case LABEL -> new WidgetDecl.Label(id, optString(w, "text", loc), textFormat);
        case BUTTON -> new WidgetDecl.Button(id, command, optString(w, "text", loc), movesView);
        case SLIDER -> {
          double min = w.has("min") ? widgetNumber(w, "min", who, loc) : WidgetDecl.Slider.DEFAULT_MIN;
          double max = w.has("max") ? widgetNumber(w, "max", who, loc) : WidgetDecl.Slider.DEFAULT_MAX;
          double step =
              w.has("step") ? widgetNumber(w, "step", who, loc) : WidgetDecl.Slider.DEFAULT_STEP;
          if (!(min < max)) throw err(loc, who + " needs min < max");
          if (!(step > 0)) throw err(loc, who + " needs step > 0");
          boolean isFloat = w.has("float") && w.get("float").getAsBoolean();
          if (!isFloat && step != Math.rint(step)) {
            throw err(loc, who + " step " + Wire.g(step) + " is not a whole number; "
                + "an integer slider moves by whole steps (or declare it float)");
          }
          OptionalDouble value =
              w.has("value")
                  ? OptionalDouble.of(widgetNumber(w, "value", who, loc))
                  : OptionalDouble.empty();
          yield new WidgetDecl.Slider(id, command, min, max, step, value, isFloat, movesView, w.has("step"));
        }
        case DROPDOWN -> {
          List<String> options = w.has("options") ? stringList(w.get("options")) : null;
          if (options == null || options.isEmpty()) {
            throw err(loc, who + " needs non-empty options");
          }
          Optional<String> value = Optional.empty();
          if (w.has("value")) {
            JsonElement v = w.get("value");
            if (!isString(v) || !options.contains(v.getAsString())) {
              throw err(loc, who + " default " + repr(v) + " not in options");
            }
            value = Optional.of(v.getAsString());
          }
          yield new WidgetDecl.Dropdown(id, command, options, value, movesView);
        }
        case CHECKBOX -> {
          Optional<Boolean> value = Optional.empty();
          if (w.has("value")) {
            if (!isBool(w.get("value"))) throw err(loc, who + " value must be a boolean");
            value = Optional.of(w.get("value").getAsBoolean());
          }
          yield new WidgetDecl.Checkbox(id, command, optString(w, "text", loc), value, movesView);
        }
        case LINE_EDIT -> {
          Optional<String> value = Optional.empty();
          if (w.has("value")) {
            if (!isString(w.get("value"))) throw err(loc, who + " value must be a string");
            value = Optional.of(w.get("value").getAsString());
          }
          yield new WidgetDecl.LineEdit(id, command, optString(w, "placeholder", loc), value, movesView);
        }
        case CHANNEL_PICKER -> {
          boolean multiple = w.has("multiple") && w.get("multiple").getAsBoolean();
          Optional<List<Integer>> value = Optional.empty();
          if (w.has("value")) {
            JsonElement v = w.get("value");
            List<Integer> idx = new ArrayList<>();
            boolean ok;
            if (multiple) {
              ok = v.isJsonArray();
              if (ok) for (JsonElement x : v.getAsJsonArray()) ok &= channelIndex(x, idx);
            } else {
              ok = channelIndex(v, idx);
            }
            if (!ok) throw err(loc, who + " value must be " + (multiple ? "a list of channel indices" : "a channel index"));
            value = Optional.of(idx);
          }
          yield new WidgetDecl.ChannelPicker(id, command, multiple, value, movesView);
        }
        case COLOR_PICKER -> {
          Optional<String> value = Optional.empty();
          if (w.has("value")) {
            JsonElement v = w.get("value");
            if (!isString(v) || !WidgetDecl.ColorPicker.isColor(v.getAsString())) {
              throw err(loc, who + " value must be a #RRGGBB color");
            }
            value = Optional.of(v.getAsString());
          }
          yield new WidgetDecl.ColorPicker(id, command, value, movesView);
        }
        case COLORMAP_PICKER, ROI_PICKER -> {
          Optional<String> value = Optional.empty();
          if (w.has("value")) {
            if (!isString(w.get("value"))) throw err(loc, who + " value must be a string");
            value = Optional.of(w.get("value").getAsString());
          }
          if (kind == WidgetDecl.Kind.COLORMAP_PICKER) yield new WidgetDecl.ColormapPicker(id, command, value, movesView);
          boolean follows = w.has("follows_selection") && w.get("follows_selection").getAsBoolean();
          WidgetDecl.RoiFilter filter = WidgetDecl.RoiFilter.ALL;
          if (w.has("filter")) {
            JsonElement f = w.get("filter");
            Optional<WidgetDecl.RoiFilter> rf = isString(f) ? Wire.lookup(WidgetDecl.RoiFilter.class, f.getAsString()) : Optional.empty();
            if (rf.isEmpty()) throw err(loc, who + " filter must be one of " + Wire.choices(WidgetDecl.RoiFilter.class));
            filter = rf.get();
          }
          yield new WidgetDecl.RoiPicker(id, command, value, movesView, follows, filter);
        }
        case LIST -> {
          if (!w.has("row") || !w.get("row").isJsonArray()) throw err(loc, who + " needs a row: a list of leaf widgets");
          var row = w.get("row").getAsJsonArray();
          if (row.isEmpty()) throw err(loc, who + " has an empty row");
          int maxRows = listBound(w, "max_rows", WidgetDecl.RowList.DEFAULT_MAX_ROWS, who, loc);
          int visibleRows = listBound(w, "visible_rows", WidgetDecl.RowList.DEFAULT_VISIBLE_ROWS, who, loc);
          List<WidgetDecl> cells = new ArrayList<>();
          for (int i = 0; i < row.size(); i++) {
            JsonElement cell = row.get(i);
            if (cell.isJsonObject() && cell.getAsJsonObject().has("kind") && isString(cell.getAsJsonObject().get("kind"))) {
              String ck = cell.getAsJsonObject().get("kind").getAsString();
              if (ck.equals("row") || ck.equals("column") || ck.equals("list") || ck.equals("table")) {
                throw err(loc, who + " row[" + i + "] is a " + ck + "; a row holds leaf widgets only");
              }
            }
            cells.add(widget(cell, depth + 1, t));
          }
          if ((long) maxRows * cells.size() > WidgetDecl.RowList.MAX_CELLS) {
            throw err(loc, who + " may realize at most " + WidgetDecl.RowList.MAX_CELLS + " cells (max_rows × row cells), not "
                + maxRows + " × " + cells.size());
          }
          boolean selectable = w.has("selectable") && w.get("selectable").getAsBoolean();
          yield new WidgetDecl.RowList(id, cells, maxRows, visibleRows, selectable, selectable ? command : "", movesView);
        }
        case TABLE -> {
          List<WidgetDecl.TableColumn> columns = tableColumns(w, who, loc);
          int visibleRows = listBound(w, "visible_rows", WidgetDecl.Table.DEFAULT_VISIBLE_ROWS, who, loc);
          boolean selectable = isTrue(w, "selectable");
          yield new WidgetDecl.Table(id, columns, selectable, isTrue(w, "multiple"), selectable ? command : "", visibleRows,
              isTrue(w, "sortable"), movesView);
        }
        case IMAGE -> new WidgetDecl.Image(id, command, movesView);
        case PROGRESS -> {
          double value = 0;
          if (w.has("value")) {
            value = widgetNumber(w, "value", who, loc);
            if (value < 0 || value > 1) throw err(loc, who + " value must be within [0, 1]");
          }
          boolean indeterminate = w.has("indeterminate") && w.get("indeterminate").getAsBoolean();
          yield new WidgetDecl.Progress(id, value, indeterminate);
        }
        case ROW, COLUMN -> {
          if (!w.has("children") || w.get("children").getAsJsonArray().isEmpty()) {
            throw err(loc, who + " has no children");
          }
          List<WidgetDecl> children = new ArrayList<>();
          for (JsonElement child : w.get("children").getAsJsonArray()) {
            children.add(widget(child, depth + 1, t));
          }
          yield kind == WidgetDecl.Kind.ROW
              ? new WidgetDecl.Row(id, children)
              : new WidgetDecl.Column(id, children);
        }
      };
    }

    /** A list's {@code max_rows} / {@code visible_rows}: an integer in [1, 1024], or the default. */
    private int listBound(JsonObject w, String field, int dflt, String who, String loc) throws ManifestException {
      if (!w.has(field)) return dflt;
      StrictJson.Num n = num(w.get(field));
      double d = n == null ? Double.NaN : n.doubleValue();
      if (n == null || d != Math.rint(d) || d < 1 || d > WidgetDecl.RowList.MAX) {
        throw err(loc, who + " " + field + " must be an integer in [1, " + WidgetDecl.RowList.MAX + "]");
      }
      return (int) d;
    }

    /**
     * A table's {@code columns} (§3.3.1): 1 to 32 objects of {@code {id, header, type, precision,
     * unit}}, ids distinct, precision and unit on number columns only. The reference's texts.
     */
    private List<WidgetDecl.TableColumn> tableColumns(JsonObject w, String who, String loc) throws ManifestException {
      if (!w.has("columns")) throw err(loc, who + " missing required field 'columns'");
      if (!w.get("columns").isJsonArray()) throw err(loc, who + " field 'columns' must be a list");
      var raw = w.get("columns").getAsJsonArray();
      if (raw.isEmpty() || raw.size() > WidgetDecl.Table.MAX_COLUMNS) {
        throw err(loc, who + " needs 1 to " + WidgetDecl.Table.MAX_COLUMNS + " columns, not " + raw.size());
      }
      Set<String> seen = new HashSet<>();
      List<WidgetDecl.TableColumn> out = new ArrayList<>();
      for (int j = 0; j < raw.size(); j++) {
        String at = who + " columns[" + j + "]";
        if (!raw.get(j).isJsonObject()) throw err(loc, at + " must be an object");
        JsonObject c = raw.get(j).getAsJsonObject();
        if (!c.has("id")) throw err(loc, at + ": missing required field 'id'");
        if (!isString(c.get("id"))) throw err(loc, at + ": field 'id' must be a string");
        String cid = c.get("id").getAsString();
        if (cid.isBlank() || !seen.add(cid)) throw err(loc, at + ": id " + Wire.quote(cid) + " must be non-empty and distinct");
        String header = cid;
        if (c.has("header")) {
          if (!isString(c.get("header"))) throw err(loc, at + ": header must be a string");
          header = c.get("header").getAsString();
        }
        WidgetDecl.ColumnType type = WidgetDecl.ColumnType.STRING;
        if (c.has("type")) {
          JsonElement t = c.get("type");
          Optional<WidgetDecl.ColumnType> ct = isString(t) ? Wire.lookup(WidgetDecl.ColumnType.class, t.getAsString()) : Optional.empty();
          if (ct.isEmpty()) throw err(loc, at + ": type must be one of " + Wire.choices(WidgetDecl.ColumnType.class));
          type = ct.get();
        }
        if (type != WidgetDecl.ColumnType.NUMBER && (c.has("precision") || c.has("unit"))) {
          throw err(loc, at + ": only a number column takes precision or unit");
        }
        int precision = WidgetDecl.TableColumn.DEFAULT_PRECISION;
        if (c.has("precision")) {
          BigInteger p = integer(c.get("precision"));
          if (p == null || p.signum() < 0 || p.compareTo(BigInteger.valueOf(WidgetDecl.TableColumn.MAX_PRECISION)) > 0) {
            throw err(loc, at + ": precision must be an integer in [0, " + WidgetDecl.TableColumn.MAX_PRECISION + "]");
          }
          precision = p.intValue();
        }
        String unit = "";
        if (c.has("unit")) {
          if (!isString(c.get("unit"))) throw err(loc, at + ": unit must be a string");
          unit = c.get("unit").getAsString();
        }
        out.add(new WidgetDecl.TableColumn(cid, header, type, precision, unit));
      }
      return out;
    }

    /** A channel index: a non-negative JSON integer (never a boolean); added to {@code out}. */
    private static boolean channelIndex(JsonElement e, List<Integer> out) {
      StrictJson.Num n = num(e);
      if (n == null) return false;
      double d = n.doubleValue();
      if (!Double.isFinite(d) || d < 0 || d != Math.rint(d) || d > Integer.MAX_VALUE) return false;
      out.add((int) d);
      return true;
    }

    /** A widget's numeric field: a finite JSON number, never a boolean (§3.3.1). */
    private double widgetNumber(JsonObject w, String field, String who, String loc)
        throws ManifestException {
      StrictJson.Num n = num(w.get(field));
      if (n == null) throw err(loc, who + " " + field + " must be numeric");
      double d = n.doubleValue();
      if (!Double.isFinite(d)) throw err(loc, who + " " + field + " must be a finite number");
      return d;
    }

    // §3.4
    private ExporterDecl exporter(JsonObject o, String loc0, Set<String> seen)
        throws ManifestException {
      String id = entryId(o, loc0, "exporter", seen);
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      String ext = reqString(o, "extension", loc);
      // the reader suffix rule (§3.4): only ASCII whitespace is blank
      if (asciiBlank(extensionCore(ext))) throw err(loc, "extension must be non-empty");
      return new ExporterDecl(id, name, ext);
    }

    // §3.5
    private OverlayDecl overlay(JsonObject o, String loc0, Set<String> seen)
        throws ManifestException {
      String id = entryId(o, loc0, "overlay", seen);
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      int mib = OverlayDecl.DEFAULT_SEGMENT_MIB;
      if (o.has("segment_mib")) {
        BigInteger m = integer(o.get("segment_mib"));
        if (m == null
            || m.compareTo(BigInteger.valueOf(OverlayDecl.MIN_SEGMENT_MIB)) < 0
            || m.compareTo(BigInteger.valueOf(OverlayDecl.MAX_SEGMENT_MIB)) > 0) {
          throw err(
              loc,
              "segment_mib must be an int in ["
                  + OverlayDecl.MIN_SEGMENT_MIB
                  + ", "
                  + OverlayDecl.MAX_SEGMENT_MIB
                  + "]");
        }
        mib = m.intValue();
      }
      boolean chunked = false;
      if (o.has("chunked")) {
        if (!isBool(o.get("chunked"))) throw err(loc, "chunked must be a bool");
        chunked = o.get("chunked").getAsBoolean();
      }
      OverlayDecl.TimeMode time = OverlayDecl.TimeMode.PER_T;
      if (o.has("time")) {
        JsonElement t = o.get("time");
        Optional<OverlayDecl.TimeMode> tm =
            isString(t) ? Wire.lookup(OverlayDecl.TimeMode.class, t.getAsString()) : Optional.empty();
        if (tm.isEmpty()) {
          throw err(loc, "time must be one of " + Wire.choices(OverlayDecl.TimeMode.class));
        }
        time = tm.get();
      }
      if (chunked && o.has("segment_mib")) {
        throw err(
            loc,
            "a chunked overlay's arena is sized by the host from the dataset; segment_mib does not"
                + " apply");
      }
      if (!chunked && o.has("time")) throw err(loc, "time applies only to a chunked overlay");
      OverlayDecl.Kind kind =
          chunked ? new OverlayDecl.Chunked(time) : new OverlayDecl.Drawn(mib);
      return new OverlayDecl(id, name, kind);
    }

    // §3.6
    private ToolDecl tool(JsonObject o, String loc0, Set<String> seen) throws ManifestException {
      String id = entryId(o, loc0, "tool", seen);
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      if (o.has("binding")) {
        throw err(
            loc,
            "binding is not a field (it was replaced by triggers, actions and pane before 1.0)");
      }

      List<ToolDecl.View> views = ToolDecl.DEFAULT_VIEWS;
      if (o.has("views")) {
        List<String> raw = stringList(o.get("views"));
        List<ToolDecl.View> vs = new ArrayList<>();
        if (raw != null) {
          for (String s : raw) Wire.lookup(ToolDecl.View.class, s).ifPresent(vs::add);
        }
        if (raw == null || raw.isEmpty() || vs.size() != raw.size()) {
          throw err(loc, "views must be a non-empty list from " + Wire.choices(ToolDecl.View.class));
        }
        views = vs;
      }
      Menu menu = menu(o, loc);

      ToolDecl.Mode mode = ToolDecl.Mode.GESTURE;
      if (o.has("mode")) mode = oneOf(o.get("mode"), "mode", ToolDecl.Mode.class, loc);

      boolean cancel = true;
      if (o.has("cancel_on_camera_move")) {
        if (!isBool(o.get("cancel_on_camera_move"))) {
          throw err(loc, "cancel_on_camera_move must be a boolean");
        }
        cancel = o.get("cancel_on_camera_move").getAsBoolean();
      }

      boolean wantsPick = true;
      if (o.has("wants_pick")) {
        if (!isBool(o.get("wants_pick"))) {
          throw err(loc, "wants_pick must be a boolean");
        }
        wantsPick = o.get("wants_pick").getAsBoolean();
      }

      List<TriggerDecl> triggers = new ArrayList<>();
      Set<String> triggerKeys = new HashSet<>();
      if (o.has("triggers")) {
        JsonElement te = o.get("triggers");
        if (!te.isJsonArray()) throw err(loc, "triggers must be a list");
        Set<Object> identities = new HashSet<>();
        JsonArray ta = te.getAsJsonArray();
        for (int i = 0; i < ta.size(); i++) {
          String tloc = loc + ".triggers[" + i + "]";
          TriggerDecl t = trigger(ta.get(i), tloc);
          Object identity =
              switch (t) {
                case TriggerDecl.Key k -> TriggerDecl.keyIdentity(k.key());
                case TriggerDecl.Mouse m -> m;
              };
          if (!identities.add(identity)) throw err(tloc, "duplicate trigger " + describe(t));
          if (t instanceof TriggerDecl.Key k) triggerKeys.add(TriggerDecl.keyIdentity(k.key()));
          triggers.add(t);
        }
      }

      List<ActionDecl> actions = new ArrayList<>();
      if (o.has("actions")) {
        JsonElement ae = o.get("actions");
        if (!ae.isJsonArray()) throw err(loc, "actions must be a list");
        JsonArray aa = ae.getAsJsonArray();
        if (aa.size() > ToolDecl.MAX_ACTIONS) {
          throw err(loc, "more than " + ToolDecl.MAX_ACTIONS + " actions (" + aa.size() + ")");
        }
        Set<String> actionIds = new HashSet<>();
        Set<String> actionKeys = new HashSet<>();
        for (int i = 0; i < aa.size(); i++) {
          actions.add(action(aa.get(i), loc + ".actions[" + i + "]", actionIds, triggerKeys, actionKeys));
        }
      }

      Optional<ToolPane> pane = Optional.empty();
      if (o.has("pane")) pane = Optional.of(toolPane(o.get("pane"), loc));

      return new ToolDecl(
          id, name, views, menu, mode, cancel, wantsPick, triggers, actions, pane, viewTargets(o, loc, "view_targets"));
    }

    // §3.6.1
    private TriggerDecl trigger(JsonElement e, String loc) throws ManifestException {
      if (!e.isJsonObject()) throw err(loc, "each trigger must be an object");
      JsonObject t = e.getAsJsonObject();
      if (t.has("key") == t.has("button")) {
        throw err(loc, "a trigger names a key or a button, not both or neither");
      }
      if (t.has("key")) {
        JsonElement k = t.get("key");
        if (!isString(k) || k.getAsString().isBlank()) {
          throw err(loc, "key must be a non-empty string");
        }
        if (t.has("modifiers")) throw err(loc, "a key trigger carries its modifiers in the key name");
        Hold hold = t.has("hold") ? oneOf(t.get("hold"), "hold", Hold.class, loc) : Hold.TOGGLE;
        return new TriggerDecl.Key(k.getAsString(), hold);
      }
      JsonElement b = t.get("button");
      if (!isString(b)) throw err(loc, "button must be a string (a name, never a platform code)");
      TriggerDecl.MouseButton button = oneOf(b, "button", TriggerDecl.MouseButton.class, loc);
      List<TriggerDecl.Modifier> modifiers = new ArrayList<>();
      if (t.has("modifiers")) {
        JsonElement m = t.get("modifiers");
        List<String> raw = isString(m) ? List.of(m.getAsString()) : stringList(m);
        if (raw == null) throw err(loc, "modifiers must be a string or a list of strings");
        for (String s : raw) {
          if (s.equals("alt")) {
            throw err(loc, "alt is not a mouse modifier (window managers take Alt+drag)");
          }
          modifiers.add(oneOf(new JsonPrimitive(s), "modifiers", TriggerDecl.Modifier.class, loc));
        }
      }
      if (button == TriggerDecl.MouseButton.LEFT && modifiers.isEmpty()) {
        throw err(loc, "the bare left button is never a trigger (it is the primary)");
      }
      if (t.has("hold")) {
        Hold hold = oneOf(t.get("hold"), "hold", Hold.class, loc);
        if (hold != Hold.MOMENTARY) {
          throw err(loc, "a mouse trigger is momentary; hold " + Wire.quote(hold.wire()) + " does not apply");
        }
      }
      return new TriggerDecl.Mouse(button, modifiers);
    }

    private static String describe(TriggerDecl t) {
      return switch (t) {
        case TriggerDecl.Key k -> "key " + Wire.quote(k.key());
        case TriggerDecl.Mouse m ->
            "button "
                + Stream.concat(
                        m.modifiers().stream().map(TriggerDecl.Modifier::wire),
                        Stream.of(m.button().wire()))
                    .collect(Collectors.joining("+"));
      };
    }

    // §3.6.2
    private ActionDecl action(
        JsonElement e,
        String loc0,
        Set<String> actionIds,
        Set<String> triggerKeys,
        Set<String> actionKeys)
        throws ManifestException {
      if (!e.isJsonObject()) throw err(loc0, "each action must be an object");
      JsonObject a = e.getAsJsonObject();
      String id = reqString(a, "id", loc0);
      if (id.isBlank()) throw err(loc0, "id must be non-empty");
      if (!actionIds.add(id)) throw err(loc0, "duplicate action id " + Wire.quote(id));
      String loc = named(loc0, id);
      String name = optString(a, "name", loc).orElse(id);
      Optional<String> key = Optional.empty();
      if (a.has("key")) {
        JsonElement k = a.get("key");
        if (!isString(k) || k.getAsString().isBlank()) {
          throw err(loc, "key must be a non-empty string");
        }
        key = Optional.of(k.getAsString());
      }
      Optional<PaneButton> pane = Optional.empty();
      if (a.has("pane")) pane = Optional.of(paneButton(a.get("pane"), loc));
      if (key.isEmpty() && pane.isEmpty()) {
        throw err(loc, "an action needs a key or a pane button to be reachable");
      }
      if (key.isPresent()) {
        String k = TriggerDecl.keyIdentity(key.get());
        String q = Wire.quote(key.get());
        if (triggerKeys.contains(k)) throw err(loc, "key " + q + " is also one of this tool's triggers");
        if (!actionKeys.add(k)) throw err(loc, "key " + q + " is also another action's of this tool");
      }
      return new ActionDecl(id, name, key, pane);
    }

    // §3.6.3
    private ToolPane toolPane(JsonElement e, String loc) throws ManifestException {
      JsonObject p = paneObject(e, loc);
      String icon = icon(p, loc);
      Optional<String> tooltip = tooltip(p, loc);
      Hold hold = p.has("hold") ? oneOf(p.get("hold"), "pane.hold", Hold.class, loc) : Hold.TOGGLE;
      return new ToolPane(icon, tooltip, hold);
    }

    /** An action's or a command's pane button: it fires once, so a hold may only say so. */
    private PaneButton paneButton(JsonElement e, String loc) throws ManifestException {
      JsonObject p = paneObject(e, loc);
      String icon = icon(p, loc);
      Optional<String> tooltip = tooltip(p, loc);
      if (p.has("hold")) {
        Hold hold = oneOf(p.get("hold"), "pane.hold", Hold.class, loc);
        if (hold != Hold.MOMENTARY) {
          throw err(
              loc,
              "pane.hold "
                  + Wire.quote(hold.wire())
                  + ": an action or command button fires once on click");
        }
      }
      return new PaneButton(icon, tooltip);
    }

    private JsonObject paneObject(JsonElement e, String loc) throws ManifestException {
      if (!e.isJsonObject()) throw err(loc, "pane must be an object");
      return e.getAsJsonObject();
    }

    private Optional<String> tooltip(JsonObject p, String loc) throws ManifestException {
      if (!p.has("tooltip")) return Optional.empty();
      JsonElement t = p.get("tooltip");
      if (!isString(t)) throw err(loc, "pane.tooltip must be a string");
      if (Wire.length(t.getAsString()) > MAX_CAPTION_CHARS) {
        throw err(loc, "pane.tooltip longer than " + MAX_CAPTION_CHARS + " characters");
      }
      return Optional.of(t.getAsString());
    }

    /**
     * {@code pane.icon}: relative, inside the plugin directory, {@code .svg} or {@code .png},
     * present, at most 64 KiB, and its first bytes what its suffix claims (§3.6.3).
     */
    private String icon(JsonObject p, String loc) throws ManifestException {
      if (!p.has("icon")) throw err(loc, "missing required field 'pane.icon'");
      JsonElement ie = p.get("icon");
      if (!isString(ie)) throw err(loc, "pane.icon must be a string");
      String icon = ie.getAsString();
      if (icon.isBlank()) throw err(loc, "pane.icon must be non-empty");
      String q = Wire.quote(icon);
      boolean absolute =
          icon.startsWith("/") || icon.startsWith("\\") || icon.matches("[A-Za-z]:.*");
      boolean climbs = Arrays.asList(icon.split("[/\\\\]")).contains("..");
      if (absolute || climbs) {
        throw err(loc, "pane.icon " + q + " must be a path inside the plugin directory");
      }
      String lower = icon.toLowerCase(Locale.ROOT);
      boolean png = lower.endsWith(".png");
      boolean svg = lower.endsWith(".svg");
      if (!png && !svg) throw err(loc, "pane.icon " + q + " is not one of ['.png', '.svg']");
      if (dir == null) return icon;

      Path file;
      try {
        file = dir.resolve(icon);
      } catch (InvalidPathException ex) {
        throw err(loc, "pane.icon " + q + " not found in the plugin directory");
      }
      if (!Files.isRegularFile(file)) {
        throw err(loc, "pane.icon " + q + " not found in the plugin directory");
      }
      byte[] bytes;
      try {
        long size = Files.size(file);
        if (size > MAX_ICON_BYTES) {
          throw err(
              loc, "pane.icon " + q + " is " + size + " bytes; the limit is " + MAX_ICON_BYTES);
        }
        bytes = Files.readAllBytes(file);
      } catch (IOException ex) {
        throw err(loc, "pane.icon " + q + " cannot be read: " + ex);
      }
      if (png && !startsWith(bytes, PNG_SIGNATURE)) {
        throw err(loc, "pane.icon " + q + " is not a PNG file");
      }
      if (svg && !looksLikeSvg(bytes)) throw err(loc, "pane.icon " + q + " is not an SVG file");
      return icon;
    }

    // §3.7
    // §3.8
    private RegionTypeDecl regionType(JsonObject o, String loc0, Set<String> seen) throws ManifestException {
      String id = reqString(o, "id", loc0);
      String problem = RegionTypeDecl.idProblem(id);
      if (problem != null) throw err(loc0, problem);
      if (!seen.add(id)) throw err(loc0, "duplicate region type id " + Wire.quote(id));
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      return new RegionTypeDecl(id, name);
    }

    private StageDecl stage(JsonObject o, String loc0, Set<String> seen) throws ManifestException {
      String id = entryId(o, loc0, "stage", seen);
      String loc = named(loc0, id);
      String name = optString(o, "name", loc).orElse(id);
      List<StageParam> params = new ArrayList<>();
      if (o.has("params")) {
        JsonElement pe = o.get("params");
        if (!pe.isJsonArray()) throw err(loc, "params must be a list");
        JsonArray pa = pe.getAsJsonArray();
        Set<String> keys = new HashSet<>();
        for (int i = 0; i < pa.size(); i++) {
          String ploc = loc + ".params[" + i + "]";
          if (!pa.get(i).isJsonObject()) throw err(ploc, "each param must be an object");
          params.add(param(pa.get(i).getAsJsonObject(), ploc, keys));
        }
      }
      return new StageDecl(id, name, params);
    }

    private StageParam param(JsonObject o, String loc0, Set<String> keys)
        throws ManifestException {
      String key = reqString(o, "key", loc0);
      if (key.isBlank()) throw err(loc0, "key must be non-empty");
      if (!keys.add(key)) throw err(loc0, "duplicate param key " + Wire.quote(key));
      String loc = named(loc0, key);
      String name = optString(o, "name", loc).orElse(key);
      if (!o.has("type")) throw err(loc, "missing required field 'type'");
      JsonElement te = o.get("type");
      Optional<StageParam.Type> typeOpt =
          isString(te) ? Wire.lookup(StageParam.Type.class, te.getAsString()) : Optional.empty();
      if (typeOpt.isEmpty()) {
        throw err(loc, "type must be one of " + Wire.choices(StageParam.Type.class));
      }
      StageParam.Type type = typeOpt.get();
      List<String> takes =
          switch (type) {
            case FLOAT, INT -> List.of("min", "max");
            case BOOL -> List.of();
            case CHOICE -> List.of("options");
            case STRING -> List.of("max_length");
          };
      List<String> refused =
          Stream.of("min", "max", "options", "max_length").filter(o::has).filter(f -> !takes.contains(f)).toList();
      if (!refused.isEmpty()) {
        throw err(
            loc,
            "a "
                + type.wire()
                + " param does not take "
                + refused.stream().map(Wire::quote).collect(Collectors.joining(", ", "[", "]")));
      }
      if (!o.has("value")) {
        throw err(loc, "param " + Wire.quote(key) + " needs a value, the default");
      }
      JsonElement v = o.get("value");
      return switch (type) {
        case FLOAT -> {
          double value = finite(v, "value", loc);
          OptionalDouble min =
              o.has("min") ? OptionalDouble.of(finite(o.get("min"), "min", loc)) : OptionalDouble.empty();
          OptionalDouble max =
              o.has("max") ? OptionalDouble.of(finite(o.get("max"), "max", loc)) : OptionalDouble.empty();
          if (min.isPresent() && max.isPresent() && min.getAsDouble() > max.getAsDouble()) {
            throw err(loc, "needs min <= max");
          }
          if ((min.isPresent() && value < min.getAsDouble())
              || (max.isPresent() && value > max.getAsDouble())) {
            throw err(loc, "value is outside [min, max]");
          }
          yield new StageParam.FloatParam(key, name, value, min, max);
        }
        case INT -> {
          long value = int64(v, "value", loc);
          OptionalLong min =
              o.has("min") ? OptionalLong.of(int64(o.get("min"), "min", loc)) : OptionalLong.empty();
          OptionalLong max =
              o.has("max") ? OptionalLong.of(int64(o.get("max"), "max", loc)) : OptionalLong.empty();
          if (min.isPresent() && max.isPresent() && min.getAsLong() > max.getAsLong()) {
            throw err(loc, "needs min <= max");
          }
          if ((min.isPresent() && value < min.getAsLong())
              || (max.isPresent() && value > max.getAsLong())) {
            throw err(loc, "value is outside [min, max]");
          }
          yield new StageParam.IntParam(key, name, value, min, max);
        }
        case BOOL -> {
          if (!isBool(v)) throw err(loc, "value must be a boolean");
          yield new StageParam.BoolParam(key, name, v.getAsBoolean());
        }
        case CHOICE -> {
          List<String> options = o.has("options") ? stringList(o.get("options")) : null;
          if (options == null || options.isEmpty() || new HashSet<>(options).size() != options.size()) {
            throw err(loc, "options must be a non-empty list of distinct strings");
          }
          if (!isString(v) || !options.contains(v.getAsString())) {
            throw err(loc, "value must be one of the options");
          }
          yield new StageParam.ChoiceParam(key, name, options, v.getAsString());
        }
        case STRING -> {
          int maxLength = StageParam.StringParam.MAX_LENGTH;
          if (o.has("max_length")) {
            BigInteger m = integer(o.get("max_length"));
            if (m == null
                || m.signum() <= 0
                || m.compareTo(BigInteger.valueOf(StageParam.StringParam.MAX_LENGTH)) > 0) {
              throw err(
                  loc,
                  "max_length must be an integer in [1, " + StageParam.StringParam.MAX_LENGTH + "]");
            }
            maxLength = m.intValue();
          }
          if (!isString(v)) throw err(loc, "value must be a string");
          // The bound is in UTF-8 bytes, so every SDK counts it the same way (§3.7).
          if (StageParam.utf8Length(v.getAsString()) > maxLength) {
            throw err(loc, "value is longer than max_length (" + maxLength + " UTF-8 bytes)");
          }
          yield new StageParam.StringParam(key, name, v.getAsString(), maxLength);
        }
      };
    }

    /** A {@code float} param's number: any JSON number but a boolean, and finite. */
    private double finite(JsonElement e, String field, String loc) throws ManifestException {
      StrictJson.Num n = num(e);
      if (n == null) throw err(loc, "numeric field " + Wire.quote(field) + " must be a number");
      double d = n.doubleValue();
      if (!Double.isFinite(d)) {
        throw err(loc, "numeric field " + Wire.quote(field) + " must be a finite number");
      }
      return d;
    }

    /** An {@code int} param's number: an integer literal within {@code Value.i}'s sint64. */
    private long int64(JsonElement e, String field, String loc) throws ManifestException {
      BigInteger b = integer(e);
      if (b == null) throw err(loc, "numeric field " + Wire.quote(field) + " must be an integer");
      if (b.bitLength() > 63) {
        throw err(loc, "numeric field " + Wire.quote(field) + " must fit a signed 64-bit integer");
      }
      return b.longValue();
    }

    // §4
    private List<AssetDecl> assets(JsonArray a) throws ManifestException {
      List<AssetDecl> out = new ArrayList<>();
      Set<String> seen = new HashSet<>();
      for (int i = 0; i < a.size(); i++) {
        String loc0 = "assets[" + i + "]";
        if (!a.get(i).isJsonObject()) throw err(loc0, "each asset must be an object");
        JsonObject o = a.get(i).getAsJsonObject();
        String id = reqString(o, "id", loc0);
        if (!ASSET_ID.matcher(id).matches()) {
          throw err(
              loc0,
              "id must start with an alphanumeric and continue with letters, digits, '.', '_' or"
                  + " '-'");
        }
        if (!seen.add(id)) throw err(loc0, "duplicate asset id " + Wire.quote(id));
        String loc = named(loc0, id);
        String name = optString(o, "name", loc).orElse(id);
        String filename = reqString(o, "filename", loc);
        if (filename.isEmpty()
            || filename.startsWith(".")
            || filename.contains("/")
            || filename.contains("\\")
            || filename.indexOf('\0') >= 0) {
          throw err(loc, "filename must be one plain file name (no path separators, not dot-leading)");
        }
        if (!o.has("size")) throw err(loc, "missing required field 'size'");
        BigInteger size = integer(o.get("size"));
        if (size == null || size.signum() <= 0) throw err(loc, "size must be a positive integer");
        if (size.bitLength() > 63) {
          throw err(loc, "size must be a positive integer that fits in 64 bits");
        }
        String sha = reqString(o, "sha256", loc);
        if (!SHA256.matcher(sha).matches()) {
          throw err(loc, "sha256 must be exactly 64 hexadecimal characters");
        }
        JsonObject src = reqObject(o, "source", loc);
        JsonElement kind = src.has("kind") ? src.get("kind") : null;
        String k = kind != null && isString(kind) ? kind.getAsString() : "";
        AssetDecl.Source source;
        if (k.equals("https")) {
          if (!src.has("url") || !isString(src.get("url"))) {
            throw err(loc, "HTTPS source needs a string url");
          }
          String url = src.get("url").getAsString();
          if (!httpsUrl(url)) {
            throw err(loc, "source.url must be an HTTPS URL without credentials, with a host");
          }
          source = new AssetDecl.Https(url);
        } else if (k.equals("manual")) {
          if (src.has("url")) throw err(loc, "manual source must not declare a url");
          source = new AssetDecl.Manual();
        } else {
          throw err(loc, "source.kind must be 'manual' or 'https'");
        }
        out.add(new AssetDecl(id, name, filename, size.longValue(), sha, source));
      }
      return out;
    }

    private static boolean httpsUrl(String s) {
      URI u;
      try {
        u = new URI(s);
      } catch (URISyntaxException e) {
        return false;
      }
      String scheme = u.getScheme();
      if (scheme == null || !scheme.equalsIgnoreCase("https")) return false;
      String authority = u.getRawAuthority();
      if (authority == null || authority.contains("@")) return false;
      String host = u.getHost();
      if (host == null) host = authority.replaceFirst(":[0-9]*$", "");
      return !host.isEmpty();
    }

    // ---- field helpers ----

    private String reqString(JsonObject o, String key, String loc) throws ManifestException {
      if (!o.has(key)) throw err(loc, "missing required field " + Wire.quote(key));
      JsonElement e = o.get(key);
      if (!isString(e)) throw err(loc, "field " + Wire.quote(key) + " must be a string");
      return e.getAsString();
    }

    private Optional<String> optString(JsonObject o, String key, String loc)
        throws ManifestException {
      if (!o.has(key)) return Optional.empty();
      JsonElement e = o.get(key);
      if (!isString(e)) throw err(loc, "field " + Wire.quote(key) + " must be a string");
      return Optional.of(e.getAsString());
    }

    private JsonObject reqObject(JsonObject o, String key, String loc) throws ManifestException {
      if (!o.has(key)) throw err(loc, "missing required field " + Wire.quote(key));
      JsonElement e = o.get(key);
      if (!e.isJsonObject()) throw err(loc, "field " + Wire.quote(key) + " must be an object");
      return e.getAsJsonObject();
    }

    /** One of an enum's spellings, else {@code <field> '<value>' not in [...]}. */
    private <E extends Enum<E> & WireName> E oneOf(
        JsonElement e, String field, Class<E> type, String loc) throws ManifestException {
      Optional<E> v = isString(e) ? Wire.lookup(type, e.getAsString()) : Optional.empty();
      if (v.isEmpty()) throw err(loc, field + " " + repr(e) + " not in " + Wire.choices(type));
      return v.get();
    }
  }

  // ---- JSON predicates ----

  private static boolean isString(JsonElement e) {
    return e instanceof JsonPrimitive p && p.isString();
  }

  private static boolean isBool(JsonElement e) {
    return e instanceof JsonPrimitive p && p.isBoolean();
  }

  /** Whether {@code key} is present and true (its type is checked before). */
  private static boolean isTrue(JsonObject o, String key) {
    return o.has(key) && isBool(o.get(key)) && o.get(key).getAsBoolean();
  }

  /** A JSON number (never a boolean), with its lexical text. */
  private static StrictJson.@Nullable Num num(JsonElement e) {
    return e instanceof JsonPrimitive p && p.isNumber() && p.getAsNumber() instanceof StrictJson.Num n
        ? n
        : null;
  }

  /** A JSON number written as an integer ({@code 1}, not {@code 1.0} or {@code 1e0}). */
  private static @Nullable BigInteger integer(JsonElement e) {
    StrictJson.Num n = num(e);
    return n != null && n.isIntegerLiteral() ? n.bigInteger() : null;
  }

  /** A list whose every element is a string, else null. */
  private static @Nullable List<String> stringList(@Nullable JsonElement e) {
    if (e == null || !e.isJsonArray()) return null;
    List<String> out = new ArrayList<>();
    for (JsonElement x : e.getAsJsonArray()) {
      if (!isString(x)) return null;
      out.add(x.getAsString());
    }
    return out;
  }

  /** A value as the reference parser's messages print it: strings in single quotes. */
  private static String repr(JsonElement e) {
    return isString(e) ? Wire.quote(e.getAsString()) : e.toString();
  }

  private static boolean startsWith(byte[] bytes, byte[] prefix) {
    return bytes.length >= prefix.length
        && Arrays.equals(bytes, 0, prefix.length, prefix, 0, prefix.length);
  }

  /**
   * What "its first bytes are what its suffix claims" means for SVG (§3.6.3), which has no magic
   * number: after an optional byte-order mark and whitespace the text begins with {@code <} (an
   * XML declaration, a comment, a doctype or the element itself), and an {@code <svg} element
   * appears in it.
   */
  private static boolean looksLikeSvg(byte[] bytes) {
    String s = new String(bytes, StandardCharsets.UTF_8);
    if (s.startsWith("﻿")) s = s.substring(1);
    s = s.stripLeading();
    return s.startsWith("<") && s.contains("<svg");
  }
}
