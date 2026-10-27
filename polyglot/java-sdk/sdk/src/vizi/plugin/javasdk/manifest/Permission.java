package vizi.plugin.javasdk.manifest;

/**
 * The host capabilities a plugin may ask for (§5). An unknown permission string in a manifest is
 * accepted with a warning and grants nothing, so it has no constant here.
 */
public enum Permission implements WireName, Introduced {
  /**
   * Active-dataset reads, ROI read-back, and runtime asset requests. It is not a sandbox: a plugin is
   * trusted code that runs as the user and can learn the same things by other means, so vizi gates
   * nothing from it (such as another plugin's stage in {@code ViewState.filter}) to imply an
   * isolation it doesn't provide.
   */
  DATA_ACCESS("data_access"),
  /** Native ROI contribution and label-volume compilation. */
  CONTRIBUTE_ROIS("contribute_rois");

  private final String wire;

  Permission(String wire) {
    this.wire = wire;
  }

  /** All of them arrived with protocol 1.0. */
  @Override
  public int introducedMinor() {
    return 0;
  }

  @Override
  public String wire() {
    return wire;
  }
}
