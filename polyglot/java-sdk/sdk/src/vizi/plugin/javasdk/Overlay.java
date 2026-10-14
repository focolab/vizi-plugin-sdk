package vizi.plugin.javasdk;

import java.util.List;
import java.util.concurrent.atomic.AtomicLong;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.doorbell.Doorbell;
import vizi.plugin.javasdk.doorbell.Notification;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.memory.PulsedLane;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.OverlayItem;
import vizi.plugin.v1.ViziPlugin.OverlayLabelVolume;
import vizi.plugin.v1.ViziPlugin.OverlayLineSet;
import vizi.plugin.v1.ViziPlugin.OverlayMarker;
import vizi.plugin.v1.ViziPlugin.OverlayMesh;
import vizi.plugin.v1.ViziPlugin.OverlayRemove;
import vizi.plugin.v1.ViziPlugin.OverlayText;
import vizi.plugin.v1.ViziPlugin.OverlayUpdate;
import vizi.plugin.v1.ViziPlugin.PluginMsg;

/**
 * A declared overlay (control_plane_protocol.md §8.5). Two lanes carry its content, and one
 * revision counter orders them both:
 *
 * <ul>
 *   <li>{@link #setItems}: a full replacement on the control plane, geometry lent from the
 *       overlay's arena, coalesced per overlay (a superseded push hands its loans back);
 *   <li>{@link #publishLive}: a value on the overlay's Pulsed lane, stamped with the pane pair of
 *       the event it answers, with a {@code D} hint; inline geometry only; dropped (not failed)
 *       when every buffer is busy.
 * </ul>
 *
 * Safe to call from any thread, the doorbell listener included (nothing here blocks).
 */
public final class Overlay {

  private final String id;
  private final OverlayDecl decl;
  private final Plugin.Runtime rt;
  private final AtomicLong revision = new AtomicLong();

  Overlay(String id, OverlayDecl decl, Plugin.Runtime rt) {
    this.id = id;
    this.decl = decl;
    this.rt = rt;
  }

  public String id() {
    return id;
  }

  /**
   * The largest live value this overlay's lane takes, in bytes ({@code slot_bytes} less the pane
   * header), or empty until the lane is granted. A bigger publish throws, naming both sizes.
   */
  public java.util.OptionalLong liveCapacity() {
    Session s = rt.sessionOrNull();
    if (s == null) return java.util.OptionalLong.empty();
    try {
      return s.store().live("O", id).pluginLane().map(l -> java.util.OptionalLong.of(l.capacity())).orElse(java.util.OptionalLong.empty());
    } catch (SegmentStore.UnavailableException e) {
      return java.util.OptionalLong.empty();
    }
  }

  /** What became of a live publish. */
  public enum Live {
    PUBLISHED,
    /** Every buffer was busy: coalesced away; the newest value already there stands. */
    COALESCED,
    /** The lane is quarantined, retiring, or not granted yet. */
    REFUSED
  }

  /** Replaces the overlay's content wholly. Throws {@link ArenaFullException} when the arena is short. */
  public void setItems(List<Item> items) {
    if (decl.kind() instanceof OverlayDecl.Chunked) throw new IllegalStateException("overlay " + id + " is chunked: its unit of state is the chunk");
    Session s = rt.session();
    @Nullable Segment seg = arena(s, items);
    try (Loans loans = new Loans(s)) {
      OverlayUpdate.Builder u = OverlayUpdate.newBuilder().setOverlayId(id).setRevision(revision.incrementAndGet());
      for (Item i : items) u.addItems(materialize(i, loans, seg, false));
      s.outbound().overlayUpdate(u.build(), loans.forQueue());
      loans.admitted();
    } catch (Loans.Retired retired) {
      // the arena was retired with nothing superseding it (§9): dropped quietly, counted by the store;
      // the loans already taken are handed back when Loans closes
    }
  }

  /** Retracts the overlay: one-shot, never coalesced or dropped. */
  public void clear() {
    Session s = rt.session();
    s.outbound().overlayRemove(PluginMsg.newBuilder()
        .setOverlayRemove(OverlayRemove.newBuilder().setOverlayId(id).setRevision(revision.incrementAndGet()))
        .build());
  }

  /**
   * Publishes {@code items} on the live lane for the pane of {@code at} (an event's info, whose
   * pane pair the value carries). Geometry goes inline whatever the items say.
   */
  public Live publishLive(InputEvent.Info at, List<Item> items) {
    return publishLive(at.viewId(), at.epoch(), items);
  }

  public Live publishLive(PaneContext pane, List<Item> items) {
    return publishLive(pane.viewId(), pane.epoch(), items);
  }

