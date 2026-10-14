package vizi.plugin.javasdk;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.io.ByteArrayOutputStream;
import java.io.PrintStream;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;
import vizi.plugin.javasdk.manifest.CommandDecl;

/**
 * A failed {@code --install} says so. Every case runs through {@code
 * PluginMain.run}, the function a plugin's {@code main} calls, with its streams and exit code, and
 * checks the headline, the cause and the fix line, and that no stack trace is printed.
 */
class InstallMessagesTest {
  @TempDir Path tmp;

  record Ran(int exit, String out, String err) {
    String all() {
      return out + err;
    }
  }

  static Plugin plugin() {
    var b = Plugin.builder("org.test.inst", "Inst", "0.3.1");
    b.command(CommandDecl.of("c", "C"), ctx -> "");
    return b.build();
  }

  Ran run(PluginMain.Running running, Map<String, String> env, String... args) {
    var out = new ByteArrayOutputStream();
    var err = new ByteArrayOutputStream();
    int exit;
    try (var o = new PrintStream(out, true, StandardCharsets.UTF_8); var e = new PrintStream(err, true, StandardCharsets.UTF_8)) {
      exit = PluginMain.run(plugin(), args, env, o, e, InstallMessagesTest.class, running);
    }
    Ran r = new Ran(exit, out.toString(StandardCharsets.UTF_8), err.toString(StandardCharsets.UTF_8));
    assertFalse(r.all().contains("\tat ") || r.all().contains("Exception:") && !r.all().contains("unexpected"), "no stack trace: " + r);
    return r;
  }

  PluginMain.Running fromJar() throws Exception {
    Path jar = tmp.resolve("build/plugin.jar");
    Files.createDirectories(jar.getParent());
    var mf = new java.util.jar.Manifest();
    mf.getMainAttributes().put(java.util.jar.Attributes.Name.MANIFEST_VERSION, "1.0");
    try (var j = new java.util.jar.JarOutputStream(Files.newOutputStream(jar), mf)) {
      j.putNextEntry(new java.util.jar.JarEntry("x.txt"));
      j.write(1);
    }
    return new PluginMain.Running(Optional.of(jar), List.of());
  }

  static void headline(Ran r, String start) {
    assertTrue(r.err.startsWith(start), "the first line is the headline: " + r.err);
    assertTrue(r.err.contains("\n  Why: "), r.err);
    assertTrue(r.err.contains("\n  To fix:\n    "), r.err);
  }

  // ---- usage mistakes ----

  @Test
  void anotherSdksFlagIsNamedAsTypedAndTheJavaSpellingGiven() throws Exception {
    Path plugins = tmp.resolve("plugins");
    Ran r = run(fromJar(), Map.of(), "--install=" + plugins, "--runner=jbang");
    assertEquals(2, r.exit);
    headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed into " + plugins.toAbsolutePath().resolve("org.test.inst") + "\n");
    assertTrue(r.err.contains("unknown argument '--runner=jbang'"), r.err);
    assertTrue(r.err.contains("--install --launcher=jbang"), r.err);
    assertTrue(r.err.contains("Run with --help"), r.err);
    assertFalse(Files.exists(plugins), "nothing written");
  }

  @Test
  void otherUsageMistakes() throws Exception {
    Ran unknown = run(fromJar(), Map.of(), "--instal");
    assertEquals(2, unknown.exit);
    headline(unknown, "FAILED: org.test.inst 0.3.1 did nothing: the command line was not understood\n");
    assertTrue(unknown.err.contains("unknown argument '--instal'"), unknown.err);
    assertTrue(unknown.err.contains("Did you mean --install?"), unknown.err);
    assertEquals("--manifest=out", PluginMain.nearest("--manfest=out"));
    assertNull(PluginMain.nearest("--frobnicate"));
    Ran two = run(fromJar(), Map.of(), "--manifest=" + tmp, "--install=" + tmp);
    assertTrue(two.err.contains("two actions were given"), two.err);
    Ran alone = run(fromJar(), Map.of(), "--launcher=jbang");
    assertTrue(alone.err.contains("Add --install: --install --launcher=jbang"), alone.err);
    Ran badLauncher = run(fromJar(), Map.of(), "--install=" + tmp, "--launcher=docker");
    assertTrue(badLauncher.err.contains("unknown launcher 'docker'"), badLauncher.err);
    assertTrue(badLauncher.err.contains("--launcher=path (the default) or --launcher=jbang"), badLauncher.err);
    assertEquals(0, run(fromJar(), Map.of(), "--help").exit);
  }

