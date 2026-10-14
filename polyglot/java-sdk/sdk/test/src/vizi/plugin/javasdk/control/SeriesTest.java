package vizi.plugin.javasdk.control;

import static org.junit.jupiter.api.Assertions.*;
import static org.junit.jupiter.api.Assumptions.assumeTrue;

import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.concurrent.TimeUnit;
import org.jspecify.annotations.Nullable;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.Test;
import vizi.plugin.javasdk.DType;
import vizi.plugin.javasdk.Dataset;
import vizi.plugin.javasdk.Plugin;
import vizi.plugin.javasdk.Refusal;
import vizi.plugin.javasdk.SeriesChoice;
import vizi.plugin.javasdk.SeriesEntry;
import vizi.plugin.javasdk.Volume;
import vizi.plugin.javasdk.manifest.ReaderDecl;
import vizi.plugin.javasdk.nativeio.Platform;
import vizi.plugin.javasdk.testkit.TestHost;
import vizi.plugin.v1.ViziPlugin.Error;
import vizi.plugin.v1.ViziPlugin.HostMsg;
import vizi.plugin.v1.ViziPlugin.OpenReader;
import vizi.plugin.v1.ViziPlugin.PluginMsg;
import vizi.plugin.v1.ViziPlugin.ReadVolume;
import vizi.plugin.v1.ViziPlugin.SeriesList;
import vizi.plugin.v1.ViziPlugin.Shutdown;
import vizi.plugin.v1.ViziPlugin.ShutdownReason;

/**
 * One image inside a multi-image file. {@code OpenReader.series}, the {@code
 * SeriesList} reply, the held listing, and the list's checks and trim (control_plane_protocol.md
 * §8.2 and §9, reader_writer_contract.md §1.2a), over the real transports.
 */
class SeriesTest {
  static final Duration T = Duration.ofSeconds(10);

  final List<String> events = new CopyOnWriteArrayList<>();
  TestHost host;

  @AfterEach
  void down() {
    if (host != null) host.close();
  }

  /** A dataset of one 2x2 plane, which records its close. */
  Dataset dataset(String name) {
    return new Dataset() {
      public Info info() {
        return Info.of(1, 1, 1, 2, 2, DType.U8);
      }

      public Volume volume(int t, int c) {
        return Volume.u8(1, 2, 2, new byte[4]);
      }

      public void close() {
        events.add("ds-close:" + name);
      }
    };
  }

  static String file(String uri) {
    return uri.substring(uri.lastIndexOf('/') + 1);
  }

  /**
   * {@code nwb} lists by file name: {@code many*} three images (held, with an opener), {@code
   * single} one (no opener), {@code empty} none, {@code badshape} a zero extent, {@code dup} one id twice, {@code confused}
   * lists even when a series is named, {@code grumpy} has a closer that throws. {@code vol} is a
   * plain reader, which never sees a series.
   */
  Plugin plugin() {
    var b = Plugin.builder("org.test.series", "Series", "0.1").entry("./run");
    b.reader(ReaderDecl.of("nwb", ".nwb").withFallback(".h5"), (uri, dir, series) -> {
      String f = file(uri);
      events.add("open:" + f + (series.isPresent() ? "#" + series.get() : ""));
      if (f.startsWith("confused")) return SeriesChoice.of(SeriesEntry.of("x", "X"), SeriesEntry.of("y", "Y")).onClose(() -> events.add("close:" + f));
      if (series.isPresent()) {
        if (series.get().equals("zz")) throw Refusal.notFound("no series 'zz' in " + f);
        return dataset(f + "#" + series.get());
      }
      return switch (f.replaceAll("\\..*", "")) {
        case "single" -> SeriesChoice.of(SeriesEntry.of("only", "The only one")).onClose(() -> events.add("close:" + f));
        case "empty" -> SeriesChoice.of().onClose(() -> events.add("close:" + f));
        case "badshape" -> SeriesChoice.of(SeriesEntry.of("a", "A").shape(1, 1, 0, 2, 2), SeriesEntry.of("b", "B"))
            .onClose(() -> events.add("close:" + f));
        case "dup" -> SeriesChoice.of(SeriesEntry.of("x", "X"), SeriesEntry.of("y", "Y"), SeriesEntry.of("x", "X again"))
            .onClose(() -> events.add("close:" + f));
        case "grumpy" -> SeriesChoice.of(SeriesEntry.of("a", ""), SeriesEntry.of("b", "")).onClose(() -> {
          events.add("close:" + f);
          throw new IllegalStateException("grumpy");
        });
        case "plain" -> dataset(f);
        default -> SeriesChoice.of(
                SeriesEntry.of("a", "Series A").shape(1, 1, 1, 2, 2).dtype(DType.U8).description("first"),
                SeriesEntry.of("b", "Series B"),
                SeriesEntry.of("", "An empty id"))
            .openingWith(id -> {
              events.add("held-open:" + f + "#" + id);
              if (id.equals("zz")) throw Refusal.notFound("no series 'zz' in " + f);
              return dataset(f + "#" + id);
            })
            .onClose(() -> events.add("close:" + f));
      };
    });
    b.reader(ReaderDecl.of("vol", ".vol"), (uri, dir) -> {
      events.add("open:" + file(uri));
      return dataset(file(uri));
    });
    return b.build();
  }

