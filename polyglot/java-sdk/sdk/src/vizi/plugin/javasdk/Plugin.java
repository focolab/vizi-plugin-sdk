package vizi.plugin.javasdk;

import java.lang.foreign.MemorySegment;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Application;
import vizi.plugin.javasdk.control.Arrangements;
import vizi.plugin.javasdk.control.HandlerFailure;
import vizi.plugin.javasdk.control.SeriesLists;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.javasdk.manifest.AssetDecl;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.ExporterDecl;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.ManifestException;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.manifest.PanelDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.manifest.Subscription;
import vizi.plugin.javasdk.manifest.ToolDecl;
import vizi.plugin.javasdk.manifest.WidgetDecl;
import vizi.plugin.v1.ViziPlugin.MetadataResult;
import vizi.plugin.v1.ViziPlugin.OpenExport;
import vizi.plugin.v1.ViziPlugin.ReaderOpened;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * A plugin: its declarations and the handlers behind them, registered together so the manifest
 * is assembled from code and cannot drift from it. Build one with {@link #builder}, then hand it
 * to {@link PluginMain#run}.
 *
 * <pre>{@code
 * var b = Plugin.builder("org.example.probe", "Probe", "0.1.0").entry("./probe");
 * Overlay marker = b.overlay(OverlayDecl.drawn("marker"));
 * b.command(CommandDecl.of("probe.clear", "Clear"), ctx -> { marker.clear(); return ""; });
 * Plugin p = b.build();
 * System.exit(PluginMain.run(p, args));
 * }</pre>
 */
public final class Plugin {

  private final Manifest manifest;
  private final Runtime runtime;

  private Plugin(Manifest manifest, Runtime runtime) {
    this.manifest = manifest;
    this.runtime = runtime;
  }

  public static Builder builder(String id, String name, String version) {
    return new Builder(id, name, version);
  }

  /** The assembled, validated manifest. */
  public Manifest manifest() {
    return manifest;
  }

  Runtime runtime() {
    return runtime;
  }

  /**
   * The plugin as the control layer sees it: what {@code testkit.TestHost.start} hosts, so a plugin
   * can drive its own tools and commands in a unit test.
   */
  public Application application() {
    return runtime;
  }

  // ---- author callbacks ----

  /** A command's handler: non-empty text is shown, {@code ""} is silent; a throw fails it. */
  @FunctionalInterface
  public interface Command {
    String run(CommandContext ctx) throws Exception;
  }

  /** Runs when a panel is opened, after the SDK has re-pushed the panel's mirror. */
  @FunctionalInterface
  public interface PanelHook {
    void opened(CommandContext ctx) throws Exception;
  }

  /**
   * Drives a Follow the person started (§8.15), on a thread of its own for as long as it lasts.
   * Returning or throwing ends the Follow (an {@code EndFollow}, unless the host ended it first).
   */
  @FunctionalInterface
  public interface FollowHandler {
    void drive(CommandContext ctx, Follow follow) throws Exception;
  }

  /** Runs inside the shutdown window, before running jobs are cancelled. */
  @FunctionalInterface
  public interface ShutdownHook {
    void run(ShutdownReason reason, String detail) throws Exception;
  }

  // ---- the builder ----

  /** Collects declarations with their handlers. {@link #build()} seals it. */
  public static final class Builder {
    private final Manifest.Builder mb;
    private final Runtime rt = new Runtime();
    private final Map<String, CommandDecl> commandDecls = new LinkedHashMap<>();
    private final java.util.Set<Subscription> subscriptions = new java.util.LinkedHashSet<>();
    private final java.util.Set<Permission> permissions = new java.util.LinkedHashSet<>();
    private final java.util.Set<String> regionTypeIds = new java.util.HashSet<>();
    private boolean entrySet;

    Builder(String id, String name, String version) {
      mb = Manifest.builder(id, name, version);
      rt.pluginId = id;
      rt.version = version;
    }

    /** {@code entry.command}: the launch line, relative to the plugin directory. */
    public Builder entry(String... command) {
      rt.checkOpen("setting the entry");
      mb.entryCommand(command);
      entrySet = true;
      return this;
    }

    /**
     * {@code entry.startup_s} (§2.3): how long the plugin expects to take from being started to
     * {@code Ready}, for a start a host's default may not cover (a cold JVM, a heavy first start).
     * Advisory: a host may honour, cap or ignore it (vizi holds it to 10–300 s and gives 60 s
     * without it), so nothing in the plugin may depend on it. It must be positive.
     */
    public Builder startup(Duration expected) {
      rt.checkOpen("setting the startup time");
      mb.startup(expected);
      return this;
    }

    /** {@code manifest_version}: 1 unless pinned (0 is a development artifact; a pinned 0 refuses v1 fields). */
    public Builder manifestVersion(int v) {
      rt.checkOpen("setting the manifest version");
      mb.manifestVersion(v);
      return this;
    }

    /** {@code engines.vizi}: the minimum protocol (default {@code "1.0"}). */
    public Builder engine(String vizi) {
      rt.checkOpen("setting the engine");
      mb.engine(vizi);
      return this;
    }

    public Builder permissions(Permission... ps) {
      rt.checkOpen("declaring permissions");
      permissions.addAll(List.of(ps));
      return this;
    }

    public Builder subscriptions(Subscription... ss) {
      rt.checkOpen("declaring subscriptions");
      subscriptions.addAll(List.of(ss));
      return this;
    }

    public Builder asset(AssetDecl a) {
      rt.checkOpen("declaring an asset");
      mb.asset(a);
      return this;
    }

    /** A command and its handler. */
    public Builder command(CommandDecl decl, Command handler) {
      rt.checkOpen("declaring a command");
      declareCommand(decl);
      rt.commands.put(decl.id(), Objects.requireNonNull(handler, "handler"));
      return this;
    }

    /**
     * A command with no handler of its own: one that a typed widget handle ({@link
     * Panel#number} and the rest) answers. {@link #build()} refuses it if no handle binds it.
     */
    public Builder command(CommandDecl decl) {
      rt.checkOpen("declaring a command");
      declareCommand(decl);
      return this;
    }

    private void declareCommand(CommandDecl decl) {
      if (commandDecls.putIfAbsent(decl.id(), decl) != null) throw new IllegalArgumentException("command " + decl.id() + " is declared twice");
      mb.command(decl);
    }

    /** A tool and its handlers. */
    public Builder tool(ToolDecl decl, Tool tool) {
      rt.checkOpen("declaring a tool");
      if (rt.tools.putIfAbsent(decl.id(), Objects.requireNonNull(tool, "tool")) != null) {
        throw new IllegalArgumentException("tool " + decl.id() + " is declared twice");
      }
      rt.toolDecls.put(decl.id(), decl);
      mb.tool(decl);
      return this;
    }

    /** An overlay; the handle is live once the plugin serves. */
    public Overlay overlay(OverlayDecl decl) {
      rt.checkOpen("declaring an overlay");
      if (decl.kind() instanceof OverlayDecl.Chunked) {
        throw new IllegalArgumentException("overlay " + decl.id() + " is chunked: declare it with chunkedOverlay(decl, source)");
      }
      if (rt.chunked.containsKey(decl.id())) throw new IllegalArgumentException("overlay " + decl.id() + " is declared twice");
      Overlay o = new Overlay(decl.id(), decl, rt);
      if (rt.overlays.putIfAbsent(decl.id(), o) != null) throw new IllegalArgumentException("overlay " + decl.id() + " is declared twice");
      mb.overlay(decl);
      return o;
    }

    /** A chunked overlay and the source that serves the volumes the host wants. */
    public ChunkedOverlay chunkedOverlay(OverlayDecl decl, ChunkedOverlay.Source source) {
      rt.checkOpen("declaring an overlay");
      ChunkedOverlay o = new ChunkedOverlay(decl, rt, Objects.requireNonNull(source, "source"));
      if (rt.overlays.containsKey(decl.id()) || rt.chunked.putIfAbsent(decl.id(), o) != null) {
        throw new IllegalArgumentException("overlay " + decl.id() + " is declared twice");
      }
      mb.overlay(decl);
      return o;
    }

    /** A panel; take widget handles from it before {@link #build()}. */
    public Panel panel(PanelDecl decl) {
      rt.checkOpen("declaring a panel");
      Panel p = new Panel(decl, rt);
      if (rt.panels.putIfAbsent(decl.id(), p) != null) throw new IllegalArgumentException("panel " + decl.id() + " is declared twice");
      mb.panel(decl);
      return p;
    }

    /** Runs when {@code panelId} opens, after the mirror is re-pushed. */
    /**
     * The driver of a Follow started from panel {@code panelId}, which must declare {@code
     * follow_targets}. Offer the Follow with {@link Panel#offerFollow}; a Follow started on a panel
     * with no driver is ended at once.
     */
    public Builder follow(String panelId, FollowHandler handler) {
      rt.checkOpen("registering a Follow");
      Panel p = rt.panels.get(panelId);
      if (p == null) throw new IllegalArgumentException("no panel " + panelId + " is declared (declare it before its Follow)");
      if (p.decl().followTargets().isEmpty()) {
        throw new IllegalArgumentException("panel " + panelId + " declares no follow_targets, so nothing may follow from it");
      }
      if (rt.followHandlers.putIfAbsent(panelId, Objects.requireNonNull(handler, "handler")) != null) {
        throw new IllegalArgumentException("panel " + panelId + " already has a Follow driver");
      }
      return this;
    }

    public Builder onPanelOpened(String panelId, PanelHook hook) {
      rt.checkOpen("adding a panel hook");
      rt.panelHooks.put(panelId, Objects.requireNonNull(hook, "hook"));
      return this;
    }

    public Builder reader(ReaderDecl decl, Reader reader) {
      Objects.requireNonNull(reader, "reader");
      return reader(decl, (uri, dir, series) -> reader.open(uri, dir));
    }

    /** A reader whose files may hold several images, and which lists them when asked. */
    public Builder reader(ReaderDecl decl, SeriesReader reader) {
      rt.checkOpen("declaring a reader");
      if (rt.readers.putIfAbsent(decl.id(), Objects.requireNonNull(reader, "reader")) != null) {
        throw new IllegalArgumentException("reader " + decl.id() + " is declared twice");
      }
      mb.reader(decl);
      return this;
    }

    public Builder exporter(ExporterDecl decl, Exporter exporter) {
      rt.checkOpen("declaring an exporter");
      if (rt.exporters.putIfAbsent(decl.id(), Objects.requireNonNull(exporter, "exporter")) != null) {
        throw new IllegalArgumentException("exporter " + decl.id() + " is declared twice");
      }
      mb.exporter(decl);
      return this;
    }

    /** A processing stage (processing_stages.md) and its implementation. */
    public Builder stage(vizi.plugin.javasdk.manifest.StageDecl decl, Stage stage) {
      rt.checkOpen("declaring a stage");
      if (rt.stages.putIfAbsent(decl.id(), Objects.requireNonNull(stage, "stage")) != null) {
        throw new IllegalArgumentException("stage " + decl.id() + " is declared twice");
      }
      mb.stage(decl);
      return this;
    }

    /**
     * A region type the plugin shapes (manifest §3.8, control_plane_protocol.md §8.8): every region
     * whose {@code type_name} is {@code decl.id()} is asked, and {@code shaper} says where its
     * vertices go when the person drags a handle. Needs {@link Permission#CONTRIBUTE_ROIS}.
     */
    public Builder regionType(vizi.plugin.javasdk.manifest.RegionTypeDecl decl, Shaper shaper) {
      rt.checkOpen("declaring a region type");
      Objects.requireNonNull(shaper, "shaper");
      declareRegionType(decl);
      rt.shapers.put(decl.id(), shaper);
      return this;
    }

    /**
     * A region type declared with no shaper: every commit of one is answered {@code REJECTED}, so its
     * regions keep their shape. For a plugin whose shaper is not ready yet; prefer the form above.
     */
    public Builder regionType(vizi.plugin.javasdk.manifest.RegionTypeDecl decl) {
      rt.checkOpen("declaring a region type");
      declareRegionType(decl);
      return this;
    }

    private void declareRegionType(vizi.plugin.javasdk.manifest.RegionTypeDecl decl) {
      if (!regionTypeIds.add(decl.id())) throw new IllegalArgumentException("region type " + decl.id() + " is declared twice");
      mb.regionType(decl);
    }

    /** Hears how each edit of an asked region ended (COMMITTED, REVERTED, CANCELLED), on the shape worker. */
    public Builder onShapeEnded(Consumer<ShapeEnded> hook) {
      rt.checkOpen("adding a shape-ended hook");
      if (rt.shapeEnded != null) throw new IllegalStateException("the plugin already has a shape-ended hook");
      rt.shapeEnded = Objects.requireNonNull(hook, "hook");
      return this;
    }

    /** The plugin's job runner; {@code onOutcome} hears how each job ended. */
    public Jobs jobs(Consumer<Jobs.Outcome> onOutcome) {
      rt.checkOpen("creating the job runner");
      if (rt.jobs != null) throw new IllegalStateException("the plugin already has a job runner");
      Jobs j = new Jobs(rt, Objects.requireNonNull(onOutcome, "onOutcome"));
      rt.jobs = j;
      return j;
    }

    /** Runs on the dispatch thread when a pane's arrangement is installed; must not block. */
    public Builder onEpoch(Consumer<PaneContext> hook) {
      rt.checkOpen("adding an epoch hook");
      rt.epochHook = Objects.requireNonNull(hook, "hook");
      return this;
    }

    /**
     * Follows the view: declares the {@code view} subscription, which needs {@link
     * Permission#DATA_ACCESS}. The handler runs on a worker of its own and sees the newest view.
     */
    public Builder onViewChanged(Subscriptions.ViewHandler handler) {
      rt.checkOpen("adding a view handler");
      if (rt.views != null) throw new IllegalStateException("the plugin already has a view handler");
      rt.views = new Subscriptions.Views(rt, Objects.requireNonNull(handler, "handler"));
      subscriptions.add(Subscription.VIEW);
      return this;
    }

    /**
     * Follows the regions: declares the {@code regions} subscription, which needs {@link
     * Permission#DATA_ACCESS}. The handler runs on a worker of its own; pushes that arrive while it
     * runs are merged by region, never dropped.
     */
    public Builder onRegionsChanged(Subscriptions.RegionsHandler handler) {
      rt.checkOpen("adding a regions handler");
      if (rt.regions != null) throw new IllegalStateException("the plugin already has a regions handler");
      rt.regions = new Subscriptions.Regions(rt, Objects.requireNonNull(handler, "handler"));
      subscriptions.add(Subscription.REGIONS);
      return this;
    }

    public Builder onShutdown(ShutdownHook hook) {
      rt.checkOpen("adding a shutdown hook");
      rt.shutdownHook = Objects.requireNonNull(hook, "hook");
      return this;
    }

    /**
     * Assembles the manifest, validates it with the same validator the manifest fixtures test,
     * and seals the plugin: nothing may be registered afterwards.
     */
    public Plugin build() {
      rt.checkOpen("building");
      if (!entrySet) mb.entryCommand("./run");
      for (Subscription sub : subscriptions) {
        if (sub.needsDataAccess() && !permissions.contains(Permission.DATA_ACCESS)) {
          throw new IllegalStateException("the '" + sub.wire() + "' subscription needs Permission.DATA_ACCESS (manifest_schema.md §5.1);"
              + " without it the host ignores it and the handler would never run");
        }
      }
      mb.permissions(permissions.toArray(new Permission[0]));
      mb.subscriptions(subscriptions.toArray(new Subscription[0]));
      for (String c : commandDecls.keySet()) {
        if (!rt.commands.containsKey(c) && !rt.widgetCommands.containsKey(c)) {
          throw new IllegalStateException("command " + c + " has no handler and no widget handle answers it");
        }
      }
      for (String c : rt.widgetCommands.keySet()) {
        if (rt.commands.containsKey(c)) {
          throw new IllegalStateException("command " + c + " is answered by a widget handle AND has a handler; give it one owner");
        }
        List<String> binders = new ArrayList<>();
        for (Panel p : rt.panels.values()) {
          for (WidgetDecl w : p.decl().root().flatten()) {
            if (w.boundCommand().filter(c::equals).isPresent()) binders.add(p.id() + "/" + w.id());
          }
        }
        if (binders.size() != 1) {
          throw new IllegalStateException("command " + c + " is bound by " + binders + "; a widget handle needs a command of its own,"
              + " since the widget's event does not say which widget fired it");
        }
      }
      for (String p : rt.panelHooks.keySet()) {
        if (!rt.panels.containsKey(p)) throw new IllegalStateException("a panel hook for undeclared panel " + p);
      }
      if (!regionTypeIds.isEmpty() && !permissions.contains(Permission.CONTRIBUTE_ROIS)) {
        throw new IllegalStateException("region types need Permission.CONTRIBUTE_ROIS (manifest_schema.md §3.8):"
            + " a type exists to be the type of regions the plugin contributes");
      }
      Manifest m = mb.build();
      try {
        m = ManifestParser.validate(m, null).manifest();
      } catch (ManifestException e) {
        throw new IllegalStateException("the assembled manifest is invalid: " + e.getMessage(), e);
      }
      rt.manifest = m;
      rt.sealed = true;
      return new Plugin(m, rt);
    }
  }

  // ---- the runtime ----

  /** The live half: the control layer's {@link Application}, and what every handle reaches. */
  static final class Runtime implements Application {
    String pluginId = "";
    String version = "";
    @Nullable Manifest manifest;
    volatile boolean sealed;
    private volatile @Nullable Session session;

    final Map<String, Command> commands = new HashMap<>();
    final Map<String, Tool> tools = new HashMap<>();
    final Map<String, ToolDecl> toolDecls = new HashMap<>();
    final Map<String, Overlay> overlays = new HashMap<>();
    final Map<String, ChunkedOverlay> chunked = new HashMap<>();
    final Map<String, Panel> panels = new LinkedHashMap<>();
    final Map<String, PanelHook> panelHooks = new HashMap<>();
    final Map<String, FollowHandler> followHandlers = new HashMap<>();
    private final Map<Long, Follow> follows = new ConcurrentHashMap<>();
    final Map<String, SeriesReader> readers = new HashMap<>();
    final Map<String, Exporter> exporters = new HashMap<>();
    final Map<String, WidgetBinding> widgetCommands = new HashMap<>();
    final Map<String, Stage> stages = new HashMap<>();
    final Map<String, Shaper> shapers = new HashMap<>();
    @Nullable Consumer<ShapeEnded> shapeEnded;
    private final StageRunner stageRunner = new StageRunner(this, stages);
    @Nullable Jobs jobs;
    Subscriptions.@Nullable Views views;
    Subscriptions.@Nullable Regions regions;
    @Nullable Consumer<PaneContext> epochHook;
    Plugin.@Nullable ShutdownHook shutdownHook;
    private final Host host = new Host(this);
    private final Map<Long, ToolContext> toolContexts = new ConcurrentHashMap<>();

    record WidgetBinding(Panel panel, Panel.Bound widget) {}

    void checkOpen(String what) {
      if (sealed) throw new IllegalStateException(what + " after build(): a plugin is sealed once built");
    }

    void bindWidgetCommand(String command, Panel panel, Panel.Bound widget) {
      checkOpen("binding a widget");
      if (widgetCommands.putIfAbsent(command, new WidgetBinding(panel, widget)) != null) {
        throw new IllegalStateException("command " + command + " is already answered by another widget handle");
      }
    }

    @Nullable Session sessionOrNull() {
      return session;
    }

    Session session() {
      Session s = session;
      if (s == null) throw new IllegalStateException("the plugin is not serving yet (no session)");
      return s;
    }

    Task startTask(String title, boolean cancellable, @Nullable Runnable onCancel) {
      return new Task(session().work().startTask(title, cancellable, onCancel));
    }

    /** A job's task, or null with no session or with 64 open (the job runs unshown, and says so). */
    @Nullable Task startTaskQuietly(String title, Runnable onCancel) {
      Session s = session;
      if (s == null) return null;
      try {
        return new Task(s.work().startTask(title, true, onCancel));
      } catch (IllegalStateException e) {
        log("job " + title + " runs without a task: " + e.getMessage());
        return null;
      }
    }

    Overlay overlay(String id) {
      Overlay o = overlays.get(Objects.requireNonNull(id, "id"));
      if (o == null && chunked.containsKey(id)) {
        throw new IllegalArgumentException("overlay " + id + " is chunked: reach it with chunked(\"" + id + "\")");
      }
      if (o == null) throw new IllegalArgumentException("no overlay " + id + " is declared");
      return o;
    }

    ChunkedOverlay chunked(String id) {
      ChunkedOverlay o = chunked.get(Objects.requireNonNull(id, "id"));
      if (o == null) throw new IllegalArgumentException("no chunked overlay " + id + " is declared");
      return o;
    }

    Panel panel(String id) {
      Panel p = panels.get(Objects.requireNonNull(id, "id"));
      if (p == null) throw new IllegalArgumentException("no panel " + id + " is declared");
      return p;
    }

    Jobs jobs() {
      Jobs j = jobs;
      if (j == null) throw new IllegalStateException("the plugin has no job runner: register one with Plugin.Builder.jobs(...)");
      return j;
    }

    Host host() {
      return host;
    }

    Optional<PaneContext> pane(int viewId) {
      Session s = session;
      if (s == null) return Optional.empty();
      return s.arrangements().pane(viewId).map(PaneContext::new);
    }

    List<PaneContext> panes() {
      Session s = session;
      if (s == null) return List.of();
      List<PaneContext> out = new ArrayList<>();
      for (Arrangements.PaneState p : s.arrangements().panes()) out.add(new PaneContext(p));
      return out;
    }

    /** A declared asset's provisioned file (VIZI_PLUGIN_ASSETS), if the host provided one. */
    Optional<Path> asset(String assetId) {
      Session s = session;
      if (s == null) return Optional.empty();
      return s.environment().asset(assetId);
    }

    /** The settings folder (VIZI_PLUGIN_SETTINGS_DIR), created on first use; empty if the host gave none. */
    Optional<Path> settingsDir() throws java.io.IOException {
      Session s = session;
      if (s == null) return Optional.empty();
      return s.environment().settingsDir();
    }

    void log(String text) {
      System.err.println("[" + pluginId + "] " + text);
    }

    // ---- Application ----

    @Override
    public Manifest manifest() {
      Manifest m = manifest;
      if (m == null) throw new IllegalStateException("not built");
      return m;
    }

    @Override
    public String versionString() {
      return pluginId + " " + version + " (vizi-sdk-java " + Sdk.VERSION + ")";
    }

    @Override
    public void started(Session s) {
      session = s;
      for (ChunkedOverlay o : chunked.values()) o.start();
    }

    @Override
    public void viewChanged(vizi.plugin.v1.ViziPlugin.ViewChanged changed) {
      Subscriptions.Views v = views;
      if (v == null) {
        log("a ViewChanged, and this plugin has no view handler");
        return;
      }
      v.offer(changed);
    }

    @Override
    public void regionsChanged(vizi.plugin.v1.ViziPlugin.RegionsChanged changed) {
      Subscriptions.Regions r = regions;
      if (r == null) {
        log("a RegionsChanged, and this plugin has no regions handler");
        return;
      }
      r.offer(changed);
    }

    @Override
    public void chunksWanted(vizi.plugin.v1.ViziPlugin.ChunksWanted wanted) {
      ChunkedOverlay o = chunked.get(wanted.getOverlayId());
      if (o == null) {
        log("ChunksWanted for " + wanted.getOverlayId() + ", which is not a chunked overlay this plugin declares");
        return;
      }
      o.onWanted(wanted);
    }

    @Override
    public vizi.plugin.v1.ViziPlugin.StagePlanned planStage(vizi.plugin.v1.ViziPlugin.PlanStage request) throws Exception {
      return stageRunner.plan(request);
    }

    @Override
    public void stageArmed(vizi.plugin.v1.ViziPlugin.StageArm arm) {
      stageRunner.armed(arm);
    }

    @Override
    public void shutdownBegan() {
      Subscriptions.Views v = views;
      if (v != null) v.stop();
      Subscriptions.Regions rg = regions;
      if (rg != null) rg.stop();
    }

    @Override
    public void ended(String reason) {
      stageRunner.stop();
      Subscriptions.Views v = views;
      if (v != null) v.stop();
      Subscriptions.Regions rg = regions;
      if (rg != null) rg.stop();
      for (ChunkedOverlay o : chunked.values()) o.stop();
      Jobs j = jobs;
      if (j != null) j.cancelAll();
      for (Follow f : follows.values()) f.ended(null, ""); // a session ending sends nothing
      follows.clear();
    }

    // ---- region types (§8.8) ----

    @Override
    public boolean shapes(String typeName) {
      return shapers.containsKey(typeName);
    }

    @Override
    public vizi.plugin.v1.ViziPlugin.ShapeResult shape(vizi.plugin.v1.ViziPlugin.ShapeEdit edit, boolean commit,
        java.util.function.BooleanSupplier stale) throws Exception {
      Shaper s = shapers.get(edit.getTypeName());
      if (s == null) throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.UNSUPPORTED, "no shaper for " + edit.getTypeName());
      Shape shape = refusals(() -> s.shape(new ShapeEdit(edit, commit, stale)));
      if (shape == null) throw new IllegalStateException("the shaper for " + edit.getTypeName() + " returned null; return Shape.rejected(why)");
      return shape.wire();
    }

    @Override
    public void shapeEditEnded(vizi.plugin.v1.ViziPlugin.ShapeEditEnded ended, vizi.plugin.v1.ViziPlugin.@Nullable ShapeEdit last) {
      Consumer<ShapeEnded> h = shapeEnded;
      if (h == null) return;
      Optional<ShapeEdit> seen = last == null ? Optional.empty() : Optional.of(new ShapeEdit(last, last.getSeq() == 0, () -> true));
      try {
        h.accept(new ShapeEnded(ended.getDragId(), ended.getOutcome(), ended.getDetail(), seen));
      } catch (RuntimeException e) {
        log("the shape-ended hook threw: " + e);
      }
    }

    // ---- Follow (§8.15) ----

    @Override
    public boolean followStarted(vizi.plugin.v1.ViziPlugin.FollowStarted m) {
      FollowHandler h = followHandlers.get(m.getPanelId());
      if (h == null) return false;
      java.util.Set<vizi.plugin.javasdk.manifest.ViewTarget> known = java.util.EnumSet.noneOf(vizi.plugin.javasdk.manifest.ViewTarget.class);
      for (String t : m.getTargetsList()) {
        for (var v : vizi.plugin.javasdk.manifest.ViewTarget.values()) if (v.wire().equals(t)) known.add(v);
      }
      Follow f = new Follow(this, m.getFollowId(), m.getPanelId(), m.getTargetsList(), known, m.getResumed());
      follows.put(f.id(), f);
      Thread.ofVirtual().name("vizi-follow-" + Long.toUnsignedString(f.id())).start(() -> {
        try {
          h.drive(new CommandContext(this, "", Map.of()), f);
        } catch (InterruptedException e) {
          Thread.currentThread().interrupt();
        } catch (vizi.plugin.javasdk.control.SessionEndedException e) {
          // the session is ending: nothing to report
        } catch (Exception | Error e) {
          log("the Follow driver for panel " + f.panelId() + " threw: " + e);
        } finally {
          if (f.hostEnded()) follows.remove(f.id());
          else f.end(); // kept until the host's PLUGIN_ENDED, so reason() is filled in
        }
      });
      return true;
    }

    @Override
    public void followEnded(vizi.plugin.v1.ViziPlugin.FollowEnded m) {
      Follow f = follows.remove(m.getFollowId());
      if (f != null) f.ended(m.getReason(), m.getDetail());
    }

    @Override
    public Optional<Application.ShutdownHook> shutdownHook() {
      Plugin.ShutdownHook h = shutdownHook;
      Jobs j = jobs;
      if (h == null && j == null) return Optional.empty();
      return Optional.of((reason, detail) -> {
        try {
          if (h != null) h.run(reason, detail);
        } finally {
          if (j != null) j.shutdown(Duration.ofMillis(1500));
        }
      });
    }

    // readers

    @Override
    public Application.Opening openReader(String readerId, String uri, String fileStateDir, Optional<String> series)
        throws Exception {
      SeriesReader r = readers.get(readerId);
      if (r == null) throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "this plugin declares no reader " + readerId);
      Path dir = Path.of(fileStateDir);
      return opening(refusals(() -> r.open(uri, dir, series)), r, uri, dir);
    }

    private static Application.Opening opening(vizi.plugin.javasdk.Opening o, SeriesReader r, String uri, Path dir) {
      return switch (o) {
        case vizi.plugin.javasdk.Dataset d -> new DatasetAdapter(d);
        case SeriesChoice c -> new ListingAdapter(c, r, uri, dir);
      };
    }

    /**
     * A {@link SeriesChoice} as control holds it. Its entries are checked here, where a negative
     * size is still negative; control refuses the choice and closes it.
     */
    private static final class ListingAdapter implements Application.Listing {
      private final SeriesChoice c;
      private final SeriesReader r;
      private final String uri;
      private final Path dir;
      private final List<vizi.plugin.v1.ViziPlugin.SeriesEntry> entries;
      private final @Nullable String problem;
      private final java.util.concurrent.atomic.AtomicBoolean closed = new java.util.concurrent.atomic.AtomicBoolean();

      ListingAdapter(SeriesChoice c, SeriesReader r, String uri, Path dir) {
        this.c = c;
        this.r = r;
        this.uri = uri;
        this.dir = dir;
        List<vizi.plugin.v1.ViziPlugin.SeriesEntry> out = new ArrayList<>();
        String first = null;
        java.util.Set<String> ids = new java.util.HashSet<>();
        for (SeriesEntry e : c.entries()) {
          String p = SeriesLists.entryProblem(e.id(), e.shapeTczyx().stream().map(Integer::longValue).toList(),
              e.dtype().map(DType::wire).orElse(vizi.plugin.v1.ViziPlugin.DType.DTYPE_UNSPECIFIED));
          if (p == null && !ids.add(e.id())) p = SeriesLists.listedTwice(e.id());
          if (p != null && first == null) first = p;
          var b = vizi.plugin.v1.ViziPlugin.SeriesEntry.newBuilder()
              .setId(e.id())
              .setLabel(e.label())
              .addAllShapeTczyx(e.shapeTczyx())
              .setDescription(e.description());
          e.dtype().ifPresent(d -> b.setDtype(d.wire()));
          out.add(b.build());
        }
        this.entries = List.copyOf(out);
        this.problem = first;
      }

      @Override
      public @Nullable String problem() {
        return problem;
      }

      @Override
      public List<vizi.plugin.v1.ViziPlugin.SeriesEntry> entries() {
        return entries;
      }

      @Override
      public Application.Opening open(String series) throws Exception {
        var opener = c.opener();
        if (opener.isPresent()) return new DatasetAdapter(refusals(() -> opener.get().open(series)));
        // nothing of the listing opens the choice: the reader opens it afresh, and the listing is done
        try {
          return opening(refusals(() -> r.open(uri, dir, Optional.of(series))), r, uri, dir);
        } finally {
          close();
        }
      }

      @Override
      public void close() {
        if (closed.compareAndSet(false, true)) c.close();
      }
    }

    private static final class DatasetAdapter implements Application.Dataset {
      private final vizi.plugin.javasdk.Dataset d;

      DatasetAdapter(vizi.plugin.javasdk.Dataset d) {
        this.d = d;
      }

      @Override
      public ReaderOpened describe() {
        vizi.plugin.javasdk.Dataset.Info i = d.info();
        ReaderOpened.Builder b = ReaderOpened.newBuilder()
            .addShapeTczyx(i.t()).addShapeTczyx(i.c()).addShapeTczyx(i.z()).addShapeTczyx(i.y()).addShapeTczyx(i.x())
            .setDtype(i.dtype().wire())
            .setLittleEndian(true)
            .setAxisInference(i.axisInference())
            .setSpacingUnit(i.spacingUnit())
            .setTimeUnit(i.timeUnit());
        i.spacingZ().ifPresent(b::setSpacingZ);
        i.spacingY().ifPresent(b::setSpacingY);
        i.spacingX().ifPresent(b::setSpacingX);
        i.timeIncrement().ifPresent(b::setTimeIncrement);
        b.addAllChannelNames(i.channelNames());
        return b.build();
      }

      @Override
      public Application.Volume volume(int t, int c) throws Exception {
        return wire(refusals(() -> d.volume(t, c)));
      }

      @Override
      public Application.Volume plane(int t, int z, int c) throws Exception {
        return wire(refusals(() -> d.plane(t, z, c)));
      }

      @Override
      public MetadataResult metadata() throws Exception {
        vizi.plugin.javasdk.Dataset.Metadata m = refusals(d::metadata);
        return MetadataResult.newBuilder().setForm(m.form()).setText(m.text()).build();
      }

      @Override
      public void close() {
        d.close();
      }

      private static Application.Volume wire(vizi.plugin.javasdk.Volume v) {
        return new Application.Volume(v.z(), v.y(), v.x(), v.dtype().wire(), v.data());
      }
    }

    // exporters

    @Override
    public Application.Export openExport(String exporterId, OpenExport msg) throws Exception {
      Exporter e = exporters.get(exporterId);
      if (e == null) throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "this plugin declares no exporter " + exporterId);
      List<Integer> s = msg.getShapeTczyxList();
      if (s.size() != 5) throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.INVALID_ARGUMENT, "shape_tczyx has " + s.size() + " entries");
      DType dt = DType.of(msg.getDtype()).orElseThrow(
          () -> new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.UNSUPPORTED, "dtype " + msg.getDtype() + " is not one this SDK writes"));
      Exporter.Request req = new Exporter.Request(exporterId, msg.getLocator(), s.get(0), s.get(1), s.get(2), s.get(3), s.get(4), dt,
          known(msg.getSpacingZ()),
          known(msg.getSpacingY()),
          known(msg.getSpacingX()),
          msg.getSpacingUnit(),
          known(msg.getTimeIncrement()),
          msg.getTimeUnit(),
          Path.of(msg.getFileStateDir()),
          new vizi.plugin.javasdk.Dataset.Metadata(msg.getSourceMetadata().getForm(), msg.getSourceMetadata().getText()),
          ChannelName.of(msg.getChannelsList()),
          msg.getProvenance(),
          msg.getLittleEndian(),
          msg);
      Exporter.Export x = refusals(() -> e.open(req));
      int z = s.get(2), y = s.get(3), xx = s.get(4);
      return new Application.Export() {
        @Override
        public void chunk(long t, int tRank, int[] channels, int[] cRanks, int[] filtered, MemorySegment bytes) throws Exception {
          Exporter.Chunk ch = new Exporter.Chunk(t, tRank, boxed(channels), boxed(cRanks), boxed(filtered), z, y, xx, dt, bytes);
          run(() -> x.chunk(ch));
        }

        @Override
        public void finish() throws Exception {
          run(() -> x.finish());
        }

        @Override
        public void abort() {
          x.abort();
        }
      };
    }

    /** A calibration field: 0 is unknown, per value (§8.6), and so is anything not positive and finite. */
    private static OptionalDouble known(double v) {
      return Double.isFinite(v) && v > 0 ? OptionalDouble.of(v) : OptionalDouble.empty();
    }

    private static List<Integer> boxed(int[] a) {
      List<Integer> out = new ArrayList<>(a.length);
      for (int v : a) out.add(v);
      return List.copyOf(out);
    }

    // commands and panels

    @Override
    public String invokeCommand(String commandId, Map<String, Value> args) throws Exception {
      return invokeCommand(commandId, args, 0);
    }

    /** Runs a command; a cause it leaves unanswered and undeferred is declined after (§8.15). */
    @Override
    public String invokeCommand(String commandId, Map<String, Value> args, long cause) throws Exception {
      ViewCause vc = cause == 0 ? null : new ViewCause(this, cause, commandId, args);
      try {
        WidgetBinding wb = widgetCommands.get(commandId);
        if (wb != null) {
          run(() -> {
            wb.widget().accept(args);
            if (vc != null) wb.widget().caused(vc);
            else if (wb.widget().movesView()) wb.widget().causeless(); // cause 0: no dataset is open
          });
          return "";
        }
        Command c = commands.get(commandId);
        if (c == null) throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "no command " + commandId);
        CommandContext ctx = new CommandContext(this, commandId, args, vc);
        String text = refusals(() -> c.run(ctx));
        return Objects.requireNonNullElse(text, "");
      } finally {
        if (vc != null) vc.settle();
      }
    }

    @Override
    public void panelOpened(String panelId) {
      Panel p = panels.get(panelId);
      if (p == null) {
        log("PanelOpened for undeclared panel " + panelId);
        return;
      }
      p.repush();
    }

    @Override
    public void panelOpenedHook(String panelId) {
      if (!panels.containsKey(panelId)) return;
      PanelHook h = panelHooks.get(panelId);
      if (h == null) return;
      try {
        h.opened(new CommandContext(this, "", Map.of()));
      } catch (Exception | Error e) {
        log("the panel-opened hook for " + panelId + " failed: " + e);
      }
    }

    @Override
    public void panelSegmentInstalled(String panelId) {
      Panel p = panels.get(panelId);
      if (p != null) p.flushHeldImages();
    }

    // tools

    private @Nullable Tool tool(ToolBinding b) {
      return tools.get(b.getToolId());
    }

    private ToolContext toolContext(ToolBinding b) {
      ToolContext c = toolContexts.computeIfAbsent(Integer.toUnsignedLong(b.getTool()), h -> new ToolContext(this, b.getToolId(), h));
      c.binding = b; // the revision installed now
      return c;
    }

    @Override
    public void toolActivated(ToolBinding binding) {
      Tool t = tool(binding);
      if (t == null) return;
      guarded("activated", binding, () -> t.activated(toolContext(binding)));
    }

    @Override
    public void toolDeactivated(ToolBinding binding) {
      Tool t = tool(binding);
      if (t == null) return;
      guarded("deactivated", binding, () -> t.deactivated(toolContext(binding)));
      toolContexts.remove(Integer.toUnsignedLong(binding.getTool()));
    }

    @Override
    public void input(ToolBinding binding, InputRecord event, long implicitlyClosed, boolean usable,
        Optional<Arrangements.PaneState> pane) {
      Tool t = tool(binding);
      if (t == null) return;
      Optional<InputEvent> ev = Events.event(binding, event, implicitlyClosed, usable, pane);
      if (ev.isEmpty()) return;
      guarded("an event", binding, () -> t.event(toolContext(binding), ev.get()));
    }

    @Override
    public void cancelled(Optional<ToolBinding> binding, Outcome.Cancellation cancellation) {
      if (binding.isEmpty()) return;
      Tool t = tool(binding.get());
      if (t == null) return;
      guarded("cancel", binding.get(), () -> t.event(toolContext(binding.get()), Events.cancel(cancellation)));
    }

    private void guarded(String what, ToolBinding b, Runnable r) {
      try {
        r.run();
      } catch (RuntimeException | Error e) {
        log("tool " + b.getToolId() + " threw from " + what + ": " + e);
        if (vizi.plugin.javasdk.doorbell.Doorbell.fatal(e)) throw e;
      }
    }

    @Override
    public void epochInstalled(Arrangements.PaneState pane) {
      Consumer<PaneContext> h = epochHook;
      if (h == null) return;
      try {
        h.accept(new PaneContext(pane));
      } catch (RuntimeException | Error e) {
        log("the epoch hook threw: " + e);
        if (vizi.plugin.javasdk.doorbell.Doorbell.fatal(e)) throw e;
      }
    }

    // ---- failure mapping ----

    @FunctionalInterface
    interface Body<T> {
      T run() throws Exception;
    }

    @FunctionalInterface
    interface VoidBody {
      void run() throws Exception;
    }

    static void run(VoidBody body) throws Exception {
      try {
        body.run();
      } catch (Refusal r) {
        throw new HandlerFailure(r.kind().wire(), r.getMessage() == null ? r.kind().name() : r.getMessage());
      }
    }

    /** Runs author code, turning a {@link Refusal} into the control layer's {@link HandlerFailure}. */
    static <T> T refusals(Body<T> body) throws Exception {
      try {
        return body.run();
      } catch (Refusal r) {
        throw new HandlerFailure(r.kind().wire(), r.getMessage() == null ? r.kind().name() : r.getMessage());
      }
    }
  }
}
