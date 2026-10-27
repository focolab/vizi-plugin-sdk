use super::*;

/// A runtime whose writer never runs, so every push stays pending.
fn runtime() -> (Runtime, std::os::fd::OwnedFd) {
    let manifest = crate::manifest::Manifest::parse(r#"{"manifest_version":1,"id":"org.example.test","name":"Test","version":"1","engines":{"vizi":"1.0"},"entry":{"command":["test"]},"contributions":{"panels":[{"id":"p","title":"Panel","root":{"kind":"column","id":"c","children":[
        {"kind":"table","id":"t","columns":[{"id":"name"},{"id":"area","type":"number"},{"id":"kept","type":"bool"}]},
        {"kind":"table","id":"u","columns":[{"id":"x"}]},
        {"kind":"label","id":"l"}]}}]}}"#).unwrap();
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

fn pending(ctx: &Context) -> Vec<pb::PanelPatch> {
    ctx.core
        .queues
        .lock()
        .unwrap()
        .pushes
        .iter()
        .map(|m| match &m.msg {
            Some(Plugin::PanelPatch(p)) => p.clone(),
            other => panic!("{other:?}"),
        })
        .collect()
}

/// Every pending op, in order, across the messages that carry them; each
/// message under §9's 1 MiB for a `PanelPatch` carrying a table op.
fn ops(ctx: &Context) -> Vec<pb::PatchOp> {
    let patches = pending(ctx);
    assert!(patches
        .iter()
        .all(|p| p.encoded_len() <= crate::manifest::TABLE_MAX_PATCH_BYTES));
    patches.into_iter().flat_map(|p| p.ops).collect()
}

fn keys(op: &pb::PatchOp) -> (&'static str, Vec<&str>, Vec<&str>) {
    match op.op.as_ref().unwrap() {
        pb::patch_op::Op::SetTable(t) => (
            "set",
            t.rows.iter().map(|r| r.key.as_str()).collect(),
            vec![],
        ),
        pb::patch_op::Op::PatchTable(p) => (
            "patch",
            p.upsert.iter().map(|r| r.key.as_str()).collect(),
            p.delete.iter().map(String::as_str).collect(),
        ),
        other => panic!("{other:?}"),
    }
}

fn row(key: &str) -> TableRow {
    TableRow::new(key, [Cell::from(key), 1.5.into(), true.into()])
}

#[test]
fn cells_go_positionally_or_by_column_and_are_checked_before_queueing() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel
        .set_table(
            "t",
            [
                TableRow::new("a", [Cell::from("A"), 2_i32.into(), Cell::Blank]),
                TableRow::by_column("b", [("kept", Cell::from(false)), ("name", "B".into())]),
                TableRow::new("c", [Cell::from(None::<f64>)]),
            ],
        )
        .unwrap();
    let patches = pending(&ctx);
    assert_eq!(patches.len(), 1);
    let Some(pb::patch_op::Op::SetTable(t)) = &patches[0].ops[0].op else {
        panic!()
    };
    use pb::value::Kind;
    let kinds: Vec<Vec<Option<Kind>>> = t
        .rows
        .iter()
        .map(|r| r.cells.iter().map(|c| c.kind.clone()).collect())
        .collect();
    assert_eq!(
        kinds,
        [
            vec![Some(Kind::S("A".into())), Some(Kind::F(2.0)), None],
            vec![Some(Kind::S("B".into())), None, Some(Kind::B(false))],
            vec![None],
        ]
    );
    for (rows, text) in [
        (vec![row("a"), row("a")], "distinct"),
        (vec![row("")], "non-empty"),
        (vec![row(&"🦀".repeat(129))], "at most 128 characters"),
        (
            vec![TableRow::new(
                "a",
                [Cell::Blank, Cell::Blank, Cell::Blank, Cell::Blank],
            )],
            "at most 3 cells",
        ),
        (
            vec![TableRow::by_column("a", [("nope", Cell::Blank)])],
            "has no column \"nope\"",
        ),
        (
            vec![TableRow::new(
                "a",
                [Cell::from("x".repeat(TABLE_PATCH_BUDGET))],
            )],
            "past one message's",
        ),
    ] {
        let e = panel.set_table("t", rows).unwrap_err();
        assert!(e.message.contains(text), "{e}");
    }
    assert!(panel
        .set_table("l", [row("a")])
        .unwrap_err()
        .message
        .contains("no table 'l'"));
    assert!(panel.row("a").unwrap().set_table("t", [row("a")]).is_err());
    let too_many = (0..=crate::manifest::TABLE_MAX_ROWS).map(|i| row(&i.to_string()));
    assert!(panel
        .set_table("t", too_many)
        .unwrap_err()
        .message
        .contains("100000"));
    assert_eq!(pending(&ctx).len(), 1, "nothing refused was queued");
}

