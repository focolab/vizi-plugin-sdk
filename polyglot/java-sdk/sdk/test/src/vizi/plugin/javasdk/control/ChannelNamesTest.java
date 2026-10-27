package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.time.Duration;
import java.util.List;
import java.util.Map;
import java.util.Optional;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.ChannelName;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Dataset;
import vizi.plugin.javasdk.Exporter;
import vizi.plugin.javasdk.Host;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.ExporterDecl;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.ChannelInfo;
import vizi.plugin.v1.ViziPlugin.ChannelNameSource;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.MetadataDoc;
import vizi.plugin.v1.ViziPlugin.OpenExport;
import vizi.plugin.v1.ViziPlugin.OpenReader;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ViewState;

/**
 * Channel names with their source. A reader's {@code channel_names} and their
 * bound, the exporter's descriptor ({@code source_metadata}, {@code channels}, {@code
 * provenance}), and the two places a plugin reads channels (control_plane_protocol.md §8.2, §8.6,
 * §8.7, §8.11).
 */
class ChannelNamesTest {
  static final Duration T = Duration.ofSeconds(10);

  final List<String> events = new CopyOnWriteArrayList<>();
  final AtomicReference<Exporter.Request> exported = new AtomicReference<>();
  TestHost host;

  @AfterEach
  void down() {
    if (host != null) host.close();
  }

  Dataset dataset(String name, Dataset.Info info) {
    return new Dataset() {
      public Info info() {
        return info;
      }

      public Volume volume(int t, int c) {
        return Volume.u8(1, 2, 2, new byte[4]);
      }

      public void close() {
        events.add("close:" + name);
      }
    };
  }

  /** Three channels; the file names how many channels the reader names, or none at all. */
  Plugin plugin() {
    var b = Plugin.builder("org.test.names", "Names", "0.1").entry("./run");
    b.reader(ReaderDecl.of("n", ".n"), (uri, dir) -> {
      String f = uri.substring(uri.lastIndexOf('/') + 1);
      Dataset.Info info = Dataset.Info.of(1, 3, 1, 2, 2, DType.U8);
      info = switch (f) {
        case "none.n" -> info;
        case "short.n" -> info.channelNames("GCaMP", "");
        case "all.n" -> info.channelNames("A", "", "C");
        case "four.n" -> info.channelNames("A", "B", "C", "D");
        default -> throw new IllegalArgumentException(f);
      };
      return dataset(f, info);
    });
    b.exporter(ExporterDecl.of("x", ".x"), req -> {
      exported.set(req);
      return new Exporter.Export() {
        public void chunk(Exporter.Chunk c) {}

        public void finish() {}
      };
    });
    return b.build();
  }

