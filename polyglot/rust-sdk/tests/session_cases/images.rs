use super::*;
use vizi_doorbell::declare::{PanelSpec, PluginBuilder, Widget};

async fn session() -> Session {
    let mut builder = PluginBuilder::new("test.images", "Images", "1.0.0");
    builder.panel(PanelSpec::new(
        "panel",
        "Panel",
        Widget::column(
            "root",
            [
                Widget::image("image"),
                Widget::List {
                    id: "rows".into(),
                    max_rows: 2,
                    visible_rows: 1,
                    selectable: false,
                    command: None,
                    moves_view: false,
                    row: vec![Widget::image("cell")],
                },
                Widget::List {
                    id: "other".into(),
                    max_rows: 2,
                    visible_rows: 1,
                    selectable: false,
                    command: None,
                    moves_view: false,
                    row: vec![Widget::image("other-cell")],
                },
            ],
        ),
    ));
    builder.on_panel_opened(|ctx, p| ctx.push_image(&p.panel_id, "image", b"hook"));
    let plugin = builder.build().unwrap();
    let mut s = Session::with_runtime(|b, l| plugin.into_runtime(b, l), 1, 0, "Hello from images")
        .await
        .unwrap();
    s.recv().await;
    s
}
async fn open(s: &mut Session) -> std::fs::File {
    let file = grant(s, 72, 512);
    let mut i = install(72);
    i.purpose = "P".into();
    i.subject = "panel".into();
    i.regions[0].producer = 2;
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    s.send(
        "",
        Host::PanelOpened(pb::PanelOpened {
            panel_id: "panel".into(),
        }),
    )
    .await;
    file
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retired_panel_keeps_latest_image_until_successor_grant_completes() {
    for capability_first in [true, false] {
        let mut s = session().await;
        let old = open(&mut s).await;
        image(&mut s, &old, "image", "", b"hook").await;
        s.send(
            "",
            Host::RetireSegment(pb::RetireSegment { segment_id: 72 }),
        )
        .await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 72)
        );
        s.ctx().push_image("panel", "image", b"old held").unwrap();
        s.ctx()
            .push_image("panel", "image", b"latest held")
            .unwrap();
        s.barrier().await;
        let mut i = install(75);
        i.purpose = "P".into();
        i.subject = "panel".into();
        i.supersedes = 72;
        i.regions[0].producer = 2;
        let file = if capability_first {
            let file = grant(&s, 75, 512);
            s.send("", Host::InstallSegment(i)).await;
            file
        } else {
            s.send("", Host::InstallSegment(i)).await;
            s.barrier().await;
            grant(&s, 75, 512)
        };
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == 75));
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 72)
        );
        // The already-open panel needs no second PanelOpened or author re-push.
        image(&mut s, &file, "image", "", b"latest held").await;
        s.barrier().await; // No duplicate image or repeated application hook.
        s.stop().await;
    }
}

