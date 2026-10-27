package vizi.plugin.javasdk.manifest;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.jspecify.annotations.Nullable;

/**
 * What differs between two manifests, for a person: used to warn that a {@code vizi-plugin.json}
 * on disk is stale against the manifest the code declares.
 *
 * <p>Both are compared in their canonical written form ({@link ManifestWriter}), so a default
 * spelled out on one side and omitted on the other is not a difference. Lists of entries with an
 * {@code id} (or a stage param's {@code key}) are matched by it, so a message names the entry
 * rather than a shifting index; an order change is reported on its own, since order is meaningful
 * for tool actions (their 1-based index is on the wire).
 */
public final class ManifestDiff {
  private ManifestDiff() {}

  /** Path-qualified differences, empty when the manifests are equal. */
  public static List<String> differences(Manifest declared, Manifest onDisk) {
    List<String> out = new ArrayList<>();
    JsonElement a = JsonParser.parseString(ManifestWriter.write(declared));
    JsonElement b = JsonParser.parseString(ManifestWriter.write(onDisk));
    diff("", a, b, out);
    if (out.isEmpty() && !declared.equals(onDisk)) {
      out.add("the manifests differ in a way their written form does not show");
    }
    return List.copyOf(out);
  }

  private static void diff(String path, JsonElement a, JsonElement b, List<String> out) {
    if (a.equals(b)) return;
    if (a.isJsonObject() && b.isJsonObject()) {
      JsonObject x = a.getAsJsonObject();
      JsonObject y = b.getAsJsonObject();
      Set<String> keys = new LinkedHashSet<>(x.keySet());
      keys.addAll(y.keySet());
      for (String k : keys) {
        String p = path.isEmpty() ? k : path + "." + k;
        // The writer omits an empty list, so a list on one side only is compared with [].
        if (!y.has(k) && x.get(k).isJsonArray()) diff(p, x.get(k), new JsonArray(), out);
        else if (!x.has(k) && y.get(k).isJsonArray()) diff(p, new JsonArray(), y.get(k), out);
        else if (!y.has(k)) out.add(p + ": declared " + x.get(k) + ", absent on disk");
        else if (!x.has(k)) out.add(p + ": absent in the declaration, on disk " + y.get(k));
        else diff(p, x.get(k), y.get(k), out);
      }
      return;
    }
    if (a.isJsonArray() && b.isJsonArray()) {
      JsonArray xa = a.getAsJsonArray();
      JsonArray ya = b.getAsJsonArray();
      Map<String, JsonElement> x = xa.isEmpty() ? Map.of() : keyed(xa);
      Map<String, JsonElement> y = ya.isEmpty() ? Map.of() : keyed(ya);
      if (x != null && y != null) {
        keyedDiff(path, x, y, out);
        return;
      }
      if (xa.size() != ya.size() || !containers(xa)) {
        out.add(path + ": declared " + a + ", on disk " + b);
        return;
      }
      for (int i = 0; i < xa.size(); i++) diff(path + "[" + i + "]", xa.get(i), ya.get(i), out);
      return;
    }
    out.add(path + ": declared " + a + ", on disk " + b);
  }

  private static void keyedDiff(
      String path, Map<String, JsonElement> x, Map<String, JsonElement> y, List<String> out) {
    List<String> commonX = new ArrayList<>();
    for (String k : x.keySet()) if (y.containsKey(k)) commonX.add(k);
    List<String> commonY = new ArrayList<>();
    for (String k : y.keySet()) if (x.containsKey(k)) commonY.add(k);
    if (!commonX.equals(commonY)) {
      out.add(path + ": order differs: declared " + commonX + ", on disk " + commonY);
    }
    for (Map.Entry<String, JsonElement> e : x.entrySet()) {
      String p = path + "[" + e.getKey() + "]";
      JsonElement other = y.get(e.getKey());
      if (other == null) out.add(p + ": declared, absent on disk");
      else diff(p, e.getValue(), other, out);
    }
    for (String k : y.keySet()) {
      if (!x.containsKey(k)) out.add(path + "[" + k + "]: on disk, absent in the declaration");
    }
  }

  /** Entries by {@code id} (or {@code key}), if every element has a distinct one; else null. */
  private static @Nullable Map<String, JsonElement> keyed(JsonArray a) {
    if (a.isEmpty()) return null;
    Map<String, JsonElement> out = new LinkedHashMap<>();
    for (JsonElement e : a) {
      if (!e.isJsonObject()) return null;
      JsonObject o = e.getAsJsonObject();
      String field = o.has("id") ? "id" : o.has("key") && o.has("type") ? "key" : null;
      if (field == null || !o.get(field).isJsonPrimitive()) return null;
      String k = "'" + o.get(field).getAsString() + "'";
      if (out.put(k, e) != null) return null;
    }
    return out;
  }

  private static boolean containers(JsonArray a) {
    for (JsonElement e : a) if (!e.isJsonObject() && !e.isJsonArray()) return false;
    return true;
  }
}
