package vizi.plugin.javasdk;

import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.OptionalInt;
import org.jspecify.annotations.Nullable;
import vizi.plugin.javasdk.control.Session;
import vizi.plugin.javasdk.manifest.OverlayDecl;
import vizi.plugin.javasdk.memory.Segment;
import vizi.plugin.javasdk.memory.SegmentStore;
import vizi.plugin.v1.ViziPlugin.ChunkWant;
import vizi.plugin.v1.ViziPlugin.ChunksWanted;
import vizi.plugin.v1.ViziPlugin.GeomBuffer;
import vizi.plugin.v1.ViziPlugin.OverlayChunk;
import vizi.plugin.v1.ViziPlugin.OverlayLabelTable;
import vizi.plugin.v1.ViziPlugin.OverlayStyle;

/**
 * A chunked overlay (control_plane_protocol.md §5.7): a time series of label volumes, one
 * {@code (level, t)} volume at a time. The HOST names the volumes it wants to hold; the SDK calls
 * the plugin's {@link Source} once per want per continuous membership of that set, in the host's
 * order, on a thread of the overlay's own, and publishes each answer into the overlay's arena.
 *
 * <ul>
 *   <li>A want that leaves the set while its answer is computing is forgotten, and its answer
 *       discarded; one that returns is served again.
 *   <li>{@link #invalidate()} says the plugin's results changed: every current want is served
 *       again.
 *   <li>A full arena DEFERS the chunk (no shortfall: the host is about to release the chunk
 *       before it); the SDK keeps the answer and retries.
 * </ul>
 */
public final class ChunkedOverlay {

  /** Serves one want. Return {@link Chunk#empty()} for a volume with no labels. */
  @FunctionalInterface
  public interface Source {
    Chunk serve(Context ctx, Want want) throws Exception;
  }

  /** A volume the host wants: its pyramid level, and {@code t} (absent for a static overlay). */
  public record Want(int level, OptionalInt t) {}

  /**
   * An answer: {@code labels} (u8 or u16, on the wanted level's lattice) with its {@code [0,0,0]}
   * at level-L voxel {@code (z0, y0, x0)}. Answer bbox-tight: the host zero-fills the rest.
   */
  public record Chunk(@Nullable Volume labels, int z0, int y0, int x0) {
    public Chunk {
      if (labels != null && labels.dtype() != DType.U8 && labels.dtype() != DType.U16) {
        throw new IllegalArgumentException("chunk labels are u8 or u16, not " + labels.dtype());
      }
    }

    public static Chunk of(Volume labels, int z0, int y0, int x0) {
      return new Chunk(Objects.requireNonNull(labels, "labels"), z0, y0, x0);
    }

    /** No labels: the empty state, not a removal. */
    public static Chunk empty() {
      return new Chunk(null, 0, 0, 0);
    }
  }

  /** What a source can reach: it runs on its own thread, so host requests are fine here. */
  public final class Context {
    public Host host() {
      return rt.host();
    }

    public ChunkedOverlay overlay() {
      return ChunkedOverlay.this;
    }

    public Overlay overlay(String id) {
      return rt.overlay(id);
    }

    public ChunkedOverlay chunked(String id) {
      return rt.chunked(id);
    }

    public Panel panel(String id) {
      return rt.panel(id);
    }

    public void log(String text) {
      rt.log(text);
    }
  }

  /** The overlay's label table: the LUT, names, visibility and style of every chunk. */
  public static final class LabelTable {
    final byte[] lut;
    final Map<Integer, String> names = new LinkedHashMap<>();
    final Map<Integer, Boolean> visible = new LinkedHashMap<>();
    final OverlayStyle.Builder style = OverlayStyle.newBuilder();

    private LabelTable(byte[] lut) {
      if (lut.length != 256 * 4) throw new IllegalArgumentException("the LUT is (256, 4) RGBA: 1024 bytes");
      this.lut = lut.clone();
    }

    /** {@code lut}: (256, 4) RGBA; row 0 is the background, label {@code id ≥ 1} uses row {@code 1 + (id − 1) mod 255}. */
    public static LabelTable of(byte[] lut) {
      return new LabelTable(lut);
    }

