package vizi.plugin.javasdk;

/** A voxel type. Every multi-byte sample is little-endian in shared memory. */
public enum DType {
  U8(1, vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8),
  U16(2, vizi.plugin.v1.ViziPlugin.DType.DTYPE_U16),
  F32(4, vizi.plugin.v1.ViziPlugin.DType.DTYPE_F32),
  F64(8, vizi.plugin.v1.ViziPlugin.DType.DTYPE_F64);

  public final int size;
  final vizi.plugin.v1.ViziPlugin.DType wire;

  DType(int size, vizi.plugin.v1.ViziPlugin.DType wire) {
    this.size = size;
    this.wire = wire;
  }

  public vizi.plugin.v1.ViziPlugin.DType wire() {
    return wire;
  }

  /** The type of a wire value, or empty for UNSPECIFIED or a value this SDK predates. */
  public static java.util.Optional<DType> of(vizi.plugin.v1.ViziPlugin.DType w) {
    for (DType d : values()) if (d.wire == w) return java.util.Optional.of(d);
    return java.util.Optional.empty();
  }
}