  void start() throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().application()).open();
    host.hello();
  }

  PluginMsg open(String uri) throws InterruptedException {
    String id = host.nextId();
    host.step("open " + uri).send(HostMsg.newBuilder().setId(id).setOpenReader(OpenReader.newBuilder().setUri(uri).setReaderId("n")).build());
    return host.reply(id, T);
  }

  @Test
  void aReaderDeclaresChannelNamesAndNoMoreThanItHas() throws Exception {
    start();
    PluginMsg none = open("/d/none.n");
    assertEquals(0, none.getReaderOpened().getChannelNamesCount(), "a reader naming none sends none");
    assertEquals(List.of("GCaMP", ""), open("/d/short.n").getReaderOpened().getChannelNamesList(), "it may stop short of C");
    PluginMsg all = open("/d/all.n");
    assertEquals(List.of("A", "", "C"), all.getReaderOpened().getChannelNamesList());
    events.clear();

    // four names for three channels: refused before sending, the instance closed and nothing else changed (§1.2 H2)
    PluginMsg four = open("/d/four.n");
    assertTrue(four.hasError(), four.toString());
    assertEquals(Error.Kind.INVALID_ARGUMENT, four.getError().getKind());
    assertEquals("open failed: the reader's ReaderInfo is not valid: channel_names names 4 channels of 3", four.getError().getMessage());
    assertEquals(List.of("close:four.n"), events, "the refused instance is closed; the open reader is not");
  }

  @Test
  void theExportersDescriptorCarriesTheSourcesDocumentTheChannelsAndTheRecord() throws Exception {
    start();
    String record = "{\"manifest_version\": 1, \"source\": {\"name\": \"a.tif\", \"bytes\": 12345678901}, \"selection\":"
        + " {\"channels\": [1, 0], \"channel_names\": [\"GFP\", null], \"scale\": 0.5, \"cropped\": false}}";
    OpenExport.Builder o = OpenExport.newBuilder().setExporterId("x").setLocator("/out/a.x")
        .addShapeTczyx(1).addShapeTczyx(3).addShapeTczyx(1).addShapeTczyx(2).addShapeTczyx(2)
        .setDtype(vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8).setLittleEndian(true)
        .setSourceMetadata(MetadataDoc.newBuilder().setForm("ome-xml").setText("<OME/>"))
        .addChannels(ChannelInfo.newBuilder().setIndex(1).setName("GFP").setSource(ChannelNameSource.CHANNEL_NAME_SOURCE_FILE))
        .addChannels(ChannelInfo.newBuilder().setIndex(0).setName("Channel 1").setSource(ChannelNameSource.CHANNEL_NAME_SOURCE_PLACEHOLDER))
        .addChannels(ChannelInfo.newBuilder().setIndex(2).setName("mine").setSourceValue(7))
        .setProvenance(record)
        .setSpacingZ(2).setSpacingX(0.5).setSpacingUnit("um");
    Exporter.Request req = export(o);
    // calibration per value: a field of 0 is unknown (§8.6)
    assertEquals(java.util.OptionalDouble.of(2), req.spacingZ());
    assertEquals(java.util.OptionalDouble.empty(), req.spacingY());
    assertEquals(java.util.OptionalDouble.of(0.5), req.spacingX());
    assertEquals(java.util.OptionalDouble.empty(), req.timeIncrement());
    assertEquals(new Dataset.Metadata("ome-xml", "<OME/>"), req.sourceMetadata());
    List<ChannelName> ch = req.channels();
    assertEquals(List.of(1, 0, 2), ch.stream().map(ChannelName::index).toList(), "the artifact's C order, by SOURCE index");
    assertEquals(List.of("GFP", "Channel 1", "mine"), ch.stream().map(ChannelName::name).toList());
    assertEquals(List.of(true, false, false), ch.stream().map(ChannelName::declared).toList());
    assertTrue(ch.get(1).placeholder());
    assertEquals(List.of("file", "placeholder", "7"), ch.stream().map(ChannelName::sourceLabel).toList(), "an unknown source is kept");
    assertEquals(record, req.provenanceJson(), "the record as it arrived");
    Map<String, Object> p = req.provenance().orElseThrow();
    assertEquals(1L, p.get("manifest_version"));
    @SuppressWarnings("unchecked")
    Map<String, Object> source = (Map<String, Object>) p.get("source");
    assertEquals(12345678901L, source.get("bytes"));
    @SuppressWarnings("unchecked")
    Map<String, Object> sel = (Map<String, Object>) p.get("selection");
    assertEquals(java.util.Arrays.asList("GFP", null), sel.get("channel_names"));
    assertEquals(0.5, sel.get("scale"));
    assertEquals(false, sel.get("cropped"));

    // no record, or one that is not a JSON object, is none: provenance never fails an export
    for (String bad : List.of("", "[1, 2]", "\"text\"", "{\"a\": 1", "{\"a\": 1} x", "{a: 1}")) {
      Exporter.Request r = export(o.clone().setProvenance(bad).setLocator("/out/" + bad.length() + ".x"));
      assertEquals(Optional.empty(), r.provenance(), bad);
      assertEquals(bad, r.provenanceJson());
    }
    Exporter.Request bare = export(o.clone().clearSourceMetadata().clearChannels());
    assertEquals(Dataset.Metadata.NONE, bare.sourceMetadata());
    assertEquals(List.of(), bare.channels());
  }

  Exporter.Request export(OpenExport.Builder o) throws InterruptedException {
    exported.set(null);
    String id = host.nextId();
    host.send(HostMsg.newBuilder().setId(id).setOpenExport(o).build());
    PluginMsg m = host.reply(id, T);
    assertTrue(m.hasExportOpened(), m.toString());
    return exported.get();
  }

  @Test
  void aViewsChannelsAndItsSeriesPresence() {
    ViewState none = ViewState.getDefaultInstance();
    assertEquals(List.of(), Host.channels(none), "empty with no dataset open");
    assertEquals(Optional.empty(), Host.datasetSeries(none), "absent: a file of one image");
    ViewState v = ViewState.newBuilder()
        .addChannels(ChannelInfo.newBuilder().setIndex(0).setName("C0").setSource(ChannelNameSource.CHANNEL_NAME_SOURCE_FILE))
        .addChannels(ChannelInfo.newBuilder().setIndex(1).setName("Channel 2").setSource(ChannelNameSource.CHANNEL_NAME_SOURCE_PLACEHOLDER))
        .addChannels(ChannelInfo.newBuilder().setIndex(2).setName("Mine").setSource(ChannelNameSource.CHANNEL_NAME_SOURCE_USER))
        .addChannels(ChannelInfo.newBuilder().setIndex(3).setName("Old"))
        .setDatasetSeries("")
        .build();
    assertEquals(Optional.of(""), Host.datasetSeries(v), "present, \"\" included, names a series");
    List<ChannelName> c = Host.channels(v);
    assertEquals(List.of("C0", "Channel 2", "Mine", "Old"), c.stream().map(ChannelName::name).toList());
    assertEquals(List.of(true, false, true, false), c.stream().map(ChannelName::declared).toList(),
        "a file's \"C0\" is believed; a host's placeholder and an unspecified source are not declared");
    assertEquals(List.of("file", "placeholder", "user", "unspecified"), c.stream().map(ChannelName::sourceLabel).toList());
  }
}
