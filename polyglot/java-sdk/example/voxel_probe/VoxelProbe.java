///usr/bin/env jbang "$0" "$@" ; exit $?
//JAVA 25+
//DEPS org.focolab:vizi-sdk-java:0.1.0-r83
//JAVA_OPTIONS --enable-native-access=ALL-UNNAMED --sun-misc-unsafe-memory-access=allow

import static vizi.plugin.javasdk.manifest.WidgetDecl.button;
import static vizi.plugin.javasdk.manifest.WidgetDecl.column;
import static vizi.plugin.javasdk.manifest.WidgetDecl.image;
import static vizi.plugin.javasdk.manifest.WidgetDecl.label;

import java.io.ByteArrayOutputStream;
import java.nio.ByteBuffer;
import java.util.ArrayList;
import java.util.List;
import java.util.zip.CRC32;
import java.util.zip.Deflater;
import vizi.plugin.javasdk.*;
import vizi.plugin.javasdk.manifest.*;

/**
 * Voxel probe: a marker that follows the cursor on the live lane and stays where you release,
 * the gesture's trail as a loaned line on a second overlay, and a panel whose snapshot command
 * pushes a PNG through the panel arena.
 */
public class VoxelProbe {

  static final Object LOCK = new Object();
  static List<double[]> trail = new ArrayList<>();
  static int gestures = 0;
  static String status = "No gesture yet";

  public static void main(String[] args) {
    var b = Plugin.builder("org.example.voxel_probe", "Voxel Probe", "0.1.0").entry("./run");
    Overlay marker = b.overlay(OverlayDecl.drawn("probe_marker").withName("Probe marker"));
    Overlay trailOverlay = b.overlay(OverlayDecl.drawn("probe_trail", 4).withName("Probe trail"));
    Panel panel = b.panel(PanelDecl.of("probe", "Voxel Probe", column("root",
        label("status", "No gesture yet"),
        image("snapshot"),
        button("snap", "probe.snapshot", "Snapshot"),
        button("clear", "probe.clear", "Clear"))));

    b.tool(ToolDecl.of("probe").withName("Voxel probe"), new Tool() {
      final List<double[]> path = new ArrayList<>();

      Item dot(Position.Voxel at) {
        return Item.markers("probe", at.z(), at.y(), at.x()).color(0, 255, 128, 255).sizePx(10);
      }

      @Override
      public void press(ToolContext ctx, InputEvent.Press ev) {
        ev.info().voxel().ifPresent(at -> {
          path.clear();
          path.add(new double[] {at.z(), at.y(), at.x()});
          marker.publishLive(ev.info(), List.of(dot(at)));
        });
      }

      @Override
      public void drag(ToolContext ctx, InputEvent.Drag ev) {
        ev.info().voxel().ifPresent(at -> {
          path.add(new double[] {at.z(), at.y(), at.x()});
          marker.publishLive(ev.info(), List.of(dot(at)));
        });
      }

      @Override
      public void release(ToolContext ctx, InputEvent.Release ev) {
        ev.info().voxel().ifPresent(at -> {
          path.add(new double[] {at.z(), at.y(), at.x()});
          marker.setItems(List.of(dot(at)));
          double[] flat = new double[path.size() * 3];
          for (int i = 0; i < path.size(); i++) System.arraycopy(path.get(i), 0, flat, 3 * i, 3);
          trailOverlay.setItems(List.of(Item.line("path", flat).color(255, 200, 0, 255).thickness(2)));
          synchronized (LOCK) {
            trail = List.copyOf(path);
            gestures++;
          }
          path.clear();
        });
      }

      @Override
      public void cancel(ToolContext ctx, InputEvent.Cancel ev) {
        path.clear();
        marker.clear();
      }
    });

    b.command(CommandDecl.of("probe.snapshot", "Snapshot the trail").unlisted(), ctx -> {
      List<double[]> t;
      synchronized (LOCK) {
        t = trail;
      }
      byte[] gray = new byte[64 * 64];
      for (double[] p : t) gray[Math.floorMod((int) p[1], 64) * 64 + Math.floorMod((int) p[2], 64)] = (byte) 255;
      panel.image("snapshot").push(png(gray, 64, 64));
      say(panel, "Snapshot of " + t.size() + " points");
      return "snapshot pushed (" + t.size() + " points)";
    });

    b.command(CommandDecl.of("probe.clear", "Clear the probe").unlisted(), ctx -> {
      synchronized (LOCK) {
        trail = List.of();
      }
      marker.clear();
      trailOverlay.clear();
      say(panel, "Cleared");
      return "";
    });

    b.onPanelOpened("probe", ctx -> {
      String s;
      int g;
      synchronized (LOCK) {
        s = status;
        g = gestures;
      }
      panel.label("status").set(s + " (" + g + " gestures so far)");
    });

    System.exit(PluginMain.run(b.build(), args));
  }

  static void say(Panel panel, String text) {
    synchronized (LOCK) {
      status = text;
    }
    panel.label("status").set(text);
  }

  /** A minimal 8-bit greyscale PNG: signature, IHDR, IDAT, IEND. */
  static byte[] png(byte[] gray, int width, int height) {
    ByteArrayOutputStream raw = new ByteArrayOutputStream();
    for (int y = 0; y < height; y++) {
      raw.write(0);
      raw.write(gray, y * width, width);
    }
    Deflater d = new Deflater();
    d.setInput(raw.toByteArray());
    d.finish();
    ByteArrayOutputStream z = new ByteArrayOutputStream();
    byte[] buf = new byte[8192];
    while (!d.finished()) z.write(buf, 0, d.deflate(buf));
    ByteArrayOutputStream out = new ByteArrayOutputStream();
    out.writeBytes(new byte[] {(byte) 0x89, 'P', 'N', 'G', '\r', '\n', 0x1a, '\n'});
    chunk(out, "IHDR", ByteBuffer.allocate(13).putInt(width).putInt(height).put((byte) 8).put((byte) 0).put((byte) 0).put((byte) 0).put((byte) 0).array());
    chunk(out, "IDAT", z.toByteArray());
    chunk(out, "IEND", new byte[0]);
    return out.toByteArray();
  }

  static void chunk(ByteArrayOutputStream out, String kind, byte[] body) {
    byte[] k = kind.getBytes(java.nio.charset.StandardCharsets.US_ASCII);
    out.writeBytes(ByteBuffer.allocate(4).putInt(body.length).array());
    out.writeBytes(k);
    out.writeBytes(body);
    CRC32 crc = new CRC32();
    crc.update(k);
    crc.update(body);
    out.writeBytes(ByteBuffer.allocate(4).putInt((int) crc.getValue()).array());
  }
}
