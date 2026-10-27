package vizi.plugin.javasdk;

import java.lang.foreign.Arena;
import java.lang.foreign.MemorySegment;
import java.time.Duration;
import java.util.List;
import java.util.Optional;
import java.util.OptionalDouble;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import vizi.plugin.javasdk.control.FeatureUnavailableException;
import vizi.plugin.javasdk.control.Features;
import vizi.plugin.javasdk.control.HostClient;
import vizi.plugin.javasdk.control.HostException;
import vizi.plugin.javasdk.control.ProtocolVersion;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.control.WrongThreadException;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.ActivateTool;
import vizi.plugin.v1.ViziPlugin.AppliedFilter;
import vizi.plugin.v1.ViziPlugin.AssetResult;
import vizi.plugin.v1.ViziPlugin.CompileLabelVolume;
import vizi.plugin.v1.ViziPlugin.ContributeRois;
import vizi.plugin.v1.ViziPlugin.DataVolume;
import vizi.plugin.v1.ViziPlugin.ExtendRoiPoses;
import vizi.plugin.v1.ViziPlugin.GetRois;
import vizi.plugin.v1.ViziPlugin.GetViewState;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.LabelVolumeCompiled;
import vizi.plugin.v1.ViziPlugin.MaskVoxels;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ReadDataVolume;
import vizi.plugin.v1.ViziPlugin.ReadMaskVoxels;
import vizi.plugin.v1.ViziPlugin.ReadRoiInclusion;
import vizi.plugin.v1.ViziPlugin.RequestAsset;
import vizi.plugin.v1.ViziPlugin.RequestStatus;
import vizi.plugin.v1.ViziPlugin.RequestStatusQuery;
import vizi.plugin.v1.ViziPlugin.RoiInclusion;
import vizi.plugin.v1.ViziPlugin.RoiList;
import vizi.plugin.v1.ViziPlugin.RoiPosesExtended;
import vizi.plugin.v1.ViziPlugin.RoisContributed;
import vizi.plugin.v1.ViziPlugin.SegmentRef;
import vizi.plugin.v1.ViziPlugin.SpacingProvenance;
import vizi.plugin.v1.ViziPlugin.ToolActivationChanged;
import vizi.plugin.v1.ViziPlugin.ViewRequest;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * The host client (control_plane_protocol.md §8.7–§8.9). Every call blocks until the host
 * answers; a {@code host_error} throws {@link HostException} carrying the host's kind, and a
 * silent host throws {@link TimeoutException} after the timeout (30 s unless given).
 *
 * <p>Calls are refused at once ({@link WrongThreadException}) on the doorbell listener and the
 * dispatch thread; make them from a command or a job. Voxels the host deposits are copied out
 * and released before a call returns: no loan ever reaches the caller.
 */
public final class Host {

  /** How long a person may take over a file picker (§8.9). */
  public static final Duration ASSET_TIMEOUT = Duration.ofMinutes(30);

  /** How long a deposit's segment may take to install after its reply arrived. */
  private static final Duration SETTLE = Duration.ofSeconds(2);

  private final Plugin.Runtime rt;

  Host(Plugin.Runtime rt) {
    this.rt = rt;
  }

  /** A volume the host deposited, with the reply that carried it. */
  public record Deposit<M>(Volume volume, M reply) {}

  private final java.util.concurrent.atomic.AtomicBoolean pickerWarned = new java.util.concurrent.atomic.AtomicBoolean();

  /**
   * A picker from a command is allowed and never refused (§8.9); its cost, the one
   * request slot held while a person browses, is said once per session.
   */
  private void pickerFromCommand(String what) {
    if (Session.onCommandWorker() && pickerWarned.compareAndSet(false, true)) {
      rt.log(what + " from a command: it waits for the person, and holds the session's one request slot (every later"
          + " command and read) meanwhile. A plugin that serves other work should ask from a job (ctx.jobs().start(...))"
          + " and return at once. (Said once per session.)");
    }
  }

