use super::*;
use std::os::{fd::FromRawFd, unix::fs::FileExt};

fn runtime() -> (Runtime, std::os::fd::OwnedFd) {
    let manifest = crate::manifest::Manifest::parse(r#"{"manifest_version":1,"id":"org.example.test","name":"Test","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]},"contributions":{"panels":[{"id":"p","title":"Panel","root":{"kind":"list","id":"rows","row":[{"kind":"label","id":"value"},{"kind":"image","id":"image"}]}}]}}"#).unwrap();
    let (fd, peer) = crate::socket_pair().unwrap();
    let (bell, listener) = Doorbell::new(fd, Default::default()).unwrap();
    let runtime = Runtime::new(
        manifest,
        Arc::new(crate::plugin::Registry::new()),
        bell,
        listener,
    )
    .unwrap();
    *runtime.context().core.peer.lock().unwrap() = Some((1, 0));
    (runtime, peer)
}

fn arena(ctx: &Context) {
    arena_sized(ctx, 4096);
}

fn arena_sized(ctx: &Context, size: u64) {
    let fd = unsafe {
        libc::memfd_create(
            c"image-order".as_ptr(),
            libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
        )
    };
    assert!(fd >= 0);
    let file = unsafe { std::fs::File::from_raw_fd(fd) };
    file.set_len(size).unwrap();
    let mut header = [0; 32];
    header[..8].copy_from_slice(&1_u64.to_le_bytes());
    let crc = crate::crc32(&header[..28]);
    header[28..].copy_from_slice(&crc.to_le_bytes());
    file.write_all_at(&header, 0).unwrap();
    assert_eq!(
        unsafe {
            libc::fcntl(
                fd,
                libc::F_ADD_SEALS,
                libc::F_SEAL_GROW | libc::F_SEAL_SHRINK,
            )
        },
        0
    );
    let grant = crate::MemoryGrant {
        segment_id: 1,
        size,
        permissions: crate::Permissions::new(15).unwrap(),
    };
    ctx.memory()
        .grant(grant.clone(), grant.encode(7).unwrap(), file.into())
        .unwrap();
    ctx.memory()
        .install(pb::InstallSegment {
            segment_id: 1,
            purpose: "P".into(),
            subject: "p".into(),
            regions: vec![pb::Region {
                offset: 64,
                extent: size - 64,
                discipline: 4,
                producer: 2,
                ..Default::default()
            }],
            ..Default::default()
        })
        .unwrap();
}

#[test]
fn image_cannot_overtake_pending_rows_and_values_without_a_drain() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    arena(&ctx);
    let panel = ctx.panel("p");
    panel.set_rows("rows", ["a"]).unwrap();
    panel
        .row("a")
        .unwrap()
        .set_value("value", "before")
        .unwrap();
    panel.row("a").unwrap().push_image("image", b"png").unwrap();
    panel.row("a").unwrap().set_value("value", "after").unwrap();
    // No service/writer exists: all four messages must coexist in the queue.
    let q = ctx.core.queues.lock().unwrap();
    assert!(q.reliable.is_empty());
    assert_eq!(q.pushes.len(), 4);
    let ops: Vec<_> = q
        .pushes
        .iter()
        .map(|message| {
            let Some(Plugin::PanelPatch(patch)) = &message.msg else {
                panic!("patch")
            };
            assert_eq!(patch.panel_id, "p");
            patch.ops[0].clone()
        })
        .collect();
    assert!(matches!(&ops[0].op, Some(pb::patch_op::Op::SetRows(rows)) if rows.keys == ["a"]));
    assert!(
        matches!(&ops[1].op, Some(pb::patch_op::Op::SetValue(v)) if v == &pb::Value::from("before"))
    );
    assert!(
        matches!(&ops[2].op, Some(pb::patch_op::Op::SetImage(image)) if image.segment_id == 1 && image.len == 3)
    );
    assert!(
        matches!(&ops[3].op, Some(pb::patch_op::Op::SetValue(v)) if v == &pb::Value::from("after"))
    );
    assert!(ops[1..].iter().all(|op| op.row_key == "a"));
    drop(q);
    runtime.stop();
}