#[test]
fn a_large_table_is_a_whole_statement_then_appends_each_under_one_mebibyte() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let rows: Vec<_> = (0..100_000)
        .map(|i| {
            TableRow::new(
                format!("row-{i:06}"),
                [Cell::from("a fairly long name, padded"), (i as f64).into()],
            )
        })
        .collect();
    ctx.panel("p").set_table("t", rows).unwrap();
    let patches = pending(&ctx);
    assert!(patches.len() > 2, "{}", patches.len());
    let mut all = Vec::new();
    for (i, patch) in patches.iter().enumerate() {
        assert!(patch.encoded_len() <= crate::manifest::TABLE_MAX_PATCH_BYTES);
        let (kind, upsert, delete) = keys(&patch.ops[0]);
        assert_eq!(kind, if i == 0 { "set" } else { "patch" });
        assert!(delete.is_empty());
        all.extend(upsert.into_iter().map(str::to_owned));
    }
    let expected: Vec<_> = (0..100_000).map(|i| format!("row-{i:06}")).collect();
    assert_eq!(all, expected);
}

#[test]
fn deletes_go_first_and_the_last_run_shares_a_message_with_the_upserts() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    // Something else for the widget is pending, so nothing below merges.
    panel.set_visible("t", true).unwrap();
    let deletes: Vec<String> = (0..70_000).map(|i| format!("{i:07}")).collect();
    let upserts: Vec<String> = (0..30_000).map(|i| format!("n{i}")).collect();
    panel
        .patch_table(
            "t",
            upserts.iter().map(|k| TableRow::new(k.as_str(), [])),
            deletes.clone(),
        )
        .unwrap();
    let ops = ops(&ctx);
    let shape: Vec<_> = ops[1..]
        .iter()
        .map(|op| {
            let (_, upsert, delete) = keys(op);
            (upsert.len(), delete.len())
        })
        .collect();
    // Upserts and deletes are each counted on their own against 32,768: the
    // shared message carries 34,464 entries in all, neither field past the cap.
    assert_eq!(shape, [(0, 32_768), (0, 32_768), (30_000, 4464)]);
    let (mut all_deletes, mut all_upserts) = (Vec::new(), Vec::new());
    for op in &ops[1..] {
        let (_, upsert, delete) = keys(op);
        all_upserts.extend(upsert.into_iter().map(str::to_owned));
        all_deletes.extend(delete.into_iter().map(str::to_owned));
    }
    assert_eq!(all_deletes, deletes);
    assert_eq!(all_upserts, upserts);
}

#[test]
fn forty_thousand_deletes_split_by_the_row_cap_on_their_own() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    // The second op cannot merge into the first (65,536 deletes > 32,768),
    // so it goes behind it as an op of its own.
    let deletes: Vec<String> = (0..40_000).map(|i| format!("k{i}")).collect();
    panel
        .patch_table("t", [row("a"), row("b"), row("c")], deletes.clone())
        .unwrap();
    let ops = ops(&ctx);
    let shape: Vec<_> = ops
        .iter()
        .map(|op| {
            let (kind, upsert, delete) = keys(op);
            (kind, upsert, delete.len())
        })
        .collect();
    assert_eq!(
        shape,
        [
            ("patch", vec![], 32_768),
            ("patch", vec!["a", "b", "c"], 7232)
        ]
    );
    let all: Vec<_> = ops
        .iter()
        .flat_map(|op| {
            keys(op)
                .2
                .into_iter()
                .map(str::to_owned)
                .collect::<Vec<_>>()
        })
        .collect();
    assert_eq!(all, deletes);
}

#[test]
fn a_merge_bounds_the_larger_field_not_the_sum() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    let deletes = |prefix: &str, n: usize| -> Vec<String> {
        (0..n).map(|i| format!("{prefix}{i}")).collect()
    };
    let rows = |n: usize| (0..n).map(|i| TableRow::new(format!("r{i}"), []));
    // A statement makes the table's rows known; the set_visible after it keeps
    // the patches below from merging into it.
    panel.set_table("t", rows(0)).unwrap();
    panel.set_visible("t", true).unwrap();
    panel
        .patch_table("t", rows(20_000), deletes("x", 0))
        .unwrap();
    // 20,000 upserts and 20,000 deletes: 40,000 in all, each field under the cap.
    panel
        .patch_table("t", rows(0), deletes("y", 20_000))
        .unwrap();
    assert_eq!(ops(&ctx).len(), 3, "merged");
    // Another 20,000 deletes would take the delete field to 40,000: queued behind.
    panel
        .patch_table("t", rows(0), deletes("z", 20_000))
        .unwrap();
    let ops = ops(&ctx);
    assert_eq!(ops.len(), 4);
    let (_, upsert, delete) = keys(&ops[2]);
    assert_eq!((upsert.len(), delete.len()), (20_000, 20_000));
}