  /**
   * The raw request path, for a message this facade has no method for. Inside a handler serving a
   * host request, a cancel of that request wakes the wait with {@link Cancelled}, which answers
   * {@code CANCELLED} (§8.16); the reply, when it comes, is disposed of.
   */
  public HostMsg call(PluginMsg.Builder request, Duration timeout) throws HostException, TimeoutException, InterruptedException {
    try {
      return client().call(request, timeout);
    } catch (vizi.plugin.javasdk.control.RequestCancelled c) {
      throw new Cancelled(c.getMessage() == null ? "canceled" : c.getMessage());
    }
  }

  /**
   * The channel a view shows first: {@code visible_channels[0]}, or 0 when none is shown. Never
   * read {@code ViewState.c} for this: it is deprecated and always 0, since vizi has no
   * active-channel control.
   */
  public static int shownChannel(ViewState view) {
    return view.getVisibleChannelsCount() == 0 ? 0 : view.getVisibleChannels(0);
  }

  /**
   * Every channel of the open dataset, in index order, with its name and whose it is
   * (§8.7); empty with no dataset open. Label a trace with any of them.
   */
  public static List<ChannelName> channels(ViewState view) {
    return ChannelName.of(view.getChannelsList());
  }

  /**
   * Which image of {@code dataset_uri} is open: the reader's series id, present ({@code ""}
   * included) when a series is open, empty for a file of one image (§8.2).
   */
  public static Optional<String> datasetSeries(ViewState view) {
    return view.hasDatasetSeries() ? Optional.of(view.getDatasetSeries()) : Optional.empty();
  }

  /**
   * {@code ViewState}'s voxel pitch, axis by axis (§8.7).
   *
   * <p><b>Check every axis you use before using its pitch: one known axis says nothing about the
   * rest.</b> Each of {@link #z()}, {@link #y()} and {@link #x()} is empty exactly where that axis
   * is not known, and positive and finite wherever it is present. A file that records X and Y but
   * not Z gives Z empty: scale what is known and leave the rest in voxels.
   *
   * <p>An axis is known when the unit is non-empty, the provenance is neither the display fallback
   * nor unspecified, and the host's value for that axis is positive and finite (an unrecorded axis
   * is NaN on the wire).
   *
   * <p>A tool's {@link PaneContext#spacing()} reads {@code Geometry}, which stays finite and calls
   * a partly recorded file {@code DISPLAY_FALLBACK}; the two agree whenever every axis or no axis is
   * known.
   *
   * @param z the z pitch in {@code unit}; empty exactly when z is not known
   * @param y the y pitch in {@code unit}; empty exactly when y is not known
   * @param x the x pitch in {@code unit}; empty exactly when x is not known
   * @param unit {@code spacing_unit}, {@code "um"}, or {@code ""} when no axis is known
   * @param provenance where the pitch came from
   */
  public record Pitch(OptionalDouble z, OptionalDouble y, OptionalDouble x, String unit, SpacingProvenance provenance) {
    /** The three axes, z first, each empty exactly where that axis is not known. */
    public List<OptionalDouble> zyx() {
      return List.of(z, y, x);
    }

    /**
     * Whether ANY axis is known: at least one, perhaps only one. It does not license using every
     * axis; check each axis you use ({@link #z()}, {@link #y()}, {@link #x()}), or {@link
     * #allKnown()}.
     */
    public boolean anyKnown() {
      return z.isPresent() || y.isPresent() || x.isPresent();
    }

    /** Whether ALL three axes are known, so every one of {@link #z()}, {@link #y()}, {@link #x()} is present. */
    public boolean allKnown() {
      return z.isPresent() && y.isPresent() && x.isPresent();
    }
  }