  void start(@Nullable Duration idle) throws Exception {
    assumeTrue(Platform.isUnix(), "unix transport");
    host = TestHost.start(plugin().application()).open();
    if (idle != null) host.serving.session().heldListingIdle = idle;
    host.hello();
  }

  PluginMsg open(String uri, String reader, @Nullable String series) throws InterruptedException {
    String id = host.nextId();
    OpenReader.Builder o = OpenReader.newBuilder().setUri(uri).setReaderId(reader);
    if (series != null) o.setSeries(series);
    host.step("open " + uri + (series == null ? "" : "#" + series)).send(HostMsg.newBuilder().setId(id).setOpenReader(o).build());
    return host.reply(id, T);
  }

  static void refused(PluginMsg m, Error.Kind kind, String message) {
    assertTrue(m.hasError(), m.toString());
    assertEquals(kind, m.getError().getKind(), m.toString());
    assertEquals(message, m.getError().getMessage());
  }

  void awaitEvent(String e) throws InterruptedException {
    long end = System.nanoTime() + T.toNanos();
    while (!events.contains(e)) {
      if (System.nanoTime() > end) fail("never saw " + e + "; saw " + events);
      Thread.sleep(5);
    }
  }

  @Test
  void aListingIsSentHeldAndOpensTheChoice() throws Exception {
    start(null);
    PluginMsg listed = open("/d/many.nwb", "nwb", null);
    assertTrue(listed.hasSeriesList(), listed.toString());
    SeriesList l = listed.getSeriesList();
    assertFalse(l.getTruncated());
    assertEquals(List.of("a", "b", ""), l.getEntriesList().stream().map(e -> e.getId()).toList());
    var a = l.getEntries(0);
    assertEquals("Series A", a.getLabel());
    assertEquals(List.of(1, 1, 1, 2, 2), a.getShapeTczyxList());
    assertEquals(vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8, a.getDtype());
    assertEquals("first", a.getDescription());
    assertEquals(0, l.getEntries(1).getShapeTczyxCount());
    assertEquals(vizi.plugin.v1.ViziPlugin.DType.DTYPE_UNSPECIFIED, l.getEntries(1).getDtype());

    // "" is an id like any other, opened on the listing that offered it
    PluginMsg opened = open("/d/many.nwb", "nwb", "");
    assertTrue(opened.hasReaderOpened(), opened.toString());
    assertEquals(List.of("open:many.nwb", "held-open:many.nwb#"), events, "the listing opened its choice, not a fresh reader");

    // the dataset owns what the listing held: the choice is not closed
    PluginMsg again = open("/d/other.vol", "vol", null);
    assertTrue(again.hasReaderOpened());
    assertFalse(events.contains("close:many.nwb"), events.toString());
  }

  @Test
  void aNamedSeriesWithNoListingOpensFreshAndAPlainReaderIgnoresTheSeries() throws Exception {
    start(null);
    assertTrue(open("/d/many.nwb", "nwb", "b").hasReaderOpened());
    assertTrue(open("/d/x.vol", "vol", "b").hasReaderOpened());
    assertEquals(List.of("open:many.nwb#b", "open:x.vol", "ds-close:many.nwb#b"), events);
  }

  @Test
  void aListOfOneIsOpenedRatherThanSent() throws Exception {
    start(null);
    PluginMsg m = open("/d/single.nwb", "nwb", null);
    assertTrue(m.hasReaderOpened(), m.toString());
    assertEquals(List.of("open:single.nwb", "open:single.nwb#only", "close:single.nwb"), events,
        "a choice with no opener is opened by the reader afresh, then closed");
  }

