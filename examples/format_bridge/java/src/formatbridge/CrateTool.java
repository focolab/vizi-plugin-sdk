package formatbridge;

import java.io.PrintStream;
import java.lang.foreign.ValueLayout;
import java.nio.charset.StandardCharsets;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Random;
import vizi.plugin.javasdk.Refusal;

/**
 * Host-free tools over the format, reached through the launcher:
 *
 * <pre>
 *   ./format-bridge compare A.crate B.crate [--calibration TABLE]
 *   ./format-bridge recreate IN.crate OUT.crate [--order ascending|reverse|scrambled] [--calibration TABLE]
 *   ./format-bridge check FILE.crate
 * </pre>
 *
 * {@code compare} exits 0 once both crates opened and a verdict was printed, 2 when either could
 * not be opened or decoded, 1 for a usage error (SPEC.md §"The round trip").
 */
public final class CrateTool {
  private CrateTool() {}

  static final List<String> COMMANDS = List.of("compare", "recreate", "check");

  static int run(String[] args, PrintStream out, PrintStream err) {
    List<String> pos = new ArrayList<>();
    String calibration = null;
    String order = "ascending";
    for (int i = 1; i < args.length; i++) {
      String a = args[i];
      if (a.equals("--calibration") || a.equals("--order")) {
        if (i + 1 >= args.length) return usage(err, a + " needs a value");
        if (a.equals("--calibration")) calibration = args[++i];
        else order = args[++i];
      } else if (a.startsWith("--calibration=")) {
        calibration = a.substring("--calibration=".length());
      } else if (a.startsWith("--order=")) {
        order = a.substring("--order=".length());
      } else if (a.startsWith("--")) {
        return usage(err, "unknown option " + a);
      } else {
        pos.add(a);
      }
    }
    try {
      switch (args[0]) {
        case "compare" -> {
          if (pos.size() != 2) return usage(err, "compare takes two crates");
          Compare.Verdict v;
          try {
            v = Compare.compare(Path.of(pos.get(0)), Path.of(pos.get(1)), calibration == null ? null : Calibration.load(Path.of(calibration)));
          } catch (Refusal r) {
            err.println("cannot compare: " + r.kind() + ": " + r.getMessage());
            return 2;
          }
          out.println(v);
          return 0;
        }
        case "recreate" -> {
          if (pos.size() != 2) return usage(err, "recreate takes a source and a target");
          if (!List.of("ascending", "reverse", "scrambled").contains(order)) return usage(err, "--order is ascending, reverse or scrambled");
          try {
            recreate(Path.of(pos.get(0)), Path.of(pos.get(1)), order, calibration == null ? null : Calibration.load(Path.of(calibration)));
          } catch (Refusal r) {
            err.println("cannot recreate: " + r.kind() + ": " + r.getMessage());
            return 2;
          }
          out.println("wrote " + pos.get(1) + " (" + order + " block order)");
          return 0;
        }
        case "check" -> {
          if (pos.size() != 1 || calibration != null) return usage(err, "check takes one crate");
          Path p = Path.of(pos.get(0));
          Crate.CheckResult r;
          try {
            r = Crate.check(p);
          } catch (Refusal x) {
            out.println(p.getFileName() + ": " + x.kind() + ": " + x.getMessage());
            return 2;
          }
          out.println(r.summary(String.valueOf(p.getFileName())));
          return r.ok() ? 0 : 3;
        }
        default -> {
          return usage(err, "unknown command " + args[0]);
        }
      }
    } catch (RuntimeException e) {
      err.println("error: " + e);
      return 2;
    }
  }

  private static int usage(PrintStream err, String problem) {
    err.println("error: " + problem);
    err.println("usage: format-bridge compare A.crate B.crate [--calibration TABLE]");
    err.println("       format-bridge recreate IN.crate OUT.crate [--order ascending|reverse|scrambled] [--calibration TABLE]");
    err.println("       format-bridge check FILE.crate");
    return 1;
  }

  /** Reads one crate through this implementation's reader and writes it through its writer. */
  public static void recreate(Path source, Path target, String order, Calibration cal) throws Refusal {
    try (Crate cr = Crate.open(source)) {
      Crate.Header h = cr.header();
      // the section as it is (a DERIVED crate's source document included), the flag, the record and
      // the names carried across; never CALIBRATED, since the blocks are written decoded
      String[] doc = cr.document();
      List<String> names = cr.channelNames();
      CrateWriter w = new CrateWriter(target, new int[] {h.t(), h.c(), h.z(), h.y(), h.x()}, h.dtype(), h.derived(),
          doc[0].getBytes(StandardCharsets.UTF_8), doc[1].getBytes(StandardCharsets.UTF_8), cr.provenanceBytes(),
          names == null ? List.of() : names, h.spacingZyx(), h.timeIncrement());
      List<int[]> slots = new ArrayList<>();
      for (int t = 0; t < h.t(); t++) for (int c = 0; c < h.c(); c++) slots.add(new int[] {t, c});
      if (order.equals("reverse")) Collections.reverse(slots);
      else if (order.equals("scrambled")) Collections.shuffle(slots, new Random(0x5EED));
      w.begin();
      try {
        for (int[] s : slots) w.writeBlock(s[0], s[1], cr.readBlock(s[0], s[1], true, cal).data().toArray(ValueLayout.JAVA_BYTE));
        w.finish();
      } catch (Refusal | RuntimeException e) {
        w.abort();
        throw e;
      }
    }
  }
}
