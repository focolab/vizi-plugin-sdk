package vizi.plugin.javasdk.control;

import java.nio.charset.StandardCharsets;
import java.util.Map;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.manifest.StageDecl;
import vizi.plugin.javasdk.manifest.StageParam;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * processing_stages.md §2: the host sends a value for every declared key and no other, of the
 * kind the type fixes ({@code float} f, {@code int} i, {@code bool} b, {@code choice} and {@code
 * string} s), finite, inside its range or options, and a string within its {@code max_length}
 * UTF-8 bytes. The same check answers {@code PlanStage} ({@code INVALID_ARGUMENT}) and refuses a
 * {@code StageArm}.
 */
public final class StageParams {
  private StageParams() {}

  /** Null when {@code params} match the declaration, else why not. */
  public static @Nullable String check(StageDecl decl, Map<String, Value> params) {
    for (StageParam p : decl.params()) {
      if (!params.containsKey(p.key())) return "param '" + p.key() + "' is missing";
    }
    for (Map.Entry<String, Value> e : params.entrySet()) {
      StageParam p = decl.param(e.getKey()).orElse(null);
      if (p == null) return "param '" + e.getKey() + "' is not declared by stage '" + decl.id() + "'";
      Value v = e.getValue();
      String bad = switch (p) {
        case StageParam.FloatParam f -> {
          if (v.getKindCase() != Value.KindCase.F) yield kind(p, "a float (f)", v);
          if (!Double.isFinite(v.getF())) yield "param '" + p.key() + "' is not finite";
          yield f.admits(v.getF()) ? null : "param '" + p.key() + "' = " + v.getF() + " is out of range";
        }
        case StageParam.IntParam i -> {
          if (v.getKindCase() != Value.KindCase.I) yield kind(p, "an int (i)", v);
          yield i.admits(v.getI()) ? null : "param '" + p.key() + "' = " + v.getI() + " is out of range";
        }
        case StageParam.BoolParam b -> v.getKindCase() == Value.KindCase.B ? null : kind(p, "a bool (b)", v);
        case StageParam.ChoiceParam c -> {
          if (v.getKindCase() != Value.KindCase.S) yield kind(p, "a choice (s)", v);
          yield c.options().contains(v.getS()) ? null
              : "param '" + p.key() + "' = \"" + v.getS() + "\" is not one of " + c.options();
        }
        case StageParam.StringParam s -> {
          if (v.getKindCase() != Value.KindCase.S) yield kind(p, "a string (s)", v);
          int len = v.getS().getBytes(StandardCharsets.UTF_8).length;
          yield len <= s.maxLength() ? null
              : "param '" + p.key() + "' is " + len + " UTF-8 bytes, over its max_length " + s.maxLength();
        }
      };
      if (bad != null) return bad;
    }
    return null;
  }

  private static String kind(StageParam p, String want, Value got) {
    return "param '" + p.key() + "' must be " + want + ", got " + got.getKindCase().name().toLowerCase(java.util.Locale.ROOT);
  }
}
