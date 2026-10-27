package vizi.plugin.javasdk;

import java.io.IOException;
import java.io.PrintStream;
import java.net.URISyntaxException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardCopyOption;
import java.security.CodeSource;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.ExecutionException;
import vizi.plugin.javasdk.control.Serving;
import vizi.plugin.javasdk.doorbell.LaunchFailure;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.ManifestDiff;
import vizi.plugin.javasdk.manifest.ManifestException;
import vizi.plugin.javasdk.manifest.ManifestParser;
import vizi.plugin.javasdk.manifest.ManifestWriter;

/**
 * The plugin's {@code main}: {@code System.exit(PluginMain.run(plugin, args))}.
 *
 * <ul>
 *   <li>{@code --manifest[=DIR]} writes the declared manifest, and the pane icons it names, into
 *       {@code DIR} (default: the working directory);
 *   <li>{@code --install[=DIR]} installs the running jar as {@code DIR/<id>/} ({@code DIR}
 *       defaults to {@code $VIZI_PLUGINS_DIR}, else {@code ~/.vizi/plugins}). The jar is copied
 *       under a name taken from its content ({@code plugin-<16 hex of its sha256>.jar}), every file
 *       is written atomically, and the manifest goes last, so a failed install leaves the old one
 *       working; the jars it replaced are then deleted where the system allows. A directory
 *       this installer did not write, or wrote for another plugin, needs {@code --force}. With
 *       {@code --launcher=path} (the default) the launch line runs this JVM by its absolute path;
 *       with {@code --launcher=jbang} it runs {@code jbang --java 25+}, which pins the JDK by
 *       version and fetches one if none is installed;
 *   <li>{@code --help};
 *   <li>no argument: serve the session vizi launched this process for, and return when it ends.
 * </ul>
 */
public final class PluginMain {
  private PluginMain() {}

  /** The flags every JVM running this SDK needs (FFM, and Netty on JDK 24+). */
  public static final List<String> JVM_FLAGS = List.of("--enable-native-access=ALL-UNNAMED", "--sun-misc-unsafe-memory-access=allow");

  /** Runs the plugin's command line; every failure says so in a headline, with why and how to fix it. */
  public static int run(Plugin plugin, String[] args) {
    Class<?> caller = StackWalker.getInstance(StackWalker.Option.RETAIN_CLASS_REFERENCE).getCallerClass();
    return run(plugin, args, System.getenv(), System.out, System.err, caller);
  }

  /** How an installed plugin's launch line starts its JVM. */
  enum Launcher {
    /** The JVM that ran {@code --install}, by its absolute path. */
    PATH,
    /** {@code jbang --java 25+}: the JDK pinned by version, fetched by jbang when absent. */
    JBANG
  }

  /** The JDK a jbang launch line asks for: this SDK's floor. */
  static final String JBANG_JAVA = "25+";

  /** How long the install's jbang warm-up may take: it may download a JDK. */
  static final java.time.Duration JBANG_WARMUP = java.time.Duration.ofMinutes(15);

  static int run(Plugin plugin, String[] args, Map<String, String> env, PrintStream out, PrintStream err, Class<?> mainClass) {
    return run(plugin, args, env, out, err, mainClass, Running.of(mainClass));
  }

  /**
   * What the installer would copy: the jar holding the main class, if it runs from one, and the
   * classpath entries the JVM loaded from outside that jar and its {@code Class-Path} (which an
   * installed copy would not have).
   */
  record Running(Optional<Path> jar, List<String> outside, List<String> classPath) {
    Running(Optional<Path> jar, List<String> outside) {
      this(jar, outside, List.of());
    }

    static Running of(Class<?> mainClass) {
      Optional<Path> jar = jarOf(mainClass);
      List<String> cp = new ArrayList<>();
      for (String e : System.getProperty("java.class.path", "").split(java.io.File.pathSeparator)) if (!e.isBlank()) cp.add(e);
      if (jar.isEmpty()) return new Running(jar, List.of(), List.copyOf(cp));
      java.util.Set<Path> inside = new java.util.HashSet<>();
      Path self = jar.get().toAbsolutePath().normalize();
      inside.add(self);
      try {
        for (String dep : PluginMain.classPath(jar.get())) inside.add(self.resolveSibling(dep).toAbsolutePath().normalize());
      } catch (IOException | RuntimeException e) {
        // an unreadable jar is reported when the install reads it
      }
      List<String> outside = new ArrayList<>();
      for (String e : System.getProperty("java.class.path", "").split(java.io.File.pathSeparator)) {
        if (e.isBlank()) continue;
        try {
          if (!inside.contains(Path.of(e).toAbsolutePath().normalize())) outside.add(e);
        } catch (java.nio.file.InvalidPathException ex) {
          outside.add(e);
        }
      }
      return new Running(jar, List.copyOf(outside), List.copyOf(cp));
    }
  }

  /** What the command line asked for, for the headline: the first action named. */
  private enum Action { INSTALL, MANIFEST, NONE }

  /**
   * A failure the person reads: {@code why} is the cause, in plain words, with the
   * operation and the full path; {@code state} what is left; {@code fix} the step(s), as exact
   * flags or commands where they are known.
   */
  static final class Failure extends Exception {
    private static final long serialVersionUID = 1L;
    final String why;
    final String state;
    final List<String> fix;
    final int exit;

    Failure(String why, String state, List<String> fix) {
      this(why, state, fix, 1);
    }

    Failure(String why, String state, List<String> fix, int exit) {
      super(why, null, false, false);
      this.why = why;
      this.state = state;
      this.fix = List.copyOf(fix);
      this.exit = exit;
    }

    static Failure usage(String why, String... fix) {
      List<String> f = new ArrayList<>(List.of(fix));
      f.add("Run with --help to see every option.");
      return new Failure(why, NOTHING, f, 2);
    }
  }

  static final String NOTHING = "Nothing was written.";

