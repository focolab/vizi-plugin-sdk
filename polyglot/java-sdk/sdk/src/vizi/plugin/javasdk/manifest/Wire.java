package vizi.plugin.javasdk.manifest;

import java.util.Arrays;
import java.util.Optional;
import java.util.stream.Collectors;

/** Lookups and message formatting shared by the parser and the writer. */
final class Wire {
  private Wire() {}

  static <E extends Enum<E> & WireName> Optional<E> lookup(Class<E> type, String wire) {
    for (E e : type.getEnumConstants()) {
      if (e.wire().equals(wire)) return Optional.of(e);
    }
    return Optional.empty();
  }

  /** The permitted spellings as the reference parser prints them: {@code ['a', 'b']}. */
  static <E extends Enum<E> & WireName> String choices(Class<E> type) {
    return Arrays.stream(type.getEnumConstants())
        .map(e -> quote(e.wire()))
        .collect(Collectors.joining(", ", "[", "]"));
  }

  static String quote(String s) {
    return "'" + s + "'";
  }

  /** Characters as a person counts them (code points, not UTF-16 units). */
  static int length(String s) {
    return s.codePointCount(0, s.length());
  }
}
