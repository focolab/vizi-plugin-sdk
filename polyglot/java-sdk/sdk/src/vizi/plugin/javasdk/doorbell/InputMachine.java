package vizi.plugin.javasdk.doorbell;

import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.Outcome.CancelReason;
import vizi.plugin.javasdk.doorbell.Outcome.Cancellation;
import vizi.plugin.javasdk.doorbell.Outcome.Disposition;

/**
 * {@code E}'s interaction state machine (doorbell_E_input.md §2, §6) and its five lifecycle
 * inputs (doorbell_implementation_details.md §6). Not thread-safe: the transport serializes every
 * call, frames and lifecycle inputs alike, under one lock.
 *
 * <p>State: the session-wide open watermark; the active tool set and its high-water mark; the
 * interaction index {@code interaction → (tool, gen, view_id, last seq, opening record)}, into
 * which only an accepted PRESS inserts; each tool's open interaction; and the installed epoch per
 * {@code view_id}.
 */
final class InputMachine {

  /** An open interaction. {@code opening} is the 112-byte record that opened it (E §2 step 2). */
  private static final class Entry {
    final long tool;
    final long gen;
    final int viewId;
    long lastSeq;
    final byte[] opening;

    Entry(long tool, long gen, int viewId, byte[] opening) {
      this.tool = tool;
      this.gen = gen;
      this.viewId = viewId;
      this.opening = opening;
    }
  }

  /** The decision on one record: its disposition, the cancellations that follow it, diagnostics. */
  record Decision(Disposition disposition, List<Cancellation> after, List<String> diagnostics) {}

  /** A lifecycle input's answer: success or failure, and the cancellations it enqueued. */
  record Lifecycle(boolean ok, List<Cancellation> cancellations, String detail) {}

  private long watermark;
  private long highWater;
  private final Set<Long> active = new HashSet<>();
  private final Map<Long, Entry> index = new LinkedHashMap<>();
  private final Map<Long, Long> openByTool = new LinkedHashMap<>();
  private final long[] epochs = new long[256];

  // ---- frames ----

