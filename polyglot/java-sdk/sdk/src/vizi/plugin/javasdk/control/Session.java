package vizi.plugin.javasdk.control;

import com.google.protobuf.ByteString;
import io.grpc.Metadata;
import io.grpc.ServerCall;
import io.grpc.Status;
import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicLong;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.doorbell.Doorbell;
import vizi.plugin.javasdk.doorbell.DoorbellFault;
import vizi.plugin.javasdk.doorbell.FaultClass;
import vizi.plugin.javasdk.doorbell.InputRecord;
import vizi.plugin.javasdk.doorbell.Outcome;
import vizi.plugin.javasdk.doorbell.Records;
import vizi.plugin.javasdk.doorbell.Violation;
import vizi.plugin.javasdk.manifest.Manifest;
import vizi.plugin.javasdk.manifest.StageDecl;
import vizi.plugin.javasdk.memory.LoanArena;
import vizi.plugin.javasdk.memory.Recheck;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.AbortExport;
import vizi.plugin.v1.ViziPlugin.ChunkWritten;
import vizi.plugin.v1.ViziPlugin.Closed;
import vizi.plugin.v1.ViziPlugin.CommandDone;
import vizi.plugin.v1.ViziPlugin.DType;
import vizi.plugin.v1.ViziPlugin.DoorbellFailureClass;
import vizi.plugin.v1.ViziPlugin.DoorbellFinalReport;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.ExportChunk;
import vizi.plugin.v1.ViziPlugin.ExportDone;
import vizi.plugin.v1.ViziPlugin.ExportOpened;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.PlanStage;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.Ready;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.SegmentShortfall;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;
import vizi.plugin.v1.ViziPlugin.ShutdownReport;
import vizi.plugin.v1.ViziPlugin.StageArm;
import vizi.plugin.v1.ViziPlugin.StageArmAck;
import vizi.plugin.v1.ViziPlugin.StagePlanned;
import vizi.plugin.v1.ViziPlugin.StageShape;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.Value;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * One plugin session: the control plane's plugin half (control_plane_protocol.md §11a's list).
 *
 * <h2>Threads</h2>
 *
 * <ul>
 *   <li><b>dispatch</b> ({@value #DISPATCH_THREAD}): gRPC delivers every host message here, in
 *       stream order. It only classifies (by kind, before any id matching) and routes: host
 *       requests go to a worker, arrangements and one-ways are applied promptly, replies go to
 *       their waiters. It never runs an author's handler and never blocks.
 *   <li><b>command worker</b>: commands, panel-opened hooks and stage plans, in stream order.
 *   <li><b>panel worker</b>: a panel's re-push at {@code PanelOpened} and its held images at its
 *       {@code P} install, in order, never behind a command; the plugin's hook then goes to the
 *       command worker.
 *   <li><b>work worker</b>: reader and exporter requests, in stream order, so a 120 s export
 *       chunk cannot stall a command and a command waiting on the host cannot stall an export.
 *   <li><b>shape worker</b>: the shapers of the region types the plugin shapes, and the hook for
 *       how an edit ended (§8.8), so a commit a person waits on never queues behind a command.
 *   <li><b>doorbell listener</b>: tool input, cancellations and the tool hooks, in one order.
 *   <li><b>shutdown thread</b>: the shutdown window and the author's hook.
 *   <li><b>outbound writer</b>: the only thread that writes to the stream.
 * </ul>
 */
public final class Session {

  public static final String DISPATCH_THREAD = "vizi-dispatch";

  /** The shutdown window's default when the host omits {@code deadline_ms} (§9). */
  public static final long DEFAULT_SHUTDOWN_MS = 5000;

  private static final Duration READ_WAITS_FOR_G = Duration.ofSeconds(2);

  private final Application app;
  private final Manifest manifest;
  private final Environment env;
  private final String manifestSource;
  private final @Nullable String token;
  private final Diagnostics diag = new Diagnostics();

  private final Outbound outbound;
  private final WorkRegistry work;
  private final Violations violations;
  private final SegmentStore store;
  private final Recheck recheck;
  private final HostClient host;
  private final Arrangements arrangements;
  private final Shapes shapes;
  private volatile @Nullable Doorbell doorbell;

  private final ExecutorService commandWorker = worker("vizi-command");
  private final ExecutorService panelWorker = worker("vizi-panel");
  private final ExecutorService workWorker = worker("vizi-work");
  private final AtomicLong inFlightWork = new AtomicLong();

  private final Object callLock = new Object();
  private @Nullable ServerCall<HostMsg, PluginMsg> call;
  private boolean gated = true;
  private volatile boolean streamOpen;
  private volatile String hostIdentity = "";
  private volatile int peerMajor = -1;
  private volatile int peerMinor = -1;
  private boolean helloSeen;

  private volatile boolean inWindow;
  private final CompletableFuture<String> ended = new CompletableFuture<>();

  // readers (work worker only)
  private long nextReaderHandle;
  private @Nullable OpenReaderState reader;
  private @Nullable HeldListing heldListing;
  /** How long a held listing waits for its follow-up open (§1.2a: a person may be choosing from thousands). */
  java.time.Duration heldListingIdle = java.time.Duration.ofMinutes(10);

  // exports (work worker only)
  private long nextExportHandle;
  private final Map<String, ExportState> exports = new HashMap<>();

  // stages
  private final Set<PlanKey> answeredPlans = ConcurrentHashMap.newKeySet();
  private final Map<ArmKey, StageArm> arms = new HashMap<>();

  private record OpenReaderState(String handle, String uri, String readerId, Application.Dataset dataset,
      vizi.plugin.v1.ViziPlugin.ReaderOpened described) {}

  /** The listing that answered a {@code SeriesList}, kept for the open that names its choice (§1.2a). */
  private record HeldListing(String uri, String readerId, Application.Listing listing) {}

  private static final class ExportState {
    final Application.Export export;
    final int z, y, x;
    final DType dtype;
    boolean filtered; // OpenExport.filter was present
    final List<Long> ts = new ArrayList<>();
    final List<Integer> channels = new ArrayList<>();

    ExportState(Application.Export export, int z, int y, int x, DType dtype) {
      this.export = export;
      this.z = z;
      this.y = y;
      this.x = x;
      this.dtype = dtype;
    }
  }

  private record PlanKey(String stage, StageShape input, Map<String, Value> params) {}

  private record ArmKey(int pipeline, int position) {}

  public Session(Application app, Environment env, String manifestSource, @Nullable String windowsToken) {
    this.app = app;
    this.manifest = app.manifest();
    this.env = env;
    this.manifestSource = manifestSource;
    this.token = windowsToken;
    this.outbound = new Outbound(new CallChannel(), diag::note);
    this.work = new WorkRegistry(outbound);
    this.violations = new Violations(this::sendPush);
    this.store = new SegmentStore(new StoreSink(), (purpose, subject) -> purpose.equals("O")
        ? manifest.contributions().overlay(subject).isPresent()
        : manifest.contributions().panel(subject).isPresent());
    this.recheck = new Recheck(diag::note);
    this.store.setHints(recheck);
    // a panel's P installed: its held images go out, in order with PanelOpened's re-push on the panel
    // worker, which no command holds
    this.store.setOnInstalled(seg -> {
      if (seg.purpose().equals("P")) submit(panelWorker, "P installed", () -> app.panelSegmentInstalled(seg.subject()));
    });
    this.host = new HostClient(this::sendRequest, this::forbiddenForRequests, m -> {
      diag.count("a reply its interrupted caller abandoned (" + m.getMsgCase() + ")", m.getId());
      disposeCarried(m);
    });
    this.arrangements = new Arrangements(new DoorbellTransport(), new ArrangementReporter());
    this.shapes = new Shapes(new Shapes.Shaper() {
      @Override
      public boolean shapes(String typeName) {
        return app.shapes(typeName);
      }

      @Override
      public boolean declared(String typeName) {
        return manifest.contributions().regionType(typeName).isPresent();
      }

      @Override
      public vizi.plugin.v1.ViziPlugin.ShapeResult shape(vizi.plugin.v1.ViziPlugin.ShapeEdit edit, boolean commit,
          java.util.function.BooleanSupplier stale) throws Exception {
        return app.shape(edit, commit, stale);
      }

      @Override
      public void ended(vizi.plugin.v1.ViziPlugin.ShapeEditEnded ended, vizi.plugin.v1.ViziPlugin.@Nullable ShapeEdit last) {
        app.shapeEditEnded(ended, last);
      }
    }, outbound, diag);
  }

  // ---- wiring ----

  /** Attaches the doorbell (started before readiness). */
  public void attachDoorbell(Doorbell d) {
    this.doorbell = d;
  }

  /** The doorbell's consumer: runs on the listener. */
  public Doorbell.Listener doorbellListener() {
    return new DoorbellConsumer();
  }

  /**
   * The doorbell's terminal sink: a non-blocking hand-off, called before the socket is released.
   * A fault becomes exactly one {@code DoorbellFault}, composed with the session's facts.
   */
  public void onDoorbellTerminal(Outcome terminal) {
    if (terminal instanceof Outcome.Fault f) {
      sendPush(PluginMsg.newBuilder().setDoorbellFault(compose(f.fault())).build());
      diag.note("doorbell fault: " + f.fault());
    } else if (terminal instanceof Outcome.Teardown t) {
      diag.trace("doorbell teardown: " + t.reason()); // orderly: not worth a line unless tracing
    }
  }

  ServerCall.Listener<HostMsg> attach(ServerCall<HostMsg, PluginMsg> c) {
    synchronized (callLock) {
      this.call = c;
    }
    streamOpen = true;
    outbound.onReady();
    return new ServerCall.Listener<>() {
      @Override
      public void onMessage(HostMsg m) {
        try {
          dispatch(m);
        } catch (RuntimeException | java.lang.Error e) {
          diag.note("dispatch of " + m.getMsgCase() + " raised " + e);
          if (vizi.plugin.javasdk.doorbell.Doorbell.fatal(e)) throw e;
        } finally {
          c.request(1);
        }
      }

      @Override
      public void onHalfClose() {
        endSession("the host closed its side of the stream");
      }

      @Override
      public void onCancel() {
        endSession("the stream was cancelled (host gone)");
      }

      @Override
      public void onReady() {
        outbound.onReady();
      }
    };
  }

  /** Completes with the reason when the session ends, however it ended. */
  public CompletableFuture<String> ended() {
    return ended;
  }

  // ---- what the application layer uses ----

  public Application application() {
    return app;
  }

  public Manifest manifest() {
    return manifest;
  }

  public Environment environment() {
    return env;
  }

  public SegmentStore store() {
    return store;
  }

  public Recheck recheck() {
    return recheck;
  }

  public HostClient host() {
    return host;
  }

  public Arrangements arrangements() {
    return arrangements;
  }

  public Outbound outbound() {
    return outbound;
  }

  /** Region types the plugin shapes (§8.8): the exchange's plugin half. */
  public Shapes shapes() {
    return shapes;
  }

  /** Long-running work (§8.16): the requests in flight and the background tasks. */
  public WorkRegistry work() {
    return work;
  }

  public Optional<Doorbell> doorbell() {
    return Optional.ofNullable(doorbell);
  }

  public Violations violations() {
    return violations;
  }

  /** The host's identity string (everything after {@code Hello from }). */
  public String hostIdentity() {
    return hostIdentity;
  }

  public boolean inShutdownWindow() {
    return inWindow;
  }

  public void diagnostic(String text) {
    diag.note(text);
  }

  /** How many diagnostics the SDK has noted or counted this session (tracing aside). */
  int diagnosticCount() {
    return (int) diag.noted.get();
  }

  /** Whether the current thread is one a host request must never block (listener, dispatch). */
  public boolean forbiddenForRequests(Thread t) {
    Doorbell d = doorbell;
    return t.getName().equals(DISPATCH_THREAD) || (d != null && d.onListenerThread());
  }

  /** Whether the current thread is the command worker (a picker there is said to hold the slot, once). */
  public static boolean onCommandWorker() {
    return Thread.currentThread().getName().equals("vizi-command");
  }

  /** A push or reply, after the §9 bounds check; a breach throws to the caller. */
  public void sendPush(PluginMsg m) {
    Bounds.Breach b = Bounds.check(m);
    if (b != null) throw new IllegalArgumentException("refused before sending: " + b);
    outbound.send(m);
  }

  private void sendRequest(PluginMsg m) {
    Bounds.Breach b = Bounds.check(m);
    if (b != null) throw new IllegalArgumentException("refused before sending: " + b);
    if (inWindow) throw new SessionEndedException("the session is in its shutdown window: no new work may start, and the host answers no request");
    outbound.send(m);
  }

  /** Sends a {@code SegmentShortfall} for a full arena (coalesced). */
  public void shortfall(LoanArena.ArenaFullException full) {
    outbound.shortfall(SegmentShortfall.newBuilder()
        .setSegmentId(full.segmentId)
        .setRegion(full.region)
        .setWantedBytes(full.wanted)
        .setLargestFreeBytes(full.largestFree)
        .setDropped(1)
        .build());
  }

  // ---- dispatch: classify by kind, before any id matching ----

  private void dispatch(HostMsg m) {
    Bounds.Breach breach = Bounds.check(m);
    if (breach != null) {
      refuseOverBound(m, breach);
      return;
    }
    claimCarried(m);
    if (!helloSeen) {
      if (m.getMsgCase() == HostMsg.MsgCase.HELLO) {
        onHello(m);
      } else {
        failSession("the host's first message was " + m.getMsgCase() + ", not Hello");
      }
      return;
    }
    Runnable route = switch (m.getMsgCase()) {
      case HELLO -> () -> diag.note("a second Hello ignored");
      // host requests: to a worker, exactly one reply
      case OPEN_READER, READ_VOLUME, READ_PLANE, METADATA, CLOSE_READER, OPEN_EXPORT, EXPORT_CHUNK,
          FINISH_EXPORT, ABORT_EXPORT -> () -> request(m, workWorker);
      case INVOKE_COMMAND, PLAN_STAGE -> () -> request(m, commandWorker);
      // replies to the plugin's own requests
      case VIEW_STATE, HOST_ERROR, ROIS, LABEL_VOLUME_COMPILED, TOOL_ACTIVATION_CHANGED, ASSET_RESULT,
          REQUEST_STATUS, ROIS_CONTRIBUTED, ROI_POSES_EXTENDED, SAVE_PATH_RESULT, COLORMAP_TABLE, VIEW_REQUEST_RESULT -> () -> reply(m, null);
      case DATA_VOLUME -> () -> {
        if (m.getDataVolume().hasInstall()) store.onInstall(m.getDataVolume().getInstall()); // apply, then interpret
        reply(m, m.getDataVolume().hasSegment() ? m.getDataVolume().getSegment() : null);
      };
      case MASK_VOXELS -> () -> reply(m, m.getMaskVoxels().hasSegment() ? m.getMaskVoxels().getSegment() : null);
      case ROI_INCLUSION -> () -> reply(m, m.getRoiInclusion().hasSegment() ? m.getRoiInclusion().getSegment() : null);
      // arrangements: applied promptly, acknowledged after installing
      case INSTALL_SEGMENT -> () -> {
        if (inWindow) diag.note("a standalone InstallSegment inside the shutdown window: applied and acknowledged");
        store.onInstall(m.getInstallSegment());
      };
      case TOOL_BINDING -> () -> onToolBinding(m.getToolBinding());
      case EPOCH_CHANGE -> () -> onEpochChange(m);
      case TIME_MAPPING -> () -> {
        var ack = arrangements.onTimeMapping(m.getTimeMapping());
        if (ack != null) sendPush(PluginMsg.newBuilder().setTimeMappingAck(ack).build());
      };
      case STAGE_ARM -> () -> onStageArm(m.getStageArm());
      // one-ways
      case GRANT_STATE_QUERY -> () -> sendPush(PluginMsg.newBuilder()
          .setId(m.getId())
          .setGrantState(store.query(m.getGrantStateQuery().getSegmentId()))
          .build());
      case RETIRE_SEGMENT -> () -> store.onRetire(m.getRetireSegment().getSegmentId());
      case RELEASE_LOAN -> () -> store.onReleaseLoan(m.getReleaseLoan());
      // the re-push on the panel worker, so a panel opened during a long command shows at once; then the
      // plugin's hook on the command worker, so hooks and commands never overlap
      case PANEL_OPENED -> () -> {
        String panel = m.getPanelOpened().getPanelId();
        submit(panelWorker, "PanelOpened", () -> {
          app.panelOpened(panel);
          submit(commandWorker, "the panel-opened hook", () -> app.panelOpenedHook(panel));
        });
      };
      case CHUNKS_WANTED -> () -> app.chunksWanted(m.getChunksWanted());
      // subscribed state pushes: coalescing; the application keeps the newest
      case VIEW_CHANGED -> () -> app.viewChanged(m.getViewChanged());
      // Follow (§8.15): one-shot transitions
      case FOLLOW_STARTED -> () -> {
        if (!app.followStarted(m.getFollowStarted())) {
          diag.note("a Follow started on panel " + m.getFollowStarted().getPanelId() + " with nothing to drive it: ended at once");
          outbound.endFollow(m.getFollowStarted().getFollowId());
        }
      };
      case FOLLOW_ENDED -> () -> app.followEnded(m.getFollowEnded());
      case REGIONS_CHANGED -> () -> app.regionsChanged(m.getRegionsChanged());
      // §8.8: to the shape worker, never the command worker, which a command may hold for minutes
      case SHAPE_DRAG -> () -> shapes.onDrag(m.getShapeDrag());
      case SHAPE_COMMIT -> () -> shapes.onCommit(m.getShapeCommit());
      case SHAPE_EDIT_ENDED -> () -> shapes.onEnded(m.getShapeEditEnded());
      // §8.16: here on the dispatch thread, never a worker, which may be busy with the very work it names
      case CANCEL_WORK -> () -> {
        if (!work.cancel(m.getCancelWork())) diag.trace("CancelWork for work not open: " + m.getCancelWork());
      };
      case CONTRACT_VIOLATION -> () -> diag.note("the host reports " + m.getContractViolation().getWhere() + " x"
          + m.getContractViolation().getCount() + ": " + m.getContractViolation().getDetail());
      case DOORBELL_SHUTDOWN -> () -> onDoorbellShutdown(m);
      case SHUTDOWN -> () -> onShutdown(m);
      case MSG_NOT_SET -> () -> unknownKind(m);
    };
    route.run();
  }

  private void onHello(HostMsg m) {
    helloSeen = true;
    String id = m.getId();
    String problem = Identity.checkHello(id);
    var hello = m.getHello();
    if (problem == null && hello.getProtocolMajor() != Identity.PROTOCOL_MAJOR) {
      problem = "the host speaks protocol " + hello.getProtocolMajor() + "." + hello.getProtocolMinor() + " and this SDK speaks "
          + Identity.PROTOCOL_MAJOR + "." + Identity.PROTOCOL_MINOR + " (the major must match)";
    }
    if (problem == null && token != null && !java.security.MessageDigest.isEqual(
        token.getBytes(StandardCharsets.US_ASCII), hello.getSessionToken().getBytes(StandardCharsets.US_ASCII))) {
      problem = "Hello's session_token does not match VIZI_PLUGIN_TOKEN";
    }
    peerMajor = hello.getProtocolMajor();
    peerMinor = hello.getProtocolMinor();
    if (problem != null) {
      failSession("handshake refused: " + problem + "; this plugin is " + readyId() + ", the host said "
          + Identity.truncate(id));
      return;
    }
    hostIdentity = id.substring(Identity.HELLO_PREFIX.length());
    PluginMsg ready = PluginMsg.newBuilder()
        .setId(readyId())
        .setReady(Ready.newBuilder()
            .setProtocolMajor(Identity.PROTOCOL_MAJOR)
            .setProtocolMinor(Identity.PROTOCOL_MINOR)
            .setPluginId(manifest.id()))
        .build();
    synchronized (callLock) {
      ServerCall<HostMsg, PluginMsg> c = call;
      if (c != null) c.sendMessage(ready);
      gated = false;
    }
    outbound.onReady();
    Doorbell d = doorbell;
    if (d != null) d.enableSends();
    app.started(this);
  }

  String readyId() {
    return Identity.readyId(app.versionString());
  }

  // ---- host requests ----

  private static final Set<HostMsg.MsgCase> REQUESTS = Set.of(
      HostMsg.MsgCase.OPEN_READER, HostMsg.MsgCase.READ_VOLUME, HostMsg.MsgCase.READ_PLANE, HostMsg.MsgCase.METADATA,
      HostMsg.MsgCase.CLOSE_READER, HostMsg.MsgCase.INVOKE_COMMAND, HostMsg.MsgCase.OPEN_EXPORT,
      HostMsg.MsgCase.EXPORT_CHUNK, HostMsg.MsgCase.FINISH_EXPORT, HostMsg.MsgCase.ABORT_EXPORT, HostMsg.MsgCase.PLAN_STAGE);

  private void request(HostMsg m, ExecutorService worker) {
    if (inWindow) {
      // refused with PROTOCOL; a ride-along install is NOT applied; what the message names is released
      disposeCarried(m);
      replyError(m.getId(), Error.Kind.PROTOCOL, "the session is in its shutdown window: " + m.getMsgCase() + " refused");
      return;
    }
    inFlightWork.incrementAndGet();
    // registered now, on the dispatch thread: a CancelWork that overtakes the handler's start is kept (§8.16)
    WorkRegistry.Request w = work.register(m.getId());
    worker.execute(() -> {
      try {
        answer(m, w);
      } finally {
        work.finished(w);
        inFlightWork.decrementAndGet();
      }
    });
  }

  /** Runs a request's handler and sends exactly one reply, whatever happens. */
  private void answer(HostMsg m, WorkRegistry.Request w) {
    PluginMsg.Builder reply;
    try {
      if (w.cancelled()) {
        // cancelled before its handler started: never run (§8.16); what it carries is released
        disposeCarried(m);
        reply = error(Error.Kind.CANCELLED, "cancelled before it started");
      } else {
        reply = WorkRegistry.serving(w, () -> handle(m));
      }
    } catch (CancelSignal c) {
      // the handler stopped for the host's CancelWork: not a fault, so nothing is noted against it
      reply = error(Error.Kind.CANCELLED, c.getMessage() == null ? "cancelled" : c.getMessage());
    } catch (HandlerFailure f) {
      reply = error(f.kind(), f.getMessage() == null ? "" : f.getMessage());
    } catch (Throwable t) {
      Error.Kind k = switch (m.getMsgCase()) {
        case OPEN_READER, READ_VOLUME, READ_PLANE, METADATA, OPEN_EXPORT, EXPORT_CHUNK, FINISH_EXPORT -> Error.Kind.IO_ERROR;
        default -> Error.Kind.INTERNAL;
      };
      reply = error(k, t.getClass().getSimpleName() + ": " + t.getMessage());
      if (!(t instanceof Exception)) diag.note("a handler raised " + t);
    }
    PluginMsg out = reply.setId(m.getId()).build();
    Bounds.Breach b = Bounds.check(out);
    if (b != null) out = error(Error.Kind.INTERNAL, "the reply exceeded a bound: " + b).setId(m.getId()).build();
    outbound.dropRequestProgress(m.getId()); // its answer overtakes it: the host would drop it anyway
    outbound.send(out);
  }

  private PluginMsg.Builder handle(HostMsg m) throws Exception {
    return switch (m.getMsgCase()) {
      case OPEN_READER -> openReader(m.getOpenReader());
      case READ_VOLUME -> {
        var r = m.getReadVolume();
        yield readInto(r.getHandle(), r.getT(), -1, r.getC(), r.getLevel());
      }
      case READ_PLANE -> {
        var r = m.getReadPlane();
        yield readInto(r.getHandle(), r.getT(), r.getZ(), r.getC(), r.getLevel());
      }
      case METADATA -> {
        OpenReaderState s = currentReader(m.getMetadata().getHandle());
        yield PluginMsg.newBuilder().setMetadata(s.dataset().metadata());
      }
      case CLOSE_READER -> closeReader(m.getCloseReader().getHandle());
      case OPEN_EXPORT -> openExport(m);
      case EXPORT_CHUNK -> exportChunk(m.getExportChunk());
      case FINISH_EXPORT -> {
        String h = m.getFinishExport().getHandle();
        ExportState s = exports.remove(h);
        if (s == null) throw new HandlerFailure(Error.Kind.NOT_FOUND, "no export is open under handle " + h);
        s.export.finish();
        yield PluginMsg.newBuilder().setExportDone(ExportDone.newBuilder().setHandle(h));
      }
      case ABORT_EXPORT -> abortExport(m.getAbortExport());
      case INVOKE_COMMAND -> invokeCommand(m);
      case PLAN_STAGE -> planStage(m.getPlanStage());
      default -> throw new IllegalStateException("not a request: " + m.getMsgCase());
    };
  }

  private static PluginMsg.Builder error(Error.Kind k, String message) {
    return PluginMsg.newBuilder().setError(Error.newBuilder().setKind(k).setMessage(message));
  }

  private void replyError(String id, Error.Kind k, String message) {
    outbound.send(error(k, message).setId(id).build());
  }

  // ---- readers (reader_writer_contract.md §1) ----

  private PluginMsg.Builder openReader(vizi.plugin.v1.ViziPlugin.OpenReader o) throws Exception {
    String readerId = o.getReaderId();
    var readers = manifest.contributions().readerIds();
    if (readerId.isEmpty()) {
      if (readers.size() == 1) readerId = readers.get(0);
      else if (readers.isEmpty()) throw new HandlerFailure(Error.Kind.NOT_FOUND, "this plugin declares no reader");
      else throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT,
          "OpenReader names no reader_id and this plugin declares " + readers.size() + ": " + readers);
    } else if (!readers.contains(readerId)) {
      throw new HandlerFailure(Error.Kind.NOT_FOUND, "reader_id \"" + readerId + "\" names no reader this plugin declares");
    }
    String uri = o.getUri();
    Optional<String> series = o.hasSeries() ? Optional.of(o.getSeries()) : Optional.empty();
    // a held listing opens the choice it offered; any other open retires it, first, since it may
    // hold the file the same-URI release below is about to reopen
    HeldListing held = heldListing;
    heldListing = null;
    Application.Listing source = held != null && series.isPresent() && held.uri().equals(uri) && held.readerId().equals(readerId)
        ? held.listing()
        : null;
    if (held != null && source == null) closeListing(held.listing());
    OpenReaderState old = reader;
    if (old != null && old.uri().equals(uri)) {
      // a reload: close first, because an exclusively locked file cannot be opened twice
      closeQuietly(old);
      reader = null;
      old = null;
    }
    Application.Opening opening;
    try {
      opening = source != null ? source.open(series.get()) : app.openReader(readerId, uri, o.getFileStateDir(), series);
    } catch (Throwable t) {
      if (source != null) closeListing(source);
      throw t;
    }
    if (opening instanceof Application.Listing listing) {
      if (source != null && !source.equals(listing)) closeListing(source);
      source = listing;
      try {
        if (series.isPresent()) throw SeriesLists.listForNamedSeries(series.get());
        String problem = listing.problem();
        if (problem == null) problem = SeriesLists.problem(listing.entries());
        if (problem != null) throw SeriesLists.invalid(problem);
        if (listing.entries().size() > 1) {
          // "nothing changed" (H2): no handle, no grant, the open reader untouched
          HeldListing h = new HeldListing(uri, readerId, listing);
          heldListing = h;
          retireWhenIdle(h);
          return PluginMsg.newBuilder().setSeriesList(SeriesLists.trim(listing.entries()));
        }
        // a list of one is not a choice: open it
        String only = listing.entries().get(0).getId();
        opening = listing.open(only);
        if (opening instanceof Application.Listing again) {
          if (!again.equals(listing)) closeListing(again);
          throw SeriesLists.listForNamedSeries(only);
        }
      } catch (Throwable t) {
        closeListing(listing);
        throw t;
      }
    }
    Application.Dataset ds = (Application.Dataset) opening;
    var described = ds.describe();
    String handle = "r" + nextReaderHandle++;
    // refused before anything else changes (§1.2 H2): the new instance is closed, the open one kept
    if (described.getShapeTczyxCount() != 5) {
      closeQuietly(new OpenReaderState(handle, uri, readerId, ds, described));
      throw new HandlerFailure(Error.Kind.INTERNAL, "the reader described " + described.getShapeTczyxCount() + " axes, not 5 (TCZYX)");
    }
    long channels = Integer.toUnsignedLong(described.getShapeTczyx(1));
    if (described.getChannelNamesCount() > channels) {
      closeQuietly(new OpenReaderState(handle, uri, readerId, ds, described));
      throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, "open failed: the reader's ReaderInfo is not valid: channel_names names "
          + described.getChannelNamesCount() + " channels of " + channels);
    }
    if (old != null) closeQuietly(old);
    reader = new OpenReaderState(handle, uri, readerId, ds, described);
    return PluginMsg.newBuilder().setReaderOpened(described.toBuilder().setHandle(handle).setLittleEndian(true));
  }

  private void closeQuietly(OpenReaderState s) {
    try {
      s.dataset().close();
    } catch (RuntimeException e) {
      diag.note("closing reader " + s.handle() + " raised " + e);
    }
  }

  /** Retires a listing; a closer that throws is noted, never raised. */
  private void closeListing(Application.Listing l) {
    try {
      l.close();
    } catch (RuntimeException e) {
      diag.note("closing a series listing raised " + e);
    }
  }

  /** Closes {@code h} after {@link #heldListingIdle} unless an open has taken or retired it by then. */
  private void retireWhenIdle(HeldListing h) {
    var idle = CompletableFuture.delayedExecutor(heldListingIdle.toNanos(), TimeUnit.NANOSECONDS, workWorker);
    var _ = CompletableFuture.runAsync(() -> {
      if (h.equals(heldListing)) {
        heldListing = null;
        diag.note("a series listing of " + h.uri() + " was idle for " + heldListingIdle + ": closed");
        closeListing(h.listing());
      }
    }, idle);
  }

  private OpenReaderState currentReader(String handle) throws HandlerFailure {
    OpenReaderState s = reader;
    if (s == null || !s.handle().equals(handle)) {
      if (s != null) diag.note("a request named reader handle " + handle + " while " + s.handle() + " is current");
      throw new HandlerFailure(Error.Kind.NOT_FOUND, "no reader is open under handle \"" + handle + "\"");
    }
    return s;
  }

  private PluginMsg.Builder closeReader(String handle) {
    OpenReaderState s = reader;
    if (s != null && s.handle().equals(handle)) {
      closeQuietly(s);
      reader = null;
    }
    return PluginMsg.newBuilder().setClosed(Closed.newBuilder().setHandle(handle));
  }

  private PluginMsg.Builder readInto(String handle, int t, int z, int c, int level) throws Exception {
    OpenReaderState s = currentReader(handle);
    List<Integer> shape = s.described().getShapeTczyxList();
    if (level != 0) throw new HandlerFailure(Error.Kind.OUT_OF_BOUNDS, "level " + Integer.toUnsignedString(level) + ": this reader serves level 0 only");
    if (Integer.compareUnsigned(t, shape.get(0)) >= 0) throw new HandlerFailure(Error.Kind.OUT_OF_BOUNDS, "t " + Integer.toUnsignedString(t) + " is outside [0, " + shape.get(0) + ")");
    if (Integer.compareUnsigned(c, shape.get(1)) >= 0) throw new HandlerFailure(Error.Kind.OUT_OF_BOUNDS, "c " + Integer.toUnsignedString(c) + " is outside [0, " + shape.get(1) + ")");
    if (z >= 0 && Integer.compareUnsigned(z, shape.get(2)) >= 0) throw new HandlerFailure(Error.Kind.OUT_OF_BOUNDS, "z " + Integer.toUnsignedString(z) + " is outside [0, " + shape.get(2) + ")");
    Segment g = liveOrWait("G", "", READ_WAITS_FOR_G);
    Application.Volume v = z < 0 ? s.dataset().volume(t, c) : s.dataset().plane(t, z, c);
    int wantZ = z < 0 ? shape.get(2) : 1;
    DType dt = s.described().getDtype();
    if (v.z() != wantZ || v.y() != shape.get(3) || v.x() != shape.get(4) || v.dtype() != dt) {
      throw new HandlerFailure(Error.Kind.INTERNAL, "the reader returned (" + v.z() + ", " + v.y() + ", " + v.x() + ") "
          + v.dtype() + " where (" + wantZ + ", " + shape.get(3) + ", " + shape.get(4) + ") " + dt + " was declared");
    }
    long len = (long) v.z() * v.y() * v.x() * Application.itemSize(dt);
    if (v.data().byteSize() != len) {
      throw new HandlerFailure(Error.Kind.INTERNAL, "the reader returned " + v.data().byteSize() + " bytes where " + len + " were due");
    }
    LoanArena.Loan loan;
    try {
      loan = store.lend(g, v.data(), 1);
    } catch (LoanArena.ArenaFullException full) {
      shortfall(full);
      throw new HandlerFailure(Error.Kind.INTERNAL, full.getMessage() == null ? "the G arena is full" : full.getMessage());
    }
    return PluginMsg.newBuilder().setSegment(SegmentRef.newBuilder()
        .setSegmentId(loan.segmentId())
        .setOffset(loan.offset())
        .setLen(loan.len())
        .addShapeZyx(v.z()).addShapeZyx(v.y()).addShapeZyx(v.x())
        .setDtype(dt));
  }

  /** The one live segment of a purpose, waiting a bounded time for a grant still landing. */
  private Segment liveOrWait(String purpose, String subject, Duration wait) throws HandlerFailure, InterruptedException {
    long deadline = System.nanoTime() + wait.toNanos();
    while (true) {
      try {
        return store.live(purpose, subject);
      } catch (SegmentStore.UnavailableException e) {
        if (e.getMessage() != null && e.getMessage().startsWith("two live")) {
          throw new HandlerFailure(Error.Kind.INTERNAL, e.getMessage());
        }
        if (System.nanoTime() > deadline) {
          throw new HandlerFailure(Error.Kind.INTERNAL, "no segment with purpose '" + purpose + "' is installed");
        }
        Thread.sleep(10);
      }
    }
  }

  // ---- exporters (reader_writer_contract.md §2) ----

  private PluginMsg.Builder openExport(HostMsg m) throws Exception {
    var o = m.getOpenExport();
    if (manifest.contributions().exporter(o.getExporterId()).isEmpty()) {
      throw new HandlerFailure(Error.Kind.NOT_FOUND, "exporter_id \"" + o.getExporterId() + "\" names no exporter this plugin declares");
    }
    if (o.getShapeTczyxCount() != 5) throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, "shape_tczyx must have 5 elements");
    Application.Export ex = app.openExport(o.getExporterId(), o);
    String h = "x" + nextExportHandle++;
    ExportState st = new ExportState(ex, o.getShapeTczyx(2), o.getShapeTczyx(3), o.getShapeTczyx(4), o.getDtype());
    st.filtered = o.hasFilter();
    exports.put(h, st);
    return PluginMsg.newBuilder().setExportOpened(ExportOpened.newBuilder().setHandle(h));
  }

  /** The chunk's contradiction of its export (§8.6), or null. */
  private static @Nullable String filterContradiction(ExportState s, ExportChunk ch) {
    if (ch.getFilteredChannelsCount() == 0) return null;
    if (!s.filtered) {
      return "the chunk names filtered_channels " + ch.getFilteredChannelsList() + " but its export was opened with no filter";
    }
    for (int c : ch.getFilteredChannelsList()) {
      if (!ch.getChannelsList().contains(c)) {
        return "filtered_channels names channel " + Integer.toUnsignedString(c) + ", which is not among the chunk's channels " + ch.getChannelsList();
      }
    }
    return null;
  }

  private PluginMsg.Builder exportChunk(ExportChunk ch) throws Exception {
    ExportState s = exports.get(ch.getHandle());
    boolean inline = ch.getSourceCase() == ExportChunk.SourceCase.INLINE;
    if (ch.hasInstall()) {
      if (inline) {
        violations.report(ViolationSite.VIOLATION_SITE_INSTALL_WITHOUT_SEGMENT,
            "ExportChunk carries inline bytes and an InstallSegment, which names no segment for it", new byte[0]);
      } else {
        store.onInstall(ch.getInstall());
      }
    }
    if (s == null) {
      if (!inline && ch.hasSegment()) disposeWhenSettled(ch.getSegment());
      throw new HandlerFailure(Error.Kind.NOT_FOUND, "no export is open under handle " + ch.getHandle());
    }
    String contradiction = filterContradiction(s, ch);
    if (contradiction != null) {
      // §8.6: a host error; nothing of it reaches the exporter, and its bytes go back
      if (!inline && ch.hasSegment()) disposeWhenSettled(ch.getSegment());
      throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, contradiction);
    }
    long want = (long) ch.getChannelsCount() * s.z * s.y * s.x * Application.itemSize(s.dtype);
    int[] chans = ch.getChannelsList().stream().mapToInt(Integer::intValue).toArray();
    int[] filtered = ch.getFilteredChannelsList().stream().mapToInt(Integer::intValue).toArray();
    long t = Integer.toUnsignedLong(ch.getT());
    try (Arena arena = Arena.ofConfined()) {
      MemorySegment bytes;
      if (inline) {
        ByteString b = ch.getInline();
        if (b.size() != want) {
          throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, "the chunk carries " + b.size() + " bytes where channels x Z x Y x X x itemsize = " + want);
        }
        bytes = arena.allocate(Math.max(1, b.size()));
        MemorySegment.copy(b.toByteArray(), 0, bytes, java.lang.foreign.ValueLayout.JAVA_BYTE, 0, b.size());
      } else {
        SegmentRef ref = ch.getSegment();
        awaitInstalled(ref.getSegmentId());
        if (ref.getLen() != want) {
          store.disposeDeposit(ref);
          throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, "the chunk's len " + ref.getLen() + " is not channels x Z x Y x X x itemsize = " + want);
        }
        bytes = arena.allocate(Math.max(1, want));
        try {
          store.readDeposit(ref, bytes);
        } catch (SegmentStore.DepositException e) {
          store.disposeDeposit(ref);
          throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, e.getMessage() == null ? "bad deposit" : e.getMessage());
        }
      }
      int tRank = rank(s.ts, t);
      int[] cRanks = new int[chans.length];
      for (int i = 0; i < chans.length; i++) cRanks[i] = rank(s.channels, chans[i]);
      s.export.chunk(t, tRank, chans, cRanks, filtered, bytes.asSlice(0, want));
    }
    return PluginMsg.newBuilder().setChunkWritten(ChunkWritten.newBuilder().setHandle(ch.getHandle()).setTotalBytes(want));
  }

  private static <T> int rank(List<T> seen, T v) {
    int i = seen.indexOf(v);
    if (i >= 0) return i;
    seen.add(v);
    return seen.size() - 1;
  }

  private PluginMsg.Builder abortExport(AbortExport a) {
    ExportState s = exports.remove(a.getHandle());
    if (s != null) {
      try {
        s.export.abort();
      } catch (RuntimeException e) {
        diag.note("aborting export " + a.getHandle() + " raised " + e);
      }
    }
    return PluginMsg.newBuilder().setExportDone(ExportDone.newBuilder().setHandle(a.getHandle()));
  }

  /** Holds a request until its segment is installed; the host suspends its deadline meanwhile. */
  private void awaitInstalled(long segmentId) throws HandlerFailure, InterruptedException {
    CompletableFuture<SegmentStore.Settlement> f = new CompletableFuture<>();
    store.whenSettled(segmentId, f::complete);
    SegmentStore.Settlement s;
    try {
      s = f.get(10, TimeUnit.MINUTES);
    } catch (java.util.concurrent.ExecutionException | java.util.concurrent.TimeoutException e) {
      throw new HandlerFailure(Error.Kind.INTERNAL, "segment " + Long.toUnsignedString(segmentId) + " never installed");
    }
    if (!(s instanceof SegmentStore.Settlement.Installed)) {
      throw new HandlerFailure(Error.Kind.INTERNAL, "segment " + Long.toUnsignedString(segmentId) + " did not install: " + s);
    }
  }

  // ---- commands ----

  private PluginMsg.Builder invokeCommand(HostMsg m) throws Exception {
    var ic = m.getInvokeCommand();
    if (manifest.contributions().command(ic.getCommandId()).isEmpty()) {
      throw new HandlerFailure(Error.Kind.NOT_FOUND, "command \"" + ic.getCommandId() + "\" is not one this plugin declares");
    }
    CommandDone.Builder done = CommandDone.newBuilder().setCommandId(ic.getCommandId());
    try {
      String text = app.invokeCommand(ic.getCommandId(), ic.getArgsMap(), ic.getCause());
      done.setOk(true).setMessage(text);
    } catch (HandlerFailure f) {
      done.setOk(false).setMessage(f.getMessage() == null ? "" : f.getMessage());
    } catch (Exception e) {
      done.setOk(false).setMessage(e.getMessage() == null ? e.getClass().getSimpleName() : e.getMessage());
    }
    return PluginMsg.newBuilder().setCommandDone(done);
  }

  // ---- stages (processing_stages.md §3, §4) ----

  private PluginMsg.Builder planStage(PlanStage p) throws Exception {
    StageDecl decl = manifest.contributions().stage(p.getStageId())
        .orElseThrow(() -> new HandlerFailure(Error.Kind.NOT_FOUND, "stage \"" + p.getStageId() + "\" is not one this plugin declares"));
    if (!p.hasInput()) throw new HandlerFailure(Error.Kind.PROTOCOL, "PlanStage carries no input");
    String shapeProblem = shapeProblem(p.getInput());
    if (shapeProblem != null) throw new HandlerFailure(Error.Kind.PROTOCOL, "input: " + shapeProblem);
    String params = StageParams.check(decl, p.getParamsMap());
    if (params != null) throw new HandlerFailure(Error.Kind.INVALID_ARGUMENT, params);
    StagePlanned plan;
    try {
      plan = app.planStage(p);
    } catch (HandlerFailure f) {
      throw f;
    } catch (Exception e) {
      throw new HandlerFailure(Error.Kind.INTERNAL, "planning raised " + e);
    }
    String bad = planProblem(p.getInput(), plan);
    if (bad != null) {
      if (bad.startsWith("index")) throw new HandlerFailure(Error.Kind.PROTOCOL, "the plan names an " + bad);
      throw new HandlerFailure(Error.Kind.INTERNAL, "the stage's plan breaks a rule: " + bad);
    }
    answeredPlans.add(new PlanKey(p.getStageId(), p.getInput(), Map.copyOf(p.getParamsMap())));
    return PluginMsg.newBuilder().setStagePlanned(plan);
  }

  private static @Nullable String shapeProblem(StageShape s) {
    if (s.getDtype() == DType.DTYPE_UNSPECIFIED || s.getDtype() == DType.UNRECOGNIZED || Application.itemSize(s.getDtype()) == 0) {
      return "dtype " + s.getDtypeValue() + " is not a DType";
    }
    Set<Integer> seen = new HashSet<>();
    for (int c : s.getChannelsList()) {
      if (Integer.toUnsignedLong(c) > 65535) return "channel index " + Integer.toUnsignedString(c) + " is above 65 535";
      if (!seen.add(c)) return "channel " + c + " appears twice";
    }
    return null;
  }

  private static @Nullable String planProblem(StageShape in, StagePlanned plan) {
    if (!plan.hasOutput()) return "the plan has no output";
    StageShape out = plan.getOutput();
    for (long[] ax : new long[][] {{out.getZ(), in.getZ()}, {out.getY(), in.getY()}, {out.getX(), in.getX()}}) {
      if (ax[0] < 1 || Long.compareUnsigned(ax[0], ax[1]) > 0) return "an output axis of " + ax[0] + " is outside [1, " + ax[1] + "]";
    }
    if (Application.itemSize(out.getDtype()) == 0) return "the output dtype is not a DType";
    Set<Integer> input = new HashSet<>(in.getChannelsList());
    for (var m : plan.getModifiesList()) {
      if (Integer.toUnsignedLong(m.getChannel()) > 65535) return "index " + Integer.toUnsignedString(m.getChannel()) + " above 65 535";
      if (!input.contains(m.getChannel())) return "it modifies channel " + m.getChannel() + ", which the input lacks";
      for (int n : m.getNeedsList()) if (!input.contains(n)) return "it needs channel " + n + ", which the input lacks";
    }
    Set<String> keys = new HashSet<>();
    for (var a : plan.getAddsList()) {
      if (a.getKey().isEmpty() || !keys.add(a.getKey())) return "an added channel's key is empty or repeated";
      for (int n : a.getNeedsList()) if (!input.contains(n)) return "it needs channel " + n + ", which the input lacks";
    }
    for (int r : plan.getReadsList()) if (!input.contains(r)) return "it reads channel " + r + ", which the input lacks";
    return null;
  }

  private void onStageArm(StageArm a) {
    String refused = armProblem(a);
    if (refused != null) {
      violations.report(ViolationSite.VIOLATION_SITE_STAGE_ARM_REFUSED,
          "StageArm " + a.getStageId() + " at (" + a.getPipeline() + ", " + a.getPosition() + ") revision " + a.getRevision()
              + " refused: " + refused, new byte[0]);
      return;
    }
    ArmKey k = new ArmKey(a.getPipeline(), a.getPosition());
    StageArm have = arms.get(k);
    boolean fresh = true;
    if (have != null) {
      if (Integer.compareUnsigned(a.getRevision(), have.getRevision()) < 0) {
        violations.report(ViolationSite.VIOLATION_SITE_ARRANGEMENT_REGRESSED,
            "StageArm at (" + a.getPipeline() + ", " + a.getPosition() + ") revision " + a.getRevision() + " after " + have.getRevision(), new byte[0]);
        return;
      }
      if (a.getRevision() == have.getRevision()) {
        if (!have.equals(a)) {
          violations.report(ViolationSite.VIOLATION_SITE_ARRANGEMENT_CONTENT_MISMATCH,
              "StageArm at (" + a.getPipeline() + ", " + a.getPosition() + ") revision " + a.getRevision() + " restated differently", new byte[0]);
          return;
        }
        fresh = false;
      }
    } else if (a.getRevision() != 1) {
      violations.report(ViolationSite.VIOLATION_SITE_ARRANGEMENT_FIRST_REVISION,
          "the first StageArm at (" + a.getPipeline() + ", " + a.getPosition() + ") carries revision " + a.getRevision() + "; installed", new byte[0]);
    }
    arms.put(k, a);
    sendPush(PluginMsg.newBuilder().setStageArmAck(StageArmAck.newBuilder()
        .setStageId(a.getStageId()).setRevision(a.getRevision()).setPipeline(a.getPipeline()).setPosition(a.getPosition())).build());
    if (fresh) app.stageArmed(a);
  }

  private @Nullable String armProblem(StageArm a) {
    if (a.getRevision() == 0) return "revision 0 names no statement";
    Optional<Segment> seg = store.installed(a.getSegmentId());
    if (seg.isEmpty() || !seg.get().purpose().equals("S")) return "segment " + Long.toUnsignedString(a.getSegmentId()) + " is not an installed S segment";
    Optional<StageDecl> decl = manifest.contributions().stage(a.getStageId());
    if (decl.isEmpty()) return "stage \"" + a.getStageId() + "\" is not declared";
    if (a.getPipeline() < 1 || a.getPipeline() > 65535) return "pipeline " + Integer.toUnsignedString(a.getPipeline()) + " is outside 1-65535";
    if (a.getPosition() < 1 || a.getPosition() > 32766) return "position " + Integer.toUnsignedString(a.getPosition()) + " is outside 1-32766";
    String params = StageParams.check(decl.get(), a.getParamsMap());
    if (params != null) return params;
    if (!a.hasInput() || !a.hasOutput()) return "the arm carries no " + (a.hasInput() ? "output" : "input");
    Set<Integer> seen = new HashSet<>();
    for (int c : a.getInput().getChannelsList()) if (!seen.add(c)) return "channel " + c + " is named twice";
    for (int c : a.getNewChannelsMap().values()) if (!seen.add(c)) return "channel " + c + " is named twice";
    if (!answeredPlans.contains(new PlanKey(a.getStageId(), a.getInput(), Map.copyOf(a.getParamsMap())))) {
      return "no answered PlanStage for this stage, input and params stands behind it";
    }
    return null;
  }

  /** The armed arms, for the stage runner. */
  public synchronized List<StageArm> armed() {
    return List.copyOf(arms.values());
  }

  // ---- replies to the plugin's own requests ----

  private void reply(HostMsg m, @Nullable SegmentRef carried) {
    if (carried != null) {
      Optional<Segment> s = store.installed(carried.getSegmentId());
      if (s.isEmpty() && store.query(carried.getSegmentId()).getState() == vizi.plugin.v1.ViziPlugin.GrantStateKind.GRANT_STATE_PENDING) {
        // a descriptor into a segment not yet installed is held unread, then delivered in order
        store.whenSettled(carried.getSegmentId(), settled -> deliverOrDispose(m, carried));
        return;
      }
    }
    deliverOrDispose(m, carried);
  }

  private void deliverOrDispose(HostMsg m, @Nullable SegmentRef carried) {
    if (host.deliver(m)) return;
    diag.count("a reply matched no waiter (" + m.getMsgCase() + ")", m.getId());
    if (carried != null) store.disposeDeposit(carried);
  }

  /** Releases what a message names when it is refused or abandoned. */
  private void disposeCarried(HostMsg m) {
    switch (m.getMsgCase()) {
      case EXPORT_CHUNK -> {
        if (m.getExportChunk().hasSegment()) disposeWhenSettled(m.getExportChunk().getSegment());
      }
      case DATA_VOLUME -> {
        if (m.getDataVolume().hasSegment()) disposeWhenSettled(m.getDataVolume().getSegment());
      }
      case MASK_VOXELS -> {
        if (m.getMaskVoxels().hasSegment()) disposeWhenSettled(m.getMaskVoxels().getSegment());
      }
      case ROI_INCLUSION -> {
        if (m.getRoiInclusion().hasSegment()) disposeWhenSettled(m.getRoiInclusion().getSegment());
      }
      default -> {}
    }
  }

  /**
   * A message carrying a host deposit has ARRIVED: the deposit counts as begun, and a retirement
   * read after it waits until this message's naming is disposed of (§9.1). Taken here, on
   * the dispatch thread, so it is ordered against {@code RetireSegment}; a segment still pending
   * takes its claim when it installs, before the message is delivered.
   */
  private void claimCarried(HostMsg m) {
    SegmentRef ref = switch (m.getMsgCase()) {
      case DATA_VOLUME -> m.getDataVolume().hasSegment() ? m.getDataVolume().getSegment() : null;
      case MASK_VOXELS -> m.getMaskVoxels().hasSegment() ? m.getMaskVoxels().getSegment() : null;
      case ROI_INCLUSION -> m.getRoiInclusion().hasSegment() ? m.getRoiInclusion().getSegment() : null;
      case EXPORT_CHUNK -> m.getExportChunk().hasSegment() ? m.getExportChunk().getSegment() : null;
      default -> null;
    };
    if (ref == null) return;
    // the message's own ride-along install is applied after this, so its segment may have neither
    // half yet: it claims at install like a pending one
    vizi.plugin.v1.ViziPlugin.InstallSegment rideAlong = switch (m.getMsgCase()) {
      case DATA_VOLUME -> m.getDataVolume().hasInstall() ? m.getDataVolume().getInstall() : null;
      case EXPORT_CHUNK -> m.getExportChunk().hasInstall() ? m.getExportChunk().getInstall() : null;
      default -> null;
    };
    if (store.installed(ref.getSegmentId()).isPresent()) {
      store.claimDeposit(ref);
    } else if (store.query(ref.getSegmentId()).getState() == vizi.plugin.v1.ViziPlugin.GrantStateKind.GRANT_STATE_PENDING
        || (rideAlong != null && rideAlong.getSegmentId() == ref.getSegmentId())) {
      store.whenSettled(ref.getSegmentId(), settled -> {
        if (settled instanceof SegmentStore.Settlement.Installed) store.claimDeposit(ref);
      });
    }
  }

  private void disposeWhenSettled(SegmentRef ref) {
    store.whenSettled(ref.getSegmentId(), s -> {
      if (s instanceof SegmentStore.Settlement.Installed) store.disposeDeposit(ref);
    });
  }

  private void refuseOverBound(HostMsg m, Bounds.Breach breach) {
    disposeCarried(m);
    if (REQUESTS.contains(m.getMsgCase())) {
      replyError(m.getId(), Error.Kind.PROTOCOL, "refused: " + breach);
    } else if (breach.repeatedCount()) {
      violations.report(ViolationSite.VIOLATION_SITE_REPEATED_FIELD_OVERFLOW, m.getMsgCase() + " refused: " + breach, new byte[0]);
    } else {
      diag.note(m.getMsgCase() + " refused, not applied: " + breach);
    }
  }

  /** A kind this SDK predates (§11): a reply to a waiter, a refused request, or a push. */
  private void unknownKind(HostMsg m) {
    String id = m.getId();
    if (!id.isEmpty() && host.inFlight(id)) {
      host.deliver(m);
    } else if (!id.isEmpty()) {
      replyError(id, Error.Kind.UNSUPPORTED, "this plugin's SDK does not implement that message kind");
    } else {
      diag.count("an unknown push kind", "");
    }
  }

  // ---- arrangements ----

  private void onToolBinding(ToolBinding b) {
    Arrangements.ToolResult r = arrangements.onToolBinding(b);
    if (r.ack() != null) sendPush(PluginMsg.newBuilder().setToolBindingAck(r.ack()).build());
    if (manifest.contributions().tool(b.getToolId()).isEmpty()) {
      diag.note("ToolBinding names tool_id \"" + b.getToolId() + "\", which this plugin does not declare");
    }
    Doorbell d = doorbell;
    switch (r.change()) {
      case Arrangements.ToolChange.Activated a -> {
        if (a.isNew()) {
          Runnable hook = () -> app.toolActivated(a.binding());
          if (d == null || !d.deferInOrder(hook)) hook.run();
        }
      }
      case Arrangements.ToolChange.Retired x -> {
        Runnable hook = () -> app.toolDeactivated(x.binding());
        if (d == null || !d.deferInOrder(hook)) hook.run();
      }
      case Arrangements.ToolChange.None n -> {}
    }
  }

  private void onEpochChange(HostMsg m) {
    Arrangements.EpochResult r = arrangements.onEpochChange(m.getEpochChange());
    sendPush(PluginMsg.newBuilder().setEpochAck(r.ack()).build());
    for (Arrangements.PaneState p : r.installed()) app.epochInstalled(p);
  }

  /** The doorbell's lifecycle inputs, in the shape the arrangements need. */
  private final class DoorbellTransport implements Arrangements.Transport {
    @Override
    public boolean toolActivated(long handle, StringBuilder why) {
      Doorbell d = doorbell;
      if (d == null) return true;
      var r = d.toolActivated(handle);
      why.append(r.detail());
      return r.ok();
    }

    @Override
    public boolean toolRetired(long handle, StringBuilder why) {
      Doorbell d = doorbell;
      if (d == null) return true;
      var r = d.toolRetired(handle);
      why.append(r.detail());
      return r.ok();
    }

    @Override
    public boolean epochAcknowledged(int viewId, long epoch, StringBuilder why) {
      Doorbell d = doorbell;
      if (d == null) return true;
      var r = d.epochAcknowledged(viewId, epoch);
      why.append(r.detail());
      return r.ok();
    }
  }

  private final class ArrangementReporter implements Arrangements.Reporter {
    @Override
    public void violation(ViolationSite where, String detail) {
      violations.report(where, detail, new byte[0]);
    }

    @Override
    public void diagnostic(String text) {
      diag.note(text);
    }
  }

  private final class StoreSink implements SegmentStore.Sink {
    @Override
    public void push(PluginMsg msg) {
      outbound.send(msg);
    }

    @Override
    public void violation(ViolationSite where, String detail, byte[] record) {
      violations.report(where, detail, record);
    }

    @Override
    public void diagnostic(String text) {
      diag.note(text);
    }

    @Override
    public void trace(String text) {
      diag.trace(text);
    }
  }

  // ---- the doorbell's consumer (listener thread) ----

  private final class DoorbellConsumer implements Doorbell.Listener {
    @Override
    public void onFrame(Outcome.Frame f) {
      for (String d : f.diagnostics()) diag.count(d.replaceAll("[0-9]+", "#"), d);
      switch (f.disposition()) {
        case Outcome.Decoded dec -> {
          switch (dec.body()) {
            case Outcome.GrantBody g -> store.onGrant(g, f.raw());
            case Outcome.InputBody ib -> onInput(ib.event(), ib.implicitlyClosed(), f.raw());
            case Outcome.NotificationBody nb -> store.onHint(nb.hint(), f.raw(), arrangements::installedEpoch);
          }
        }
        case Outcome.Rejected r -> {
          if (Records.op(f.raw()) == Records.OP_M && r.where() == Violation.ZERO_ID) {
            violations.reportZeroIdGrant("M with segment_id 0", f.raw());
          } else {
            violations.report(site(r.where()), "a " + (char) Records.op(f.raw()) + " record rejected: " + r.where().wireName(), f.raw());
          }
        }
        case Outcome.GrantRejected g -> store.onGrantRejected(g.segmentId(), f.raw(), g.detail());
        case Outcome.UnknownOp u -> diag.count("unknown doorbell op", "0x" + Integer.toHexString(u.op()));
        case Outcome.Ignored i -> diag.count("ignored doorbell record: " + i.reason().replaceAll("[0-9]+", "#"), i.reason());
        case Outcome.FramingRejection fr -> {}
      }
    }

    @Override
    public void onCancellation(Outcome.Cancellation c) {
      app.cancelled(arrangements.binding(c.tool()), c);
    }

    @Override
    public void onTerminal(Outcome terminal) {}
  }

  private void onInput(InputRecord ev, long implicitlyClosed, byte[] raw) {
    Optional<ToolBinding> b = arrangements.binding(ev.tool());
    if (b.isEmpty()) {
      diag.count("an E record for a tool with no binding", Long.toString(ev.tool()));
      return;
    }
    Optional<Arrangements.PaneState> pane = arrangements.pane(ev.viewId());
    String unusable = Usability.problem(ev, pane);
    if (unusable != null) {
      violations.report(ViolationSite.VIOLATION_SITE_UNUSABLE_CONTEXT, unusable, raw);
    }
    app.input(b.get(), ev, implicitlyClosed, unusable == null, pane);
  }

  static ViolationSite site(Violation v) {
    return switch (v) {
      case ZERO_ID -> ViolationSite.VIOLATION_SITE_ZERO_ID;
      case SEQ_ON_OPEN -> ViolationSite.VIOLATION_SITE_SEQ_ON_OPEN;
      case POSITION_TAIL -> ViolationSite.VIOLATION_SITE_POSITION_TAIL;
      case NONFINITE_POSITION -> ViolationSite.VIOLATION_SITE_NONFINITE_POSITION;
      case MISS_WITH_PICKED -> ViolationSite.VIOLATION_SITE_MISS_WITH_PICKED;
      case INVALID_TARGET -> ViolationSite.VIOLATION_SITE_INVALID_TARGET;
      case INTERACTION_MISMATCH -> ViolationSite.VIOLATION_SITE_INTERACTION_MISMATCH;
      case UNKNOWN_TOOL -> ViolationSite.VIOLATION_SITE_UNKNOWN_TOOL;
      case EPOCH_MISMATCH -> ViolationSite.VIOLATION_SITE_EPOCH_MISMATCH;
      case UNUSABLE_CONTEXT -> ViolationSite.VIOLATION_SITE_UNUSABLE_CONTEXT;
    };
  }

  /** The transport's facts plus the session's (§8.13): absent where unknown, never zero. */
  private vizi.plugin.v1.ViziPlugin.DoorbellFault compose(DoorbellFault f) {
    var b = vizi.plugin.v1.ViziPlugin.DoorbellFault.newBuilder()
        .setFailureClass(failureClass(f.failureClass()))
        .setTransportClass(switch (f.transportClass()) {
          case SEQPACKET -> "seqpacket";
          case DGRAM -> "dgram";
          case STREAM -> "tcp";
        })
        .setDirection(f.direction())
        .setDetail(f.detail())
        .setPluginId(manifest.id())
        .setManifestSource(manifestSource)
        .setSdkVersion("vizi-sdk-java " + Identity.SDK_VERSION)
        .setProcessHealthyAtDetection(true)
        .setStreamHealthyAtDetection(streamOpen);
    f.capture().ifPresent(c -> b.setCapture(ByteString.copyFrom(c)));
    f.observedLen().ifPresent(b::setObservedLen);
    f.msgCtrunc().ifPresent(b::setMsgCtrunc);
    f.ordinal().ifPresent(b::setOrdinal);
    f.osError().ifPresent(b::setOsError);
    f.expectedCrc().ifPresent(v -> b.setExpectedCrc((int) v));
    f.observedCrc().ifPresent(v -> b.setObservedCrc((int) v));
    if (peerMajor >= 0) b.setPeerProtocolMajor(peerMajor).setPeerProtocolMinor(peerMinor);
    return b.build();
  }

  static DoorbellFailureClass failureClass(FaultClass c) {
    return switch (c) {
      case BAD_CRC -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_CRC;
      case BAD_LENGTH -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_LENGTH;
      case BAD_VER -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_VER;
      case BAD_FLAGS -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_FLAGS;
      case BAD_RESERVED -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_RESERVED;
      case BAD_PADDING -> DoorbellFailureClass.DOORBELL_FAILURE_BAD_PADDING;
      case CTRUNC -> DoorbellFailureClass.DOORBELL_FAILURE_CTRUNC;
      case WRONG_FD_COUNT -> DoorbellFailureClass.DOORBELL_FAILURE_WRONG_FD_COUNT;
      case RESERVED_OP -> DoorbellFailureClass.DOORBELL_FAILURE_RESERVED_OP;
      case ILLEGAL_DIRECTION -> DoorbellFailureClass.DOORBELL_FAILURE_ILLEGAL_DIRECTION;
      case SHORT_WRITE -> DoorbellFailureClass.DOORBELL_FAILURE_SHORT_WRITE;
      case AMBIGUOUS_SEND -> DoorbellFailureClass.DOORBELL_FAILURE_AMBIGUOUS_SEND;
      case RECV_ERROR -> DoorbellFailureClass.DOORBELL_FAILURE_RECV_ERROR;
    };
  }

  // ---- teardown ----

  private void onDoorbellShutdown(HostMsg m) {
    var ds = m.getDoorbellShutdown();
    diag.note("the host is closing the doorbell: " + ds.getReason() + (ds.hasFailureClass() ? " (" + ds.getFailureClass() + ")" : "")
        + " " + ds.getDetail());
    Doorbell d = doorbell;
    String stats = d == null ? "no doorbell" : d.describe();
    if (d != null) d.stop();
    sendPush(PluginMsg.newBuilder().setDoorbellFinalReport(DoorbellFinalReport.newBuilder()
        .setDetail("vizi-sdk-java " + Identity.SDK_VERSION + ": " + stats)).build());
    // the stream stays open: the session ends on the host's Shutdown (§8.13)
  }

  private void onShutdown(HostMsg m) {
    var s = m.getShutdown();
    if (inWindow) {
      diag.note("a second Shutdown inside the window changes nothing");
      return;
    }
    inWindow = true;
    long received = System.nanoTime();
    // §3.3: a request in the window may go unanswered, and vizi drops every data request once its
    // teardown begins. Waiting on one would hold the hook (and the exit) to the window's end, so
    // every pending request fails now, and so does any made later; then no push is delivered.
    host.endSession("shutdown began (" + s.getReason() + "): the host answers no request in its window");
    app.shutdownBegan();
    long deadlineMs = s.hasDeadlineMs() ? Integer.toUnsignedLong(s.getDeadlineMs()) : DEFAULT_SHUTDOWN_MS;
    if (!s.hasDeadlineMs()) {
      violations.report(ViolationSite.VIOLATION_SITE_MISSING_DEADLINE,
          "Shutdown carries no deadline_ms; adopting " + DEFAULT_SHUTDOWN_MS + " ms", new byte[0]);
    }
    Thread t = new Thread(() -> window(s.getReason(), s.getDetail(), received, deadlineMs), "vizi-shutdown");
    t.setDaemon(true);
    t.start();
  }

  private void window(ShutdownReason reason, String detail, long receivedNanos, long deadlineMs) {
    long end = receivedNanos + TimeUnit.MILLISECONDS.toNanos(Math.max(100, deadlineMs - 300));
    ShutdownReport.Outcome outcome;
    String note = "";
    Optional<Application.ShutdownHook> hook = app.shutdownHook();
    if (hook.isEmpty()) {
      outcome = ShutdownReport.Outcome.NO_HOOK;
    } else {
      CompletableFuture<Throwable> ran = new CompletableFuture<>();
      Thread h = new Thread(() -> {
        try {
          hook.get().run(reason, detail);
          ran.complete(null);
        } catch (Throwable e) {
          ran.complete(e);
        }
      }, "vizi-shutdown-hook");
      h.setDaemon(true);
      h.start();
      try {
        Throwable raised = ran.get(Math.max(1, (end - System.nanoTime()) / 1_000_000), TimeUnit.MILLISECONDS);
        if (raised == null) {
          outcome = ShutdownReport.Outcome.COMPLETED;
        } else {
          outcome = ShutdownReport.Outcome.RAISED;
          note = raised.toString();
        }
      } catch (java.util.concurrent.TimeoutException e) {
        outcome = ShutdownReport.Outcome.OVERRAN;
        note = "the hook was still running at the window's end";
      } catch (Exception e) {
        outcome = ShutdownReport.Outcome.RAISED;
        note = e.toString();
      }
    }
    // let work in flight finish, for what is left of the window
    while (inFlightWork.get() > 0 && System.nanoTime() < end) {
      try {
        Thread.sleep(10);
      } catch (InterruptedException e) {
        break;
      }
    }
    releaseOpenWork(end);
    // §8.16: the open background tasks end with the session, before its report
    work.endAll("stopped: the plugin is shutting down");
    violations.flush(true);
    sendPush(PluginMsg.newBuilder().setShutdownReport(ShutdownReport.newBuilder().setOutcome(outcome).setDetail(note)).build());
    try {
      outbound.drain(Math.max(50, (end - System.nanoTime()) / 1_000_000 + 250));
    } catch (InterruptedException ignored) {
      // closing anyway
    }
    closeStream("shutdown: " + reason + (note.isEmpty() ? "" : " (" + note + ")"));
  }

  /**
   * Inside the window, after in-flight work: aborts every unfinished export (never finalising it)
   * and closes the open reader, on the work worker whose state they are, within the window.
   */
  private void releaseOpenWork(long endNanos) {
    java.util.concurrent.Future<?> f;
    try {
      f = workWorker.submit(() -> {
        for (Map.Entry<String, ExportState> e : exports.entrySet()) {
          try {
            e.getValue().export.abort();
          } catch (RuntimeException ex) {
            diag.note("aborting export " + e.getKey() + " at shutdown raised " + ex);
          }
        }
        exports.clear();
        HeldListing held = heldListing;
        heldListing = null;
        if (held != null) closeListing(held.listing());
        OpenReaderState r = reader;
        reader = null;
        if (r != null) {
          try {
            r.dataset().close();
          } catch (RuntimeException ex) {
            diag.note("closing the reader at shutdown raised " + ex);
          }
        }
      });
    } catch (java.util.concurrent.RejectedExecutionException e) {
      return;
    }
    try {
      f.get(Math.max(1, (endNanos - System.nanoTime()) / 1_000_000), TimeUnit.MILLISECONDS);
    } catch (Exception e) {
      diag.note("releasing open exports and the reader did not finish inside the shutdown window: " + e);
    }
  }

  /** Closes our side of the stream; the close is the completion signal. */
  private void closeStream(String reason) {
    outbound.seal();
    synchronized (callLock) {
      ServerCall<HostMsg, PluginMsg> c = call;
      if (c != null) {
        try {
          c.close(Status.OK, new Metadata());
        } catch (RuntimeException e) {
          diag.note("closing the stream raised " + e);
        }
      }
    }
    endSession(reason);
  }

  /** Fails the session: say why on stderr, close the stream. */
  private void failSession(String why) {
    System.err.println("[vizi-sdk] " + why);
    outbound.seal();
    synchronized (callLock) {
      ServerCall<HostMsg, PluginMsg> c = call;
      if (c != null) {
        try {
          c.close(Status.FAILED_PRECONDITION.withDescription(why), new Metadata());
        } catch (RuntimeException ignored) {
          // already closed
        }
      }
    }
    endSession(why);
  }

  /** The session is over, however it ended; idempotent. */
  public void endSession(String reason) {
    if (ended.isDone()) return;
    streamOpen = false;
    work.endAll("stopped: the session ended"); // cancelled for their threads; sent only if the stream still takes it
    host.endSession(reason);
    shapes.close(); // a session's end ends its drags, with no message (§8.8)
    Doorbell d = doorbell;
    if (d != null) d.stop();
    violations.close();
    recheck.close();
    commandWorker.shutdownNow();
    panelWorker.shutdownNow();
    workWorker.shutdownNow();
    outbound.close();
    try {
      app.ended(reason);
    } catch (RuntimeException e) {
      diag.note("the ended hook raised " + e);
    }
    ended.complete(reason);
  }

  private void submit(ExecutorService worker, String what, Runnable r) {
    try {
      worker.execute(() -> {
        try {
          r.run();
        } catch (RuntimeException | java.lang.Error e) {
          diag.note(what + " raised " + e);
          if (vizi.plugin.javasdk.doorbell.Doorbell.fatal(e)) throw e;
        }
      });
    } catch (java.util.concurrent.RejectedExecutionException e) {
      diag.note(what + " arrived after the session ended");
    }
  }

  private static ExecutorService worker(String name) {
    return Executors.newSingleThreadExecutor(r -> {
      Thread t = new Thread(r, name);
      t.setDaemon(true);
      return t;
    });
  }

  /** The outbound queue's view of the call: gated until {@code Ready} has gone. */
  private final class CallChannel implements Outbound.Channel {
    @Override
    public boolean isReady() {
      synchronized (callLock) {
        ServerCall<HostMsg, PluginMsg> c = call;
        return c != null && !gated && c.isReady();
      }
    }

    @Override
    public void send(PluginMsg msg) {
      synchronized (callLock) {
        ServerCall<HostMsg, PluginMsg> c = call;
        if (c == null) throw new IllegalStateException("no call");
        c.sendMessage(msg);
      }
    }
  }

  /** Aggregated diagnostics: counted per condition, logged at powers of two. */
  static final class Diagnostics {
    private final Map<String, Long> counts = new ConcurrentHashMap<>();
    private final boolean trace = System.getenv("VIZI_SDK_TRACE") != null;
    /** Notes and counted conditions so far, for tests that pin "nothing was reported". */
    final java.util.concurrent.atomic.AtomicLong noted = new java.util.concurrent.atomic.AtomicLong();

    void note(String text) {
      noted.incrementAndGet();
      System.err.println("[vizi-sdk] " + text);
    }

    /** Only under {@code VIZI_SDK_TRACE}. */
    void trace(String text) {
      if (trace) System.err.println("[vizi-sdk] " + text);
    }

    void count(String condition, String example) {
      noted.incrementAndGet();
      long n = counts.merge(condition, 1L, Long::sum);
      if (trace || Long.bitCount(n) == 1) System.err.println("[vizi-sdk] " + condition + " (x" + n + ", e.g. " + example + ")");
    }
  }
}
