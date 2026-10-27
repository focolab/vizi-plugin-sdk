use super::*;
use vizi_doorbell::control::{row_keys, Appearance, Cell, TableRow, WidgetSize};
use vizi_doorbell::declare::{
    CommandSpec, PanelSpec, PluginBuilder, TableColumn, ViewTarget, Widget,
};

fn geometry(panel: &str, widget: &str, width_px: u32, height_px: u32, dpr: f64) -> Host {
    Host::WidgetGeometry(pb::WidgetGeometry {
        panel_id: panel.into(),
        widget_id: widget.into(),
        width_px,
        height_px,
        dpr,
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn widget_geometry_reaches_the_panels_handler_newest_per_widget() {
    let (tx, rx) = std::sync::mpsc::channel::<WidgetSize>();
    let tx = Mutex::new(tx);
    let s = Session::new(Registry::new().on_widget_geometry("panel", move |_, size| {
        tx.lock().unwrap().send(size).unwrap();
        Ok(())
    }))
    .await;
    for width in 1..=50 {
        s.send("", geometry("panel", "plot", width, 100, 2.0)).await;
    }
    s.send("", geometry("panel", "other", 3, 5, 0.0)).await;
    s.send("", geometry("elsewhere", "plot", 7, 7, 1.0)).await;
    let mut newest = std::collections::BTreeMap::new();
    let mut widths = Vec::new();
    while newest.len() < 2 || newest["plot"] != (50, 100) {
        let size = rx.recv_timeout(Duration::from_secs(3)).expect("geometry");
        assert_eq!(
            size.panel_id, "panel",
            "only the registered panel's handler runs"
        );
        if size.widget_id == "plot" {
            assert_eq!(size.dpr, 2.0);
            assert_eq!(size.pixels(), (size.width_px * 2, 200));
            widths.push(size.width_px);
        } else {
            // A host that sends no ratio is read as 1.
            assert_eq!(size.dpr, 1.0);
            assert_eq!(size.pixels(), (3, 5));
        }
        newest.insert(size.widget_id, (size.width_px, size.height_px));
    }
    // Never an older size after a newer one.
    assert!(widths.windows(2).all(|w| w[0] < w[1]), "{widths:?}");
    assert!(rx.recv_timeout(Duration::from_millis(200)).is_err());
    s.stop().await;
}

fn appearance(theme: &str, accent: u32) -> Host {
    Host::Appearance(pb::HostAppearance {
        theme: theme.into(),
        window_bg: 0x202124ff,
        panel_bg: 0x2b2c2fff,
        text: 0xe8eaedff,
        text_disabled: 0x80868bff,
        accent,
        border: 0x5f6368ff,
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn appearance_is_cached_before_a_panel_opens_and_its_handler_gets_the_newest() {
    let (opened_tx, opened_rx) = std::sync::mpsc::channel::<Option<Appearance>>();
    let (tx, rx) = std::sync::mpsc::channel::<Appearance>();
    let (opened_tx, tx) = (Mutex::new(opened_tx), Mutex::new(tx));
    let mut s = Session::new(
        Registry::new()
            .on_panel_opened(move |ctx, _| {
                opened_tx.lock().unwrap().send(ctx.appearance()).unwrap();
                Ok(())
            })
            .on_appearance(move |_, appearance| {
                tx.lock().unwrap().send(appearance).unwrap();
                Ok(())
            }),
    )
    .await;
    s.recv().await;
    let opened = || {
        Host::PanelOpened(pb::PanelOpened {
            panel_id: "panel".into(),
        })
    };
    let wait = Duration::from_secs(3);
    // None before the first push.
    assert_eq!(s.ctx().appearance(), None);
    s.send("", opened()).await;
    assert_eq!(opened_rx.recv_timeout(wait).unwrap(), None);
    // Sent after Ready and before the panel opens: the panel's handler reads
    // it without waiting for the appearance handler.
    s.send("", appearance("dark", 0x3d7ae5ff)).await;
    s.send("", opened()).await;
    let seen = opened_rx.recv_timeout(wait).unwrap().expect("cached");
    assert!(seen.dark());
    assert_eq!(seen.accent, (0x3d, 0x7a, 0xe5, 0xff));
    assert_eq!(seen.hex("text").as_deref(), Some("#e8eaed"));
    assert_eq!(rx.recv_timeout(wait).unwrap(), seen);
    // A run of switches: the handler never sees an older one after a newer,
    // and the last it sees is the newest.
    for accent in 1..=50 {
        s.send(
            "",
            appearance(["light", "dark"][accent as usize % 2], accent),
        )
        .await;
    }
    let mut accents = Vec::new();
    while accents.last() != Some(&50) {
        let a = rx.recv_timeout(wait).expect("appearance");
        accents.push(u32::from_be_bytes([
            a.accent.0, a.accent.1, a.accent.2, a.accent.3,
        ]));
    }
    assert!(accents.windows(2).all(|w| w[0] < w[1]), "{accents:?}");
    assert!(rx.recv_timeout(Duration::from_millis(200)).is_err());
    let newest = s.ctx().appearance().unwrap();
    assert_eq!(
        (newest.theme.as_str(), newest.accent),
        ("light", (0, 0, 0, 50))
    );
    s.stop().await;
}

#[test]
fn pixels_round_half_to_even_like_the_reference() {
    let size = |width_px, dpr| WidgetSize {
        panel_id: String::new(),
        widget_id: String::new(),
        width_px,
        height_px: 1,
        dpr,
    };
    assert_eq!(size(5, 1.5).pixels(), (8, 2)); // 7.5 -> 8, 1.5 -> 2
    assert_eq!(size(3, 1.5).pixels(), (4, 2)); // 4.5 -> 4
    assert_eq!(size(101, 1.25).pixels(), (126, 1)); // 126.25, 1.25
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_declared_table_carries_rows_selection_and_string_lists() {
    let picked = Arc::new(Mutex::new(Vec::new()));
    let seen = picked.clone();
    let mut b = PluginBuilder::new("test.tables", "Tables", "1");
    let command = b.command(CommandSpec::new("pick", "Pick"), move |ctx, c| {
        let rows = row_keys(&c.args["rows"]).expect("rows").to_vec();
        seen.lock().unwrap().push(rows.clone());
        ctx.panel("panel").set_value("stats", rows)?;
        Ok(done())
    });
    let mut panel = PanelSpec::new(
        "panel",
        "Panel",
        Widget::column(
            "col",
            [
                Widget::Table {
                    id: "stats".into(),
                    moves_view: true,
                    columns: vec![
                        TableColumn::string("name", "Region"),
                        TableColumn::number("area", "Area").precision(2).unit("µm²"),
                        TableColumn::bool("kept", "Kept"),
                    ],
                    visible_rows: 12,
                    sortable: true,
                    selectable: true,
                    multiple: true,
                    command: Some(command),
                },
                Widget::table("plain", [TableColumn::string("x", "X")]),
            ],
        ),
    );
    panel.view_targets = vec![ViewTarget::SelectRegion];
    b.panel(panel);
    let plugin = b.build().unwrap();
    let root = &plugin.manifest().value()["contributions"]["panels"][0]["root"]["children"];
    assert_eq!(
        root[0],
        serde_json::json!({"kind":"table","id":"stats","moves_view":true,"visible_rows":12,
            "sortable":true,"selectable":true,"multiple":true,"command":"pick",
            "columns":[{"id":"name","header":"Region","type":"string"},
                {"id":"area","header":"Area","type":"number","precision":2,"unit":"µm²"},
                {"id":"kept","header":"Kept","type":"bool"}]})
    );
    assert_eq!(root[1]["selectable"], false);
    assert!(root[1].get("command").is_none());
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from table fixture",
    )
    .await
    .unwrap();
    s.recv().await;
    let ctx = s.ctx();
    let p = ctx.panel("panel");
    p.set_table(
        "stats",
        [
            TableRow::by_column("r1", [("area", Cell::from(2.5)), ("name", "one".into())]),
            TableRow::new("r2", [Cell::from("two"), 3.into(), true.into()]),
        ],
    )
    .unwrap();
    p.patch_table(
        "stats",
        [TableRow::new("r3", [Cell::from("three")])],
        ["r1"],
    )
    .unwrap();
    use pb::{patch_op::Op, value::Kind};
    // One message when the patch merged into the queued statement, two when
    // the writer had already taken the statement.
    let mut ops = Vec::new();
    while !ops.iter().any(|op| match op {
        Op::SetTable(t) => t.rows.iter().any(|r| r.key == "r3"),
        Op::PatchTable(_) => true,
        _ => false,
    }) {
        let Some(Plugin::PanelPatch(patch)) = s.recv().await.msg else {
            panic!()
        };
        ops.extend(patch.ops.into_iter().map(|op| op.op.unwrap()));
    }
    let Op::SetTable(whole) = &ops[0] else {
        panic!("{ops:?}")
    };
    // Merged into the pending statement, or sent behind it: the same rows.
    let rows = match &ops[1..] {
        [] => whole.rows.clone(),
        [Op::PatchTable(delta)] => {
            assert_eq!(delta.delete, ["r1"]);
            let mut rows: Vec<_> = whole
                .rows
                .iter()
                .filter(|r| r.key != "r1")
                .cloned()
                .collect();
            rows.extend(delta.upsert.iter().cloned());
            rows
        }
        other => panic!("{other:?}"),
    };
    let keys: Vec<_> = rows.iter().map(|r| r.key.as_str()).collect();
    assert_eq!(keys, ["r2", "r3"]);
    assert_eq!(
        rows[0]
            .cells
            .iter()
            .map(|c| c.kind.clone())
            .collect::<Vec<_>>(),
        [
            Some(Kind::S("two".into())),
            Some(Kind::F(3.0)),
            Some(Kind::B(true))
        ]
    );
    s.send(
        "pick",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "pick".into(),
            cause: 1,
            args: [("rows".into(), vec!["r2", "r3"].into())].into(),
        }),
    )
    .await;
    let mut patched = false;
    let mut finished = false;
    while !patched || !finished {
        match s.recv().await.msg {
            Some(Plugin::PanelPatch(patch)) => {
                assert_eq!(
                    patch.ops[0].op,
                    Some(Op::SetValue(pb::Value {
                        kind: Some(Kind::Strings(pb::StringList {
                            values: vec!["r2".into(), "r3".into()]
                        }))
                    }))
                );
                patched = true;
            }
            Some(Plugin::CommandDone(_)) => finished = true,
            other => panic!("{other:?}"),
        }
    }
    // A cleared selection is {"rows": []}: Value.strings with no keys, on the
    // wire field 6 with an empty body. It is an empty key list, not a refusal.
    use prost::Message;
    let cleared = pb::Value::decode(&[0x32, 0x00][..]).unwrap();
    assert_eq!(row_keys(&cleared), Some(&[][..]));
    s.send(
        "clear",
        Host::InvokeCommand(pb::InvokeCommand {
            command_id: "pick".into(),
            cause: 1,
            args: [("rows".into(), cleared)].into(),
        }),
    )
    .await;
    let (mut patched, mut finished) = (false, false);
    while !patched || !finished {
        match s.recv().await.msg {
            Some(Plugin::PanelPatch(patch)) => {
                assert_eq!(
                    patch.ops[0].op,
                    Some(Op::SetValue(pb::Value {
                        kind: Some(Kind::Strings(pb::StringList { values: vec![] }))
                    }))
                );
                patched = true;
            }
            Some(Plugin::CommandDone(c)) => {
                assert!(c.ok, "{c:?}");
                finished = true
            }
            other => panic!("{other:?}"),
        }
    }
    assert_eq!(
        *picked.lock().unwrap(),
        [vec!["r2".to_owned(), "r3".into()], vec![]]
    );
    // Row keys are Value.strings only: an int list, empty or not, is not one.
    assert!(row_keys(&pb::Value::from(vec![1_i64])).is_none());
    let ints = pb::Value::decode(&[0x2a, 0x00][..]).unwrap();
    assert!(matches!(ints.kind, Some(Kind::Ints(_))));
    assert!(row_keys(&ints).is_none());
    // A list set on a declared table always goes out as Value.strings, an
    // empty one included: an int list's numbers become their decimal keys.
    for (list, keys) in [(vec![], vec![]), (vec![7_i64, -2], vec!["7", "-2"])] {
        p.set_value("stats", list).unwrap();
        let Some(Plugin::PanelPatch(patch)) = s.recv().await.msg else {
            panic!()
        };
        assert_eq!(
            patch.ops[0].op,
            Some(Op::SetValue(pb::Value {
                kind: Some(Kind::Strings(pb::StringList {
                    values: keys.into_iter().map(Into::into).collect()
                }))
            }))
        );
    }
    s.stop().await;
}