  Decision onRecord(InputRecord ev, byte[] raw) {
    List<String> diags = new ArrayList<>();
    List<Cancellation> after = new ArrayList<>();
    // 1. phase
    if (!ev.knownPhase()) {
      return new Decision(new Outcome.Ignored("unknown phase " + ev.phase()), after, diags);
    }
    boolean opening = ev.opening();
    // 2. opening records at or below the watermark: a retry, or a distinct record reusing a live id
    if (opening && ev.interaction() != 0 && ev.interaction() <= watermark) {
      Entry open = index.get(ev.interaction());
      if (open == null) {
        return new Decision(new Outcome.Ignored("opening record for closed interaction " + ev.interaction()), after, diags);
      }
      if (Arrays.equals(open.opening, raw)) {
        return new Decision(new Outcome.Ignored("duplicate opening record for interaction " + ev.interaction()), after, diags);
      }
      return reject(ev, Violation.INTERACTION_MISMATCH, after, diags);
    }
    // 3. structural zeros
    if (ev.interaction() == 0 || ev.tool() == 0 || ev.gen() == 0 || ev.epoch() == 0) {
      return reject(ev, Violation.ZERO_ID, after, diags);
    }
    // 4. seq on an opening record
    if (opening && ev.seq() != 0) {
      return reject(ev, Violation.SEQ_ON_OPEN, after, diags);
    }
    // 5 and 6: the position, skipped whole for an unknown kind
    if (ev.knownPayloadKind()) {
      Violation pos = positionViolation(ev, raw);
      if (pos != null) return reject(ev, pos, after, diags);
      if (ev.miss() && (ev.payloadKind() == InputRecord.KIND_PICKED || ev.payloadKind() == InputRecord.KIND_RAY_AND_PICKED)) {
        return reject(ev, Violation.MISS_WITH_PICKED, after, diags);
      }
    } else {
      diags.add("unknown payload_kind " + ev.payloadKind() + ": processed with no position");
    }
    // 7. the target invariant, for a known kind
    if (ev.knownTargetKind()) {
      boolean none = ev.targetKind() == InputRecord.TARGET_NONE;
      if (none != (ev.targetId() == 0) || (none && ev.targetPart() != 0)) {
        return reject(ev, Violation.INVALID_TARGET, after, diags);
      }
    } else {
      diags.add("unknown target_kind " + ev.targetKind() + ": target fields ignored");
    }
    // 8. the pane epoch
    long installed = epochs[ev.viewId()];
    if (installed == 0 || ev.epoch() > installed) {
      return reject(ev, Violation.EPOCH_MISMATCH, after, diags);
    }
    if (ev.epoch() < installed) {
      return new Decision(
          new Outcome.Ignored("stale epoch " + ev.epoch() + " < " + installed + " for pane " + ev.viewId()), after, diags);
    }
    // 9. tool status: never seen, or retired
    if (ev.tool() > highWater) {
      return reject(ev, Violation.UNKNOWN_TOOL, after, diags);
    }
    if (!active.contains(ev.tool())) {
      return new Decision(new Outcome.Ignored("retired tool " + ev.tool()), after, diags);
    }
    if ((ev.mods() & 0xF0) != 0) diags.add("reserved mods bits 0x" + Integer.toHexString(ev.mods() & 0xF0) + " ignored");
    if ((ev.positionFlags() & 0xFC) != 0) {
      diags.add("reserved position_flags bits 0x" + Integer.toHexString(ev.positionFlags() & 0xFC) + " ignored");
    }
    if (ev.miss() && ev.payloadKind() == InputRecord.KIND_NONE) diags.add("MISS with no position ignored");

    if (!opening) {
      // 10. the interaction index
      Entry e = index.get(ev.interaction());
      if (e == null) {
        return new Decision(new Outcome.Ignored("unknown interaction " + ev.interaction()), after, diags);
      }
      if (e.gen != ev.gen() || e.viewId != ev.viewId() || e.tool != ev.tool()) {
        return reject(ev, Violation.INTERACTION_MISMATCH, after, diags);
      }
      // 11. seq: a repeat is dropped, a gap is counted and processed
      if (ev.seq() <= e.lastSeq) {
        return new Decision(new Outcome.Ignored("duplicate seq " + ev.seq() + " <= " + e.lastSeq), after, diags);
      }
      if (ev.seq() > e.lastSeq + 1) diags.add("seq gap: " + (ev.seq() - e.lastSeq - 1) + " samples missing");
      e.lastSeq = ev.seq();
      if (ev.phase() == InputRecord.RELEASE || ev.phase() == InputRecord.CANCEL) {
        index.remove(ev.interaction());
        openByTool.remove(ev.tool(), ev.interaction());
      }
      return new Decision(new Outcome.Decoded(new Outcome.InputBody(ev, 0)), after, diags);
    }

    // An accepted opening record. PRESS closes the tool's prior gesture and opens its own; FIRE
    // touches no gesture state. Both advance the watermark.
    watermark = Math.max(watermark, ev.interaction());
    long closed = 0;
    if (ev.phase() == InputRecord.PRESS) {
      Long prior = openByTool.remove(ev.tool());
      if (prior != null) {
        index.remove(prior);
        closed = prior;
      }
      index.put(ev.interaction(), new Entry(ev.tool(), ev.gen(), ev.viewId(), raw.clone()));
      openByTool.put(ev.tool(), ev.interaction());
    }
    return new Decision(new Outcome.Decoded(new Outcome.InputBody(ev, closed)), after, diags);
  }

  /**
   * A semantic rejection, with E §2's cancellation rules. A non-opening record cancels the
   * interaction it identifies; if it identifies none ({@code interaction == 0}), its tool's open
   * gesture; else nothing. A rejected PRESS closes its active tool's prior gesture. FIRE cancels
   * nothing.
   */
  private Decision reject(InputRecord ev, Violation where, List<Cancellation> after, List<String> diags) {
    if (ev.phase() == InputRecord.PRESS) {
      if (ev.tool() != 0 && active.contains(ev.tool())) {
        Long prior = openByTool.remove(ev.tool());
        if (prior != null) {
          Entry e = index.remove(prior);
          int view = e != null ? e.viewId : ev.viewId();
          after.add(new Cancellation(prior, ev.tool(), view, CancelReason.REJECTED_PRESS_IMPLICIT_CLOSE));
        }
      }
    } else if (!ev.opening()) {
      if (ev.interaction() != 0) {
        cancel(ev.interaction(), CancelReason.REJECTED_RECORD, after);
      } else if (ev.tool() != 0) {
        Long open = openByTool.get(ev.tool());
        if (open != null) cancel(open, CancelReason.REJECTED_RECORD, after);
      }
    }
    return new Decision(new Outcome.Rejected(where), after, diags);
  }

  private void cancel(long interaction, CancelReason reason, List<Cancellation> out) {
    Entry e = index.remove(interaction);
    if (e == null) return;
    openByTool.remove(e.tool, interaction);
    out.add(new Cancellation(interaction, e.tool, e.viewId, reason));
  }

