package vizi.plugin.javasdk.manifest;

import java.util.List;
import java.util.Objects;
import java.util.OptionalDouble;
import java.util.OptionalLong;

/**
 * One setting of a processing stage (§3.7, {@code processing_stages.md} §2). On the wire its value
 * is the {@code Value} kind the type names: {@code float} is {@code f}, {@code int} is {@code i},
 * {@code bool} is {@code b}, {@code choice} and {@code string} are {@code s}.
 */
public sealed interface StageParam
    permits StageParam.FloatParam,
        StageParam.IntParam,
        StageParam.BoolParam,
        StageParam.ChoiceParam,
        StageParam.StringParam {

  /** Unique within the stage. */
  String key();

  /** The display name ({@link #key()} when the manifest gives none). */
  String name();

  Type type();

  enum Type implements WireName {
    FLOAT("float"),
    INT("int"),
    BOOL("bool"),
    CHOICE("choice"),
    STRING("string");

    private final String wire;

    Type(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  /** A finite double, optionally bounded; the default {@code value} lies within the bounds. */
  record FloatParam(String key, String name, double value, OptionalDouble min, OptionalDouble max)
      implements StageParam {
    public FloatParam {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(name, "name");
      Objects.requireNonNull(min, "min");
      Objects.requireNonNull(max, "max");
    }

    @Override
    public Type type() {
      return Type.FLOAT;
    }

    public FloatParam withName(String name) {
      return new FloatParam(key, name, value, min, max);
    }

    public FloatParam withMin(double min) {
      return new FloatParam(key, name, value, OptionalDouble.of(min), max);
    }

    public FloatParam withMax(double max) {
      return new FloatParam(key, name, value, min, OptionalDouble.of(max));
    }

    /** Whether {@code v} is finite and within the bounds. */
    public boolean admits(double v) {
      return Double.isFinite(v)
          && (min.isEmpty() || v >= min.getAsDouble())
          && (max.isEmpty() || v <= max.getAsDouble());
    }
  }

  /** A signed 64-bit integer ({@code Value.i}), optionally bounded. */
  record IntParam(String key, String name, long value, OptionalLong min, OptionalLong max)
      implements StageParam {
    public IntParam {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(name, "name");
      Objects.requireNonNull(min, "min");
      Objects.requireNonNull(max, "max");
    }

    @Override
    public Type type() {
      return Type.INT;
    }

    public IntParam withName(String name) {
      return new IntParam(key, name, value, min, max);
    }

    public IntParam withMin(long min) {
      return new IntParam(key, name, value, OptionalLong.of(min), max);
    }

    public IntParam withMax(long max) {
      return new IntParam(key, name, value, min, OptionalLong.of(max));
    }

    public boolean admits(long v) {
      return (min.isEmpty() || v >= min.getAsLong()) && (max.isEmpty() || v <= max.getAsLong());
    }
  }

  record BoolParam(String key, String name, boolean value) implements StageParam {
    public BoolParam {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(name, "name");
    }

    @Override
    public Type type() {
      return Type.BOOL;
    }

    public BoolParam withName(String name) {
      return new BoolParam(key, name, value);
    }
  }

  /** One of a non-empty list of distinct strings. */
  record ChoiceParam(String key, String name, List<String> options, String value)
      implements StageParam {
    public ChoiceParam {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(name, "name");
      options = List.copyOf(options);
      Objects.requireNonNull(value, "value");
    }

    @Override
    public Type type() {
      return Type.CHOICE;
    }

    public ChoiceParam withName(String name) {
      return new ChoiceParam(key, name, options, value);
    }
  }

  /**
   * Text, compared by the host exactly, byte for byte (§3.7).
   *
   * @param maxLength the most UTF-8 bytes a value may hold, 1 to 4096 (default 4096)
   */
  record StringParam(String key, String name, String value, int maxLength) implements StageParam {
    /** The default and the ceiling of {@code max_length}. */
    public static final int MAX_LENGTH = 4096;

    public StringParam {
      Objects.requireNonNull(key, "key");
      Objects.requireNonNull(name, "name");
      Objects.requireNonNull(value, "value");
    }

    @Override
    public Type type() {
      return Type.STRING;
    }

    public StringParam withName(String name) {
      return new StringParam(key, name, value, maxLength);
    }

    public StringParam withMaxLength(int maxLength) {
      return new StringParam(key, name, value, maxLength);
    }

    /** Whether {@code v} fits {@link #maxLength()} UTF-8 bytes. */
    public boolean admits(String v) {
      return utf8Length(v) <= maxLength;
    }
  }

  /** The length of {@code s} in UTF-8 bytes, the unit of {@code max_length}. */
  static int utf8Length(String s) {
    return s.getBytes(java.nio.charset.StandardCharsets.UTF_8).length;
  }

  static StringParam ofString(String key, String value) {
    return new StringParam(key, key, value, StringParam.MAX_LENGTH);
  }

  static FloatParam ofFloat(String key, double value) {
    return new FloatParam(key, key, value, OptionalDouble.empty(), OptionalDouble.empty());
  }

  static IntParam ofInt(String key, long value) {
    return new IntParam(key, key, value, OptionalLong.empty(), OptionalLong.empty());
  }

  static BoolParam ofBool(String key, boolean value) {
    return new BoolParam(key, key, value);
  }

  static ChoiceParam ofChoice(String key, String value, List<String> options) {
    return new ChoiceParam(key, key, options, value);
  }
}
