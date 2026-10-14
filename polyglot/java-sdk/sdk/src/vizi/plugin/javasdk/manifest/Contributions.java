package vizi.plugin.javasdk.manifest;

import java.util.List;
import java.util.Optional;
import java.util.function.Function;

/**
 * The eight contribution kinds (§3). Ids are unique within a kind, not across kinds. An empty
 * list is an absent kind.
 */
public record Contributions(
    List<ReaderDecl> readers,
    List<CommandDecl> commands,
    List<PanelDecl> panels,
    List<ExporterDecl> exporters,
    List<OverlayDecl> overlays,
    List<ToolDecl> tools,
    List<StageDecl> stages,
    List<RegionTypeDecl> regionTypes) {

  public static final Contributions NONE =
      new Contributions(List.of(), List.of(), List.of(), List.of(), List.of(), List.of(), List.of(), List.of());

  public Contributions {
    readers = List.copyOf(readers);
    commands = List.copyOf(commands);
    panels = List.copyOf(panels);
    exporters = List.copyOf(exporters);
    overlays = List.copyOf(overlays);
    tools = List.copyOf(tools);
    stages = List.copyOf(stages);
    regionTypes = List.copyOf(regionTypes);
  }

  /** The seven kinds before {@code region_types} (§3.8), which is then empty. */
  public Contributions(
      List<ReaderDecl> readers,
      List<CommandDecl> commands,
      List<PanelDecl> panels,
      List<ExporterDecl> exporters,
      List<OverlayDecl> overlays,
      List<ToolDecl> tools,
      List<StageDecl> stages) {
    this(readers, commands, panels, exporters, overlays, tools, stages, List.of());
  }

  public boolean isEmpty() {
    return readers.isEmpty()
        && commands.isEmpty()
        && panels.isEmpty()
        && exporters.isEmpty()
        && overlays.isEmpty()
        && tools.isEmpty()
        && stages.isEmpty()
        && regionTypes.isEmpty();
  }

  public List<String> readerIds() {
    return ids(readers, ReaderDecl::id);
  }

  public List<String> commandIds() {
    return ids(commands, CommandDecl::id);
  }

  public List<String> panelIds() {
    return ids(panels, PanelDecl::id);
  }

  public List<String> exporterIds() {
    return ids(exporters, ExporterDecl::id);
  }

  public List<String> overlayIds() {
    return ids(overlays, OverlayDecl::id);
  }

  public List<String> toolIds() {
    return ids(tools, ToolDecl::id);
  }

  public List<String> stageIds() {
    return ids(stages, StageDecl::id);
  }

  public List<String> regionTypeIds() {
    return ids(regionTypes, RegionTypeDecl::id);
  }

  public Optional<ReaderDecl> reader(String id) {
    return find(readers, ReaderDecl::id, id);
  }

  public Optional<CommandDecl> command(String id) {
    return find(commands, CommandDecl::id, id);
  }

  public Optional<PanelDecl> panel(String id) {
    return find(panels, PanelDecl::id, id);
  }

  public Optional<ExporterDecl> exporter(String id) {
    return find(exporters, ExporterDecl::id, id);
  }

  public Optional<OverlayDecl> overlay(String id) {
    return find(overlays, OverlayDecl::id, id);
  }

  public Optional<ToolDecl> tool(String id) {
    return find(tools, ToolDecl::id, id);
  }

  public Optional<StageDecl> stage(String id) {
    return find(stages, StageDecl::id, id);
  }

  public Optional<RegionTypeDecl> regionType(String id) {
    return find(regionTypes, RegionTypeDecl::id, id);
  }

  /** Declared widgets over all panels: a term of the coalescing-key capacity (§6). */
  public int widgetCount() {
    return panels.stream().mapToInt(p -> p.widgets().size()).sum();
  }

  /** The chunked overlays, in declaration order. */
  public List<OverlayDecl> chunkedOverlays() {
    return overlays.stream().filter(OverlayDecl::isChunked).toList();
  }

  private static <T> List<String> ids(List<T> xs, Function<T, String> id) {
    return xs.stream().map(id).toList();
  }

  private static <T> Optional<T> find(List<T> xs, Function<T, String> id, String wanted) {
    return xs.stream().filter(x -> id.apply(x).equals(wanted)).findFirst();
  }
}
