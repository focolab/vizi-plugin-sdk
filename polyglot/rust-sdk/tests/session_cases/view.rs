use super::*;
use vizi_doorbell::control::{Input, ViewMove};

fn result(request: &pb::ViewRequest, outcome: pb::ViewOutcome, reason: pb::ViewRefusal) -> Host {
    Host::ViewRequestResult(pb::ViewRequestResult {
        cause: request.cause,
        tool_cause: request.tool_cause,
        outcome: outcome as i32,
        reason: reason as i32,
        detail: format!("{reason:?}"),
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn view_command_cause_and_image_args_wait_for_the_acknowledgement() {
    let observed = Arc::new(Mutex::new(None));
    let got = observed.clone();
    let app = Registry::new().command("run", move |ctx, command| {
        assert_eq!(command.args["x"], 12_i64.into());
        assert_eq!(command.args["y"], 9_i64.into());
        assert_eq!(command.args["button"], "left".into());
        assert_eq!(command.args["modifiers"], "shift+ctrl".into());
        assert_eq!(command.args["image"], 4096_i64.into());
        let result = ctx.command_view(command)?.move_view(ViewMove {
            t: Some(0),
            z: Some(4),
            ..Default::default()
        })?;
        *got.lock().unwrap() = Some(result);
        Ok(done())
    });
    let mut s = Session::new(app).await;
    s.recv().await;
    s.send(
        "invoke",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            cause: u64::MAX,
            args: [
                ("x".into(), 12_i64.into()),
                ("y".into(), 9_i64.into()),
                ("button".into(), "left".into()),
                ("modifiers".into(), "shift+ctrl".into()),
                ("image".into(), 4096_i64.into()),
            ]
            .into_iter()
            .collect(),
        }),
    )
    .await;
    let request = s.recv().await;
    let Some(Plugin::ViewRequest(view)) = request.msg else {
        panic!("expected view request")
    };
    assert_eq!(view.cause, u64::MAX);
    assert_eq!(view.t, Some(0));
    assert_eq!(view.z, Some(4));
    assert!(view.tool_cause.is_none());
    s.send(
        "",
        Host::ViewChanged(pb::ViewChanged {
            t: 0,
            ..Default::default()
        }),
    )
    .await;
    s.barrier().await;
    assert!(
        observed.lock().unwrap().is_none(),
        "a ViewChanged must not complete navigation"
    );
    s.send(
        &request.id,
        result(&view, pb::ViewOutcome::Applied, pb::ViewRefusal::None),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    assert!(observed.lock().unwrap().as_ref().unwrap().applied());
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn view_refusals_echo_checks_and_request_lifecycle() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    let cause = ctx
        .command_view(&pb::InvokeCommand {
            cause: 17,
            ..Default::default()
        })
        .unwrap();
    for reason in [
        pb::ViewRefusal::NotEnabled,
        pb::ViewRefusal::NotDeclared,
        pb::ViewRefusal::Stale,
        pb::ViewRefusal::Superseded,
        pb::ViewRefusal::Consumed,
        pb::ViewRefusal::UnknownRegion,
        pb::ViewRefusal::OutOfRange,
        pb::ViewRefusal::DatasetChanged,
        pb::ViewRefusal::FollowActive,
        pb::ViewRefusal::ExportActive,
        pb::ViewRefusal::Unsupported,
        pb::ViewRefusal::Invalid,
    ] {
        let mut pending = cause
            .start_move(ViewMove {
                select_region: Some("region".into()),
                frame_region: Some("region".into()),
                look_at: Some(pb::LookAt {
                    target_zyx: vec![1., 2., 3.],
                    fit_extent: 5.,
                    direction_zyx: vec![0., 0., 1.],
                }),
                orbit_point: Some(pb::OrbitPoint {
                    zyx: vec![4., 5., 6.],
                }),
                ..Default::default()
            })
            .unwrap();
        let request = s.recv().await;
        assert_eq!(request.id, pending.id());
        let Some(Plugin::ViewRequest(view)) = request.msg else {
            panic!("expected view request")
        };
        assert_eq!(view.frame_region.as_deref(), Some("region"));
        assert_eq!(view.look_at.as_ref().unwrap().fit_extent, 5.);
        assert_eq!(view.orbit_point.as_ref().unwrap().zyx, [4., 5., 6.]);
        assert!(pending.wait_timeout(Duration::ZERO).unwrap().is_none());
        s.send(&request.id, result(&view, pb::ViewOutcome::Refused, reason))
            .await;
        let reply = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
            .await
            .unwrap()
            .unwrap();
        assert!(!reply.applied());
        assert_eq!(reply.reason, reason as i32);
        assert_eq!(reply.detail, format!("{reason:?}"));
    }
    let pending = cause.start_decline().unwrap();
    let request = s.recv().await;
    let Some(Plugin::ViewRequest(view)) = request.msg else {
        panic!("expected decline")
    };
    assert!(view.decline);
    assert_eq!(
        view,
        pb::ViewRequest {
            cause: 17,
            decline: true,
            ..Default::default()
        }
    );
    s.send(
        &request.id,
        result(&view, pb::ViewOutcome::Declined, pb::ViewRefusal::None),
    )
    .await;
    let reply = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
        .await
        .unwrap()
        .unwrap();
    assert_eq!(reply.outcome, pb::ViewOutcome::Declined as i32);
    for outcome in [0, 999] {
        let pending = cause.start_decline().unwrap();
        let request = s.recv().await;
        let expected = pb::ViewRequestResult {
            cause: 17,
            outcome,
            reason: 999,
            detail: "future result".into(),
            ..Default::default()
        };
        s.send(&request.id, Host::ViewRequestResult(expected.clone()))
            .await;
        let reply = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(reply, expected);
        assert!(!reply.applied());
    }
    for bad in [
        pb::ViewRequestResult {
            cause: 18,
            outcome: 1,
            ..Default::default()
        },
        pb::ViewRequestResult {
            cause: 17,
            tool_cause: Some(pb::ToolCause {
                tool: 1,
                interaction: 1,
            }),
            outcome: 1,
            ..Default::default()
        },
    ] {
        let pending = cause.start_decline().unwrap();
        let request = s.recv().await;
        s.send(&request.id, Host::ViewRequestResult(bad)).await;
        assert!(
            tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
                .await
                .unwrap()
                .is_err()
        );
    }
    // Dropping/timing out doesn't cancel the host's operation; late replies are harmless.
    let pending = cause.start_decline().unwrap();
    let request = s.recv().await;
    assert!(pending.wait(Duration::ZERO).is_err());
    s.send(
        &request.id,
        Host::ViewRequestResult(pb::ViewRequestResult {
            cause: 17,
            outcome: 2,
            ..Default::default()
        }),
    )
    .await;
    s.barrier().await;
    let pending = cause.start_decline().unwrap();
    s.recv().await;
    s.runtime.stop();
    assert!(
        tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
            .await
            .unwrap()
            .is_err()
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn view_causes_are_checked_locally_and_fire_uses_captured_ids() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    assert!(ctx.command_view(&pb::InvokeCommand::default()).is_err());
    let cause = ctx
        .command_view(&pb::InvokeCommand {
            cause: 1,
            ..Default::default()
        })
        .unwrap();
    assert!(cause.start_move(ViewMove::default()).is_err());
    for request in [
        pb::ViewRequest {
            t: Some(1),
            ..Default::default()
        },
        pb::ViewRequest {
            cause: 1,
            tool_cause: Some(pb::ToolCause {
                tool: 2,
                interaction: 3,
            }),
            decline: true,
            ..Default::default()
        },
        pb::ViewRequest {
            cause: 1,
            decline: true,
            t: Some(0),
            ..Default::default()
        },
        pb::ViewRequest {
            tool_cause: Some(pb::ToolCause {
                tool: 0,
                interaction: 3,
            }),
            decline: true,
            ..Default::default()
        },
    ] {
        assert!(ctx.start_view_request(request).is_err());
    }
    let mut input = Input {
        event: example_event(Position::None),
        implicitly_closed: None,
        state: None,
        usable_context: false,
        binding: None,
    };
    for phase in [Phase::Press, Phase::Drag, Phase::Release, Phase::Cancel] {
        input.event.phase = phase;
        assert!(ctx.input_view(&input).is_err());
    }
    s.barrier().await;
    input.event.phase = Phase::Fire;
    input.event.tool = 23;
    for action in [0, 2] {
        input.event.action = action;
        input.event.interaction = 45 + u32::from(action) * 2;
        let cause = ctx.input_view(&input).unwrap();
        let pending = cause
            .start_move(ViewMove {
                t: Some(1),
                look_at: Some(pb::LookAt {
                    target_zyx: vec![1., 2., 3.],
                    fit_extent: 8.,
                    direction_zyx: vec![],
                }),
                orbit_point: Some(pb::OrbitPoint {
                    zyx: vec![4., 5., 6.],
                }),
                ..Default::default()
            })
            .unwrap();
        let request = s.recv().await;
        let Some(Plugin::ViewRequest(view)) = request.msg else {
            panic!("expected tool move")
        };
        assert_eq!(view.cause, 0);
        assert_eq!(
            view.tool_cause,
            Some(pb::ToolCause {
                tool: 23,
                interaction: input.event.interaction
            })
        );
        s.send(
            &request.id,
            result(&view, pb::ViewOutcome::Applied, pb::ViewRefusal::None),
        )
        .await;
        let reply = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
            .await
            .unwrap()
            .unwrap();
        assert!(reply.applied());
        assert_eq!(view.look_at.as_ref().unwrap().target_zyx, [1., 2., 3.]);
        assert_eq!(view.orbit_point.as_ref().unwrap().zyx, [4., 5., 6.]);
        // A fresh FIRE is a fresh cause: do not answer the applied move twice.
        input.event.interaction += 1;
        let pending = ctx.input_view(&input).unwrap().start_decline().unwrap();
        let request = s.recv().await;
        let Some(Plugin::ViewRequest(decline)) = request.msg else {
            panic!("expected tool decline")
        };
        assert!(decline.decline);
        assert_eq!(
            decline.tool_cause,
            Some(pb::ToolCause {
                tool: 23,
                interaction: input.event.interaction
            })
        );
        assert_eq!(decline.cause, 0);
        s.send(
            &request.id,
            result(&decline, pb::ViewOutcome::Declined, pb::ViewRefusal::None),
        )
        .await;
        let reply = tokio::task::spawn_blocking(move || pending.wait(Duration::from_secs(3)))
            .await
            .unwrap()
            .unwrap();
        assert_eq!(reply.outcome, pb::ViewOutcome::Declined as i32);
    }
    s.stop().await;
}