  /** Where the failing action was going, for the headline; set as soon as it is known. */
  private static final class Going {
    @org.jspecify.annotations.Nullable String where;
    String doing = "reading the command line";
  }

  static int run(Plugin plugin, String[] args, Map<String, String> env, PrintStream out, PrintStream err, Class<?> mainClass,
      Running running) {
    if (args.length == 0) return serve(plugin, env, err);
    Action action = Action.NONE;
    for (String x : args) {
      if (action == Action.NONE && (x.equals("--install") || x.startsWith("--install="))) action = Action.INSTALL;
      if (action == Action.NONE && (x.equals("--manifest") || x.startsWith("--manifest="))) action = Action.MANIFEST;
    }
    Going going = new Going();
    // where an install was going, for a headline even when the rest of the line is not understood
    for (int i = 0; i < args.length; i++) {
      String x = args[i];
      if (going.where != null || !(x.equals("--install") || x.startsWith("--install="))) continue;
      // "--install DIR" means DIR, though it is refused below; an empty "--install=" names nowhere
      String typed = x.equals("--install") ? (i + 1 < args.length && !args[i + 1].startsWith("-") ? args[i + 1] : null)
          : x.substring("--install=".length());
      if (typed != null && typed.isEmpty()) break;
      try {
        Path dir = typed == null ? defaultPluginsDir(env) : Path.of(typed);
        going.where = dir.toAbsolutePath().resolve(plugin.manifest().id()).toString();
      } catch (java.nio.file.InvalidPathException e) {
        // reported as a usage mistake below
      }
    }
    try {
      return dispatch(plugin, args, env, out, err, mainClass, running, going);
    } catch (Failure f) {
      report(plugin, action, going, f, err);
      return f.exit;
    } catch (RuntimeException | java.lang.Error e) {
      if (e instanceof VirtualMachineError) throw e;
      String what = e.getClass().getSimpleName() + (e.getMessage() == null ? "" : ": " + e.getMessage());
      report(plugin, action, going, new Failure("an unexpected error while " + going.doing + ": " + what, unknownState(action, going),
          List.of("This is a bug in the plugin or the Java SDK. Run again with VIZI_SDK_TRACE=1 for the stack trace, and report it.")), err);
      if (env.get("VIZI_SDK_TRACE") != null) e.printStackTrace(err);
      return 1;
    }
  }

  private static String unknownState(Action action, Going going) {
    return action == Action.INSTALL && going.where != null
        ? "The manifest at " + going.where + " was not replaced, so any previous install there is unchanged; check that directory."
        : "Check what was written.";
  }

  /** Headline, then why, what is left and the fix, each set apart. No stack trace. */
  private static void report(Plugin plugin, Action action, Going going, Failure f, PrintStream err) {
    String who = plugin.manifest().id() + " " + plugin.manifest().version();
    String head = switch (action) {
      case INSTALL -> "INSTALL FAILED: " + who + " was not installed" + (going.where == null ? "" : " into " + going.where);
      case MANIFEST -> "MANIFEST NOT WRITTEN: " + who + "'s " + ManifestParser.FILE_NAME + " was not written"
          + (going.where == null ? "" : " into " + going.where);
      case NONE -> "FAILED: " + who + " did nothing: the command line was not understood";
    };
    err.println(head);
    err.println("  Why: " + f.why);
    err.println("  " + f.state);
    err.println("  To fix:");
    for (String line : f.fix) err.println("    " + line);
  }

