package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import org.jspecify.annotations.Nullable;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * One row of a {@code table} widget (manifest_schema.md §3.3.1): a key, and its cells either in
 * the declared column order ({@link #of}) or by column id ({@link #byColumn}). A cell is a {@code
 * String}, a number (sent as {@code Value.f}), a {@code Boolean}, or {@code null} for a blank cell;
 * a missing trailing cell, or a column a by-id row leaves out, is blank too. Keys are non-empty,
 * at most 128 characters and distinct within a table.
 */
public final class TableRow {
  private final String key;
  private final @Nullable List<@Nullable Object> cells;
  private final @Nullable Map<String, @Nullable Object> byColumn;

  private TableRow(String key, @Nullable List<@Nullable Object> cells, @Nullable Map<String, @Nullable Object> byColumn) {
    this.key = Objects.requireNonNull(key, "key");
    this.cells = cells;
    this.byColumn = byColumn;
  }

  /** A row whose cells are given in the table's column order. */
  public static TableRow of(String key, @Nullable Object... cells) {
    return new TableRow(key, Collections.unmodifiableList(new ArrayList<>(Arrays.asList(cells))), null);
  }

  /** A row whose cells are given in the table's column order. */
  public static TableRow of(String key, List<? extends @Nullable Object> cells) {
    return new TableRow(key, Collections.unmodifiableList(new ArrayList<@Nullable Object>(cells)), null);
  }

  /** A row whose cells are given by column id; a column left out is blank. */
  public static TableRow byColumn(String key, Map<String, ? extends @Nullable Object> cells) {
    return new TableRow(key, null, Collections.unmodifiableMap(new LinkedHashMap<String, @Nullable Object>(cells)));
  }

  public String key() {
    return key;
  }

  /**
   * The cells in column order, given the table's column ids; throws for a column id the table does
   * not declare.
   */
  List<@Nullable Object> cells(String tableId, List<String> columns) {
    Map<String, @Nullable Object> m = byColumn;
    if (m == null) return Objects.requireNonNull(cells);
    for (String c : m.keySet()) {
      if (!columns.contains(c)) throw new IllegalArgumentException("table " + tableId + " has no column '" + c + "'");
    }
    List<@Nullable Object> out = new ArrayList<>();
    for (String c : columns) out.add(m.get(c));
    return out;
  }

  /** A cell on the wire: {@code s}, {@code f} or {@code b}, or a Value with no kind for a blank. */
  static Value cell(@Nullable Object v) {
    return switch (v) {
      case null -> Value.getDefaultInstance();
      case String s -> Value.newBuilder().setS(s).build();
      case Boolean b -> Value.newBuilder().setB(b).build();
      case Number n -> Value.newBuilder().setF(n.doubleValue()).build();
      case Value w when w.getKindCase() != Value.KindCase.INTS && w.getKindCase() != Value.KindCase.STRINGS -> w;
      default -> throw new IllegalArgumentException("a table cell is a String, a number, a Boolean or null, not " + v.getClass().getSimpleName());
    };
  }

  @Override
  public String toString() {
    return "TableRow[" + key + ", " + (byColumn != null ? byColumn : cells) + "]";
  }
}