  @Test
  void badListsAreRefusedAndClosed() throws Exception {
    start(null);
    refused(open("/d/empty.nwb", "nwb", null), Error.Kind.INVALID_ARGUMENT,
        "open failed: the reader's SeriesChoice is not valid: a SeriesChoice lists no series; a file with no image is refused");
    assertTrue(events.contains("close:empty.nwb"), events.toString());
    refused(open("/d/badshape.nwb", "nwb", null), Error.Kind.INVALID_ARGUMENT,
        "open failed: the reader's SeriesChoice is not valid: series 'a': shape_tczyx must be five positive sizes, not [1, 1, 0, 2, 2]");
    assertTrue(events.contains("close:badshape.nwb"), events.toString());
    // the host answers with the id alone, so two entries sharing one are malformed
    refused(open("/d/dup.nwb", "nwb", null), Error.Kind.INVALID_ARGUMENT,
        "open failed: the reader's SeriesChoice is not valid: series id 'x' is listed twice");
    assertTrue(events.contains("close:dup.nwb"), events.toString());
    refused(open("/d/confused.nwb", "nwb", "x"), Error.Kind.INVALID_ARGUMENT,
        "open failed: asked for series 'x', the reader answered with a list");
    assertTrue(events.contains("close:confused.nwb"), events.toString());
  }

  @Test
  void anUnknownIdIsNotFoundAndRetiresTheListing() throws Exception {
    start(null);
    assertTrue(open("/d/many.nwb", "nwb", null).hasSeriesList());
    refused(open("/d/many.nwb", "nwb", "zz"), Error.Kind.NOT_FOUND, "no series 'zz' in many.nwb");
    assertEquals(List.of("open:many.nwb", "held-open:many.nwb#zz", "close:many.nwb"), events);
    // nothing is held any more: a named series now opens fresh
    assertTrue(open("/d/many.nwb", "nwb", "a").hasReaderOpened());
    assertEquals("open:many.nwb#a", events.get(3));
  }

  @Test
  void anotherOpenRetiresTheListingBeforeTheSameUriRelease() throws Exception {
    start(null);
    PluginMsg first = open("/d/many.nwb", "nwb", "a");
    String handle = first.getReaderOpened().getHandle();
    assertTrue(open("/d/many2.nwb", "nwb", null).hasSeriesList());
    assertEquals(List.of("open:many.nwb#a", "open:many2.nwb"), events, "a listing leaves the open reader open");

    // a listing of the open reader's own file: the held listing goes first, then the reader
    assertTrue(open("/d/many.nwb", "nwb", null).hasSeriesList());
    assertEquals(List.of("open:many.nwb#a", "open:many2.nwb", "close:many2.nwb", "ds-close:many.nwb#a", "open:many.nwb"), events);
    String read = host.nextId();
    host.send(HostMsg.newBuilder().setId(read).setReadVolume(ReadVolume.newBuilder().setHandle(handle)).build());
    assertEquals(Error.Kind.NOT_FOUND, host.reply(read, T).getError().getKind(), "the listed file's reader was closed");

    // the same file under another reader is another key: retired, and opened fresh
    events.clear();
    assertTrue(open("/d/many.nwb", "vol", "a").hasReaderOpened());
    assertEquals(List.of("close:many.nwb", "open:many.nwb"), events);
  }

  @Test
  void aThrowingCloserIsNotedNotRaised() throws Exception {
    start(null);
    assertTrue(open("/d/grumpy.nwb", "nwb", null).hasSeriesList());
    assertTrue(open("/d/x.vol", "vol", null).hasReaderOpened());
    assertEquals(List.of("open:grumpy.nwb", "close:grumpy.nwb", "open:x.vol"), events);
  }

  @Test
  void anIdleListingIsClosed() throws Exception {
    start(Duration.ofMillis(100));
    assertTrue(open("/d/many.nwb", "nwb", null).hasSeriesList());
    awaitEvent("close:many.nwb");
    assertTrue(open("/d/many.nwb", "nwb", "b").hasReaderOpened());
    assertEquals("open:many.nwb#b", events.get(2), "the idle listing no longer opens: " + events);
  }

