package vizi.plugin.javasdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import java.util.regex.Matcher;
import java.util.regex.Pattern;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * The packages' dependency order: nativeio → doorbell → memory → control → the root
 * API → testkit, with the manifest model beside nativeio. A file may name only its own layer and
 * those below it, in imports and in fully-qualified names alike.
 */
class LayeringTest {

  private static final Map<String, Integer> RANK = Map.of(
      "nativeio", 0, "manifest", 0, "doorbell", 1, "memory", 2, "control", 3, "", 4, "testkit", 5);

  private static final Pattern NAMED = Pattern.compile("vizi\\.plugin\\.javasdk\\.([A-Za-z_]\\w*)");

  private static int rank(String layer) {
    Integer r = RANK.get(layer);
    if (r == null) throw new AssertionError("unknown layer " + layer);
    return r;
  }

  @Test
  void noFileNamesALayerAboveItsOwn() throws IOException {
    String repo = System.getenv("VIZI_SDK_REPO");
    assertTrue(repo != null && !repo.isEmpty(), "VIZI_SDK_REPO must name the repository root");
    Path src = Path.of(repo, "polyglot", "java-sdk", "sdk", "src", "vizi", "plugin", "javasdk");
    List<String> breaches = new ArrayList<>();
    int files = 0;
    try (Stream<Path> all = Files.walk(src)) {
      for (Path f : (Iterable<Path>) all.filter(p -> p.toString().endsWith(".java"))::iterator) {
        files++;
        Path rel = src.relativize(f);
        String own = rel.getNameCount() == 1 ? "" : rel.getName(0).toString();
        int mine = rank(own);
        Matcher m = NAMED.matcher(Files.readString(f));
        while (m.find()) {
          String first = m.group(1);
          String layer = Character.isUpperCase(first.charAt(0)) ? "" : first;
          if (rank(layer) > mine) breaches.add(rel + " names " + m.group());
        }
      }
    }
    assertTrue(files > 50, "found only " + files + " source files under " + src);
    assertEquals(List.of(), breaches);
  }
}