  // ---- the SDK's own refusals ----

  @Test
  void notRunningFromAJar() {
    Ran r = run(new PluginMain.Running(Optional.empty(), List.of()), Map.of(), "--install=" + tmp);
    assertEquals(1, r.exit);
    headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed into " + tmp.toAbsolutePath().resolve("org.test.inst"));
    assertFalse(r.err.contains("<repo>") || r.err.contains("this repository"), "true for any plugin: " + r.err);
    assertTrue(r.err.contains("-jar YOUR-PLUGIN.jar --install"), r.err);
    // a mill build is recognised by its class directory, and only then named
    Ran mill = run(new PluginMain.Running(Optional.empty(), List.of(), List.of("/p/out/compile.dest/classes", "/m2/x.jar")), Map.of(),
        "--install=" + tmp);
    assertTrue(mill.err.contains("    mill assembly\n"), mill.err);
    assertTrue(mill.err.contains("-jar out/assembly.dest/out.jar --install"), mill.err);
  }

  // ---- usage mistakes and refusals ----

  @Test
  void anEmptyInstallValueIsAUsageMistakeNotAnInstallHere() throws Exception {
    Path here = Path.of("").toAbsolutePath();
    Ran r = run(fromJar(), Map.of("VIZI_PLUGINS_DIR", tmp.resolve("p").toString()), "--install=");
    assertEquals(2, r.exit);
    headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed\n");
    assertTrue(r.err.contains("--install= names no directory"), r.err);
    assertTrue(r.err.contains("--install=DIR"), r.err);
    assertFalse(Files.exists(here.resolve("org.test.inst")), "nothing installed here");
    Ran m = run(fromJar(), Map.of(), "--manifest=");
    assertEquals(2, m.exit);
    assertTrue(m.err.contains("--manifest= names no directory"), m.err);
    // an empty separate argument (an unset "$DIR") is refused the same way, never advised as a folder
    Ran sep = run(fromJar(), Map.of("VIZI_PLUGINS_DIR", tmp.resolve("p").toString()), "--install", "");
    assertEquals(2, sep.exit);
    headline(sep, "INSTALL FAILED: org.test.inst 0.3.1 was not installed\n");
    assertTrue(sep.err.contains("the folder given is empty"), sep.err);
    assertTrue(sep.err.contains("Give the plugins directory: --install=DIR"), sep.err);
    assertFalse(sep.err.contains("one argument"), sep.err);
    assertTrue(run(fromJar(), Map.of(), "--manifest", "").err.contains("the folder given is empty"));
  }

  @Test
  void aDirectoryAfterASpaceIsNamedInTheHeadlineAndTheFix() throws Exception {
    Path dir = tmp.resolve("mine");
    Ran r = run(fromJar(), Map.of(), "--install", dir.toString());
    assertEquals(2, r.exit);
    headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed into " + dir.toAbsolutePath().resolve("org.test.inst") + "\n");
    assertTrue(r.err.contains("Write it as one argument: --install=" + dir), r.err);
    Ran m = run(fromJar(), Map.of(), "--manifest", dir.toString());
    assertTrue(m.err.contains("Write it as one argument: --manifest=" + dir), m.err);
  }

  @Test
  void aRunnerIsMappedOnlyWhereThereIsALauncher() throws Exception {
    assertTrue(run(fromJar(), Map.of(), "--install=" + tmp, "--runner=java").err.contains("--install --launcher=path"));
    assertTrue(run(fromJar(), Map.of(), "--install=" + tmp, "--runner=jbang").err.contains("--install --launcher=jbang"));
    Ran other = run(fromJar(), Map.of(), "--install=" + tmp, "--runner=scala-cli");
    assertFalse(other.err.contains("--launcher=scala-cli"), other.err);
    assertTrue(other.err.contains("--install --launcher=path") && other.err.contains("--install --launcher=jbang"), other.err);
  }

