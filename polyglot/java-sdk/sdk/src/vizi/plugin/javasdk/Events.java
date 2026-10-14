package vizi.plugin.javasdk;

import java.util.Optional;
import vizi.plugin.javasdk.control.Arrangements;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.v1.ViziPlugin.ToolAction;
import vizi.plugin.v1.ViziPlugin.ToolBinding;

/** Decodes an accepted {@code E} record into the author's event (doorbell_E_input.md §1, §3). */
final class Events {
  private Events() {}

  static InputEvent.Info info(InputRecord r, boolean usable, Optional<Arrangements.PaneState> pane) {
    Position pos = position(r);
    boolean miss = r.miss() && !(pos instanceof Position.None) && !(pos instanceof Position.Unknown);
    boolean target = r.knownTargetKind();
    return new InputEvent.Info(
        r.interaction(),
        r.seq(),
        r.tool(),
        r.gen(),
        r.t(),
        r.viewId(),
        r.epoch(),
        r.level(),
        r.mods() & 0x0F,
        r.clickCount(),
        r.channels(),
        target ? r.targetKind() : InputRecord.TARGET_NONE,
        target ? r.targetId() : 0,
        target ? r.targetPart() : 0,
        pos,
        miss,
        usable,
        pane.map(PaneContext::new),
        r);
  }

  static Position position(InputRecord r) {
    return switch (r.payloadKind()) {
      case InputRecord.KIND_NONE -> new Position.None();
      case InputRecord.KIND_VOXEL, InputRecord.KIND_PICKED -> new Position.Voxel(r.position(0), r.position(1), r.position(2));
      case InputRecord.KIND_COLUMN_YX -> new Position.Column(Position.Column.Axis.Z, r.position(0), r.position(1), r.position(2), r.position(3));
      case InputRecord.KIND_COLUMN_ZX -> new Position.Column(Position.Column.Axis.Y, r.position(0), r.position(1), r.position(2), r.position(3));
      case InputRecord.KIND_COLUMN_ZY -> new Position.Column(Position.Column.Axis.X, r.position(0), r.position(1), r.position(2), r.position(3));
      case InputRecord.KIND_RAY, InputRecord.KIND_RAY_AND_PICKED -> new Position.Ray(
          new double[] {r.position(0), r.position(1), r.position(2)},
          new double[] {r.position(3), r.position(4), r.position(5)},
          r.position(6),
          r.position(7),
          (r.positionFlags() & InputRecord.FLAG_ROLL_REF_Y) != 0,
          r.payloadKind() == InputRecord.KIND_RAY_AND_PICKED
              ? Optional.of(new Position.Voxel(r.position(8), r.position(9), r.position(10)))
              : Optional.empty(),
          r.payloadKind() == InputRecord.KIND_RAY_AND_PICKED ? r.position(11) : Double.NaN);
      default -> new Position.Unknown(r.payloadKind());
    };
  }

  /** The event for an accepted record; empty for a phase the SDK does not act on. */
  static Optional<InputEvent> event(ToolBinding binding, InputRecord r, long implicitlyClosed, boolean usable,
      Optional<Arrangements.PaneState> pane) {
    InputEvent.Info info = info(r, usable, pane);
    return switch (r.phase()) {
      case InputRecord.PRESS -> Optional.of(new InputEvent.Press(info, implicitlyClosed));
      case InputRecord.DRAG -> Optional.of(new InputEvent.Drag(info));
      case InputRecord.RELEASE -> Optional.of(new InputEvent.Release(info));
      case InputRecord.CANCEL -> Optional.of(new InputEvent.Cancel(r.interaction(), Optional.of(info), "CANCEL record"));
      case InputRecord.FIRE -> {
        if (r.action() == 0) yield Optional.of(new InputEvent.Fire(info));
        String id = "";
        for (ToolAction a : binding.getActionsList()) if (a.getAction() == r.action()) id = a.getActionId();
        yield Optional.of(new InputEvent.Action(info, r.action(), id));
      }
      default -> Optional.empty();
    };
  }

  static InputEvent.Cancel cancel(Outcome.Cancellation c) {
    return new InputEvent.Cancel(c.interaction(), Optional.empty(), switch (c.reason()) {
      case REJECTED_RECORD -> "a record of the gesture was rejected";
      case REJECTED_PRESS_IMPLICIT_CLOSE -> "a rejected press closed it";
      case TOOL_RETIRED -> "the tool was withdrawn";
      case EPOCH_INSTALLED -> "the pane's arrangement changed";
      case REQUESTED -> "cancelled by the plugin";
      case TERMINAL -> "the session is ending";
    });
  }
}
