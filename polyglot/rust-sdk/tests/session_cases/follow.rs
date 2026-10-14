use super::*;
use vizi_doorbell::control::{Follow, ViewMove};

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn declared_follow_receives_every_host_end_reason_and_future_values() {
    use vizi_doorbell::declare::{PanelSpec, PluginBuilder, ViewTarget, Widget};
    let mut builder = PluginBuilder::new("test.follow", "Follow", "1");
    let mut panel = PanelSpec::new("panel", "Panel", Widget::label("status", "Ready"));
    panel.follow_targets = vec![ViewTarget::T];
    builder.panel(panel);
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    builder.follow("panel", move |_, follow| {
        tx.send(follow.clone())
            .unwrap_or_else(|_| panic!("receiver gone"));
        follow.wait(Duration::from_secs(60));
        Ok(())
    });
    let plugin = builder.build().unwrap();
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from Follow fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    for reason in (1..=10).chain([200]) {
        let id = reason as u64;
        s.send("", start(id, "panel", false)).await;
        let follow = tokio::time::timeout(Duration::from_secs(5), rx.recv())
            .await
            .unwrap()
            .unwrap();
        let ended = pb::FollowEnded {
            follow_id: id,
            reason,
            detail: format!("reason {reason}"),
        };
        s.send("", Host::FollowEnded(ended.clone())).await;
        s.barrier().await;
        assert!(!follow.active());
        assert_eq!(follow.ended(), Some(ended));
    }
    s.stop().await;
}

