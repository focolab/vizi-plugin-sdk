package vizi.plugin.javasdk.manifest;

import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonPrimitive;
import com.google.gson.Strictness;
import com.google.gson.stream.JsonReader;
import com.google.gson.stream.JsonToken;
import java.io.IOException;
import java.io.StringReader;
import java.math.BigDecimal;
import java.math.BigInteger;
import java.util.List;
import org.jspecify.annotations.Nullable;

/**
 * Reads RFC 8259 JSON into a Gson tree without binding anything.
 *
 * <p>Two things the stock {@code JsonParser} does not give: numbers keep their lexical text
 * ({@link Num}), because {@code 1} and {@code 1.0} differ where the schema wants an integer (the
 * reference parser, reading JSON with Python, sees an {@code int} and a {@code float}); and a
 * duplicate object key is reported as a warning. The later value wins, as it does for the
 * reference parser.
 */
final class StrictJson {
  private StrictJson() {}

  /** Thrown for text that is not JSON; the message is Gson's, with its position. */
  static final class NotJson extends Exception {
    private static final long serialVersionUID = 1L;

    NotJson(String message) {
      super(message);
    }
  }

  static JsonElement parse(String text, List<String> warnings) throws NotJson {
    if (text.startsWith("﻿")) throw new NotJson("the text starts with a byte-order mark");
    JsonReader r = new JsonReader(new StringReader(text));
    r.setStrictness(Strictness.STRICT);
    try {
      JsonElement e = read(r, warnings);
      if (r.peek() != JsonToken.END_DOCUMENT) {
        throw new NotJson("unexpected text after the JSON value at " + r.getPath());
      }
      return e;
    } catch (IOException | IllegalStateException | NumberFormatException ex) {
      throw new NotJson(String.valueOf(ex.getMessage()));
    }
  }

  private static JsonElement read(JsonReader r, List<String> warnings) throws IOException {
    return switch (r.peek()) {
      case BEGIN_OBJECT -> {
        JsonObject o = new JsonObject();
        r.beginObject();
        while (r.hasNext()) {
          String path = r.getPath();
          String name = r.nextName();
          if (o.has(name)) {
            warnings.add("duplicate key " + Wire.quote(name) + " at " + path + "; the last one is read");
          }
          o.add(name, read(r, warnings));
        }
        r.endObject();
        yield o;
      }
      case BEGIN_ARRAY -> {
        JsonArray a = new JsonArray();
        r.beginArray();
        while (r.hasNext()) a.add(read(r, warnings));
        r.endArray();
        yield a;
      }
      case STRING -> new JsonPrimitive(r.nextString());
      case NUMBER -> new JsonPrimitive(new Num(r.nextString()));
      case BOOLEAN -> new JsonPrimitive(r.nextBoolean());
      case NULL -> {
        r.nextNull();
        yield JsonNull.INSTANCE;
      }
      case END_ARRAY, END_OBJECT, NAME, END_DOCUMENT ->
          throw new IOException("unexpected " + r.peek() + " at " + r.getPath());
    };
  }

  /**
   * A JSON number as written. {@link #toString()} is the lexical text, which {@link
   * #isIntegerLiteral()} inspects.
   */
  static final class Num extends Number {
    private static final long serialVersionUID = 1L;
    private final String text;

    Num(String text) {
      this.text = text;
    }

    /** Written without a fraction or exponent: what Python's {@code json} reads as an int. */
    boolean isIntegerLiteral() {
      return text.indexOf('.') < 0 && text.indexOf('e') < 0 && text.indexOf('E') < 0;
    }

    BigInteger bigInteger() {
      return new BigInteger(text);
    }

    @Override
    public double doubleValue() {
      return Double.parseDouble(text);
    }

    @Override
    public float floatValue() {
      return (float) doubleValue();
    }

    @Override
    public long longValue() {
      return new BigDecimal(text).longValue();
    }

    @Override
    public int intValue() {
      return new BigDecimal(text).intValue();
    }

    @Override
    public String toString() {
      return text;
    }

    @Override
    public boolean equals(@Nullable Object o) {
      return o instanceof Num n && n.text.equals(text);
    }

    @Override
    public int hashCode() {
      return text.hashCode();
    }
  }
}