  private static int dispatch(Plugin plugin, String[] args, Map<String, String> env, PrintStream out, PrintStream err,
      Class<?> mainClass, Running running, Going going) throws Failure {
    Launcher launcher = null;
    boolean force = false;
    List<String> actions = new ArrayList<>();
    for (int i = 0; i < args.length; i++) {
      String x = args[i];
      String prev = i == 0 ? "" : args[i - 1];
      if (x.startsWith("--launcher=")) {
        String v = x.substring("--launcher=".length());
        if (launcher != null) throw Failure.usage("--launcher is given twice", "Give one: --launcher=path or --launcher=jbang.");
        if (v.equals("path")) launcher = Launcher.PATH;
        else if (v.equals("jbang")) launcher = Launcher.JBANG;
        else throw Failure.usage("unknown launcher '" + v + "' in '" + x + "'", "Use --launcher=path (the default) or --launcher=jbang.");
      } else if (x.equals("--force")) {
        force = true;
      } else if (x.equals("--install") || x.startsWith("--install=") || x.equals("--manifest") || x.startsWith("--manifest=")
          || x.equals("--help") || x.equals("-h")) {
        actions.add(x);
      } else if (x.startsWith("--runner")) {
        String v = x.contains("=") ? x.substring(x.indexOf('=') + 1) : "";
        String why = "unknown argument '" + x + "'; the Java SDK has no --runner (that is another SDK's spelling)";
        if (v.equals("java")) throw Failure.usage(why, "Use --launcher instead: --install --launcher=path");
        if (v.equals("jbang")) throw Failure.usage(why, "Use --launcher instead: --install --launcher=jbang");
        throw Failure.usage(why, "Use --launcher, which is one of:", "  --install --launcher=path    (this JVM, by its absolute path; the default)",
            "  --install --launcher=jbang   (jbang --java " + JBANG_JAVA + ")");
      } else if (x.isEmpty() && (prev.equals("--install") || prev.equals("--manifest"))) {
        // an unset "$DIR" in a script: the same refusal as an empty --install=
        throw emptyValue(prev, env, "an empty argument follows " + prev + " (a quoted variable that is unset, perhaps)");
      } else if (!x.startsWith("-") && (prev.equals("--install") || prev.equals("--manifest"))) {
        throw Failure.usage("'" + x + "' follows " + prev + " as a separate argument; " + prev + " takes its directory after '='",
            "Write it as one argument: " + prev + "=" + x);
      } else {
        String near = nearest(x);
        if (near != null) throw Failure.usage("unknown argument '" + x + "'", "Did you mean " + near + "?");
        throw Failure.usage("unknown argument '" + x + "'");
      }
    }
    if (actions.size() > 1) {
      throw Failure.usage("two actions were given, '" + actions.get(0) + "' and '" + actions.get(1) + "'; give one", "Run once per action.");
    }
    if (actions.isEmpty()) {
      String flag = launcher != null ? "--launcher=" + (launcher == Launcher.JBANG ? "jbang" : "path") : "--force";
      throw Failure.usage(flag + " was given with no action; it goes with --install", "Add --install: --install " + flag);
    }
    String a = actions.get(0);
    boolean installing = a.equals("--install") || a.startsWith("--install=");
    if (launcher != null && !installing) {
      throw Failure.usage("--launcher goes with --install, not with '" + a + "'", "Drop --launcher, or install: --install --launcher=...");
    }
    if (force && !installing) throw Failure.usage("--force goes with --install, not with '" + a + "'", "Drop --force, or install: --install --force");
    if (a.equals("--help") || a.equals("-h")) {
      usage(plugin, out);
      return 0;
    }
    if (a.equals("--install=")) throw emptyValue("--install", env, "--install= names no directory");
    if (a.equals("--manifest=")) throw emptyValue("--manifest", env, "--manifest= names no directory");
    if (a.equals("--manifest") || a.startsWith("--manifest=")) {
      Path dir = path(a.equals("--manifest") ? "" : a.substring("--manifest=".length()), "--manifest=DIR").toAbsolutePath();
      going.where = dir.toString();
      if (Files.exists(dir) && !Files.isDirectory(dir)) {
        throw new Failure(dir + " is a file, and --manifest=DIR names a directory", NOTHING,
            List.of("Name a directory: --manifest=" + (dir.getParent() == null ? "DIR" : dir.getParent().toString())));
      }
      Path to = dir.resolve(ManifestParser.FILE_NAME);
      going.doing = "writing the manifest";
      Map<String, byte[]> icons = iconFiles(plugin.manifest(), mainClass);
      for (var e : icons.entrySet()) writeInside(dir, e.getKey(), e.getValue(), "pane icon");
      byte[] text = ManifestWriter.write(plugin.manifest()).getBytes(StandardCharsets.UTF_8);
      io("writing", to, () -> writeAtomically(to, text), List.of("Write it where you can: --manifest=DIR"));
      out.println("WROTE: " + to);
      return 0;
    }
    Path dir = a.equals("--install") ? defaultPluginsDir(env) : path(a.substring("--install=".length()), "--install=DIR");
    going.where = dir.toAbsolutePath().resolve(plugin.manifest().id()).toString();
    going.doing = "checking what to install";
    if (running.jar().isEmpty()) {
      boolean mill = running.classPath().stream().anyMatch(e -> e.replace('\\', '/').matches(".*/out/(.*/)?compile\\.dest/classes/?"));
      List<String> fix = new ArrayList<>();
      fix.add("Build a jar that carries its libraries (an assembly, a fat jar, or jbang export portable), then install from it:");
      if (mill) {
        fix.add("  mill assembly");
        fix.add("  java " + String.join(" ", JVM_FLAGS) + " -jar out/assembly.dest/out.jar --install");
      } else {
        fix.add("  java " + String.join(" ", JVM_FLAGS) + " -jar YOUR-PLUGIN.jar --install");
      }
      throw new Failure("--install copies the jar this process runs from, and it is not running from a jar (a build tool's"
          + " class directories, most likely)", NOTHING, fix);
    }
    if (!running.outside().isEmpty()) {
      Path jar = running.jar().get();
      boolean jbangRun = jar.toString().replace('\\', '/').contains("/.jbang/");
      String src = jbangScript(jar).orElse(mainClass.getSimpleName() + ".java");
      List<String> fix = new ArrayList<>();
      fix.add("Install from a jar that carries its libraries.");
      if (jbangRun) {
        fix.add("  jbang export portable --force -O build/plugin.jar " + src);
        fix.add("  java " + String.join(" ", JVM_FLAGS) + " -jar build/plugin.jar --install");
      } else {
        fix.add("Build one (an assembly, or a jar whose Class-Path names its libraries beside it) and run it with java -jar ... --install.");
      }
      throw new Failure(jar + " holds the plugin but not everything it runs on: the JVM also loaded " + outsideText(running.outside())
          + ", which the jar does not carry or name in its Class-Path, so an installed copy would not start", NOTHING, fix);
    }
    return install(plugin, running.jar().get(), dir, force, launcher == null ? Launcher.PATH : launcher, env, out, err, mainClass, going);
  }

  /** An empty directory for {@code flag}, whether written {@code --install=} or {@code --install ""}. */
  private static Failure emptyValue(String flag, Map<String, String> env, String what) {
    return flag.equals("--install")
        ? Failure.usage(what + ": the folder given is empty (it would install into the working directory)",
            "Give the plugins directory: --install=DIR", "or leave the value off for the default, " + defaultPluginsDir(env) + ": --install")
        : Failure.usage(what + ": the folder given is empty", "Give one: --manifest=DIR", "or leave the value off for the working directory: --manifest");
  }

  /** The script jbang built a cache jar from: its directory is {@code <Script>.java.<hash>}. */
  static Optional<String> jbangScript(Path jar) {
    Path dir = jar.getParent();
    if (dir == null || dir.getFileName() == null) return Optional.empty();
    var m = java.util.regex.Pattern.compile("(.+\\.(?:java|jsh|kt|groovy))\\.[0-9a-f]{16,}").matcher(dir.getFileName().toString());
    return m.matches() ? Optional.of(m.group(1)) : Optional.empty();
  }

  /** The nearest ancestor of {@code p} (itself included) that exists: the directory that refused a write below it. */
  static Path nearestExisting(Path p) {
    Path q = p.toAbsolutePath();
    while (q != null && !Files.exists(q)) q = q.getParent();
    return q == null ? p.toAbsolutePath() : q;
  }

