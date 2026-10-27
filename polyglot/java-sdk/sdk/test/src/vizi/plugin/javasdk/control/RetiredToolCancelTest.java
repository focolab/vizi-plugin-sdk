package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.Test;
import vizi.plugin.v1.ViziPlugin.ToolBinding;
import vizi.plugin.v1.ViziPlugin.ToolMode;
import vizi.plugin.v1.ViziPlugin.ViolationSite;

/**
 * doorbell_E_input.md §2: deactivating a tool queues the open gesture's cancellation on the doorbell
 * and retires the binding on the control plane. The listener drains the cancellation after the
 * retirement is installed, and the tool must still hear it, even when the retirement omits
 * {@code tool_id}.
 */
class RetiredToolCancelTest {
  final List<String> problems = new ArrayList<>();

  Arrangements arrangements() {
    return new Arrangements(
        new Arrangements.Transport() {
          @Override
          public boolean toolActivated(long handle, StringBuilder why) {
            return true;
          }

          @Override
          public boolean toolRetired(long handle, StringBuilder why) {
            return true;
          }

          @Override
          public boolean epochAcknowledged(int viewId, long epoch, StringBuilder why) {
            return true;
          }
        },
        new Arrangements.Reporter() {
          @Override
          public void violation(ViolationSite where, String detail) {
            problems.add(where + ": " + detail);
          }

          @Override
          public void diagnostic(String text) {}
        });
  }

  static ToolBinding live(int handle, String id) {
    return ToolBinding.newBuilder().setTool(handle).setToolId(id).setRevision(1).setMode(ToolMode.TOOL_MODE_GESTURE).build();
  }

  static ToolBinding retirement(int handle) {
    return ToolBinding.newBuilder().setTool(handle).setRevision(2).setRetired(true).build();
  }

  @Test
  void aCancelDrainedAfterTheRetirementStillReachesItsTool() {
    Arrangements a = arrangements();
    a.onToolBinding(live(1, "probe"));
    // The retirement is installed first, as the dispatch thread does before the listener drains.
    assertTrue(a.onToolBinding(retirement(1)).change() instanceof Arrangements.ToolChange.Retired);
    assertEquals("", a.binding(1).orElseThrow().getToolId(), "the retirement statement names no tool");
    ToolBinding reached = a.cancellationBinding(1).orElseThrow();
    assertEquals("probe", reached.getToolId());
    assertFalse(reached.getRetired());
    assertEquals(List.of(), problems);
  }

  @Test
  void theNewest64RetiredHandlesAreRemembered() {
    Arrangements a = arrangements();
    int n = Arrangements.RETIRED_TOOLS_KEPT + 1;
    for (int h = 1; h <= n; h++) {
      a.onToolBinding(live(h, "tool" + h));
      a.onToolBinding(retirement(h));
    }
    assertEquals("", a.cancellationBinding(1).orElseThrow().getToolId(), "the oldest is forgotten");
    for (int h = 2; h <= n; h++) assertEquals("tool" + h, a.cancellationBinding(h).orElseThrow().getToolId());
    a.onToolBinding(live(n + 1, "live"));
    assertEquals("live", a.cancellationBinding(n + 1).orElseThrow().getToolId(), "a live binding is its own");
    assertTrue(a.cancellationBinding(n + 2).isEmpty());
  }
}
