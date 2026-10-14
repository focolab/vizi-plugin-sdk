package vizi.plugin.javasdk.control;

import java.lang.foreign.MemorySegment;
import java.util.Map;
import java.util.Optional;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.v1.ViziPlugin.ChunksWanted;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.MetadataResult;
import vizi.plugin.v1.ViziPlugin.OpenExport;
import vizi.plugin.v1.ViziPlugin.PlanStage;
import vizi.plugin.v1.ViziPlugin.ReaderOpened;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StagePlanned;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Value;

/**
 * What the control layer calls into: the application behind a session, as the author-facing
 * layer assembles it. Every method has a documented thread; none may block the dispatch thread or
 * the doorbell listener beyond the bound its comment gives. A method throwing {@link
 * HandlerFailure} chooses the reply's {@code Error.kind}; any other exception is mapped to the
 * area's default and its message is kept. Control guarantees every host request is answered
 * exactly once whatever these do.
 */
public interface Application {

  /** The manifest the application declares (validated). */
  Manifest manifest();

  /** The version string for {@code Ready; I am <version string>}. */
  String versionString();

  // ---- readers (work worker) ----

  /** What an open produced (reader_writer_contract.md §1.2a): a dataset, or the file's images. */
  sealed interface Opening permits Dataset, Listing {}

  /**
   * A file of several images, listed rather than opened. Control sends its entries as a
   * {@code SeriesList} and holds it: the next {@code OpenReader} with the same {@code uri} and
   * {@code reader_id} and a series set is opened with {@link #open}; any other retires it with
   * {@link #close}, as do 10 idle minutes and the session's end. Control checks and trims the
   * entries, and opens a one-entry list directly.
   */
  non-sealed interface Listing extends Opening, AutoCloseable {
    /** In the order a person is shown them. */
    java.util.List<vizi.plugin.v1.ViziPlugin.SeriesEntry> entries();

    /** Why the entries are not valid as the application built them, before control's own check, or null. */
    default @org.jspecify.annotations.Nullable String problem() {
      return null;
    }

    /** Opens {@code series} on what the listing holds; an id it does not have is {@code NOT_FOUND}. */
    Opening open(String series) throws Exception;

    /** Retired without having opened anything, or its open failed. */
    @Override
    default void close() {}
  }

  /** An open dataset (reader_writer_contract.md §1). */
  non-sealed interface Dataset extends AutoCloseable, Opening {
    /** Shape, dtype and the calibration the reader KNOWS; the handle is filled in by control. */
    ReaderOpened describe();

    /** The level-0 volume at {@code (t, c)}, {@code Z·Y·X} samples in C order. */
    Volume volume(int t, int c) throws Exception;

    /** The plane {@code (1, Y, X)} at {@code (t, z, c)}; derived from the volume by default. */
    default Volume plane(int t, int z, int c) throws Exception {
      Volume v = volume(t, c);
      long planeBytes = (long) v.y() * v.x() * itemSize(v.dtype());
      return new Volume(1, v.y(), v.x(), v.dtype(), v.data().asSlice(z * planeBytes, planeBytes));
    }

    /** The metadata document, or the empty form. */
    MetadataResult metadata() throws Exception;

    @Override
    void close();
  }

  /** Samples to deposit: C order, little-endian, unpadded, exactly {@code z·y·x·itemsize} bytes. */
  record Volume(int z, int y, int x, DType dtype, MemorySegment data) {}

  static int itemSize(DType d) {
    return switch (d) {
      case DTYPE_U8 -> 1;
      case DTYPE_U16 -> 2;
      case DTYPE_F32 -> 4;
      case DTYPE_F64 -> 8;
      default -> 0;
    };
  }

