package vizi.plugin.javasdk.manifest;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.DynamicTest;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.TestFactory;
import org.junit.jupiter.api.io.TempDir;

/**
 * Runs every fixture of {@code docs/spec/manifest_schema.md} §8, read from the spec at test time:
 * an accepted fixture parses and round-trips through the writer; a rejected one is refused with
 * the quoted text in its diagnostic.
 */
class ManifestFixturesTest {
  private static final Pattern MARKER =
      Pattern.compile("<!-- fixture: (accepted|rejected \"(.*)\") -->");

  /**
   * Rejected fixtures whose diagnostic here does not contain the spec's quoted text, with why.
   * Empty: every one matches.
   */
  private static final Map<Integer, String> KNOWN_DIFFERENT_DIAGNOSTICS = Map.of();

  @TempDir static Path pluginDir;

  record Fixture(int line, boolean accepted, String expected, String json) {
    String label() {
      return "line " + line + (accepted ? ": accepted" : ": rejected \"" + expected + "\"");
    }
  }

  static List<Fixture> fixtures() throws IOException {
    String repo = System.getenv("VIZI_SDK_REPO");
    assertTrue(repo != null && !repo.isEmpty(), "VIZI_SDK_REPO must name the repository root");
    Path spec = Path.of(repo, "docs", "spec", "manifest_schema.md");
    List<String> lines = Files.readAllLines(spec, StandardCharsets.UTF_8);
    List<Fixture> out = new ArrayList<>();
    for (int i = 0; i < lines.size(); i++) {
      Matcher m = MARKER.matcher(lines.get(i).strip());
      if (!m.matches()) continue;
      int markerLine = i + 1;
      int j = i + 1;
      while (j < lines.size() && lines.get(j).isBlank()) j++;
      assertEquals("```json", lines.get(j).strip(), "fixture at line " + markerLine + " has no json block");
      StringBuilder json = new StringBuilder();
      for (j = j + 1; !lines.get(j).strip().equals("```"); j++) json.append(lines.get(j)).append('\n');
      boolean accepted = m.group(1).equals("accepted");
      String expected = accepted ? "" : m.group(2);
      out.add(new Fixture(markerLine, accepted, expected, json.toString()));
      i = j;
    }
    return out;
  }

  /**
   * The fixture directory holds {@code icons/tool.svg} and {@code icons/go.png} (§8.1), and the
   * files the icon rejections are about: one too big, and two whose bytes are not what their
   * suffix claims. A {@code .gif} exists too, so its rejection is about the suffix, not absence.
   */
  @BeforeAll
  static void icons() throws IOException {
    Path icons = Files.createDirectories(pluginDir.resolve("icons"));
    byte[] png = pngBytes();
    Files.writeString(
        icons.resolve("tool.svg"),
        "<?xml version=\"1.0\"?>\n<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 16 16\">"
            + "<rect width=\"16\" height=\"16\"/></svg>\n");
    Files.write(icons.resolve("go.png"), png);
    byte[] big = Arrays.copyOf(png, ManifestParser.MAX_ICON_BYTES + 1);
    Files.write(icons.resolve("big.png"), big);
    Files.write(icons.resolve("fake.png"), "GIF89a not a png".getBytes(StandardCharsets.US_ASCII));
    Files.write(icons.resolve("fake.svg"), png);
    Files.write(icons.resolve("tool.gif"), "GIF89a".getBytes(StandardCharsets.US_ASCII));
  }

  /** A PNG signature followed by an IHDR chunk: enough for a first-bytes check. */
  static byte[] pngBytes() {
    return new byte[] {
      (byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n',
      0, 0, 0, 13, 'I', 'H', 'D', 'R', 0, 0, 0, 1, 0, 0, 0, 1, 8, 6, 0, 0, 0,
      0x1f, 0x15, (byte) 0xc4, (byte) 0x89
    };
  }

  /**
   * The accepted and rejected fixture counts in docs/spec/manifest_schema.md §8. A plain {@code
   * grep -c} of the markers finds one more of each: §8's own description of the two markers,
   * which is not a fixture.
   */
  @Test
  void theSpecHasTheExpectedFixtureCounts() throws IOException {
    List<Fixture> all = fixtures();
    assertEquals(25, all.stream().filter(Fixture::accepted).count(), "accepted fixtures");
    assertEquals(159, all.stream().filter(f -> !f.accepted()).count(), "rejected fixtures");
  }

  @TestFactory
  Stream<DynamicTest> everyFixture() throws IOException {
    return fixtures().stream().map(f -> DynamicTest.dynamicTest(f.label(), () -> run(f)));
  }

  private static void run(Fixture f) throws ManifestException {
    if (f.accepted()) {
      ParseResult r = ManifestParser.parse(f.json(), pluginDir);
      String written = ManifestWriter.write(r.manifest());
      ParseResult again = ManifestParser.parse(written, pluginDir);
      assertEquals(r.manifest(), again.manifest(), "write → parse round trip");
      assertEquals(written, ManifestWriter.write(again.manifest()), "the written form is stable");
      assertEquals(List.of(), ManifestDiff.differences(r.manifest(), again.manifest()));
      assertEquals(List.of(), again.warnings(), "the canonical form draws no warnings");
      return;
    }
    ManifestException e =
        assertThrows(ManifestException.class, () -> ManifestParser.parse(f.json(), pluginDir));
    String msg = String.valueOf(e.getMessage());
    if (KNOWN_DIFFERENT_DIAGNOSTICS.containsKey(f.line())) return;
    assertTrue(
        msg.contains(f.expected()),
        () -> "diagnostic does not contain \"" + f.expected() + "\": " + msg);
    // The engines sentence is the one sentence every implementation says, without the file.
    if (!(e instanceof EnginesIncompatibleException)) {
      assertTrue(msg.startsWith(pluginDir.resolve(ManifestParser.FILE_NAME).toString()), msg);
    }
  }

  @Test
  void theUnknownPermissionIsWarnedAbout() throws Exception {
    Fixture future =
        fixtures().stream().filter(f -> f.json().contains("org.example.future")).findFirst().orElseThrow();
    ParseResult r = ManifestParser.parse(future.json(), pluginDir);
    assertEquals(List.of(Permission.DATA_ACCESS), r.manifest().permissions());
    assertEquals(1, r.warnings().size(), r.warnings().toString());
    assertTrue(r.warnings().get(0).contains("'teleport'"), r.warnings().toString());
  }
}
