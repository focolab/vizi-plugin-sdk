package vizi.plugin.javasdk;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;
import vizi.plugin.v1.PluginGrpc;
import vizi.plugin.v1.ViziPlugin;

class ScaffoldTest {
  @Test
  void wireCodeIsGenerated() {
    assertEquals("vizi.plugin.v1.Plugin", PluginGrpc.SERVICE_NAME);
    assertNotNull(ViziPlugin.HostMsg.getDescriptor());
  }
}