  /** The pitch of {@code view}, per axis; see {@link Pitch}. Nothing is known with no dataset open. */
  public static Pitch pitch(ViewState view) {
    String unit = view.getSpacingUnit();
    SpacingProvenance p = view.getSpacingProvenance();
    boolean believed = !unit.isEmpty() && p != SpacingProvenance.SPACING_PROVENANCE_DISPLAY_FALLBACK
        && p != SpacingProvenance.SPACING_PROVENANCE_UNSPECIFIED && view.getSpacingZyxCount() == 3;
    OptionalDouble[] axes = new OptionalDouble[3];
    for (int i = 0; i < 3; i++) {
      double d = believed ? view.getSpacingZyx(i) : Double.NaN;
      axes[i] = Double.isFinite(d) && d > 0 ? OptionalDouble.of(d) : OptionalDouble.empty(); // a 0 or negative pitch is no measurement
    }
    return new Pitch(axes[0], axes[1], axes[2], unit, p);
  }

  /**
   * Which filter's output a read returned (§8.7): present when the person shares a filter's output
   * with plugins and the read was not {@code raw}; empty for the file's own voxels. A read never
   * infers this from {@code ViewState.filter}, which says what the views show.
   */
  public static Optional<AppliedFilter> filterOf(DataVolume reply) {
    return reply.hasFilter() ? Optional.of(reply.getFilter()) : Optional.empty();
  }

