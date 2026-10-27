use super::*;
use std::collections::HashMap;
use vizi_doorbell::stage::{ParamSpec, ParamType, ParamValue, Params};
type ParameterPlan =
    dyn Fn(&pb::StageShape, &Params) -> control::Result<pb::StagePlanned> + Send + Sync;
struct ParameterStage {
    plan: Box<ParameterPlan>,
    process: Box<Process>,
}
impl Stage for ParameterStage {
    fn plan(
        &self,
        _: &Context,
        input: &pb::StageShape,
        params: &Params,
    ) -> control::Result<pb::StagePlanned> {
        (self.plan)(input, params)
    }
    fn process(
        &self,
        ctx: &Context,
        arm: &pb::StageArm,
        slot: &mut Slot,
    ) -> control::Result<StageOutcome> {
        (self.process)(ctx, arm, slot)
    }
}
fn declarations() -> Vec<ParamSpec> {
    vec![
        ParamSpec::new(
            "sigma",
            ParamType::Float {
                value: 2.,
                min: Some(0.5),
                max: Some(50.),
            },
        ),
        ParamSpec::new(
            "passes",
            ParamType::Int {
                value: 1,
                min: Some(1),
                max: Some(9007199254740993),
            },
        ),
        ParamSpec::new("clip", ParamType::Bool { value: false }),
        ParamSpec::new(
            "label",
            ParamType::String {
                value: "é".into(),
                max_length: Some(2),
            },
        ),
        ParamSpec::new(
            "text",
            ParamType::String {
                value: "".into(),
                max_length: None,
            },
        ),
        ParamSpec::new(
            "mode",
            ParamType::Choice {
                value: "reflect".into(),
                options: vec!["reflect".into(), "constant".into()],
            },
        ),
    ]
}
fn wire(sigma: f64) -> HashMap<String, pb::Value> {
    use pb::value::Kind::*;
    [
        ("sigma", F(sigma)),
        ("passes", I(1)),
        ("clip", B(false)),
        ("label", S("é".into())),
        ("text", S("".into())),
        ("mode", S("reflect".into())),
    ]
    .into_iter()
    .map(|(key, kind)| (key.into(), pb::Value { kind: Some(kind) }))
    .collect()
}
async fn setup(stage: ParameterStage) -> Session {
    let mut builder = PluginBuilder::new("test.params", "Params", "1");
    let mut spec = StageSpec::new("edit");
    spec.params = declarations();
    builder.stage(spec, Arc::new(stage));
    let plugin = builder.build().unwrap();
    let declared = &plugin.manifest().value()["contributions"]["stages"][0]["params"];
    assert_eq!(declared[0]["name"], "sigma");
    assert_eq!(declared[0]["value"], 2.);
    assert_eq!(declared[1]["max"], 9007199254740993_i64);
    let mut s = Session::with_runtime(
        |bell, listener| plugin.into_runtime(bell, listener),
        1,
        0,
        "Hello from params host",
    )
    .await
    .unwrap();
    s.recv().await;
    s
}
async fn request_plan(s: &mut Session, params: HashMap<String, pb::Value>) -> pb::PluginMsg {
    s.send(
        "plan-params",
        Host::PlanStage(pb::PlanStage {
            stage_id: "edit".into(),
            input: Some(input()),
            params,
        }),
    )
    .await;
    let reply = s.recv().await;
    assert_eq!(reply.id, "plan-params");
    reply
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn two_positions_keep_their_values_plans_and_revisions() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let observations = seen.clone();
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, params| {
            assert_eq!(params["clip"], ParamValue::Bool(false));
            assert_eq!(params["mode"], ParamValue::Choice("reflect".into()));
            let mut p = identity_plan(i)?;
            if params["sigma"] == ParamValue::Float(5.) {
                p.output.as_mut().unwrap().x = 2;
            }
            Ok(p)
        }),
        process: Box::new(move |_, a, slot| {
            let ParamValue::Float(sigma) = slot.params()["sigma"] else {
                unreachable!()
            };
            observations.lock().unwrap().push((a.position, sigma));
            let Voxel::U8(v) = slot.read_voxel(7, [0, 0, 0])? else {
                unreachable!()
            };
            slot.write_voxel(7, [0, 0, 0], Voxel::U8(v + sigma as u8))?;
            slot.layout.allocation[2] = a.output.as_ref().unwrap().x;
            slot.extent[2][1] = slot.layout.allocation[2];
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    // Match the reference's test: plan both positions before granting/arming.
    let mut arms = Vec::new();
    for (position, sigma) in [(1, 2.), (2, 5.)] {
        let params = wire(sigma);
        let Some(Plugin::StagePlanned(p)) = request_plan(&mut s, params.clone()).await.msg else {
            panic!("plan failed")
        };
        let mut a = arrangement(input(), position);
        a.output = p.output;
        a.params = params;
        arms.push(a);
    }
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for a in &arms {
        arm(&mut s, a.clone()).await;
    }
    ring.fill(0);
    ring.hand(0, 1, 37, 1);
    ring.wait_position(0, 1, 37, &[3]).await;
    assert_eq!(*seen.lock().unwrap(), [(1, 2.), (2, 5.)]);
    assert_eq!(ring.u32(0, 48), 2);
    let mut byte = [0];
    ring.file.read_exact_at(&mut byte, 64 + 320).unwrap();
    assert_eq!(byte, [8]);
    // Replanning cannot mutate the first arm. Changing params requires rev2.
    assert!(matches!(
        request_plan(&mut s, wire(3.)).await.msg,
        Some(Plugin::StagePlanned(_))
    ));
    arm(&mut s, arms[0].clone()).await;
    let mut replacement = arms[0].clone();
    replacement.params = wire(3.);
    s.send("", Host::StageArm(replacement.clone())).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 19));
    replacement.revision = 2;
    arm(&mut s, replacement).await;
    ring.fill(0);
    ring.hand(0, 2, 37, 1);
    ring.wait_position(0, 2, 37, &[3]).await;
    assert_eq!(*seen.lock().unwrap(), [(1, 2.), (2, 5.), (1, 3.), (2, 5.)]);
    s.barrier().await;
    s.stop().await;
}