    public LabelTable name(int labelId, String name) {
      names.put(labelId, name);
      return this;
    }

    /**
     * Whether a label becomes a visible annotation region when the host compiles the overlay
     * ({@code label_visible}, §8.8). It governs the compile only: the host draws voxels straight
     * from the LUT, so to hide a label on screen also give its LUT row alpha 0.
     */
    public LabelTable visible(int labelId, boolean v) {
      visible.put(labelId, v);
      return this;
    }

    public LabelTable opacity(double o) {
      style.setOpacity(o);
      return this;
    }

    public LabelTable hidden(boolean h) {
      style.setHidden(h);
      return this;
    }

    /**
     * Higher draws later, on top. In 3D a label overlay that encloses another hides it unless the
     * enclosing one has the <em>lower</em> priority; opacity does not rescue it, since a low
     * per-voxel opacity accumulated through a thick region is opaque.
     */
    public LabelTable drawPriority(int p) {
      style.setDrawPriority(p);
      return this;
    }
  }

  private record Key(int level, long t) {} // t = -1: absent, never t = 0

  private record Served(long membership, long generation) {}

  private final String id;
  private final Plugin.Runtime rt;
  private final Source source;
  private final Context ctx = new Context();
  private final Object lock = new Object();
  private List<Want> wants = List.of();
  private final Map<Key, Long> membership = new HashMap<>();
  private final Map<Key, Served> served = new HashMap<>();
  private long generation;
  private long stamps;
  private boolean stopped;
  private @Nullable Thread thread;

  ChunkedOverlay(OverlayDecl decl, Plugin.Runtime rt, Source source) {
    if (!(decl.kind() instanceof OverlayDecl.Chunked)) throw new IllegalArgumentException("overlay " + decl.id() + " is not declared chunked");
    this.id = decl.id();
    this.rt = rt;
    this.source = source;
  }

  public String id() {
    return id;
  }

  /** The host's current wants, in its priority order. */
  public List<Want> wants() {
    synchronized (lock) {
      return wants;
    }
  }

  /** The plugin's results changed: serve every current want again. */
  public void invalidate() {
    synchronized (lock) {
      generation++;
      lock.notifyAll();
    }
  }

  /** Sends the label table (coalescing: the newest table stands). */
  public void labelTable(LabelTable table) {
    Session s = rt.session();
    OverlayLabelTable.Builder t = OverlayLabelTable.newBuilder()
        .setOverlayId(id)
        .setColormap(GeomBuffer.newBuilder().addShape(256).addShape(4).setDtype(DType.U8.wire())
            .setInline(com.google.protobuf.ByteString.copyFrom(table.lut)))
        .putAllLabelNames(table.names)
        .putAllLabelVisible(table.visible)
        .setStyle(table.style);
    s.outbound().labelTable(t.build(), vizi.plugin.javasdk.control.Outbound.Loans.NONE);
  }

  // ---- the host's side ----

  /** A restatement of the set: reconciled at once, under the lock. */
  void onWanted(ChunksWanted cw) {
    synchronized (lock) {
      List<Want> next = new ArrayList<>();
      Map<Key, Long> kept = new HashMap<>();
      for (ChunkWant w : cw.getWantsList()) {
        Key k = key(w.getLevel(), w.hasT() ? Integer.toUnsignedLong(w.getT()) : -1);
        if (kept.containsKey(k)) continue;
        Long m = membership.get(k);
        kept.put(k, m != null ? m : ++stamps);
        next.add(new Want(w.getLevel(), w.hasT() ? OptionalInt.of(w.getT()) : OptionalInt.empty()));
      }
      membership.clear();
      membership.putAll(kept);
      served.keySet().retainAll(kept.keySet());
      wants = List.copyOf(next);
      lock.notifyAll();
    }
  }

  private static Key key(int level, long t) {
    return new Key(level, t);
  }

  private static Key key(Want w) {
    return new Key(w.level(), w.t().isPresent() ? Integer.toUnsignedLong(w.t().getAsInt()) : -1);
  }

