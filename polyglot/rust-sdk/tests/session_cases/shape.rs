use super::*;
use vizi_doorbell::{proto::pois_math::v1 as roi, shape::Shape};

async fn session(registry: Registry) -> Session {
    let mut value = manifest().value().clone();
    value["permissions"] = serde_json::json!(["contribute_rois"]);
    value["contributions"]["region_types"] = serde_json::json!([{"id":"tetra"}]);
    let manifest = Manifest::parse(&value.to_string()).unwrap();
    let mut s = Session::with_runtime(
        |bell, listener| Runtime::new(manifest, Arc::new(registry), bell, listener),
        1,
        0,
        "Hello from Rust shape fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    s
}
fn edit(id: u64, seq: u32) -> pb::ShapeEdit {
    pb::ShapeEdit {
        drag_id: id,
        seq,
        type_name: "tetra".into(),
        region_id: "copy-owned-by-user".into(),
        // Copies retain type_owner but have no contribution id here.
        contrib_id: String::new(),
        r#move: Some(roi::ConstrainMoveRequest {
            roi: Some(roi::Roi {
                type_name: "tetra".into(),
                vertices: vec![roi::Vertex::default(); 4],
                ..Default::default()
            }),
            p1: Some(roi::Vec3 {
                z: seq as f64,
                y: 2.,
                x: 3.,
                ..Default::default()
            }),
            ..Default::default()
        }),
    }
}
fn positions(edit: &pb::ShapeEdit) -> Vec<roi::Vec3> {
    vec![edit.r#move.as_ref().unwrap().p1.clone().unwrap(); 4]
}
async fn answer(s: &mut Session, id: u64) -> pb::ShapeResult {
    let m = s.recv().await;
    assert!(m.id.is_empty());
    let Some(Plugin::ShapeAnswer(result)) = m.msg else {
        panic!("{m:?}")
    };
    assert_eq!((result.drag_id, result.seq), (id, 0));
    result
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn silent_missing_shaper_answers_spinbox_commit_and_late_drag_is_ignored() {
    let mut s = session(Registry::new()).await;
    s.send("", Host::ShapeCommit(edit(1, 0))).await;
    let r = answer(&mut s, 1).await;
    assert_eq!(r.resolution, roi::Resolution::Rejected as i32);
    assert!(
        r.positions.is_empty()
            && r.message.contains("no shaper registered")
            && r.message.contains("tetra")
    );
    s.send("", Host::ShapeDrag(edit(1, 9))).await;
    s.send(
        "",
        Host::ShapeEditEnded(pb::ShapeEditEnded {
            drag_id: 99,
            outcome: 3,
            detail: "never proposed".into(),
        }),
    )
    .await;
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn proposals_clamping_and_all_end_outcomes_preserve_full_state() {
    let (tx, rx) = std::sync::mpsc::channel();
    let registry = Registry::new()
        .shaper("tetra", |_, e| {
            assert!(e.contrib_id.is_empty());
            assert_eq!(e.region_id, "copy-owned-by-user");
            Ok(if e.seq == 0 {
                Shape::clamped(positions(e))
            } else {
                Shape::ok(positions(e))
            })
        })
        .on_shape_edit_ended(move |_, ended| {
            tx.send(ended).unwrap();
            Ok(())
        });
    let mut s = session(registry).await;
    for outcome in 1..=3 {
        let id = outcome as u64;
        s.send("", Host::ShapeDrag(edit(id, 1))).await;
        let Some(Plugin::ShapeProposal(r)) = s.recv().await.msg else {
            panic!()
        };
        assert_eq!(
            (r.drag_id, r.seq, r.resolution),
            (id, 1, roi::Resolution::Ok as i32)
        );
        assert_eq!(r.positions, positions(&edit(id, 1)));
        s.send("", Host::ShapeCommit(edit(id, 0))).await;
        let r = answer(&mut s, id).await;
        assert_eq!(r.resolution, roi::Resolution::Clamped as i32);
        assert_eq!(r.positions, positions(&edit(id, 0)));
        let ended = pb::ShapeEditEnded {
            drag_id: id,
            outcome,
            detail: format!("outcome {outcome}"),
        };
        s.send("", Host::ShapeEditEnded(ended.clone())).await;
        assert_eq!(rx.recv_timeout(Duration::from_secs(3)).unwrap(), ended);
    }
    let unknown = pb::ShapeEditEnded {
        drag_id: 99,
        outcome: 3,
        detail: "no proposal".into(),
    };
    s.send("", Host::ShapeEditEnded(unknown.clone())).await;
    assert_eq!(rx.recv_timeout(Duration::from_secs(3)).unwrap(), unknown);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn invalid_shapes_and_errors_become_rejected_answers_and_proposals() {
    let mut s = session(Registry::new().shaper("tetra", |_, e| {
        let mut p = positions(e);
        match e.drag_id {
            1 => {
                p.pop();
            }
            2 => p[0].x = f64::NAN,
            3 => p[0].y = f64::INFINITY,
            4 => {
                return Ok(Shape {
                    resolution: roi::Resolution::Rejected,
                    positions: p,
                    message: "invalid rejection".into(),
                })
            }
            5 => return Err(control::Error::internal("unable to shape")),
            6 => return Ok(Shape::rejected("constraint cannot be satisfied")),
            7 => {
                return Ok(Shape {
                    message: "x".repeat(8 * 1024 * 1024 + 1),
                    ..Shape::ok(p)
                })
            }
            _ => panic!("shape calculation panic"),
        }
        Ok(Shape::ok(p))
    }))
    .await;
    s.send("", Host::ShapeDrag(edit(2, 1))).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ShapeProposal(r))
        if r.resolution == 2 && r.positions.is_empty() && !r.message.is_empty()));
    for id in 1..=8 {
        s.send("", Host::ShapeCommit(edit(id, 0))).await;
        let r = answer(&mut s, id).await;
        assert_eq!(r.resolution, roi::Resolution::Rejected as i32);
        assert!(r.positions.is_empty() && !r.message.is_empty());
    }
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn slow_previews_coalesce_to_latest_without_emitting_obsolete_result() {
    let (started, observed) = std::sync::mpsc::channel();
    let (release, gate) = std::sync::mpsc::channel();
    let gate = Mutex::new(gate);
    let mut s = session(Registry::new().shaper("tetra", move |_, e| {
        started.send(e.seq).unwrap();
        if e.seq == 1 {
            gate.lock()
                .unwrap()
                .recv_timeout(Duration::from_secs(5))
                .unwrap();
        }
        Ok(Shape::ok(positions(e)))
    }))
    .await;
    s.send("", Host::ShapeDrag(edit(1, 1))).await;
    assert_eq!(observed.recv_timeout(Duration::from_secs(3)).unwrap(), 1);
    for seq in 2..=30 {
        s.send("", Host::ShapeDrag(edit(1, seq))).await;
    }
    s.barrier().await;
    release.send(()).unwrap();
    let Some(Plugin::ShapeProposal(r)) = s.recv().await.msg else {
        panic!()
    };
    assert_eq!(r.seq, 30);
    assert_eq!(observed.recv_timeout(Duration::from_secs(3)).unwrap(), 30);
    assert!(observed.try_recv().is_err());
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn silent_commit_and_end_overtake_blocked_preview_and_command() {
    let (started, observed) = std::sync::mpsc::channel();
    let (release, gate) = std::sync::mpsc::channel();
    let gate = Mutex::new(gate);
    let (command_release, command_gate) = std::sync::mpsc::channel();
    let command_gate = Mutex::new(command_gate);
    let (ended, outcomes) = std::sync::mpsc::channel();
    let calls = Arc::new(AtomicUsize::new(0));
    let counted = calls.clone();
    let mut s = session(
        Registry::new()
            .command("run", move |ctx, _| {
                ctx.report_progress(None, "blocked command")?;
                command_gate
                    .lock()
                    .unwrap()
                    .recv_timeout(Duration::from_secs(5))
                    .unwrap();
                Ok(done())
            })
            .shaper("tetra", move |_, e| {
                counted.fetch_add(1, Ordering::SeqCst);
                if e.seq != 0 {
                    started.send(e.drag_id).unwrap();
                    gate.lock()
                        .unwrap()
                        .recv_timeout(Duration::from_secs(5))
                        .unwrap();
                }
                Ok(Shape::ok(positions(e)))
            })
            .on_shape_edit_ended(move |_, e| {
                ended.send(e).unwrap();
                Ok(())
            }),
    )
    .await;
    s.send(
        "busy",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::WorkProgress(_))));
    s.send("", Host::ShapeDrag(edit(1, 1))).await;
    assert_eq!(observed.recv_timeout(Duration::from_secs(3)).unwrap(), 1);
    s.send("", Host::ShapeCommit(edit(1, 0))).await;
    assert_eq!(answer(&mut s, 1).await.resolution, 0);
    // A different region can commit while both the first preview and command wait.
    s.send("", Host::ShapeCommit(edit(2, 0))).await;
    assert_eq!(answer(&mut s, 2).await.resolution, 0);
    s.send("", Host::ShapeDrag(edit(1, 2))).await;
    s.send(
        "",
        Host::ShapeEditEnded(pb::ShapeEditEnded {
            drag_id: 1,
            outcome: 1,
            detail: "committed".into(),
        }),
    )
    .await;
    assert_eq!(
        outcomes
            .recv_timeout(Duration::from_secs(3))
            .unwrap()
            .detail,
        "committed"
    );
    s.barrier().await;
    assert_eq!(calls.load(Ordering::SeqCst), 3);
    release.send(()).unwrap();
    command_release.send(()).unwrap();
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    s.barrier().await;
    s.stop().await;
}

#[test]
fn silent_orderings_produce_no_stderr_diagnostic() {
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "shape_cases::silent_",
            "--skip",
            "shape_cases::silent_orderings_produce_no_stderr_diagnostic",
            "--nocapture",
        ])
        .output()
        .unwrap();
    assert!(
        output.status.success(),
        "{}\n{}",
        String::from_utf8_lossy(&output.stdout),
        String::from_utf8_lossy(&output.stderr)
    );
    assert!(
        output.stderr.is_empty(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}