  private Live publishLive(int viewId, long epoch, List<Item> items) {
    if (decl.kind() instanceof OverlayDecl.Chunked) throw new IllegalStateException("overlay " + id + " is chunked and has no live lane");
    Session s = rt.session();
    Segment seg;
    try {
      seg = s.store().live("O", id);
    } catch (SegmentStore.UnavailableException e) {
      return Live.REFUSED;
    }
    PulsedLane lane = seg.pluginLane().orElse(null);
    if (lane == null) return Live.REFUSED;
    OverlayUpdate.Builder u = OverlayUpdate.newBuilder().setOverlayId(id).setRevision(revision.incrementAndGet());
    try (Loans loans = new Loans(s)) {
      for (Item i : items) u.addItems(materialize(i, loans, seg, true));
    }
    byte[] payload = u.build().toByteArray();
    if (payload.length > lane.capacity()) {
      throw new IllegalArgumentException("a live value of " + payload.length + " bytes exceeds overlay " + id
          + "'s lane capacity of " + lane.capacity() + " bytes (slot_bytes less the pane header)");
    }
    PulsedLane.Published p;
    try (SegmentStore.Op op = s.store().begin(seg, true)) {
      p = lane.publish(payload, viewId, epoch);
    } catch (SegmentStore.UnavailableException e) {
      return Live.REFUSED;
    }
    if (p == PulsedLane.Published.PUBLISHED) {
      Doorbell d = s.doorbell().orElse(null);
      int region = laneRegion(seg);
      if (d != null && region >= 0) d.sendHint(Notification.forPane(seg.id(), viewId, epoch, region));
      return Live.PUBLISHED;
    }
    return p == PulsedLane.Published.COALESCED ? Live.COALESCED : Live.REFUSED;
  }

  private static int laneRegion(Segment seg) {
    for (var r : seg.regions()) {
      if (r.pluginProduced() && r.discipline() == vizi.plugin.v1.ViziPlugin.RegionDiscipline.REGION_DISCIPLINE_PULSED) return r.index();
    }
    return -1;
  }

  /** The overlay's O segment, when any item needs the arena. */
  private @Nullable Segment arena(Session s, List<Item> items) {
    boolean needs = false;
    for (Item i : items) if (!i.inline && hasGeometry(i)) needs = true;
    if (!needs) return null;
    try {
      return s.store().live("O", id);
    } catch (SegmentStore.UnavailableException e) {
      throw new IllegalStateException("overlay " + id + " has no arena yet: " + e.getMessage(), e);
    }
  }

  private static boolean hasGeometry(Item i) {
    return i.kind == Item.Kind.MARKER || i.kind == Item.Kind.LINE_SET || i.kind == Item.Kind.LABEL_VOLUME;
  }

  static OverlayItem materialize(Item i, Loans loans, @Nullable Segment seg, boolean forceInline) {
    return materialize(i, loans, seg, forceInline, i.uses);
  }

  /** With {@code uses} declared on every loan (a compile's fresh copy is named once). */
  static OverlayItem materialize(Item i, Loans loans, @Nullable Segment seg, boolean forceInline, int uses) {
    boolean inline = forceInline || i.inline;
    if (!inline && hasGeometry(i) && seg == null) throw new IllegalStateException("no arena for item " + i.id);
    OverlayItem.Builder b = OverlayItem.newBuilder()
        .setItemId(i.id)
        .setTransform(i.transform)
        .setStyle(i.style)
        .setGeometryVersion(i.geometryVersion);
    switch (i.kind) {
      case MARKER -> {
        double[] p = i.positions == null ? new double[0] : i.positions;
        OverlayMarker.Builder m = OverlayMarker.newBuilder()
            .setPositionsZyx(loans.geom(Item.f64Bytes(p), List.of(p.length / 3, 3), DType.F64, inline, seg, uses))
            .setSizePx(i.sizePx)
            .setShape(i.shape);
        b.setMarker(m);
      }
      case LINE_SET -> {
        double[] p = i.positions == null ? new double[0] : i.positions;
        OverlayLineSet.Builder l = OverlayLineSet.newBuilder()
            .setPositionsZyx(loans.geom(Item.f64Bytes(p), List.of(p.length / 3, 3), DType.F64, inline, seg, uses))
            .setTopology(i.topology);
        if (i.colors != null) {
          l.setColorsRgba(loans.geom(java.lang.foreign.MemorySegment.ofArray(i.colors), List.of(i.colors.length / 4, 4), DType.U8,
              inline, seg, uses));
        }
        b.setLineSet(l);
      }
      case TEXT -> b.setText(OverlayText.newBuilder().setText(i.text)
          .addAnchorZyx(i.anchor[0]).addAnchorZyx(i.anchor[1]).addAnchorZyx(i.anchor[2]).setSizePx(i.sizePx));
      case MESH -> b.setMesh(OverlayMesh.newBuilder().setGeometry(i.mesh));
      case LABEL_VOLUME -> {
        Volume v = java.util.Objects.requireNonNull(i.labels);
        byte[] lut = java.util.Objects.requireNonNull(i.lut);
        OverlayLabelVolume.Builder lv = OverlayLabelVolume.newBuilder()
            .setLabels(loans.geom(v.data(), List.of(v.z(), v.y(), v.x()), v.dtype(), inline, seg, uses))
            .setColormap(loans.geom(java.lang.foreign.MemorySegment.ofArray(lut), List.of(256, 4), DType.U8, inline, seg, uses))
            .putAllLabelNames(i.names)
            .putAllLabelVisible(i.visible);
        i.t.ifPresent(lv::setT);
        b.setLabelVolume(lv);
      }
    }
    return b.build();
  }

}