  void start() {
    synchronized (lock) {
      if (thread != null || stopped) return;
      Thread t = new Thread(this::loop, "vizi-chunks-" + id);
      t.setDaemon(true);
      thread = t;
      t.start();
    }
  }

  void stop() {
    Thread t;
    synchronized (lock) {
      stopped = true;
      t = thread;
      lock.notifyAll();
    }
    if (t != null) t.interrupt();
  }

  private record Job(Want want, Key key, long membership, long generation) {}

  private @Nullable Job next() throws InterruptedException {
    synchronized (lock) {
      while (!stopped) {
        for (Want w : wants) {
          Key k = key(w);
          Long m = membership.get(k);
          if (m == null) continue;
          Served s = served.get(k);
          if (s == null || s.membership() != m || s.generation() != generation) return new Job(w, k, m, generation);
        }
        lock.wait();
      }
      return null;
    }
  }

  private boolean current(Job j) {
    synchronized (lock) {
      Long m = membership.get(j.key());
      return !stopped && m != null && m == j.membership() && generation == j.generation();
    }
  }

  private void markServed(Job j) {
    synchronized (lock) {
      Long m = membership.get(j.key());
      if (m != null && m == j.membership()) served.put(j.key(), new Served(j.membership(), j.generation()));
    }
  }

  private void loop() {
    while (true) {
      Job j;
      try {
        j = next();
      } catch (InterruptedException e) {
        return;
      }
      if (j == null) return;
      Chunk c;
      try {
        c = source.serve(ctx, j.want());
        Objects.requireNonNull(c, "a chunk source returned null; return Chunk.empty() for no labels");
      } catch (InterruptedException e) {
        if (stoppedNow()) return;
        continue;
      } catch (vizi.plugin.javasdk.control.SessionEndedException e) {
        // The source's request met the session's end (Shutdown, or the host gone): the host's
        // doing, not the source's, and no request will be answered again -- a quiet end, as for
        // a push handler or a Follow driver.
        return;
      } catch (Exception | Error e) {
        rt.log("chunked overlay " + id + ": serving " + j.want() + " failed: " + e);
        markServed(j); // not retried until the results change or the want returns
        continue;
      }
      try {
        publish(j, c);
      } catch (InterruptedException e) {
        if (stoppedNow()) return;
      } catch (RuntimeException e) {
        rt.log("chunked overlay " + id + ": publishing " + j.want() + " failed: " + e);
        markServed(j);
      }
    }
  }

  private boolean stoppedNow() {
    synchronized (lock) {
      return stopped;
    }
  }

  /** Publishes, deferring while the arena is full or not yet installed; drops a stale answer. */
  private void publish(Job j, Chunk c) throws InterruptedException {
    while (current(j)) {
      Session s = rt.session();
      OverlayChunk.Builder b = OverlayChunk.newBuilder().setOverlayId(id).setLevel(j.want().level());
      j.want().t().ifPresent(b::setT);
      Volume v = c.labels();
      if (v == null) {
        s.outbound().chunk(b.build(), vizi.plugin.javasdk.control.Outbound.Loans.NONE);
        markServed(j);
        return;
      }
      Segment seg;
      try {
        seg = s.store().live("O", id);
      } catch (SegmentStore.UnavailableException e) {
        Thread.sleep(20); // the arena is being (re)granted: nothing is named until it installs
        continue;
      }
      try (Loans loans = new Loans(s)) {
        GeomBuffer g;
        try {
          g = loans.tryGeom(v.data(), List.of(v.z(), v.y(), v.x()), v.dtype(), seg);
        } catch (Loans.Retired retired) {
          Thread.sleep(20); // the arena is being replaced: look it up again
          continue;
        }
        if (g == null) {
          Thread.sleep(10); // deferred: the host releases the chunk before it
          continue;
        }
        b.setLabels(g).addPositionZyx(c.z0()).addPositionZyx(c.y0()).addPositionZyx(c.x0());
        s.outbound().chunk(b.build(), loans.forQueue());
        loans.admitted();
      }
      markServed(j);
      return;
    }
  }
}
