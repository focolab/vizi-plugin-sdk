package vizi.plugin.javasdk;

import java.util.Optional;
import vizi.plugin.javasdk.doorbell.InputRecord;

/**
 * One event for an engaged tool (doorbell_E_input.md). A {@code switch} over it is exhaustive:
 *
 * <ul>
 *   <li>{@link Press} opens a gesture ({@code mode: gesture}); it names the gesture of the same
 *       tool it implicitly closed, if any;
 *   <li>{@link Drag} continues it; {@link Release} completes it;
 *   <li>{@link Cancel} ends it any other way: a {@code CANCEL} record, or the SDK's own
 *       cancellation (a rejected record, a retired tool, an epoch change) with no record;
 *   <li>{@link Fire} is a whole interaction in one record ({@code mode: fire});
 *   <li>{@link Action} is a declared action fired into the engaged tool, gesture or not.
 * </ul>
 */
public sealed interface InputEvent {

  /** The interaction this event belongs to. */
  long interaction();

  /** What every record carries. {@code usable} is false when its context could not be reconciled. */
  record Info(
      long interaction,
      long seq,
      long tool,
      long gen,
      long t,
      int viewId,
      long epoch,
      int level,
      int mods,
      int clickCount,
      int channels,
      int targetKind,
      long targetId,
      long targetPart,
      Position position,
      boolean miss,
      boolean usable,
      Optional<PaneContext> pane,
      InputRecord record) {

    public boolean shift() {
      return (mods & InputRecord.MOD_SHIFT) != 0;
    }

    public boolean ctrl() {
      return (mods & InputRecord.MOD_CTRL) != 0;
    }

    public boolean alt() {
      return (mods & InputRecord.MOD_ALT) != 0;
    }

    public boolean meta() {
      return (mods & InputRecord.MOD_META) != 0;
    }

    /** Whether channel {@code c} is visible: bits 0–14 of the mask, then the pane's epoch state. */
    public boolean channelVisible(int c) {
      if (c < 15) return (channels & (1 << c)) != 0;
      if ((channels & InputRecord.CHANNELS_HIGH) == 0) return false;
      return pane.map(p -> p.channelsHigh().contains(c)).orElse(false);
    }

    /**
     * Whether {@link #visibleChannels()} is complete: false when bit 15 says a channel at index
     * ≥ 15 is visible but no pane arrangement says which (E §1).
     */
    public boolean channelsKnown() {
      return (channels & InputRecord.CHANNELS_HIGH) == 0 || pane.isPresent();
    }

    /**
     * The visible channels, low ones from the mask and high ones from the epoch. Incomplete when
     * {@link #channelsKnown()} is false.
     */
    public java.util.List<Integer> visibleChannels() {
      java.util.List<Integer> out = new java.util.ArrayList<>();
      for (int c = 0; c < 15; c++) if ((channels & (1 << c)) != 0) out.add(c);
      if ((channels & InputRecord.CHANNELS_HIGH) != 0) pane.ifPresent(p -> out.addAll(p.channelsHigh()));
      return out;
    }

    /** The voxel pointed at, when the position is a 2D voxel and not a MISS. */
    public Optional<Position.Voxel> voxel() {
      return position instanceof Position.Voxel v && !miss ? Optional.of(v) : Optional.empty();
    }

    /**
     * The 3D ray this record carries, whether or not a pick came with it ({@code RAY} or {@code
     * RAY_AND_PICKED}); empty for every other kind. A tool reads the ray here and does not branch
     * on which of the two arrived.
     */
    public Optional<Position.Ray> ray() {
      return position instanceof Position.Ray r ? Optional.of(r) : Optional.empty();
    }

    /**
     * The host's pick along the ray, when one came: that ray, whose {@code picked()} is present.
     * Empty on a bare {@code RAY} (the tool declared {@code wants_pick} false, or the host found
     * nothing to send) and on every non-ray kind.
     */
    public Optional<Position.Ray> pick() {
      return position instanceof Position.Ray r && r.picked().isPresent() ? Optional.of(r) : Optional.empty();
    }
  }

  record Press(Info info, long implicitlyClosed) implements InputEvent {
    @Override
    public long interaction() {
      return info.interaction();
    }
  }

  record Drag(Info info) implements InputEvent {
    @Override
    public long interaction() {
      return info.interaction();
    }
  }

  record Release(Info info) implements InputEvent {
    @Override
    public long interaction() {
      return info.interaction();
    }
  }

  record Fire(Info info) implements InputEvent {
    @Override
    public long interaction() {
      return info.interaction();
    }
  }

  /**
   * A declared action (1-based, manifest order) fired into the engaged tool. {@code actionId} is
   * empty for an index the binding does not carry: forward compatibility, never a fault.
   */
  record Action(Info info, int index, String actionId) implements InputEvent {
    @Override
    public long interaction() {
      return info.interaction();
    }
  }

  /**
   * A gesture that ended without its release: {@code info} is present for a {@code CANCEL}
   * record and empty for the SDK's own cancellation, whose {@code reason} says why.
   */
  record Cancel(long interaction, Optional<Info> info, String reason) implements InputEvent {}
}
