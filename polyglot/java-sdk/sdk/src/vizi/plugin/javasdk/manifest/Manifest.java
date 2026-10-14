package vizi.plugin.javasdk.manifest;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.time.Duration;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.OptionalInt;

/**
 * A plugin manifest, {@code vizi-plugin.json} (the model of {@code manifest_schema.md}).
 *
 * <p>Immutable. Fields the spec gives a default for are stored resolved (a reader's {@code name}
 * is its id when absent, a command's {@code menu} is {@link Menu#TOOLS}, ...), so two manifests
 * that mean the same thing are {@code equals}; {@link ManifestWriter} omits a field that equals
 * its default. Unknown fields and unknown permission/subscription strings are not kept.
 *
 * <p>A {@code Manifest} built in code is not validated by construction; {@link
 * ManifestParser#validate} checks it against the same rules as a file.
 *
 * @param manifestVersion 0 or 1 (§7: a set, not a floor)
 * @param id non-blank, globally unique; with {@code version}, the handshake's identity
 * @param name human-readable
 * @param version the plugin's own version
 * @param engine {@code engines.vizi}, the minimum protocol
 * @param entryCommand {@code entry.command}, a non-empty list
 * @param startupS {@code entry.startup_s} (§2.3): how many seconds the plugin expects to take from
 *     being started to {@code Ready}, positive; advisory, a host may honour, cap or ignore it
 * @param permissions the known permissions declared, de-duplicated, in declaration order
 * @param subscriptions the known subscriptions declared, de-duplicated, in declaration order
 */
public record Manifest(
    int manifestVersion,
    String id,
    String name,
    String version,
    EngineRequirement engine,
    List<String> entryCommand,
    OptionalDouble startupS,
    Contributions contributions,
    List<AssetDecl> assets,
    List<Permission> permissions,
    List<Subscription> subscriptions) {

  /** The {@code manifest_version} values this SDK reads (§7). */
  public static final List<Integer> SUPPORTED_VERSIONS = List.of(0, 1);

  public Manifest {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(version, "version");
    Objects.requireNonNull(engine, "engine");
    entryCommand = List.copyOf(entryCommand);
    Objects.requireNonNull(startupS, "startupS");
    Objects.requireNonNull(contributions, "contributions");
    assets = List.copyOf(assets);
    permissions = List.copyOf(new LinkedHashSet<>(permissions));
    subscriptions = List.copyOf(new LinkedHashSet<>(subscriptions));
  }

  public static Builder builder(String id, String name, String version) {
    return new Builder(id, name, version);
  }

  /** This manifest with another {@code entry.command}, everything else (including {@code startup_s}) kept. */
  public Manifest withEntryCommand(List<String> command) {
    return new Manifest(
        manifestVersion, id, name, version, engine, command, startupS, contributions, assets,
        permissions, subscriptions);
  }

  public boolean has(Permission permission) {
    return permissions.contains(permission);
  }

  public boolean subscribes(Subscription subscription) {
    return subscriptions.contains(subscription);
  }

  public List<String> assetIds() {
    return assets.stream().map(AssetDecl::id).toList();
  }

  public Optional<AssetDecl> asset(String assetId) {
    return assets.stream().filter(a -> a.id().equals(assetId)).findFirst();
  }

  /** Whether anything declared needs {@code manifest_version} 1 (§3.6, §3.2's command pane). */
  public boolean needsVersion1() {
    return contributions.tools().stream().anyMatch(ToolDecl::needsVersion1)
        || contributions.commands().stream().anyMatch(c -> c.pane().isPresent());
  }

  /**
   * A fluent builder. {@code manifest_version} defaults to 1: 0 is a development artifact, not a
   * released version, and 1 is a superset of it. {@code
   * engines.vizi} defaults to this SDK's protocol, {@code "1.0"}.
   */
  public static final class Builder {
    private final String id;
    private final String name;
    private final String version;
    private OptionalInt manifestVersion = OptionalInt.empty();
    private EngineRequirement engine = EngineRequirement.current();
    private List<String> entryCommand = List.of();
    private OptionalDouble startupS = OptionalDouble.empty();
    private final List<ReaderDecl> readers = new ArrayList<>();
    private final List<CommandDecl> commands = new ArrayList<>();
    private final List<PanelDecl> panels = new ArrayList<>();
    private final List<ExporterDecl> exporters = new ArrayList<>();
    private final List<OverlayDecl> overlays = new ArrayList<>();
    private final List<ToolDecl> tools = new ArrayList<>();
    private final List<StageDecl> stages = new ArrayList<>();
    private final List<RegionTypeDecl> regionTypes = new ArrayList<>();
    private final List<AssetDecl> assets = new ArrayList<>();
    private final List<Permission> permissions = new ArrayList<>();
    private final List<Subscription> subscriptions = new ArrayList<>();

    private Builder(String id, String name, String version) {
      this.id = Objects.requireNonNull(id, "id");
      this.name = Objects.requireNonNull(name, "name");
      this.version = Objects.requireNonNull(version, "version");
    }

    /** Pins {@code manifest_version}; otherwise it is 1. A pinned 0 refuses the v1-only fields. */
    public Builder manifestVersion(int v) {
      manifestVersion = OptionalInt.of(v);
      return this;
    }

    /**
     * Sets {@code engines.vizi}.
     *
     * @throws IllegalArgumentException if it is not a version (§2.1)
     */
    public Builder engine(String vizi) {
      engine = EngineRequirement.parse(vizi);
      return this;
    }

    public Builder entryCommand(String... command) {
      entryCommand = List.of(command);
      return this;
    }

    public Builder entryCommand(List<String> command) {
      entryCommand = List.copyOf(command);
      return this;
    }

    /**
     * {@code entry.startup_s} (§2.3): how long the plugin expects to take from being started to
     * {@code Ready} — a cold JVM, a heavy first start. Advisory: a host may honour, cap or ignore
     * it (vizi holds it to 10–300 s and gives 60 s without it), so nothing may depend on it. It
     * must be positive; {@link ManifestParser#validate} refuses anything else.
     */
    public Builder startup(Duration expected) {
      startupS = OptionalDouble.of(seconds(expected));
      return this;
    }

    public Builder reader(ReaderDecl r) {
      readers.add(r);
      return this;
    }

    public Builder command(CommandDecl c) {
      commands.add(c);
      return this;
    }

    public Builder panel(PanelDecl p) {
      panels.add(p);
      return this;
    }

    public Builder exporter(ExporterDecl e) {
      exporters.add(e);
      return this;
    }

    public Builder overlay(OverlayDecl o) {
      overlays.add(o);
      return this;
    }

    public Builder tool(ToolDecl t) {
      tools.add(t);
      return this;
    }

    public Builder stage(StageDecl s) {
      stages.add(s);
      return this;
    }

    public Builder regionType(RegionTypeDecl r) {
      regionTypes.add(r);
      return this;
    }

    public Builder asset(AssetDecl a) {
      assets.add(a);
      return this;
    }

    public Builder permissions(Permission... ps) {
      permissions.addAll(Arrays.asList(ps));
      return this;
    }

    public Builder subscriptions(Subscription... ss) {
      subscriptions.addAll(Arrays.asList(ss));
      return this;
    }

    public Manifest build() {
      Contributions c =
          new Contributions(readers, commands, panels, exporters, overlays, tools, stages, regionTypes);
      int v = manifestVersion.orElse(1);
      return new Manifest(
          v, id, name, version, engine, entryCommand, startupS, c, assets, permissions,
          subscriptions);
    }

    private static double seconds(Duration d) {
      return d.getSeconds() + d.getNano() / 1e9;
    }
  }
}