#[test]
fn a_hundred_thousand_short_key_rows_split_by_count_as_well_as_bytes() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    // Key-only rows of short keys: 100,000 of them fit in one mebibyte, so
    // only the row cap splits them.
    let rows = (0..100_000).map(|i| TableRow::new(format!("{i:x}"), []));
    ctx.panel("p").set_table("t", rows).unwrap();
    let ops = ops(&ctx);
    let counts: Vec<_> = ops.iter().map(|op| keys(op).1.len()).collect();
    assert_eq!(counts, [32_768, 32_768, 32_768, 1696]);
    assert!(counts
        .iter()
        .all(|&n| n <= crate::manifest::TABLE_MAX_ROWS_PER_MESSAGE));
    assert_eq!(keys(&ops[0]).0, "set");
}

#[test]
fn a_patch_merges_into_the_pending_table_op_until_the_bound_and_never_past_another_op() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    // Each table stated once, so the rows the statements below replace are known.
    panel.set_table("t", [] as [TableRow; 0]).unwrap();
    panel.set_table("u", [] as [TableRow; 0]).unwrap();
    panel.set_table("t", [row("a"), row("b")]).unwrap();
    panel
        .set_table("u", [TableRow::new("x", [Cell::from("x")])])
        .unwrap();
    // Each widget's ops share the message pending for it, in order. Into the
    // pending set_table, past u's message: applied.
    panel.patch_table("t", [row("c"), row("a")], ["b"]).unwrap();
    let patches = pending(&ctx);
    assert_eq!(patches.len(), 2);
    assert_eq!(keys(&patches[0].ops[1]), ("set", vec!["a", "c"], vec![]));
    let bytes = ctx.core.queues.lock().unwrap().bytes;
    assert_eq!(
        bytes,
        patches
            .iter()
            .map(|p| pb::PluginMsg {
                id: String::new(),
                msg: Some(Plugin::PanelPatch(p.clone()))
            }
            .encoded_len())
            .sum::<usize>()
    );
    // A set_value for the table is pending after it: the patch goes behind.
    panel.set_value("t", vec!["a"]).unwrap();
    panel.patch_table("t", [row("d")], [] as [&str; 0]).unwrap();
    // ... and the next patch folds into that one.
    panel.patch_table("t", [row("e")], ["d"]).unwrap();
    assert_eq!(
        shapes(&ctx)[0],
        [
            ("set", vec![]),
            ("set", strings(&["a", "c"])),
            ("value", strings(&["a"])),
            ("patch", strings(&["-d", "e"])),
        ]
    );
    // A patch that would take the pending message past 1 MiB starts one of
    // its own: the full message goes as it is, and nothing is refused.
    let big = |prefix: &str| -> Vec<TableRow> {
        (0..20_000)
            .map(|i| TableRow::new(format!("{prefix}{i:05}"), [Cell::from("y".repeat(20))]))
            .collect()
    };
    panel.patch_table("u", big("p"), [] as [&str; 0]).unwrap();
    assert_eq!(pending(&ctx).len(), 2, "merged into u's set_table");
    assert_eq!(keys(&pending(&ctx)[1].ops[1]).1.len(), 20_001);
    panel.patch_table("u", big("q"), [] as [&str; 0]).unwrap();
    let patches = pending(&ctx);
    assert_eq!(patches.len(), 3);
    assert!(patches
        .iter()
        .all(|p| p.encoded_len() <= crate::manifest::TABLE_MAX_PATCH_BYTES));
    assert_eq!(keys(&patches[2].ops[0]).1.len(), 20_000);
}

#[test]
fn raw_table_patches_are_bounded() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let rows = (0..=crate::manifest::TABLE_MAX_ROWS)
        .map(|i| pb::TableRow {
            key: i.to_string(),
            cells: vec![],
        })
        .collect();
    let op = |op| pb::PatchOp {
        widget_id: "t".into(),
        row_key: String::new(),
        op: Some(op),
    };
    let e = ctx
        .patch(
            "p",
            vec![op(pb::patch_op::Op::SetTable(pb::TableRows { rows }))],
        )
        .unwrap_err();
    assert!(e.message.contains("at most 100000 rows"), "{e}");
    let e = ctx
        .patch(
            "p",
            vec![op(pb::patch_op::Op::PatchTable(pb::TablePatch {
                upsert: vec![
                    pb::TableRow {
                        key: "a".into(),
                        cells: vec![]
                    };
                    20_000
                ],
                delete: vec!["b".into(); 32_769],
            }))],
        )
        .unwrap_err();
    assert!(e.message.contains("at most 32768 rows"), "{e}");
    let e = ctx
        .patch(
            "p",
            vec![op(pb::patch_op::Op::PatchTable(pb::TablePatch {
                upsert: vec![pb::TableRow {
                    key: "a".into(),
                    cells: vec!["z".repeat(1 << 20).into()],
                }],
                delete: vec![],
            }))],
        )
        .unwrap_err();
    assert!(e.message.contains("table op is at most"), "{e}");
    assert!(pending(&ctx).is_empty());
}

