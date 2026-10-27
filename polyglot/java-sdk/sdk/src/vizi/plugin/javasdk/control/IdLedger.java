package vizi.plugin.javasdk.control;

import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayDeque;
import java.util.Arrays;
import java.util.HashMap;
import java.util.Map;
import java.util.Set;
import vizi.plugin.v1.ViziPlugin.HostMsg;

/**
 * Ids are never reused. This records the id of every host message that deserves a reply (a host
 * request, a {@code GrantStateQuery}, an unknown kind carrying an id) and, in a separate table, of
 * every reply received: a reply carries the id of this plugin's own request, not one the host
 * chose. A second message under a recorded id is a byte-identical re-send, deduplicated, or a
 * protocol error that faults the session. The bytes compared are the message's encoding, which
 * keeps the fields this build does not know as received. The tables hold the last {@link #WINDOW}
 * ids received; an older id is not checked.
 */
final class IdLedger {
  /** How many received ids a session remembers: the host's own bound. */
  static final int WINDOW = 65_536;

  enum Seen { NEW, RESENT, REUSED }

  private record Entry(boolean reply, String id) {}

  private final Set<HostMsg.MsgCase> requests;
  private final Set<HostMsg.MsgCase> replies;
  private final Map<String, byte[]> requestIds = new HashMap<>();
  private final Map<String, byte[]> replyIds = new HashMap<>();
  private final ArrayDeque<Entry> window = new ArrayDeque<>();

  IdLedger(Set<HostMsg.MsgCase> requests, Set<HostMsg.MsgCase> replies) {
    this.requests = requests;
    this.replies = replies;
  }

  /** Called on the dispatch thread only. */
  Seen observe(HostMsg m) {
    String id = m.getId();
    if (id.isEmpty()) return Seen.NEW;
    HostMsg.MsgCase k = m.getMsgCase();
    boolean reply;
    if (requests.contains(k) || k == HostMsg.MsgCase.GRANT_STATE_QUERY || k == HostMsg.MsgCase.MSG_NOT_SET) {
      reply = false;
    } else if (replies.contains(k)) {
      reply = true;
    } else {
      return Seen.NEW;
    }
    Map<String, byte[]> table = reply ? replyIds : requestIds;
    byte[] digest = sha256(m.toByteArray());
    byte[] seen = table.putIfAbsent(id, digest);
    if (seen != null) return Arrays.equals(seen, digest) ? Seen.RESENT : Seen.REUSED;
    window.addLast(new Entry(reply, id));
    if (window.size() > WINDOW) {
      Entry old = window.removeFirst();
      (old.reply() ? replyIds : requestIds).remove(old.id());
    }
    return Seen.NEW;
  }

  /** The ids remembered now. */
  int size() {
    return requestIds.size() + replyIds.size();
  }

  private static byte[] sha256(byte[] b) {
    try {
      return MessageDigest.getInstance("SHA-256").digest(b);
    } catch (NoSuchAlgorithmException e) {
      throw new AssertionError("every JVM provides SHA-256", e);
    }
  }
}
