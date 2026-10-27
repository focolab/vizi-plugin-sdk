package vizi.plugin.javasdk.manifest;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Objects;
import java.time.Duration;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.regex.Pattern;

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
 * @param id the plugin id: lower-case reverse-DNS ({@link #validId}), globally unique; with {@code
 *     version}, the handshake's identity
 * @param name human-readable
 * @param version the plugin's own version
 * @param engine {@code engines.vizi}, the minimum protocol
 * @param minVizi {@code min_vizi}: the oldest vizi application release the plugin runs in, a PEP
 *     440 version ({@link #validMinVizi}); optional
 * @param sdkVersion {@code sdk_version}: the SDK release that built the plugin; {@code
 *     Plugin.builder} sets it to this SDK's release
 * @param entryCommand {@code entry.command}, a non-empty list
 * @param startupS {@code entry.startup_s} (§2.3): how many seconds the plugin expects to take from
 *     being started to {@code Ready}, positive; advisory, a host may honor, cap or ignore it
 * @param permissions the known permissions declared, de-duplicated, in declaration order
 * @param subscriptions the known subscriptions declared, de-duplicated, in declaration order
 */
public record Manifest(
    String id,
    String name,
    String version,
    EngineRequirement engine,
    Optional<String> minVizi,
    Optional<String> sdkVersion,
    List<String> entryCommand,
    OptionalDouble startupS,
    Contributions contributions,
    List<AssetDecl> assets,
    List<Permission> permissions,
    List<Subscription> subscriptions) {

  /**
   * The one {@code manifest_version} this SDK reads and writes. A 1.x addition raises {@code
   * engines.vizi}, never {@code manifest_version}.
   */
  public static final int VERSION = 1;

  private static final Pattern ID = Pattern.compile("[a-z][a-z0-9]*(\\.[a-z][a-z0-9_]*)+");

  /**
   * The PEP 440 versions {@code min_vizi} accepts: the normalized public form without an epoch or a
   * local label -- release segments, then an optional {@code a}/{@code b}/{@code rc} pre-release,
   * {@code .post} and {@code .dev} ({@code 1.0.0}, {@code 1.0rc1}, {@code 1.2.post3}).
   */
  private static final Pattern MIN_VIZI =
      Pattern.compile("[0-9]+(\\.[0-9]+)*((a|b|rc)[0-9]+)?(\\.post[0-9]+)?(\\.dev[0-9]+)?");

  public Manifest {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(version, "version");
    Objects.requireNonNull(engine, "engine");
    Objects.requireNonNull(minVizi, "minVizi");
    Objects.requireNonNull(sdkVersion, "sdkVersion");
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
        id, name, version, engine, minVizi, sdkVersion, command, startupS, contributions, assets,
        permissions, subscriptions);
  }

  /** {@code manifest_version}, which is always {@link #VERSION}. */
  public int manifestVersion() {
    return VERSION;
  }

  /**
   * Whether {@code id} is a plugin id: lower-case reverse-DNS with at least one dot ({@code
   * ^[a-z][a-z0-9]*(\.[a-z][a-z0-9_]*)+$}), 3 to 64 characters, whose first part is not a Windows
   * device name. Distinct ids then name distinct folders, and every one is a folder Windows can
   * create.
   */
  public static boolean validId(String id) {
    return deviceName(id) == null
        && id.length() >= 3
        && id.length() <= 64
        && ID.matcher(id).matches();
  }

  /**
   * The names Windows refuses as a folder name, in any case: an id is an installer's folder name,
   * so its first part may not be one.
   */
  private static final Pattern DEVICE_NAME =
      Pattern.compile("con|prn|aux|nul|com[0-9]|lpt[0-9]", Pattern.CASE_INSENSITIVE);

  /** {@code id}'s first dot-separated part when it is a Windows device name, otherwise null. */
  private static @org.jspecify.annotations.Nullable String deviceName(String id) {
    int dot = id.indexOf('.');
    String first = dot < 0 ? id : id.substring(0, dot);
    return DEVICE_NAME.matcher(first).matches() ? first : null;
  }

  static String idMessage(String id) {
    String part = deviceName(id);
    if (part != null) {
      return "id " + Wire.quote(id) + " is not a plugin id: its first part " + Wire.quote(part)
          + " is a Windows device name";
    }
    return "id " + Wire.quote(id) + " is not a plugin id: lower-case reverse-DNS, 3 to 64"
        + " characters, matching ^[a-z][a-z0-9]*(\\.[a-z][a-z0-9_]*)+$";
  }

  /** Whether {@code v} is a {@code min_vizi} this SDK accepts (see {@link #minVizi}). */
  public static boolean validMinVizi(String v) {
    return MIN_VIZI.matcher(v).matches();
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

  /**
   * A fluent builder. {@code engines.vizi} defaults to this SDK's protocol, {@code "1.0"}; {@code
   * min_vizi} and {@code sdk_version} are absent unless set.
   */
  public static final class Builder {
    private final String id;
    private final String name;
    private final String version;
    private EngineRequirement engine = EngineRequirement.current();
    private Optional<String> minVizi = Optional.empty();
    private Optional<String> sdkVersion = Optional.empty();
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

    /**
     * Sets {@code min_vizi}: the oldest vizi application release the plugin runs in, a PEP 440
     * version such as {@code "1.0.0"}. {@link ManifestParser#validate} refuses another form.
     */
    public Builder minVizi(String version) {
      minVizi = Optional.of(Objects.requireNonNull(version, "version"));
      return this;
    }

    /** Sets {@code sdk_version}: the SDK release that built the plugin, such as {@code "1.0.0"}. */
    public Builder sdkVersion(String release) {
      sdkVersion = Optional.of(Objects.requireNonNull(release, "release"));
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
     * {@code Ready} — a cold JVM, a heavy first start. Advisory: a host may honor, cap or ignore
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
      return new Manifest(
          id, name, version, engine, minVizi, sdkVersion, entryCommand, startupS, c, assets,
          permissions, subscriptions);
    }

    private static double seconds(Duration d) {
      return d.getSeconds() + d.getNano() / 1e9;
    }
  }
}