/// Each pending message's ops, as (kind, keys) pairs; a patch's deletes are
/// written `-key`, ahead of its upserts.
fn shapes(ctx: &Context) -> Vec<Vec<(&'static str, Vec<String>)>> {
    use pb::patch_op::Op;
    pending(ctx)
        .iter()
        .map(|p| {
            p.ops
                .iter()
                .map(|op| match op.op.as_ref().unwrap() {
                    Op::SetTable(t) => ("set", t.rows.iter().map(|r| r.key.clone()).collect()),
                    Op::PatchTable(p) => (
                        "patch",
                        p.delete
                            .iter()
                            .map(|k| format!("-{k}"))
                            .chain(p.upsert.iter().map(|r| r.key.clone()))
                            .collect(),
                    ),
                    Op::SetValue(v) => ("value", row_keys(v).unwrap().to_vec()),
                    Op::SetVisible(_) => ("visible", vec![]),
                    other => panic!("{other:?}"),
                })
                .collect()
        })
        .collect()
}

/// [`shapes`], whatever messages carry the ops.
fn flat(ctx: &Context) -> Vec<(&'static str, Vec<String>)> {
    shapes(ctx).into_iter().flatten().collect()
}

fn strings(keys: &[&str]) -> Vec<String> {
    keys.iter().map(|k| (*k).to_owned()).collect()
}

fn raw_op(widget: &str, op: pb::patch_op::Op) -> pb::PatchOp {
    pb::PatchOp {
        widget_id: widget.into(),
        row_key: String::new(),
        op: Some(op),
    }
}

fn raw_row(key: &str, cells: usize) -> pb::TableRow {
    pb::TableRow {
        key: key.into(),
        cells: vec![pb::Value::default(); cells],
    }
}

#[test]
fn a_set_table_never_overtakes_a_pending_set_value() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [] as [TableRow; 0]).unwrap();
    panel.set_table("t", [row("j")]).unwrap();
    panel.patch_table("t", [row("k")], [] as [&str; 0]).unwrap();
    panel.set_value("t", vec!["k"]).unwrap();
    // A later statement goes behind the selection that names a row of the
    // earlier one; it neither supersedes nor jumps ahead of it.
    panel.set_table("t", [row("m")]).unwrap();
    assert_eq!(
        shapes(&ctx),
        [vec![
            ("set", vec![]),
            ("set", strings(&["j", "k"])),
            ("value", strings(&["k"])),
            ("set", strings(&["m"])),
        ]]
    );
}

#[test]
fn no_patch_merges_across_a_pending_set_value() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [row("j"), row("k")]).unwrap();
    panel.set_value("t", vec!["k"]).unwrap();
    // Merged into the statement, the delete would land before the selection
    // and the host would refuse the selection; applied in order, the
    // selection is dropped quietly. So the patch goes behind.
    panel.patch_table("t", [row("n")], ["k"]).unwrap();
    // The same for a set_value inside the table op's own message.
    use pb::patch_op::Op;
    ctx.patch(
        "p",
        vec![
            raw_op(
                "u",
                Op::SetTable(pb::TableRows {
                    rows: vec![raw_row("x", 0)],
                }),
            ),
            raw_op("u", Op::SetValue(vec!["x"].into())),
        ],
    )
    .unwrap();
    panel.patch_table("u", [] as [TableRow; 0], ["x"]).unwrap();
    assert_eq!(
        shapes(&ctx),
        [
            vec![
                ("set", strings(&["j", "k"])),
                ("value", strings(&["k"])),
                ("patch", strings(&["-k", "n"])),
            ],
            vec![
                ("set", strings(&["x"])),
                ("value", strings(&["x"])),
                ("patch", strings(&["-x"])),
            ],
        ]
    );
}

fn upsert(ctx: &Context, widget: &str, rows: Vec<pb::TableRow>) {
    ctx.patch(
        "p",
        vec![raw_op(
            widget,
            pb::patch_op::Op::PatchTable(pb::TablePatch {
                upsert: rows,
                delete: vec![],
            }),
        )],
    )
    .unwrap();
}

