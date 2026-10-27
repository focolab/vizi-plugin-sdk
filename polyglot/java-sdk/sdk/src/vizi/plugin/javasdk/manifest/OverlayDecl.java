package vizi.plugin.javasdk.manifest;

import java.util.Objects;
import java.util.Optional;
import java.util.OptionalInt;

/**
 * An overlay: "I draw into the views" (§3.5). Either a drawn overlay with a loaned arena of {@code
 * segment_mib} MiB, or a chunked overlay whose arena the host sizes from the dataset.
 *
 * @param id unique among overlays; the {@code subject} of its {@code O} segment
 * @param name the display name ({@link #id()} when the manifest gives none)
 * @param kind drawn or chunked
 */
public record OverlayDecl(String id, String name, Kind kind) {
  /** The arena size when the manifest gives none (§3.5). */
  public static final int DEFAULT_SEGMENT_MIB = 32;

  public static final int MIN_SEGMENT_MIB = 1;
  public static final int MAX_SEGMENT_MIB = 512;

  public OverlayDecl {
    Objects.requireNonNull(id, "id");
    Objects.requireNonNull(name, "name");
    Objects.requireNonNull(kind, "kind");
  }

  /** What an overlay carries. */
  public sealed interface Kind permits Drawn, Chunked {}

  /** A draw list, pushed whole, with a loaned arena of {@code segmentMib} MiB. */
  public record Drawn(int segmentMib) implements Kind {}

  /**
   * One label volume per (level, t) the host asks for ({@code control_plane_protocol.md} §5.7).
   */
  public record Chunked(TimeMode time) implements Kind {
    public Chunked {
      Objects.requireNonNull(time, "time");
    }
  }

  /** Whether a chunked overlay's volumes vary with t (§3.5). */
  public enum TimeMode implements WireName {
    /** The host asks per t (the default). */
    PER_T("per_t"),
    /** One volume per level serves every timepoint (a fixed atlas). */
    STATIC("static");

    private final String wire;

    TimeMode(String wire) {
      this.wire = wire;
    }

    @Override
    public String wire() {
      return wire;
    }
  }

  /** A drawn overlay with the default 32 MiB arena. */
  public static OverlayDecl drawn(String id) {
    return new OverlayDecl(id, id, new Drawn(DEFAULT_SEGMENT_MIB));
  }

  public static OverlayDecl drawn(String id, int segmentMib) {
    return new OverlayDecl(id, id, new Drawn(segmentMib));
  }

  public static OverlayDecl chunked(String id, TimeMode time) {
    return new OverlayDecl(id, id, new Chunked(time));
  }

  public OverlayDecl withName(String name) {
    return new OverlayDecl(id, name, kind);
  }

  public boolean isChunked() {
    return kind instanceof Chunked;
  }

  /** The time mode of a chunked overlay; empty for a drawn one. */
  public Optional<TimeMode> timeMode() {
    return kind instanceof Chunked c ? Optional.of(c.time()) : Optional.empty();
  }

  /** The arena size of a drawn overlay; empty for a chunked one. */
  public OptionalInt segmentMib() {
    return kind instanceof Drawn d ? OptionalInt.of(d.segmentMib()) : OptionalInt.empty();
  }
}