  private static final List<String> FLAGS = List.of("--install", "--manifest", "--force", "--launcher", "--help");

  /** The flag {@code typed} most likely meant (two edits or fewer), with its {@code =VALUE} kept; or null. */
  static @org.jspecify.annotations.Nullable String nearest(String typed) {
    int eq = typed.indexOf('=');
    String flag = eq < 0 ? typed : typed.substring(0, eq);
    String best = null;
    int bestD = 3;
    for (String f : FLAGS) {
      int d = editDistance(flag, f);
      if (d < bestD) {
        bestD = d;
        best = f;
      }
    }
    return best == null ? null : best + (eq < 0 ? "" : typed.substring(eq));
  }

  private static int editDistance(String a, String b) {
    int[] prev = new int[b.length() + 1];
    for (int j = 0; j <= b.length(); j++) prev[j] = j;
    for (int i = 1; i <= a.length(); i++) {
      int[] cur = new int[b.length() + 1];
      cur[0] = i;
      for (int j = 1; j <= b.length(); j++) {
        cur[j] = Math.min(Math.min(cur[j - 1] + 1, prev[j] + 1), prev[j - 1] + (a.charAt(i - 1) == b.charAt(j - 1) ? 0 : 1));
      }
      prev = cur;
    }
    return prev[b.length()];
  }

  private static String outsideText(List<String> outside) {
    String first = outside.get(0);
    return outside.size() == 1 ? first : first + " and " + (outside.size() - 1) + " more classpath entr" + (outside.size() == 2 ? "y" : "ies");
  }

  /** A path the person typed, refused plainly when the system cannot take it. */
  private static Path path(String typed, String flag) throws Failure {
    try {
      return Path.of(typed);
    } catch (java.nio.file.InvalidPathException e) {
      throw Failure.usage("'" + typed + "' is not a path this system accepts (" + e.getReason() + ")", "Give another: " + flag);
    }
  }

  @FunctionalInterface
  interface IoStep {
    void run() throws IOException;
  }

  /** One file operation: an {@code IOException} becomes a failure naming the operation, the full path and the cause. */
  static void io(String doing, Path path, IoStep step, List<String> fix) throws Failure {
    io(doing, path, step, NOTHING, fix);
  }

  static void io(String doing, Path path, IoStep step, String state, List<String> fix) throws Failure {
    try {
      step.run();
    } catch (IOException e) {
      List<String> f = new ArrayList<>(fix);
      if (f.isEmpty()) f.add("Check that " + path.toAbsolutePath() + " is writable and that its disk has room.");
      throw new Failure(why(e, doing, path), state, f);
    }
  }

  /**
   * An I/O failure in words: what was being done, on which full path, and why — never a bare path
   * or {@code null} ("permission denied creating the directory /opt/vizi/plugins/x").
   */
  static String why(IOException e, String doing, Path path) {
    String at = doing + " " + path.toAbsolutePath();
    return switch (e) {
      case java.nio.file.AccessDeniedException x -> "permission denied " + at;
      case java.nio.file.NoSuchFileException x -> "no such file or directory " + at;
      default -> at + " failed: " + cause(e);
    };
  }

  /** The cause alone, in words. */
  static String cause(IOException e) {
    return switch (e) {
      case java.nio.file.AccessDeniedException x -> "permission denied";
      case java.nio.file.NoSuchFileException x -> "no such file or directory";
      case java.nio.file.FileAlreadyExistsException x -> "something is already there";
      case java.nio.file.NotDirectoryException x -> "a part of the path is not a directory";
      case java.nio.file.DirectoryNotEmptyException x -> "the directory is not empty";
      case java.nio.file.FileSystemException x when x.getReason() != null -> lower(x.getReason());
      default -> e.getMessage() == null ? e.getClass().getSimpleName() : lower(e.getMessage());
    };
  }

  private static String lower(String s) {
    return s.isEmpty() ? s : Character.toLowerCase(s.charAt(0)) + s.substring(1);
  }

  private static void usage(Plugin plugin, PrintStream to) {
    to.println(plugin.manifest().name() + " " + plugin.manifest().version() + " (" + plugin.manifest().id() + "), a vizi plugin");
    to.println("  (no argument)      serve: vizi launches the plugin this way");
    to.println("  --manifest[=DIR]   write " + ManifestParser.FILE_NAME + " and its pane icons into DIR (default: here)");
    to.println("  --install[=DIR]    install into DIR/" + plugin.manifest().id() + " (default $VIZI_PLUGINS_DIR or ~/.vizi/plugins)");
    to.println("  --force            (with --install) write into a directory this installer did not write for this plugin");
    to.println("  --launcher=path    (with --install) launch with this JVM, by its absolute path (the default)");
    to.println("  --launcher=jbang   (with --install) launch with jbang --java " + JBANG_JAVA + ": the JDK pinned by version");
    to.println("  --help             this text");
  }

  static Path defaultPluginsDir(Map<String, String> env) {
    String d = env.get("VIZI_PLUGINS_DIR");
    if (d != null && !d.isBlank()) return Path.of(d);
    return Path.of(System.getProperty("user.home"), ".vizi", "plugins");
  }

  /**
   * Written into every directory this installer fills, naming the SDK and the plugin; a directory
   * without it, or whose marker names another plugin, is not this installer's to write.
   */
  static final String MARKER = ".vizi-java-sdk";

  /** Held (created exclusively) while an install writes; one a crash left must be removed by hand. */
  static final String LOCK = ".vizi-install-lock";

  static String markerText(String pluginId) {
    return "sdk vizi-sdk-java\nplugin " + pluginId + "\n";
  }

  /** Whether the marker's contents name this SDK and this plugin (the version and the rest aside). */
  static boolean markerNames(String text, String pluginId) {
    List<String> lines = text.lines().toList();
    return lines.contains("sdk vizi-sdk-java") && lines.contains("plugin " + pluginId);
  }

