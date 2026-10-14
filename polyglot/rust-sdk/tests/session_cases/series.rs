use super::*;
use vizi_doorbell::plugin::{Reader, ReaderOpen, SeriesChoice, SeriesEntry, Volume};

struct MultiReader {
    events: Arc<Mutex<Vec<String>>>,
    serial: usize,
}
impl Reader for MultiReader {
    fn open(&self, request: &pb::OpenReader) -> control::Result<ReaderOpen> {
        self.events.lock().unwrap().push(format!(
            "open {} {} {:?}",
            self.serial, request.uri, request.series
        ));
        if request.series.as_deref() == Some("missing") {
            return Err(control::Error::new(
                pb::error::Kind::NotFound,
                "unknown series",
            ));
        }
        if request.uri == "bad-named" || request.series.is_none() && request.uri != "direct" {
            let ids: Vec<String> = match request.uri.as_str() {
                "single" => vec![String::new()],
                "empty" => vec![],
                "duplicate" => vec!["x".into(), "x".into()],
                "many" => (0..60_000).map(|i| format!("{i:020}")).collect(),
                "oversized" => (0..40_000).map(|i| format!("{i:020}")).collect(),
                _ => vec![String::new(), "second".into()],
            };
            return Ok(ReaderOpen::Series(SeriesChoice(
                ids.into_iter()
                    .map(|id| SeriesEntry {
                        label: if request.uri == "oversized" {
                            "x".repeat(1000)
                        } else if request.uri == "many" {
                            "x".repeat(20)
                        } else {
                            id.clone()
                        },
                        id,
                        shape_tczyx: if request.uri == "bad-shape" {
                            vec![1, 0, 2]
                        } else {
                            vec![]
                        },
                        dtype: if request.uri == "bad-dtype" {
                            pb::DType::DtypeF64 as i32
                        } else {
                            0
                        },
                        ..Default::default()
                    })
                    .collect(),
            )));
        }
        Ok(ReaderOpen::Opened(pb::ReaderOpened {
            shape_tczyx: vec![1; 5],
            dtype: pb::DType::DtypeU8 as i32,
            ..Default::default()
        }))
    }
    fn read_volume(&self, _: &pb::ReadVolume) -> control::Result<Volume> {
        Ok(Volume {
            bytes: vec![42],
            shape_zyx: [1; 3],
            dtype: pb::DType::DtypeU8,
        })
    }
    fn close(&self) -> control::Result<()> {
        self.events
            .lock()
            .unwrap()
            .push(format!("close {}", self.serial));
        // Cleanup failures must never turn a successful replacement into Error.
        Err(control::Error::internal("test cleanup failure"))
    }
}
fn registry(events: Arc<Mutex<Vec<String>>>) -> Registry {
    let count = AtomicUsize::new(0);
    Registry::new().reader("raw-reader", move |_, open| {
        let serial = count.fetch_add(1, Ordering::SeqCst);
        events
            .lock()
            .unwrap()
            .push(format!("factory {serial} {}", open.uri));
        Ok(Arc::new(MultiReader {
            events: events.clone(),
            serial,
        }))
    })
}
async fn open(s: &mut Session, uri: &str, series: Option<&str>) -> Plugin {
    s.send(
        "series-open",
        Host::OpenReader(pb::OpenReader {
            uri: uri.into(),
            series: series.map(str::to_owned),
            ..Default::default()
        }),
    )
    .await;
    // Large listings may be serialized alongside the rest of the session suite.
    let reply = tokio::time::timeout(Duration::from_secs(30), s.rx.message())
        .await
        .unwrap()
        .unwrap()
        .unwrap();
    assert_eq!(reply.id, "series-open");
    reply.msg.unwrap()
}
fn opened(reply: Plugin) -> String {
    let Plugin::ReaderOpened(info) = reply else {
        panic!("{reply:?}")
    };
    assert!(!info.handle.is_empty());
    info.handle
}
fn listed(reply: Plugin, count: usize) {
    let Plugin::SeriesList(list) = reply else {
        panic!("{reply:?}")
    };
    assert_eq!(list.entries.len(), count);
    assert!(!list.truncated);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn listing_preserves_current_reader_and_selection_reuses_listing_instance() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    let first = opened(open(&mut s, "direct", None).await);
    listed(open(&mut s, "multi", None).await, 2);
    assert_reader_present(&mut s, &first).await;
    assert!(s.ctx().memory().installed().is_empty()); // Listing needs no G.
    let next = opened(open(&mut s, "multi", Some("")).await);
    assert_reader_absent(&mut s, &first).await;
    assert_reader_present(&mut s, &next).await;
    assert_eq!(
        *events.lock().unwrap(),
        [
            "factory 0 direct",
            "open 0 direct None",
            "factory 1 multi",
            "open 1 multi None",
            "open 1 multi Some(\"\")",
            "close 0",
        ]
    );
    // Same-URI listing must first release that reader.
    listed(open(&mut s, "multi", None).await, 2);
    assert_reader_absent(&mut s, &next).await;
    assert!(events.lock().unwrap().ends_with(&[
        "close 1".into(),
        "factory 2 multi".into(),
        "open 2 multi None".into()
    ]));
    s.send(
        "",
        Host::Shutdown(pb::Shutdown {
            deadline_ms: Some(5000),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ShutdownReport(_))
    ));
    assert_eq!(events.lock().unwrap().last().unwrap(), "close 2");
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn abrupt_end_closes_both_current_and_listing_even_if_runtime_is_retained() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    opened(open(&mut s, "direct", None).await);
    listed(open(&mut s, "multi", None).await, 2);
    let retained = s.runtime.clone();
    s.stop().await;
    tokio::time::timeout(Duration::from_secs(5), async {
        loop {
            if events
                .lock()
                .unwrap()
                .iter()
                .filter(|e| e.starts_with("close "))
                .count()
                == 2
            {
                break;
            }
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    })
    .await
    .unwrap();
    assert!(events
        .lock()
        .unwrap()
        .ends_with(&["close 1".into(), "close 0".into()]));
    drop(retained);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn one_entry_opens_directly_and_named_series_can_open_without_listing() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    opened(open(&mut s, "single", None).await);
    opened(open(&mut s, "fresh", Some("second")).await);
    assert_eq!(
        *events.lock().unwrap(),
        [
            "factory 0 single",
            "open 0 single None",
            "open 0 single Some(\"\")",
            "factory 1 fresh",
            "open 1 fresh Some(\"second\")",
            "close 0",
        ]
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn listing_is_retired_before_another_open_and_failed_selection_preserves_current() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    let first = opened(open(&mut s, "direct", None).await);
    listed(open(&mut s, "multi", None).await, 2);
    listed(open(&mut s, "multi", None).await, 2); // Unset is a new listing.
    assert!(events.lock().unwrap().ends_with(&[
        "close 1".into(),
        "factory 2 multi".into(),
        "open 2 multi None".into()
    ]));
    let Plugin::Error(error) = open(&mut s, "multi", Some("missing")).await else {
        panic!()
    };
    assert_eq!(error.kind, pb::error::Kind::NotFound as i32);
    assert_eq!(events.lock().unwrap().last().unwrap(), "close 2");
    assert_reader_present(&mut s, &first).await;
    listed(open(&mut s, "other", None).await, 2);
    opened(open(&mut s, "direct", Some("recent")).await);
    assert!(events.lock().unwrap().ends_with(&[
        "close 3".into(),
        "close 0".into(),
        "factory 4 direct".into(),
        "open 4 direct Some(\"recent\")".into()
    ]));
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_lists_close_instances_and_keep_previous_reader() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    let first = opened(open(&mut s, "direct", None).await);
    for (uri, series, detail) in [
        (
            "empty",
            None,
            "a SeriesChoice lists no series; a file with no image is refused",
        ),
        ("duplicate", None, "series id 'x' is listed twice"),
        ("bad-shape", None, "shape_tczyx must be five positive sizes"),
        ("bad-dtype", None, "is not one of"),
        (
            "bad-named",
            Some("second"),
            "open failed: asked for series 'second', the reader answered with a list",
        ),
    ] {
        let Plugin::Error(error) = open(&mut s, uri, series).await else {
            panic!()
        };
        assert_eq!(error.kind, pb::error::Kind::InvalidArgument as i32);
        assert!(error.message.contains(detail), "{}", error.message);
        if series.is_none() {
            assert!(error
                .message
                .starts_with("open failed: the reader's SeriesChoice is not valid: "));
        }
        assert!(events.lock().unwrap().last().unwrap().starts_with("close "));
        assert_reader_present(&mut s, &first).await;
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_reader_selection_retires_listing_but_preserves_open_reader() {
    let events = Arc::new(Mutex::new(vec![]));
    let mut s = Session::new(registry(events.clone())).await;
    s.recv().await;
    let handle = opened(open(&mut s, "direct", None).await);
    listed(open(&mut s, "multi", None).await, 2);
    s.send(
        "invalid",
        Host::OpenReader(pb::OpenReader {
            uri: "multi".into(),
            reader_id: "undeclared".into(),
            series: Some("".into()),
            ..Default::default()
        }),
    )
    .await;
    assert!(
        matches!(s.recv().await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::NotFound as i32)
    );
    assert_eq!(events.lock().unwrap().last().unwrap(), "close 1");
    assert_reader_present(&mut s, &handle).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn series_lists_arrive_untrimmed_or_as_message_sized_prefix() {
    let mut s = Session::new(registry(Arc::new(Mutex::new(vec![])))).await;
    s.recv().await;
    listed(open(&mut s, "many", None).await, 60_000);
    let Plugin::SeriesList(list) = open(&mut s, "oversized", None).await else {
        panic!()
    };
    assert!(list.truncated);
    assert!(!list.entries.is_empty() && list.entries.len() < 40_000);
    for (i, entry) in list.entries.iter().enumerate() {
        assert_eq!(entry.id, format!("{i:020}"));
        assert_eq!(entry.label.len(), 1000);
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn existing_reader_ignores_even_an_explicit_empty_series() {
    let mut s =
        Session::new(Registry::new().reader("raw-reader", |_, _| Ok(Arc::new(TestReader)))).await;
    s.recv().await;
    let handle = opened(open(&mut s, "old", Some("")).await);
    assert_reader_present(&mut s, &handle).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn reader_channel_names_validate_before_replacement_and_close_bad_instances() {
    struct NamedReader(Vec<String>, Arc<AtomicUsize>);
    impl Reader for NamedReader {
        fn info(&self) -> control::Result<pb::ReaderOpened> {
            Ok(pb::ReaderOpened {
                shape_tczyx: vec![1, 2, 1, 1, 1],
                dtype: pb::DType::DtypeU8 as i32,
                channel_names: self.0.clone(),
                ..Default::default()
            })
        }
        fn read_volume(&self, _: &pb::ReadVolume) -> control::Result<Volume> {
            unreachable!()
        }
        fn close(&self) -> control::Result<()> {
            self.1.fetch_add(1, Ordering::SeqCst);
            Ok(())
        }
    }
    let closed = Arc::new(AtomicUsize::new(0));
    let counter = closed.clone();
    let mut s = Session::new(Registry::new().reader("raw-reader", move |_, req| {
        let names = match req.uri.as_str() {
            "none" => vec![],
            "short" => vec!["DNA".into()],
            "full" => vec!["".into(), "RNA".into()],
            _ => vec!["".into(); 3],
        };
        Ok(Arc::new(NamedReader(names, counter.clone())))
    }))
    .await;
    s.recv().await;
    let mut previous = String::new();
    for (uri, expected) in [
        ("none", vec![]),
        ("short", vec!["DNA"]),
        ("full", vec!["", "RNA"]),
    ] {
        let Plugin::ReaderOpened(info) = open(&mut s, uri, None).await else {
            panic!()
        };
        assert_eq!(info.channel_names, expected);
        previous = info.handle;
    }
    assert_eq!(closed.load(Ordering::SeqCst), 2);
    let Plugin::Error(error) = open(&mut s, "bad", None).await else {
        panic!()
    };
    assert_eq!(error.kind, pb::error::Kind::InvalidArgument as i32);
    assert_eq!(
        error.message,
        "open failed: the reader's ReaderInfo is not valid: channel_names names 3 channels of 2"
    );
    assert_eq!(closed.load(Ordering::SeqCst), 3);
    assert_reader_present(&mut s, &previous).await;
    s.stop().await;
}
