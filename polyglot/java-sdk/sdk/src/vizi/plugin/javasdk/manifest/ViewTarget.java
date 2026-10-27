package vizi.plugin.javasdk.manifest;

/**
 * What a panel or a tool may move with set-view ({@code view_targets}, §3.3, §3.6;
 * control_plane_protocol.md §8.15). Append-only.
 */
public enum ViewTarget implements WireName {
  /** The frame. */
  T("t"),
  /** The plane. */
  Z("z"),
  /** Select a region, as a single click on it does. */
  SELECT_REGION("select_region"),
  /** Frame a region, as choosing it in the Regions list does. */
  FRAME_REGION("frame_region"),
  /** Aim the camera at a voxel. */
  LOOK_AT("look_at"),
  /** Set the one orbit point. */
  ORBIT_POINT("orbit_point");

  private final String wire;

  ViewTarget(String wire) {
    this.wire = wire;
  }

  @Override
  public String wire() {
    return wire;
  }
}
