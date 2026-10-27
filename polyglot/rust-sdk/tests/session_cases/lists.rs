use super::*;
use vizi_doorbell::declare::{CommandSpec, PanelSpec, PluginBuilder, Widget};

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn list_rows_preserve_every_operation_images_and_command_args() {
    let observed = Arc::new(Mutex::new(Vec::new()));
    let seen = observed.clone();
    let mut b = PluginBuilder::new("test.lists", "Lists", "1");
    let command = b.command(CommandSpec::new("set", "Set"), move |_, c| {
        seen.lock().unwrap().push(c.args.clone());
        Ok(done())
    });
    b.panel(PanelSpec::new(
        "panel",
        "Panel",
        Widget::List {
            moves_view: false,
            id: "rows".into(),
            max_rows: 2,
            visible_rows: 1,
            selectable: true,
            command: Some(command.clone()),
            row: vec![
                Widget::label("name", "Default"),
                Widget::progress("progress", 0.0, false),
                Widget::image("image"),
                Widget::Dropdown {
                    moves_view: false,
                    id: "choice".into(),
                    command,
                    options: vec!["old".into()],
                    value: None,
                },
            ],
        },
    ));
    let plugin = b.build().unwrap();
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from list fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    let panel = s.ctx().panel("panel");
    // Even multi-byte Unicode keys use characters, not encoded byte length.
    let key = "🦀".repeat(128);
    assert!(panel.row("").is_err());
    assert!(panel.row("x".repeat(129)).is_err());
    for keys in [
        vec!["".to_owned()],
        vec!["a".into(), "a".into()],
        vec!["a".into(), "b".into(), "c".into()],
        vec!["🦀".repeat(129)],
    ] {
        assert!(panel.set_rows("rows", keys).is_err());
    }
    assert!(panel.set_rows("name", ["a"]).is_err());
    assert!(panel.row("a").unwrap().set_rows("rows", ["a"]).is_err());
    s.barrier().await; // No partially accepted/truncated set_rows was sent.
    panel.set_rows("rows", [&key, "b"]).unwrap();
    let a = panel.row(&key).unwrap();
    let b = panel.row("b").unwrap();
    a.set_value("name", "First").unwrap();
    b.set_value("name", "Second").unwrap();
    a.set_enabled("choice", false).unwrap();
    a.set_visible("name", true).unwrap();
    a.set_progress("progress", 0.75, true).unwrap();
    a.set_options("choice", ["new", "other"]).unwrap();
    a.set_value("choice", "new").unwrap();
    let expected = [
        (
            "rows",
            "",
            pb::patch_op::Op::SetRows(pb::RowKeys {
                keys: vec![key.clone(), "b".into()],
            }),
        ),
        (
            "name",
            key.as_str(),
            pb::patch_op::Op::SetValue("First".into()),
        ),
        ("name", "b", pb::patch_op::Op::SetValue("Second".into())),
        ("choice", key.as_str(), pb::patch_op::Op::SetEnabled(false)),
        ("name", key.as_str(), pb::patch_op::Op::SetVisible(true)),
        (
            "progress",
            key.as_str(),
            pb::patch_op::Op::SetProgress(pb::Progress {
                value: 0.75,
                indeterminate: true,
            }),
        ),
        (
            "choice",
            key.as_str(),
            pb::patch_op::Op::SetOptions(pb::Options {
                options: vec!["new".into(), "other".into()],
            }),
        ),
        (
            "choice",
            key.as_str(),
            pb::patch_op::Op::SetValue("new".into()),
        ),
    ];
    for (widget, row, op) in expected {
        let Some(Plugin::PanelPatch(patch)) = s.recv().await.msg else {
            panic!("expected patch")
        };
        assert_eq!(patch.panel_id, "panel");
        assert_eq!(
            patch.ops,
            vec![pb::PatchOp {
                widget_id: widget.into(),
                row_key: row.into(),
                op: Some(op)
            }]
        );
    }
    // Removing and re-adding a key must remain ordered, not coalesce away its reset.
    panel.set_rows("rows", Vec::<String>::new()).unwrap();
    panel.set_rows("rows", ["b", &key]).unwrap();
    for keys in [vec![], vec!["b".to_owned(), key.clone()]] {
        let Some(Plugin::PanelPatch(patch)) = s.recv().await.msg else {
            panic!("expected rows")
        };
        assert_eq!(
            patch.ops[0].op,
            Some(pb::patch_op::Op::SetRows(pb::RowKeys { keys }))
        );
    }
    a.push_image("image", b"png").unwrap(); // Held until the panel grant completes.
    let file = grant(&s, 72, 512);
    let mut i = install(72);
    i.purpose = "P".into();
    i.subject = "panel".into();
    i.regions[0].producer = 2;
    s.send("", Host::InstallSegment(i)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::GrantAck(_))));
    let Some(Plugin::PanelPatch(held)) = s.recv().await.msg else {
        panic!("held image")
    };
    let Some(pb::patch_op::Op::SetImage(held)) = held.ops[0].op else {
        panic!("image ref")
    };
    let mut bytes = vec![0; held.len as usize];
    file.read_exact_at(&mut bytes, held.offset).unwrap();
    assert_eq!(bytes, b"png");
    s.send(
        "",
        Host::ReleaseLoan(pb::ReleaseLoan {
            segment_id: held.segment_id,
            region: 0,
            offset: held.offset,
            len: held.len,
        }),
    )
    .await;
    s.barrier().await;
    a.push_image("image", b"first png").unwrap();
    b.push_image("image", b"second png").unwrap();
    let mut loans = Vec::new();
    for (row, bytes) in [
        (&key, b"first png".as_slice()),
        (&"b".to_owned(), b"second png".as_slice()),
    ] {
        let Some(Plugin::PanelPatch(patch)) = s.recv().await.msg else {
            panic!("expected image")
        };
        assert_eq!(&patch.ops[0].row_key, row);
        let Some(pb::patch_op::Op::SetImage(image)) = &patch.ops[0].op else {
            panic!("expected image ref")
        };
        let mut actual = vec![0; bytes.len()];
        file.read_exact_at(&mut actual, image.offset).unwrap();
        assert_eq!(actual, bytes);
        loans.push(*image);
    }
    assert_ne!(loans[0].offset, loans[1].offset);
    for loan in loans {
        s.send(
            "",
            Host::ReleaseLoan(pb::ReleaseLoan {
                segment_id: 72,
                region: 0,
                offset: loan.offset,
                len: loan.len,
            }),
        )
        .await;
    }
    s.barrier().await;
    // Full arena is reusable only after both images' loans have been returned.
    let loan = s.ctx().memory().reserve(72, &[0; 256]).unwrap();
    s.ctx().memory().cancel_unsent(loan).unwrap();
    for (id, args) in [
        (
            "cell",
            [
                ("row".into(), key.clone().into()),
                ("value".into(), "new".into()),
            ]
            .into_iter()
            .collect(),
        ),
        (
            "selection",
            [("row".into(), "b".into())].into_iter().collect(),
        ),
    ] {
        s.send(
            id,
            Host::InvokeCommand(pb::InvokeCommand {
                cause: 0,
                command_id: "set".into(),
                args,
            }),
        )
        .await;
        let reply = s.recv().await;
        assert_eq!(reply.id, id);
        assert!(matches!(reply.msg, Some(Plugin::CommandDone(_))));
    }
    let seen = observed.lock().unwrap().clone();
    assert_eq!(seen.len(), 2);
    assert_eq!(seen[0]["row"], key.into());
    assert_eq!(seen[0]["value"], "new".into());
    assert_eq!(seen[1].len(), 1);
    assert_eq!(seen[1]["row"], "b".into());
    s.stop().await;
}
