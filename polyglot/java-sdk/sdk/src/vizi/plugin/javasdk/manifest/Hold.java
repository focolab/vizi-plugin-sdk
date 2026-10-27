package vizi.plugin.javasdk.manifest;

/** How a trigger or a tool's pane button holds the tool engaged (§3.6.1, §3.6.3). */
public enum Hold implements WireName {
  /** Press once to engage, again to disengage. */
  TOGGLE("toggle"),
  /** Engaged only while held (a trigger) or for one gesture (a tool's pane button). */
  MOMENTARY("momentary");

  private final String wire;

  Hold(String wire) {
    this.wire = wire;
  }

  @Override
  public String wire() {
    return wire;
  }
}