  @Test
  void thePermissionsFixNamesTheDirectoryThatRefused() throws Exception {
    assumeTrue(tmp.getFileSystem().supportedFileAttributeViews().contains("posix"), "posix permissions");
    Path ro = tmp.resolve("ro");
    Files.createDirectories(ro);
    Files.setPosixFilePermissions(ro, java.nio.file.attribute.PosixFilePermissions.fromString("r-xr-xr-x"));
    try {
      assumeTrue(!Files.isWritable(ro), "not running as root");
      Ran r = run(fromJar(), Map.of(), "--install=" + ro.resolve("sub/deeper"));
      assertTrue(r.err.contains("or fix the permissions of " + ro.toAbsolutePath() + "."), r.err);
    } finally {
      Files.setPosixFilePermissions(ro, java.nio.file.attribute.PosixFilePermissions.fromString("rwxr-xr-x"));
    }
  }

  @Test
  void theJbangScriptIsNamedFromItsCacheDirectory() {
    assertEquals(Optional.of("VoxelProbe.java"),
        PluginMain.jbangScript(Path.of("/h/.jbang/cache/jars/VoxelProbe.java.5727dd12485aae800ab76bbaf9e11297014ff495e1941154368d1e3b42e1099f/VoxelProbe.jar")));
    assertEquals(Optional.empty(), PluginMain.jbangScript(Path.of("/h/build/plugin.jar")));
  }

  /** The jbang single-file case: a jar without the SDK would install and then fail to start. */
  @Test
  void aJarThatDoesNotCarryItsLibrariesIsRefused() throws Exception {
    Path jar = tmp.resolve(".jbang/cache/jars/VoxelProbe.jar");
    Files.createDirectories(jar.getParent());
    Files.write(jar, new byte[] {1});
    Ran r = run(new PluginMain.Running(Optional.of(jar), List.of("/m2/vizi-sdk-java-0.1.0.jar", "/m2/grpc.jar")), Map.of(),
        "--install=" + tmp.resolve("plugins"));
    assertEquals(1, r.exit);
    headline(r, "INSTALL FAILED:");
    assertTrue(r.err.contains("holds the plugin but not everything it runs on"), r.err);
    assertTrue(r.err.contains("/m2/vizi-sdk-java-0.1.0.jar and 1 more classpath entry"), r.err);
    assertTrue(r.err.contains("jbang export portable --force -O build/plugin.jar InstallMessagesTest.java"), r.err);
    assertFalse(Files.exists(tmp.resolve("plugins")), "nothing written");
  }

  @Test
  void aDirectoryNotThisInstallersIsRefusedAndLeftAsItWas() throws Exception {
    Path plugins = tmp.resolve("plugins");
    Path dir = plugins.resolve("org.test.inst");
    Files.createDirectories(dir);
    Files.writeString(dir.resolve("notes.txt"), "mine");
    Ran r = run(fromJar(), Map.of(), "--install=" + plugins);
    assertEquals(1, r.exit);
    headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed into " + dir.toAbsolutePath());
    assertTrue(r.err.contains("this installer did not write them"), r.err);
    assertTrue(r.err.contains("--install --force"), r.err);
    try (var l = Files.list(dir)) {
      assertEquals(List.of(dir.resolve("notes.txt")), l.toList(), "nothing written, no lock left");
    }
    Files.writeString(dir.resolve(PluginMain.MARKER), PluginMain.markerText("org.test.other"));
    Ran other = run(fromJar(), Map.of(), "--install=" + plugins);
    assertTrue(other.err.contains("for another plugin or SDK"), other.err);
    assertTrue(other.err.contains("--install --force"), other.err);
  }

  @Test
  void aFileInTheWay() throws Exception {
    Path plugins = tmp.resolve("plugins");
    Files.createDirectories(plugins);
    Files.writeString(plugins.resolve("org.test.inst"), "x");
    Ran r = run(fromJar(), Map.of(), "--install=" + plugins);
    headline(r, "INSTALL FAILED:");
    assertTrue(r.err.contains("a file, not a directory, is in the way at " + plugins.toAbsolutePath().resolve("org.test.inst")), r.err);
  }

