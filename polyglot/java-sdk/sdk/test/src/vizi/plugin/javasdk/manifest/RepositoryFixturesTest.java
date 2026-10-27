package vizi.plugin.javasdk.manifest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.stream.Stream;
import java.util.stream.StreamSupport;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import vizi.plugin.javasdk.Plugin;

/**
 * The repository's manifest fixtures ({@code polyglot/manifest_fixtures/*.json}), which {@code
 * scripts/check_manifest_fixtures.py} also runs through the reference parser.
 */
class RepositoryFixturesTest {
  static List<Path> files() throws IOException {
    String repo = System.getenv("VIZI_SDK_REPO");
    assertTrue(repo != null && !repo.isEmpty(), "VIZI_SDK_REPO must name the repository root");
    try (Stream<Path> s = Files.list(Path.of(repo, "polyglot", "manifest_fixtures"))) {
      return s.filter(p -> p.toString().endsWith(".json")).sorted().toList();
    }
  }

  @TestFactory
  Stream<DynamicTest> everyCase() throws IOException {
    List<Path> files = files();
    assertTrue(files.size() >= 2, files.toString());
    List<DynamicTest> tests = new ArrayList<>();
    for (Path file : files) {
      JsonObject doc =
          JsonParser.parseString(Files.readString(file, StandardCharsets.UTF_8)).getAsJsonObject();
      assertEquals(
          EngineRequirement.PROTOCOL_MAJOR + "." + EngineRequirement.PROTOCOL_MINOR,
          doc.get("protocol").getAsString(),
          file.toString());
      assertTrue(doc.getAsJsonArray("cases").size() >= 4);
      StreamSupport.stream(doc.getAsJsonArray("cases").spliterator(), false)
          .map(JsonElement::getAsJsonObject)
          .map(
              c ->
                  DynamicTest.dynamicTest(
                      file.getFileName() + ": " + c.get("name").getAsString(), () -> run(c)))
          .forEach(tests::add);
    }
    return tests.stream();
  }

  private static void run(JsonObject c) throws ManifestException {
    String json = c.get("json").toString();
    JsonObject expected = c.getAsJsonObject("expected");
    String kind = expected.get("kind").getAsString();
    if (kind.equals("accepted")) {
      ManifestParser.parseDetached(json);
      return;
    }
    String message = expected.get("message").getAsString();
    ManifestException e =
        assertThrows(ManifestException.class, () -> ManifestParser.parseDetached(json));
    switch (kind) {
      case "engines_incompatible" -> {
        assertInstanceOf(EnginesIncompatibleException.class, e);
        assertEquals(message, e.getMessage());
      }
      case "invalid" -> {
        assertFalse(e instanceof EnginesIncompatibleException, e.getMessage());
        assertTrue(e.getMessage().contains(message), e.getMessage());
      }
      default -> throw new AssertionError("unknown expected kind " + kind);
    }
  }

  @Test
  void theExceptionCarriesWhatItNames() {
    EnginesIncompatibleException e =
        assertThrows(
            EnginesIncompatibleException.class,
            () ->
                ManifestParser.parseDetached(
                    "{\"manifest_version\": 3, \"id\": \"org.example.x\", \"name\": \"X\","
                        + " \"version\": \"2\", \"engines\": {\"vizi\": \"1.4\"}}"));
    assertEquals("org.example.x", e.pluginId());
    assertEquals("X", e.name());
    assertEquals("2", e.version());
    assertEquals("1.4", e.engines());
    assertEquals("X 2 needs vizi protocol 1.4; this vizi speaks 1.0", e.getMessage());
  }

  @Test
  void aPluginStatesTheSdkReleaseAndMinVizi() {
    Manifest m =
        Plugin.builder("org.example.top", "Top", "1").entry("./run").minVizi("0.9.7").build().manifest();
    assertEquals(java.util.Optional.of(vizi.plugin.javasdk.Sdk.RELEASE), m.sdkVersion());
    assertEquals(java.util.Optional.of("0.9.7"), m.minVizi());
    // unset, it is the oldest vizi release that speaks protocol 1.0, and the written manifest says so
    Manifest unset = Plugin.builder("org.example.top", "Top", "1").entry("./run").build().manifest();
    assertEquals(java.util.Optional.of("1.0.0"), unset.minVizi());
    assertTrue(ManifestWriter.write(unset).contains("\"min_vizi\": \"1.0.0\""), ManifestWriter.write(unset));
    assertTrue(vizi.plugin.javasdk.Sdk.RELEASE.matches("[0-9]+\\.[0-9]+((a|b|rc)[0-9]+|\\.[0-9]+)?"));
    String expected =
        vizi.plugin.javasdk.Sdk.VERSION
            .replaceFirst("^(\\d+\\.\\d+)\\.0-", "$1-")
            .replaceFirst("-alpha\\.", "a")
            .replaceFirst("-beta\\.", "b")
            .replaceFirst("-rc\\.", "rc");
    assertEquals(expected, vizi.plugin.javasdk.Sdk.RELEASE);
    IllegalStateException e =
        assertThrows(
            IllegalStateException.class,
            () -> Plugin.builder("Top", "Top", "1").entry("./run").build());
    assertTrue(e.getMessage().contains("id 'Top' is not a plugin id"), e.getMessage());
  }

  @Test
  void aPluginDeclaringANewerProtocolIsRefusedWithTheSentence() {
    Plugin.Builder b =
        Plugin.builder("org.example.newer", "Newer", "1.0.0").entry("./run").engine("1.1");
    IllegalStateException e = assertThrows(IllegalStateException.class, b::build);
    assertEquals("Newer 1.0.0 needs vizi protocol 1.1; this vizi speaks 1.0", e.getMessage());
  }
}