fn marker(loan: &memory::Loan, revision: u64) -> Plugin {
    Plugin::OverlayUpdate(pb::OverlayUpdate {
        overlay_id: "overlay".into(),
        revision,
        items: vec![pb::OverlayItem {
            item_id: "marker".into(),
            geometry_version: revision,
            geometry: Some(pb::overlay_item::Geometry::Marker(pb::OverlayMarker {
                positions_zyx: Some(pb::GeomBuffer {
                    dtype: pb::DType::DtypeF32 as i32,
                    shape: vec![1, 3],
                    source: Some(pb::geom_buffer::Source::Segment(pb::SegmentRef {
                        segment_id: loan.segment_id,
                        offset: loan.offset,
                        len: loan.len,
                        dtype: pb::DType::DtypeF32 as i32,
                        shape_zyx: vec![1, 1, 3],
                        uses: 1,
                    })),
                }),
                ..Default::default()
            })),
            ..Default::default()
        }],
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn live_overlay_quietly_waits_for_and_publishes_into_replacement() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    let update = pb::OverlayUpdate {
        overlay_id: "overlay".into(),
        revision: 1,
        ..Default::default()
    };
    assert!(!ctx.set_overlay_live(&update, None).unwrap());
    let mut typo = update.clone();
    typo.overlay_id = "typo".into();
    assert_eq!(
        ctx.set_overlay_live(&typo, None).unwrap_err().kind,
        pb::error::Kind::NotFound
    );
    for (id, supersedes) in [(80, 0), (81, 80)] {
        let file = grant(&s, id, 512);
        file.write_all_at(&[0; 48], 64).unwrap(); // Empty Pulsed headers.
        s.send(
            "",
            Host::InstallSegment(pb::InstallSegment {
                segment_id: id,
                supersedes,
                purpose: "O".into(),
                subject: "overlay".into(),
                regions: vec![pb::Region {
                    offset: 64,
                    extent: 240,
                    discipline: 3,
                    producer: 2,
                    buffers: 3,
                    slot_bytes: 64,
                    ..Default::default()
                }],
            }),
        )
        .await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == id));
        if supersedes != 0 {
            assert!(
                matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == supersedes)
            );
        }
        assert!(ctx.set_overlay_live(&update, None).unwrap());
        let mut headers = [0; 48];
        file.read_exact_at(&mut headers, 64).unwrap();
        let slot = headers
            .as_chunks::<16>()
            .0
            .iter()
            .position(|h| u64::from_le_bytes(h[..8].try_into().unwrap()) == 1)
            .expect("READY publication in this mapping");
        let expected = prost::Message::encode_to_vec(&update);
        let mut bytes = vec![0; expected.len()];
        file.read_exact_at(&mut bytes, 112 + slot as u64 * 64)
            .unwrap();
        assert_eq!(bytes, expected);
        s.send(
            "",
            Host::RetireSegment(pb::RetireSegment { segment_id: id }),
        )
        .await;
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == id)
        );
        assert!(!ctx.set_overlay_live(&update, None).unwrap());
        s.barrier().await;
    }
    assert_eq!(ctx.memory().retirement_drops(), 2);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn superseded_overlay_finishes_parked_push_and_ignores_late_loan_return() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let install_arena = |id, supersedes| {
        let mut i = install(id);
        i.purpose = "O".into();
        i.subject = "overlay".into();
        i.supersedes = supersedes;
        i.regions[0].producer = 2;
        i
    };
    let _old_file = grant(&s, 76, 512);
    s.send("", Host::InstallSegment(install_arena(76, 0))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    let ctx = s.ctx();
    let old = ctx
        .memory()
        .try_reserve_push(76, &[0; 12])
        .unwrap()
        .unwrap();
    let file = grant(&s, 77, 512);
    s.send("", Host::InstallSegment(install_arena(77, 76)))
        .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(a)) if a.segment_id == 77));
    s.barrier().await; // A parked reservation prevents early SegmentReleased.
    ctx.push_with_loans(marker(&old, 1), vec![old.clone()])
        .unwrap();
    let push = s.recv().await;
    assert_eq!(push.msg, Some(marker(&old, 1)));
    // Model the host's quiet drop; delay its loan return until after release.
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 76));
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: old.segment_id,
            region: old.region,
            offset: old.offset,
            len: old.len,
        }),
    )
    .await;
    s.barrier().await; // No unknown-release violation or second SegmentReleased.
    assert_eq!(ctx.memory().crossed_releases(), 1);
    let next = ctx
        .memory()
        .try_reserve_push(76, &[0; 12])
        .unwrap()
        .unwrap();
    assert_eq!(next.segment_id, 77);
    ctx.push_with_loans(marker(&next, 2), vec![next.clone()])
        .unwrap();
    assert_eq!(s.recv().await.msg, Some(marker(&next, 2)));
    let mut bytes = [1; 12];
    file.read_exact_at(&mut bytes, next.offset).unwrap();
    assert_eq!(bytes, [0; 12]);
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: next.segment_id,
            region: next.region,
            offset: next.offset,
            len: next.len,
        }),
    )
    .await;
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn cancelling_last_unsent_loan_settles_without_further_host_traffic() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _file = chunk_arena(&mut s, 78, 0).await;
    let ctx = s.ctx();
    let loan = ctx.memory().reserve(78, b"cancel").unwrap();
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 78 }),
    )
    .await;
    s.barrier().await;
    ctx.memory().cancel_unsent(loan).unwrap();
    // No additional message/query to kick the dispatcher's memory flush.
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 78));
    s.barrier().await;
    s.stop().await;
}
async fn image(s: &mut Session, file: &std::fs::File, widget: &str, row: &str, expected: &[u8]) {
    let Some(Plugin::PanelPatch(p)) = s.recv().await.msg else {
        panic!("expected image")
    };
    assert_eq!(p.ops[0].widget_id, widget);
    assert_eq!(p.ops[0].row_key, row);
    let Some(pb::patch_op::Op::SetImage(d)) = p.ops[0].op else {
        panic!("expected image ref")
    };
    let mut bytes = vec![0; d.len as usize];
    file.read_exact_at(&mut bytes, d.offset).unwrap();
    assert_eq!(bytes, expected);
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn pre_open_images_keep_latest_and_flush_before_hook() {
    let mut s = session().await;
    let ctx = s.ctx();
    ctx.push_image("panel", "image", b"old").unwrap();
    ctx.push_image("panel", "image", b"latest").unwrap();
    s.barrier().await; // No image descriptors before a grant.
    let file = open(&mut s).await;
    image(&mut s, &file, "image", "", b"latest").await;
    image(&mut s, &file, "image", "", b"hook").await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn set_rows_prunes_held_images_only_in_its_list_even_when_key_returns() {
    let mut s = session().await;
    let panel = s.ctx().panel("panel");
    for list in ["rows", "other"] {
        panel.set_rows(list, ["a"]).unwrap();
        s.recv().await;
    }
    panel
        .row("a")
        .unwrap()
        .push_image("cell", b"removed")
        .unwrap();
    panel
        .row("a")
        .unwrap()
        .push_image("other-cell", b"keep")
        .unwrap();
    // Exercise the raw patch path too, not only Panel::set_rows.
    s.ctx()
        .patch(
            "panel",
            vec![pb::PatchOp {
                widget_id: "rows".into(),
                row_key: String::new(),
                op: Some(pb::patch_op::Op::SetRows(pb::RowKeys { keys: vec![] })),
            }],
        )
        .unwrap();
    s.recv().await;
    panel.set_rows("rows", ["a"]).unwrap();
    s.recv().await;
    let file = open(&mut s).await;
    image(&mut s, &file, "other-cell", "a", b"keep").await;
    image(&mut s, &file, "image", "", b"hook").await;
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn retired_overlay_waits_for_reserved_publication_before_release() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let file = chunk_arena(&mut s, 72, 0).await;
    let ctx = s.ctx();
    let loan = ctx.memory().reserve(72, b"old").unwrap();
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 72 }),
    )
    .await;
    s.barrier().await; // Dispatcher processed retirement, but no release yet.
    assert_eq!(ctx.memory().query(72).state, 3);
    assert!(ctx
        .memory()
        .try_reserve_push(72, b"drop")
        .unwrap()
        .is_none());
    assert_eq!(ctx.memory().retirement_drops(), 1);
    ctx.push_with_loans(
        Plugin::OverlayLabelTable(pb::OverlayLabelTable {
            overlay_id: "chunks".into(),
            colormap: Some(pb::GeomBuffer {
                dtype: pb::DType::DtypeU8 as i32,
                shape: vec![3],
                source: Some(pb::geom_buffer::Source::Segment(pb::SegmentRef {
                    segment_id: loan.segment_id,
                    offset: loan.offset,
                    len: loan.len,
                    uses: 1,
                    ..Default::default()
                })),
            }),
            ..Default::default()
        }),
        vec![loan],
    )
    .unwrap();
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::OverlayLabelTable(_))
    ));
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 72));
    let mut bytes = [0; 3];
    file.read_exact_at(&mut bytes, 64).unwrap();
    assert_eq!(&bytes, b"old");
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn delivered_deposit_pins_mapping_until_copy_or_abandonment() {
    for copy in [true, false] {
        let mut s = Session::new(Registry::new()).await;
        s.recv().await;
        let file = grant(&s, 73, 512);
        file.write_all_at(b"hello", 64).unwrap();
        s.send("", Host::InstallSegment(install(73))).await;
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
        let ctx = s.ctx();
        let waiter = tokio::task::spawn_blocking(move || {
            ctx.request_timeout(
                Plugin::ReadDataVolume(pb::ReadDataVolume::default()),
                Duration::from_secs(5),
            )
        });
        let request = s.recv().await;
        let mut volume = data(73);
        volume.install = None;
        s.send(&request.id, Host::DataVolume(volume)).await;
        let mut reply = waiter.await.unwrap().unwrap();
        s.send(
            "",
            Host::RetireSegment(pb::RetireSegment { segment_id: 73 }),
        )
        .await;
        s.barrier().await;
        assert_eq!(s.ctx().memory().query(73).state, 3);
        if copy {
            assert_eq!(reply.copy_payload().unwrap().bytes, b"hello");
        }
        drop(reply);
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::ReleaseDeposit(r)) if r.segment_id == 73)
        );
        assert!(
            matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 73)
        );
        assert_eq!(s.ctx().memory().query(73).state, 5);
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn descriptor_bearing_request_also_finishes_retirement() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let _file = chunk_arena(&mut s, 74, 0).await;
    let ctx = s.ctx();
    let loan = ctx.memory().reserve(74, b"labels").unwrap();
    s.send(
        "",
        Host::RetireSegment(pb::RetireSegment { segment_id: 74 }),
    )
    .await;
    s.barrier().await;
    let waiting = ctx
        .start_request(Plugin::CompileLabelVolume(pb::CompileLabelVolume {
            overlay_id: "chunks".into(),
            item: Some(pb::OverlayItem {
                item_id: "labels".into(),
                geometry: Some(pb::overlay_item::Geometry::LabelVolume(
                    pb::OverlayLabelVolume {
                        labels: Some(pb::GeomBuffer {
                            dtype: pb::DType::DtypeU8 as i32,
                            shape: vec![1, 1, 6],
                            source: Some(pb::geom_buffer::Source::Segment(pb::SegmentRef {
                                segment_id: loan.segment_id,
                                offset: loan.offset,
                                len: loan.len,
                                dtype: pb::DType::DtypeU8 as i32,
                                shape_zyx: vec![1, 1, 6],
                                uses: 1,
                            })),
                        }),
                        ..Default::default()
                    },
                )),
                ..Default::default()
            }),
            ..Default::default()
        }))
        .unwrap();
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::CompileLabelVolume(_))
    ));
    assert!(matches!(s.recv().await.msg, Some(Plugin::SegmentReleased(r)) if r.segment_id == 74));
    drop(waiting);
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn relaunch_flushes_images_on_either_grant_order_without_panel_opened() {
    for capability_first in [true, false] {
        let mut s = session().await;
        s.ctx().push_image("panel", "image", b"relaunch").unwrap();
        let mut i = install(72);
        i.purpose = "P".into();
        i.subject = "panel".into();
        i.regions[0].producer = 2;
        let file = if capability_first {
            let file = grant(&s, 72, 512);
            s.send("", Host::InstallSegment(i)).await;
            file
        } else {
            s.send("", Host::InstallSegment(i)).await;
            s.barrier().await;
            grant(&s, 72, 512)
        };
        assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
        image(&mut s, &file, "image", "", b"relaunch").await;
        s.barrier().await; // No PanelOpened and no hook-generated image.
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn failed_held_image_does_not_block_other_images_or_panel_open_hook() {
    let mut s = session().await;
    // The actual grant is smaller than vizi's default pre-grant limit.
    // A failing key sorts before the valid image, exercising continued replay.
    s.ctx()
        .push_image_row("panel", "cell", "a", &[1; 257])
        .unwrap();
    s.ctx().push_image("panel", "image", b"good").unwrap();
    let file = open(&mut s).await;
    let mut images = Vec::new();
    while images.len() < 2 {
        match s.recv().await.msg.unwrap() {
            Plugin::SegmentShortfall(_) => {}
            Plugin::PanelPatch(p) => {
                assert_eq!(p.ops[0].widget_id, "image");
                let Some(pb::patch_op::Op::SetImage(d)) = p.ops[0].op else {
                    panic!("image")
                };
                let mut bytes = vec![0; d.len as usize];
                file.read_exact_at(&mut bytes, d.offset).unwrap();
                images.push(bytes);
                s.send(
                    "",
                    Host::ReleaseLoan(pb::ReleaseLoan {
                        segment_id: d.segment_id,
                        region: 0,
                        offset: d.offset,
                        len: d.len,
                    }),
                )
                .await;
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert_eq!(images, [b"good".to_vec(), b"hook".to_vec()]);
    s.barrier().await;
    // A second open runs the hook again; the failed image was discarded.
    s.send(
        "",
        Host::PanelOpened(pb::PanelOpened {
            panel_id: "panel".into(),
        }),
    )
    .await;
    image(&mut s, &file, "image", "", b"hook").await;
    s.barrier().await;
    s.stop().await;
}