  @Test
  void theSessionsEndClosesTheListing() throws Exception {
    start(null);
    assertTrue(open("/d/many.nwb", "nwb", null).hasSeriesList());
    host.send(HostMsg.newBuilder().setShutdown(Shutdown.newBuilder().setReason(ShutdownReason.SHUTDOWN_REASON_HOST_EXIT).setDeadlineMs(3000)).build());
    host.expect(PluginMsg::hasShutdownReport, T);
    assertTrue(events.contains("close:many.nwb"), events.toString());
    assertTrue(host.closed().get(5, TimeUnit.SECONDS).isOk());
  }

  // ---- the ceiling (§9) ----

  static vizi.plugin.v1.ViziPlugin.SeriesEntry entry(int i, int labelLength) {
    String id = String.format("series-%013d", i);
    return vizi.plugin.v1.ViziPlugin.SeriesEntry.newBuilder().setId(id).setLabel(("L" + id).repeat(labelLength / 21 + 1).substring(0, labelLength)).build();
  }

  @Test
  void sixtyThousandEntriesFitUntrimmedAndPassTheBounds() {
    List<vizi.plugin.v1.ViziPlugin.SeriesEntry> es = new ArrayList<>();
    for (int i = 0; i < 60_000; i++) es.add(entry(i, 20));
    assertEquals(20, es.get(0).getId().length());
    SeriesList l = SeriesLists.trim(es);
    assertFalse(l.getTruncated());
    assertEquals(60_000, l.getEntriesCount());
    assertNull(Bounds.check(PluginMsg.newBuilder().setId("p-1").setSeriesList(l).build()));
  }

  @Test
  void aListOverTheCeilingArrivesAsAPrefixMarkedTruncated() {
    List<vizi.plugin.v1.ViziPlugin.SeriesEntry> es = new ArrayList<>();
    for (int i = 0; i < 40_000; i++) es.add(entry(i, 1000)); // about 41 MB
    SeriesList l = SeriesLists.trim(es);
    assertTrue(l.getTruncated());
    assertTrue(l.getEntriesCount() > 30_000 && l.getEntriesCount() < 40_000, "" + l.getEntriesCount());
    assertEquals(es.subList(0, l.getEntriesCount()), l.getEntriesList());
    PluginMsg m = PluginMsg.newBuilder().setId("p-12345").setSeriesList(l).build();
    assertTrue(m.getSerializedSize() <= Bounds.MAX_MESSAGE, "" + m.getSerializedSize());
    assertNull(Bounds.check(m));
    // one more entry would not have fit
    SeriesList more = l.toBuilder().addEntries(es.get(l.getEntriesCount())).build();
    assertTrue(more.getSerializedSize() - 2 > SeriesLists.BUDGET, "the trim stopped early");
  }

  @Test
  void seriesListEntriesAreExemptFromTheRepeatedBound() {
    SeriesList.Builder l = SeriesList.newBuilder();
    for (int i = 0; i < 70_000; i++) l.addEntries(vizi.plugin.v1.ViziPlugin.SeriesEntry.newBuilder().setId("s" + i));
    assertNull(Bounds.check(PluginMsg.newBuilder().setSeriesList(l).build()));
    var over = vizi.plugin.v1.ViziPlugin.SeriesEntry.newBuilder();
    for (int i = 0; i < 70_000; i++) over.addShapeTczyx(1);
    assertNotNull(Bounds.check(over.build()), "the entries' own fields are bounded as usual");
  }

  @Test
  void entryChecks() {
    assertNull(SeriesLists.entryProblem("a", List.of(), vizi.plugin.v1.ViziPlugin.DType.DTYPE_UNSPECIFIED));
    assertNull(SeriesLists.entryProblem("a", List.of(1L, 2L, 3L, 4L, 5L), vizi.plugin.v1.ViziPlugin.DType.DTYPE_F64));
    assertNotNull(SeriesLists.entryProblem("a", List.of(1L, 2L, 3L, 4L), vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8));
    assertNotNull(SeriesLists.entryProblem("a", List.of(1L, 2L, 3L, 4L, -5L), vizi.plugin.v1.ViziPlugin.DType.DTYPE_U8));
    String bad = SeriesLists.entryProblem("a", List.of(), vizi.plugin.v1.ViziPlugin.DType.UNRECOGNIZED);
    assertNotNull(bad);
    assertEquals(SeriesLists.EMPTY, SeriesLists.problem(List.of()));
    assertEquals(Optional.empty(), Optional.ofNullable(SeriesLists.problem(List.of(entry(1, 3)))));
  }
}