#[test]
fn oversized_hold_is_refused_without_replacing_the_previous_image() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    ctx.push_image("p", "image", b"keep").unwrap();
    let error = ctx
        .push_image("p", "image", &vec![0; 16 * 1024 * 1024 + 1])
        .unwrap_err();
    assert!(error.message.contains("16 MiB"));
    let images = ctx.core.images.lock().unwrap();
    assert_eq!(images.bytes, 4);
    assert_eq!(images.held.len(), 1);
    assert_eq!(images.held.values().next().unwrap(), b"keep");
    drop(images);
    runtime.stop();
}

#[test]
fn failed_replay_admission_drops_only_the_failed_image_and_cancels_its_loan() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    ctx.push_image("p", "image", b"held").unwrap();
    let panel = ctx.panel("p");
    for _ in 0..256 {
        panel.set_rows("rows", ["a"]).unwrap();
    }
    arena(&ctx);
    ctx.flush_panel_images("p");
    assert!(ctx.core.images.lock().unwrap().held.is_empty());
    assert_eq!(ctx.core.images.lock().unwrap().bytes, 0);
    // Canceling the refused reservation leaves the entire arena reusable.
    let loan = ctx.memory().reserve(1, &vec![0; 4032]).unwrap();
    ctx.memory().cancel_unsent(loan).unwrap();
    assert_eq!(ctx.core.queues.lock().unwrap().pushes.len(), 256);
    assert!(!ctx.core.queues.lock().unwrap().closed);
    runtime.stop();
}

#[test]
fn panel_hold_budget_counts_all_keys_but_excludes_replaced_bytes() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let bytes = vec![0; 8 * 1024 * 1024];
    ctx.push_image_row("p", "image", "a", &bytes).unwrap();
    ctx.push_image_row("p", "image", "b", &bytes).unwrap();
    ctx.push_image_row("p", "image", "a", &bytes).unwrap();
    assert!(ctx.push_image_row("p", "image", "c", b"excess").is_err());
    assert_eq!(ctx.core.images.lock().unwrap().bytes, 16 * 1024 * 1024);
    ctx.push_image_row("p", "image", "a", b"smaller").unwrap();
    ctx.push_image_row("p", "image", "c", b"fits now").unwrap();
    runtime.stop();
}

#[test]
fn raw_rows_over_the_manifest_limit_are_refused_before_queueing_or_pruning() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    ctx.push_image_row("p", "image", "held", b"keep").unwrap();
    let patch = Plugin::PanelPatch(pb::PanelPatch {
        panel_id: "p".into(),
        ops: vec![pb::PatchOp {
            widget_id: "rows".into(),
            row_key: String::new(),
            op: Some(pb::patch_op::Op::SetRows(pb::RowKeys {
                keys: (0..257).map(|n| n.to_string()).collect(),
            })),
        }],
    });
    assert!(ctx.push(patch).unwrap_err().message.contains("max_rows"));
    assert!(ctx.core.queues.lock().unwrap().pushes.is_empty());
    assert_eq!(ctx.core.images.lock().unwrap().bytes, 4);
    runtime.stop();
}

#[test]
fn a_thousand_images_fit_the_queue_both_live_and_on_install() {
    for held in [false, true] {
        let (runtime, _peer) = runtime();
        let ctx = runtime.context();
        if !held {
            arena_sized(&ctx, 1 << 20);
        }
        let rows: Vec<_> = (0..1024)
            .map(|n| (n.to_string(), b"png".to_vec()))
            .collect();
        ctx.push_image_rows("p", "image", &rows).unwrap();
        if held {
            assert!(ctx.core.queues.lock().unwrap().pushes.is_empty());
            arena_sized(&ctx, 1 << 20);
            ctx.flush_panel_images("p");
        }
        assert!(ctx.core.images.lock().unwrap().held.is_empty());
        let q = ctx.core.queues.lock().unwrap();
        assert_eq!(q.pushes.len(), 16);
        assert!(q
            .pushes
            .iter()
            .all(|m| matches!(&m.msg, Some(Plugin::PanelPatch(p)) if p.ops.len() == 64)));
        drop(q);
        runtime.stop();
    }
}

#[test]
fn image_batch_allocation_failure_cancels_earlier_unsent_reservations() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    arena(&ctx);
    let rows: Vec<_> = (0..64).map(|n| (n.to_string(), vec![0; 64])).collect();
    assert!(ctx.push_image_rows("p", "image", &rows).is_err());
    assert!(ctx.core.queues.lock().unwrap().pushes.is_empty());
    let loan = ctx.memory().reserve(1, &vec![0; 4032]).unwrap();
    ctx.memory().cancel_unsent(loan).unwrap();
    runtime.stop();
}