  private static final java.util.Set<String> DEVICE_NAMES = deviceNames();

  private static java.util.Set<String> deviceNames() {
    java.util.Set<String> s = new java.util.HashSet<>(List.of("CON", "PRN", "AUX", "NUL"));
    for (int i = 1; i <= 9; i++) {
      s.add("COM" + i);
      s.add("LPT" + i);
    }
    return java.util.Set.copyOf(s);
  }

  /**
   * Whether {@code id} names a plugin directory the same way everywhere: 1 to 200 ASCII letters,
   * digits, {@code . _ -}, starting with a letter or digit, not ending in {@code .}, no Windows
   * device name before the first dot. The schema asks only for a non-blank id, and an id becomes a
   * path here, where a {@code /} or {@code ..} would leave the plugins folder.
   */
  static @org.jspecify.annotations.Nullable String idProblem(String id) {
    boolean fine = !id.isEmpty() && id.length() <= 200 && Character.isLetterOrDigit(id.charAt(0)) && id.charAt(0) < 128
        && id.chars().allMatch(c -> c < 128 && (Character.isLetterOrDigit(c) || c == '.' || c == '_' || c == '-'))
        && !id.endsWith(".");
    String stem = id.contains(".") ? id.substring(0, id.indexOf('.')) : id;
    if (fine && !DEVICE_NAMES.contains(stem.toUpperCase(java.util.Locale.ROOT))) return null;
    return "the plugin id '" + id + "' cannot name a plugin directory everywhere: installing needs 1 to 200 ASCII letters,"
        + " digits, '.', '_' or '-', starting with a letter or digit, no trailing '.', and no Windows device name (CON, NUL,"
        + " COM1, ...) before the first '.'";
  }

  /** Every pane icon the manifest names. */
  static List<String> icons(Manifest m) {
    List<String> out = new ArrayList<>();
    m.contributions().commands().forEach(c -> c.pane().ifPresent(p -> out.add(p.icon())));
    for (var t : m.contributions().tools()) {
      t.pane().ifPresent(p -> out.add(p.icon()));
      t.actions().forEach(a -> a.pane().ifPresent(p -> out.add(p.icon())));
    }
    return out.stream().distinct().toList();
  }

  /** An icon's bytes: the classpath resource at its path, else the file under the working directory. */
  private static byte[] iconBytes(String icon, Class<?> mainClass) throws Failure {
    Path local = Path.of(icon).toAbsolutePath();
    try {
      try (var in = mainClass.getClassLoader() == null ? null : mainClass.getClassLoader().getResourceAsStream(icon)) {
        if (in != null) return in.readAllBytes();
      }
      if (Files.isRegularFile(local)) return Files.readAllBytes(local);
    } catch (IOException e) {
      throw new Failure(why(e, "reading the pane icon", local), NOTHING, List.of("Check that the icon file is readable."));
    }
    throw new Failure("the pane icon '" + icon + "' is neither a resource in the plugin's jar nor a file at " + local
        + ", and a host refuses a manifest whose icon is absent", NOTHING,
        List.of("Put the icon in the jar's resources at " + icon + " (or beside the working directory), then run this again."));
  }

  /** Each pane icon's bytes, by the path the manifest names. */
  private static Map<String, byte[]> iconFiles(Manifest m, Class<?> mainClass) throws Failure {
    Map<String, byte[]> out = new java.util.LinkedHashMap<>();
    for (String icon : icons(m)) out.put(icon, iconBytes(icon, mainClass));
    return out;
  }

  /** Writes {@code rel} under {@code dir}, refusing a path that leaves it. */
  private static void writeInside(Path dir, String rel, byte[] bytes, String what) throws Failure {
    writeInside(dir, rel, bytes, what, NOTHING);
  }

  private static void writeInside(Path dir, String rel, byte[] bytes, String what, String state) throws Failure {
    Path to = dir.resolve(rel).normalize();
    if (!to.startsWith(dir)) {
      throw new Failure("the " + what + " '" + rel + "' would be written outside " + dir, state,
          List.of("Name it by a relative path inside the plugin, with no '..'."));
    }
    io("writing the " + what, to, () -> writeAtomically(to, bytes), state, List.of());
  }

  /** The installed jar's name: {@code plugin-} and the first 16 hex digits of its sha256. */
  static String jarName(byte[] jar) {
    try {
      byte[] d = java.security.MessageDigest.getInstance("SHA-256").digest(jar);
      return "plugin-" + java.util.HexFormat.of().formatHex(d, 0, 8) + ".jar";
    } catch (java.security.NoSuchAlgorithmException e) {
      throw new IllegalStateException(e); // every JDK has SHA-256
    }
  }

  /** For tests: an install as {@code --install} runs it, failures reported as the person sees them. */
  static int install(Plugin plugin, Path jar, Path pluginsDir, boolean force, Launcher launcher, Map<String, String> env,
      PrintStream out, PrintStream err, Class<?> mainClass) {
    Going going = new Going();
    going.where = pluginsDir.toAbsolutePath().resolve(plugin.manifest().id()).toString();
    try {
      return install(plugin, jar, pluginsDir, force, launcher, env, out, err, mainClass, going);
    } catch (Failure f) {
      report(plugin, Action.INSTALL, going, f, err);
      return f.exit;
    }
  }

