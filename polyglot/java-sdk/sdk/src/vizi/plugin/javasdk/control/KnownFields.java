package vizi.plugin.javasdk.control;

import com.google.protobuf.Descriptors.FieldDescriptor;
import com.google.protobuf.Message;
import com.google.protobuf.UnknownFieldSet;
import java.util.Map;
import org.jspecify.annotations.Nullable;

/**
 * A message's known fields alone. An arrangement's content and a plan's identity are the fields the
 * receiver knows (control_plane_protocol.md §5.5 rule 4): a field a later minor adds is skipped, so a
 * restatement that differs only by one is the same content, and an arm that differs from its plan only
 * by one matches it.
 */
final class KnownFields {
  private KnownFields() {}

  /** {@code m} without the fields this build does not know, at every depth. */
  @SuppressWarnings("unchecked")
  static <M extends Message> M of(M m) {
    return (M) strip(m);
  }

  /** Whether {@code a} and {@code b} carry the same known fields. */
  static boolean same(Message a, Message b) {
    return strip(a).equals(strip(b));
  }

  private static Message strip(Message m) {
    if (!hasUnknown(m)) return m;
    Message.Builder b = m.toBuilder();
    b.setUnknownFields(UnknownFieldSet.getDefaultInstance());
    for (Map.Entry<FieldDescriptor, Object> e : m.getAllFields().entrySet()) {
      FieldDescriptor fd = e.getKey();
      if (fd.getJavaType() != FieldDescriptor.JavaType.MESSAGE) continue;
      if (fd.isRepeated()) {
        b.clearField(fd);
        for (Object v : (Iterable<?>) e.getValue()) b.addRepeatedField(fd, strip((Message) v));
      } else {
        b.setField(fd, strip((Message) e.getValue()));
      }
    }
    return b.build();
  }

  /**
   * The first oneof at or under {@code m} that is unset while its message carries fields this build
   * does not know: a later minor's arm in place of a known one, named as {@code Value.kind (field 7)}
   * with its path; or null. A StageArm whose input or params hold one cannot be used, and is refused
   * like one naming an unknown dtype, in every SDK.
   */
  static @Nullable String unknownOneofArm(Message m) {
    if (!m.getUnknownFields().asMap().isEmpty()) {
      for (var o : m.getDescriptorForType().getRealOneofs()) {
        if (m.hasOneof(o)) continue;
        return m.getDescriptorForType().getName() + "." + o.getName() + " (field "
            + String.join(", ", m.getUnknownFields().asMap().keySet().stream().map(String::valueOf).toList()) + ")";
      }
    }
    for (Map.Entry<FieldDescriptor, Object> e : m.getAllFields().entrySet()) {
      FieldDescriptor fd = e.getKey();
      if (fd.getJavaType() != FieldDescriptor.JavaType.MESSAGE) continue;
      if (fd.isMapField()) {
        var entries = new java.util.TreeMap<String, Message>();
        for (Object v : (Iterable<?>) e.getValue()) {
          Message entry = (Message) v;
          FieldDescriptor value = entry.getDescriptorForType().findFieldByName("value");
          if (value.getJavaType() != FieldDescriptor.JavaType.MESSAGE) break;
          entries.put(String.valueOf(entry.getField(entry.getDescriptorForType().findFieldByName("key"))),
              (Message) entry.getField(value));
        }
        for (var en : entries.entrySet()) {
          String found = unknownOneofArm(en.getValue());
          if (found != null) return fd.getName() + "[\"" + en.getKey() + "\"] " + found;
        }
      } else if (fd.isRepeated()) {
        int i = 0;
        for (Object v : (Iterable<?>) e.getValue()) {
          String found = unknownOneofArm((Message) v);
          if (found != null) return fd.getName() + "[" + i + "] " + found;
          i++;
        }
      } else {
        String found = unknownOneofArm((Message) e.getValue());
        if (found != null) return fd.getName() + " " + found;
      }
    }
    return null;
  }

  private static boolean hasUnknown(Message m) {
    if (!m.getUnknownFields().asMap().isEmpty()) return true;
    for (Map.Entry<FieldDescriptor, Object> e : m.getAllFields().entrySet()) {
      if (e.getKey().getJavaType() != FieldDescriptor.JavaType.MESSAGE) continue;
      if (e.getKey().isRepeated()) {
        for (Object v : (Iterable<?>) e.getValue()) if (hasUnknown((Message) v)) return true;
      } else if (hasUnknown((Message) e.getValue())) {
        return true;
      }
    }
    return false;
  }
}
