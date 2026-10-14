use super::*;

fn filter() -> pb::AppliedFilter {
    use pb::value::Kind;
    pb::AppliedFilter {
        plugin_id: "test.plugin".into(),
        plugin_version: "1.2.3".into(),
        stage_id: "gain".into(),
        params: [
            ("label", Kind::S(" é; run 1 ".into())),
            ("factor", Kind::F(1.5)),
            ("enabled", Kind::B(true)),
            ("seed", Kind::I(9007199254740993)),
        ]
        .into_iter()
        .map(|(key, kind)| (key.into(), pb::Value { kind: Some(kind) }))
        .collect(),
        channels: vec![7, 9],
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn read_provenance_belongs_to_each_reply_not_the_view_or_previous_read() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _file = grant(&s, 191, 512);
    let shown = filter();
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.view_state());
    let request = s.recv().await;
    assert!(matches!(request.msg, Some(Plugin::GetViewState(_))));
    s.send(
        &request.id,
        Host::ViewState(pb::ViewState {
            filter: Some(shown.clone()),
            spacing_zyx: vec![f64::NAN, 0.3208, 0.3208],
            spacing_unit: "um".into(),
            spacing_provenance: 1, // FILE: Z unrecorded, XY measured.
            ..Default::default()
        }),
    )
    .await;
    let view = task.await.unwrap().unwrap();
    assert_eq!(view.filter, Some(shown.clone()));
    assert!(view.spacing_zyx[0].is_nan());
    assert_eq!(view.axis_known_zyx(), [false, true, true]);
    assert_eq!(
        view.measured_spacing_zyx(),
        [None, Some(0.3208), Some(0.3208)]
    );
    assert!(view.spacing_known());
    assert_eq!(view.physical_spacing(), None);

    let mut actual = filter();
    actual.plugin_version = "1.2.4".into();
    // An explicit raw read, unmodified channel, higher level, and failed/refused
    // frame each return None, even when the view names a shared filter.
    for (t, c, level, raw, provenance) in [
        (0, 7, 0, false, Some(actual.clone())),
        (0, 7, 0, true, None),
        (0, 3, 0, false, None),
        (0, 7, 1, false, None),
        (1, 7, 0, false, None),
        (2, 9, 0, false, Some(filter())),
    ] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || {
            if raw {
                ctx.read_volume_raw(t, c, level, None)
            } else {
                ctx.read_volume(t, c, level, None)
            }
        });
        let request = s.recv().await;
        assert!(matches!(request.msg, Some(Plugin::ReadDataVolume(ref read))
            if (read.t, read.c, read.level, read.raw) == (t, c, level, raw)
                && read.roi_min_zyx.is_empty() && read.roi_max_zyx.is_empty()));
        let mut volume = data(191);
        volume.t = t;
        volume.c = c;
        volume.level = level;
        volume.open_generation = 42;
        volume.filter = provenance.clone();
        s.send(&request.id, Host::DataVolume(volume)).await;
        loop {
            match s.recv().await.msg {
                Some(Plugin::GrantAck(_)) => {}
                Some(Plugin::ReleaseDeposit(r)) => {
                    assert_eq!((r.segment_id, r.offset, r.len), (191, 64, 5));
                    break;
                }
                other => panic!("{other:?}"),
            }
        }
        let volume = task.await.unwrap().unwrap();
        assert_eq!(volume.filter, provenance);
        assert_eq!(volume.open_generation, 42);
        assert_eq!(volume.data, control::VoxelData::U8(b"hello".to_vec()));
    }
    // A raw read does not clear the filter the person sees in the views.
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.view_state());
    let request = s.recv().await;
    s.send(
        &request.id,
        Host::ViewState(pb::ViewState {
            filter: Some(shown.clone()),
            ..Default::default()
        }),
    )
    .await;
    assert_eq!(task.await.unwrap().unwrap().filter, Some(shown));
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.view_state());
    let request = s.recv().await;
    s.send(&request.id, Host::ViewState(pb::ViewState::default()))
        .await;
    assert!(task.await.unwrap().unwrap().filter.is_none());
    s.stop().await;
}

