package vizi.plugin.javasdk.doorbell;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import com.google.gson.Gson;
import com.google.gson.GsonBuilder;
import com.google.gson.JsonArray;
import com.google.gson.JsonElement;
import com.google.gson.JsonNull;
import com.google.gson.JsonObject;
import com.google.gson.JsonParser;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.nativeio.Posix;

/**
 * The doorbell cross-check replayer (polyglot/CROSSCHECK.md). Skipped unless {@code
 * VIZI_V2_CORPUS} names a corpus. Each scenario runs on a fresh transport over a real {@code
 * SOCK_SEQPACKET} socketpair: the lifecycle calls are applied, each record is sent from the other
 * end about 10 ms apart (with one {@code SCM_RIGHTS} descriptor when the scenario asks), and the
 * outcomes become verdicts. The column is compared strictly with every other column the corpus
 * carries, and written to {@code VIZI_V2_VERDICTS} when that is set.
 */
class CrossCheckTest {

  static final String COLUMN = "java";

  @Test
  void replayCorpus() throws Exception {
    String corpusPath = System.getenv("VIZI_V2_CORPUS");
    assumeTrue(corpusPath != null && !corpusPath.isEmpty(), "VIZI_V2_CORPUS is not set");
    assumeTrue(Platform.OS == Platform.LINUX, "the corpus replays over SOCK_SEQPACKET");
    JsonObject corpus = JsonParser.parseString(Files.readString(Path.of(corpusPath))).getAsJsonObject();
    JsonArray unsettled = corpus.has("unsettled") ? corpus.getAsJsonArray("unsettled") : new JsonArray();
    assertEquals(0, unsettled.size(), "unsettled.json is empty; comparison is strict");

    JsonArray outScenarios = new JsonArray();
    List<String> disagreements = new ArrayList<>();
    int verdictCount = 0;
    for (JsonElement se : corpus.getAsJsonArray("scenarios")) {
      JsonObject s = se.getAsJsonObject();
      String name = s.get("name").getAsString();
      JsonArray mine = replay(s);
      verdictCount += mine.size();
      JsonObject o = new JsonObject();
      o.addProperty("name", name);
      o.add(COLUMN, mine);
      outScenarios.add(o);
      for (Map.Entry<String, JsonElement> col : s.entrySet()) {
        String key = col.getKey();
        if (List.of("name", "lifecycle", "records", "fds_per_record", "terminal", COLUMN).contains(key)) continue;
        List<String> theirs = canonical(col.getValue().getAsJsonArray());
        List<String> ours = canonical(mine);
        if (!theirs.equals(ours)) {
          disagreements.add(name + ": java " + ours + "\n   vs " + key + " " + theirs);
        }
      }
    }
    String out = System.getenv("VIZI_V2_VERDICTS");
    if (out != null && !out.isEmpty()) {
      JsonObject blob = new JsonObject();
      blob.add("scenarios", outScenarios);
      Gson gson = new GsonBuilder().setPrettyPrinting().serializeNulls().create();
      Files.writeString(Path.of(out), gson.toJson(blob) + "\n", StandardCharsets.UTF_8);
    }
    System.out.println("cross-check: " + outScenarios.size() + " scenarios, " + verdictCount + " verdicts");
    assertTrue(disagreements.isEmpty(), String.join("\n", disagreements));
  }

  /** The comparator's canonical form, folding the labels only prose names. */
  static List<String> canonical(JsonArray verdicts) {
    List<String> out = new ArrayList<>();
    for (JsonElement e : verdicts) {
      JsonObject v = e.getAsJsonObject();
      String type = v.get("type").getAsString();
      switch (type) {
        case "frame" -> {
          String d = v.get("disposition").getAsString();
          if (d.equals("unknown_op")) d = "unknown_opcode";
          String where = v.has("where") && !v.get("where").isJsonNull() ? v.get("where").getAsString() : "";
          boolean grant = v.has("grant_failure") && !v.get("grant_failure").isJsonNull() && v.get("grant_failure").getAsBoolean();
          long closed = v.has("implicitly_closed") && !v.get("implicitly_closed").isJsonNull() ? v.get("implicitly_closed").getAsLong() : 0;
          out.add("frame(" + d + "," + where + "," + grant + "," + closed + ")");
        }
        case "cancellation" -> out.add("cancellation(" + v.get("interaction").getAsLong() + ")");
        default -> out.add("fault(" + v.get("class").getAsString() + ")");
      }
    }
    return out;
  }

