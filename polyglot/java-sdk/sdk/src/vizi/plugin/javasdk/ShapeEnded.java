package vizi.plugin.javasdk;

import java.util.Optional;
import vizi.plugin.v1.ViziPlugin.ShapeEditEnded;

/**
 * How an edit of an asked region ended (§8.8): sent for every drag the host began, except across
 * the session's end. {@link #last()} is the newest edit the plugin saw for it, if it saw one (a
 * drag may end before any of its input arrives).
 *
 * @param outcome {@code COMMITTED} (the answer's shape is the region's, as one undo step), {@code
 *     REVERTED} (the region is as before: the answer was rejected or refused) or {@code CANCELLED}
 * @param detail for the plugin's log; never parsed
 */
public record ShapeEnded(long dragId, ShapeEditEnded.Outcome outcome, String detail, Optional<ShapeEdit> last) {}