#[test]
fn no_patch_merges_before_the_first_set_table_and_patches_merge_after_it() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    // No statement this session: the host may hold rows this session never
    // stated, so the outbox cannot know a patch's outcome. They stay apart,
    // in order, in one message.
    panel.patch_table("t", [row("a")], [] as [&str; 0]).unwrap();
    panel.patch_table("t", [row("b")], [] as [&str; 0]).unwrap();
    assert_eq!(shapes(&ctx).len(), 1);
    assert_eq!(flat(&ctx).len(), 2);
    // After a statement (kept apart from them by a set_visible) they fold,
    // deletes and upserts landing on the simulated keys.
    panel.set_table("t", [row("c")]).unwrap();
    panel.set_visible("t", true).unwrap();
    panel.patch_table("t", [row("d")], [] as [&str; 0]).unwrap();
    panel.patch_table("t", [row("e")], ["c"]).unwrap();
    let shapes = flat(&ctx);
    assert_eq!(shapes.len(), 5, "{shapes:?}");
    assert_eq!(shapes[4], ("patch", strings(&["-c", "d", "e"])));
}

#[test]
fn a_bad_op_goes_alone_while_its_neighbours_still_merge() {
    for (bad, why) in [
        (raw_row("", 0), "an empty key"),
        (raw_row(&"k".repeat(129), 0), "a 129-character key"),
        (raw_row("b", 4), "more cells than the 3 declared"),
    ] {
        let (runtime, _peer) = runtime();
        let ctx = runtime.context();
        ctx.panel("p").set_table("t", [] as [TableRow; 0]).unwrap();
        ctx.panel("p").set_table("t", [row("a")]).unwrap();
        upsert(&ctx, "t", vec![raw_row("b", 3)]);
        upsert(&ctx, "t", vec![raw_row("c", 3), bad]);
        upsert(&ctx, "t", vec![raw_row("d", 3)]);
        upsert(&ctx, "t", vec![raw_row("e", 3)]);
        let shapes = flat(&ctx);
        // The valid patch merged into the statement; the bad one went as an
        // op of its own and nothing merged into it; the two after it merged
        // together.
        assert_eq!(shapes.len(), 4, "{why}: {shapes:?}");
        assert_eq!(shapes[1], ("set", strings(&["a", "b"])), "{why}");
        assert_eq!(shapes[3], ("patch", strings(&["d", "e"])), "{why}");
    }
    // A key repeated within one op is refused the same way.
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    ctx.panel("p").set_table("t", [row("a")]).unwrap();
    upsert(&ctx, "t", vec![raw_row("b", 0), raw_row("b", 0)]);
    upsert(&ctx, "t", vec![raw_row("c", 0)]);
    assert_eq!(flat(&ctx).len(), 3);
}

#[test]
fn the_patch_past_100000_rows_goes_alone_and_the_table_is_simulated_without_it() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    let rows = (0..99_999).map(|i| TableRow::new(format!("{i:x}"), []));
    panel.set_table("t", rows).unwrap();
    let statement = ops(&ctx).len();
    // The 100,000th row is valid and merges into the last chunk.
    panel.patch_table("t", [row("x")], [] as [&str; 0]).unwrap();
    assert_eq!(ops(&ctx).len(), statement);
    // Two more would make 100,002: the host refuses that patch whole, so it
    // goes alone and nothing merges into it.
    panel
        .patch_table("t", [row("y1"), row("y2")], [] as [&str; 0])
        .unwrap();
    // The table is still 100,000 rows: an update of a held key is valid,
    // goes behind the bad patch, and the next one merges into it.
    panel.patch_table("t", [row("x")], [] as [&str; 0]).unwrap();
    panel.patch_table("t", [row("x")], [] as [&str; 0]).unwrap();
    let shapes = flat(&ctx);
    assert_eq!(shapes.len(), statement + 2, "{:?}", &shapes[statement..]);
    assert_eq!(shapes[statement], ("patch", strings(&["y1", "y2"])));
    assert_eq!(shapes[statement + 1], ("patch", strings(&["x"])));
}

#[test]
fn no_patch_merges_into_a_table_the_manifest_does_not_declare() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    for widget in ["nope", "nope", "l", "l"] {
        upsert(&ctx, widget, vec![raw_row("a", 0)]);
    }
    assert_eq!(ops(&ctx).len(), 4);
}

