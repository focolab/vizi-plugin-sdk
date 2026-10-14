package vizi.plugin.javasdk.manifest;

import java.util.ArrayList;
import java.util.List;
import java.util.Objects;
import java.util.Optional;

/**
 * A processing stage: "I edit a volume in place, in a chain" (§3.7, {@code processing_stages.md}).
 *
 * @param id unique among stages; the {@code stage_id} of every stage message
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param params the stage's settings, a flat list with unique keys
 */
public record StageDecl(String id, String name, List<StageParam> params) {
  public StageDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    params = List.copyOf(params);
  }

  public static StageDecl of(String id, StageParam... params) {
    return new StageDecl(id, id, List.of(params));
  }

  public StageDecl withName(String name) {
    return new StageDecl(id, name, params);
  }

  public StageDecl withParam(StageParam param) {
    List<StageParam> p = new ArrayList<>(params);
    p.add(param);
    return new StageDecl(id, name, p);
  }

  public Optional<StageParam> param(String key) {
    return params.stream().filter(p -> p.key().equals(key)).findFirst();
  }
}
