package vizi.plugin.javasdk;

/**
 * Computes the shape of a region type the plugin declares (control_plane_protocol.md §8.8):
 * where every vertex goes when the person drags one of a region's handles. Register one per type
 * with {@link Plugin.Builder#regionType}.
 *
 * <p>One function serves drags and commits: {@link ShapeEdit#commit()} says which. A drag's answer
 * is a proposal the host draws while the person drags; a commit's answer is the one that is kept,
 * and the host waits on it with the region held. Every edit carries the whole region as it stood
 * when the drag began, so answer each one from the edit alone.
 *
 * <p>Runs on the SDK's shape worker, never the command worker, one call at a time. Return {@link
 * Shape#rejected} for a position with no shape; a {@link Refusal} or any other exception answers a
 * commit {@code REJECTED} with its message, and proposes nothing for a drag. The SDK checks the
 * result (one finite position per vertex) before it sends it.
 */
@FunctionalInterface
public interface Shaper {
  Shape shape(ShapeEdit edit) throws Exception;
}