  private static int install(Plugin plugin, Path jar, Path pluginsDir, boolean force, Launcher launcher, Map<String, String> env,
      PrintStream out, PrintStream err, Class<?> mainClass, Going going) throws Failure {
    Manifest m = plugin.manifest();
    String bad = idProblem(m.id());
    if (bad != null) throw new Failure(bad, NOTHING, List.of("Give the plugin an id of that form in Plugin.builder(...)."));
    Path target = pluginsDir.toAbsolutePath().resolve(m.id());
    going.where = target.toString();
    List<String> elsewhere = List.of("Install somewhere you can write: --install=DIR (or set VIZI_PLUGINS_DIR=DIR),"
        + " or fix the permissions of " + nearestExisting(target) + ".");
    if (Files.exists(target) && !Files.isDirectory(target)) {
      throw new Failure("a file, not a directory, is in the way at " + target, NOTHING,
          List.of("Move or remove that file, then run this again; or install elsewhere with --install=DIR."));
    }
    // ownership is checked before anything is created, so a refused first install leaves no directory
    // behind; it is checked again under the lock, where it decides
    if (!force) checkOwnership(m, target);
    // jbang: the bare name when it is on PATH, else jbang's own install location, by absolute path
    Jbang jbang = null;
    if (launcher == Launcher.JBANG) {
      jbang = findJbang(env).orElse(null);
      if (jbang == null) {
        throw new Failure("--launcher=jbang was asked for, and jbang is neither on PATH nor at " + jbangHome(), NOTHING, List.of(
            "Install jbang (https://www.jbang.dev/download/), then run this again;",
            "or install without it: --install --launcher=path"));
      }
    }
    going.doing = "reading the pane icons";
    Map<String, byte[]> icons = iconFiles(m, mainClass);
    boolean created = !Files.exists(target);
    going.doing = "creating the plugin directory";
    io("creating the directory", target, () -> Files.createDirectories(target), elsewhere);
    Path lock = target.resolve(LOCK);
    try {
      Files.createFile(lock);
    } catch (java.nio.file.FileAlreadyExistsException e) {
      throw new Failure("another install holds the lock " + lock + ": one is writing this plugin now, or one was interrupted",
          "Nothing was written; the previous install, if any, is unchanged.", List.of(
          "If no other install is running, remove the lock and run this again:",
          "  " + (Platformish.windows() ? "del \"" + lock + "\"" : "rm '" + lock + "'")));
    } catch (IOException e) {
      removeIfEmpty(created, target);
      throw new Failure(why(e, "creating the install lock", lock), NOTHING, elsewhere);
    }
    try {
      return installLocked(m, jar, target, force, jbang, icons, out, err, going);
    } finally {
      try {
        Files.deleteIfExists(lock);
      } catch (IOException e) {
        err.println("warning: the install lock " + lock + " was left behind (deleting it failed: " + cause(e) + "); remove it by hand once no install is running");
      }
      removeIfEmpty(created, target);
    }
  }

  /** A directory this run created and left empty (a failure before any file) goes again. */
  private static void removeIfEmpty(boolean created, Path target) {
    if (!created) return;
    try (var l = Files.list(target)) {
      if (l.findAny().isEmpty()) Files.deleteIfExists(target);
    } catch (IOException e) {
      // it stays; the failure already says nothing was installed
    }
  }

  /** A non-empty directory must carry this installer's marker for this plugin, unless --force. */
  private static void checkOwnership(Manifest m, Path target) throws Failure {
    if (!Files.isDirectory(target)) return;
    List<Path> there = new ArrayList<>();
    io("listing", target, () -> {
      try (var l = Files.list(target)) {
        l.filter(f -> !f.getFileName().toString().equals(LOCK)).forEach(there::add);
      }
    }, List.of());
    if (there.isEmpty()) return;
    Path marker = target.resolve(MARKER);
    if (!Files.isRegularFile(marker)) {
      throw new Failure(target + " already holds files, and this installer did not write them (no " + MARKER + " marker)", NOTHING,
          List.of("If that directory is yours to replace, add --force: --install --force", "or install elsewhere: --install=DIR"));
    }
    String[] text = new String[1];
    io("reading", marker, () -> text[0] = Files.readString(marker, StandardCharsets.UTF_8), List.of());
    if (!markerNames(text[0], m.id())) {
      throw new Failure(target + " was written by an installer for another plugin or SDK (its marker " + marker + " says so)", NOTHING,
          List.of("If that directory is yours to replace, add --force: --install --force", "or install elsewhere: --install=DIR"));
    }
  }

