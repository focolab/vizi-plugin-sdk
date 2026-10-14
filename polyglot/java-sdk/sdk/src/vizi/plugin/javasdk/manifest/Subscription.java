package vizi.plugin.javasdk.manifest;

/**
 * Host pushes a session asks to be arranged (§5.1). An unknown name is warned and ignored, so it
 * has no constant here.
 */
public enum Subscription implements WireName {
  /** {@code EpochChange} for a session that declares no tool. */
  EPOCHS("epochs"),
  /** {@code ViewChanged}; needs {@code data_access}. */
  VIEW("view"),
  /** {@code RegionsChanged}; needs {@code data_access}. */
  REGIONS("regions");

  private final String wire;

  Subscription(String wire) {
    this.wire = wire;
  }

  /** Whether it pushes what {@code data_access} gates (§5.1). */
  public boolean needsDataAccess() {
    return this == VIEW || this == REGIONS;
  }

  @Override
  public String wire() {
    return wire;
  }
}
