package vizi.plugin.javasdk;

import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.StringReader;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import org.jspecify.annotations.Nullable;

/**
 * RFC 8259 JSON as plain Java values: an object is an unmodifiable {@code Map<String, Object>} in
 * document order (a repeated key: the later value wins), an array an unmodifiable {@code List},
 * then {@code String}, {@code Boolean}, {@code null}, and a number as a {@code Long} when its text
 * is an integer that fits, else a {@code Double}.
 */
final class JsonValues {
  private JsonValues() {}

  /** The object {@code text} holds, or empty for anything else, malformed text included. */
  static Optional<Map<String, @Nullable Object>> object(String text) {
    if (text.isEmpty()) return Optional.empty();
    JsonReader r = new JsonReader(new StringReader(text));
    r.setStrictness(Strictness.STRICT);
    try {
      if (r.peek() != JsonToken.BEGIN_OBJECT) return Optional.empty();
      Map<String, @Nullable Object> m = readObject(r);
      if (r.peek() != JsonToken.END_DOCUMENT) return Optional.empty();
      return Optional.of(m);
    } catch (IOException | IllegalStateException | NumberFormatException e) {
      return Optional.empty();
    }
  }

  private static Map<String, @Nullable Object> readObject(JsonReader r) throws IOException {
    Map<String, @Nullable Object> m = new LinkedHashMap<>();
    r.beginObject();
    while (r.hasNext()) {
      String k = r.nextName();
      m.remove(k);
      m.put(k, read(r));
    }
    r.endObject();
    return Collections.unmodifiableMap(m);
  }

  private static @Nullable Object read(JsonReader r) throws IOException {
    return switch (r.peek()) {
      case BEGIN_OBJECT -> readObject(r);
      case BEGIN_ARRAY -> {
        List<@Nullable Object> l = new ArrayList<>();
        r.beginArray();
        while (r.hasNext()) l.add(read(r));
        r.endArray();
        yield Collections.unmodifiableList(l);
      }
      case STRING -> r.nextString();
      case BOOLEAN -> r.nextBoolean();
      case NULL -> {
        r.nextNull();
        yield null;
      }
      case NUMBER -> number(r.nextString());
      default -> throw new IllegalStateException("unexpected " + r.peek() + " at " + r.getPath());
    };
  }

  private static Number number(String text) {
    if (text.chars().allMatch(ch -> ch == '-' || (ch >= '0' && ch <= '9'))) {
      try {
        return Long.parseLong(text);
      } catch (NumberFormatException tooBig) {
        // falls through to a double
      }
    }
    return Double.parseDouble(text);
  }
}