fn start(id: u64, panel: &str, resumed: bool) -> Host {
    Host::FollowStarted(pb::FollowStarted {
        follow_id: id,
        panel_id: panel.into(),
        targets: vec!["t".into(), "look_at".into()],
        resumed,
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn follow_worker_moves_waits_and_host_end_wakes_without_echo() {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    let (finished, finish) = tokio::sync::oneshot::channel();
    let finished = Mutex::new(Some(finished));
    let app = Registry::new().follow("panel", move |ctx, follow| {
        assert_eq!(follow.id(), u64::MAX);
        assert_eq!(follow.panel_id(), "panel");
        assert!(follow.resumed());
        assert!(follow.move_view(ViewMove::default()).is_err());
        assert!(follow
            .move_view(ViewMove {
                z: Some(0),
                ..Default::default()
            })
            .is_err());
        assert!(follow.move_view(ViewMove {
            t: Some(0),
            ..Default::default()
        })?);
        tx.lock()
            .unwrap()
            .take()
            .unwrap()
            .send(follow.clone())
            .unwrap_or_else(|_| panic!("receiver gone"));
        assert!(!follow.wait(Duration::from_secs(60)));
        assert!(!follow.move_view(ViewMove {
            t: Some(5),
            ..Default::default()
        })?);
        follow.end()?;
        // Host requests from this worker remain possible after an individual Follow ends.
        ctx.panel("panel").set_value("status", "stopped")?;
        finished.lock().unwrap().take().unwrap().send(()).unwrap();
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send("", start(u64::MAX, "panel", true)).await;
    let message = s.recv().await;
    let Some(Plugin::FollowState(state)) = message.msg else {
        panic!("expected state")
    };
    assert!(message.id.is_empty());
    assert_eq!(
        (state.follow_id, state.seq, state.t, state.z),
        (u64::MAX, 1, Some(0), None)
    );
    let follow: Follow = rx.await.unwrap();
    assert!(follow
        .move_view(ViewMove {
            look_at: Some(pb::LookAt {
                target_zyx: vec![1., 2., 3.],
                fit_extent: 10.,
                direction_zyx: vec![]
            }),
            ..Default::default()
        })
        .unwrap());
    let Some(Plugin::FollowState(state)) = s.recv().await.msg else {
        panic!("expected state")
    };
    assert_eq!(state.seq, 2);
    assert_eq!(state.t, None, "states are whole, not accumulated deltas");
    assert_eq!(state.look_at.unwrap().target_zyx, [1., 2., 3.]);
    // This barrier completes while the handler is sleeping, proving the dispatcher is free.
    s.barrier().await;
    let end = pb::FollowEnded {
        follow_id: u64::MAX,
        reason: pb::FollowEndReason::FollowEndInvalid as i32,
        detail: "t=9 is outside 0..2".into(),
    };
    s.send("", Host::FollowEnded(end.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::PanelPatch(_))));
    tokio::time::timeout(Duration::from_secs(5), finish)
        .await
        .unwrap()
        .unwrap();
    assert_eq!(follow.ended(), Some(end));
    s.barrier().await; // No EndFollow was echoed.
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn follow_return_error_panic_missing_handler_and_explicit_end() {
    let app = Registry::new()
        .follow("return", |_, _| Ok(()))
        .follow("error", |_, _| {
            Err(vizi_doorbell::control::Error::internal("test error"))
        })
        .follow("panic", |_, _| panic!("test panic"))
        .follow("explicit", |_, f| {
            f.move_view(ViewMove {
                t: Some(0),
                ..Default::default()
            })?;
            f.end()?;
            f.end()?;
            assert!(!f.active());
            assert!(!f.wait(Duration::ZERO));
            assert_eq!(
                f.ended().unwrap().reason,
                pb::FollowEndReason::FollowEndPluginEnded as i32
            );
            Ok(())
        });
    let mut s = Session::new(app).await;
    s.recv().await;
    for (id, panel) in ["return", "error", "panic", "missing", "explicit"]
        .into_iter()
        .enumerate()
    {
        let id = id as u64 + 1;
        s.send("", start(id, panel, false)).await;
        if panel == "explicit" {
            assert!(
                matches!(s.recv().await.msg, Some(Plugin::FollowState(_))),
                "final state must precede EndFollow"
            );
        }
        let Some(Plugin::EndFollow(end)) = s.recv().await.msg else {
            panic!("expected EndFollow")
        };
        assert_eq!(end.follow_id, id);
        s.send(
            "",
            Host::FollowEnded(pb::FollowEnded {
                follow_id: id,
                reason: pb::FollowEndReason::FollowEndPluginEnded as i32,
                detail: String::new(),
            }),
        )
        .await;
        s.barrier().await;
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn follow_session_stop_wakes_waiters_without_inventing_reason() {
    let (tx, rx) = tokio::sync::oneshot::channel();
    let tx = Mutex::new(Some(tx));
    let (done, finished) = tokio::sync::oneshot::channel();
    let done = Mutex::new(Some(done));
    let app = Registry::new().follow("panel", move |_, follow| {
        tx.lock()
            .unwrap()
            .take()
            .unwrap()
            .send(follow.clone())
            .unwrap_or_else(|_| panic!("receiver gone"));
        assert!(!follow.wait(Duration::from_secs(60)));
        assert!(follow.ended().is_none());
        follow.end()?;
        done.lock().unwrap().take().unwrap().send(()).unwrap();
        Ok(())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send("", start(7, "panel", false)).await;
    let follow = rx.await.unwrap();
    s.stop().await;
    tokio::time::timeout(Duration::from_secs(5), finished)
        .await
        .unwrap()
        .unwrap();
    assert!(!follow.active());
}

#[test]
fn missing_follow_driver_names_the_panel_in_a_diagnostic() {
    let output = std::process::Command::new(std::env::current_exe().unwrap())
        .args([
            "--exact",
            "follow_cases::follow_return_error_panic_missing_handler_and_explicit_end",
            "--nocapture",
        ])
        .output()
        .unwrap();
    let stderr = String::from_utf8_lossy(&output.stderr);
    assert!(
        output.status.success(),
        "{}\n{stderr}",
        String::from_utf8_lossy(&output.stdout)
    );
    assert_eq!(
        stderr
            .matches("no Follow driver for panel \"missing\"; ending Follow")
            .count(),
        1,
        "{stderr}"
    );
}