/// A table as the host holds it: `control_plane_protocol.md` §8.4's rules
/// for `set_table`, `patch_table` and a table's `set_value`, as the
/// reference applies them, so the outbox's merges can be checked against the
/// ops applied one by one.
#[derive(Clone, Debug, Default, PartialEq)]
struct HostTable {
    keys: Vec<String>,
    rows: HashMap<String, pb::TableRow>,
    selection: Vec<String>,
}
impl HostTable {
    fn apply(&mut self, op: &pb::patch_op::Op, width: usize, multiple: bool) {
        use pb::patch_op::Op;
        let fits = |rows: &[pb::TableRow]| {
            rows.iter().all(|r| {
                !r.key.is_empty()
                    && r.key.chars().count() <= TABLE_MAX_KEY_CHARS
                    && r.cells.len() <= width
            })
        };
        match op {
            Op::SetTable(t) => {
                let keys: Vec<String> = t.rows.iter().map(|r| r.key.clone()).collect();
                let distinct = keys.iter().collect::<HashSet<_>>().len() == keys.len();
                if t.rows.len() > TABLE_MAX_ROWS || !fits(&t.rows) || !distinct {
                    return;
                }
                self.keys = keys;
                self.rows = t.rows.iter().map(|r| (r.key.clone(), r.clone())).collect();
            }
            Op::PatchTable(p) => {
                if !fits(&p.upsert) {
                    return;
                }
                let gone: HashSet<&String> = p.delete.iter().collect();
                let mut keys: Vec<String> = self
                    .keys
                    .iter()
                    .filter(|k| !gone.contains(k))
                    .cloned()
                    .collect();
                let mut rows: HashMap<String, pb::TableRow> = keys
                    .iter()
                    .map(|k| (k.clone(), self.rows[k].clone()))
                    .collect();
                for row in &p.upsert {
                    if !rows.contains_key(&row.key) {
                        keys.push(row.key.clone());
                    }
                    rows.insert(row.key.clone(), row.clone());
                }
                if keys.len() > TABLE_MAX_ROWS {
                    return;
                }
                self.keys = keys;
                self.rows = rows;
                // A key the patch deletes loses its selection, even when the
                // same patch upserts it again: that is a fresh row.
                self.selection.retain(|k| !gone.contains(k));
            }
            Op::SetValue(v) => {
                let mut keys: Vec<String> = Vec::new();
                match &v.kind {
                    Some(pb::value::Kind::S(s)) if s.is_empty() => {}
                    Some(pb::value::Kind::S(s)) => keys.push(s.clone()),
                    Some(pb::value::Kind::Strings(list)) => {
                        for k in &list.values {
                            if !keys.contains(k) {
                                keys.push(k.clone());
                            }
                        }
                    }
                    _ => return,
                }
                if keys.len() > 1 && !multiple || keys.iter().any(|k| !self.rows.contains_key(k)) {
                    return;
                }
                self.selection = keys;
                return;
            }
            other => panic!("{other:?}"),
        }
        let rows = &self.rows;
        self.selection.retain(|k| rows.contains_key(k));
    }
}

/// A small xorshift, so the replay below is reproducible from its seed.
struct Rng(u64);
impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }
    fn below(&mut self, n: usize) -> usize {
        (self.next() % n as u64) as usize
    }
    /// Distinct keys from a pool of six, in a random order.
    fn keys(&mut self, most: usize) -> Vec<String> {
        let mut pool: Vec<String> = "abcdef".chars().map(String::from).collect();
        let mut out = Vec::new();
        for _ in 0..self.below(most + 1) {
            out.push(pool.remove(self.below(pool.len())));
            if pool.is_empty() {
                break;
            }
        }
        out
    }
    fn rows(&mut self, most: usize) -> Vec<pb::TableRow> {
        self.keys(most)
            .into_iter()
            .map(|key| pb::TableRow {
                cells: vec![
                    Cell::from(key.as_str()).into_value(),
                    Cell::Number(self.below(100) as f64).into_value(),
                ],
                key,
            })
            .collect()
    }
}

#[test]
fn the_outbox_reaches_the_rows_and_selection_the_ops_in_order_do() {
    use pb::patch_op::Op;
    let mut merges = 0;
    for seed in 1..=400u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
        let (runtime, _peer) = runtime();
        let ctx = runtime.context();
        // What the panel shows before this session: a previous session's rows
        // and the person's selection, unknown to this one.
        let mut start = HostTable::default();
        start.apply(&Op::SetTable(pb::TableRows { rows: rng.rows(6) }), 3, true);
        let held = start.keys.clone();
        start.selection = held.into_iter().filter(|_| rng.below(2) == 0).collect();
        let mut in_order = start.clone();
        let mut through_outbox = start.clone();
        let drain = |host: &mut HostTable| {
            let mut q = ctx.core.queues.lock().unwrap();
            let drained: Vec<_> = q.pushes.drain(..).collect();
            q.bytes -= drained.iter().map(|m| m.encoded_len()).sum::<usize>();
            drop(q);
            for message in drained {
                let Some(Plugin::PanelPatch(patch)) = message.msg else {
                    panic!()
                };
                for op in patch.ops {
                    host.apply(op.op.as_ref().unwrap(), 3, false);
                }
            }
        };
        let mut queued = 0;
        for _ in 0..12 {
            let op = match rng.below(9) {
                0 | 1 => Op::SetTable(pb::TableRows { rows: rng.rows(5) }),
                2..=5 => Op::PatchTable(pb::TablePatch {
                    upsert: rng.rows(3),
                    delete: rng.keys(3),
                }),
                6 => Op::SetValue(rng.keys(1).pop().unwrap_or_default().as_str().into()),
                7 => Op::SetValue(rng.keys(2).into()),
                _ => {
                    drain(&mut through_outbox);
                    continue;
                }
            };
            in_order.apply(&op, 3, false);
            ctx.patch("p", vec![raw_op("t", op)]).unwrap();
            queued += 1;
        }
        merges += queued - pending(&ctx).len().min(queued);
        drain(&mut through_outbox);
        assert_eq!(through_outbox, in_order, "seed {seed}, from {start:?}");
    }
    // The replay is worth something only if it merged.
    assert!(merges > 100, "{merges}");
}

