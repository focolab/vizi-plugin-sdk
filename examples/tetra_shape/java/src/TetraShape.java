import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.PluginMain;
import vizi.plugin.javasdk.Rois;
import vizi.plugin.javasdk.Shape;
import vizi.plugin.javasdk.ShapeEdit;
import vizi.plugin.javasdk.ShapeEnded;
import vizi.plugin.javasdk.manifest.CommandDecl;
import vizi.plugin.javasdk.manifest.Permission;
import vizi.plugin.javasdk.manifest.RegionTypeDecl;
import vizi.plugin.v1.ViziPlugin.RoiContribution;

/**
 * The tetra_shape plugin (../SPEC.md): one irregular tetrahedron contributed four ways. {@code
 * free} and {@code scale} are declared regions the host enacts from their data; {@code volume} is
 * asked, and {@link #keepVolume} answers it; {@code unshaped} is declared with no shaper, so the SDK
 * answers its commits {@code REJECTED} itself.
 */
public final class TetraShape {
  private TetraShape() {}

  public static void main(String[] args) {
    System.exit(PluginMain.run(build(), args));
  }

  static Plugin build() {
    var b = Plugin.builder("org.example.tetra_shape", "Tetra Shape", "0.1.0")
        .entry("./tetra-shape")
        .permissions(Permission.CONTRIBUTE_ROIS);
    b.command(CommandDecl.of("tetra.contribute", "Contribute the tetrahedra").unlisted(), ctx -> {
      RoiContribution[] rois = Tetra.REGIONS.stream()
          .map(r -> Rois.contribution(r.contribId(), Tetra.region(r), r.name(), ""))
          .toArray(RoiContribution[]::new);
      ctx.host().contributeRois(true, rois);
      return "contributed " + rois.length + " tetrahedra";
    });
    b.regionType(RegionTypeDecl.of("tetra.volume").withName("Tetrahedron (keeps its volume)"), TetraShape::keepVolume);
    b.regionType(RegionTypeDecl.of("tetra.unshaped").withName("Tetrahedron (no shaper)")); // no shaper: §1, §4
    b.onShapeEnded(TetraShape::ended);
    return b.build();
  }

  /** §3, from the message alone: the same for a drag's proposal and for the commit. */
  static Shape keepVolume(ShapeEdit e) {
    return switch (Tetra.keepVolume(e.positions(), e.handle(), Tetra.zyx(e.p1()))) {
      case Tetra.Positions p -> Shape.ok(p.positions());
      case Tetra.Rejected r -> Shape.rejected(r.message());
    };
  }

  /** §4: every outcome, as a log line. */
  static void ended(ShapeEnded e) {
    System.err.println("[tetra_shape] drag " + e.dragId() + " " + e.outcome()
        + e.last().map(l -> " (" + l.typeName() + ", region " + l.regionId() + ")").orElse("")
        + (e.detail().isEmpty() ? "" : ": " + e.detail()));
  }
}