  // ---- the environment ----

  @Test
  void anUnwritableDirectory() throws Exception {
    assumeTrue(tmp.getFileSystem().supportedFileAttributeViews().contains("posix"), "posix permissions");
    Path plugins = tmp.resolve("locked");
    Files.createDirectories(plugins);
    Files.setPosixFilePermissions(plugins, java.nio.file.attribute.PosixFilePermissions.fromString("r-xr-xr-x"));
    try {
      assumeTrue(!Files.isWritable(plugins), "not running as root");
      Ran r = run(fromJar(), Map.of(), "--install=" + plugins);
      assertEquals(1, r.exit);
      Path target = plugins.toAbsolutePath().resolve("org.test.inst");
      headline(r, "INSTALL FAILED: org.test.inst 0.3.1 was not installed into " + target + "\n");
      assertTrue(r.err.contains("  Why: permission denied creating the directory " + target + "\n"), r.err);
      assertTrue(r.err.contains("  Nothing was written.\n"), r.err);
      assertTrue(r.err.contains("--install=DIR (or set VIZI_PLUGINS_DIR=DIR)"), r.err);
    } finally {
      Files.setPosixFilePermissions(plugins, java.nio.file.attribute.PosixFilePermissions.fromString("rwxr-xr-x"));
    }
  }

  @Test
  void aHeldLock() throws Exception {
    Path plugins = tmp.resolve("plugins");
    assertEquals(0, run(fromJar(), Map.of(), "--install=" + plugins).exit);
    Path lock = plugins.toAbsolutePath().resolve("org.test.inst").resolve(PluginMain.LOCK);
    Files.createFile(lock);
    Ran r = run(fromJar(), Map.of(), "--install=" + plugins);
    assertEquals(1, r.exit);
    headline(r, "INSTALL FAILED:");
    assertTrue(r.err.contains("another install holds the lock " + lock), r.err);
    assertTrue(r.err.contains("the previous install, if any, is unchanged"), r.err);
    assertTrue(r.err.contains("rm '" + lock + "'") || r.err.contains("del \"" + lock + "\""), r.err);
    assertTrue(Files.exists(lock), "the lock is the person's to remove");
  }

  @Test
  void successIsOneClearLine() throws Exception {
    Path plugins = tmp.resolve("plugins");
    Ran r = run(fromJar(), Map.of(), "--install=" + plugins);
    assertEquals(0, r.exit);
    Path target = plugins.toAbsolutePath().resolve("org.test.inst");
    assertTrue(r.out.startsWith("INSTALLED: org.test.inst 0.3.1 into " + target + "\n"), r.out);
    assertTrue(r.out.contains("  vizi launches it with: "), r.out);
    assertEquals("", r.err);
    Ran m = run(fromJar(), Map.of(), "--manifest=" + tmp.resolve("m"));
    assertEquals("WROTE: " + tmp.resolve("m").toAbsolutePath().resolve("vizi-plugin.json") + "\n", m.out);
  }

  @Test
  void aManifestIntoAFile() throws Exception {
    Path file = Files.writeString(tmp.resolve("f"), "x");
    Ran r = run(fromJar(), Map.of(), "--manifest=" + file);
    assertEquals(1, r.exit);
    headline(r, "MANIFEST NOT WRITTEN: org.test.inst 0.3.1's vizi-plugin.json was not written into " + file.toAbsolutePath());
    assertTrue(r.err.contains("is a file, and --manifest=DIR names a directory"), r.err);
  }

  @Test
  void anIoFailureNamesTheOperationNotJustAPath() {
    Path p = Path.of("/opt/vizi/plugins/x");
    assertEquals("permission denied creating the directory /opt/vizi/plugins/x",
        PluginMain.why(new java.nio.file.AccessDeniedException("/opt/vizi/plugins/x"), "creating the directory", p));
    assertEquals("writing the manifest /opt/vizi/plugins/x failed: no space left on device",
        PluginMain.why(new java.nio.file.FileSystemException("/tmp/.x123.tmp", null, "No space left on device"), "writing the manifest", p));
    assertEquals("IOException", PluginMain.cause(new java.io.IOException()), "never null");
  }
}
