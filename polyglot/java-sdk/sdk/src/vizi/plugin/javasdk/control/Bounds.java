package vizi.plugin.javasdk.control;

import com.google.protobuf.ByteString;
import com.google.protobuf.Descriptors.Descriptor;
import com.google.protobuf.Descriptors.FieldDescriptor;
import com.google.protobuf.Message;
import java.util.List;
import java.util.Map;
import org.jspecify.annotations.Nullable;

/**
 * control_plane_protocol.md §9's size bounds, enforced on every message received and every
 * message sent, by walking the protobuf descriptors generically so that no hand-kept field list
 * can drift from the IDL.
 *
 * <ul>
 *   <li>an inline {@code bytes} or {@code string} field: at most 8 MiB;
 *   <li>a repeated (or map) field: at most 65 536 entries, except {@code TimeMapping.t} and the
 *       three dense pose arrays of {@code pois_math.v1.Roi} and {@code RoiPoseExtension} (1 048 576),
 *       {@code Geometry.levels} (64), and {@code SeriesList.entries} ({@link #MAX_SERIES});
 *   <li>a whole message: at most 32 MiB serialized.
 * </ul>
 */
public final class Bounds {
  private Bounds() {}

  public static final int MAX_INLINE = 8 * 1024 * 1024;
  public static final int MAX_MESSAGE = 32 * 1024 * 1024;
  public static final int MAX_REPEATED = 65_536;
  public static final int MAX_POSE = 1_048_576;
  public static final int MAX_LEVELS = 64;
  /**
   * {@code SeriesList.entries} (§9): high enough that the message ceiling binds first for
   * entries of ordinary size. Not always: an entry holding only an id of 4 bytes is 8 bytes on the
   * wire, and 2^22 of them are 32 MiB, so ids of 4 bytes or fewer reach this count first. Either way the
   * list is refused, never cut.
   */
  public static final int MAX_SERIES = 1 << 22;

  /** What was over which bound. */
  public record Breach(String field, String what, boolean repeatedCount) {
    @Override
    public String toString() {
      return field + ": " + what;
    }
  }

  /** The limit for a repeated field, by its full name. */
  static int repeatedLimit(FieldDescriptor f) {
    String owner = f.getContainingType().getFullName();
    String name = f.getName();
    if (owner.equals("vizi.plugin.v1.TimeMapping") && name.equals("t")) return MAX_POSE;
    if (owner.equals("vizi.plugin.v1.Geometry") && name.equals("levels")) return MAX_LEVELS;
    if (owner.equals("vizi.plugin.v1.SeriesList") && name.equals("entries")) return MAX_SERIES;
    if ((owner.equals("pois_math.v1.Roi") || owner.equals("vizi.plugin.v1.RoiPoseExtension"))
        && (name.equals("pose_translation") || name.equals("pose_rotation") || name.equals("pose_stretch"))) {
      return MAX_POSE;
    }
    return MAX_REPEATED;
  }

  /** The first bound {@code m} breaks, or null. */
  public static @Nullable Breach check(Message m) {
    int size = m.getSerializedSize();
    if (size > MAX_MESSAGE) {
      return new Breach(m.getDescriptorForType().getName(), size + " bytes exceeds the 32 MiB message bound", false);
    }
    return walk(m, m.getDescriptorForType().getName());
  }

  private static @Nullable Breach walk(Message m, String path) {
    for (Map.Entry<FieldDescriptor, Object> e : m.getAllFields().entrySet()) {
      FieldDescriptor f = e.getKey();
      String here = path + "." + f.getName();
      Object v = e.getValue();
      if (f.isRepeated()) {
        List<?> items = (List<?>) v;
        int limit = repeatedLimit(f);
        if (items.size() > limit) {
          return new Breach(here, items.size() + " entries exceeds the bound of " + limit, true);
        }
        for (int i = 0; i < items.size(); i++) {
          Breach b = scalar(f, items.get(i), here + "[" + i + "]");
          if (b != null) return b;
        }
      } else {
        Breach b = scalar(f, v, here);
        if (b != null) return b;
      }
    }
    return null;
  }

  private static @Nullable Breach scalar(FieldDescriptor f, Object v, String here) {
    return switch (f.getJavaType()) {
      case BYTE_STRING -> ((ByteString) v).size() > MAX_INLINE
          ? new Breach(here, ((ByteString) v).size() + " bytes exceeds the 8 MiB inline bound", false)
          : null;
      case STRING -> utf8Length((String) v) > MAX_INLINE
          ? new Breach(here, "a string over the 8 MiB inline bound", false)
          : null;
      case MESSAGE -> walk((Message) v, here);
      default -> null;
    };
  }

  private static long utf8Length(String s) {
    if (s.length() * 3L <= MAX_INLINE) return s.length(); // cannot exceed; skip the count
    long n = 0;
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      if (c < 0x80) n += 1;
      else if (c < 0x800) n += 2;
      else if (Character.isHighSurrogate(c)) {
        n += 4;
        i++;
      } else n += 3;
    }
    return n;
  }

  /** Every message type reachable from {@code root}, for tests that the walker sees them all. */
  static void types(Descriptor root, java.util.Set<String> out) {
    if (!out.add(root.getFullName())) return;
    for (FieldDescriptor f : root.getFields()) {
      if (f.getJavaType() == FieldDescriptor.JavaType.MESSAGE) types(f.getMessageType(), out);
    }
  }
}