fn invalid_values() -> Vec<HashMap<String, pb::Value>> {
    use pb::value::Kind::*;
    let mut cases = Vec::new();
    let mut missing = wire(2.);
    missing.remove("clip");
    cases.push(missing);
    let mut extra = wire(2.);
    extra.insert(
        "other".into(),
        pb::Value {
            kind: Some(B(true)),
        },
    );
    cases.push(extra);
    for (key, kind) in [
        ("sigma", Some(I(2))),
        ("passes", Some(F(1.))),
        ("clip", Some(I(0))),
        ("mode", Some(B(true))),
        ("label", Some(I(1))),
        ("label", None),
        ("label", Some(S("éx".into()))),
        ("label", Some(S("e\u{301}".into()))),
        ("text", Some(S("é".repeat(2048) + "x"))),
        ("clip", None),
        ("mode", Some(S("Reflect".into()))),
        ("mode", Some(S("wrap".into()))),
        ("sigma", Some(F(0.49))),
        ("sigma", Some(F(50.1))),
        ("sigma", Some(F(f64::NAN))),
        ("sigma", Some(F(f64::INFINITY))),
        ("sigma", Some(F(f64::NEG_INFINITY))),
        ("passes", Some(I(0))),
        ("passes", Some(I(9007199254740994))),
    ] {
        let mut value = wire(2.);
        value.insert(key.into(), pb::Value { kind });
        cases.push(value);
    }
    cases
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn strings_are_exact_utf8_in_plans_and_per_arm_snapshots() {
    let seen = Arc::new(Mutex::new(Vec::new()));
    let observations = seen.clone();
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, params| {
            assert_eq!(params["text"], ParamValue::String("é".repeat(2048)));
            identity_plan(i)
        }),
        process: Box::new(move |_, _, slot| {
            observations
                .lock()
                .unwrap()
                .push(slot.params()["label"].clone());
            assert_eq!(slot.params()["text"], ParamValue::String("é".repeat(2048)));
            Ok(StageOutcome::Processed)
        }),
    })
    .await;
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    for (n, value) in ["", "é", " e", "E "].into_iter().enumerate() {
        let mut params = wire(2.);
        params.insert(
            "label".into(),
            pb::Value {
                kind: Some(pb::value::Kind::S(value.into())),
            },
        );
        params.insert(
            "text".into(),
            pb::Value {
                kind: Some(pb::value::Kind::S("é".repeat(2048))),
            },
        );
        assert!(matches!(
            request_plan(&mut s, params.clone()).await.msg,
            Some(Plugin::StagePlanned(_))
        ));
        let mut a = arrangement(input(), 1);
        a.params = params;
        a.revision = n as u32 + 1;
        arm(&mut s, a).await;
        ring.fill(0);
        ring.hand(0, n as u32 + 1, 37, 1);
        ring.wait_position(0, n as u32 + 1, 37, &[2]).await;
    }
    assert_eq!(
        *seen.lock().unwrap(),
        ["", "é", " e", "E "].map(|s| ParamValue::String(s.into()))
    );
    // Same byte count but different text cannot reuse an answered plan.
    let mut unplanned = arrangement(input(), 3);
    unplanned.params = wire(2.);
    unplanned.params.insert(
        "label".into(),
        pb::Value {
            kind: Some(pb::value::Kind::S("É".into())),
        },
    );
    unplanned.params.insert(
        "text".into(),
        pb::Value {
            kind: Some(pb::value::Kind::S("é".repeat(2048))),
        },
    );
    s.send("", Host::StageArm(unplanned)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn bad_values_are_rejected_on_both_messages_before_author_code() {
    // One session per case keeps each site-32 report independent of rate limiting.
    for params in invalid_values() {
        let mut s = setup(ParameterStage {
            plan: Box::new(|_, _| panic!("invalid params reached author")),
            process: Box::new(|_, _, _| panic!("invalid arm ran")),
        })
        .await;
        assert!(
            matches!(request_plan(&mut s, params.clone()).await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::InvalidArgument as i32)
        );
        let ring = Ring::new(&s);
        install_ring(&mut s).await;
        let mut a = arrangement(input(), 1);
        // A Value with no kind is what prost makes of a later minor's arm:
        // the arm is refused unreported, as an unknown dtype is (see
        // an_unknown_value_kind_refuses_the_arm_alone_and_the_plan_stays_planned).
        let kindless = params.values().any(|v| v.kind.is_none());
        a.params = params;
        s.send("", Host::StageArm(a)).await;
        if !kindless {
            assert!(
                matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32 && v.detail.starts_with("stage param"))
            );
        }
        s.barrier().await; // No acknowledgement was sent.
        ring.fill(0);
        ring.hand(0, 1, 37, 1);
        tokio::time::sleep(Duration::from_millis(15)).await;
        assert_eq!(ring.u32(0, 8), 0);
        s.stop().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn numeric_boundaries_are_inclusive_and_an_arm_must_match_its_planned_params() {
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, _| identity_plan(i)),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    for (position, sigma, passes) in [(1, 0.5, 1), (3, 50., 9007199254740993)] {
        let mut params = wire(sigma);
        params.insert(
            "passes".into(),
            pb::Value {
                kind: Some(pb::value::Kind::I(passes)),
            },
        );
        assert!(matches!(
            request_plan(&mut s, params.clone()).await.msg,
            Some(Plugin::StagePlanned(_))
        ));
        let mut a = arrangement(input(), position);
        a.params = params;
        arm(&mut s, a).await;
    }
    let mut unplanned = arrangement(input(), 5);
    unplanned.params = wire(4.);
    s.send("", Host::StageArm(unplanned)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32));
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn undeclared_params_are_rejected_even_on_a_parameterless_stage() {
    let mut s = session(Handler {
        plan: Box::new(identity_plan),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    assert!(
        matches!(request_plan(&mut s, wire(2.)).await.msg, Some(Plugin::Error(e)) if e.kind == pb::error::Kind::InvalidArgument as i32)
    );
    let ring = Ring::new(&s);
    install_ring(&mut s).await;
    let mut a = arrangement(input(), 1);
    a.params = wire(2.);
    s.send("", Host::StageArm(a)).await;
    assert!(matches!(s.recv().await.msg, Some(Plugin::ContractViolation(v)) if v.r#where == 32));
    drop(ring);
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_unknown_value_kind_refuses_the_arm_alone_and_the_plan_stays_planned() {
    use prost::Message;
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, _| identity_plan(i)),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    assert!(matches!(
        request_plan(&mut s, wire(2.)).await.msg,
        Some(Plugin::StagePlanned(_))
    ));
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    let mut a = arrangement(input(), 1);
    a.params = wire(2.);
    // A later minor's Value arm (field 7, carrying 08 01) in place of `f`:
    // prost skips it, so the Value decodes with no kind. (Field 6 is
    // `strings`.)
    let later = pb::Value::decode(&[0x3a, 0x02, 0x08, 0x01][..]).unwrap();
    assert!(later.kind.is_none());
    let mut refused = a.clone();
    refused.params.insert("sigma".into(), later);
    s.send("", Host::StageArm(refused)).await;
    // Neither acknowledged nor reported: the barrier's reply is next.
    s.barrier().await;
    // The plan stays planned: the arm with a known kind is acknowledged.
    arm(&mut s, a).await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_unusable_first_arm_was_received_so_revision_2_is_not_first() {
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, _| identity_plan(i)),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    let mut a = arrangement(input(), 1);
    a.params = wire(2.);
    // Not yet planned: refused as STAGE_ARM_REFUSED, not installed.
    s.send("", Host::StageArm(a.clone())).await;
    assert!(matches!(
        s.recv().await.msg,
        Some(Plugin::ContractViolation(v))
            if v.r#where == pb::ViolationSite::StageArmRefused as i32
    ));
    assert!(matches!(
        request_plan(&mut s, wire(2.)).await.msg,
        Some(Plugin::StagePlanned(_))
    ));
    // Revision 2 is acknowledged with no first-revision report.
    a.revision = 2;
    arm(&mut s, a).await;
    s.barrier().await;
    s.stop().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_refused_first_arm_was_received_so_revision_2_is_not_first() {
    use prost::Message;
    let mut s = setup(ParameterStage {
        plan: Box::new(|i, _| identity_plan(i)),
        process: Box::new(|_, _, _| Ok(StageOutcome::Processed)),
    })
    .await;
    assert!(matches!(
        request_plan(&mut s, wire(2.)).await.msg,
        Some(Plugin::StagePlanned(_))
    ));
    let _ring = Ring::new(&s);
    install_ring(&mut s).await;
    let mut a = arrangement(input(), 1);
    a.params = wire(2.);
    let mut refused = a.clone();
    refused.params.insert(
        "sigma".into(),
        pb::Value::decode(&[0x3a, 0x02, 0x08, 0x01][..]).unwrap(),
    );
    s.send("", Host::StageArm(refused)).await;
    s.barrier().await;
    // The re-arm at revision 2 is acknowledged with no first-revision report:
    // the refused revision 1 was received.
    a.revision = 2;
    arm(&mut s, a.clone()).await;
    s.barrier().await;
    // A position whose first arm received is revision 2 is still reported.
    a.position = 2;
    s.send("", Host::StageArm(a)).await;
    // The report is queued, the acknowledgement sent directly: either order.
    let (mut reported, mut acknowledged) = (false, false);
    for _ in 0..2 {
        match s.recv().await.msg {
            Some(Plugin::ContractViolation(v))
                if v.r#where == pb::ViolationSite::ArrangementFirstRevision as i32 =>
            {
                reported = true
            }
            Some(Plugin::StageArmAck(a)) if a.position == 2 && a.revision == 2 => {
                acknowledged = true
            }
            other => panic!("unexpected {other:?}"),
        }
    }
    assert!(reported && acknowledged);
    s.stop().await;
}