  /**
   * The host's view; for the channel it shows, see {@link #shownChannel}, and for the channels'
   * names {@link #channels}. An open dataset is the pair {@code dataset_uri} and {@link
   * #datasetSeries}, presence included: two images of one file share a URI.
   */
  public ViewState viewState() throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setGetViewState(GetViewState.getDefaultInstance()), HostClient.DEFAULT_TIMEOUT).getViewState();
  }

  /*
   * Reads (§8.7). A read is the source unless the person shares a filter's output with plugins; then
   * it is that output, and filterOf(deposit.reply()) names the filter. A raw read asks for the file's
   * own voxels whatever is shared: its reply carries no filter, and ViewState.filter still says what
   * the views show, for a plugin that records it.
   */

  /**
   * The whole level-0 volume at {@code (t, c)}: the source, or the filter's output the person shares
   * with plugins ({@link #filterOf} on the reply says which). For the file's own voxels whatever is
   * shared, use {@link #readRawVolume(int, int)}.
   */
  public Deposit<DataVolume> readVolume(int t, int c) throws HostException, TimeoutException, InterruptedException {
    return readVolume(ReadDataVolume.newBuilder().setT(t).setC(c).build());
  }

  /** The whole volume at {@code (t, c)} on pyramid level {@code level}. */
  public Deposit<DataVolume> readVolume(int t, int c, int level) throws HostException, TimeoutException, InterruptedException {
    return readVolume(ReadDataVolume.newBuilder().setT(t).setC(c).setLevel(level).build());
  }

  /**
   * The file's own voxels at {@code (t, c)}, level 0, whatever filter the person shares with
   * plugins ({@code ReadDataVolume.raw}): for a plugin that must measure the source. The
   * reply carries no {@code filter}; {@code ViewState.filter} still names what the views show.
   */
  public Deposit<DataVolume> readRawVolume(int t, int c) throws HostException, TimeoutException, InterruptedException {
    return readVolume(ReadDataVolume.newBuilder().setT(t).setC(c).setRaw(true).build());
  }

  /** {@link #readRawVolume(int, int)} on pyramid level {@code level}. */
  public Deposit<DataVolume> readRawVolume(int t, int c, int level) throws HostException, TimeoutException, InterruptedException {
    return readVolume(ReadDataVolume.newBuilder().setT(t).setC(c).setLevel(level).setRaw(true).build());
  }

  /**
   * Any read: a level, a box ({@code roi_min_zyx}/{@code roi_max_zyx}), and {@code raw} for the
   * file's own voxels whatever is shared. {@link #filterOf} on the reply says whose voxels came back.
   */
  public Deposit<DataVolume> readVolume(ReadDataVolume request) throws HostException, TimeoutException, InterruptedException {
    DataVolume dv = call(PluginMsg.newBuilder().setReadDataVolume(request), HostClient.DEFAULT_TIMEOUT).getDataVolume();
    if (!dv.hasSegment()) throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, "the DataVolume carried no segment");
    return new Deposit<>(copyOut(dv.getSegment()), dv);
  }

  /** A native region's mask, as u8 voxels placed at {@code reply().getOriginZyxList()}. */
  public Deposit<MaskVoxels> readMaskVoxels(String regionId) throws HostException, TimeoutException, InterruptedException {
    MaskVoxels mv = call(PluginMsg.newBuilder().setReadMaskVoxels(ReadMaskVoxels.newBuilder().setRegionId(regionId)),
        HostClient.DEFAULT_TIMEOUT).getMaskVoxels();
    if (!mv.hasSegment()) throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, "the MaskVoxels carried no segment");
    return new Deposit<>(copyOut(mv.getSegment()), mv);
  }

  /**
   * A region's inclusion at {@code t} (§8.7): the reply, and its mask when there is one (bbox-local
   * u8 0/1, placed at {@code reply.getOriginZyxList()}). No mask means hidden at {@code t} ({@code
   * present} false) or covering no voxel ({@code present}, count 0). The host's {@code
   * voxel_count} is checked against the mask; a disagreement is a {@link HostException}.
   */
  public record Inclusion(Optional<Volume> mask, RoiInclusion reply) {
    public boolean present() {
      return reply.getPresent();
    }

    public long voxelCount() {
      return reply.getVoxelCount();
    }
  }

  public Inclusion readRoiInclusion(ReadRoiInclusion request) throws HostException, TimeoutException, InterruptedException {
    RoiInclusion ri = call(PluginMsg.newBuilder().setReadRoiInclusion(request), HostClient.DEFAULT_TIMEOUT).getRoiInclusion();
    if (!ri.hasSegment()) {
      if (ri.getVoxelCount() != 0) {
        throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, ri.getPresent()
            ? "the host counts " + Long.toUnsignedString(ri.getVoxelCount()) + " included voxels but sent no mask"
            : "the host says the region is not present at t " + ri.getT() + " yet counts " + Long.toUnsignedString(ri.getVoxelCount())
                + " included voxels");
      }
      return new Inclusion(Optional.empty(), ri);
    }
    Volume mask = copyOut(ri.getSegment()); // disposed of whatever follows
    if (!ri.getPresent()) {
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL,
          "the host sent a mask for a region it says is not present at t " + ri.getT());
    }
    long included = 0;
    for (long i = 0; i < mask.samples(); i++) if (mask.get(i) != 0) included++;
    if (included != ri.getVoxelCount()) {
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, "the host counts " + Long.toUnsignedString(ri.getVoxelCount())
          + " included voxels, and the mask it sent includes " + included);
    }
    return new Inclusion(Optional.of(mask), ri);
  }

  public RoiList rois() throws HostException, TimeoutException, InterruptedException {
    return rois(GetRois.getDefaultInstance());
  }

  /** Just these regions: an id that no longer exists is simply absent from the reply. */
  public RoiList rois(java.util.Collection<String> regionIds) throws HostException, TimeoutException, InterruptedException {
    return rois(GetRois.newBuilder().addAllRegionIds(regionIds).build());
  }

  public RoiList rois(GetRois request) throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setGetRois(request), HostClient.DEFAULT_TIMEOUT).getRois();
  }

  public RoisContributed contributeRois(ContributeRois request) throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setContributeRois(request), HostClient.DEFAULT_TIMEOUT).getRoisContributed();
  }

  /**
   * Contributes regions (built with {@link Rois#contribution}); {@code replaceAll} first removes
   * every region this plugin contributed before.
   */
  public RoisContributed contributeRois(boolean replaceAll, vizi.plugin.v1.ViziPlugin.RoiContribution... rois)
      throws HostException, TimeoutException, InterruptedException {
    return contributeRois(ContributeRois.newBuilder().setReplaceAll(replaceAll).addAllRois(java.util.List.of(rois)).build());
  }

  public RoiPosesExtended extendRoiPoses(ExtendRoiPoses request) throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setExtendRoiPoses(request), HostClient.DEFAULT_TIMEOUT).getRoiPosesExtended();
  }

  /**
   * Compiles a label volume item (built with {@link Item#labels}) into native regions. Its
   * geometry is lent afresh from {@code overlay}'s arena for this request alone, so no overlay
   * push can free bytes the compile still names.
   */
  public LabelVolumeCompiled compileLabelVolume(Overlay overlay, Item item, boolean replaceAll)
      throws HostException, TimeoutException, InterruptedException {
    if (item.kind != Item.Kind.LABEL_VOLUME) throw new IllegalArgumentException("item " + item.id + " is not a label volume");
    Session s = rt.session();
    Segment seg;
    try {
      seg = s.store().live("O", overlay.id());
    } catch (SegmentStore.UnavailableException e) {
      throw new IllegalStateException("overlay " + overlay.id() + " has no arena yet: " + e.getMessage(), e);
    }
    try (Loans loans = new Loans(s)) {
      vizi.plugin.v1.ViziPlugin.OverlayItem wire;
      try {
        wire = Overlay.materialize(item, loans, seg, false, 1); // this copy is named by the compile alone
      } catch (Loans.Retired retired) {
        throw new IllegalStateException("overlay " + overlay.id() + "'s arena was retired with nothing superseding it: the compile"
            + " was not sent; retry once the host grants the overlay again", retired);
      }
      var req = PluginMsg.newBuilder().setCompileLabelVolume(
          CompileLabelVolume.newBuilder().setOverlayId(overlay.id()).setItem(wire).setReplaceAll(replaceAll));
      HostClient c = client();
      try {
        HostMsg reply = c.call(req, HostClient.DEFAULT_TIMEOUT);
        loans.admitted();
        loans.sent();
        return reply.getLabelVolumeCompiled();
      } catch (HostException | TimeoutException | InterruptedException e) {
        // it went (an interrupt comes only while waiting for the reply): the host releases what it
        // names, so reclaiming the range would hand bytes the host may still read to the next loan
        loans.admitted();
        loans.sent();
        throw e;
      } catch (vizi.plugin.javasdk.control.RequestCancelled e) {
        loans.admitted(); // as above: it went
        loans.sent();
        throw new Cancelled(e.getMessage() == null ? "canceled" : e.getMessage());
      }
    }
  }

  /** Engages ({@code active}) or disengages a tool this plugin declares. */
  public ToolActivationChanged activateTool(String toolId, boolean active)
      throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setActivateTool(ActivateTool.newBuilder().setToolId(toolId).setDeactivate(!active)),
        HostClient.DEFAULT_TIMEOUT).getToolActivationChanged();
  }

  /**
   * A declared asset's path, prompting the user if the host must (§8.9). It works from a command,
   * which then waits while the person browses. That command holds the session's one request slot
   * meanwhile, so a plugin that also serves reads or other commands should ask from a job
   * ({@code ctx.jobs().start(...)}) and let the command return at once; the SDK says so once per
   * session when a command asks.
   */
  public AssetResult requestAsset(String slot, boolean forceReprompt) throws HostException, TimeoutException, InterruptedException {
    pickerFromCommand("RequestAsset");
    return call(PluginMsg.newBuilder().setRequestAsset(RequestAsset.newBuilder().setSlot(slot).setForceReprompt(forceReprompt)),
        ASSET_TIMEOUT).getAssetResult();
  }

  /**
   * A path to save a file to, chosen by the person in the host's save dialog (§8.9), or empty
   * when they canceled. {@code suggestedName} is a file name, never a path; {@code extension}
   * (without the dot, {@code ""} for any) is appended when the person leaves it off. The host
   * creates nothing: the plugin writes the file. It waits on a person, and, like {@link
   * #requestAsset}, it works from a command, though a job is the better place for it in a plugin
   * that serves other work.
   */
  public Optional<java.nio.file.Path> requestSavePath(String suggestedName, String extension, String title)
      throws HostException, TimeoutException, InterruptedException {
    pickerFromCommand("RequestSavePath");
    var r = call(PluginMsg.newBuilder().setRequestSavePath(vizi.plugin.v1.ViziPlugin.RequestSavePath.newBuilder()
        .setSuggestedName(suggestedName).setExtension(extension).setTitle(title)), ASSET_TIMEOUT).getSavePathResult();
    return r.getDeclined() || r.getPath().isEmpty() ? Optional.empty() : Optional.of(java.nio.file.Path.of(r.getPath()));
  }

  /**
   * The table vizi renders colormap {@code name} with (§8.4): 256 RGBA entries of u8, 1024
   * bytes, row {@code i} at {@code [4i, 4i + 4)}. An unknown name is {@code NOT_FOUND}.
   */
  public byte[] colormap(String name) throws HostException, TimeoutException, InterruptedException {
    var t = call(PluginMsg.newBuilder().setGetColormap(vizi.plugin.v1.ViziPlugin.GetColormap.newBuilder().setName(name)),
        HostClient.DEFAULT_TIMEOUT).getColormapTable();
    if (t.getRgba().size() != 1024) {
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL,
          "colormap " + name + " came back as " + t.getRgba().size() + " bytes, not 256 RGBA entries (1024)");
    }
    return t.getRgba().toByteArray();
  }

  /** How long a set-view request may wait for its answer; a cause is stale after 2 s anyway. */
  public static final Duration VIEW_TIMEOUT = Duration.ofSeconds(5);

  /**
   * Asks the host to move the view (§8.15), the raw form of {@link ViewCause#move} and {@link
   * ToolContext#moveView}. A request without exactly one cause, or a decline that names targets,
   * or neither, is refused here ({@link IllegalArgumentException}), before the host would call it
   * {@code INVALID}. The host's refusals come back as a {@link ViewResult}, never thrown.
   */
  public ViewResult requestView(ViewRequest request) throws HostException, TimeoutException, InterruptedException {
    checkView(request);
    return ViewResult.of(call(PluginMsg.newBuilder().setViewRequest(request), VIEW_TIMEOUT).getViewRequestResult());
  }

  /** {@link #requestView} without waiting: allowed on every thread, the doorbell listener included. */
  public CompletableFuture<ViewResult> requestViewLater(ViewRequest request) {
    checkView(request);
    return client().submit(PluginMsg.newBuilder().setViewRequest(request), VIEW_TIMEOUT)
        .thenApply(m -> ViewResult.of(m.getViewRequestResult()));
  }

  private static void checkView(ViewRequest r) {
    boolean cause = r.getCause() != 0;
    if (cause == r.hasToolCause()) {
      throw new IllegalArgumentException(cause
          ? "a view request names one cause, not both a cause and a tool cause"
          : "a view request needs a cause: the plugin moves the view only when the person asked it to (no cause, no move)");
    }
    boolean targets = r.hasT() || r.hasZ() || r.hasSelectRegion() || r.hasFrameRegion() || r.hasLookAt() || r.hasOrbitPoint();
    if (r.getDecline() && targets) throw new IllegalArgumentException("a decline names no target");
    if (!r.getDecline() && !targets) throw new IllegalArgumentException("a view request names a target, or declines");
  }

  /** Whether request {@code subjectId} is still the host's (§8.9). */
  public RequestStatus requestStatus(String subjectId) throws HostException, TimeoutException, InterruptedException {
    return call(PluginMsg.newBuilder().setRequestStatusQuery(RequestStatusQuery.newBuilder().setSubjectId(subjectId)),
        HostClient.DEFAULT_TIMEOUT).getRequestStatus();
  }

  /** The host's identity string, from {@code Hello}. */
  public String identity() {
    return rt.session().hostIdentity();
  }

  /**
   * The protocol version the host's {@code Hello} announced, {@code major.minor}; empty before the
   * handshake. A message newer than the protocol this SDK speaks may be sent only when the host's
   * minor says it exists ({@link ProtocolVersion#includes}).
   */
  public Optional<ProtocolVersion> protocol() {
    Session s = rt.sessionOrNull();
    return s == null ? Optional.empty() : s.hostProtocol();
  }

  /**
   * The session's negotiated protocol: the version this plugin's {@code Ready} announced, the host's
   * {@code Hello} minor clamped to this SDK's. Empty before the handshake. Gate a feature on it,
   * through {@link #supports}, rather than on {@link #protocol}.
   */
  public Optional<ProtocolVersion> sessionProtocol() {
    Session s = rt.sessionOrNull();
    return s == null ? Optional.empty() : s.sessionProtocol();
  }

  /**
   * Whether this session may use {@code feature} (a {@link Features} name): the name is one this SDK
   * knows and the negotiated minor includes it. False before the handshake.
   */
  public boolean supports(String feature) {
    return Features.unavailable(feature, sessionProtocol().orElse(null)) == null;
  }

  /**
   * {@link #supports} as a call-site guard: returns when the session may use {@code feature}, and
   * otherwise throws, naming the feature, the minor it needs and the session's minor. Call it before
   * a newer feature and fall back on the exception.
   *
   * @throws FeatureUnavailableException when the session may not use it
   */
  public void require(String feature) {
    String why = Features.unavailable(feature, sessionProtocol().orElse(null));
    if (why != null) throw new FeatureUnavailableException(feature, why);
  }

  private HostClient client() {
    return rt.session().host();
  }

  /** Copies a deposit out, releasing this naming of it whatever happens. */
  private Volume copyOut(SegmentRef ref) throws HostException, InterruptedException {
    Session s = rt.session();
    SegmentStore store = s.store();
    CompletableFuture<SegmentStore.Settlement> settled = new CompletableFuture<>();
    store.whenSettled(ref.getSegmentId(), settled::complete);
    SegmentStore.Settlement how;
    try {
      how = settled.get(SETTLE.toMillis(), TimeUnit.MILLISECONDS);
    } catch (TimeoutException e) {
      // it may still install: release this naming when it does
      store.whenSettled(ref.getSegmentId(), st -> {
        if (st instanceof SegmentStore.Settlement.Installed) store.disposeDeposit(ref);
      });
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL,
          "the deposit's segment " + Long.toUnsignedString(ref.getSegmentId()) + " did not install within " + SETTLE.toMillis() + " ms");
    } catch (ExecutionException e) {
      throw new IllegalStateException(e.getCause());
    }
    if (!(how instanceof SegmentStore.Settlement.Installed)) {
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL,
          "the deposit's segment " + Long.toUnsignedString(ref.getSegmentId()) + " is not usable: " + how);
    }
    Optional<DType> dt = DType.of(ref.getDtype());
    List<Integer> shape = ref.getShapeZyxList();
    if (dt.isEmpty() || shape.size() != 3) {
      store.disposeDeposit(ref);
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, "the deposit's shape or dtype is not one this SDK reads");
    }
    long want = (long) shape.get(0) * shape.get(1) * shape.get(2) * dt.get().size;
    if (want != ref.getLen()) {
      store.disposeDeposit(ref);
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL,
          "the deposit is " + ref.getLen() + " bytes; its shape and dtype say " + want);
    }
    MemorySegment dst = Arena.ofAuto().allocate(Math.max(1, want), 8).asSlice(0, want);
    try {
      store.readDeposit(ref, dst);
    } catch (SegmentStore.DepositException e) {
      store.disposeDeposit(ref);
      throw new HostException(vizi.plugin.v1.ViziPlugin.Error.Kind.INTERNAL, "the deposit could not be read: " + e.getMessage());
    }
    return Volume.of(shape.get(0), shape.get(1), shape.get(2), dt.get(), dst);
  }
}
