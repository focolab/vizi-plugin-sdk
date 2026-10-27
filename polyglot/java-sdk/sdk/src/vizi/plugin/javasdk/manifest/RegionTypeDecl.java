package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Set;
import java.util.regex.Pattern;
import org.jspecify.annotations.Nullable;

/**
 * A region type the plugin shapes (§3.8): every region whose {@code Roi.type_name} is {@link
 * #id()} is ASKED, so the host asks the plugin where every vertex goes when the person drags one of
 * its handles (control_plane_protocol.md §8.8). Needs the {@code contribute_rois} permission.
 *
 * @param id the {@code type_name}: starts with a letter or digit, then letters, digits, {@code .},
 *     {@code _} and {@code -}; not a built-in type, and not beginning {@code vizi.}
 * @param name what a person reads ({@link #id()} when the manifest gives none)
 */
public record RegionTypeDecl(String id, String name) {

  /** The host's own types, which a plugin may not declare. */
  public static final Set<String> BUILT_IN = Set.of("box", "ellipsoid", "cylinder", "surfq6");

  /** The prefix reserved for the host's future built-ins. */
  public static final String RESERVED_PREFIX = "vizi.";

  private static final Pattern FIRST = Pattern.compile("[A-Za-z0-9]");
  private static final Pattern ID = Pattern.compile("[A-Za-z0-9][A-Za-z0-9._-]*");

  public RegionTypeDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    String problem = idProblem(id);
    if (problem != null) throw new IllegalArgumentException(problem);
  }

  public static RegionTypeDecl of(String id) {
    return new RegionTypeDecl(id, id);
  }

  public RegionTypeDecl withName(String name) {
    return new RegionTypeDecl(id, name);
  }

  /** Why {@code id} cannot name a region type (§3.8), or null. */
  public static @Nullable String idProblem(String id) {
    if (id.isEmpty() || !FIRST.matcher(id.substring(0, 1)).matches()) {
      return "region type id must start with a letter or digit";
    }
    if (BUILT_IN.contains(id)) return "region type id " + Wire.quote(id) + " is a built-in type";
    if (id.startsWith(RESERVED_PREFIX)) {
      return "region type id " + Wire.quote(id) + " is reserved: ids beginning " + Wire.quote(RESERVED_PREFIX) + " are the host's";
    }
    if (!ID.matcher(id).matches()) {
      return "region type id " + Wire.quote(id) + " may hold only letters, digits, '.', '_' and '-'";
    }
    return null;
  }
}