#[test]
fn a_key_dropped_and_brought_back_stays_unselected() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [row("a"), row("b")]).unwrap();
    panel.set_value("t", vec!["a"]).unwrap();
    // In order, the delete drops a's selection and the upsert brings back
    // its row unselected. As one patch, a would stay selected.
    panel.patch_table("t", [] as [TableRow; 0], ["a"]).unwrap();
    panel.patch_table("t", [row("a")], [] as [&str; 0]).unwrap();
    // In ONE op, a delete and upsert of the same key is a fresh row as well,
    // so that patch does not fold with the one before it either.
    panel.patch_table("t", [row("b")], ["b"]).unwrap();
    // The first statement of a session replaces rows it does not know, any of
    // which may be selected: a patch bringing back a key it left out does not
    // merge, and one changing a key it holds does.
    panel
        .set_table("u", [TableRow::new("x", [Cell::from("x")])])
        .unwrap();
    panel
        .patch_table(
            "u",
            [TableRow::new("y", [Cell::from("y")])],
            [] as [&str; 0],
        )
        .unwrap();
    panel
        .set_table("u", [TableRow::new("x", [Cell::from("x")])])
        .unwrap();
    panel
        .patch_table(
            "u",
            [TableRow::new("x", [Cell::from("z")])],
            [] as [&str; 0],
        )
        .unwrap();
    assert_eq!(
        flat(&ctx),
        [
            ("set", strings(&["a", "b"])),
            ("value", strings(&["a"])),
            ("patch", strings(&["-a"])),
            ("patch", strings(&["a"])),
            ("patch", strings(&["-b", "b"])),
            ("set", strings(&["x"])),
            ("patch", strings(&["y"])),
            ("set", strings(&["x"])),
        ]
    );
}

#[test]
fn a_key_deleted_and_upserted_in_one_patch_is_a_fresh_row() {
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [row("a"), row("b")]).unwrap();
    panel.set_value("t", vec!["a"]).unwrap();
    panel.patch_table("t", [row("c")], [] as [&str; 0]).unwrap();
    // a stood before the patch it would merge into: deleted and upserted it is
    // a new row, which loses its selection, so the patch stays its own.
    panel.patch_table("t", [row("a")], ["a"]).unwrap();
    // A run holding such a row takes nothing more: merged, a later patch
    // would still delete and upsert a, but it is a row removed and present.
    panel.patch_table("t", [row("d")], [] as [&str; 0]).unwrap();
    // A row the run itself added stood before none of it: deleted and
    // upserted again, it merges.
    panel.patch_table("t", [row("e")], [] as [&str; 0]).unwrap();
    panel.patch_table("t", [row("e")], ["e"]).unwrap();
    assert_eq!(
        flat(&ctx),
        [
            ("set", strings(&["a", "b"])),
            ("value", strings(&["a"])),
            ("patch", strings(&["c"])),
            ("patch", strings(&["-a", "a"])),
            ("patch", strings(&["-e", "d", "e"])),
        ]
    );
    // Through the host: a is unselected, exactly as in order.
    let mut host = HostTable::default();
    for (_, op) in flat_ops(&ctx) {
        host.apply(&op, 3, false);
    }
    assert!(host.selection.is_empty(), "{:?}", host.selection);
    assert_eq!(host.keys, strings(&["b", "c", "a", "d", "e"]));
}

/// Every pending op, as the host applies it.
fn flat_ops(ctx: &Context) -> Vec<(String, pb::patch_op::Op)> {
    ops(ctx)
        .into_iter()
        .map(|op| (op.widget_id, op.op.unwrap()))
        .collect()
}

#[test]
fn a_burst_of_a_thousand_small_patches_is_never_refused() {
    // Merging: after a statement, with no selection ever sent.
    let (first, _peer) = runtime();
    let ctx = first.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [row("a")]).unwrap();
    for i in 0..1000 {
        panel
            .patch_table("t", [row(&format!("k{i}"))], [] as [&str; 0])
            .unwrap();
    }
    let shapes = flat(&ctx);
    assert_eq!(
        shapes.len(),
        2,
        "folded into one patch behind the statement"
    );
    assert_eq!(shapes[1].1.len(), 1000);
    // Not merging: each re-states a row as a fresh one, or no statement has
    // gone this session. They go in order, many to a message, never refused.
    for statement in [true, false] {
        let (rt, _peer) = runtime();
        let ctx = rt.context();
        let panel = ctx.panel("p");
        if statement {
            panel.set_table("t", [row("a")]).unwrap();
        }
        for _ in 0..1000 {
            panel.patch_table("t", [row("a")], ["a"]).unwrap();
        }
        let messages = pending(&ctx);
        assert_eq!(messages.len(), 4, "{statement}");
        assert_eq!(ops(&ctx).len(), 1000 + statement as usize);
    }
}

