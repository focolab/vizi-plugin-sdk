package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertNull;

import java.util.OptionalInt;
import org.junit.jupiter.api.Test;

class FeaturesTest {
  @Test
  void readyIsTheLowerMinor() {
    for (int host : new int[] {0, 1, 99}) assertEquals(0, Features.readyMinor(host));
  }

  @Test
  void refusalsNameTheFeatureAndBothMinors() {
    var session = new ProtocolVersion(1, 0);
    assertNull(Features.unavailable(Features.SET_VIEW, session));
    assertEquals("feature \"test_future\" needs protocol 1.1; this session speaks 1.0",
        Features.refusal("test_future", OptionalInt.of(1), session));
    assertEquals("feature \"follow\" needs protocol 1.0; the session has no handshake yet",
        Features.unavailable(Features.FOLLOW, null));
    assertEquals("this plugin's manifest requires protocol 1.2 but its code speaks 1.0", Features.enginesRefusal(2, 0));
    assertNull(Features.enginesRefusal(0, 0));
  }
}