  /** Opens {@code uri} with the reader contribution {@code readerId}. */
  default Dataset openReader(String readerId, String uri, String fileStateDir) throws Exception {
    throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "this plugin declares no reader " + readerId);
  }

  /**
   * Opens {@code uri}, or lists it when it holds several images and {@code series} is empty
   * (§1.2a); {@code series} present names one ({@code ""} may be an id). By default the series is
   * ignored: a reader whose files hold one image opens them whatever is asked.
   */
  default Opening openReader(String readerId, String uri, String fileStateDir, Optional<String> series) throws Exception {
    return openReader(readerId, uri, fileStateDir);
  }

  // ---- exporters (work worker) ----

  /** An export in progress (reader_writer_contract.md §2). */
  interface Export {
    /**
     * One chunk: {@code channels.length} volumes of the artifact's {@code (Z, Y, X)}, concatenated
     * in channel order, in {@code bytes}, which the SDK owns and frees after the call returns.
     *
     * @param tRank the artifact time index: the rank of {@code t} among the export's distinct t's
     * @param cRanks each channel's artifact index: its rank among the distinct channels named
     */
    void chunk(long t, int tRank, int[] channels, int[] cRanks, int[] filteredChannels, MemorySegment bytes) throws Exception;

    void finish() throws Exception;

    /** Abandon and clean up; never finalise. */
    void abort();
  }

  default Export openExport(String exporterId, OpenExport msg) throws Exception {
    throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "this plugin declares no exporter " + exporterId);
  }

  // ---- commands and panels (command worker) ----

  /** Runs a command; non-empty text is shown, empty is silent. A throw is {@code ok=false}. */
  default String invokeCommand(String commandId, Map<String, Value> args) throws Exception {
    throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "no command " + commandId);
  }

  /**
   * Runs a command whose invocation carries {@code cause} (§8.15): the host's token for an
   * interaction with a {@code moves_view} widget, 0 for any other.
   */
  default String invokeCommand(String commandId, Map<String, Value> args, long cause) throws Exception {
    return invokeCommand(commandId, args);
  }

  /**
   * The person started a Follow (§8.15; dispatch thread, must not block). Return false when nothing
   * will drive it, and the session ends it at once.
   */
  default boolean followStarted(vizi.plugin.v1.ViziPlugin.FollowStarted started) {
    return false;
  }

  /** The host ended a Follow (§8.15; dispatch thread, must not block): sent exactly once. */
  default void followEnded(vizi.plugin.v1.ViziPlugin.FollowEnded ended) {}

  /** A panel opened (panel worker, never behind a command): the application re-pushes its state. */
  default void panelOpened(String panelId) {}

  /** After {@link #panelOpened}'s re-push, on the command worker: the plugin's own hook for the open. */
  default void panelOpenedHook(String panelId) {}

  /**
   * A panel's {@code P} segment installed (panel worker): the application sends the images it
   * held for want of one. It may come with no {@code PanelOpened} (vizi's relaunch of a plugin whose
   * panel is open).
   */
  default void panelSegmentInstalled(String panelId) {}

  // ---- stages (command worker for plans) ----

  /** Plans {@code stage} for an input whose params already match the declaration. */
  default StagePlanned planStage(PlanStage request) throws Exception {
    throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.NOT_FOUND, "no stage " + request.getStageId());
  }

  /** A validated, acknowledged arm (dispatch thread; must not block). */
  default void stageArmed(StageArm arm) {}

  // ---- region types a plugin shapes (§8.8; the shape worker) ----

  /** Whether a shaper is registered for {@code typeName}; control answers a commit of any other REJECTED. */
  default boolean shapes(String typeName) {
    return false;
  }

  /**
   * The shape for an edit of a region whose type {@link #shapes} names: {@code resolution},
   * {@code positions} (one per {@code edit.move.roi.vertices}) and {@code message}; control fills
   * in {@code drag_id} and {@code seq} and checks the positions before sending. {@code commit} is
   * true for a {@code shape_commit}, whose answer is kept. A {@link HandlerFailure} or any other
   * exception answers a commit {@code REJECTED} with its message, and proposes nothing for a drag.
   *
   * @param stale true once the answer is no longer wanted, for a shaper that can stop early
   */
  default vizi.plugin.v1.ViziPlugin.ShapeResult shape(vizi.plugin.v1.ViziPlugin.ShapeEdit edit, boolean commit,
      java.util.function.BooleanSupplier stale) throws Exception {
    throw new HandlerFailure(vizi.plugin.v1.ViziPlugin.Error.Kind.UNSUPPORTED, "this plugin shapes no region type " + edit.getTypeName());
  }

  /** How a drag ended (shape worker, after any answer to its commit); {@code last} is its newest edit seen, or null. */
  default void shapeEditEnded(vizi.plugin.v1.ViziPlugin.ShapeEditEnded ended, vizi.plugin.v1.ViziPlugin.@org.jspecify.annotations.Nullable ShapeEdit last) {}

  // ---- tools (doorbell listener, in order with input) ----

  default void toolActivated(ToolBinding binding) {}

  default void toolDeactivated(ToolBinding binding) {}

  /**
   * One accepted {@code E} record for an active binding. {@code usable} is false when its
   * context cannot be reconciled with the arranged state (E §2); the violation has already been
   * reported. Runs on the doorbell listener: return within 5 ms, and hand anything longer to a job.
   */
  default void input(ToolBinding binding, InputRecord event, long implicitlyClosed, boolean usable,
      Optional<Arrangements.PaneState> pane) {}

  /** An interaction ended with neither its terminal nor a successor announcing it. */
  default void cancelled(Optional<ToolBinding> binding, Outcome.Cancellation cancellation) {}

  // ---- epochs and chunk wants (dispatch thread; must not block) ----

  default void epochInstalled(Arrangements.PaneState pane) {}

  default void chunksWanted(ChunksWanted wanted) {}

  /** A {@code view} subscription's push; coalescing state. Dispatch thread; must not block. */
  default void viewChanged(vizi.plugin.v1.ViziPlugin.ViewChanged changed) {}

  /** A {@code regions} subscription's push; fold, never drop. Dispatch thread; must not block. */
  default void regionsChanged(vizi.plugin.v1.ViziPlugin.RegionsChanged changed) {}

  // ---- the session ----

  /** The session is up: {@code Ready} has been sent. Dispatch thread; must not block. */
  default void started(Session session) {}

  /** The hook run inside the shutdown window, on a dedicated thread, or empty for none. */
  default Optional<ShutdownHook> shutdownHook() {
    return Optional.empty();
  }

  /**
   * {@code Shutdown} arrived (dispatch thread; must not block): pending host requests have already
   * failed, and none may start. Stop delivering pushes; the hook runs next.
   */
  default void shutdownBegan() {}

  /** The session has ended, however it ended. */
  default void ended(String reason) {}

  /** The shutdown hook: may send final messages, must not start new work. */
  interface ShutdownHook {
    void run(ShutdownReason reason, String detail) throws Exception;
  }
}