  private static int installLocked(Manifest m, Path jar, Path target, boolean force, @org.jspecify.annotations.Nullable Jbang jbang,
      Map<String, byte[]> icons, PrintStream out, PrintStream err, Going going) throws Failure {
    List<Path> before = new ArrayList<>();
    io("listing", target, () -> {
      try (var l = Files.list(target)) {
        l.filter(f -> !f.getFileName().toString().equals(LOCK)).forEach(before::add);
      }
    }, List.of());
    Path marker = target.resolve(MARKER);
    if (!force) checkOwnership(m, target);
    going.doing = "reading the plugin's jar";
    byte[][] jarBytes = new byte[1][];
    io("reading the plugin's jar", jar, () -> jarBytes[0] = Files.readAllBytes(jar), List.of("Check that the jar is readable; rebuild it if it is gone."));
    String jarName = jarName(jarBytes[0]);
    List<String> deps = new ArrayList<>();
    io("reading the Class-Path of", jar, () -> deps.addAll(classPath(jar)), List.of("Check that the jar is a valid jar; rebuild it."));
    for (String dep : deps) {
      Path from = jar.resolveSibling(dep).normalize();
      if (!Files.isRegularFile(from)) {
        throw new Failure("the jar names " + dep + " in its Class-Path, and there is no file at " + from, NOTHING,
            List.of("Rebuild or re-export the jar with its libraries beside it, then run this again."));
      }
    }
    List<String> entry = new ArrayList<>();
    if (jbang == null) {
      entry.add(Path.of(System.getProperty("java.home"), "bin", Platformish.windows() ? "java.exe" : "java").toString());
      entry.addAll(JVM_FLAGS);
      entry.add("-jar");
      entry.add(jarName);
    } else {
      entry.add(jbang.written());
      entry.addAll(jbangArgs(jarName));
    }
    Manifest installed = m.withEntryCommand(entry);
    // from the first file written until the manifest, vizi still reads the old manifest (or none)
    String partial = "The manifest was not replaced, so vizi still sees " + (before.contains(target.resolve(ManifestParser.FILE_NAME))
        ? "the previous install, unchanged" : "no plugin here") + "; files written so far in " + target + " are unused.";
    going.doing = "copying the plugin into place";
    // a new name, never over a jar an installed plugin may be running from
    Path jarTo = target.resolve(jarName);
    io("writing the plugin's jar", jarTo, () -> writeAtomically(jarTo, jarBytes[0]), before.isEmpty() ? NOTHING : partial, List.of());
    // a portable export names its libraries in Class-Path, relative to the jar: they go beside it
    for (String dep : deps) {
      Path from = jar.resolveSibling(dep).normalize();
      byte[][] b = new byte[1][];
      io("reading the library", from, () -> b[0] = Files.readAllBytes(from), partial, List.of());
      writeInside(target, dep, b[0], "library", partial);
    }
    for (var e : icons.entrySet()) writeInside(target, e.getKey(), e.getValue(), "pane icon", partial);
    byte[] markerBytes = markerText(m.id()).getBytes(StandardCharsets.UTF_8);
    io("writing the marker", marker, () -> writeAtomically(marker, markerBytes), partial, List.of());
    if (jbang != null) {
      // the warm-up: jbang resolves (or downloads) its JDK now, not at vizi's first launch, and
      // the launch line is proven to start this jar before the manifest names it
      going.doing = "warming up jbang";
      warmUp(jbang.run(), jarName, target, err, partial);
    }
    going.doing = "writing the manifest";
    String text = ManifestWriter.write(installed);
    try {
      ManifestParser.parse(text, target); // the host's reader, against the directory, icons and all
    } catch (vizi.plugin.javasdk.manifest.ManifestException e) {
      throw new Failure("the manifest would not read once installed: " + e.getMessage(), partial,
          List.of("This is a bug in the plugin's declarations or the Java SDK; run --manifest to see the declared manifest."));
    }
    Path manifestTo = target.resolve(ManifestParser.FILE_NAME);
    io("writing the manifest", manifestTo, () -> writeAtomically(manifestTo, text.getBytes(StandardCharsets.UTF_8)), partial, List.of());
    // the jars the old manifest named: deleted where the system allows (Windows keeps a running one)
    for (Path f : before) {
      String n = f.getFileName().toString();
      if ((n.equals("plugin.jar") || n.matches("plugin-[0-9a-f]{16}\\.jar")) && !n.equals(jarName)) {
        try {
          Files.deleteIfExists(f);
        } catch (IOException e) {
          err.println("warning: the replaced jar " + f + " was left in place (deleting it failed: " + cause(e) + "); it is unused and safe to remove");
        }
      }
    }
    out.println("INSTALLED: " + m.id() + " " + m.version() + " into " + target);
    out.println("  vizi launches it with: " + String.join(" ", entry));
    return 0;
  }

  /** jbang as the launch line names it ({@code written}), and as the installer runs it now. */
  record Jbang(String written, Path run) {}

  /** The arguments after {@code jbang} in a launch line that runs {@code jar}. */
  static List<String> jbangArgs(String jar) {
    List<String> a = new ArrayList<>(List.of("--java", JBANG_JAVA));
    // -R=VALUE, never -RVALUE: jbang 0.139 reads a value that starts with "--" as an option of its
    // own, and refuses "-R--flag" as a runtime option with no value; the = form works in 0.137 too
    for (String f : JVM_FLAGS) a.add("-R=" + f);
    a.add(jar);
    return a;
  }

  /** Where jbang's own installer puts it. */
  static Path jbangHome() {
    return Path.of(System.getProperty("user.home"), ".jbang", "bin", Platformish.windows() ? "jbang.cmd" : "jbang");
  }

  /**
   * jbang on {@code PATH} (written as its bare name, with its extension on Windows), else in
   * {@link #jbangHome()} (written by absolute path).
   */
  static Optional<Jbang> findJbang(Map<String, String> env) {
    String pathVar = env.getOrDefault("PATH", env.getOrDefault("Path", ""));
    List<String> names = Platformish.windows() ? List.of("jbang.cmd", "jbang.exe", "jbang.bat") : List.of("jbang");
    for (String dir : pathVar.split(java.io.File.pathSeparator)) {
      if (dir.isBlank()) continue;
      for (String n : names) {
        Path p = Path.of(dir).resolve(n);
        if (Files.isRegularFile(p) && (Platformish.windows() || Files.isExecutable(p))) return Optional.of(new Jbang(n, p.toAbsolutePath()));
      }
    }
    Path home = jbangHome();
    if (Files.isRegularFile(home) && (Platformish.windows() || Files.isExecutable(home))) {
      return Optional.of(new Jbang(home.toString(), home));
    }
    return Optional.empty();
  }

  /** How many of jbang's last output lines a failed warm-up shows. */
  static final int WARMUP_TAIL = 15;

