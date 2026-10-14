use super::*;

async fn subscribed(app: Registry) -> Session {
    let mut value = manifest().value().clone();
    value["permissions"] = serde_json::json!(["data_access"]);
    value["subscriptions"] = serde_json::json!(["view", "regions"]);
    let manifest = Manifest::parse(&value.to_string()).unwrap();
    Session::with_runtime(
        move |bell, listener| Runtime::new(manifest, Arc::new(app), bell, listener),
        1,
        23,
        "Hello from subscribed-state fixture",
    )
    .await
    .unwrap()
}
fn change(id: &str, revision: u64) -> pb::RegionChange {
    pb::RegionChange {
        region_id: id.into(),
        revision,
        origin: if revision == 0 { "" } else { "user" }.into(),
        deleted: revision == 0,
    }
}
fn regions(generation: u64, reset: bool, changes: Vec<pb::RegionChange>) -> Host {
    Host::RegionsChanged(pb::RegionsChanged {
        open_generation: generation,
        reset,
        changes,
    })
}
async fn observed<T>(rx: &mut tokio::sync::mpsc::UnboundedReceiver<T>) -> T {
    tokio::time::timeout(Duration::from_secs(5), rx.recv())
        .await
        .unwrap()
        .unwrap()
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn subscribed_workers_keep_latest_view_merge_regions_and_can_call_host() {
    let (views, mut view_rx) = tokio::sync::mpsc::unbounded_channel();
    let (regions_tx, mut regions_rx) = tokio::sync::mpsc::unbounded_channel();
    let app = Registry::new()
        .on_view_changed(move |ctx, v| {
            views.send(v).unwrap();
            ctx.get_colormap("gray")?;
            Ok(())
        })
        .on_regions_changed(move |ctx, r| {
            regions_tx.send(r).unwrap();
            ctx.get_rois()?;
            Ok(())
        })
        .command("run", |_, _| Ok(done()));
    let mut s = subscribed(app).await;
    s.recv().await;
    s.send(
        "",
        Host::ViewChanged(pb::ViewChanged {
            open_generation: 7,
            ..Default::default()
        }),
    )
    .await;
    assert_eq!(observed(&mut view_rx).await.open_generation, 7);
    let view_request = s.recv().await;
    assert!(matches!(view_request.msg, Some(Plugin::GetColormap(_))));
    // The view callback is blocked on its host request. Regions and commands
    // still run, and the dispatcher continues acknowledging arrangement queries.
    s.send("", regions(7, true, vec![change("initial", 1)]))
        .await;
    assert!(observed(&mut regions_rx).await.reset);
    let region_request = s.recv().await;
    assert!(matches!(region_request.msg, Some(Plugin::GetRois(_))));
    s.send(
        "command",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::CommandDone(_))));
    for t in 1..=10 {
        s.send(
            "",
            Host::ViewChanged(pb::ViewChanged {
                open_generation: 7,
                t,
                z: 3,
                visible_channels: vec![2, 5],
                selected_region_id: "b".into(),
                filter: Some(pb::AppliedFilter {
                    plugin_id: "filter.plugin".into(),
                    plugin_version: "1".into(),
                    stage_id: "stage".into(),
                    params: [("value".into(), (t as i64).into())].into_iter().collect(),
                    channels: vec![2],
                }),
            }),
        )
        .await;
    }
    s.send("", regions(7, false, vec![change("a", 4), change("b", 5)]))
        .await;
    s.send("", regions(7, false, vec![change("a", 9), change("c", 6)]))
        .await;
    s.send("", regions(7, false, vec![change("a", 2), change("b", 0)]))
        .await;
    s.barrier().await; // All queued pushes were ingested before unblocking.
    s.send(
        &view_request.id,
        Host::ColormapTable(pb::ColormapTable {
            name: "gray".into(),
            rgba: vec![0; 1024],
        }),
    )
    .await;
    let latest = observed(&mut view_rx).await;
    assert_eq!(
        (
            latest.t,
            latest.z,
            latest.visible_channels,
            latest.selected_region_id
        ),
        (10, 3, vec![2, 5], "b".into())
    );
    let filter = latest.filter.unwrap();
    assert_eq!(filter.plugin_id, "filter.plugin");
    assert_eq!(filter.params["value"], 10_i64.into());
    let blocked_view = s.recv().await;
    assert!(matches!(blocked_view.msg, Some(Plugin::GetColormap(_))));
    s.send(&region_request.id, Host::Rois(pb::RoiList::default()))
        .await;
    let merged = observed(&mut regions_rx).await;
    assert!(!merged.reset);
    assert_eq!(
        merged.changes,
        vec![change("a", 2), change("b", 0), change("c", 6)]
    );
    let blocked_regions = s.recv().await;
    assert!(matches!(blocked_regions.msg, Some(Plugin::GetRois(_))));
    // A reset voids all pending old changes; subsequent deltas retain reset.
    s.send("", regions(7, false, vec![change("old", 90)])).await;
    s.send("", regions(8, true, vec![change("new", 1)])).await;
    s.send(
        "",
        regions(8, false, vec![change("new", 2), change("other", 3)]),
    )
    .await;
    s.barrier().await;
    s.send(&blocked_regions.id, Host::Rois(pb::RoiList::default()))
        .await;
    let reset = observed(&mut regions_rx).await;
    assert_eq!((reset.open_generation, reset.reset), (8, true));
    assert_eq!(reset.changes, vec![change("new", 2), change("other", 3)]);
    assert!(matches!(s.recv().await.msg, Some(Plugin::GetRois(_))));
    assert!(view_rx.try_recv().is_err());
    assert!(regions_rx.try_recv().is_err());
    // Closing the dataset clears the optional applied filter too.
    s.send("", Host::ViewChanged(pb::ViewChanged::default()))
        .await;
    s.barrier().await;
    s.send(
        &blocked_view.id,
        Host::ColormapTable(pb::ColormapTable {
            name: "gray".into(),
            rgba: vec![0; 1024],
        }),
    )
    .await;
    let closed = observed(&mut view_rx).await;
    assert_eq!(closed.open_generation, 0);
    assert!(closed.filter.is_none());
    assert!(matches!(s.recv().await.msg, Some(Plugin::GetColormap(_))));
    // Shutdown wakes callbacks waiting on the host and drops queued state.
    s.send("", regions(8, false, vec![change("never-called", 100)]))
        .await;
    s.send("", Host::ViewChanged(pb::ViewChanged::default()))
        .await;
    s.barrier().await;
    s.stop().await;
    assert!(tokio::time::timeout(Duration::from_secs(5), view_rx.recv())
        .await
        .unwrap()
        .is_none());
    assert!(
        tokio::time::timeout(Duration::from_secs(5), regions_rx.recv())
            .await
            .unwrap()
            .is_none()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn host_helpers_preserve_identity_validate_tables_and_route_save_replies() {
    let mut s = Session::new(Registry::new()).await;
    s.recv().await;
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || {
        ctx.get_rois_selected(&["one".into(), "missing".into()], Some(3), Some(7))
    });
    let q = s.recv().await;
    assert!(
        matches!(q.msg, Some(Plugin::GetRois(r)) if r.region_ids == ["one", "missing"] && r.pose_t_start == Some(3) && r.pose_t_end == Some(7))
    );
    let record = pb::RoiRecord {
        region_id: "one".into(),
        revision: u64::MAX,
        content_hash: "0123456789abcdef".into(),
        ..Default::default()
    };
    s.send(
        &q.id,
        Host::Rois(pb::RoiList {
            rois: vec![record.clone()],
        }),
    )
    .await;
    assert_eq!(task.await.unwrap().unwrap(), vec![record]);
    let ctx = s.ctx();
    let task = tokio::task::spawn_blocking(move || ctx.view_state());
    let q = s.recv().await;
    let view = pb::ViewState {
        selected_region_id: "one".into(),
        time_increment: 0.25,
        time_unit: "s".into(),
        ..Default::default()
    };
    s.send(&q.id, Host::ViewState(view.clone())).await;
    assert_eq!(task.await.unwrap().unwrap(), view);
    for (name, len, error) in [
        ("gray", 1024, None),
        ("other", 1024, Some(pb::error::Kind::Protocol)),
        ("gray", 1023, Some(pb::error::Kind::Protocol)),
        ("gray", 0, Some(pb::error::Kind::NotFound)),
    ] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || ctx.get_colormap("gray"));
        let q = s.recv().await;
        assert!(matches!(q.msg, Some(Plugin::GetColormap(c)) if c.name == "gray"));
        let response = if len == 0 {
            Host::HostError(pb::Error {
                kind: pb::error::Kind::NotFound as i32,
                message: "unknown colormap".into(),
            })
        } else {
            Host::ColormapTable(pb::ColormapTable {
                name: name.into(),
                rgba: vec![123; len],
            })
        };
        s.send(&q.id, response).await;
        let result = task.await.unwrap();
        if let Some(kind) = error {
            assert_eq!(result.unwrap_err().kind, kind);
        } else {
            assert_eq!(result.unwrap().rgba, vec![123; 1024]);
        }
    }
    for (path, declined) in [("/tmp/measure.csv", false), ("", true), ("", false)] {
        let ctx = s.ctx();
        let task = tokio::task::spawn_blocking(move || {
            ctx.request_save_path(
                "measure.csv",
                "csv",
                "Save measurements",
                Duration::from_secs(5),
            )
        });
        let q = s.recv().await;
        assert!(
            matches!(q.msg, Some(Plugin::RequestSavePath(p)) if p.suggested_name == "measure.csv" && p.extension == "csv" && p.title == "Save measurements")
        );
        s.barrier().await;
        s.send(
            &q.id,
            Host::SavePathResult(pb::SavePathResult {
                path: path.into(),
                declined,
            }),
        )
        .await;
        let result = task.await.unwrap();
        if path.is_empty() && !declined {
            assert_eq!(result.unwrap_err().kind, pb::error::Kind::Protocol);
        } else {
            assert_eq!(result.unwrap().declined, declined);
        }
    }
    // Generic requests have the same new-arm routing, including host refusal.
    let request = s
        .ctx()
        .start_request(Plugin::RequestSavePath(pb::RequestSavePath {
            suggested_name: "dir/file".into(),
            ..Default::default()
        }))
        .unwrap();
    let q = s.recv().await;
    s.send(
        &q.id,
        Host::HostError(pb::Error {
            kind: pb::error::Kind::InvalidArgument as i32,
            message: "suggested_name is a path".into(),
        }),
    )
    .await;
    assert_eq!(
        tokio::task::spawn_blocking(move || request.wait(Duration::from_secs(5)))
            .await
            .unwrap()
            .err()
            .unwrap()
            .kind,
        pb::error::Kind::InvalidArgument
    );
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn integer_lists_roundtrip_through_commands_and_panel_patches() {
    let values = vec![i64::MIN, -1, 0, 2, i64::MAX];
    let expected = values.clone();
    let mut s = Session::new(Registry::new().command("run", move |ctx, command| {
        let Some(pb::value::Kind::Ints(list)) = &command.args["value"].kind else {
            panic!("missing list")
        };
        assert_eq!(list.values, expected);
        ctx.panel("panel")
            .set_value("status", list.values.clone())?;
        Ok(done())
    }))
    .await;
    s.recv().await;
    s.send(
        "list",
        Host::InvokeCommand(pb::InvokeCommand {
            cause: 0,
            command_id: "run".into(),
            args: [("value".into(), values.clone().into())].into(),
        }),
    )
    .await;
    let mut patched = false;
    let mut done = false;
    while !patched || !done {
        match s.recv().await.msg {
            Some(Plugin::PanelPatch(p)) => {
                assert_eq!(
                    p.ops[0].op,
                    Some(pb::patch_op::Op::SetValue(values.clone().into()))
                );
                patched = true;
            }
            Some(Plugin::CommandDone(_)) => done = true,
            other => panic!("{other:?}"),
        }
    }
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn save_picker_holds_command_budget_like_asset_picker() {
    let mut s = Session::new(Registry::new().command("run", |ctx, _| {
        assert!(
            ctx.request_save_path("out.csv", "csv", "", Duration::from_secs(30))?
                .declined
        );
        Ok(done())
    }))
    .await;
    s.recv().await;
    s.send(
        "invoke",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "run".into(),
            ..Default::default()
        }),
    )
    .await;
    let q = s.recv().await;
    assert!(matches!(q.msg, Some(Plugin::RequestSavePath(_))));
    tokio::time::sleep(Duration::from_millis(10_200)).await;
    s.barrier().await;
    s.send(
        &q.id,
        Host::SavePathResult(pb::SavePathResult {
            declined: true,
            path: String::new(),
        }),
    )
    .await;
    let answer = s.recv().await;
    assert_eq!(answer.id, "invoke");
    assert!(matches!(answer.msg, Some(Plugin::CommandDone(c)) if c.ok));
    s.stop().await;
}