  /** Step 5: the tail past the kind's length, then non-finite values and the ray's own rules. */
  private static @Nullable Violation positionViolation(InputRecord ev, byte[] raw) {
    int len = InputRecord.positionBytes(ev.payloadKind());
    int base = Records.PAYLOAD + 44;
    if (!Records.allZero(raw, base + len, base + 4 * InputRecord.POSITION_VALUES)) return Violation.POSITION_TAIL;
    int k = ev.payloadKind();
    // RAY_AND_PICKED's back_frac (value 11) is the one value whose NaN has a meaning (nothing stood out);
    // it is checked on its own below, so the blanket finiteness check stops short of it
    int n = k == InputRecord.KIND_RAY_AND_PICKED ? 11 : len / 4;
    for (int i = 0; i < n; i++) {
      if (!Float.isFinite(ev.position(i))) return Violation.NONFINITE_POSITION;
    }
    if (k == InputRecord.KIND_RAY_AND_PICKED) {
      float back = ev.position(11);
      if (!Float.isNaN(back) && !(back >= 0f && back <= 1f)) return Violation.NONFINITE_POSITION;
    }
    if (k == InputRecord.KIND_RAY || k == InputRecord.KIND_RAY_AND_PICKED) {
      float entry = ev.position(6);
      if (!(entry >= 0f && entry <= 1f)) return Violation.NONFINITE_POSITION;
      if (ev.position(0) == ev.position(3) && ev.position(1) == ev.position(4) && ev.position(2) == ev.position(5)) {
        return Violation.NONFINITE_POSITION;
      }
    }
    return null;
  }

  // ---- lifecycle inputs ----

  Lifecycle toolActivated(long handle) {
    if (handle == 0) return fail("handle 0 is never a tool");
    if (handle > highWater) {
      active.add(handle);
      highWater = handle;
      return ok(List.of());
    }
    if (active.contains(handle)) return ok(List.of());
    return fail("handle " + handle + " is at or below the high-water mark " + highWater + " and not active (retired)");
  }

  Lifecycle toolRetired(long handle) {
    if (handle == 0 || handle > highWater) return fail("handle " + handle + " was never activated");
    List<Cancellation> out = new ArrayList<>();
    if (active.remove(handle)) {
      for (Long i : List.copyOf(index.keySet())) {
        Entry e = index.get(i);
        if (e != null && e.tool == handle) cancel(i, CancelReason.TOOL_RETIRED, out);
      }
      openByTool.remove(handle);
    }
    return ok(out);
  }

  Lifecycle epochAcknowledged(int viewId, long epoch) {
    if (viewId < 0 || viewId > 255) return fail("view_id " + viewId + " cannot be named by an E record");
    if (epoch == 0) return fail("epoch 0 is the absent-epoch sentinel");
    long installed = epochs[viewId];
    if (epoch == installed) return ok(List.of());
    if (installed != 0 && epoch < installed) {
      return fail("epoch " + epoch + " is older than the installed " + installed + " for pane " + viewId);
    }
    epochs[viewId] = epoch;
    return ok(cancelPane(viewId, CancelReason.EPOCH_INSTALLED));
  }

  Lifecycle cancelInteraction(long interaction) {
    List<Cancellation> out = new ArrayList<>();
    cancel(interaction, CancelReason.REQUESTED, out);
    return ok(out);
  }

  Lifecycle cancelInteractions(int viewId) {
    return ok(cancelPane(viewId, CancelReason.REQUESTED));
  }

  /** One cancellation per interaction still open, for a terminal. */
  List<Cancellation> cancelAll() {
    List<Cancellation> out = new ArrayList<>();
    for (Long i : List.copyOf(index.keySet())) cancel(i, CancelReason.TERMINAL, out);
    return out;
  }

  private List<Cancellation> cancelPane(int viewId, CancelReason reason) {
    List<Cancellation> out = new ArrayList<>();
    for (Long i : List.copyOf(index.keySet())) {
      Entry e = index.get(i);
      if (e != null && e.viewId == viewId) cancel(i, reason, out);
    }
    return out;
  }

  long installedEpoch(int viewId) {
    return epochs[viewId];
  }

  long watermark() {
    return watermark;
  }

  int openInteractions() {
    return index.size();
  }

  private static Lifecycle ok(List<Cancellation> c) {
    return new Lifecycle(true, c, "");
  }

  private static Lifecycle fail(String why) {
    return new Lifecycle(false, List.of(), why);
  }
}