  /** Runs {@code jbang … JAR --help} in the plugin directory; a failure shows jbang's last lines. */
  private static void warmUp(Path jbang, String jar, Path dir, PrintStream err, String state) throws Failure {
    List<String> cmd = new ArrayList<>();
    cmd.add(jbang.toString());
    cmd.addAll(jbangArgs(jar));
    cmd.add("--help");
    String line = String.join(" ", cmd);
    err.println("note: warming up: " + line + " (jbang may download a JDK " + JBANG_JAVA + " once)");
    Path log = null;
    try {
      log = Files.createTempFile("vizi-jbang-warmup", ".log");
      Process p = new ProcessBuilder(cmd).directory(dir.toFile()).redirectErrorStream(true)
          .redirectOutput(log.toFile()).start();
      boolean done;
      try {
        done = p.waitFor(JBANG_WARMUP.toMinutes(), java.util.concurrent.TimeUnit.MINUTES);
      } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
        p.destroyForcibly();
        throw new Failure("the install was interrupted during the jbang warm-up", state, List.of("Run it again."));
      }
      if (!done) {
        p.destroyForcibly();
        throw new Failure("the jbang warm-up (" + line + ") took longer than " + JBANG_WARMUP.toMinutes() + " minutes", state,
            withTail(List.of("A first JDK download can be slow: check the network, then run this again; or use --launcher=path."), log));
      }
      if (p.exitValue() != 0) {
        throw new Failure("the jbang warm-up exited " + p.exitValue() + ": " + line + " (in " + dir + ")", state,
            withTail(List.of("Run that line yourself in " + dir + " to see what jbang needs; or install with --launcher=path."), log));
      }
    } catch (IOException e) {
      throw new Failure(why(e, "starting jbang (" + line + ") from", dir), state, List.of("Check that " + jbang + " runs; or install with --launcher=path."));
    } finally {
      if (log != null) {
        try {
          Files.deleteIfExists(log);
        } catch (IOException ignored) {
          // a temp file
        }
      }
    }
  }

  /** {@code fix}, then jbang's last lines as it printed them. */
  private static List<String> withTail(List<String> fix, Path log) {
    List<String> out = new ArrayList<>(fix);
    try {
      List<String> lines = Files.readAllLines(log, StandardCharsets.UTF_8);
      if (lines.isEmpty()) return out;
      out.add("jbang's last lines:");
      for (String l : lines.subList(Math.max(0, lines.size() - WARMUP_TAIL), lines.size())) out.add("  | " + l);
    } catch (IOException | java.io.UncheckedIOException e) {
      // nothing more to show
    }
    return out;
  }

  /**
   * How the file on disk differs from the declared manifest, {@code entry.command} aside: {@code
   * --install} writes its own launch line, and the development file keeps a relative one. A
   * different {@code entry.startup_s} is drift like any other field.
   */
  static List<String> drift(Manifest declared, Manifest disk) {
    Manifest diskButEntry = disk.withEntryCommand(declared.entryCommand());
    return ManifestDiff.differences(declared, diskButEntry);
  }

  private static int serve(Plugin plugin, Map<String, String> env, PrintStream err) {
    String source = "code";
    Path onDisk = Path.of(ManifestParser.FILE_NAME).toAbsolutePath();
    if (Files.isRegularFile(onDisk)) {
      source = onDisk.toString();
      try {
        Manifest disk = ManifestParser.read(java.util.Objects.requireNonNull(onDisk.getParent())).manifest();
        List<String> diffs = drift(plugin.manifest(), disk);
        if (!diffs.isEmpty()) {
          err.println("[vizi-sdk] warning: " + onDisk + " differs from the manifest this plugin declares (entry aside);"
              + " vizi reads the file, so re-run --manifest or --install:");
          for (String d : diffs) err.println("[vizi-sdk]   " + d);
        }
      } catch (ManifestException e) {
        err.println("[vizi-sdk] warning: " + onDisk + " does not parse: " + e.getMessage());
      }
    }
    Serving s;
    try {
      s = Serving.start(plugin.runtime(), env, source);
    } catch (LaunchFailure e) {
      err.println("[vizi-sdk] " + e.getMessage());
      return 1;
    }
    String reason;
    try {
      reason = s.ended().get();
    } catch (InterruptedException e) {
      Thread.currentThread().interrupt();
      reason = "interrupted";
    } catch (ExecutionException e) {
      reason = String.valueOf(e.getCause());
    }
    s.close();
    boolean orderly = reason.startsWith("shutdown");
    if (!orderly) err.println("[vizi-sdk] the session ended: " + reason);
    return orderly ? 0 : 1;
  }

  private static List<String> classPath(Path jar) throws IOException {
    try (java.util.jar.JarFile j = new java.util.jar.JarFile(jar.toFile())) {
      java.util.jar.Manifest mf = j.getManifest();
      String cp = mf == null ? null : mf.getMainAttributes().getValue(java.util.jar.Attributes.Name.CLASS_PATH);
      if (cp == null || cp.isBlank()) return List.of();
      List<String> out = new ArrayList<>();
      for (String e : cp.trim().split("\\s+")) if (!e.isEmpty() && !e.contains(":")) out.add(e);
      return out;
    }
  }

  /** The jar holding the plugin's main class: the one to install. */
  private static Optional<Path> jarOf(Class<?> mainClass) {
    CodeSource cs = mainClass.getProtectionDomain().getCodeSource();
    if (cs == null) return Optional.empty();
    try {
      Path p = Path.of(cs.getLocation().toURI());
      return Files.isRegularFile(p) && p.toString().endsWith(".jar") ? Optional.of(p) : Optional.empty();
    } catch (URISyntaxException | IllegalArgumentException e) {
      return Optional.empty();
    }
  }

  /** Writes beside {@code to}, then renames over it: a reader sees the old file or the new, never half. */
  static void writeAtomically(Path to, byte[] bytes) throws IOException {
    Path abs = to.toAbsolutePath();
    Path dir = abs.getParent();
    if (dir != null) Files.createDirectories(dir);
    Path tmp = Files.createTempFile(dir == null ? Path.of(".") : dir, "." + abs.getFileName(), ".tmp");
    try {
      Files.write(tmp, bytes);
      // createTempFile makes 0600; a plugins folder is read by the host and perhaps by others
      if (tmp.getFileSystem().supportedFileAttributeViews().contains("posix")) {
        Files.setPosixFilePermissions(tmp, java.nio.file.attribute.PosixFilePermissions.fromString("rw-r--r--"));
      }
      Files.move(tmp, abs, StandardCopyOption.REPLACE_EXISTING, StandardCopyOption.ATOMIC_MOVE);
    } finally {
      Files.deleteIfExists(tmp);
    }
  }

  private static final class Platformish {
    static boolean windows() {
      return System.getProperty("os.name", "").toLowerCase(java.util.Locale.ROOT).startsWith("windows");
    }
  }
}