  static JsonArray replay(JsonObject scenario) throws Exception {
    int[] sv = Posix.socketpair(Posix.SOCK_SEQPACKET);
    int[] spare = Posix.socketpair(Posix.SOCK_DGRAM);
    Collector c = new Collector();
    Doorbell d = Doorbell.adoptForTest(sv[1], c, t -> {});
    try {
      if (scenario.has("lifecycle")) {
        for (JsonElement le : scenario.getAsJsonArray("lifecycle")) {
          JsonArray call = le.getAsJsonArray();
          String what = call.get(0).getAsString();
          Doorbell.LifecycleResult r = switch (what) {
            case "tool_activated" -> d.toolActivated(call.get(1).getAsLong());
            case "tool_retired" -> d.toolRetired(call.get(1).getAsLong());
            case "epoch_acknowledged" -> d.epochAcknowledged(call.get(1).getAsInt(), call.get(2).getAsLong());
            case "cancel_interaction" -> d.cancelInteraction(call.get(1).getAsLong());
            case "cancel_interactions" -> d.cancelInteractions(call.get(1).getAsInt());
            default -> throw new AssertionError("unknown lifecycle call " + what);
          };
          assertTrue(r.ok(), what + " failed: " + r.detail());
        }
      }
      boolean withFd = scenario.has("fds_per_record") && scenario.get("fds_per_record").getAsInt() > 0;
      JsonArray records = scenario.getAsJsonArray("records");
      for (JsonElement re : records) {
        byte[] rec = Records.unhex(re.getAsString());
        Posix.Sent s = withFd ? Posix.sendmsg(sv[0], rec, spare[0]) : Posix.sendmsg(sv[0], rec);
        assertEquals(rec.length, s.n(), "send");
        Thread.sleep(10);
      }
      c.awaitFrames(records.size(), Duration.ofSeconds(10));
      d.stop();
      assertTrue(d.join(Duration.ofSeconds(5)), "listener exits");
      return c.verdicts();
    } finally {
      Posix.close(sv[0]);
      Posix.close(spare[0]);
      Posix.close(spare[1]);
    }
  }

  /** Collects outcomes as verdicts; waits on the event, never sleeps for it. */
  static final class Collector implements Doorbell.Listener {
    private final List<JsonObject> verdicts = new ArrayList<>();
    private int frames;
    private boolean terminal;

    @Override
    public synchronized void onFrame(Outcome.Frame f) {
      JsonObject v = new JsonObject();
      v.addProperty("type", "frame");
      JsonElement where = JsonNull.INSTANCE;
      boolean grant = false;
      long closed = 0;
      String disposition = switch (f.disposition()) {
        case Outcome.Decoded dec -> {
          if (dec.body() instanceof Outcome.InputBody ib) closed = ib.implicitlyClosed();
          yield "decoded";
        }
        case Outcome.UnknownOp u -> "unknown_op";
        case Outcome.Rejected r -> {
          where = new com.google.gson.JsonPrimitive(r.where().wireName());
          yield "semantic_rejection";
        }
        case Outcome.GrantRejected g -> {
          grant = true;
          yield "semantic_rejection";
        }
        case Outcome.Ignored i -> "ignored";
        case Outcome.FramingRejection fr -> "framing_rejection";
      };
      v.addProperty("disposition", disposition);
      v.add("where", where);
      v.addProperty("grant_failure", grant);
      v.addProperty("implicitly_closed", closed);
      verdicts.add(v);
      frames++;
      notifyAll();
    }

    @Override
    public synchronized void onCancellation(Outcome.Cancellation c) {
      JsonObject v = new JsonObject();
      v.addProperty("type", "cancellation");
      v.addProperty("interaction", c.interaction());
      verdicts.add(v);
    }

    @Override
    public synchronized void onTerminal(Outcome t) {
      if (t instanceof Outcome.Fault f) {
        JsonObject v = new JsonObject();
        v.addProperty("type", "fault");
        v.addProperty("class", f.fault().failureClass().wireName());
        verdicts.add(v);
      }
      terminal = true;
      notifyAll();
    }

    synchronized void awaitFrames(int n, Duration timeout) throws InterruptedException {
      long deadline = System.nanoTime() + timeout.toNanos();
      while (frames < n && !terminal) {
        long left = deadline - System.nanoTime();
        if (left <= 0) throw new AssertionError("saw " + frames + " of " + n + " frames");
        wait(Math.max(1, left / 1_000_000));
      }
    }

    synchronized JsonArray verdicts() {
      JsonArray a = new JsonArray();
      verdicts.forEach(a::add);
      return a;
    }
  }
}