#[test]
fn a_full_message_goes_as_it_is_and_the_next_op_starts_another() {
    let (first, _peer) = runtime();
    let ctx = first.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [] as [TableRow; 0]).unwrap();
    panel.set_table("t", [] as [TableRow; 0]).unwrap();
    // Each patch is about 300 KB: three fold into one op, the fourth would
    // take the message past 1 MiB and starts the next.
    let mut expected = Vec::new();
    for p in 0..10 {
        let rows: Vec<TableRow> = (0..8000)
            .map(|i| TableRow::new(format!("{p}-{i:05}"), [Cell::from("y".repeat(24))]))
            .collect();
        expected.extend(rows.iter().map(|r| r.key().to_owned()));
        panel.patch_table("t", rows, [] as [&str; 0]).unwrap();
    }
    let messages = pending(&ctx);
    assert!(messages.len() > 2, "{}", messages.len());
    let mut all = Vec::new();
    for op in ops(&ctx) {
        all.extend(keys(&op).1.into_iter().map(str::to_owned));
    }
    assert_eq!(all, expected);
    // The ops count is a bound as well: 300 ops that cannot merge make two
    // messages, 256 and 45 (the statement's).
    let (rt, _peer) = runtime();
    let ctx = rt.context();
    let panel = ctx.panel("p");
    panel.set_table("t", [row("a")]).unwrap();
    for _ in 0..300 {
        panel.patch_table("t", [row("a")], ["a"]).unwrap();
    }
    let counts: Vec<_> = pending(&ctx).iter().map(|p| p.ops.len()).collect();
    assert_eq!(counts, [256, 45]);
}

#[test]
fn an_unstated_table_may_hold_any_row_before_the_first_statement() {
    // A relaunched panel keeps its rows, so before this session states the
    // table any key may stand there, selected. Deleted and stated again it is
    // a fresh row: the ops never drain as the one statement that would keep
    // it selected.
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.patch_table("t", [] as [TableRow; 0], ["a"]).unwrap();
    panel.set_table("t", [row("a")]).unwrap();
    panel.set_table("t", [row("a")]).unwrap();
    // A row the second statement's run adds merges into it: whatever stood
    // before, the first statement has already taken it away.
    panel.patch_table("t", [row("b")], [] as [&str; 0]).unwrap();
    assert_eq!(
        flat(&ctx),
        [
            ("patch", strings(&["-a"])),
            ("set", strings(&["a"])),
            ("set", strings(&["a", "b"])),
        ]
    );
    // Through the host, from a relaunched panel showing a and b selected:
    // nothing stays selected, as in order.
    let mut host = HostTable::default();
    host.apply(
        &pb::patch_op::Op::SetTable(pb::TableRows {
            rows: vec![raw_row("a", 0), raw_row("b", 0)],
        }),
        3,
        true,
    );
    host.selection = strings(&["a", "b"]);
    for (_, op) in flat_ops(&ctx) {
        host.apply(&op, 3, false);
    }
    assert!(host.selection.is_empty(), "{:?}", host.selection);
}

#[test]
fn a_selection_on_a_declared_table_keeps_its_order_before_any_table_op() {
    // The panel declares the table, and no table op has gone this session.
    // A selection is still the table's: kept in order, never superseded by a
    // later one the host may refuse.
    let (runtime, _peer) = runtime();
    let ctx = runtime.context();
    let panel = ctx.panel("p");
    panel.set_value("t", vec!["k3"]).unwrap();
    panel.set_value("t", vec!["k2"]).unwrap();
    assert_eq!(
        shapes(&ctx),
        [vec![
            ("value", strings(&["k3"])),
            ("value", strings(&["k2"]))
        ]]
    );
    // Through the host, from a relaunched panel holding k3 but not k2: k2 is
    // refused and k3 stays selected, as in order.
    let mut host = HostTable::default();
    host.apply(
        &pb::patch_op::Op::SetTable(pb::TableRows {
            rows: vec![raw_row("k1", 0), raw_row("k3", 0)],
        }),
        3,
        false,
    );
    for (_, op) in flat_ops(&ctx) {
        host.apply(&op, 3, false);
    }
    assert_eq!(host.selection, strings(&["k3"]));
}