#[derive(Debug, PartialEq)]
struct Written {
    t: u32,
    bytes: Vec<u8>,
    volumes: Vec<(u32, Option<pb::AppliedFilter>)>,
}
struct ProvenanceExporter(Arc<Mutex<Vec<Written>>>);
impl plugin::Exporter for ProvenanceExporter {
    // Only `write_chunk` is implemented. Exporters implementing `write` are
    // covered by the other session export tests.
    fn write_chunk(&self, chunk: &plugin::ExportChunk<'_>) -> control::Result<u64> {
        assert!(
            chunk.filter_for(0).is_none(),
            "absent channel has no volume"
        );
        self.0.lock().unwrap().push(Written {
            t: chunk.t,
            bytes: chunk.bytes.to_vec(),
            volumes: chunk
                .channels
                .iter()
                .map(|&c| (c, chunk.filter_for(c).cloned()))
                .collect(),
        });
        Ok(chunk.bytes.len() as u64)
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        Ok(Default::default())
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn export_provenance_is_per_chunk_and_channel_for_inline_and_deposited_data() {
    let written = Arc::new(Mutex::new(Vec::new()));
    let observed_open = Arc::new(Mutex::new(Vec::new()));
    let records = written.clone();
    let opens = observed_open.clone();
    let app = Registry::new().exporter("raw", move |_, open| {
        opens.lock().unwrap().push(open.filter.clone());
        Ok(Arc::new(ProvenanceExporter(records.clone())))
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    let file = grant(&s, 192, 512);
    file.write_all_at(&[7; 6], 64).unwrap();
    let mut install = install(192);
    install.purpose = "X".into();
    s.send("", Host::InstallSegment(install)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    for open_filter in [Some(filter()), None] {
        s.send(
            "open",
            Host::OpenExport(pb::OpenExport {
                exporter_id: "raw".into(),
                shape_tczyx: vec![3, 3, 1, 1, 2],
                dtype: pb::DType::DtypeU8 as i32,
                filter: open_filter.clone(),
                ..Default::default()
            }),
        )
        .await;
        let Some(Plugin::ExportOpened(opened)) = s.recv().await.msg else {
            panic!("open failed")
        };
        // Source channel identities differ from plane indices. With the user's
        // raw-fallback option, a failed frame can be entirely raw, followed by
        // a frame with a different filtered subset.
        for (t, filtered) in [(4, vec![7]), (5, vec![]), (6, vec![9])] {
            let filtered = if open_filter.is_some() {
                filtered
            } else {
                vec![]
            };
            let deposited = t != 4;
            let source = if deposited {
                pb::export_chunk::Source::Segment(pb::SegmentRef {
                    segment_id: 192,
                    offset: 64,
                    len: 6,
                    shape_zyx: vec![1, 1, 2],
                    dtype: pb::DType::DtypeU8 as i32,
                    ..Default::default()
                })
            } else {
                pb::export_chunk::Source::Inline(vec![5; 6])
            };
            s.send(
                "write",
                Host::ExportChunk(pb::ExportChunk {
                    handle: opened.handle.clone(),
                    t,
                    channels: vec![3, 7, 9],
                    filtered_channels: filtered.clone(),
                    source: Some(source),
                    ..Default::default()
                }),
            )
            .await;
            let mut released = !deposited;
            let mut replied = false;
            while !released || !replied {
                match s.recv().await.msg {
                    Some(Plugin::ReleaseDeposit(r)) => {
                        assert_eq!((r.segment_id, r.len), (192, 6));
                        released = true;
                    }
                    Some(Plugin::ChunkWritten(c)) => {
                        assert_eq!(c.total_bytes, 6);
                        replied = true;
                    }
                    other => panic!("{other:?}"),
                }
            }
            let observed = written.lock().unwrap().pop().unwrap();
            assert_eq!(
                observed,
                Written {
                    t,
                    bytes: vec![if deposited { 7 } else { 5 }; 6],
                    volumes: [3, 7, 9]
                        .into_iter()
                        .map(|c| (
                            c,
                            if filtered.contains(&c) {
                                open_filter.clone()
                            } else {
                                None
                            }
                        ))
                        .collect(),
                }
            );
        }
        s.send(
            "finish",
            Host::FinishExport(pb::FinishExport {
                handle: opened.handle,
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::ExportDone(_))));
    }
    assert_eq!(*observed_open.lock().unwrap(), vec![Some(filter()), None]);
    s.stop().await;
}

struct PartialArtifact {
    path: std::path::PathBuf,
    writes: Arc<AtomicUsize>,
}
impl PartialArtifact {
    fn append(&self, bytes: &[u8]) -> control::Result<u64> {
        use std::io::Write;
        self.writes.fetch_add(1, Ordering::SeqCst);
        std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.path)
            .unwrap()
            .write_all(bytes)
            .unwrap();
        Ok(bytes.len() as u64)
    }
    fn cleanup(&self) -> control::Result<()> {
        std::fs::remove_file(&self.path).unwrap();
        Ok(())
    }
}
impl plugin::Exporter for PartialArtifact {
    fn write_chunk(&self, chunk: &plugin::ExportChunk<'_>) -> control::Result<u64> {
        self.append(chunk.bytes)
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        panic!("an abandoned export must not be finalized")
    }
    fn abort(&self) -> control::Result<()> {
        self.cleanup()
    }
}
struct LegacyPartialArtifact(PartialArtifact);
impl plugin::Exporter for LegacyPartialArtifact {
    fn write(&self, _: u32, _: &[u32], bytes: &[u8]) -> control::Result<u64> {
        self.0.append(bytes)
    }
    fn finish(&self) -> control::Result<pb::ExportDone> {
        panic!("an abandoned export must not be finalized")
    }
    fn abort(&self) -> control::Result<()> {
        self.0.cleanup()
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn contradictory_export_chunks_never_reach_either_callback_and_abort_cleans_partial_file() {
    for legacy in [false, true] {
        let directory = tempfile::tempdir().unwrap();
        let path = directory.path().join("partial.raw");
        let output = path.clone();
        let writes = Arc::new(AtomicUsize::new(0));
        let observed = writes.clone();
        let app = Registry::new().exporter("raw", move |_, _| {
            let artifact = PartialArtifact {
                path: output.clone(),
                writes: observed.clone(),
            };
            Ok(if legacy {
                Arc::new(LegacyPartialArtifact(artifact)) as Arc<dyn plugin::Exporter>
            } else {
                Arc::new(artifact)
            })
        });
        let mut s = Session::new(app).await;
        s.recv().await;
        let _file = grant(&s, 193, 512);
        let mut install = install(193);
        install.purpose = "X".into();
        s.send("", Host::InstallSegment(install)).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
        for has_filter in [false, true] {
            for deposited in [false, true] {
                s.send(
                    "open",
                    Host::OpenExport(pb::OpenExport {
                        exporter_id: "raw".into(),
                        shape_tczyx: vec![2, 1, 1, 1, 5],
                        dtype: pb::DType::DtypeU8 as i32,
                        filter: has_filter.then(filter),
                        ..Default::default()
                    }),
                )
                .await;
                let Some(Plugin::ExportOpened(opened)) = s.recv().await.msg else {
                    panic!("open failed")
                };
                writes.store(0, Ordering::SeqCst);
                s.send(
                    "valid",
                    Host::ExportChunk(pb::ExportChunk {
                        handle: opened.handle.clone(),
                        channels: vec![7],
                        filtered_channels: if has_filter { vec![7] } else { vec![] },
                        source: Some(pb::export_chunk::Source::Inline(b"prior".to_vec())),
                        ..Default::default()
                    }),
                )
                .await;
                assert!(matches!(s.recv().await.msg,
                    Some(Plugin::ChunkWritten(c)) if c.total_bytes == 5));
                let source = if deposited {
                    pb::export_chunk::Source::Segment(pb::SegmentRef {
                        segment_id: 193,
                        offset: 64,
                        len: 5,
                        shape_zyx: vec![1, 1, 5],
                        dtype: pb::DType::DtypeU8 as i32,
                        ..Default::default()
                    })
                } else {
                    pb::export_chunk::Source::Inline(b"wrong".to_vec())
                };
                s.send(
                    "invalid",
                    Host::ExportChunk(pb::ExportChunk {
                        handle: opened.handle.clone(),
                        t: 1,
                        channels: vec![7],
                        // The subset case includes a valid filtered channel:
                        // no part of a contradictory chunk may be written.
                        filtered_channels: if has_filter { vec![7, 9] } else { vec![7] },
                        source: Some(source),
                        ..Default::default()
                    }),
                )
                .await;
                let mut released = !deposited;
                let mut replied = false;
                while !released || !replied {
                    let reply = s.recv().await;
                    match reply.msg {
                        Some(Plugin::ReleaseDeposit(r)) => {
                            assert_eq!((r.segment_id, r.offset, r.len), (193, 64, 5));
                            released = true;
                        }
                        Some(Plugin::Error(e)) => {
                            assert_eq!(reply.id, "invalid");
                            assert_eq!(e.kind, pb::error::Kind::InvalidArgument as i32);
                            assert!(e.message.contains("filtered_channels"));
                            assert!(e.message.contains(if has_filter {
                                "channel 9 absent from ExportChunk.channels"
                            } else {
                                "OpenExport.filter is absent"
                            }));
                            replied = true;
                        }
                        other => panic!("{other:?}"),
                    }
                }
                assert_eq!(writes.load(Ordering::SeqCst), 1);
                assert_eq!(std::fs::read(&path).unwrap(), b"prior");
                s.send(
                    "abort",
                    Host::AbortExport(pb::AbortExport {
                        handle: opened.handle.clone(),
                    }),
                )
                .await;
                assert!(matches!(s.recv().await.msg,
                    Some(Plugin::ExportDone(done)) if done.handle == opened.handle));
                assert!(
                    !path.exists(),
                    "abort must remove earlier successful writes"
                );
                s.barrier().await;
            }
        }
        s.stop().await;
    }
}
