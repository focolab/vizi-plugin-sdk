package vizi.plugin.javasdk;

import java.util.List;
import java.util.Optional;
import vizi.plugin.v1.ViziPlugin.IntList;
import vizi.plugin.v1.ViziPlugin.Value;

/** Wire {@code Value}s and plain Java values, both ways, every kind included ({@code ints}). */
public final class Values {
  private Values() {}

  /** String, Double, Long, Boolean, or an unmodifiable List of Long; empty for an unset Value. */
  public static Optional<Object> toJava(Value v) {
    return switch (v.getKindCase()) {
      case S -> Optional.of(v.getS());
      case F -> Optional.of(v.getF());
      case I -> Optional.of(v.getI());
      case B -> Optional.of(v.getB());
      case INTS -> Optional.of(List.copyOf(v.getInts().getValuesList()));
      case KIND_NOT_SET -> Optional.empty();
    };
  }

  public static Value of(String s) {
    return Value.newBuilder().setS(s).build();
  }

  public static Value of(double f) {
    return Value.newBuilder().setF(f).build();
  }

  public static Value of(long i) {
    return Value.newBuilder().setI(i).build();
  }

  public static Value of(boolean b) {
    return Value.newBuilder().setB(b).build();
  }

  public static Value ofInts(List<? extends Number> values) {
    IntList.Builder l = IntList.newBuilder();
    for (Number n : values) l.addValues(n.longValue());
    return Value.newBuilder().setInts(l).build();
  }
}
